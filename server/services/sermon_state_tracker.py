import logging
from collections import deque
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

SERMON_MODES = frozenset({
    "scripture", "exposition", "illustration", "application", "exhortation", "procedural"
})

# Number of consecutive matching signals required to confirm a mode change.
# Prevents a single ambiguous sentence from flipping the settled mode.
SETTLE_THRESHOLD = 3

_MODE_LABELS: dict[str, str] = {
    "scripture":    "pastor reading scripture",
    "exposition":   "pastor explaining a biblical text",
    "illustration": "pastor in a narrative illustration",
    "application":  "pastor applying text to the congregation",
    "exhortation":  "pastor in an exhortation",
    "procedural":   "procedural/logistics",
}


class SermonStateTracker:
    """Tracks the current rhetorical mode of a live sermon.

    Each sentence the LLM enrichment service processes is classified into one
    of six modes ("scripture", "exposition", "illustration", etc.). This class
    accumulates those per-sentence signals and emits a stable "settled mode"
    only after SETTLE_THRESHOLD consecutive signals agree — preventing a single
    ambiguous sentence from flipping the mode.

    The settled mode is used by LLMEnrichmentService to:
    - Gate verse suggestions (suppressed during illustration/exhortation/procedural)
    - Guard the verse scratch pad against quoted detections during narrative
    - Inject a [CURRENT MODE] context block into each enrichment prompt
    """

    def __init__(
        self,
        on_mode_change: Callable[[str, str, int], Awaitable[None]] | None = None,
    ):
        # Rolling window of the last 5 per-sentence mode signals
        self._history: deque[str] = deque(maxlen=5)
        # Mode that has been stable for >= SETTLE_THRESHOLD consecutive sentences
        self._settled_mode: str = "exposition"
        # How many sentences we have been in the current settled mode
        self._mode_sentence_count: int = 0
        # Fired when the settled mode transitions; receives (old_mode, new_mode, ts)
        self._on_mode_change = on_mode_change

    async def add_signal(self, mode: str, ts: int) -> None:
        """Record a per-sentence mode classification from the enrichment LLM.

        Updates the settled mode when the last SETTLE_THRESHOLD signals all agree
        on a different mode than the current one, then fires on_mode_change.
        """
        if mode not in SERMON_MODES:
            logger.debug("[state_tracker] Unknown mode %r at ts=%d — ignoring", mode, ts)
            return

        self._history.append(mode)

        # Check if the most recent SETTLE_THRESHOLD signals all agree
        recent = list(self._history)[-SETTLE_THRESHOLD:]
        if len(recent) == SETTLE_THRESHOLD and len(set(recent)) == 1:
            candidate = recent[0]
            if candidate != self._settled_mode:
                old_mode = self._settled_mode
                self._settled_mode = candidate
                self._mode_sentence_count = 0
                logger.info(
                    "[state_tracker] Mode settled: %s → %s (ts=%d)",
                    old_mode, candidate, ts,
                )
                if self._on_mode_change:
                    try:
                        await self._on_mode_change(old_mode, candidate, ts)
                    except Exception as e:
                        logger.warning("[state_tracker] on_mode_change failed: %s", e)

        if mode == self._settled_mode:
            self._mode_sentence_count += 1

    # --- Properties ---

    @property
    def settled_mode(self) -> str:
        """The current stable sermon mode."""
        return self._settled_mode

    @property
    def mode_sentence_count(self) -> int:
        """Number of consecutive sentences in the current settled mode."""
        return self._mode_sentence_count

    # --- Convenience helpers ---

    def is_narrative(self) -> bool:
        """True when the pastor is in a story or analogy (suppress verse detection/suggestions)."""
        return self._settled_mode == "illustration"

    def is_expository(self) -> bool:
        """True during scripture reading or verse-by-verse explanation."""
        return self._settled_mode in ("scripture", "exposition")

    def get_context_label(self) -> str:
        """Short natural-language description for injection into LLM prompts."""
        return _MODE_LABELS.get(self._settled_mode, self._settled_mode)
