from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

STTProvider = Literal["google", "deepgram"]


def _default_model() -> str:
    return (
        os.getenv("STT_MODEL")
        or os.getenv("GOOGLE_SPEECH_MODEL")
        or "nova-3"
    ).strip() or "nova-3"


def _dedupe_language_codes(codes: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for code in codes:
        cleaned = str(code).strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(cleaned)
    return tuple(ordered)


def _default_language_codes() -> tuple[str, ...]:
    raw_codes = os.getenv("GOOGLE_SPEECH_LANGUAGE_CODES", "").strip()
    if raw_codes:
        return _dedupe_language_codes(raw_codes.split(","))

    primary = os.getenv("GOOGLE_SPEECH_LANGUAGE", "es-US").strip() or "es-US"
    secondary = os.getenv("GOOGLE_SPEECH_SECONDARY_LANGUAGE", "").strip()
    if secondary:
        return _dedupe_language_codes((primary, secondary))
    if primary.lower().startswith("es"):
        return _dedupe_language_codes((primary, "en-US"))
    if primary.lower().startswith("en"):
        return _dedupe_language_codes((primary, "es-US"))
    return (primary,)


def _default_location() -> str:
    return os.getenv("GOOGLE_CLOUD_LOCATION", "us").strip() or "us"


def _default_recognizer() -> str:
    return os.getenv("GOOGLE_SPEECH_RECOGNIZER", "_").strip() or "_"


def _parse_language_codes(payload: dict) -> tuple[str, ...]:
    raw_codes = payload.get("languageCodes")
    if isinstance(raw_codes, (list, tuple)):
        cleaned = _dedupe_language_codes(raw_codes)
        if cleaned:
            return cleaned

    legacy_language = str(payload.get("language") or "").strip()
    if legacy_language:
        return _dedupe_language_codes(legacy_language.split(","))

    return _default_language_codes()


def _normalized_model_name(model: str) -> str:
    return str(model or "").strip().lower()


def _language_family(code: str) -> str:
    normalized = str(code or "").strip().lower()
    if normalized.startswith("es"):
        return "es"
    if normalized.startswith("en"):
        return "en"
    if normalized.startswith("fr"):
        return "fr"
    if normalized.startswith("de"):
        return "de"
    if normalized.startswith("hi"):
        return "hi"
    if normalized.startswith("ru"):
        return "ru"
    if normalized.startswith("pt"):
        return "pt"
    if normalized.startswith("ja"):
        return "ja"
    if normalized.startswith("it"):
        return "it"
    if normalized.startswith("nl"):
        return "nl"
    return normalized.split("-", 1)[0]


def infer_stt_provider(model: str) -> STTProvider:
    normalized = _normalized_model_name(model)
    if normalized.startswith("nova-") or normalized.startswith("flux"):
        return "deepgram"
    return "google"


def deepgram_language_option(config: "STTConfig") -> str:
    cleaned = [str(code).strip() for code in config.language_codes if str(code).strip()]
    if not cleaned:
        return "en"
    if any(code.lower() == "multi" for code in cleaned):
        return "multi"

    families = {_language_family(code) for code in cleaned if _language_family(code)}
    if len(cleaned) > 1 or len(families) > 1:
        return "multi"

    return _language_family(cleaned[0]) or "en"


@dataclass(frozen=True)
class STTConfig:
    model: str = _default_model()
    language_codes: tuple[str, ...] = field(default_factory=_default_language_codes)
    interim_results: bool = True
    utterance_end_ms: int = 2000
    vad_events: bool = True
    smart_format: bool = True
    punctuate: bool = True
    confidence_hold_threshold: float = 0.72
    low_confidence_hold_secs: float = 2.5
    location: str = _default_location()
    recognizer: str = _default_recognizer()
    diarization_enabled: bool = False
    diarization_min_speakers: int = 2
    diarization_max_speakers: int = 2

    @classmethod
    def from_payload(cls, payload: dict | None) -> "STTConfig":
        payload = payload or {}
        location = str(payload.get("location") or "").strip() or _default_location()
        recognizer = str(payload.get("recognizer") or "").strip() or _default_recognizer()
        return cls(
            model=str(payload.get("model") or "").strip() or _default_model(),
            language_codes=_parse_language_codes(payload),
            interim_results=bool(payload.get("interimResults", cls.interim_results)),
            utterance_end_ms=max(500, int(payload.get("utteranceEndMs", cls.utterance_end_ms))),
            vad_events=bool(payload.get("vadEvents", cls.vad_events)),
            smart_format=bool(payload.get("smartFormat", cls.smart_format)),
            punctuate=bool(payload.get("punctuate", cls.punctuate)),
            confidence_hold_threshold=float(payload.get("confidenceHoldThreshold", cls.confidence_hold_threshold)),
            low_confidence_hold_secs=float(payload.get("lowConfidenceHoldSecs", cls.low_confidence_hold_secs)),
            location=location,
            recognizer=recognizer,
            diarization_enabled=bool(payload.get("diarizationEnabled", cls.diarization_enabled)),
            diarization_min_speakers=max(1, int(payload.get("diarizationMinSpeakers", cls.diarization_min_speakers))),
            diarization_max_speakers=max(1, int(payload.get("diarizationMaxSpeakers", cls.diarization_max_speakers))),
        )

    def public_payload(self) -> dict:
        primary_language = self.language_codes[0] if self.language_codes else ""
        return {
            "model": self.model,
            "language": primary_language,
            "languageCodes": list(self.language_codes),
            "location": self.location,
            "recognizer": self.recognizer,
            "interimResults": self.interim_results,
            "utteranceEndMs": self.utterance_end_ms,
            "vadEvents": self.vad_events,
            "smartFormat": self.smart_format,
            "punctuate": self.punctuate,
            "confidenceHoldThreshold": self.confidence_hold_threshold,
            "lowConfidenceHoldSecs": self.low_confidence_hold_secs,
            "diarizationEnabled": self.diarization_enabled,
            "diarizationMinSpeakers": self.diarization_min_speakers,
            "diarizationMaxSpeakers": self.diarization_max_speakers,
        }
