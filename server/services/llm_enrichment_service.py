import asyncio
import json
import logging
import os
import re
from typing import Callable, Awaitable, TYPE_CHECKING

import anthropic

from server.db.verses import save_verse_detection, save_verse_suggestions

if TYPE_CHECKING:
    from server.services.topic_tracker import TopicTracker

logger = logging.getLogger(__name__)

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
MAX_ENRICHMENT_TOKENS = 600

_SYSTEM_PROMPT_BASE = """\
You are a bilingual (Spanish/English) theological assistant helping a live church sermon translation system.

{glossary_block}

Your job is to analyze one sentence from a live Spanish sermon and return a JSON object with three fields.

RULES:
1. Output ONLY valid JSON. No prose, no markdown fences, no code blocks.
2. improved_translation: provide a better English rendering than the Google Translate output if needed. \
Preserve the preaching register — declarative, present tense, active voice where natural. \
If Google's translation is already excellent, return it unchanged.
3. verse_detected: if the speaker explicitly cites a Bible reference (e.g. "Juan 3:16") OR clearly \
quotes a well-known verse, populate this object. Otherwise return null. \
Never hallucinate references — if uncertain, return null.
4. verse_suggestions: based on the theological theme of this sentence and the sermon context, suggest \
1–3 related Bible verses the congregation would find meaningful. Use NIV text for canonical_english. \
Return an empty array [] if the sentence is procedural or non-theological.
5. For verse references use English book names (e.g. "John", "Romans", "Revelation").
6. Infer chapter/verse from quoted text only when highly confident.

JSON schema (return exactly this shape):
{
  "improved_translation": "string",
  "verse_detected": {
    "book": "string",
    "chapter": integer,
    "verse_start": integer,
    "verse_end": integer | null,
    "spanish_text": "string",
    "canonical_english": "string",
    "reference": "string",
    "confidence": "explicit" | "quoted"
  } | null,
  "verse_suggestions": [
    {
      "reference": "string",
      "canonical_english": "string",
      "relevance_note": "string"
    }
  ]
}\
"""


def _build_system_prompt(church_terms: dict[str, str]) -> str:
    if church_terms:
        lines = "\n".join(f"  {es} → {en}" for es, en in church_terms.items())
        glossary_block = f"THEOLOGICAL GLOSSARY — always use these exact translations:\n{lines}"
    else:
        glossary_block = ""
    return _SYSTEM_PROMPT_BASE.replace("{glossary_block}", glossary_block)


def _build_user_message(
    spanish: str,
    google_english: str,
    topic_context: str,
) -> str:
    parts: list[str] = []
    if topic_context:
        parts.append(f"[SERMON CONTEXT]\n{topic_context}")
    parts.append(f"[SOURCE — Spanish original]\n{spanish}")
    parts.append(f"[GOOGLE TRANSLATION — may need improvement]\n{google_english}")
    return "\n\n".join(parts)


class LLMEnrichmentService:
    """Post-translation enrichment via Claude structured output.

    Called once per committed sentence (accurate track only).
    Always runs as a fire-and-forget background task — never blocks Google translation.

    For each sentence it:
    - Improves the Google translation where possible → fires on_translation_update
    - Detects explicit or quoted Bible verse references → fires on_verse_detected
    - Suggests 1–3 related verses based on theme → fires on_verse_suggestion
    """

    def __init__(
        self,
        church_id: str,
        church_terms: dict[str, str],
        topic_tracker: "TopicTracker",
        on_translation_update: Callable[[int, str], Awaitable[None]],
        on_verse_detected: Callable[[int, dict], Awaitable[None]],
        on_verse_suggestion: Callable[[int, list[dict]], Awaitable[None]],
        session_id: int,
    ):
        self._church_id = church_id
        self._topic_tracker = topic_tracker
        self._on_translation_update = on_translation_update
        self._on_verse_detected = on_verse_detected
        self._on_verse_suggestion = on_verse_suggestion
        self._session_id = session_id
        self._system_prompt = _build_system_prompt(church_terms)
        self._client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._tasks: list[asyncio.Task] = []

    def enrich(self, spanish: str, google_english: str, ts: int) -> asyncio.Task:
        """Schedule enrichment as a fire-and-forget task. Does not block."""
        task = asyncio.create_task(self._run_enrichment(spanish, google_english, ts))
        # Prune completed tasks to avoid unbounded list growth
        self._tasks = [t for t in self._tasks if not t.done()]
        self._tasks.append(task)
        return task

    async def _run_enrichment(self, spanish: str, google_english: str, ts: int) -> None:
        topic_context = self._topic_tracker.get_context()
        user_msg = _build_user_message(spanish, google_english, topic_context)

        try:
            response = await self._client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=MAX_ENRICHMENT_TOKENS,
                temperature=0,
                system=self._system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = response.content[0].text.strip()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[enrichment:%s] Claude call failed for ts=%d: %s", self._church_id, ts, e)
            return

        # Strip markdown fences if Claude wrapped the response
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[enrichment:%s] Could not parse JSON for ts=%d: %.120s", self._church_id, ts, raw)
            return

        if not isinstance(result, dict):
            logger.warning("[enrichment:%s] Expected JSON object for ts=%d, got %s", self._church_id, ts, type(result).__name__)
            return

        # --- Translation improvement ---
        improved = result.get("improved_translation", "").strip()
        if improved and improved != google_english:
            logger.info(
                "[enrichment:%s] Translation improved ts=%d:\n  google: %s\n     llm: %s",
                self._church_id, ts, google_english[:80], improved[:80],
            )
            try:
                await self._on_translation_update(ts, improved)
            except Exception as e:
                logger.warning("[enrichment:%s] on_translation_update failed: %s", self._church_id, e)
        else:
            logger.info("[enrichment:%s] Translation accepted ts=%d — no change", self._church_id, ts)

        # --- Verse detection ---
        verse = result.get("verse_detected")
        if verse and isinstance(verse, dict) and _is_valid_verse(verse):
            logger.info(
                "[enrichment:%s] Verse detected ts=%d: %s (%s)",
                self._church_id, ts, verse["reference"], verse["confidence"],
            )
            try:
                await self._on_verse_detected(ts, verse)
                await save_verse_detection(self._session_id, ts, verse)
            except Exception as e:
                logger.warning("[enrichment:%s] on_verse_detected failed: %s", self._church_id, e)
        else:
            logger.info("[enrichment:%s] No verse detected ts=%d", self._church_id, ts)

        # --- Verse suggestions ---
        suggestions = result.get("verse_suggestions", [])
        if isinstance(suggestions, list):
            valid = [s for s in suggestions if _is_valid_suggestion(s)]
            if valid:
                logger.info(
                    "[enrichment:%s] Verse suggestions ts=%d: %s",
                    self._church_id, ts, [s["reference"] for s in valid],
                )
                try:
                    await self._on_verse_suggestion(ts, valid)
                    await save_verse_suggestions(self._session_id, ts, valid)
                except Exception as e:
                    logger.warning("[enrichment:%s] on_verse_suggestion failed: %s", self._church_id, e)
            else:
                logger.info("[enrichment:%s] No verse suggestions ts=%d", self._church_id, ts)

    async def close(self) -> None:
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()


def _is_valid_verse(v: dict) -> bool:
    return (
        isinstance(v.get("book"), str) and v["book"]
        and isinstance(v.get("chapter"), int)
        and isinstance(v.get("verse_start"), int)
        and isinstance(v.get("reference"), str) and v["reference"]
        and isinstance(v.get("canonical_english"), str) and v["canonical_english"]
        and isinstance(v.get("spanish_text"), str)
        and v.get("confidence") in ("explicit", "quoted")
    )


def _is_valid_suggestion(s: dict) -> bool:
    return (
        isinstance(s.get("reference"), str) and s["reference"]
        and isinstance(s.get("canonical_english"), str) and s["canonical_english"]
        and isinstance(s.get("relevance_note"), str)
    )
