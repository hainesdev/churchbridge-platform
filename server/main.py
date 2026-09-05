from dotenv import load_dotenv
load_dotenv(override=True)

import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from server.db.index import init_db
from server.services.broadcaster import Broadcaster
from server.services.mobile_diagnostics import MobileDiagnosticsHub
from server.services.session_manager import SessionManager
from server.routes import stream, display, listen, services, mobile_diagnostics

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s:%(message)s",
    force=True,
)
for _name in ("server.services", "server.db", "server.routes"):
    logging.getLogger(_name).setLevel(logging.INFO)

REQUIRED = ["GOOGLE_TRANSLATE_API_KEY", "ANTHROPIC_API_KEY"]
for key in REQUIRED:
    if not os.getenv(key):
        raise RuntimeError(f"Missing required environment variable: {key}")

broadcaster = Broadcaster()
mobile_diagnostics_hub = MobileDiagnosticsHub()
session_manager: SessionManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global session_manager
    await init_db()
    await broadcaster.connect()
    session_manager = SessionManager(broadcaster)
    stream.set_session_manager(session_manager)
    services.set_session_manager(session_manager)
    display.set_broadcaster(broadcaster)
    listen.set_broadcaster(broadcaster)
    mobile_diagnostics.set_broadcaster(broadcaster)
    mobile_diagnostics.set_mobile_diagnostics_hub(mobile_diagnostics_hub)
    yield
    await broadcaster.disconnect()


app = FastAPI(title="ChurchBridge AI", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static assets served to clients — currently the compact Bible database the
# iOS app downloads from /api/static/bible_ios.sqlite. The directory sits beside
# the database on the persistent volume so it survives container rebuilds, and
# is created here so a fresh deployment starts cleanly rather than failing to
# boot on a missing path.
_DB_PATH = os.getenv("DATABASE_URL", "data/churchbridge.db").replace("sqlite:///./", "")
STATIC_DIR = os.getenv(
    "STATIC_DIR", os.path.join(os.path.dirname(_DB_PATH) or ".", "static")
)
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/api/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(stream.router)
app.include_router(display.router)
app.include_router(listen.router)
app.include_router(services.router)
app.include_router(mobile_diagnostics.router)


@app.get("/health")
async def health():
    return {"ok": True, "redis": broadcaster.available}
