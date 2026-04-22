from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import numpy as np

from server.services.audio_utils import resample_float32_to_pcm16


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def analyze_float32_audio_bytes(data: bytes, sample_rate: int) -> dict[str, Any]:
    """Analyze raw Float32 LE mono audio similar to the live ingest path."""

    samples = np.frombuffer(data, dtype=np.float32)
    finite_mask = np.isfinite(samples)
    finite_samples = samples[finite_mask]

    if finite_samples.size == 0:
        float_stats = {
            "sample_count": int(samples.size),
            "duration_ms": 0.0,
            "finite_sample_count": 0,
            "nan_count": int(np.isnan(samples).sum()),
            "inf_count": int(np.isinf(samples).sum()),
            "rms": 0.0,
            "peak": 0.0,
            "mean_abs": 0.0,
            "min": 0.0,
            "max": 0.0,
            "near_zero_ratio": 1.0,
            "clipping_ratio": 0.0,
            "zero_crossing_ratio": 0.0,
            "looks_silent": True,
            "looks_clipped": False,
            "looks_like_speech_energy": False,
            "sample_preview": [],
        }
        pcm16_stats = {
            "sample_count": 0,
            "duration_ms": 0.0,
            "rms": 0.0,
            "peak": 0,
            "mean_abs": 0.0,
            "min": 0,
            "max": 0,
            "near_zero_ratio": 1.0,
            "zero_crossing_ratio": 0.0,
        }
        return {"float32": float_stats, "pcm16": pcm16_stats}

    abs_samples = np.abs(finite_samples)
    rms = float(np.sqrt(np.mean(np.square(finite_samples))))
    peak = float(abs_samples.max(initial=0.0))
    near_zero_ratio = float(np.mean(abs_samples < 1e-3))
    clipping_ratio = float(np.mean(abs_samples >= 0.98))
    zero_crossing_ratio = 0.0
    if finite_samples.size > 1:
        zero_crossing_ratio = float(np.mean(np.diff(np.signbit(finite_samples)).astype(np.float32)))

    pcm16 = np.frombuffer(
        resample_float32_to_pcm16(data, src_rate=sample_rate, dst_rate=16000),
        dtype=np.int16,
    )
    pcm16_abs = np.abs(pcm16.astype(np.int32)) if pcm16.size else np.array([], dtype=np.int32)
    pcm16_zero_crossings = 0.0
    if pcm16.size > 1:
        pcm16_zero_crossings = float(np.mean(np.diff(np.signbit(pcm16)).astype(np.float32)))

    float_stats = {
        "sample_count": int(samples.size),
        "duration_ms": round((float(samples.size) / max(sample_rate, 1)) * 1000, 2),
        "finite_sample_count": int(finite_samples.size),
        "nan_count": int(np.isnan(samples).sum()),
        "inf_count": int(np.isinf(samples).sum()),
        "rms": round(rms, 6),
        "peak": round(peak, 6),
        "mean_abs": round(float(abs_samples.mean()), 6),
        "min": round(float(finite_samples.min(initial=0.0)), 6),
        "max": round(float(finite_samples.max(initial=0.0)), 6),
        "near_zero_ratio": round(near_zero_ratio, 6),
        "clipping_ratio": round(clipping_ratio, 6),
        "zero_crossing_ratio": round(zero_crossing_ratio, 6),
        "looks_silent": bool(rms < 0.005 and near_zero_ratio > 0.9),
        "looks_clipped": bool(clipping_ratio > 0.05),
        "looks_like_speech_energy": bool(rms >= 0.01 and peak >= 0.03 and near_zero_ratio < 0.98),
        "sample_preview": [round(float(value), 6) for value in finite_samples[:16]],
    }
    pcm16_stats = {
        "sample_count": int(pcm16.size),
        "duration_ms": round((float(pcm16.size) / 16000) * 1000, 2),
        "rms": round(float(np.sqrt(np.mean(np.square(pcm16.astype(np.float32))))) if pcm16.size else 0.0, 3),
        "peak": int(pcm16_abs.max(initial=0)) if pcm16.size else 0,
        "mean_abs": round(float(pcm16_abs.mean()) if pcm16.size else 0.0, 3),
        "min": int(pcm16.min(initial=0)) if pcm16.size else 0,
        "max": int(pcm16.max(initial=0)) if pcm16.size else 0,
        "near_zero_ratio": round(float(np.mean(pcm16_abs <= 16)) if pcm16.size else 1.0, 6),
        "zero_crossing_ratio": round(pcm16_zero_crossings, 6),
    }
    return {"float32": float_stats, "pcm16": pcm16_stats}


class MobileDiagnosticsHub:
    """In-memory command/report store for remote mobile diagnostics."""

    def __init__(self) -> None:
        self._commands_by_church: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self._reports_by_church: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def create_command(self, church_id: str, command: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        command_id = uuid4().hex
        record = {
            "id": command_id,
            "church_id": church_id,
            "command": command,
            "payload": deepcopy(payload or {}),
            "status": "queued",
            "created_at": _utc_now_iso(),
            "updated_at": _utc_now_iso(),
            "latest_report": None,
            "report_count": 0,
        }
        self._commands_by_church[church_id][command_id] = record
        return deepcopy(record)

    def get_command(self, church_id: str, command_id: str) -> dict[str, Any] | None:
        record = self._commands_by_church.get(church_id, {}).get(command_id)
        return deepcopy(record) if record else None

    def list_reports(self, church_id: str, limit: int = 20) -> list[dict[str, Any]]:
        reports = self._reports_by_church.get(church_id, [])
        if limit <= 0:
            return []
        return [deepcopy(report) for report in reports[-limit:]][::-1]

    def add_report(self, church_id: str, report: dict[str, Any]) -> dict[str, Any]:
        report_record = {
            **deepcopy(report),
            "church_id": church_id,
            "received_at": _utc_now_iso(),
        }
        self._reports_by_church[church_id].append(report_record)

        command_id = report_record.get("command_id")
        if command_id:
            command = self._commands_by_church.get(church_id, {}).get(command_id)
            if command is not None:
                command["latest_report"] = deepcopy(report_record)
                command["report_count"] = int(command.get("report_count", 0)) + 1
                command["updated_at"] = _utc_now_iso()
                status = report_record.get("status")
                if status:
                    command["status"] = status

        return deepcopy(report_record)
