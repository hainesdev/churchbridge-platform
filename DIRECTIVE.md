# ChurchBridge AI — Technical Directive

This document captures the goals, architecture decisions, API choices, and
reasoning behind the ChurchBridge AI system. It is a living document. Update
it when a significant decision is made or reversed.

---

## Mission

Provide real-time, high-quality Spanish → English sermon translation for live
Pentecostal church services. The congregation receives captions on a display
screen or mobile device within ~1 second of the pastor speaking.

The system must handle:
- Pentecostal preaching register (fast, emotional, interjection-heavy)
- Theological vocabulary (scripture references, church terms)
- Spontaneous structure (no script, sentence boundaries are implicit)
- Imperfect audio environments (reverb, congregation noise, music)

Accuracy of theological content takes priority over low latency. A 1–2 second
delay is acceptable; a mistranslated scripture verse is not.

---

## Architecture Overview

```
Browser mic (Float32 48kHz)
    │
    ▼
[audio_utils] resample → PCM16 16kHz           (server-side; browser migration planned)
    │
    ▼
[DeepgramSession] streaming WebSocket (nova-3, es)
    │
    ├─ interim transcript ──────────────────────────────────────► display (live typing)
    │
    └─ is_final transcript
          │
          ├─ broadcast raw stt_final (operator stream — unmodified)
          │
          ├─ _clean_stt() ─── noise removal (fillers, stutters, Pentecostés, Santo)
          │
          ├─ GoogleTranslateService.translate_fragment()
          │   └─ interim_translation ──────────────────────────► display (fast preview)
          │
          └─ SentenceBuffer.add()
                │
                │  Flush hierarchy (priority order):
                │  1. Terminal punctuation (.?!…;)
                │  2. UtteranceEnd VAD event (with incomplete-tail soft guard)
                │  3. ABSOLUTE_MAX_WORDS = 60 (unconditional safety valve)
                │  4. Fallback timer (3.5s + discourse holds, up to 8 extensions × 2s)
                │
                ▼
           _on_sentence (complete sentence)
                │
                ├─ TopicTracker.add_segment()        (rolling sermon context; updates async)
                │
                ├─ Discourse hold checks (synchronous)
                │   ├─ quote_introduction → hold_next(4.0s)
                │   └─ rhetorical_question → hold_next(2.0s)
                │
                └─ GoogleTranslateService.translate()
                      │
                      ├─ dual-pass correction ────────────────► display (silent update)
                      │
                      └─ translation ──────────────────────────► display (committed)
                            │
                            └─ LLMEnrichmentService.enrich()   [async, fire-and-forget]
                                  │
                                  │  Context injected per call:
                                  │  ├─ TopicTracker.get_context() (sermon summary)
                                  │  ├─ SermonStateTracker.settled_mode
                                  │  ├─ previous sentence discourse metadata
                                  │  └─ church-specific theological glossary
                                  │
                                  ├─ improved_translation ─────► display (replaces Google)
                                  │   [suppressed if display_ready=false until merge
                                  │    or 6s deferred-release timeout]
                                  │
                                  ├─ discourse_tag ─────────────► client metadata
                                  ├─ thought_complete / continuation_required
                                  │   └─ on_buffer_hold ────────► SentenceBuffer.hold_next()
                                  │
                                  ├─ merge_with_previous ───────► caption_merge event
                                  │   [absorb_ts removed; keep_ts updated with merged text]
                                  │
                                  ├─ verse_detected ────────────► verse_detected event
                                  │   [consolidated via verse scratch pad; gap=45s]
                                  │   └─ verse_range_update ────► extends active passage
                                  │
                                  ├─ verse_suggestion ──────────► congregation display
                                  │
                                  ├─ sermon_mode ───────────────► SermonStateTracker
                                  │   [3-sentence settling; fires mode_change on transition]
                                  │
                                  ├─ paragraph_break ───────────► visual separator
                                  ├─ translation_register ──────► client metadata
                                  ├─ source_quality ────────────► client metadata
                                  └─ _enrichment_settled ───────► suppresses stale corrections
```

Three-pass translation is intentional:

- **Google fragment** (~400ms): interim display while the sentence is still arriving.
  Cancelled and superseded when the full sentence translation arrives.
- **Google sentence** (~400ms): full sentence with context injection and dual-pass
  correction. Appears on screen while the LLM works.
- **Claude second** (~600–900ms): enriches, fixes theological terms, detects scripture,
  merges fragments, classifies discourse, controls buffer holds. Replaces Google
  silently when improved. Suppressed until `display_ready=true` when the sentence
  is fragmented or incomplete.

---

## Component Decisions

### Speech-to-Text: Deepgram nova-3

**Why Deepgram:**
- Best-in-class WER on Spanish religious speech
- Streaming WebSocket API with low latency (interim results every ~200ms)
- `UtteranceEnd` VAD event is a reliable "speaker paused" signal
- `keyterms` parameter boosts theological vocabulary in the model
- Pre-recorded API allows reproducible offline benchmarking

**Why nova-3 (not nova-2):**
- Released 2025; significant WER improvement on Spanish
- Better handling of proper nouns (biblical names, locations)
- `keyterms` replaces deprecated `keywords:boost` from nova-2

**Current streaming parameters:**
| Parameter | Value | Reason |
|---|---|---|
| `model` | `nova-3` | Best available for Spanish |
| `language` | `es` | Target language |
| `punctuate` | `true` | Adds `.?!` — primary SentenceBuffer flush signal |
| `smart_format` | `true` | Numbers, dates, abbreviations |
| `utterance_end_ms` | `2000` | Preachers pause 2–3s mid-clause; 1500ms caused false fires |
| `vad_events` | `true` | Enables UtteranceEnd events |
| `interim_results` | `true` | Enables live typing effect |
| `encoding` | `linear16` | PCM16 — lossless, low overhead |
| `sample_rate` | `16000` | Downsampled to 16 kHz in the browser AudioWorklet before transmission |

**Audio path:** The browser AudioWorklet downsamples from the native AudioContext
rate (48 kHz or 44.1 kHz) to 16 kHz using a step-accumulator before posting chunks
to the main thread. The main thread base64-encodes and sends these 16 kHz Float32
chunks over WebSocket. The server decodes and converts Float32→PCM16 with no
resampling (fast path: `src_rate == dst_rate`). This keeps the WebSocket payload
at ⅓ of the native rate and eliminates server-side CPU for resampling.

---

### Primary Translation: Google Cloud Translation v2

**Why Google Translate:**
- ~400ms latency — acceptable for a live display
- Context injection (send prior sentences alongside current) dramatically
  improves theological term disambiguation
- Dual-pass correction: retranslate previous sentence with new trailing context;
  silently correct if improved
- Cost-effective at scale

**Two translation tracks:**
- **Fragment track** (`translate_fragment`): translates each Deepgram `is_final`
  immediately, showing interim English as the sentence builds. Cancelled when the
  full sentence translation arrives.
- **Sentence track** (`translate`): called when SentenceBuffer flushes. Uses up to
  2 prior sentences as context, and retranslates the prior sentence with new trailing
  context for dual-pass correction.

**Why not DeepL:**
- Tested; Google outperforms DeepL on Pentecostal sermon register
- Google handles theological names (Jesucristo, Espíritu Santo) more reliably

**Migration target:** Google Cloud Translation v3 Advanced (AutoML glossary
injection, congregation-specific example pairs). Blocked on: cost evaluation.

---

### Enrichment: Claude Haiku (claude-haiku-4-5-20251001)

**Why Claude (not GPT):**
- Haiku is extremely fast for a structured JSON task (~600–900ms)
- Claude follows JSON schema instructions more reliably than GPT-4o-mini at
  the same price tier
- Anthropic's models have better theological vocabulary recognition

**What enrichment does (all in one API call, fire-and-forget):**
1. Improves Google translation (theological register, "transmit" → "share")
2. Controls `display_ready` — suppresses fragmented or incomplete sentences until
   a merge resolves them or the 6-second deferred-release timeout fires
3. `merge_with_previous` — collapses two segments into one caption via `caption_merge`
   (e.g. rhetorical question + answer, quote introduction + scripture text)
4. Classifies discourse (`discourse_tag`: rhetorical_question, scripture_quote, etc.)
5. Detects `thought_complete` / `continuation_required` — feeds back to buffer holds
6. Detects Bible verses (explicit citations and quoted text) — consolidated via verse
   scratch pad with a 45-second gap threshold before emitting `verse_range_update`
7. Suggests related verses for the congregation (separate async call, gated by mode)
8. Classifies `sermon_mode` — fed into SermonStateTracker for settling
9. Signals `paragraph_break` for visual separators
10. Provides `translation_register` and `source_quality` metadata for client display logic

**Tradeoff:** Fire-and-forget — enrichment never blocks Google translation.
If Claude is slow or fails, Google translation still appears. The worst case
is the Google translation staying on screen (which is acceptable).

**`display_ready` suppression:** When a sentence is flagged `display_ready=false`
(fragmented, incomplete, or a quote introduction), the improved translation is
held in `_deferred_updates`. If a `merge_with_previous` arrives, the deferred
translation is superseded by the merged caption. If no merge arrives within 6
seconds, the deferred translation is released as-is.

---

### Sentence Buffer

**Why we buffer at all:**
Deepgram `is_final` events do not correspond to grammatical sentences. A 15-word
sentence may arrive as 3 separate finals over 4 seconds. Displaying each
fragment independently produces choppy, confusing captions.

**Flush hierarchy (priority order):**
1. Terminal punctuation (`.?!…;`) from Deepgram `punctuate=true`
2. `UtteranceEnd` VAD event (soft guard applies if incomplete tail detected)
3. Word count safety valve (ABSOLUTE_MAX_WORDS = 60)
4. Fallback timer (3.5s) with incomplete-tail extension (up to 8× × 2.0s = ~19.5s cap)

**Incomplete tail detection:**
The buffer refuses to flush on timer/UtteranceEnd if the accumulated text ends
with a preposition, conjunction, article, dangling verb, or opens an unresolved
conditional (`Si...` without apodosis). This prevents half-sentences from
appearing on screen.

**Discourse holds:**
The session layer calls `hold_next(reason, secs)` to extend the next timer flush.
Two sources:
- Synchronous (in `_on_sentence`): quote introduction → +4s, rhetorical question → +2s
- Async (from LLM): `continuation_required=true` → `on_buffer_hold` → hold

Multiple holds keep the maximum, not the sum.

---

### Sermon State Tracker

Accumulates per-sentence `sermon_mode` signals from LLM enrichment and emits a
stable "settled mode" only after 3 consecutive sentences agree on a new mode.
This prevents a single ambiguous sentence from flipping the mode.

**Modes:** `scripture`, `exposition`, `illustration`, `application`, `exhortation`, `procedural`

**Used by LLMEnrichmentService to:**
- Gate verse suggestions (suppressed during illustration/exhortation/procedural)
- Guard the verse scratch pad against false detections during narrative
- Inject a `[CURRENT MODE]` context block into each enrichment prompt

**Fires `mode_change` events** to the client and persists transitions to DB.

---

### Topic Tracker

Maintains a rolling structured summary of sermon content, updated on an adaptive
schedule. The summary is injected into every enrichment prompt as `[SERMON CONTEXT]`.

**Update schedule:**
- Eager first summary: after 3 segments (captures the passage announcement)
- First 10 minutes: update every 60 seconds
- After 10 minutes: update every 180 seconds

**Summary structure:** `summary`, `key_themes`, `illustration_subject`,
`sermon_arc`, `rhetorical_goal` — formatted as a short natural-language context block.

**Active passage injection:** When a verse is detected, `set_active_passage()` injects
the reference immediately into `get_context()` — before the next scheduled summary —
so subsequent enrichment calls know what passage is being expounded.

---

### STT Noise Cleaning

Applied in `_clean_stt()` before segmentation, translation, and buffering.
The raw STT text is still broadcast for operator visibility.

| Pattern | Action | Reason |
|---|---|---|
| `AAA`, `Uh`, `Mmm`, `Este...` | Remove | Multi-char filler sounds |
| `que que`, `el el`, `de de`, etc. | Collapse | Stutter on enumerated Spanish function words only |
| `A a Cristo` | Collapse to last | Same-char stutter |
| `Santo tú/él/ella...` | Remove | Interjection before pronoun |
| `^Santo` (sentence-initial) | Remove | Bare exclamation noise |
| `Pentecostés` (people context) | → `Pentecostales` | Feast name vs people |
| `Pentecostés` (no anchor) | Remove | Pure STT noise prefix |

**Function-word stutter allowlist:** Only a fixed set of Spanish function words
(prepositions, articles, conjunctions, relative pronouns) are collapsed when doubled.
Lexical words ("muy muy", "bien bien") are preserved — deliberate repetition is common
in Pentecostal preaching register and carries meaning.

---

### Verse Detection and Scratch Pad

**Detection:** LLM enrichment returns `verse_detected` when it identifies an explicit
citation ("Juan 3:16", "Romanos 8 versículo 28") or a confident quotation.

**Scratch pad consolidation:** Detected verses are held in `_verse_scratch` (keyed by
book+chapter+verse). If a new detection for the same passage arrives within
`VERSE_GAP_THRESHOLD_S` (default 45s) of the prior one, the range is extended
rather than emitting a duplicate event. This collapses multi-verse readings into a
single expanding range (e.g. "1 John 1:5" → "1 John 1:5–9").

**Events:**
- `verse_detected` — first occurrence of a new passage
- `verse_range_update` — subsequent detections extending the range

**Verse suggestions:** A separate async Haiku call proposes 1–3 cross-reference verses
for the congregation. Gated by `sermon_mode` (suppressed during illustration and procedural).

---

### Benchmarking

#### Methodology

`tests/benchmark/run_benchmark.py` submits a sermon MP3 to Deepgram's
pre-recorded API and computes WER against the SRT ground truth.

**Keyterms:** The benchmark injects the same default theological keyterms used in
production sessions, so WER scores are comparable to live performance.

**Caveat:** The SRT files are auto-generated subtitles and may themselves
contain errors. WER measures Deepgram's alignment with the SRT, not absolute
transcription accuracy. A WER of 10–15% against an imperfect SRT likely
represents near-human accuracy on the actual audio.

**Cached Deepgram responses:** The raw API JSON is saved to
`tests/benchmark/results/<audio_dir>/deepgram_response.json` so re-runs
only recompute WER and translation without re-transcribing.

#### Test Audio

| # | File | Duration | Source | Notes |
|---|---|---|---|---|
| 1 | `tests/audio/1/*.mp3` | ~49 min | Pr. Bullón sermon | 1 John 1 exposition + illustration |

#### Running

```bash
# From project root
python tests/benchmark/run_benchmark.py

# Force re-transcription (discards cached Deepgram response)
python tests/benchmark/run_benchmark.py --retranscribe

# Different audio set
python tests/benchmark/run_benchmark.py --audio-dir tests/audio/2
```

#### Benchmark History

| Date | Deepgram Model | WER | Avg Conf | Notes |
|---|---|---|---|---|
| — | — | — | — | Run benchmark to populate |

*(Update this table after each meaningful system change.)*

---

## Known Limitations and Open Issues

### STT
- Deepgram occasionally misrecognizes biblical proper nouns (Getsemaní,
  Apocalipsis, Efesios) despite `keyterms` boosting. Adding more terms to
  the glossary DB should help.
- Congregation noise and music can corrupt fragments. `source_quality=noisy`
  in LLM enrichment is the current mitigation.

### Translation
- Google Translate paraphrases scripture rather than quoting it verbatim.
  LLM enrichment corrects this via `translation_register=scripture`, but
  the correction arrives 1–2s after the initial display.
- "Pentecostés" at sentence start occasionally passes STT cleaning when
  the word is followed by a capitalized proper noun, making it look structural.
  Monitor `stt_noise_removed_count` in session metrics.

### Sentence Buffering
- Very long sentences (>40 words) trigger the ABSOLUTE_MAX_WORDS safety valve
  before the LLM can signal `continuation_required`. This occasionally splits
  long scripture quotes across two captions.
- Discourse holds stack to a maximum of `hold_secs` — the largest single hold
  wins, not the sum. Extremely slow preachers may still produce premature flushes.

### Architecture
- No Redis in development — broadcaster falls back to in-process queues.
  This means no multi-process scaling in dev. Production requires Redis.
- Google Translate v2 has no theological glossary support. Migrating to v3
  Advanced with a congregation-specific glossary is the highest-leverage
  translation improvement available.
- Audio resampling now happens in the browser AudioWorklet (step-accumulator,
  nearest-neighbor). No anti-aliasing filter is applied. For speech this is
  acceptable; a polyphase sinc filter could be added to the worklet if artefacts
  are ever observed.

---

## Improvement Roadmap

### High priority
- [ ] Migrate Google Translate → v3 Advanced with AutoML theological glossary
- [ ] Add more biblical proper nouns to Deepgram `keyterms` glossary
- [ ] Build reference English translations for benchmark audio (enables BLEU scoring)
- [x] Move audio resampling to browser AudioWorklet (reduces server CPU, 3× payload reduction)

### Medium priority
- [ ] Evaluate Whisper large-v3 as a pre-recorded benchmark baseline for WER comparison
- [ ] Add `diarize=true` for multi-speaker services (Q&A, panels)
- [ ] Implement verse text lookup against a local Bible DB (NIV/ESV) for exact quotes

### Low priority
- [ ] Evaluate DeepL Voice as an alternative to Deepgram + Google separate calls
- [ ] Add congregation language support beyond English (Portuguese, French)
