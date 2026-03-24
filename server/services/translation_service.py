import os
import asyncio
import logging
from collections import deque
from typing import Callable, Awaitable, TYPE_CHECKING

from openai import AsyncOpenAI

from server.services.prompt_manager import build_system_prompt, build_user_message

if TYPE_CHECKING:
    from server.services.topic_tracker import TopicTracker

logger = logging.getLogger(__name__)

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
CONTEXT_WINDOW = 5   # verbatim utterances kept for pronoun/term continuity


class TranslationService:
    """Translates Deepgram final transcripts via OpenAI streaming.

    Improvements over v1:
    - temperature=0 (translation is deterministic — no creative variation)
    - presence_penalty=0, frequency_penalty=0 (preserve source repetition)
    - 5-utterance verbatim context window (up from 3)
    - TopicTracker integration: rolling sermon summary injected per call
    - Two-part user message: topic context + recent utterances + source text
    - Cancels in-flight translation if a new final segment arrives
    """

    def __init__(
        self,
        church_id: str,
        church_terms: dict[str, str],
        topic_tracker: "TopicTracker",
        on_token: Callable[[str], Awaitable[None]],
        on_complete: Callable[[str, str], Awaitable[None]],
    ):
        self._church_id = church_id
        self._topic_tracker = topic_tracker
        self._on_token = on_token
        self._on_complete = on_complete
        self._context: deque[str] = deque(maxlen=CONTEXT_WINDOW)
        self._client = AsyncOpenAI()
        self._static_prompt = build_system_prompt(church_terms)
        self._active_task: asyncio.Task | None = None

    async def translate(self, spanish_text: str):
        """Called on each Deepgram is_final event."""
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()

        self._context.append(spanish_text)
        self._active_task = asyncio.create_task(
            self._stream_translation(spanish_text)
        )

    async def _stream_translation(self, text: str):
        # Build context: all utterances except the current one (which is being translated)
        recent = list(self._context)[:-1]
        topic_context = self._topic_tracker.get_context()

        user_msg = build_user_message(
            current_text=text,
            recent_utterances=recent,
            topic_context=topic_context,
        )

        try:
            stream = await self._client.chat.completions.create(
                model=OPENAI_MODEL,
                stream=True,
                messages=[
                    {"role": "system", "content": self._static_prompt},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=250,
                temperature=0,
                presence_penalty=0,
                frequency_penalty=0,
            )

            full = ""
            async for chunk in stream:
                token = chunk.choices[0].delta.content or ""
                if token:
                    full += token
                    await self._on_token(token)

            if full:
                await self._on_complete(text, full)

        except asyncio.CancelledError:
            logger.debug("[translation] Task cancelled for: %s", text[:40])
        except Exception as e:
            logger.error("[translation] Error translating '%s': %s", text[:40], e)
