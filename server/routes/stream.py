import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

logger = logging.getLogger(__name__)
router = APIRouter()

# session_manager injected from main.py at startup
_session_manager = None


def set_session_manager(mgr):
    global _session_manager
    _session_manager = mgr


@router.websocket("/api/stream/v1")
async def stream_audio(
    ws: WebSocket,
    church_id: str = Query(..., description="Church identifier"),
):
    """Audio ingestion WebSocket for the Soundboard Admin.

    Protocol (client → server):
      { type: 'session.start', sampleRate: number }
      { type: 'audio', audio: '<base64 Float32 LE>' }
      { type: 'session.stop' }

    Protocol (server → client):
      { type: 'session_started', sessionId: number }
      { type: 'error', message: string }
    """
    await ws.accept()
    session = None

    try:
        async for msg in ws.iter_json():
            msg_type = msg.get("type")

            if msg_type == "session.start":
                sample_rate = msg.get("sampleRate", 48000)
                sermon_topic = msg.get("topic", "").strip()
                source_scripture_version = msg.get("sourceScriptureVersion", "rvr1960")
                display_scripture_version = msg.get("displayScriptureVersion", "kjv")
                session = await _session_manager.create(
                    church_id,
                    ws,
                    sample_rate,
                    sermon_topic,
                    source_scripture_version=source_scripture_version,
                    display_scripture_version=display_scripture_version,
                )

            elif msg_type == "audio":
                if session:
                    await session.ingest(msg["audio"])

            elif msg_type == "session.stop":
                break

    except WebSocketDisconnect:
        logger.info("[stream] Admin disconnected for church %s", church_id)
    except Exception as e:
        logger.error("[stream] Error for church %s: %s", church_id, e, exc_info=True)
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        if session:
            await _session_manager.remove(church_id)
