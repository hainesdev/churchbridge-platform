import os
import asyncio
import logging
from collections import deque
from typing import Callable, Awaitable

from openai import AsyncOpenAI

from server.services.prompt_manager import build_system_prompt

logger = logging.getLogger(__name__)

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


class TranslationService:
    """Translates Deepgram final transcripts via OpenAI streaming.

    - Maintains a 3-segment context window for coherence across utterances.
    - Streams tokens immediately to on_token callback.
    - Calls on_complete with the full translated segment when done.
    - Cancels in-flight translation if a new final segment arrives (fast speaker).
    """

    def __init__(
        self,
        church_id: str,
        church_terms: dict[str, str],
        on_token: Callable[[str], Awaitable[None]],
        on_complete: Callable[[str, str], Awaitable[None]],
    ):
        self._church_id = church_id
        self._church_terms = church_terms
        self._on_token = on_token
        self._on_complete = on_complete
        self._context: deque[str] = deque(maxlen=3)
        self._client = AsyncOpenAI()
        self._system_prompt = build_system_prompt(church_terms)
        self._active_task: asyncio.Task | None = None

    async def translate(self, spanish_text: str):
        """Called on each Deepgram is_final event."""
        # Cancel previous translation if still streaming (speaker moved on)
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()

        self._context.append(spanish_text)
        self._active_task = asyncio.create_task(
            self._stream_translation(spanish_text)
        )

    async def _stream_translation(self, text: str):
        context_str = " | ".join(self._context)
        try:
            stream = await self._client.chat.completions.create(
                model=OPENAI_MODEL,
                stream=True,
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    {"role": "user", "content": f"Context: {context_str}\nTranslate: {text}"},
                ],
                max_tokens=200,
                temperature=0.1,   # low temperature for consistency
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
