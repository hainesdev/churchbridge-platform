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
[audio_utils] resample → PCM16 16kHz
    │
    ▼
[DeepgramSession] streaming WebSocket (nova-3, es)
    │
    ├─ interim transcript ──────────────────────────────────► display (live typing effect)
    │
    └─ is_final transcript
          │
          ├─ _clean_stt() (noise removal: Pentecostés, Santo, stutters)
          │
          ├─ GoogleTranslateService.translate_fragment()
          │   └─ interim English ──────────────────────────► display (fast preview)
          │
          └─ SentenceBuffer.add()
                │  (accumulates fragments; flushes on punctuation / UtteranceEnd / timer)
                │
                ▼
           _on_sentence (complete sentence)
                │
                ├─ GoogleTranslateService.translate() ──────► display (committed translation)
                │   └─ dual-pass correction (context window)
                │
                └─ LLMEnrichmentService.enrich() [async, fire-and-forget]
                      │
                      ├─ improved_translation ─────────────► display (replaces Google)
                      ├─ verse_detected ────────────────────► display (scripture overlay)
                      ├─ verse_suggestion ──────────────────► display (related verses)
                      ├─ caption_merge ─────────────────────► display (combines fragments)
                      └─ buffer_hold signal ────────────────► SentenceBuffer (hold next)
```

Two-pass translation is intentional:
- **Google first** (~400ms): gets something on screen quickly. Handles context
  injection and dual-pass correction. Good baseline for non-theological content.
- **Claude second** (~1–2s): enriches, fixes theological terms, detects scripture
  references, merges fragments, flags incomplete thoughts. Replaces Google
  silently if improved.

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
| `sample_rate` | `16000` | Resampled client-side from 48kHz |

**Known limitation:** We downsample from 48kHz to 16kHz in Python before sending.
Deepgram accepts 48kHz natively. This is a CPU tradeoff; revisit if server load
becomes an issue.

---

### Primary Translation: Google Cloud Translation v2

**Why Google Translate:**
- ~400ms latency — acceptable for a live display
- Context injection (send prior sentences alongside current) dramatically
  improves theological term disambiguation
- Dual-pass correction: retranslate previous sentence with new trailing context;
  silently correct if improved
- Cost-effective at scale

**Why not DeepL:**
- Tested; Google outperforms DeepL on Pentecostal sermon register
- Google handles theological names (Jesucristo, Espíritu Santo) more reliably

**Migration target:** Google Cloud Translation v3 Advanced (AutoML glossary
injection, congregation-specific example pairs). Blocked on: cost evaluation.

---

### Enrichment: Claude Haiku (claude-haiku-4-5)

**Why Claude (not GPT):**
- Haiku is extremely fast for a structured JSON task (~600–900ms)
- Claude follows JSON schema instructions more reliably than GPT-4o-mini at
  the same price tier
- Anthropic's models have better theological vocabulary recognition

**What enrichment does:**
1. Improves Google translation (theological register, "transmit" → "share")
2. Classifies discourse (rhetorical question, scripture quote, exhortation, etc.)
3. Detects `thought_complete` / `continuation_required` — feeds back to buffer
4. Detects Bible verses (explicit citations and quoted text)
5. Suggests related verses for the congregation
6. Merges sentence fragments into coherent captions
7. Signals `display_ready=false` to hold fragmented sentences until merge arrives

**Tradeoff:** Fire-and-forget — enrichment never blocks Google translation.
If Claude is slow or fails, Google translation still appears. The worst case
is the Google translation staying on screen (which is acceptable).

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

---

### STT Noise Cleaning

Applied in `_clean_stt()` before segmentation, translation, and buffering.
The raw STT text is still broadcast for operator visibility.

| Pattern | Action | Reason |
|---|---|---|
| `AAA`, `Uh`, `Mmm`, `Este...` | Remove | Multi-char filler sounds |
| `que que`, `el el` | Collapse | Stutter repetition |
| `A a Cristo` | Collapse to last | Same-char stutter |
| `Santo tú/él/ella...` | Remove | Interjection before pronoun |
| `^Santo` (sentence-initial) | Remove | Bare exclamation noise |
| `Pentecostés` (people context) | → `Pentecostales` | Feast name vs people |
| `Pentecostés` (no anchor) | Remove | Pure STT noise prefix |

---

## Benchmarking

### Methodology

`tests/benchmark/run_benchmark.py` submits a sermon MP3 to Deepgram's
pre-recorded API and computes WER against the SRT ground truth.

**Caveat:** The SRT files are auto-generated subtitles and may themselves
contain errors. WER measures Deepgram's alignment with the SRT, not absolute
transcription accuracy. A WER of 10–15% against an imperfect SRT likely
represents near-human accuracy on the actual audio.

**Cached Deepgram responses:** The raw API JSON is saved to
`tests/benchmark/results/<audio_dir>/deepgram_response.json` so re-runs
only recompute WER and translation without re-transcribing.

### Test Audio

| # | File | Duration | Source | Notes |
|---|---|---|---|---|
| 1 | `tests/audio/1/*.mp3` | ~49 min | Pr. Bullón sermon | 1 John 1 exposition + illustration |

### Running

```bash
# From project root
python tests/benchmark/run_benchmark.py

# Force re-transcription (discards cached Deepgram response)
python tests/benchmark/run_benchmark.py --retranscribe

# Different audio set
python tests/benchmark/run_benchmark.py --audio-dir tests/audio/2
```

### Benchmark History

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
- Discourse holds stack to a maximum of `hold_secs=4.5s`. Extremely slow
  preachers (or long pauses) may still produce premature flushes.

### Architecture
- No Redis in development — broadcaster falls back to in-process queues.
  This means no multi-process scaling in dev. Production requires Redis.
- Google Translate v2 has no theological glossary support. Migrating to v3
  Advanced with a congregation-specific glossary is the highest-leverage
  translation improvement available.

---

## Improvement Roadmap

### High priority
- [ ] Migrate Google Translate → v3 Advanced with AutoML theological glossary
- [ ] Add more biblical proper nouns to Deepgram `keyterms` glossary
- [ ] Build reference English translations for benchmark audio (enables BLEU scoring)

### Medium priority
- [ ] Evaluate Whisper large-v3 as a pre-recorded benchmark baseline for WER comparison
- [ ] Add `diarize=true` for multi-speaker services (Q&A, panels)
- [ ] Implement verse text lookup against a local Bible DB (NIV/ESV) for exact quotes

### Low priority
- [ ] Migrate Deepgram to 48kHz native (remove client-side resampling)
- [ ] Evaluate DeepL Voice as an alternative to Deepgram + Google separate calls
- [ ] Add congregation language support beyond English (Portuguese, French)
