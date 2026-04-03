from dotenv import load_dotenv
load_dotenv(override=True)

import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.db.index import init_db
from server.services.broadcaster import Broadcaster
from server.services.session_manager import SessionManager
from server.routes import stream, display, listen, services

logging.basicConfig(level=logging.INFO)

REQUIRED = ["DEEPGRAM_API_KEY", "GOOGLE_TRANSLATE_API_KEY", "OPENAI_API_KEY"]
for key in REQUIRED:
    if not os.getenv(key):
        raise RuntimeError(f"Missing required environment variable: {key}")

broadcaster = Broadcaster()
session_manager: SessionManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global session_manager
    await init_db()
    await broadcaster.connect()
    session_manager = SessionManager(broadcaster)
    stream.set_session_manager(session_manager)
    display.set_broadcaster(broadcaster)
    listen.set_broadcaster(broadcaster)
    yield
    await broadcaster.disconnect()


app = FastAPI(title="ChurchBridge AI", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(stream.router)
app.include_router(display.router)
app.include_router(listen.router)
app.include_router(services.router)


@app.get("/health")
async def health():
    return {"ok": True, "redis": broadcaster._available}
