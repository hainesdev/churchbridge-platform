from dotenv import load_dotenv
load_dotenv(override=True)

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.db.index import init_db
from server.services.broadcaster import Broadcaster

# Validate required env vars on startup
REQUIRED = ["DEEPGRAM_API_KEY", "OPENAI_API_KEY"]
for key in REQUIRED:
    if not os.getenv(key):
        raise RuntimeError(f"Missing required environment variable: {key}")

broadcaster = Broadcaster()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await broadcaster.connect()
    yield
    await broadcaster.disconnect()


app = FastAPI(title="ChurchBridge AI", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers registered here as each is built
# from server.routes.stream import router as stream_router
# from server.routes.display import router as display_router
# from server.routes.listen import router as listen_router
# from server.routes.services import router as services_router
# app.include_router(stream_router)
# app.include_router(display_router)
# app.include_router(listen_router)
# app.include_router(services_router)


@app.get("/health")
async def health():
    return {"ok": True}
