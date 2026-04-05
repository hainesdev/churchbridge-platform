import asyncio
import logging
import os
import time

import anthropic

logger = logging.getLogger(__name__)

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
SUMMARY_INTERVAL_SECS = int(os.getenv("TOPIC_SUMMARY_INTERVAL", "300"))  # 5 min default
MIN_SEGMENTS_BEFORE_SUMMARY = 8   # wait for enough speech before first summary


class TopicTracker:
    """Maintains a rolling 2-3 sentence summary of sermon content.

    Updated every SUMMARY_INTERVAL_SECS seconds via a lightweight async
    Claude Haiku call. The summary is injected into each enrichment prompt
    to resolve theological term ambiguity — e.g. whether "misión" means a
    mission trip or the Great Commission in this particular sermon.

    The tracker never blocks translation. Summary updates run as a fire-and-
    forget background task; the previous summary remains available until the
    new one completes.
    """

    def __init__(self, church_id: str, sermon_topic: str = ""):
        self._church_id = church_id
        self._sermon_topic = sermon_topic.strip()
        self._segments: list[str] = []
        # Seed summary with the sermon topic so it's useful from the first call
        self._summary: str = sermon_topic.strip()
        self._last_summary_time: float = 0.0
        self._update_task: asyncio.Task | None = None
        self._client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def add_segment(self, spanish_text: str):
        """Record a final transcript segment and schedule a summary refresh if due."""
        self._segments.append(spanish_text)
        self._maybe_schedule_update()

    def get_context(self) -> str:
        """Return the current topic summary for injection into the enrichment prompt.
        Returns the sermon topic alone if no summary has been generated yet."""
        return self._summary

    def _maybe_schedule_update(self):
        now = time.monotonic()
        enough_content = len(self._segments) >= MIN_SEGMENTS_BEFORE_SUMMARY
        interval_elapsed = (now - self._last_summary_time) >= SUMMARY_INTERVAL_SECS

        if enough_content and interval_elapsed:
            if not self._update_task or self._update_task.done():
                self._last_summary_time = now
                self._update_task = asyncio.create_task(self._update_summary())

    async def _update_summary(self):
        # Use the last ~80 segments (~5-8 minutes of speech)
        recent_text = " ".join(self._segments[-80:])
        topic_hint = f' The sermon topic is: "{self._sermon_topic}".' if self._sermon_topic else ""

        try:
            response = await self._client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=120,
                temperature=0,
                system=(
                    "You summarize live Spanish sermon transcripts for a simultaneous interpreter. "
                    "Be brief and precise. Focus on theological themes, key terms, and the "
                    "current direction of the message. Respond in English."
                ),
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"In 2-3 sentences, summarize what this pastor has been discussing, "
                            f"focusing on theological themes and any key Spanish terms used.{topic_hint}\n\n"
                            f"Transcript:\n{recent_text}"
                        ),
                    }
                ],
            )
            new_summary = response.content[0].text.strip()
            self._summary = new_summary
            logger.info("[topic] Summary updated for church %s: %s", self._church_id, new_summary[:100])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[topic] Summary update failed for church %s: %s", self._church_id, e)

    async def stop(self):
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
