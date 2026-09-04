from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import Counter
from typing import Awaitable, Callable
from urllib.parse import urlencode

import websockets

from server.services.stt import STTConfig, deepgram_language_option

logger = logging.getLogger(__name__)

DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"
STREAM_RESTART_BACKOFF_S = 0.5
MAX_STREAM_RESTART_BACKOFF_S = 5.0
DEEPGRAM_PING_INTERVAL_S = 10
DEEPGRAM_PING_TIMEOUT_S = 5


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
        self._ws = None
        self._stt_config: STTConfig = STTConfig()
        self._startup_error: str = ""
        self._stream_restart_count: int = 0
        self._stream_error_count: int = 0
        self._last_stream_end_reason: str = ""
        self._response_count: int = 0
        # Deepgram's start timestamps reset after reconnect, so keep a running
        # offset to preserve sermon-relative timing.
        self._stream_offset: float = 0.0
        self._last_stream_audio_end: float = 0.0

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
        self._stream_offset = 0.0
        self._last_stream_audio_end = 0.0
        ready: asyncio.Event = asyncio.Event()
        self._task = asyncio.create_task(self._run(glossary, sample_rate, ready, self._stt_config, api_key))
        await ready.wait()
        if self._ws is None:
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
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
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
        self._ws = None
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
        url = _build_deepgram_socket_url(stt_config, sample_rate, glossary)
        try:
            while not self._stopping():
                try:
                    async with websockets.connect(
                        url,
                        additional_headers={"Authorization": f"Token {api_key}"},
                        ping_interval=DEEPGRAM_PING_INTERVAL_S,
                        ping_timeout=DEEPGRAM_PING_TIMEOUT_S,
                    ) as ws:
                        self._ws = ws
                        if not ready.is_set():
                            ready.set()
                        sender_task = asyncio.create_task(self._send_audio_loop(ws))
                        try:
                            async for raw in ws:
                                self._response_count += 1
                                if self._stopping():
                                    break
                                await self._handle_raw(raw)
                        finally:
                            sender_task.cancel()
                            try:
                                await sender_task
                            except asyncio.CancelledError:
                                pass
                    if self._stopping():
                        break
                    self._stream_restart_count += 1
                    self._last_stream_end_reason = "closed"
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
                finally:
                    self._accumulate_stream_offset()
                    self._ws = None
                if self._stopping():
                    break
                await self._sleep_with_stop(restart_backoff_s)
                restart_backoff_s = min(restart_backoff_s * 2, MAX_STREAM_RESTART_BACKOFF_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[deepgram] Stream error for church %s: %s", self._church_id, exc)
            self._startup_error = str(exc)
            self._ws = None
            if not ready.is_set():
                ready.set()
        finally:
            if not ready.is_set():
                ready.set()

    async def _send_audio_loop(self, ws) -> None:
        while self._audio_queue is not None:
            chunk = await self._audio_queue.get()
            if chunk is None:
                try:
                    await ws.send(json.dumps({"type": "Finalize"}))
                    await ws.send(json.dumps({"type": "CloseStream"}))
                except Exception:
                    return
                return
            try:
                await ws.send(chunk)
            except Exception:
                return

    async def _handle_raw(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            return
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return

        message_type = str(message.get("type") or "")
        if message_type == "UtteranceEnd":
            if self._on_utterance_end:
                await self._on_utterance_end()
            return
        if message_type in {"Metadata", "SpeechStarted"}:
            return
        if message_type == "Error":
            logger.error("[deepgram] Server error for church %s: %s", self._church_id, message)
            return
        if message_type != "Results":
            return
        await self._handle_transcript(message)

    async def _handle_transcript(self, message: dict) -> None:
        try:
            alternatives = list((((message.get("channel") or {}).get("alternatives")) or []))
            if not alternatives:
                return
            alt = alternatives[0] or {}
            text = str(alt.get("transcript") or "").strip()
            if not text:
                return

            words = list(alt.get("words") or [])
            detected_languages = _deepgram_detected_languages(alt)
            detected_language = detected_languages[0] if detected_languages else ""
            avg_confidence = _deepgram_average_confidence(alt, words)
            raw_start = _raw_audio_start_s(message, words)
            raw_end = _raw_audio_end_s(message, words)
            audio_start = raw_start + self._stream_offset
            audio_end = raw_end + self._stream_offset
            self._last_stream_audio_end = max(self._last_stream_audio_end, raw_end)
            stt_meta = {
                "avg_confidence": avg_confidence,
                "word_count": len(words),
                "confidence_threshold": self._stt_config.confidence_hold_threshold,
                "low_confidence": avg_confidence > 0
                and avg_confidence < self._stt_config.confidence_hold_threshold,
                "detected_language": detected_language,
                "detected_languages": detected_languages,
                "segment_language_mode": _segment_language_mode(detected_language, detected_languages),
                "speech_final": bool(message.get("speech_final")),
                "result_start_s": round(audio_start, 4),
                "result_end_s": round(audio_end, 4),
            }
            stt_meta.update(_deepgram_speaker_metadata(words))

            if bool(message.get("is_final")):
                await self._on_final(text, audio_start, audio_end, stt_meta)
                if stt_meta["speech_final"] and self._on_utterance_end:
                    await self._on_utterance_end()
            else:
                await self._on_interim(text, stt_meta)
        except Exception as exc:
            logger.error("[deepgram] Result parse error for church %s: %s", self._church_id, exc)

    def get_stats(self) -> dict:
        return {
            "provider": "deepgram",
            "stream_restart_count": self._stream_restart_count,
            "stream_error_count": self._stream_error_count,
            "last_stream_end_reason": self._last_stream_end_reason,
            "response_count": self._response_count,
            "task_done": bool(self._task.done()) if self._task else False,
        }

    def _accumulate_stream_offset(self) -> None:
        self._stream_offset += self._last_stream_audio_end
        self._last_stream_audio_end = 0.0

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
    endpointing_ms = _deepgram_endpointing_ms(stt_config)
    options: dict[str, object] = {
        "model": stt_config.model,
        "language": deepgram_language_option(stt_config),
        "encoding": "linear16",
        "sample_rate": sample_rate,
        "channels": 1,
        "interim_results": stt_config.interim_results,
        "endpointing": endpointing_ms,
        "utterance_end_ms": stt_config.utterance_end_ms,
        "vad_events": stt_config.vad_events,
        "smart_format": stt_config.smart_format,
        "punctuate": stt_config.punctuate,
    }
    if stt_config.diarization_enabled:
        options["diarize"] = True

    keyterms = _deepgram_keyterms(glossary)
    if keyterms:
        options["keyterms"] = keyterms
    return options


def _build_deepgram_socket_url(
    stt_config: STTConfig,
    sample_rate: int,
    glossary: dict[str, int],
) -> str:
    options = _build_deepgram_listen_options(stt_config, sample_rate, glossary)
    params: list[tuple[str, str]] = []
    for key, value in options.items():
        if key == "keyterms":
            for term in value if isinstance(value, list) else []:
                cleaned = str(term).strip()
                if cleaned:
                    params.append(("keyterms", cleaned))
            continue
        if isinstance(value, bool):
            params.append((key, "true" if value else "false"))
            continue
        params.append((key, str(value)))
    return f"{DEEPGRAM_WS_URL}?{urlencode(params)}"


def _deepgram_keyterms(glossary: dict[str, int], limit: int = 50) -> list[str]:
    ranked = sorted(
        (
            (str(term).strip(), int(weight or 0))
            for term, weight in dict(glossary or {}).items()
            if str(term).strip()
        ),
        key=lambda item: (-item[1], item[0].lower()),
    )
    seen: set[str] = set()
    keyterms: list[str] = []
    for term, _weight in ranked:
        normalized = term.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        keyterms.append(term)
        if len(keyterms) >= limit:
            break
    return keyterms


def _deepgram_endpointing_ms(stt_config: STTConfig) -> int:
    configured = int(getattr(stt_config, "endpointing_ms", 0) or 0)
    if configured > 0:
        return configured
    if deepgram_language_option(stt_config) == "multi":
        return 100
    return 300


def _deepgram_detected_languages(alt: dict) -> list[str]:
    return [
        str(code).strip()
        for code in list(alt.get("languages") or [])
        if str(code).strip()
    ]


def _deepgram_average_confidence(alt: dict, words: list[dict]) -> float:
    confidences = [
        float((word or {}).get("confidence") or 0.0)
        for word in words
        if float((word or {}).get("confidence") or 0.0) > 0
    ]
    if confidences:
        return sum(confidences) / len(confidences)
    return float(alt.get("confidence") or 0.0)


def _raw_audio_start_s(message: dict, words: list[dict]) -> float:
    if words:
        return float((words[0] or {}).get("start") or 0.0)
    return float(message.get("start") or 0.0)


def _raw_audio_end_s(message: dict, words: list[dict]) -> float:
    if words:
        return float((words[-1] or {}).get("end") or 0.0)
    start = float(message.get("start") or 0.0)
    duration = float(message.get("duration") or 0.0)
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


def _deepgram_word_speaker(word: dict) -> int:
    speaker = (word or {}).get("speaker")
    if speaker is None:
        return 0
    return int(float(speaker)) + 1


def _deepgram_word_text(word: dict) -> str:
    punctuated = str((word or {}).get("punctuated_word") or "").strip()
    if punctuated:
        return punctuated
    return str((word or {}).get("word") or "").strip()


def _build_speaker_segments(words: list[dict]) -> list[dict]:
    segments: list[dict] = []
    current: dict | None = None

    for index, word in enumerate(words):
        text = _deepgram_word_text(word)
        if not text:
            continue
        speaker = _deepgram_word_speaker(word)
        start_s = float((word or {}).get("start") or 0.0)
        end_s = float((word or {}).get("end") or 0.0)
        confidence = float((word or {}).get("confidence") or 0.0)

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


def _deepgram_speaker_metadata(words: list[dict]) -> dict:
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
