# Runtime Data Flow

This document describes the runtime data flow that exists in the code today.
It is implementation-focused: routes, services, event shapes, client state, and
persistence behavior.

## Why This Exists

The root `README.md` explains the product and the major pipeline ideas well, but
it intentionally compresses several important implementation details:

- STT provider selection is dynamic, not Google-only.
- Fast translation, LLM repair, and display commit are separate stages.
- Some state is persisted to SQLite, while other state only exists on the live
  event stream.
- The display and mobile clients consume the same church feed differently.

Use this doc when you want to trace how one piece of data moves through the
system end to end.

## High-Level Path

1. The browser-based soundboard client captures microphone audio.
2. Audio is downsampled in an AudioWorklet, base64 encoded, and streamed to
   `/api/stream/v1`.
3. The backend creates one `ServiceSession` per `church_id`.
4. The session forwards PCM16 audio to the selected STT provider.
5. STT interim and final results are normalized into shared callbacks.
6. Finalized Spanish text is buffered until it looks like a complete thought.
7. The session emits a fast English path first: Google Translate for Spanish,
   passthrough for English-dominant segments.
8. The LLM runs asynchronously to classify structure, improve translation,
   trigger merges, detect verses, and drive sermon-mode state.
9. The broadcaster fans events out to display clients, mobile listeners, and
   diagnostics consumers.
10. SQLite stores stable session history, capture metadata, verse data, Bible
    content, and church-specific configuration.

## 1. Browser Audio Ingest

The soundboard UI lives in
`client/components/SoundboardAdmin.tsx`.

It does four important things before audio reaches the backend:

- opens the microphone with browser audio processing mostly disabled
- creates an `AudioContext` and loads `public/worklets/recorder-worklet.js`
- downsamples audio to 16 kHz inside the worklet
- batches Float32 chunks into bounded base64 payloads before websocket send

The send protocol is:

- `session.start`
- repeated `audio`
- `session.stop`

The browser includes session setup values on `session.start`, including:

- sermon topic
- source and display Bible versions
- optional STT configuration payload
- optional benchmark capture metadata

## 2. Session Startup

`/api/stream/v1` is handled by `server/routes/stream.py`.

On `session.start`, the route calls `SessionManager.create(...)`, which creates
or replaces the active `ServiceSession` for that church. `ServiceSession.start()`
then wires together the live session dependencies:

- SQLite-backed service session record
- glossary terms for STT adaptation
- church-specific translation overrides
- `SentenceBuffer`
- `GoogleTranslateService`
- `LLMEnrichmentService`
- `SermonStateTracker`
- `TopicTracker`
- `SessionRecorder` when capture is enabled
- the selected STT session implementation

This makes `ServiceSession` the runtime orchestration hub for the pipeline.

## 3. STT Provider Selection

The current implementation does not hard-code Google STT.

`server/services/stt.py` selects the provider from the configured model name:

- models starting with `nova-` or `flux` use Deepgram
- everything else uses Google Speech

With the current defaults, the live pipeline is Deepgram-first unless you
explicitly configure a Google model.

Both STT implementations expose the same callback surface back into
`ServiceSession`:

- `on_interim(text, stt_meta)`
- `on_final(text, audio_start, audio_end, stt_meta)`
- `on_utterance_end()`

That shared callback contract is why the rest of the pipeline can stay mostly
provider-agnostic.

## 4. STT Output Normalization

Whether the backend is using Google or Deepgram, the STT layer produces the same
kinds of metadata:

- detected language and language family
- `segment_language_mode`
- confidence and low-confidence flags
- word counts
- speaker metadata when available
- sermon-relative audio timing

Interim STT results go straight to the live event stream as `interim`.

Final STT results go through additional normalization in
`ServiceSession._on_final(...)`:

- raw final text is broadcast as `stt_final`
- STT noise cleanup is applied before downstream processing
- the cleaned text may be split into smaller sentence candidates
- those candidates are handed to `SentenceBuffer`

The raw event stream preserves the original STT text for diagnostics, while the
cleaned text is what the pipeline actually reasons over.

## 5. Sentence Buffering And Flush

`server/services/sentence_buffer.py` is the first major control point after STT.

Its job is not simple sentence splitting. It tries to decide when a fragment is
safe enough to treat as a thought unit.

It flushes on:

- terminal punctuation
- utterance-end / VAD events
- max-word safety valves
- fallback timers

It delays flushing when the text still looks structurally incomplete, for
example:

- too few words
- trailing connector words
- dangling copulas
- open questions
- unresolved conditional clauses

The buffer can also be told to hold the next sentence longer. That happens from
two places:

- synchronous heuristics in `ServiceSession`, like quote introductions
- asynchronous LLM feedback, like `continuation_required`

When the buffer finally flushes, `ServiceSession._on_sentence(...)` creates the
canonical `segment_id`, broadcasts `final_spanish`, stores timing state, and
starts the fast English path.

## 6. Fast English Path

The live system intentionally shows something quickly before the full structural
analysis settles.

Routing happens in `ServiceSession`:

- English-dominant segments use passthrough
- Spanish-dominant segments use `GoogleTranslateService`

The translation service has two fast-preview behaviors:

- `translate_interim(...)` for interim STT preview text
- `translate_fragment(...)` for finalized STT fragments before sentence flush

When a full sentence is flushed, `translate(...)` does the stable Google
sentence translation with context injection.

At this stage the backend broadcasts `live_translation`, but it still does not
consider the caption stable. Instead, it queues a pending commit with a short
delay. That delay gives merge repair or better translation a chance to replace
the line before users treat it as committed.

## 7. Pending Commit, Correction, And Stable Commit

Stable display lines are emitted through `feed_commit`.

Before that happens, a segment may change in several ways:

- Google can issue a forward-context correction for the previous sentence.
- The LLM can replace the English with a better translation.
- A merge can absorb the segment into an earlier chain head.

`ServiceSession` stores these as pending commit state in
`_pending_feed_commits`.

When the delay expires, or when the LLM chooses to commit immediately,
`_commit_pending_segment(...)` does the stable work:

- broadcasts `feed_commit`
- clears the live draft line with `live_translation_clear`
- requests phrase alignment if needed
- marks the segment committed
- persists the first committed Spanish/English pair to SQLite
- flushes buffered metadata, verse detections, and suggestions

Important current behavior:

- SQLite stores the first committed version of a segment.
- Later `feed_revision` updates and `caption_merge` repairs are broadcast live,
  but this code path does not rewrite `transcript_segments`.

That means historical transcript rows can differ from the final repaired caption
that the congregation saw on screen.

## 8. LLM Enrichment

`server/services/llm_enrichment_service.py` runs after the fast sentence
translation returns.

It is asynchronous, but it preserves sentence application order before mutating
shared enrichment state. That protects merge decisions, history, and sermon-mode
state from out-of-order API completion.

The structural enrichment turn decides:

- `improved_translation`
- `discourse_tag`
- `thought_complete`
- `continuation_required`
- `display_ready`
- `merge_with_previous`
- `source_quality`
- `translation_register`
- `sermon_mode`
- optional verse detection
- optional topic refresh signal

`display_ready` is still the main emission gate. If it is false, the backend
suppresses the immediate translation update and schedules a deferred release
instead.

## 9. Deferred Release

When a caption is not safe to show yet, the LLM layer stores it in
`_deferred_updates`.

This covers cases like:

- incomplete thoughts
- quote introductions
- fragmented source
- lines that are likely to merge with the previous segment

If no merge arrives within the adaptive timeout, the deferred release path emits
the best available English anyway. This prevents captions from remaining blank
forever while still giving merge repair a chance to produce a cleaner final
unit.

## 10. Head-Anchored Merge Chains

Caption repair is chain-based, not replace-the-whole-screen based.

When the LLM decides that a segment should merge with the previous one:

- the earliest visible segment remains the head
- newer fragments are absorbed into that head
- the absorbed segment identity is discarded
- the head gets the merged Spanish and English
- lineage is preserved with `root_segment_id` and
  `merged_from_segment_ids`

The live repair event is `caption_merge`. If the head had already been committed,
the backend also emits a `feed_revision` with reason `segmentation_repair`.

This is how the UI keeps a stable screen position while still repairing earlier
caption boundaries.

## 11. Phrase Alignment

Phrase alignment is an additional LLM pass, separate from the main structural
turn.

It produces bilingual chunk pairs and tries to keep chunk identity stable across:

- plain revisions
- caption merges
- alignment recomputation

`ServiceSession._build_alignment_payload(...)` assigns:

- `chunk_id`
- English and Spanish spans
- `alignment_version`
- `previous_alignment_version`
- optional lineage data like `derived_from_chunk_ids`

The display client uses these chunk IDs and spans to make bilingual lines
interactive without losing continuity after repairs.

## 12. Verse Detection And Suggestions

Verse detection happens in the main structural enrichment turn because it shares
the same sentence context.

The detected verse is then hydrated from the imported Bible corpus with both:

- the source Bible version
- the display Bible version

Verse suggestions run as a separate async call after the structural decision has
already settled. This is deliberate: suggestions should never delay buffering,
gating, or merge repair.

The session may emit:

- `verse_detected`
- `verse_range_update`
- `verse_suggestion`

Those events are buffered until the owning segment is committed when necessary.

## 13. Sermon State And Topic Memory

Two side channels update alongside the caption flow:

### SermonStateTracker

This debounces per-sentence `sermon_mode` signals into a settled mode like:

- scripture
- exposition
- illustration
- application
- exhortation
- procedural

Mode transitions are broadcast as `mode_change` and persisted for session
history.

### TopicTracker

This accumulates flushed Spanish segments and periodically refreshes a structured
semantic memory of the sermon:

- active passage
- sermon arc
- rhetorical goal
- theme state
- short and long summaries

That memory does not directly render on the display. Instead, it feeds future
LLM prompts and diagnostics.

## 14. Broadcast Fan-Out

All live events move through `server/services/broadcaster.py`.

The broadcaster publishes per-church JSON payloads:

- Redis pub/sub in multi-client environments
- in-process callback queues when Redis is unavailable

Consumers:

- `/api/display/v1` receives the full stream
- `/api/listen/v1` filters to English translation events only
- diagnostics pages can consume the display stream for raw event tracing

This means the display and the diagnostics timeline see much more than the
mobile listener.

## 15. Client-Side State Flow

### Sanctuary Display

`client/lib/useTranslationFeed.ts` is the display-side state machine.

It listens to the display websocket and builds:

- committed `segments`
- live draft English
- rolling Spanish context
- merge mappings
- verse and suggestion attachment
- sermon mode
- browser-side feed diagnostics

It applies `caption_merge` by removing the absorbed segment, re-pointing verse
attachments to the visible head, and preserving merge lineage.

`client/components/TranslationDisplay.tsx` then renders that state in:

- full mode
- lower-third mode
- Spanish-only mode
- bilingual interactive mode

### Mobile Listener

`client/components/MobileListener.tsx` consumes `/api/listen/v1`, which already
contains a filtered event stream. It keeps a much simpler state model:

- committed English lines
- one live draft line
- connection state

It does not need the full bilingual or merge-aware display logic.

## 16. Persistence

SQLite is used for stable records and church configuration:

- glossary terms for STT adaptation
- church translation overrides
- service session rows
- transcript segments
- verse detections
- verse suggestions
- sermon mode transitions
- session capture metadata
- imported Bible text and search indexes

The capture sidecar also writes:

- WAV audio captures
- JSONL event logs
- capture metadata files

These capture artifacts are for replay, benchmarks, and diagnostics. They do not
participate in live control flow.

## 17. Diagnostics And Observability

The system emits a rich runtime trace through `pipeline_trace` events. These
cover:

- ingest stages
- sentence buffering
- Google and LLM translation steps
- phrase alignment requests and emissions
- merge decisions
- verse events
- topic-tracker prompt/response cycles
- display emissions

The diagnostics UI combines two sources:

- live websocket event logs from `/api/display/v1`
- polled stats from `/api/churches/{church_id}/stats`

There is also a separate mobile diagnostics subsystem for remote browser and
mobile reports, plus ad hoc audio payload analysis.

## 18. Event Summary

The main live event families are:

- `interim`
- `stt_final`
- `final_spanish`
- `live_translation`
- `live_translation_clear`
- `feed_commit`
- `feed_revision`
- `caption_merge`
- `segment_metadata`
- `verse_detected`
- `verse_range_update`
- `verse_suggestion`
- `mode_change`
- `pipeline_trace`

The event stream is the most complete representation of what users saw in real
time. Database rows are intentionally narrower and more stable.

## 19. Practical Reading Guide

If you want to trace one caption from mic to screen, read these files in order:

1. `client/components/SoundboardAdmin.tsx`
2. `server/routes/stream.py`
3. `server/services/session_manager.py`
4. `server/services/sentence_buffer.py`
5. `server/services/google_translate_service.py`
6. `server/services/llm_enrichment_service.py`
7. `server/services/broadcaster.py`
8. `client/lib/useTranslationFeed.ts`
9. `client/components/TranslationDisplay.tsx`

If you want the live debug view of that same path, add:

1. `client/lib/useEventLog.ts`
2. `client/components/diagnostics/PipelineTrace.tsx`
3. `server/routes/services.py` for stats

