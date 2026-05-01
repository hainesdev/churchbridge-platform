from __future__ import annotations

import asyncio
import inspect
import logging
import os
from typing import Awaitable, Callable

from google.api_core.client_options import ClientOptions
from google.cloud.speech_v2 import SpeechAsyncClient
from google.cloud.speech_v2.types import cloud_speech
from google.protobuf.duration_pb2 import Duration

from server.services.stt import STTConfig

logger = logging.getLogger(__name__)


class GoogleSpeechSession:
    """Manages a single Chirp 3 streaming session over Google Speech-to-Text V2."""

    def __init__(
        self,
        church_id: str,
        on_interim: Callable[[str], Awaitable[None]],
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
        self._client: SpeechAsyncClient | None = None
        self._stt_config: STTConfig = STTConfig()
        self._stream_offset: float = 0.0
        self._last_stream_audio_end: float = 0.0
        self._last_final_audio_end: float = 0.0
        self._startup_error: str = ""

    async def start(
        self,
        glossary: dict[str, int],
        sample_rate: int = 16000,
        stt_config: STTConfig | None = None,
    ) -> None:
        self._project_id = (
            os.getenv("GOOGLE_CLOUD_PROJECT")
            or os.getenv("GCLOUD_PROJECT")
            or os.getenv("GCP_PROJECT")
        )
        if not self._project_id:
            raise RuntimeError("Missing GOOGLE_CLOUD_PROJECT for Google Speech-to-Text")

        self._stop_event = asyncio.Event()
        self._audio_queue = asyncio.Queue()
        self._stt_config = stt_config or STTConfig()
        self._startup_error = ""
        ready: asyncio.Event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(glossary, sample_rate, ready, self._stt_config)
        )
        await ready.wait()
        if self._client is None:
            detail = f": {self._startup_error}" if self._startup_error else ""
            raise RuntimeError(f"[google_speech] Failed to connect for church {self._church_id}{detail}")
        logger.info(
            "[google_speech] Session started for church %s at %dHz (%s/%s)",
            self._church_id,
            sample_rate,
            self._stt_config.location,
            self._stt_config.model,
        )

    async def send(self, pcm16_bytes: bytes) -> None:
        if self._audio_queue is not None:
            await self._audio_queue.put(pcm16_bytes)

    async def stop(self) -> None:
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
        if self._client:
            close_result = self._client.transport.close()
            if inspect.isawaitable(close_result):
                await close_result
        self._task = None
        self._client = None
        logger.info("[google_speech] Session closed for church %s", self._church_id)

    async def _run(
        self,
        glossary: dict[str, int],
        sample_rate: int,
        ready: asyncio.Event,
        stt_config: STTConfig,
    ) -> None:
        try:
            endpoint = _google_api_endpoint(stt_config.location)
            self._client = SpeechAsyncClient(
                client_options=ClientOptions(api_endpoint=endpoint),
            )
            stream = await self._client.streaming_recognize(
                requests=self._request_generator(glossary, sample_rate, stt_config)
            )
            ready.set()
            async for response in stream:
                await self._handle_response(response)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[google_speech] Stream error for church %s: %s", self._church_id, e)
            self._startup_error = str(e)
            self._client = None
            if not ready.is_set():
                ready.set()

    async def _request_generator(
        self,
        glossary: dict[str, int],
        sample_rate: int,
        stt_config: STTConfig,
    ):
        recognizer = (
            stt_config.recognizer
            if stt_config.recognizer.startswith("projects/")
            else (
                f"projects/{self._project_id}/locations/{stt_config.location}/recognizers/"
                f"{stt_config.recognizer or '_'}"
            )
        )
        yield cloud_speech.StreamingRecognizeRequest(
            recognizer=recognizer,
            streaming_config=cloud_speech.StreamingRecognitionConfig(
                config=_build_recognition_config(stt_config, sample_rate, glossary),
                streaming_features=cloud_speech.StreamingRecognitionFeatures(
                    interim_results=stt_config.interim_results,
                    enable_voice_activity_events=stt_config.vad_events,
                    voice_activity_timeout=cloud_speech.StreamingRecognitionFeatures.VoiceActivityTimeout(
                        speech_end_timeout=_duration_from_ms(stt_config.utterance_end_ms),
                    ),
                ),
            ),
        )
        while self._audio_queue is not None:
            chunk = await self._audio_queue.get()
            if chunk is None:
                break
            yield cloud_speech.StreamingRecognizeRequest(audio=chunk)

    async def _handle_response(self, response: cloud_speech.StreamingRecognizeResponse) -> None:
        if response.speech_event_type in (
            cloud_speech.StreamingRecognizeResponse.SpeechEventType.END_OF_SINGLE_UTTERANCE,
            cloud_speech.StreamingRecognizeResponse.SpeechEventType.SPEECH_ACTIVITY_END,
        ):
            if self._on_utterance_end:
                await self._on_utterance_end()

        for result in response.results:
            alt = result.alternatives[0] if result.alternatives else None
            if alt is None:
                continue
            text = (alt.transcript or "").strip()
            if not text:
                continue

            relative_end = _duration_to_seconds(result.result_end_offset)
            absolute_end = self._stream_offset + relative_end
            audio_start = self._last_final_audio_end if result.is_final else max(0.0, absolute_end - 0.1)
            audio_end = max(audio_start, absolute_end)
            self._last_stream_audio_end = max(self._last_stream_audio_end, relative_end)

            words = list(getattr(alt, "words", []) or [])
            speaker_tags = sorted({
                int(getattr(word, "speaker_label", 0) or 0)
                for word in words
                if getattr(word, "speaker_label", 0)
            })
            avg_confidence = (
                sum(float(word.confidence or 0.0) for word in words) / len(words)
                if words else float(getattr(alt, "confidence", 0.0) or 0.0)
            )
            stt_meta = {
                "avg_confidence": avg_confidence,
                "word_count": len(words),
                "confidence_threshold": self._stt_config.confidence_hold_threshold,
                "low_confidence": avg_confidence > 0
                and avg_confidence < self._stt_config.confidence_hold_threshold,
                "detected_language": getattr(result, "language_code", "") or "",
                "detected_languages": [getattr(result, "language_code", "")] if getattr(result, "language_code", "") else [],
                "speaker_tags": speaker_tags,
            }

            if result.is_final:
                self._last_final_audio_end = audio_end
                await self._on_final(text, audio_start, audio_end, stt_meta)
            else:
                await self._on_interim(text)


def _google_api_endpoint(location: str) -> str:
    if location == "global":
        return "speech.googleapis.com"
    return f"{location}-speech.googleapis.com"


def _duration_from_ms(value_ms: int) -> Duration:
    duration = Duration()
    duration.FromMilliseconds(value_ms)
    return duration


def _duration_to_seconds(value) -> float:
    if value is None:
        return 0.0
    seconds = float(getattr(value, "seconds", 0))
    nanos = float(getattr(value, "nanos", 0))
    return seconds + nanos / 1_000_000_000


def _build_recognition_config(
    stt_config: STTConfig,
    sample_rate: int,
    glossary: dict[str, int],
) -> cloud_speech.RecognitionConfig:
    config = cloud_speech.RecognitionConfig(
        explicit_decoding_config=cloud_speech.ExplicitDecodingConfig(
            encoding=cloud_speech.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=sample_rate,
            audio_channel_count=1,
        ),
        model=stt_config.model,
        language_codes=list(stt_config.language_codes),
        features=cloud_speech.RecognitionFeatures(
            enable_automatic_punctuation=stt_config.punctuate,
            max_alternatives=1,
        ),
    )
    if stt_config.diarization_enabled:
        config.features.diarization_config = cloud_speech.SpeakerDiarizationConfig(
            min_speaker_count=stt_config.diarization_min_speakers,
            max_speaker_count=max(
                stt_config.diarization_min_speakers,
                stt_config.diarization_max_speakers,
            ),
        )
    adaptation = _build_adaptation(glossary)
    if adaptation is not None:
        config.adaptation = adaptation
    return config


def _build_adaptation(glossary: dict[str, int]) -> cloud_speech.SpeechAdaptation | None:
    if not glossary:
        return None
    phrases = [
        cloud_speech.PhraseSet.Phrase(value=term, boost=float(boost))
        for term, boost in sorted(glossary.items())
        if term.strip()
    ]
    if not phrases:
        return None
    return cloud_speech.SpeechAdaptation(
        phrase_sets=[
            cloud_speech.SpeechAdaptation.AdaptationPhraseSet(
                inline_phrase_set=cloud_speech.PhraseSet(phrases=phrases)
            )
        ]
    )
