from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import httpx
import numpy as np
from dotenv import load_dotenv
from google.api_core.client_options import ClientOptions
from google.cloud.speech_v2 import SpeechAsyncClient
from google.cloud.speech_v2.types import cloud_speech

from server.services.google_speech_session import _build_recognition_config
from server.services.stt import STTConfig
from tests.benchmark.degradations import DegradationSpec, apply_degradation, build_single_spec
from tests.benchmark.run_benchmark import compute_wer, parse_srt

ROOT = Path(__file__).parent.parent.parent
load_dotenv(ROOT / ".env", override=True)

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_REALTIME_TRANSLATE_MODEL = os.getenv("OPENAI_REALTIME_TRANSLATE_MODEL", "").strip() or "gpt-realtime-translate"
OPENAI_REALTIME_TRANSLATE_URL = os.getenv("OPENAI_REALTIME_TRANSLATE_URL", "").strip() or "wss://api.openai.com/v1/realtime/translations"
DEEPGRAM_PRERECORDED_URL = "https://api.deepgram.com/v1/listen"
OPENAI_AUDIO_CHUNK_MS = 100
OPENAI_DRAIN_TIMEOUT_S = 12.0
CHIRP_MAX_AUDIO_SECONDS = 55.0
DEFAULT_GLOSSARY = {
    "Jesucristo": 10,
    "Espíritu Santo": 10,
    "Pentecostés": 8,
    "evangelio": 6,
    "salvación": 6,
    "alabanza": 4,
    "adoración": 4,
}


@dataclass(frozen=True)
class Scenario:
    label: str
    spec: DegradationSpec


@dataclass
class ProviderRunResult:
    provider: str
    model: str
    condition: str
    evaluation_role: str
    evaluation_phase: str
    transcript: str
    latency_s: float
    time_to_first_text_s: float | None
    wer: dict[str, Any] | None
    transcript_word_count: int
    metadata: dict[str, Any]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "condition": self.condition,
            "evaluation_role": self.evaluation_role,
            "evaluation_phase": self.evaluation_phase,
            "transcript": self.transcript,
            "latency_s": round(self.latency_s, 3),
            "time_to_first_text_s": None if self.time_to_first_text_s is None else round(self.time_to_first_text_s, 3),
            "wer": self.wer,
            "transcript_word_count": self.transcript_word_count,
            "metadata": self.metadata,
            "error": self.error,
        }


def resolve_scenarios(
    conditions: list[str] | tuple[str, ...],
    *,
    echo_profile: str,
    noise_type: str,
    snr_db: float,
    seed: int,
) -> list[Scenario]:
    normalized = [str(condition).strip().lower() for condition in conditions if str(condition).strip()]
    if not normalized:
        raise ValueError("At least one condition is required.")

    scenarios: list[Scenario] = []
    for condition in normalized:
        if condition == "raw":
            scenarios.append(Scenario("raw", build_single_spec("clean", None, None, None, seed)))
        elif condition == "echo":
            scenarios.append(Scenario("echo", build_single_spec("echo", echo_profile, None, None, seed)))
        elif condition == "noise":
            scenarios.append(Scenario("noise", build_single_spec("noise", None, noise_type, snr_db, seed)))
        else:
            raise ValueError(f"Unsupported condition: {condition}")
    return scenarios


def resample_mono_pcm16(samples: np.ndarray, sample_rate: int, target_rate: int = 16000) -> np.ndarray:
    mono = np.asarray(samples, dtype=np.float32)
    if mono.ndim != 1:
        raise ValueError("Expected mono samples.")
    if mono.size == 0:
        return np.zeros(0, dtype=np.int16)

    clipped = np.clip(mono, -1.0, 1.0)
    if sample_rate == target_rate:
        return np.round(clipped * 32767.0).astype(np.int16)

    duration_s = mono.size / float(sample_rate)
    target_len = max(int(round(duration_s * target_rate)), 1)
    source_x = np.linspace(0.0, duration_s, num=mono.size, endpoint=False)
    target_x = np.linspace(0.0, duration_s, num=target_len, endpoint=False)
    resampled = np.interp(target_x, source_x, clipped).astype(np.float32)
    return np.round(np.clip(resampled, -1.0, 1.0) * 32767.0).astype(np.int16)


def build_reference_text(srt_path: Path, *, duration_s: float, start_offset_s: float) -> str:
    segments = parse_srt(srt_path)
    clip_end_s = start_offset_s + duration_s
    return " ".join(
        segment["text"]
        for segment in segments
        if start_offset_s <= float(segment["start"]) < clip_end_s
    )


def summarize_results(results: list[ProviderRunResult]) -> dict[str, dict[str, dict[str, float | int | None]]]:
    summary: dict[str, dict[str, dict[str, float | int | None]]] = {}
    for result in results:
        provider_summary = summary.setdefault(result.provider, {})
        provider_summary[result.condition] = {
            "evaluation_role": result.evaluation_role,
            "evaluation_phase": result.evaluation_phase,
            "latency_s": round(result.latency_s, 3),
            "time_to_first_text_s": None if result.time_to_first_text_s is None else round(result.time_to_first_text_s, 3),
            "wer_pct": None if result.wer is None else result.wer.get("score_pct"),
            "transcript_word_count": result.transcript_word_count,
            "ok": int(result.error is None),
        }
    return summary


def _google_api_endpoint(location: str) -> str:
    if location == "global":
        return "speech.googleapis.com"
    return f"{location}-speech.googleapis.com"


def _google_recognizer_name(config: STTConfig, project_id: str) -> str:
    if config.recognizer.startswith("projects/"):
        return config.recognizer
    return f"projects/{project_id}/locations/{config.location}/recognizers/{config.recognizer or '_'}"


def _write_wav_bytes(path: Path, pcm16: np.ndarray, sample_rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm16.astype("<i2").tobytes())


def split_pcm16_for_max_seconds(
    pcm16: np.ndarray,
    *,
    sample_rate: int = 16000,
    max_seconds: float = CHIRP_MAX_AUDIO_SECONDS,
) -> list[np.ndarray]:
    samples = np.asarray(pcm16, dtype=np.int16)
    if samples.ndim != 1:
        raise ValueError("Expected 1D PCM16 samples.")
    if samples.size == 0:
        return [samples]

    max_samples = max(int(sample_rate * max_seconds), 1)
    return [samples[start : start + max_samples] for start in range(0, samples.size, max_samples)]


async def run_deepgram_benchmark(
    pcm16: np.ndarray,
    *,
    language: str = "es",
    model: str = "nova-3",
    glossary: dict[str, int] | None = None,
) -> tuple[str, float, dict[str, Any]]:
    if not DEEPGRAM_API_KEY:
        raise RuntimeError("DEEPGRAM_API_KEY not set")

    glossary = glossary or DEFAULT_GLOSSARY
    with NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        tmp_path = Path(handle.name)
    try:
        _write_wav_bytes(tmp_path, pcm16)
        params: list[tuple[str, str]] = [
            ("model", model),
            ("language", language),
            ("smart_format", "true"),
            ("punctuate", "true"),
            ("paragraphs", "true"),
            ("utterances", "true"),
            ("diarize", "false"),
        ]
        params.extend(("keyterms", term) for term in glossary if str(term).strip())

        start = time.monotonic()
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                DEEPGRAM_PRERECORDED_URL,
                params=params,
                content=tmp_path.read_bytes(),
                headers={
                    "Authorization": f"Token {DEEPGRAM_API_KEY}",
                    "Content-Type": "audio/wav",
                },
            )
            response.raise_for_status()
        elapsed = time.monotonic() - start
        payload = response.json()
        alternative = payload["results"]["channels"][0]["alternatives"][0]
        transcript = str(alternative.get("transcript", "") or "").strip()
        metadata = {
            "language": language,
            "request_model": model,
            "detected_duration_s": payload.get("metadata", {}).get("duration"),
            "utterance_count": len(payload.get("results", {}).get("utterances", []) or []),
        }
        return transcript, elapsed, metadata
    finally:
        tmp_path.unlink(missing_ok=True)


async def run_chirp3_benchmark(
    pcm16: np.ndarray,
    *,
    stt_config: STTConfig | None = None,
    glossary: dict[str, int] | None = None,
) -> tuple[str, float, dict[str, Any]]:
    glossary = glossary or DEFAULT_GLOSSARY
    project_id = (
        os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("GCLOUD_PROJECT")
        or os.getenv("GCP_PROJECT")
        or ""
    ).strip()
    if not project_id:
        raise RuntimeError("Missing GOOGLE_CLOUD_PROJECT for Chirp 3 benchmark")

    config = stt_config or STTConfig.from_payload({"model": "chirp_3"})
    client = SpeechAsyncClient(client_options=ClientOptions(api_endpoint=_google_api_endpoint(config.location)))
    try:
        start = time.monotonic()
        responses = []
        for chunk in split_pcm16_for_max_seconds(pcm16, sample_rate=16000, max_seconds=CHIRP_MAX_AUDIO_SECONDS):
            request = cloud_speech.RecognizeRequest(
                recognizer=_google_recognizer_name(config, project_id),
                config=_build_recognition_config(config, 16000, glossary),
                content=chunk.astype("<i2").tobytes(),
            )
            responses.append(await client.recognize(request=request))
        elapsed = time.monotonic() - start
    finally:
        close_result = client.transport.close()
        if inspect.isawaitable(close_result):
            await close_result

    transcript_parts: list[str] = []
    detected_languages: list[str] = []
    result_count = 0
    for response in responses:
        for result in response.results:
            result_count += 1
            alternative = result.alternatives[0] if result.alternatives else None
            if alternative and str(alternative.transcript or "").strip():
                transcript_parts.append(str(alternative.transcript).strip())
            language_code = str(getattr(result, "language_code", "") or "").strip()
            if language_code:
                detected_languages.append(language_code)

    metadata = {
        "request_model": config.model,
        "language_codes": list(config.language_codes),
        "location": config.location,
        "recognizer": config.recognizer,
        "chunk_count": len(responses),
        "chunk_max_seconds": CHIRP_MAX_AUDIO_SECONDS,
        "result_count": result_count,
        "detected_languages": detected_languages,
    }
    return " ".join(transcript_parts).strip(), elapsed, metadata


class OpenAIRealtimeTextAccumulator:
    def __init__(self) -> None:
        self._parts: list[str] = []
        self.first_text_at_s: float | None = None
        self.completed = False

    def consume(self, event: dict[str, Any], elapsed_s: float) -> None:
        event_type = str(event.get("type") or "")
        text = _extract_openai_text_delta(event)
        if event_type.endswith(".done"):
            transcript = str(event.get("transcript") or "").strip()
            current = "".join(self._parts).strip()
            if transcript and transcript == current:
                text = ""
        if text:
            self._parts.append(text)
            if self.first_text_at_s is None:
                self.first_text_at_s = elapsed_s
        if event_type.endswith(".completed") or event_type.endswith(".done") or event_type in {"response.completed", "translation.completed"}:
            self.completed = True

    def text(self) -> str:
        joined = "".join(self._parts).strip()
        return " ".join(joined.split())


def _extract_openai_text_delta(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "")
    if event_type in {"session.output_audio.delta", "response.output_audio.delta"}:
        return ""
    if event_type in {"session.output_transcript.delta", "session.output_transcript.done"}:
        value = event.get("delta") if event_type.endswith(".delta") else event.get("transcript")
        if isinstance(value, str) and value:
            return value
    if event_type in {
        "response.output_audio_transcript.delta",
        "response.output_audio_transcript.done",
        "response.output_text.delta",
        "response.output_text.done",
    }:
        value = event.get("delta") if event_type.endswith(".delta") else event.get("text") or event.get("transcript")
        if isinstance(value, str) and value:
            return value

    for key in ("delta", "text", "transcript"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value

    item = event.get("item")
    if isinstance(item, dict):
        content = item.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for chunk in content:
                if not isinstance(chunk, dict):
                    continue
                for key in ("text", "transcript"):
                    value = chunk.get(key)
                    if isinstance(value, str) and value:
                        parts.append(value)
            if parts:
                return "".join(parts)
    return ""


async def run_openai_realtime_translate_benchmark(
    pcm16: np.ndarray,
    *,
    model: str = OPENAI_REALTIME_TRANSLATE_MODEL,
    source_language: str = "es",
    target_language: str = "es",
) -> tuple[str, float, float | None, dict[str, Any]]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")

    import websockets

    url = f"{OPENAI_REALTIME_TRANSLATE_URL}?model={model}"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }
    accumulator = OpenAIRealtimeTextAccumulator()
    event_types: list[str] = []
    start = time.monotonic()

    async with websockets.connect(url, additional_headers=headers, max_size=16 * 1024 * 1024) as ws:
        initial_event = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        initial_type = str(initial_event.get("type") or "")
        event_types.append(initial_type)
        if initial_type == "error":
            raise RuntimeError(_format_openai_realtime_error(initial_event))

        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "audio": {
                    "output": {
                        "language": target_language,
                    },
                },
            },
        }))

        listener_error: Exception | None = None
        stream_done = asyncio.Event()
        stream_done_at: float | None = None
        last_transcript_at = time.monotonic()

        async def listen_for_events() -> None:
            nonlocal listener_error, last_transcript_at
            try:
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except asyncio.TimeoutError:
                        if stream_done.is_set() and accumulator.first_text_at_s is not None and time.monotonic() - last_transcript_at >= 2.0:
                            break
                        if (
                            stream_done.is_set()
                            and accumulator.first_text_at_s is None
                            and stream_done_at is not None
                            and time.monotonic() - stream_done_at >= OPENAI_DRAIN_TIMEOUT_S
                        ):
                            break
                        continue

                    event = json.loads(raw)
                    event_type = str(event.get("type") or "")
                    event_types.append(event_type)
                    if event_type == "error":
                        raise RuntimeError(_format_openai_realtime_error(event))
                    accumulator.consume(event, time.monotonic() - start)
                    if _is_openai_transcript_event(event_type):
                        last_transcript_at = time.monotonic()
                    if accumulator.completed and stream_done.is_set():
                        break
            except Exception as exc:
                listener_error = exc

        listener_task = asyncio.create_task(listen_for_events())

        chunk_samples = max(int(24000 * OPENAI_AUDIO_CHUNK_MS / 1000), 1)
        for offset in range(0, len(pcm16), chunk_samples):
            chunk = pcm16[offset : offset + chunk_samples]
            await ws.send(json.dumps({
                "type": "session.input_audio_buffer.append",
                "audio": base64.b64encode(chunk.astype("<i2").tobytes()).decode("ascii"),
            }))
            await asyncio.sleep(OPENAI_AUDIO_CHUNK_MS / 1000)
        stream_done_at = time.monotonic()
        stream_done.set()
        try:
            await asyncio.wait_for(listener_task, timeout=OPENAI_DRAIN_TIMEOUT_S + 5.0)
        finally:
            if not listener_task.done():
                listener_task.cancel()
                try:
                    await listener_task
                except asyncio.CancelledError:
                    pass
            await ws.send(json.dumps({"type": "session.close"}))
        if listener_error is not None:
            raise listener_error

    return accumulator.text(), time.monotonic() - start, accumulator.first_text_at_s, {
        "request_model": model,
        "source_language": source_language,
        "target_language": target_language,
        "event_types": event_types,
    }


def _is_openai_transcript_event(event_type: str) -> bool:
    return event_type in {
        "session.output_transcript.delta",
        "session.output_transcript.done",
        "response.output_audio_transcript.delta",
        "response.output_audio_transcript.done",
        "response.output_text.delta",
        "response.output_text.done",
    }


def _format_openai_realtime_error(event: dict[str, Any]) -> str:
    payload = event.get("error")
    if isinstance(payload, dict):
        code = str(payload.get("code") or "").strip()
        message = str(payload.get("message") or "").strip()
        if code and message:
            return f"{code}: {message}"
        return message or json.dumps(payload, ensure_ascii=False)
    return str(event)


async def run_provider_suite(
    *,
    degraded_samples: np.ndarray,
    sample_rate: int,
    reference_text: str,
    condition: str,
    deepgram_model: str,
    chirp_config: STTConfig,
    openai_source_language: str,
    openai_target_language: str,
) -> list[ProviderRunResult]:
    pcm16 = resample_mono_pcm16(degraded_samples, sample_rate, 16000)
    openai_pcm16 = resample_mono_pcm16(degraded_samples, sample_rate, 24000)
    provider_roles = {
        "deepgram": "primary_stt_baseline",
        "chirp_3": "primary_stt_baseline",
        "gpt-realtime-translate": "translation_model_stt_probe",
    }
    providers = [
        ("deepgram", lambda: run_deepgram_benchmark(pcm16, model=deepgram_model)),
        ("chirp_3", lambda: run_chirp3_benchmark(pcm16, stt_config=chirp_config)),
        (
            "gpt-realtime-translate",
            lambda: run_openai_realtime_translate_benchmark(
                openai_pcm16,
                source_language=openai_source_language,
                target_language=openai_target_language,
            ),
        ),
    ]

    results: list[ProviderRunResult] = []
    for provider_name, runner in providers:
        try:
            if provider_name == "gpt-realtime-translate":
                transcript, latency_s, first_text_s, metadata = await runner()
                model = str(metadata.get("request_model") or OPENAI_REALTIME_TRANSLATE_MODEL)
            else:
                transcript, latency_s, metadata = await runner()
                first_text_s = None
                model = str(metadata.get("request_model") or "")
            wer = compute_wer(reference_text, transcript) if transcript else None
            results.append(
                ProviderRunResult(
                    provider=provider_name,
                    model=model,
                    condition=condition,
                    evaluation_role=provider_roles[provider_name],
                    evaluation_phase="phase_1_stt_baseline",
                    transcript=transcript,
                    latency_s=latency_s,
                    time_to_first_text_s=first_text_s,
                    wer=wer,
                    transcript_word_count=len(transcript.split()),
                    metadata=metadata,
                )
            )
        except Exception as exc:
            results.append(
                ProviderRunResult(
                    provider=provider_name,
                    model=(
                        OPENAI_REALTIME_TRANSLATE_MODEL
                        if provider_name == "gpt-realtime-translate"
                        else deepgram_model if provider_name == "deepgram" else chirp_config.model
                    ),
                    condition=condition,
                    evaluation_role=provider_roles[provider_name],
                    evaluation_phase="phase_1_stt_baseline",
                    transcript="",
                    latency_s=0.0,
                    time_to_first_text_s=None,
                    wer=None,
                    transcript_word_count=0,
                    metadata={},
                    error=str(exc),
                )
            )
    return results
