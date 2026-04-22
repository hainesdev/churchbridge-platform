from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
