import logging
import base64
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from server.services.mobile_diagnostics import analyze_float32_audio_bytes

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/churches/{church_id}/mobile-diagnostics", tags=["mobile-diagnostics"])

_broadcaster = None
_hub = None


def set_broadcaster(broadcaster):
    global _broadcaster
    _broadcaster = broadcaster


def set_mobile_diagnostics_hub(hub):
    global _hub
    _hub = hub


class DiagnosticsCommandIn(BaseModel):
    command: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class DiagnosticsReportIn(BaseModel):
    command_id: str | None = None
    report_type: str = Field(..., min_length=1)
    status: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    device: dict[str, Any] = Field(default_factory=dict)
    app: dict[str, Any] = Field(default_factory=dict)


class DiagnosticsAudioChunkIn(BaseModel):
    audio_base64: str = Field(..., min_length=1)
    sample_rate: int = Field(default=16_000, ge=1)
    label: str | None = None


@router.post("/commands")
async def create_mobile_diagnostics_command(church_id: str, body: DiagnosticsCommandIn):
    if _hub is None or _broadcaster is None:
        raise HTTPException(status_code=503, detail="Mobile diagnostics is not initialized")

    command = _hub.create_command(church_id, body.command, body.payload)
    await _broadcaster.publish(church_id, {
        "type": "diagnostics_command",
        "command": command,
    })
    logger.info(
        "[mobile-diagnostics] queued command %s for church %s (%s)",
        command["id"],
        church_id,
        body.command,
    )
    return command


@router.get("/commands/{command_id}")
async def get_mobile_diagnostics_command(church_id: str, command_id: str):
    if _hub is None:
        raise HTTPException(status_code=503, detail="Mobile diagnostics is not initialized")
    command = _hub.get_command(church_id, command_id)
    if command is None:
        raise HTTPException(status_code=404, detail="Diagnostics command not found")
    return command


@router.get("/reports")
async def list_mobile_diagnostics_reports(church_id: str, limit: int = 20):
    if _hub is None:
        raise HTTPException(status_code=503, detail="Mobile diagnostics is not initialized")
    limit = max(1, min(limit, 100))
    return {"reports": _hub.list_reports(church_id, limit=limit)}


@router.post("/reports")
async def ingest_mobile_diagnostics_report(church_id: str, body: DiagnosticsReportIn):
    if _hub is None:
        raise HTTPException(status_code=503, detail="Mobile diagnostics is not initialized")
    report = _hub.add_report(church_id, body.model_dump())
    logger.info(
        "[mobile-diagnostics] report for church %s type=%s status=%s command_id=%s",
        church_id,
        report.get("report_type"),
        report.get("status"),
        report.get("command_id"),
    )
    return {"ok": True, "report": report}


@router.post("/analyses/audio-chunk")
async def analyze_mobile_diagnostics_audio_chunk(church_id: str, body: DiagnosticsAudioChunkIn):
    try:
        raw = base64.b64decode(body.audio_base64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 audio payload: {exc}") from exc

    if not raw:
        raise HTTPException(status_code=400, detail="Audio payload decoded to zero bytes")

    analysis = analyze_float32_audio_bytes(raw, sample_rate=body.sample_rate)
    return {
        "church_id": church_id,
        "label": body.label,
        "sample_rate": body.sample_rate,
        "byte_length": len(raw),
        "analysis": analysis,
    }
