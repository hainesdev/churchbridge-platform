"""
Regression tests for pipeline concurrency and reconnect edge cases.

These tests target bugs that are easy to miss in sequential unit tests:
- committed sentence translations being cancelled by later sentences
- enrichment state being applied in completion order instead of sentence order
- deferred-release captions never clearing pending state
- Deepgram reconnect timestamp drift after multiple reconnects
"""
import asyncio
import os
import sys
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from server.services.google_translate_service import GoogleTranslateService
from server.services.deepgram_session import DeepgramSession
from server.services.llm_enrichment_service import LLMEnrichmentService


class SlowFakeGoogleTranslateService(GoogleTranslateService):
    def __init__(self, responses: list[str], delay_s: float, **kwargs):
        self._api_key = "FAKE_KEY"
        self._context = deque(maxlen=2)
        self._active_task = None
        self._fragment_task = None
        self._sentence_tasks = []
        self._sentence_lock = asyncio.Lock()
        self._fragment_context = []
        self._http = None
        self._responses = list(responses)
        self._call_count = 0
        self._delay_s = delay_s
        self._on_translation = kwargs["on_translation"]
        self._on_correction = kwargs["on_correction"]
        self._on_interim_translation = kwargs.get("on_interim_translation", lambda *a: None)

    async def _call_api(self, html_body: str) -> str:
        await asyncio.sleep(self._delay_s)
        result = self._responses[self._call_count % len(self._responses)]
        self._call_count += 1
        return result

    async def close(self):
        pass


class _NoopHttpClient:
    async def aclose(self):
        return None


class ControlledGoogleTranslateService(GoogleTranslateService):
    def __init__(self, responses: list[str], delay_s: float, **kwargs):
        self._api_key = "FAKE_KEY"
        self._context = deque(maxlen=2)
        self._active_task = None
        self._fragment_task = None
        self._sentence_tasks = []
        self._sentence_lock = asyncio.Lock()
        self._fragment_context = []
        self._http = _NoopHttpClient()
        self._responses = list(responses)
        self._call_count = 0
        self._delay_s = delay_s
        self._on_translation = kwargs["on_translation"]
        self._on_correction = kwargs["on_correction"]
        self._on_interim_translation = kwargs.get("on_interim_translation", lambda *a: None)

    async def _call_api(self, html_body: str) -> str:
        await asyncio.sleep(self._delay_s)
        result = self._responses[self._call_count % len(self._responses)]
        self._call_count += 1
        return result


class StubTopicTracker:
    def get_context(self) -> str:
        return ""


class StubStateTracker:
    settled_mode = "exposition"

    def get_context_label(self) -> str:
        return "pastor explaining a biblical text"

    async def add_signal(self, mode: str, ts: int) -> None:
        return None

    def is_narrative(self) -> bool:
        return False


class FakeAnthropicMessages:
    def __init__(self, responses_by_ts: dict[int, tuple[float, str]]):
        self._responses_by_ts = responses_by_ts

    async def create(self, *, messages, **kwargs):
        prompt = messages[0]["content"]
        marker = "[SOURCE — Spanish original]\n"
        spanish = prompt.split(marker, 1)[1].split("\n\n[GOOGLE TRANSLATION", 1)[0]
        delay_s, raw = self._responses_by_ts[spanish]
        await asyncio.sleep(delay_s)
        return type("Resp", (), {"content": [type("Block", (), {"text": raw})()]})()


class FakeAnthropicClient:
    def __init__(self, responses_by_ts):
        self.messages = FakeAnthropicMessages(responses_by_ts)


class SequentialFakeAnthropicMessages:
    def __init__(self, responses: list[tuple[float, str]]):
        self._responses = list(responses)

    async def create(self, **kwargs):
        delay_s, raw = self._responses.pop(0)
        await asyncio.sleep(delay_s)
        return type("Resp", (), {"content": [type("Block", (), {"text": raw})()]})()


class SequentialFakeAnthropicClient:
    def __init__(self, responses: list[tuple[float, str]]):
        self.messages = SequentialFakeAnthropicMessages(responses)


def make_json_result(
    improved_translation: str,
    *,
    merge_with_previous: bool = False,
    display_ready: bool = True,
    thought_complete: bool = True,
    continuation_required: bool = False,
    discourse_tag: str = "statement",
):
    return (
        "{"
        f"\"improved_translation\": {improved_translation!r}, "
        f"\"discourse_tag\": {discourse_tag!r}, "
        "\"introduces_quote\": false, "
        f"\"thought_complete\": {str(thought_complete).lower()}, "
        f"\"continuation_required\": {str(continuation_required).lower()}, "
        f"\"merge_with_previous\": {str(merge_with_previous).lower()}, "
        "\"paragraph_break\": false, "
        "\"source_quality\": \"clean\", "
        "\"translation_register\": \"expository\", "
        "\"sermon_mode\": \"exposition\", "
        f"\"display_ready\": {str(display_ready).lower()}, "
        "\"verse_detected\": null"
        "}"
    ).replace("'", '"')


def run(coro):
    return asyncio.run(coro)


class TestGoogleSentenceConcurrency:
    def test_later_sentence_does_not_cancel_earlier_committed_translation(self):
        async def run_():
            translations = []

            async def on_translation(spanish, english, ts):
                translations.append((ts, spanish, english))

            svc = SlowFakeGoogleTranslateService(
                responses=["<p>First</p>", "<p>Second</p>"],
                delay_s=0.05,
                on_translation=on_translation,
                on_correction=lambda *args: None,
            )

            await svc.translate("uno", ts=1000)
            await asyncio.sleep(0.01)
            await svc.translate("dos", ts=2000)
            await asyncio.sleep(0.15)

            assert translations == [
                (1000, "uno", "First"),
                (2000, "dos", "Second"),
            ]

        run(run_())

    def test_close_waits_for_in_flight_sentence_translation(self):
        async def run_():
            translations = []

            async def on_translation(spanish, english, ts):
                translations.append((ts, spanish, english))

            svc = ControlledGoogleTranslateService(
                responses=["<p>First</p>"],
                delay_s=0.05,
                on_translation=on_translation,
                on_correction=lambda *args: None,
            )

            await svc.translate("uno", ts=1000)
            await svc.close()

            assert translations == [
                (1000, "uno", "First"),
            ]

        run(run_())


class TestDeepgramReconnectTimestamps:
    def test_multiple_reconnects_keep_monotonic_non_duplicated_audio_offsets(self):
        async def run_():
            finals = []

            async def on_final(text, audio_start, audio_end):
                finals.append((text, audio_start, audio_end))

            session = DeepgramSession(
                church_id="test",
                on_interim=lambda text: asyncio.sleep(0),
                on_final=on_final,
            )

            await session._handle_transcript({
                "channel": {"alternatives": [{"transcript": "uno"}]},
                "start": 0.0,
                "duration": 5.0,
                "is_final": True,
            })
            session._stream_offset += session._last_stream_audio_end
            session._last_stream_audio_end = 0.0

            await session._handle_transcript({
                "channel": {"alternatives": [{"transcript": "dos"}]},
                "start": 0.0,
                "duration": 5.0,
                "is_final": True,
            })
            session._stream_offset += session._last_stream_audio_end
            session._last_stream_audio_end = 0.0

            await session._handle_transcript({
                "channel": {"alternatives": [{"transcript": "tres"}]},
                "start": 0.5,
                "duration": 1.0,
                "is_final": True,
            })

            assert finals == [
                ("uno", 0.0, 5.0),
                ("dos", 5.0, 10.0),
                ("tres", 10.5, 11.5),
            ]

        run(run_())


class TestEnrichmentOrdering:
    def test_merge_targets_prior_sentence_order_not_completion_order(self):
        async def run_():
            merges = []
            metadata = []
            settled = []

            async def on_caption_merge(absorb_ts, keep_ts, merged_spanish, merged_english):
                merges.append((absorb_ts, keep_ts, merged_spanish, merged_english))

            async def on_segment_metadata(ts, payload):
                metadata.append((ts, payload))

            async def on_enrichment_settled(ts):
                settled.append(ts)

            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=lambda *args: asyncio.sleep(0),
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=on_enrichment_settled,
                on_caption_merge=on_caption_merge,
                on_segment_metadata=on_segment_metadata,
                state_tracker=StubStateTracker(),
            )
            service._client = FakeAnthropicClient({
                "primero": (0.10, make_json_result("First")),
                "segundo": (0.20, make_json_result("Second")),
                "tercero": (0.01, make_json_result("Second third merged", merge_with_previous=True)),
            })

            t1 = service.enrich("primero", "First", 1000)
            t2 = service.enrich("segundo", "Second", 2000)
            t3 = service.enrich("tercero", "Third", 3000)
            await asyncio.gather(t1, t2, t3)

            assert merges == [
                (3000, 2000, "segundo tercero", "Second third merged"),
            ]
            assert settled == [1000, 2000, 3000]
            assert [ts for ts, _ in metadata] == [1000, 2000, 3000]

        run(run_())


class TestDeferredRelease:
    def test_deferred_release_clears_pending_state_even_without_text_change(self):
        async def run_():
            updates = []
            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=lambda ts, english: updates.append((ts, english)) or asyncio.sleep(0),
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=lambda *args: asyncio.sleep(0),
                state_tracker=StubStateTracker(),
            )

            task = asyncio.create_task(
                service._deferred_translation_release(123, "Same text", "Same text")
            )
            service._deferred_updates[123] = ("Same text", task)
            await asyncio.wait_for(task, timeout=7.5)

            assert updates == [(123, "Same text")]

        run(run_())

    def test_deferred_release_marks_incomplete_tail_with_ellipsis(self):
        async def run_():
            updates = []
            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=lambda ts, english: updates.append((ts, english)) or asyncio.sleep(0),
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=lambda *args: asyncio.sleep(0),
                state_tracker=StubStateTracker(),
            )

            task = asyncio.create_task(
                service._deferred_translation_release(
                    456,
                    "Now, let's understand this phrase by phrase, word by word, what",
                    "Now, let's understand, phrase by phrase, word by word, what",
                )
            )
            service._deferred_updates[456] = ("placeholder", task)
            await asyncio.wait_for(task, timeout=7.5)

            assert updates == [
                (456, "Now, let's understand, phrase by phrase, word by word, what...")
            ]

        run(run_())


class TestTerminalIncompleteEnrichment:
    def test_terminal_incomplete_segment_emits_single_metadata_and_ellipsized_release(self):
        async def run_():
            updates = []
            metadata = []
            settled = []

            async def on_translation_update(ts, english):
                updates.append((ts, english))

            async def on_segment_metadata(ts, payload):
                metadata.append((ts, payload))

            async def on_enrichment_settled(ts):
                settled.append(ts)

            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=on_translation_update,
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=on_enrichment_settled,
                on_segment_metadata=on_segment_metadata,
                state_tracker=StubStateTracker(),
            )
            service._should_generate_verse_suggestions = lambda *args: False
            service._client = FakeAnthropicClient({
                "Ahora, vamos a entender frase por frase palabra por palabra, lo que": (
                    0.01,
                    make_json_result(
                        "Now, let us understand this phrase by phrase, word by word, what",
                        display_ready=False,
                        thought_complete=False,
                        continuation_required=True,
                        discourse_tag="transition",
                    ),
                ),
            })

            await service.enrich(
                "Ahora, vamos a entender frase por frase palabra por palabra, lo que",
                "Now, let's understand, phrase by phrase, word by word, what...",
                1000,
                terminal_incomplete=True,
            )

            assert metadata == [
                (
                    1000,
                    {
                        "translation_register": "expository",
                        "paragraph_break": False,
                        "source_quality": "clean",
                        "pending_completion": True,
                        "terminal_incomplete": True,
                    },
                )
            ]
            assert settled == [1000]
            task = service._deferred_updates[1000][1]
            await asyncio.wait_for(task, timeout=7.5)
            assert updates == [
                (1000, "Now, let's understand, phrase by phrase, word by word, what...")
            ]

        run(run_())


class TestCorrectionSuppressedEvent:
    def test_suppressed_correction_broadcasts_correction_suppressed_event(self):
        """_on_correction must emit correction_suppressed when enrichment has already
        settled for the same ts, so scorecard.stale_correction_suppression_count is
        non-zero and the metric is not silently stuck at 0."""
        async def run_():
            events = []

            class _StubBroadcaster:
                async def publish(self, church_id, event):
                    events.append(event)

            from server.services.session_manager import ServiceSession

            session = ServiceSession.__new__(ServiceSession)
            session._church_id = "test"
            session._broadcaster = _StubBroadcaster()
            session._enrichment_settled = {1000}

            # ts=1000 is settled → should emit correction_suppressed
            await session._on_correction(1000, "Late Google correction")
            # ts=2000 is not settled → should emit normal correction
            await session._on_correction(2000, "Normal correction")

            assert events == [
                {"type": "correction_suppressed", "ts": 1000},
                {"type": "correction", "ts": 2000, "english": "Normal correction"},
            ]

        run(run_())


class TestEnrichmentCloseDrain:
    def test_close_waits_for_in_flight_enrichment_task(self):
        async def run_():
            updates = []
            settled = []

            async def on_translation_update(ts, english):
                updates.append((ts, english))

            async def on_enrichment_settled(ts):
                settled.append(ts)

            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=on_translation_update,
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=on_enrichment_settled,
                state_tracker=StubStateTracker(),
            )
            service._client = FakeAnthropicClient({
                "primero": (0.05, make_json_result("Better First")),
            })
            service._should_generate_verse_suggestions = lambda *args: False

            service.enrich("primero", "First", 1000)
            await service.close()

            assert updates == [(1000, "Better First")]
            assert settled == [1000]

        run(run_())


class TestSessionCloseIncompleteMetadata:
    def test_session_close_incomplete_flush_is_marked_terminal_incomplete(self):
        async def run_():
            events = []
            translate_calls = []

            class _StubBroadcaster:
                async def publish(self, church_id, event):
                    events.append(event)

            class _StubTranslation:
                async def translate(self, text, ts):
                    translate_calls.append((text, ts))

            from server.services.session_manager import ServiceSession

            session = ServiceSession.__new__(ServiceSession)
            session._church_id = "test"
            session._broadcaster = _StubBroadcaster()
            session._topic_tracker = None
            session._state_tracker = None
            session._sentence_buffer = None
            session._translation = _StubTranslation()
            session._enrichment = None
            session._pending_audio_timing = {}
            session._enrichment_settled = set()
            session._db_session_id = None

            await session._on_sentence(
                "Ahora, vamos a entender frase por frase palabra por palabra, lo que",
                10.0,
                12.0,
                "session_close",
            )

            assert events[0]["type"] == "final_spanish"
            assert events[0]["terminal_incomplete"] is True
            assert events[0]["flush_reason"] == "session_close"
            assert translate_calls == [
                ("Ahora, vamos a entender frase por frase palabra por palabra, lo que", events[0]["ts"])
            ]
            assert session._pending_audio_timing[events[0]["ts"]]["terminal_incomplete"] is True
            assert len(events) == 1

        run(run_())

    def test_terminal_incomplete_translation_event_is_ellipsized_immediately(self):
        async def run_():
            events = []

            class _StubBroadcaster:
                async def publish(self, church_id, event):
                    events.append(event)

            from server.services.session_manager import ServiceSession

            session = ServiceSession.__new__(ServiceSession)
            session._church_id = "test"
            session._broadcaster = _StubBroadcaster()
            session._db_session_id = None
            session._enrichment = None
            session._pending_audio_timing = {
                1000: {
                    "audio_start": 0.0,
                    "audio_end": 1.0,
                    "terminal_incomplete": True,
                    "flush_reason": "session_close",
                }
            }

            await session._on_translation(
                "Ahora, vamos a entender frase por frase palabra por palabra, lo que",
                "Now, let's understand, phrase by phrase, word by word, what",
                1000,
            )

            assert events == [
                {
                    "type": "translation",
                    "spanish": "Ahora, vamos a entender frase por frase palabra por palabra, lo que",
                    "english": "Now, let's understand, phrase by phrase, word by word, what...",
                    "ts": 1000,
                }
            ]

        run(run_())


class TestTranslationRepairFallback:
    def test_merge_candidate_is_checked_against_full_merged_unit(self):
        async def run_():
            updates = []
            merges = []

            async def on_translation_update(ts, english):
                updates.append((ts, english))

            async def on_caption_merge(absorb_ts, keep_ts, merged_spanish, merged_english):
                merges.append((absorb_ts, keep_ts, merged_spanish, merged_english))

            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=on_translation_update,
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=lambda *args: asyncio.sleep(0),
                on_caption_merge=on_caption_merge,
                state_tracker=StubStateTracker(),
            )
            service._should_generate_verse_suggestions = lambda *args: False
            service._client = SequentialFakeAnthropicClient([
                (
                    0.01,
                    make_json_result("What is the proof that we are in the light?", discourse_tag="rhetorical_question"),
                ),
                (
                    0.01,
                    make_json_result(
                        "We have fellowship with one another.",
                        merge_with_previous=True,
                        discourse_tag="answer_to_question",
                    ),
                ),
                (
                    0.01,
                    (
                        "{"
                        "\"literal_translation\": \"What is the proof that we are in the light? "
                        "We have fellowship with one another.\", "
                        "\"natural_translation\": \"What is the proof that we are in the light? "
                        "We have fellowship with one another.\""
                        "}"
                    ),
                ),
            ])

            await service.enrich(
                "¿Cuál es la prueba de que estamos en la luz?",
                "What is the proof that we are in the light?",
                1000,
            )
            await service.enrich(
                "Tenemos comunión unos con otros.",
                "We have fellowship with one another.",
                2000,
            )

            assert merges == [
                (
                    2000,
                    1000,
                    "¿Cuál es la prueba de que estamos en la luz? Tenemos comunión unos con otros.",
                    "What is the proof that we are in the light? We have fellowship with one another.",
                )
            ]
            assert updates[-1] == (
                2000,
                "What is the proof that we are in the light? We have fellowship with one another.",
            )

        run(run_())

    def test_incomplete_primary_candidate_uses_repair_choice(self):
        async def run_():
            updates = []
            settled = []

            async def on_translation_update(ts, english):
                updates.append((ts, english))

            async def on_enrichment_settled(ts):
                settled.append(ts)

            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=on_translation_update,
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=on_enrichment_settled,
                state_tracker=StubStateTracker(),
            )
            service._should_generate_verse_suggestions = lambda *args: False
            service._client = SequentialFakeAnthropicClient([
                (
                    0.01,
                    make_json_result("We have fellowship with one another."),
                ),
                (
                    0.01,
                    (
                        "{"
                        "\"literal_translation\": \"If we walk in the light as he is in the light, "
                        "we have fellowship with one another.\", "
                        "\"natural_translation\": \"If we walk in the light, as he himself is in the light, "
                        "we have fellowship with one another.\""
                        "}"
                    ),
                ),
            ])

            await service.enrich(
                "Si andamos en luz, como él está en luz, tenemos comunión unos con otros.",
                "If we walk in the light, as he is in the light, we have fellowship with one another.",
                1000,
            )

            assert updates == [
                (1000, "If we walk in the light, as he himself is in the light, we have fellowship with one another.")
            ]
            assert settled == [1000]

        run(run_())

    def test_unrepaired_incomplete_candidate_falls_back_to_google(self):
        async def run_():
            updates = []
            settled = []

            async def on_translation_update(ts, english):
                updates.append((ts, english))

            async def on_enrichment_settled(ts):
                settled.append(ts)

            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=on_translation_update,
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=on_enrichment_settled,
                state_tracker=StubStateTracker(),
            )
            service._should_generate_verse_suggestions = lambda *args: False
            service._client = SequentialFakeAnthropicClient([
                (
                    0.01,
                    make_json_result("We have fellowship with one another."),
                ),
                (
                    0.01,
                    (
                        "{"
                        "\"literal_translation\": \"We have fellowship with one another.\", "
                        "\"natural_translation\": \"We have fellowship together.\""
                        "}"
                    ),
                ),
            ])

            await service.enrich(
                "Si andamos en luz, como él está en luz, tenemos comunión unos con otros.",
                "If we walk in the light, as he is in the light, we have fellowship with one another.",
                1000,
            )

            assert updates == []
            assert settled == [1000]

        run(run_())
