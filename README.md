# ChurchBridge AI

Real-time Spanish-to-English translation for houses of worship. Streams live speech through Deepgram STT, translates via GPT-4o-mini with theological context, and distributes to sanctuary displays and mobile listeners simultaneously.

## Architecture

```
Soundboard Admin (Browser)
    |
    └── WebSocket ──► FastAPI /api/stream/v1
                          |
                          ├── Deepgram (STT, nova-2, streaming)
                          │       └── interim results → display preview
                          │       └── final results → TranslationService
                          │
                          ├── OpenAI gpt-4o-mini (streaming translation)
                          │       └── church-specific terminology override
                          │
                          └── Redis Pub/Sub  church:{id}:translations
                                  |
                                  ├── /api/display/v1 → Sanctuary Display (kiosk)
                                  └── /api/listen/v1  → Mobile PWA (QR code)
```

## Stack

- **Backend:** FastAPI (Python), asyncio throughout
- **STT:** Deepgram nova-2 with keyword boosting for theological terms
- **Translation:** OpenAI gpt-4o-mini, streaming tokens, 3-segment context window
- **Pub/Sub:** Redis
- **Frontend:** Next.js 14 (App Router)
- **Database:** SQLite (church config, glossary, attempt history)

## Project Structure

```
server/
    main.py                  — FastAPI app, lifespan, router registration
    routes/
        stream.py            — WebSocket /api/stream/v1 (audio ingestion)
        display.py           — WebSocket /api/display/v1 (sanctuary output)
        listen.py            — WebSocket /api/listen/v1  (mobile output)
        services.py          — REST: church config, glossary management
    services/
        session_manager.py   — One session per church_id
        deepgram_session.py  — Deepgram streaming connection
        translation_service.py — LLM translation with context window
        broadcaster.py       — Redis pub/sub publisher
        audio_utils.py       — PCM resampling (Float32 → 16kHz linear16)
        prompt_manager.py    — System prompt + terminology injection
    db/
        index.py             — SQLite setup, migrations
        glossary.py          — church_glossary table
        church_terms.py      — church_terms translation overrides
        sessions.py          — Service session + transcript storage
client/
    app/
        admin/[churchId]/    — Soundboard Admin (audio capture + VU meter)
        display/[churchId]/  — Sanctuary Display (kiosk, lower thirds mode)
        listen/[churchId]/   — Mobile PWA (English only, optional TTS)
    components/
        SoundboardAdmin.tsx
        VUMeter.tsx
        TranslationDisplay.tsx
        MobileListener.tsx
    public/
        worklets/
            recorder-worklet.js
data/
    churchbridge.db          — SQLite (gitignored)
tests/
    server/
    fixtures/
```

## Setup

### Backend

```bash
cd server
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Run the API from the **repository root** (imports use the `server` package):

```bash
cd ..                          # back to churchbridge-ai root
python -m uvicorn server.main:app --reload --host 127.0.0.1 --port 8000
```

### Frontend

```bash
cd client
npm install
npm run dev
```

### Redis

```bash
docker run -d -p 6379:6379 redis:alpine
```

Without Redis, the server falls back to in-process broadcast (fine for a single machine; use Redis if you need pub/sub across processes or hosts).

### Environment

```bash
cp .env.example .env
# Fill in DEEPGRAM_API_KEY, GOOGLE_TRANSLATE_API_KEY, and OPENAI_API_KEY
```

Place `.env` in the repository root so the backend loads it. For Next.js, copy `client/.env.local.example` to `client/.env.local` if you need to override `NEXT_PUBLIC_WS_URL` or `NEXT_PUBLIC_API_URL` (defaults target `localhost:8000`).

## Running the app

Start these in separate terminals (after [Setup](#setup) once). Order: Redis → API → web UI.

| Step | What | Command |
|------|------|---------|
| 1 | **Redis** (optional but recommended) | `docker run -d -p 6379:6379 redis:alpine` |
| 2 | **Backend** (FastAPI + WebSockets) | From repo root, with `server/.venv` activated: `python -m uvicorn server.main:app --reload --host 127.0.0.1 --port 8000` |
| 3 | **Frontend** (Next.js) | `cd client && npm run dev` |

**URLs**

| Service | URL | Purpose |
|---------|-----|---------|
| Web UI | [http://localhost:3000](http://localhost:3000) | Admin, sanctuary display, and mobile listener pages (Next.js) |
| API | [http://127.0.0.1:8000](http://127.0.0.1:8000) | REST and WebSocket endpoints; [`/health`](http://127.0.0.1:8000/health) checks the process |

Open the **Next.js** URL for browser pages. Port **8000** is the API only—routes like `/display/[churchId]` live on port **3000** (for example `http://localhost:3000/display/default`).

## Latency Budget

| Stage | Target |
|---|---|
| Mic → server | ~30ms |
| Server → Deepgram interim | ~150ms |
| Deepgram final boundary | ~200ms |
| LLM first token (streaming) | ~300ms |
| Token → client | ~20ms |
| **Total (first translated word)** | **~700ms** |

## Key Design Decisions

- **Deepgram is the source of truth for transcription.** The LLM never scores or corrects the STT output directly — it only translates what Deepgram returns.
- **Translate on `is_final` only.** Interim results display as a preview but never enter the LLM pipeline — avoids burning tokens on incomplete utterances.
- **Streaming tokens to clients.** Translation tokens are pushed to the display the moment they arrive from OpenAI — no waiting for a complete sentence.
- **3-segment context window.** The last 3 Spanish utterances are included as context for each new translation call, preventing theological term drift mid-sermon.
