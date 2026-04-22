from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.routes import mobile_diagnostics
from server.services.mobile_diagnostics import MobileDiagnosticsHub


class FakeBroadcaster:
    def __init__(self):
        self.events = []

    async def publish(self, church_id: str, event: dict):
        self.events.append((church_id, event))


def build_client():
    app = FastAPI()
    hub = MobileDiagnosticsHub()
    broadcaster = FakeBroadcaster()
    mobile_diagnostics.set_mobile_diagnostics_hub(hub)
    mobile_diagnostics.set_broadcaster(broadcaster)
    app.include_router(mobile_diagnostics.router)
    return TestClient(app), hub, broadcaster


def test_create_command_publishes_diagnostics_event():
    client, _, broadcaster = build_client()

    response = client.post(
        "/api/churches/christ-fellowship/mobile-diagnostics/commands",
        json={"command": "snapshot", "payload": {"duration_ms": 2500}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["church_id"] == "christ-fellowship"
    assert body["command"] == "snapshot"
    assert body["payload"] == {"duration_ms": 2500}
    assert body["status"] == "queued"
    assert len(broadcaster.events) == 1
    church_id, event = broadcaster.events[0]
    assert church_id == "christ-fellowship"
    assert event["type"] == "diagnostics_command"
    assert event["command"]["id"] == body["id"]


def test_report_ingestion_updates_command_status_and_report_listing():
    client, hub, _ = build_client()
    command = hub.create_command("christ-fellowship", "audio_probe", {"duration_ms": 4000})

    report_response = client.post(
        "/api/churches/christ-fellowship/mobile-diagnostics/reports",
        json={
            "command_id": command["id"],
            "report_type": "audio_probe",
            "status": "completed",
            "payload": {"speech_seen": True, "batches_sent_delta": 12},
            "device": {"model": "iPhone"},
            "app": {"build_number": "1"},
        },
    )

    assert report_response.status_code == 200
    report = report_response.json()["report"]
    assert report["church_id"] == "christ-fellowship"
    assert report["status"] == "completed"
    assert report["payload"]["batches_sent_delta"] == 12

    command_response = client.get(
        f"/api/churches/christ-fellowship/mobile-diagnostics/commands/{command['id']}"
    )
    assert command_response.status_code == 200
    command_body = command_response.json()
    assert command_body["status"] == "completed"
    assert command_body["report_count"] == 1
    assert command_body["latest_report"]["payload"]["speech_seen"] is True

    reports_response = client.get("/api/churches/christ-fellowship/mobile-diagnostics/reports")
    assert reports_response.status_code == 200
    reports = reports_response.json()["reports"]
    assert len(reports) == 1
    assert reports[0]["command_id"] == command["id"]
