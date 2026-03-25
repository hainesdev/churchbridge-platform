import json
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

logger = logging.getLogger(__name__)
router = APIRouter()

_broadcaster = None


def set_broadcaster(b):
    global _broadcaster
    _broadcaster = b


@router.websocket("/api/listen/v1")
async def listen_ws(
    ws: WebSocket,
    church_id: str = Query(...),
):
    """Mobile listener WebSocket. Forwards only translation events (English only)."""
    await ws.accept()

    if _broadcaster._available:
        try:
            async for payload in _broadcaster.subscribe(church_id):
                msg = json.loads(payload)
                # Mobile only needs tokens and completed translations
                if msg.get("type") in ("interim_translation", "translation"):
                    await ws.send_text(payload)
        except WebSocketDisconnect:
            logger.info("[listen] Mobile disconnected for church %s", church_id)
        except Exception as e:
            logger.error("[listen] Error for church %s: %s", church_id, e)
    else:
        import asyncio
        queue: asyncio.Queue = asyncio.Queue()
        unsubscribe = _broadcaster.subscribe_local(church_id, lambda p: queue.put(p))
        try:
            while True:
                payload = await queue.get()
                msg = json.loads(payload)
                if msg.get("type") in ("interim_translation", "translation"):
                    await ws.send_text(payload)
        except WebSocketDisconnect:
            pass
        finally:
            unsubscribe()
