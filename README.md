# ChurchBridge AI

A discourse-aware real-time sermon interpreter. Accepts live Spanish speech from a soundboard, assembles it into structurally complete thoughts, translates with theological precision, and distributes captions to sanctuary displays and mobile listeners simultaneously.

The system does not simply translate sentences as they arrive. It holds partial utterances until it is confident a thought is complete, uses an LLM to classify discourse structure and detect when fragments should be merged, suppresses incomplete captions until they can be finalised, and corrects itself retroactively via caption merges.

## How the pipeline works

```
Soundboard mic (browser)
  │
  └── PCM audio (Float32, resampled to 16kHz)
        │
        ▼
  Deepgram STT (nova-2, streaming)
    interim results ──► display preview only
    final results ──► STT noise cleanup
                          │
                          ├── filler removal (Uh, Mmm, stutter collapses)
                          ├── Pentecostés/Pentecostales disambiguation
                          └── sentence boundary splitting
                                    │
                                    ▼
                          SentenceBuffer (discourse-aware gating)
                            holds text until:
                              • terminal punctuation detected
                              • UtteranceEnd signal from Deepgram
                              • fallback timer (3.5s + extensions)
                            extends when:
                              • trailing connector word detected (que, porque, es…)
                              • unclosed interrogative (¿ without ?)
                              • < 4 words accumulated
                              • LLM signals continuation_required on prior sentence
                                    │
                                    ▼ (complete thought)
                          Google Translate  ──►  fast English (< 300ms)
                                    │                │
                                    │                ▼
                                    │          broadcast: translation
                                    │          (displayed immediately)
                                    │
                                    ▼
                          LLM Enrichment (Claude Haiku, async)
                            computes per sentence:
                              • improved_translation
                              • discourse_tag, thought_complete, continuation_required
                              • display_ready  ← authoritative emission gate
                              • merge_with_previous  ← chain merge signal
                              • source_quality, translation_register
                              • sermon_mode  ← feeds SermonStateTracker
                              • verse_detected  ← feeds verse scratch pad
                                    │
                          ┌─────────┴──────────┐
                          │                    │
                    display_ready=true    display_ready=false
                          │               (thought incomplete/fragmented)
                          ▼                    │
                  translation_update      deferred 6s
                  broadcast immediately   │
                                          ├── if merge arrives → caption_merge broadcast
                                          │    (head-anchored: earliest segment stays,
                                          │     fragments absorbed into it, full merged
                                          │     English covers the complete thought)
                                          └── if no merge → release after 6s fallback
                                    │
                                    ▼ (separate async call, per display_ready sentence)
                          Verse Suggestions (Claude Haiku, lightweight)
                            suggests 1–3 cross-reference verses
                            suppressed for pending/fragmented/procedural sentences
```

## Key design decisions

**Discourse-aware buffering, not sentence-boundary translation.** The `SentenceBuffer` extends its timer whenever the accumulated text ends with a structural incomplete signal — a preposition, dangling copula, unclosed question, or fewer than four words. The LLM's `continuation_required` signal feeds back into the buffer to extend future boundaries when the prior sentence was incomplete.

**display_ready is the authoritative emission gate.** The LLM computes `display_ready` and the server enforces it deterministically: `thought_complete AND NOT continuation_required AND quality != "fragmented" AND tag != "quote_introduction"`. The LLM's value can only make it more restrictive, never relax it. When false, the Google translation is suppressed until a merge arrives or a 6-second fallback releases it.

**Head-anchored caption chains.** When the LLM signals `merge_with_previous`, the system always keeps the earliest segment in the chain on screen and absorbs subsequent fragments into it. A 3-fragment chain produces a single stable caption at the original screen position — no visual jumping.

**Two-tier translation.** Google Translate provides a < 300ms fast path that displays immediately. Claude runs asynchronously and fires `translation_update` only when `display_ready` is true. For merged sentences, the LLM receives the pending sentence's Spanish and English explicitly and must write a unified translation for the full merged unit.

**Verse suggestions are decoupled from structural decisions.** Verse detection stays in the main enrichment call (it shares sentence context). Suggestions run as a separate lightweight async call that cannot compete with structural decisions for prompt attention.

**Sermon state tracking.** Each sentence's `sermon_mode` (scripture, exposition, illustration, application, exhortation, procedural) feeds a debounced `SermonStateTracker`. Mode signals are used to gate verse suggestions, tune illustration-mode behaviour, and inform the `TopicTracker`'s rolling theological summary.

## Architecture

```
Soundboard Admin (Browser)
    │
    └── WebSocket ──► FastAPI /api/stream/v1
                          │
                          ├── Deepgram (STT, nova-2, streaming)
                          ├── SentenceBuffer (discourse-aware gating)
                          ├── Google Translate (fast path)
                          ├── Claude Haiku (enrichment + verse suggestions)
                          ├── SermonStateTracker (mode detection)
                          ├── TopicTracker (rolling theological context)
                          └── Broadcaster (in-process or Redis pub/sub)
                                  │
                                  ├── /api/display/v1 → Sanctuary Display (kiosk)
                                  └── /api/listen/v1  → Mobile PWA (QR code)
```

## Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI (Python), asyncio throughout |
| STT | Deepgram nova-2 with keyword boosting for theological terms |
| Fast translation | Google Cloud Translation API |
| Enrichment LLM | Anthropic Claude Haiku (claude-haiku-4-5) |
| Frontend | Next.js (App Router) |
| Pub/Sub | In-process broadcaster; optional Redis |
| Database | SQLite (church config, glossary, sessions, verse detections) |

## Project structure

```
server/
    main.py                       — FastAPI app, lifespan, router registration
    routes/
        stream.py                 — WebSocket /api/stream/v1 (audio ingestion)
        display.py                — WebSocket /api/display/v1 (sanctuary output)
        listen.py                 — WebSocket /api/listen/v1  (mobile output)
        services.py               — REST: church config, glossary management
    services/
        session_manager.py        — One ServiceSession per church_id; STT noise
                                    cleanup, sentence splitting, discourse holds
        sentence_buffer.py        — Discourse-aware flush gating; incomplete-tail
                                    detection; UtteranceEnd soft guard
        deepgram_session.py       — Deepgram streaming connection + callbacks
        google_translate_service.py — Fast-path translation + dual-pass correction
        llm_enrichment_service.py — Claude enrichment: translation improvement,
                                    discourse classification, display_ready gating,
                                    head-anchored caption merge chains,
                                    verse detection, deferred translation release
        topic_tracker.py          — Rolling theological context via adaptive LLM
                                    summarisation (sermon arc, key themes, mode)
        sermon_state_tracker.py   — Debounced sermon mode from per-sentence signals
        broadcaster.py            — In-process pub/sub; Redis when available
        audio_utils.py            — PCM resampling (Float32 → 16kHz linear16)
    db/
        index.py                  — SQLite setup, schema migrations
        glossary.py               — church_glossary table
        church_terms.py           — per-church translation overrides
        sessions.py               — service session + transcript storage
        verses.py                 — verse detection + suggestion persistence
        modes.py                  — mode transition logging
client/
    app/
        admin/[churchId]/         — Soundboard Admin (audio capture + VU meter)
        display/[churchId]/       — Sanctuary Display (kiosk, lower thirds)
        listen/[churchId]/        — Mobile PWA (English only)
    lib/
        useTranslationFeed.ts     — WebSocket feed: segments, pendingCompletion,
                                    caption merge, verse detection, mode changes
    components/
        SoundboardAdmin.tsx
        TranslationDisplay.tsx
        MobileListener.tsx
data/
    churchbridge.db               — SQLite (gitignored)
```

## Setup

### Backend

```bash
cd server
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Run the API from the **repository root**:

```bash
python -m uvicorn server.main:app --reload --host 127.0.0.1 --port 8000
```

### Frontend

```bash
cd client
npm install
npm run dev
```

### Redis (optional)

```bash
docker run -d -p 6379:6379 redis:alpine
```

Without Redis, the server uses in-process broadcast. Use Redis only if you need pub/sub across multiple processes or hosts.

### Environment

```bash
cp .env.example .env
```

Required variables:

| Variable | Purpose |
|---|---|
| `DEEPGRAM_API_KEY` | Deepgram STT streaming |
| `GOOGLE_TRANSLATE_API_KEY` | Fast-path translation |
| `ANTHROPIC_API_KEY` | Claude enrichment + verse suggestions |

Place `.env` in the repository root. For Next.js, copy `client/.env.local.example` to `client/.env.local` if you need to override `NEXT_PUBLIC_WS_URL` or `NEXT_PUBLIC_API_URL` (defaults target `localhost:8000`).

## Running

Start in order: Redis (optional) → backend → frontend.

| Step | Command |
|---|---|
| Redis | `docker run -d -p 6379:6379 redis:alpine` |
| Backend | From repo root, venv active: `python -m uvicorn server.main:app --reload --host 127.0.0.1 --port 8000` |
| Frontend | `cd client && npm run dev` |

| Service | URL |
|---|---|
| Web UI | http://localhost:3000 |
| Admin | http://localhost:3000/admin/`{churchId}` |
| Sanctuary display | http://localhost:3000/display/`{churchId}` |
| Mobile listener | http://localhost:3000/listen/`{churchId}` |
| API health | http://127.0.0.1:8000/health |

## Latency profile

| Stage | Typical |
|---|---|
| Mic → Deepgram interim | ~150ms |
| Deepgram final → Google translation | ~250ms |
| Google translation → display | ~300ms total |
| LLM enrichment (async, non-blocking) | ~600–1200ms |
| LLM translation update → display | fires only when `display_ready=true` |
| Deferred release fallback | 6s after enrichment if no merge |

The audience sees the Google translation within ~300ms of a sentence finalising. The LLM-improved translation updates it silently a second later if the sentence is display-ready, or holds it until a merge assembles the complete thought.
