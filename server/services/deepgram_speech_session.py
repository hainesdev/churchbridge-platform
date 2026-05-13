from __future__ import annotations

import asyncio
import logging
import os
from collections import Counter
from typing import Awaitable, Callable

from deepgram import AsyncDeepgramClient
from deepgram.listen.v1.types.listen_v1metadata import ListenV1Metadata
from deepgram.listen.v1.types.listen_v1results import ListenV1Results
from deepgram.listen.v1.types.listen_v1speech_started import ListenV1SpeechStarted
from deepgram.listen.v1.types.listen_v1utterance_end import ListenV1UtteranceEnd

from server.services.stt import STTConfig, deepgram_language_option

logger = logging.getLogger(__name__)
STREAM_RESTART_BACKOFF_S = 0.5
MAX_STREAM_RESTART_BACKOFF_S = 5.0


class DeepgramSpeechSession:
    """Manages a single Deepgram live-streaming STT session."""

    def __init__(
        self,
        church_id: str,
        on_interim: Callable[[str, dict], Awaitable[None]],
        on_final: Callable[[str, float, float, dict], Awaitable[None]],
        on_utterance_end: Callable[[], Awaitable[None]] | None = None,
    ):
        self._church_id = church_id
        self._on_interim = on_interim
        self._on_final = on_final
        self._on_utterance_end = on_utterance_end
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._audio_queue: asyncio.Queue[bytes | None] | None = None
        self._client: AsyncDeepgramClient | None = None
        self._stt_config: STTConfig = STTConfig()
        self._startup_error: str = ""
        self._stream_restart_count: int = 0
        self._stream_error_count: int = 0
        self._last_stream_end_reason: str = ""
        self._response_count: int = 0

    async def start(
        self,
        glossary: dict[str, int],
        sample_rate: int = 16000,
        stt_config: STTConfig | None = None,
    ) -> None:
        api_key = os.getenv("DEEPGRAM_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("Missing DEEPGRAM_API_KEY for Deepgram streaming STT")

        self._stop_event = asyncio.Event()
        self._audio_queue = asyncio.Queue()
        self._stt_config = stt_config or STTConfig()
        self._startup_error = ""
        self._stream_restart_count = 0
        self._stream_error_count = 0
        self._last_stream_end_reason = ""
        self._response_count = 0
        ready: asyncio.Event = asyncio.Event()
        self._task = asyncio.create_task(self._run(glossary, sample_rate, ready, self._stt_config, api_key))
        await ready.wait()
        if self._client is None:
            detail = f": {self._startup_error}" if self._startup_error else ""
            raise RuntimeError(f"[deepgram] Failed to connect for church {self._church_id}{detail}")
        logger.info(
            "[deepgram] Session started for church %s at %dHz (%s)",
            self._church_id,
            sample_rate,
            self._stt_config.model,
        )

    async def send(self, pcm16_bytes: bytes) -> None:
        if self._audio_queue is not None:
            await self._audio_queue.put(pcm16_bytes)

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        if self._audio_queue is not None:
            await self._audio_queue.put(None)
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=8.0)
            except asyncio.TimeoutError:
                if self._stop_event:
                    self._stop_event.set()
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._client = None
        logger.info("[deepgram] Session closed for church %s", self._church_id)

    async def _run(
        self,
        glossary: dict[str, int],
        sample_rate: int,
        ready: asyncio.Event,
        stt_config: STTConfig,
        api_key: str,
    ) -> None:
        restart_backoff_s = STREAM_RESTART_BACKOFF_S
        self._client = AsyncDeepgramClient(api_key=api_key)
        try:
            while not self._stopping():
                try:
                    async with self._client.listen.v1.connect(
                        **_build_deepgram_listen_options(stt_config, sample_rate, glossary)
                    ) as socket:
                        sender_task = asyncio.create_task(self._send_audio_loop(socket))
                        if not ready.is_set():
                            ready.set()
                        try:
                            while True:
                                response = await socket.recv()
                                self._response_count += 1
                                await self._handle_response(response)
                        finally:
                            sender_task.cancel()
                            try:
                                await sender_task
                            except asyncio.CancelledError:
                                pass
                    if self._stopping():
                        break
                    self._stream_restart_count += 1
                    self._last_stream_end_reason = "eof"
                    logger.warning(
                        "[deepgram] Stream ended unexpectedly for church %s; restarting (count=%d)",
                        self._church_id,
                        self._stream_restart_count,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._stream_error_count += 1
                    self._last_stream_end_reason = f"error:{type(exc).__name__}"
                    if not ready.is_set():
                        raise
                    logger.error(
                        "[deepgram] Stream error for church %s (restart=%d): %s",
                        self._church_id,
                        self._stream_restart_count + 1,
                        exc,
                    )
                if self._stopping():
                    break
                await self._sleep_with_stop(restart_backoff_s)
                restart_backoff_s = min(restart_backoff_s * 2, MAX_STREAM_RESTART_BACKOFF_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[deepgram] Stream error for church %s: %s", self._church_id, exc)
            self._startup_error = str(exc)
            self._client = None
            if not ready.is_set():
                ready.set()

    async def _send_audio_loop(self, socket) -> None:
        while self._audio_queue is not None:
            chunk = await self._audio_queue.get()
            if chunk is None:
                try:
                    await socket.send_finalize()
                    await socket.send_close_stream()
                except Exception:
                    return
                return
            await socket.send_media(chunk)

    async def _handle_response(self, response: object) -> None:
        if isinstance(response, ListenV1UtteranceEnd):
            if self._on_utterance_end:
                await self._on_utterance_end()
            return
        if isinstance(response, (ListenV1SpeechStarted, ListenV1Metadata)):
            return
        if not isinstance(response, ListenV1Results):
            return

        alternatives = list(getattr(getattr(response, "channel", None), "alternatives", []) or [])
        if not alternatives:
            return
        alt = alternatives[0]
        text = str(getattr(alt, "transcript", "") or "").strip()
        if not text:
            return

        words = list(getattr(alt, "words", []) or [])
        detected_languages = [str(code).strip() for code in list(getattr(alt, "languages", []) or []) if str(code).strip()]
        detected_language = detected_languages[0] if detected_languages else ""
        avg_confidence = (
            sum(float(getattr(word, "confidence", 0.0) or 0.0) for word in words) / len(words)
            if words else float(getattr(alt, "confidence", 0.0) or 0.0)
        )
        audio_start = _audio_start_s(response, words)
        audio_end = _audio_end_s(response, words)
        stt_meta = {
            "avg_confidence": avg_confidence,
            "word_count": len(words),
            "confidence_threshold": self._stt_config.confidence_hold_threshold,
            "low_confidence": avg_confidence > 0
            and avg_confidence < self._stt_config.confidence_hold_threshold,
            "detected_language": detected_language,
            "detected_languages": detected_languages,
            "segment_language_mode": _segment_language_mode(detected_language, detected_languages),
        }
        stt_meta.update(_deepgram_speaker_metadata(words))

        if bool(getattr(response, "is_final", False)):
            await self._on_final(text, audio_start, audio_end, stt_meta)
        else:
            await self._on_interim(text, stt_meta)

    def get_stats(self) -> dict:
        return {
            "provider": "deepgram",
            "stream_restart_count": self._stream_restart_count,
            "stream_error_count": self._stream_error_count,
            "last_stream_end_reason": self._last_stream_end_reason,
            "response_count": self._response_count,
            "task_done": bool(self._task.done()) if self._task else False,
        }

    def _stopping(self) -> bool:
        return bool(self._stop_event and self._stop_event.is_set())

    async def _sleep_with_stop(self, delay_s: float) -> None:
        if not self._stop_event:
            await asyncio.sleep(delay_s)
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay_s)
        except asyncio.TimeoutError:
            return


def _build_deepgram_listen_options(
    stt_config: STTConfig,
    sample_rate: int,
    glossary: dict[str, int],
) -> dict[str, object]:
    # Deepgram Nova 3 live streaming currently rejects the richer Google-style
    # websocket flags we would normally send here (interim results, VAD events,
    # utterance_end_ms, smart formatting, punctuation, diarization, and keyterms)
    # with a 400 during connection initialization in this environment. Keep the
    # live handshake to the minimal set that is known to connect so benchmark
    # comparisons measure transcription quality instead of a broken startup.
    _ = glossary
    return {
        "model": stt_config.model,
        "encoding": "linear16",
        "sample_rate": sample_rate,
        "language": deepgram_language_option(stt_config),
    }


def _audio_start_s(response: ListenV1Results, words: list) -> float:
    if words:
        return float(getattr(words[0], "start", 0.0) or 0.0)
    return float(getattr(response, "start", 0.0) or 0.0)


def _audio_end_s(response: ListenV1Results, words: list) -> float:
    if words:
        return float(getattr(words[-1], "end", 0.0) or 0.0)
    start = float(getattr(response, "start", 0.0) or 0.0)
    duration = float(getattr(response, "duration", 0.0) or 0.0)
    return max(start, start + duration)


def _language_family(code: str) -> str:
    normalized = str(code or "").strip().lower()
    if normalized.startswith("es"):
        return "es"
    if normalized.startswith("en"):
        return "en"
    return normalized.split("-", 1)[0]


def _segment_language_mode(primary_code: str, detected_codes: list[str]) -> str:
    families = {
        family
        for family in [_language_family(primary_code), *(_language_family(code) for code in detected_codes)]
        if family
    }
    if families == {"en"}:
        return "english"
    if families == {"es"}:
        return "spanish"
    if families:
        return "mixed"
    return "unknown"


def _deepgram_word_speaker(word) -> int:
    speaker = getattr(word, "speaker", None)
    if speaker is None:
        return 0
    return int(float(speaker)) + 1


def _deepgram_word_text(word) -> str:
    punctuated = str(getattr(word, "punctuated_word", "") or "").strip()
    if punctuated:
        return punctuated
    return str(getattr(word, "word", "") or "").strip()


def _build_speaker_segments(words: list) -> list[dict]:
    segments: list[dict] = []
    current: dict | None = None

    for index, word in enumerate(words):
        text = _deepgram_word_text(word)
        if not text:
            continue
        speaker = _deepgram_word_speaker(word)
        start_s = float(getattr(word, "start", 0.0) or 0.0)
        end_s = float(getattr(word, "end", 0.0) or 0.0)
        confidence = float(getattr(word, "confidence", 0.0) or 0.0)

        if current is None or current["speaker"] != speaker:
            if current is not None:
                current["text"] = " ".join(current.pop("words"))
                word_count = max(len(current.pop("confidences")), 1)
                current["avg_confidence"] = round(current["confidence_total"] / word_count, 4)
                current.pop("confidence_total")
                segments.append(current)
            current = {
                "speaker": speaker,
                "start_s": start_s,
                "end_s": end_s,
                "words": [text],
                "confidence_total": confidence,
                "confidences": [confidence],
                "word_start_index": index,
                "word_end_index": index,
            }
            continue

        current["words"].append(text)
        current["end_s"] = end_s
        current["confidence_total"] += confidence
        current["confidences"].append(confidence)
        current["word_end_index"] = index

    if current is not None:
        current["text"] = " ".join(current.pop("words"))
        word_count = max(len(current.pop("confidences")), 1)
        current["avg_confidence"] = round(current["confidence_total"] / word_count, 4)
        current.pop("confidence_total")
        segments.append(current)

    return segments


def _deepgram_speaker_metadata(words: list) -> dict:
    speaker_tags = sorted({
        _deepgram_word_speaker(word)
        for word in words
        if _deepgram_word_speaker(word) > 0
    })
    speaker_segments = _build_speaker_segments(words)
    speaker_counts = Counter(
        segment["speaker"]
        for segment in speaker_segments
        if int(segment.get("speaker", 0) or 0) > 0
    )
    dominant_speaker = speaker_counts.most_common(1)[0][0] if speaker_counts else 0
    return {
        "speaker_tags": speaker_tags,
        "speaker_count": len(speaker_tags),
        "dominant_speaker": dominant_speaker or None,
        "speaker_segments": speaker_segments,
    }
