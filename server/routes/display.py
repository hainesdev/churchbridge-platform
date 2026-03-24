import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

logger = logging.getLogger(__name__)
router = APIRouter()

_broadcaster = None


def set_broadcaster(b):
    global _broadcaster
    _broadcaster = b


@router.websocket("/api/display/v1")
async def display_ws(
    ws: WebSocket,
    church_id: str = Query(...),
):
    """Sanctuary display WebSocket. Subscribes to Redis and forwards all events.

    Receives:
      { type: 'interim',       text: string,  ts: number }
      { type: 'final_spanish', text: string,  ts: number }
      { type: 'token',         text: string,  ts: number }
      { type: 'translation',   spanish: string, english: string, ts: number }
    """
    await ws.accept()

    if _broadcaster._available:
        try:
            async for payload in _broadcaster.subscribe(church_id):
                await ws.send_text(payload)
        except WebSocketDisconnect:
            logger.info("[display] Client disconnected for church %s", church_id)
        except Exception as e:
            logger.error("[display] Error for church %s: %s", church_id, e)
    else:
        # In-process fallback (dev without Redis)
        import asyncio
        queue: asyncio.Queue = asyncio.Queue()
        unsubscribe = _broadcaster.subscribe_local(church_id, queue.put)
        try:
            while True:
                payload = await queue.get()
                await ws.send_text(payload)
        except WebSocketDisconnect:
            pass
        finally:
            unsubscribe()
