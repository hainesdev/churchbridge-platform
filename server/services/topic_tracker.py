import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

import anthropic

logger = logging.getLogger(__name__)

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

FIRST_SUMMARY_MIN_SEGMENTS = 3    # fire once early to capture the passage announcement
MIN_SEGMENTS_BEFORE_SUMMARY = 8   # subsequent summaries require more content

# Update frequently for the first FAST_INTERVAL_LIMIT_SECS of the sermon to
# capture mode transitions quickly, then relax to avoid redundant LLM calls.
FAST_INTERVAL_SECS = 60           # first 10 minutes: update every minute
FAST_INTERVAL_LIMIT_SECS = 600    # 10 minutes
SLOW_INTERVAL_SECS = 180          # after 10 minutes: update every 3 minutes


@dataclass
class SermonContext:
    """Structured snapshot of the current sermon state, used as enrichment prompt context."""
    summary: str                          # 2-3 sentence theological summary
    current_mode: str = "exposition"      # current settled sermon mode
    key_themes: list[str] = field(default_factory=list)  # ["fellowship", "walking in light"]
    illustration_subject: str | None = None  # set only when mode == "illustration"

    def to_context_str(self) -> str:
        """Format for injection into the enrichment LLM prompt."""
        if self.illustration_subject:
            header = f"[ILLUSTRATION IN PROGRESS] {self.illustration_subject}"
            themes = f"Key themes: {', '.join(self.key_themes)}." if self.key_themes else ""
            return f"{header}\n{themes}".strip()
        themes = f" Key themes: {', '.join(self.key_themes)}." if self.key_themes else ""
        return f"{self.summary}{themes}".strip()


class TopicTracker:
    """Maintains a rolling structured summary of sermon content.

    Updated on an adaptive schedule: every 60 seconds for the first 10 minutes
    of the sermon (to capture mode transitions quickly), then every 3 minutes.
    An eager first summary fires after only 3 segments to capture the passage
    announcement before the first interval would kick in.

    The tracker accepts a current_mode hint on each add_segment call so the
    LLM can frame the summary with awareness of whether we are in exposition,
    illustration, etc. The summary is injected into each enrichment prompt as
    [SERMON CONTEXT], providing the model with the theological framing needed
    to correctly classify vocabulary and avoid false verse detections.
    """

    def __init__(self, church_id: str, sermon_topic: str = ""):
        self._church_id = church_id
        self._sermon_topic = sermon_topic.strip()
        self._segments: list[str] = []
        self._current_mode: str = "exposition"
        # Seed with sermon topic so context is useful from the very first call
        self._context = SermonContext(
            summary=sermon_topic.strip(),
            current_mode="exposition",
        )
        self._session_start: float = time.monotonic()
        self._last_summary_time: float = 0.0
        self._update_task: asyncio.Task | None = None
        self._client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def add_segment(self, spanish_text: str, mode: str = "exposition") -> None:
        """Record a final transcript segment and schedule a summary refresh if due."""
        self._segments.append(spanish_text)
        self._current_mode = mode
        self._maybe_schedule_update()

    def get_context(self) -> str:
        """Return the current context string for injection into the enrichment prompt."""
        return self._context.to_context_str()

    def get_context_obj(self) -> SermonContext:
        """Return the full structured context object."""
        return self._context

    def _interval(self) -> int:
        """Return the appropriate update interval based on elapsed session time."""
        elapsed = time.monotonic() - self._session_start
        return FAST_INTERVAL_SECS if elapsed < FAST_INTERVAL_LIMIT_SECS else SLOW_INTERVAL_SECS

    def _maybe_schedule_update(self) -> None:
        now = time.monotonic()
        n = len(self._segments)
        first_run = (self._last_summary_time == 0.0 and n >= FIRST_SUMMARY_MIN_SEGMENTS)
        subsequent = (
            n >= MIN_SEGMENTS_BEFORE_SUMMARY
            and (now - self._last_summary_time) >= self._interval()
        )

        if first_run or subsequent:
            if not self._update_task or self._update_task.done():
                self._last_summary_time = now
                self._update_task = asyncio.create_task(self._update_summary())

    async def _update_summary(self) -> None:
        # Use the last ~80 segments (~5-8 minutes of speech)
        recent_text = " ".join(self._segments[-80:])
        topic_hint = f' The sermon topic is: "{self._sermon_topic}".' if self._sermon_topic else ""
        mode_hint = f" The current sermon mode appears to be: {self._current_mode}."

        try:
            response = await self._client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=200,
                temperature=0,
                system=(
                    "You summarize live Spanish sermon transcripts for a simultaneous interpreter. "
                    "Be brief and precise. Return ONLY valid JSON — no prose, no markdown fences."
                ),
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Analyze this sermon transcript and return a JSON object with these fields:\n"
                            f"- summary: 2-3 sentences on theological themes and current direction\n"
                            f"- key_themes: array of 2-4 short theme strings\n"
                            f"- illustration_subject: if the pastor is currently telling a personal "
                            f"story or analogy, describe it in one sentence; otherwise null\n\n"
                            f"JSON schema: "
                            f'{{ "summary": "string", "key_themes": ["string"], '
                            f'"illustration_subject": "string | null" }}\n\n'
                            f"Transcript:{topic_hint}{mode_hint}\n{recent_text}"
                        ),
                    }
                ],
            )
            raw = response.content[0].text.strip()
            # Strip markdown fences if model adds them despite instructions
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()

            parsed = json.loads(raw)
            new_context = SermonContext(
                summary=parsed.get("summary", self._context.summary),
                current_mode=self._current_mode,
                key_themes=parsed.get("key_themes", []),
                illustration_subject=parsed.get("illustration_subject") or None,
            )
            self._context = new_context
            logger.info(
                "[topic] Context updated for church %s (mode=%s): %s",
                self._church_id, self._current_mode, new_context.to_context_str()[:120],
            )
        except (json.JSONDecodeError, KeyError):
            # Graceful fallback: store the raw text as a plain summary
            raw_text = response.content[0].text.strip() if 'response' in dir() else ""
            if raw_text:
                self._context = SermonContext(
                    summary=raw_text[:300],
                    current_mode=self._current_mode,
                )
            logger.warning(
                "[topic] Could not parse structured context for church %s — using plain text",
                self._church_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[topic] Context update failed for church %s: %s", self._church_id, e)

    async def stop(self) -> None:
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
