# ChurchBridge AI

A discourse-aware real-time sermon interpreter. Accepts live Spanish sermon audio from a soundboard, tolerates English code-switching, assembles speech into structurally complete thoughts, and distributes English captions to sanctuary displays and mobile listeners simultaneously.

The system does not simply translate sentences as they arrive. It holds partial utterances until it is confident a thought is complete, uses an LLM to classify discourse structure and detect when fragments should be merged, suppresses incomplete captions until they can be finalised, and corrects itself retroactively via caption merges.

## Documentation map

Use these entry points depending on what you are trying to do:

- Project/docs index: [`docs/README.md`](docs/README.md)
- Deployment and production operations: [`DEPLOYMENT.md`](DEPLOYMENT.md)
- Testing and benchmarks: [`TESTING_AND_BENCHMARKS.md`](TESTING_AND_BENCHMARKS.md)
- Technical directive and architecture guardrails: [`DIRECTIVE.md`](DIRECTIVE.md)
- Topic-tracker semantic memory plan: [`docs/plans/topic-tracker-semantic-memory-plan.md`](docs/plans/topic-tracker-semantic-memory-plan.md)

## How the pipeline works

```
Soundboard mic (browser)
  │
  └── PCM audio (Float32, resampled to 16kHz)
        │
        ▼
  Google Speech-to-Text V2 (Chirp 3, streaming)
    interim results ──► preview language router
                         • Spanish-dominant interim ──► Google preview translation
                         • English-dominant interim ──► passthrough preview
    final results ──► STT noise cleanup
                          │
                          ├── filler removal (Uh, Mmm, stutter collapses)
                          ├── Pentecostés/Pentecostales disambiguation
                          ├── detected-language metadata (Spanish / English)
                          └── sentence boundary splitting
                                    │
                                    ▼
                          SentenceBuffer (discourse-aware gating)
                            holds text until:
                              • terminal punctuation detected
                              • utterance-end / VAD signal from Google Speech
                              • fallback timer (3.5s + extensions)
                            extends when:
                              • trailing connector word detected (que, porque, es…)
                              • unclosed interrogative (¿ without ?)
                              • < 4 words accumulated
                              • LLM signals continuation_required on prior sentence
                                    │
                                    ▼ (complete thought)
                          Language router
                            • Spanish-dominant sentence ──► Google Translate fast path
                            • English-dominant sentence ──► passthrough commit
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
                  translation_update      adaptive deferred release
                  broadcast immediately   │
                                          ├── if merge arrives → caption_merge broadcast
                                          │    (head-anchored: earliest segment stays,
                                          │     fragments absorbed into it, full merged
                                          │     English covers the complete thought)
                                          └── if no merge → release after 2.0–4.5s,
                                               depending on fragment risk
                                    │
                                    ▼ (separate async call, per display_ready sentence)
                          Verse Suggestions (Claude Haiku, lightweight)
                            suggests 1–3 cross-reference verses
                            suppressed for pending/fragmented/procedural sentences
```

## Key design decisions

**Discourse-aware buffering, not sentence-boundary translation.** The `SentenceBuffer` extends its timer whenever the accumulated text ends with a structural incomplete signal — a preposition, dangling copula, unclosed question, or fewer than four words. The LLM's `continuation_required` signal feeds back into the buffer to extend future boundaries when the prior sentence was incomplete.

**display_ready is the authoritative emission gate.** The LLM computes `display_ready` and the server enforces it deterministically: `thought_complete AND NOT continuation_required AND quality != "fragmented" AND tag != "quote_introduction"`. The LLM's value can only make it more restrictive, never relax it. When false, the Google translation is suppressed until a merge arrives or an adaptive deferred-release timeout emits the best safe fallback.

**Head-anchored caption chains.** When the LLM signals `merge_with_previous`, the system always keeps the earliest segment in the chain on screen and absorbs subsequent fragments into it. A 3-fragment chain produces a single stable caption at the original screen position — no visual jumping.

**Two-tier translation.** Spanish sentences go through Google Translate as the fast path; English-dominant sentences bypass translation and pass straight through. The same language routing now applies to interim preview text, so quick display no longer tries to mistranslate English code-switching as Spanish. Claude runs asynchronously and fires `translation_update` only when `display_ready` is true. Feed commits also use adaptive timing: clearly complete captions commit faster, while tiny bridge fragments wait slightly longer so merge repair can cancel unsafe commits.

**Verse suggestions are decoupled from structural decisions.** Verse detection stays in the main enrichment call (it shares sentence context). Suggestions run as a separate lightweight async call that cannot compete with structural decisions for prompt attention.

**Sermon state tracking.** Each sentence's `sermon_mode` (scripture, exposition, illustration, application, exhortation, procedural) feeds a debounced `SermonStateTracker`. Mode signals are used to gate verse suggestions, tune illustration-mode behaviour, and inform the `TopicTracker`'s rolling theological summary.

## Architecture

```
Soundboard Admin (Browser)
    │
    └── WebSocket ──► FastAPI /api/stream/v1
                          │
                          ├── Google Speech V2 (STT, Chirp 3 streaming)
                          ├── SentenceBuffer (discourse-aware gating)
                          ├── Google Translate / English passthrough
                          ├── Claude Haiku (enrichment + verse suggestions)
                          ├── SermonStateTracker (mode detection)
                          ├── TopicTracker (rolling theological context)
                          └── Broadcaster (in-process or Redis pub/sub)
                                  │
                                  ├── /api/display/v1 → Sanctuary Display (kiosk)
                                  └── /api/listen/v1  → Mobile PWA (QR code)
```

## Long-term target pipeline

The current system is discourse-aware and bilingual at the sentence level. The
ideal long-term system is **speaker-aware, mixed-language aware, and display-
aware at the segment level**.

### Target design principles

**Speaker boundaries are structural signals, not optional metadata.**
Chirp 3 diarization should shape merge decisions, translation routing, and UI
rendering the same way punctuation and discourse tags do today.

**Code-switching is a first-class mode.**
A segment that contains both English and Spanish should not be flattened into
"mostly Spanish" or "mostly English". The pipeline should explicitly represent
mixed-language segments and render them accordingly.

**The display should distinguish original speech from interpreted speech.**
The congregation should be able to tell whether a line is:

- direct English passthrough
- Spanish translated into English
- a mixed-language segment
- a leader line, congregational response, or other speaker role

### Ideal future flow

```text
Soundboard / house mix / mobile source
  │
  └── PCM audio
        │
        ▼
  Google Speech-to-Text V2 (Chirp 3, streaming, diarization enabled)
    emits:
      • transcript text
      • detected language(s)
      • word timing
      • word confidence
      • per-word speaker labels
        │
        ▼
  STT metadata shaping
    derives:
      • speaker_segments
      • dominant_speaker
      • speaker_switch_count
      • mixed_speaker_segment
      • segment_language_mode = english | spanish | mixed
      • low-confidence / fragmented flags
        │
        ▼
  Discourse-aware SentenceBuffer
    uses:
      • punctuation / VAD / timing
      • continuation heuristics
      • speaker-change guardrails
      • language-switch guardrails
        │
        ▼
  Translation router
    • english  → passthrough
    • spanish  → Google fast translation
    • mixed    → bilingual segment builder
                 (passthrough English spans, translate Spanish spans)
        │
        ▼
  LLM enrichment
    sees:
      • discourse context
      • topic / sermon mode context
      • speaker context
      • language-mode context
    decides:
      • display_ready
      • merge_with_previous
      • translation improvement
      • verse detection / suggestions
      • speaker-role hints (leader / congregation / reader / interpreter / unknown)
        │
        ▼
  Segment model for clients
    includes:
      • source_language
      • display_language
      • speaker_id
      • speaker_role
      • is_passthrough
      • is_translation
      • is_mixed_segment
      • phrase / span alignment where available
        │
        ├── Sanctuary display
        │     • stable lower-third English
        │     • optional language / speaker badges
        │
        └── Mobile listener
              • committed transcript
              • live preview
              • optional bilingual and speaker-aware rendering
```

### The most important future upgrades

**1. Make diarization first-class**

Store and propagate more than `speaker_tags`. The server should preserve
contiguous speaker runs, per-run confidence, and whether the fragment changed
speaker mid-thought. The LLM should see speaker transitions before it decides
to merge caption fragments.

**2. Add a true `mixed` translation mode**

Today routing is sentence-level: English passthrough or Spanish translation.
Long term, mixed segments should be shaped explicitly so the pipeline can:

- passthrough English spans
- translate Spanish spans
- preserve the order of bilingual speech
- avoid mistranslating English code-switches as Spanish content

**3. Use speaker and language boundaries as merge guardrails**

The merge path should strongly penalize or block merges when:

- `dominant_speaker` changes
- `speaker_switch_count > 0`
- the language family flips between adjacent fragments

unless lexical continuation evidence is overwhelming.

**4. Separate speaker identity from speaker role**

Chirp diarization yields anonymous speaker ids; the pipeline should keep that
authoritative signal and optionally infer higher-level roles such as:

- `leader`
- `congregation`
- `reader`
- `interpreter`
- `unknown`

Role inference should remain best-effort and never override raw speaker ids.

**5. Stop treating every caption as a single-language ribbon**

Client payloads should evolve toward a segment model that can render:

- `[EN] All right, welcome to Lakeview Church...`
- `[ES→EN] Vamos, canta. → Come on, sing.`

without pretending both are the same kind of line.

### Recommended implementation order

Phase 1 — Metadata and capture

- request diarization by default for bilingual / multi-speaker capture runs
- preserve per-word speaker labels and build `speaker_segments`
- add capture metrics for code-switching, speaker switches, and stream restarts

Phase 2 — Interpretation guardrails

- add `segment_language_mode = english | spanish | mixed`
- inject speaker / language context into the enrichment prompt
- penalize cross-speaker and cross-language merges

Phase 3 — Display model

- expand segment payloads with language and speaker metadata
- add UI badges and mixed-segment treatment
- later support richer per-span bilingual rendering

This sequencing keeps the highest-risk structural decisions in the backend
where they can be benchmarked before the UI grows more expressive.

### Current implementation status

- `segment_language_mode` now flows through the live session pipeline and into
  capture artifacts.
- The enrichment prompt now receives segment speaker/language structure when it
  is available, and merge decisions are conservatively blocked across language
  flips or speaker-structure conflicts.
- The capture runner records speaker/language-mode summaries even for audio
  without an `.srt`.
- Google Speech streaming currently rejects speaker diarization on this live
  path. When diarization is requested, the server now logs the limitation and
  automatically retries without diarization so the live interpreter does not
  fail dark.

## Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI (Python), asyncio throughout |
| STT | Google Cloud Speech-to-Text V2 (`chirp_3`) with phrase adaptation, bilingual `languageCodes`, and optional diarization |
| Fast translation | Google Cloud Translation API for Spanish; direct passthrough for English-dominant STT segments |
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
                                    cleanup, language routing, sentence splitting,
                                    discourse holds
        sentence_buffer.py        — Discourse-aware flush gating; incomplete-tail
                                    detection; UtteranceEnd soft guard
        google_speech_session.py  — Google Speech streaming session + restart logic
        stt.py                    — STT provider config, languageCodes, VAD, diarization
        google_translate_service.py — Fast-path translation + dual-pass correction
        llm_enrichment_service.py — Claude enrichment: translation improvement,
                                    discourse classification, display_ready gating,
                                    head-anchored caption merge chains,
                                    verse detection, deferred translation release
        topic_tracker.py          — Rolling theological context via adaptive LLM
                                    summarisation (sermon arc, key themes, mode)
        sermon_state_tracker.py   — Debounced sermon mode from per-sentence signals
        broadcaster.py            — In-process pub/sub; Redis when available
        mobile_diagnostics.py     — Remote browser/mobile diagnostics reports
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
| `GOOGLE_CLOUD_PROJECT` | Google Speech-to-Text project id |
| `GOOGLE_TRANSLATE_API_KEY` | Fast-path translation |
| `ANTHROPIC_API_KEY` | Claude enrichment + verse suggestions |

Google Speech also needs usable Google Cloud credentials in the local environment, typically through `GOOGLE_APPLICATION_CREDENTIALS` or another Application Default Credentials flow.

Useful optional STT variables:

| Variable | Purpose |
|---|---|
| `GOOGLE_SPEECH_LANGUAGE_CODES` | Comma-separated language list for STT, e.g. `es-US,en-US` |
| `GOOGLE_SPEECH_LANGUAGE` | Primary STT language if `GOOGLE_SPEECH_LANGUAGE_CODES` is not set |
| `GOOGLE_SPEECH_SECONDARY_LANGUAGE` | Optional secondary STT language |
| `GOOGLE_SPEECH_MODEL` | STT model override; defaults to `chirp_3` |

Place `.env` in the repository root. For Next.js, add `client/.env.local` and set `NEXT_PUBLIC_WS_URL` or `NEXT_PUBLIC_API_URL` only if you need to override defaults (they target `localhost:8000` for local dev).

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
| Mic → Google STT interim | ~150ms |
| Google STT final → Google translation | ~250ms |
| Google translation / passthrough → display | ~300ms total |
| LLM enrichment (async, non-blocking) | ~600–1200ms |
| LLM translation update → display | fires only when `display_ready=true` |
| Deferred release fallback | adaptive, typically ~2.0–4.5s if no merge |

The audience sees interim preview text first, then either a fast Google translation or an English passthrough caption within roughly the same window after a sentence finalises. The LLM-improved translation updates it silently a second later if the sentence is display-ready, or holds it until a merge assembles the complete thought.

## Benchmarking

The live pipeline benchmark is the closest check of the server-side production
pipeline:

```bash
server/.venv/Scripts/python.exe tests/benchmark/run_pipeline_test.py --audio-dir tests/audio/1 --translation-quality
```

It launches its own local server on port `8799` by default, captures the full
display event stream, writes the raw run JSON, and then updates the
self-improvement artifacts for that benchmark set.

When a recording does not yet have an `.srt` reference, use the capture-only
pipeline runner instead:

```bash
server/.venv/Scripts/python.exe tests/benchmark/run_capture_pipeline_test.py --audio-dir tests/audio/3 --duration 60 --allow-long-duration
```

This capture mode records the full event stream, timing, language counts, feed
commits, and revisions without attempting WER or scorecard evaluation.

Important: this is a replay harness, not a native-client microphone benchmark.
It validates the backend WebSocket/STT/translation/display path after audio has
already been prepared, but it does not validate iPhone/browser capture,
voice-processing, echo cancellation, AGC, route changes, or other front-end
audio behavior.

### Long recordings and offsets

The benchmark audio directories are long-form sources, not just one-shot test
cases. Use `--start-offset` with a bounded `--duration` to sample different
windows from the same recording and turn one dataset into several benchmark
segments.

This is especially useful for `tests/audio/1`: it is long enough that we can
pick cleaner windows as optimization targets without losing realism. By
contrast, clipped or noisy windows are still valuable, but they should be read
more as resilience checks than as the primary quality target.

A simple sweep against one recording looks like:

```bash
server/.venv/Scripts/python.exe tests/benchmark/run_pipeline_test.py --audio-dir tests/audio/1 --duration 30 --start-offset 0 --translation-quality
server/.venv/Scripts/python.exe tests/benchmark/run_pipeline_test.py --audio-dir tests/audio/1 --duration 30 --start-offset 30 --translation-quality
server/.venv/Scripts/python.exe tests/benchmark/run_pipeline_test.py --audio-dir tests/audio/1 --duration 30 --start-offset 60 --translation-quality
```

Recommended use:

- Use cleaner `tests/audio/1` windows as the main optimization target.
- Use clipped or noisy windows as stress tests for graceful degradation.
- Keep notes on which offsets represent clean baseline behavior versus damaged input.

### Concurrent benchmark runs

Concurrent pipeline runs need isolation. If two processes reuse the default
port or write into the same benchmark namespace, one run can fail to bind its
server or produce logs and loop artifacts that are difficult to interpret.

Use these rules when running more than one benchmark at once:

- Give each process a unique `--port`.
- Keep `--church-id` isolated if the runs share downstream infrastructure.
- Use separate `--results-root` values when you want fully independent loop
  history and reports.
- Prefer sequential runs unless you specifically need concurrency.

A safe concurrent pattern looks like:

```bash
server/.venv/Scripts/python.exe tests/benchmark/run_pipeline_test.py --audio-dir tests/audio/1 --translation-quality --port 8799 --church-id bench-a --results-root tests/benchmark/results/concurrent-a
server/.venv/Scripts/python.exe tests/benchmark/run_pipeline_test.py --audio-dir tests/audio/2 --translation-quality --port 8800 --church-id bench-b --results-root tests/benchmark/results/concurrent-b
```

### Translation safety and interpreting results

The live translation path is intentionally layered now:

- Google Translate is still the fast baseline shown first for Spanish content, including Spanish interim preview.
- English-dominant STT sentences and interim previews pass straight through without translation.
- Claude enrichment is validated before it can replace that baseline.
- Unsafe rewrites fall back to Google.
- Higher-risk cases can trigger a repair pass before release.
- Session-end incomplete captions are emitted with `...` so they are visibly truncated rather than looking complete.

That means future self-improvement runs should separate three different outcomes:

- True pipeline regressions: ordering, leaks, latency, unsafe rewrite behavior.
- Evaluator uncertainty: malformed quality JSON, low-confidence chunk analysis, or too few evaluated pairs.
- Expected stress-case degradation: clipped or damaged audio can lower WER and translation quality without implying the clean-path pipeline regressed.

Recommended benchmark interpretation:

- Treat clean `tests/audio/1` windows as primary optimization targets.
- Treat `tests/audio/1 --start-offset 60` as a translation-safety regression window.
- Treat clipped `tests/audio/2` windows as resilience checks, not the sole optimization target.
- Treat broad offset sweeps as exploratory sampling, not one combined trajectory for promotion/revert decisions.
