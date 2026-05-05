"""
Regression tests for pipeline concurrency and reconnect edge cases.

These tests target bugs that are easy to miss in sequential unit tests:
- committed sentence translations being cancelled by later sentences
- enrichment state being applied in completion order instead of sentence order
- deferred-release captions never clearing pending state
"""
import asyncio
import os
import sys
from collections import deque
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from server.services.google_translate_service import GoogleTranslateService
from server.services.google_speech_session import (
    GoogleSpeechSession,
    _build_adaptation,
    _build_speaker_segments,
    _build_recognition_config,
    _segment_language_mode,
    _speaker_metadata,
    _supports_diarization_fallback,
)
from server.services.sentence_buffer import SentenceBuffer
from server.services.llm_enrichment_service import (
    LLMEnrichmentService,
    _build_alignment_request_message,
    _build_user_message,
    _merge_blocked_by_segment_structure,
    _normalize_segment_structure,
)
from server.services.stt import STTConfig


class SlowFakeGoogleTranslateService(GoogleTranslateService):
    def __init__(self, responses: list[str], delay_s: float, **kwargs):
        self._api_key = "FAKE_KEY"
        self._context = deque(maxlen=2)
        self._active_task = None
        self._fragment_task = None
        self._sentence_tasks = []
        self._sentence_lock = asyncio.Lock()
        self._fragment_context = []
        self._last_preview_spanish = ""
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
        self._last_preview_spanish = ""
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
        if isinstance(prompt, list):
            prompt = "\n\n".join(block["text"] for block in prompt)
        marker = "[SOURCE — Spanish original]\n"
        spanish = prompt.split(marker, 1)[1].split("\n\n[GOOGLE TRANSLATION", 1)[0]
        delay_s, raw = self._responses_by_ts[spanish]
        await asyncio.sleep(delay_s)
        usage = type(
            "Usage",
            (),
            {
                "input_tokens": 100,
                "output_tokens": 40,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        )()
        return type("Resp", (), {"content": [type("Block", (), {"text": raw})()], "usage": usage})()


class FakeAnthropicClient:
    def __init__(self, responses_by_ts):
        self.messages = FakeAnthropicMessages(responses_by_ts)


class SequentialFakeAnthropicMessages:
    def __init__(self, responses: list[tuple[float, str]]):
        self._responses = list(responses)

    async def create(self, **kwargs):
        delay_s, raw = self._responses.pop(0)
        await asyncio.sleep(delay_s)
        usage = type(
            "Usage",
            (),
            {
                "input_tokens": 100,
                "output_tokens": 40,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        )()
        return type("Resp", (), {"content": [type("Block", (), {"text": raw})()], "usage": usage})()


class SequentialFakeAnthropicClient:
    def __init__(self, responses: list[tuple[float, str]]):
        self.messages = SequentialFakeAnthropicMessages(responses)


class CaptureAnthropicMessages:
    def __init__(self, raw: str):
        self.raw = raw
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        usage = type(
            "Usage",
            (),
            {
                "input_tokens": 123,
                "output_tokens": 45,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        )()
        return type(
            "Resp",
            (),
            {"content": [type("Block", (), {"text": self.raw})()], "usage": usage},
        )()


class CaptureAnthropicClient:
    def __init__(self, raw: str):
        self.messages = CaptureAnthropicMessages(raw)


def make_json_result(
    improved_translation: str,
    *,
    merge_with_previous: bool = False,
    display_ready: bool = True,
    thought_complete: bool = True,
    continuation_required: bool = False,
    discourse_tag: str = "statement",
    translation_register: str = "expository",
    sermon_mode: str = "exposition",
    source_quality: str = "clean",
    phrase_alignment: list[tuple[str, str]] | None = None,
):
    alignment_json = (
        "[" +
        ", ".join(
            "{"
            f"\"english_text\": {english!r}, "
            f"\"spanish_text\": {spanish!r}"
            "}"
            for english, spanish in (phrase_alignment or [])
        ) +
        "]"
    )
    return (
        "{"
        f"\"improved_translation\": {improved_translation!r}, "
        f"\"discourse_tag\": {discourse_tag!r}, "
        "\"introduces_quote\": false, "
        f"\"thought_complete\": {str(thought_complete).lower()}, "
        f"\"continuation_required\": {str(continuation_required).lower()}, "
        f"\"merge_with_previous\": {str(merge_with_previous).lower()}, "
        "\"paragraph_break\": false, "
        f"\"source_quality\": {source_quality!r}, "
        f"\"translation_register\": {translation_register!r}, "
        f"\"sermon_mode\": {sermon_mode!r}, "
        f"\"display_ready\": {str(display_ready).lower()}, "
        f"\"phrase_alignment\": {alignment_json}, "
        "\"verse_detected\": null"
        "}"
    ).replace("'", '"')


def make_translation_only_result(improved_translation: str):
    return (
        "{"
        f"\"improved_translation\": {improved_translation!r}"
        "}"
    ).replace("'", '"')


def run(coro):
    return asyncio.run(coro)


async def _wait_for(predicate, interval_s: float = 0.01):
    while not predicate():
        await asyncio.sleep(interval_s)


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


class TestEnrichmentOrdering:
    def test_create_json_response_enables_prompt_cache(self):
        async def run_():
            service = LLMEnrichmentService(
                church_id="church",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=lambda *args: None,
                on_verse_detected=lambda *args: None,
                on_verse_range_update=lambda *args: None,
                on_verse_suggestion=lambda *args: None,
                on_enrichment_settled=lambda *args: None,
                session_id=1,
                on_caption_merge=lambda *args: None,
                on_segment_metadata=lambda *args: None,
                state_tracker=StubStateTracker(),
            )
            service._client = CaptureAnthropicClient(make_json_result("Test translation"))

            result = await service._create_json_response(
                system="system",
                user_message="user",
                ts=1,
                stage="enrichment",
            )

            assert result is not None
            call = service._client.messages.calls[0]
            assert call["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}

        run(run_())

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
                (3000, 2000, "segundo tercero", "Second Third"),
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
                on_translation_update=lambda ts, english, phrase_alignment=None: updates.append((ts, english, phrase_alignment)) or asyncio.sleep(0),
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

            assert updates == [(123, "Same text", None)]

        run(run_())

    def test_deferred_release_marks_incomplete_tail_with_ellipsis(self):
        async def run_():
            updates = []
            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=lambda ts, english, phrase_alignment=None: updates.append((ts, english, phrase_alignment)) or asyncio.sleep(0),
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
                (456, "Now, let's understand, phrase by phrase, word by word, what...", None)
            ]

        run(run_())


class TestTerminalIncompleteEnrichment:
    def test_terminal_incomplete_segment_emits_single_metadata_and_ellipsized_release(self):
        async def run_():
            updates = []
            metadata = []
            settled = []

            async def on_translation_update(ts, english, phrase_alignment=None):
                updates.append((ts, english, phrase_alignment))

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
                        "chain_state": "deferred_pending",
                        "chain_head_ts": 1000,
                        "chain_length": 1,
                        "pending_reason": "terminal_incomplete",
                        "released_from_fallback": False,
                    },
                )
            ]
            assert settled == [1000]
            task = service._deferred_updates[1000][1]
            await asyncio.wait_for(task, timeout=7.5)
            assert updates == [
                (1000, "Now, let's understand, phrase by phrase, word by word, what...", None)
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
            session._pending_feed_commits = {}
            session._segment_text_cache = {}
            session._last_segment_id = 0

            # ts=1000 is settled → should emit correction_suppressed
            await session._on_correction(1000, "Late Google correction")
            # ts=2000 is not settled → should emit normal correction
            await session._on_correction(2000, "Normal correction")

            assert events == [
                {"type": "correction_suppressed", "segment_id": 1000, "ts": 1000},
                {
                    "type": "feed_revision",
                    "segment_id": 2000,
                    "ts": 2000,
                    "english": "Normal correction",
                    "source": "google",
                    "reason": "forward_context_correction",
                },
            ]

        run(run_())


class TestEnrichmentCloseDrain:
    def test_close_waits_for_in_flight_enrichment_task(self):
        async def run_():
            updates = []
            settled = []

            async def on_translation_update(ts, english, phrase_alignment=None):
                updates.append((ts, english, phrase_alignment))

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

            assert updates == [(1000, "Better First", None)]
            assert settled == [1000]

        run(run_())


class TestSessionCloseIncompleteMetadata:
    def test_close_stops_stt_before_flushing_buffered_tail(self):
        async def run_():
            events = []
            sentence_translate_calls = []
            fragment_translate_calls = []

            class _StubBroadcaster:
                async def publish(self, church_id, event):
                    events.append(event)

            class _StubTranslation:
                def __init__(self):
                    self.closed = False

                async def translate_fragment(self, text):
                    fragment_translate_calls.append((text, self.closed))

                async def translate(self, text, ts):
                    sentence_translate_calls.append((text, self.closed))

                async def close(self):
                    self.closed = True

            class _StubSttSession:
                async def stop(self):
                    await session._on_final(
                        "Este es el mensaje que hemos",
                        1.0,
                        2.0,
                        {
                            "detected_language": "es-US",
                            "detected_languages": ["es-US"],
                            "avg_confidence": 0.0,
                            "word_count": 6,
                            "low_confidence": False,
                            "speaker_tags": [],
                        },
                    )

            from server.services.session_manager import ServiceSession

            session = ServiceSession.__new__(ServiceSession)
            session._church_id = "test"
            session._broadcaster = _StubBroadcaster()
            session._topic_tracker = None
            session._state_tracker = None
            session._translation = _StubTranslation()
            session._enrichment = None
            session._stt_session = _StubSttSession()
            session._sentence_buffer = SentenceBuffer(on_sentence=session._on_sentence)
            session._pending_audio_timing = {}
            session._enrichment_settled = set()
            session._pending_feed_commits = {}
            session._committed_segment_ids = set()
            session._persisted_segment_ids = set()
            session._segment_text_cache = {}
            session._segment_stt_cache = {}
            session._segment_metadata_cache = {}
            session._pending_segment_metadata = {}
            session._pending_detected_verses = {}
            session._pending_suggested_verses = {}
            session._db_session_id = None
            session._recorder = None
            session._last_segment_id = 0
            session._stt_noise_removed_count = 0
            session._stt_config = STTConfig()

            await session.close()
            await asyncio.sleep(0.05)

            assert fragment_translate_calls == [("es el mensaje que hemos", False)]
            assert sentence_translate_calls == [("es el mensaje que hemos", False)]
            assert any(event["type"] == "final_spanish" for event in events)

        run(run_())

    def test_merge_segment_metadata_includes_chain_debug_fields(self):
        async def run_():
            metadata = []

            async def on_segment_metadata(ts, payload):
                metadata.append((ts, payload))

            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=lambda *args: asyncio.sleep(0),
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=lambda *args: asyncio.sleep(0),
                on_caption_merge=lambda *args: asyncio.sleep(0),
                on_segment_metadata=on_segment_metadata,
                state_tracker=StubStateTracker(),
            )
            service._should_generate_verse_suggestions = lambda *args: False
            service._client = FakeAnthropicClient({
                "primero": (0.01, make_json_result("First")),
                "segundo": (0.01, make_json_result("Merged second", merge_with_previous=True)),
            })

            await service.enrich("primero", "First", 1000)
            await service.enrich("segundo", "Second", 2000)

            assert metadata[1] == (
                2000,
                {
                    "translation_register": "expository",
                    "paragraph_break": False,
                    "source_quality": "clean",
                    "pending_completion": False,
                    "terminal_incomplete": False,
                    "chain_state": "merge_chain",
                    "chain_head_ts": 1000,
                    "chain_length": 2,
                    "pending_reason": "merge_with_previous",
                    "released_from_fallback": False,
                },
            )
            assert service.metrics["merge_chain_opened"] == 1
            assert service.metrics["deferred_release_cancelled_for_merge"] == 0

        run(run_())

    def test_hidden_merge_prefers_google_chain_text_and_defers_head_alignment_until_close(self):
        async def run_():
            merges = []
            alignment_requests = []

            async def on_caption_merge(absorb_ts, keep_ts, merged_spanish, merged_english):
                merges.append((absorb_ts, keep_ts, merged_spanish, merged_english))

            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=lambda *args: asyncio.sleep(0),
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=lambda *args: asyncio.sleep(0),
                on_caption_merge=on_caption_merge,
                state_tracker=StubStateTracker(),
            )
            service._should_generate_verse_suggestions = lambda *args: False
            service._schedule_phrase_alignment = lambda **kwargs: alignment_requests.append(kwargs)
            service._client = FakeAnthropicClient({
                "primero": (0.01, make_json_result("First")),
                "segundo": (
                    0.01,
                    make_json_result(
                        "LLM merge rewrite that should be ignored",
                        merge_with_previous=True,
                        discourse_tag="answer_to_question",
                    ),
                ),
                "tercero": (0.01, make_json_result("Third")),
            })

            await service.enrich("primero", "First", 1000)
            alignment_requests.clear()

            await service.enrich("segundo", "Second", 2000)

            assert merges == [(2000, 1000, "primero segundo", "First Second")]
            assert alignment_requests == []

            await service.enrich("tercero", "Third", 3000)

            assert any(
                request["ts"] == 1000
                and request["spanish"] == "primero segundo"
                and request["english"] == "First Second"
                for request in alignment_requests
            )

        run(run_())

    def test_translation_refinement_is_skipped_for_simple_clean_sentence(self):
        async def run_():
            calls = []

            class _Messages:
                async def create(self, **kwargs):
                    calls.append(kwargs)
                    usage = type(
                        "Usage",
                        (),
                        {
                            "input_tokens": 100,
                            "output_tokens": 40,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    )()
                    return type(
                        "Resp",
                        (),
                        {"content": [type("Block", (), {"text": make_json_result("God is light.")})()], "usage": usage},
                    )()

            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=lambda *args: asyncio.sleep(0),
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=lambda *args: asyncio.sleep(0),
                state_tracker=StubStateTracker(),
            )
            service._should_generate_verse_suggestions = lambda *args: False
            service._client = type("Client", (), {"messages": _Messages()})()

            await service.enrich("Dios es luz.", "God is light.", 1000)

            assert len(calls) == 1
            assert service.metrics["translation_refinement_skipped"] == 1

        run(run_())

    def test_translation_refinement_is_skipped_for_merge_repair(self):
        async def run_():
            calls = []
            raws = deque([
                make_json_result(
                    "We have fellowship with one another.",
                    merge_with_previous=True,
                    discourse_tag="answer_to_question",
                ),
            ])

            class _Messages:
                async def create(self, **kwargs):
                    calls.append(kwargs)
                    usage = type(
                        "Usage",
                        (),
                        {
                            "input_tokens": 100,
                            "output_tokens": 40,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    )()
                    return type(
                        "Resp",
                        (),
                        {"content": [type("Block", (), {"text": raws.popleft()})()], "usage": usage},
                    )()

            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=lambda *args: asyncio.sleep(0),
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=lambda *args: asyncio.sleep(0),
                state_tracker=StubStateTracker(),
            )
            service._should_generate_verse_suggestions = lambda *args: False
            service._sentence_history.append(
                ("¿Cuál es la prueba de que estamos en la luz?", "What is the proof that we are in the light?")
            )
            service._prev_discourse = {
                "discourse_tag": "rhetorical_question",
                "introduces_quote": False,
                "thought_complete": True,
                "continuation_required": False,
                "source_quality": "clean",
                "display_ready": True,
            }
            service._prev_sentence_ts = 1000
            service._client = type("Client", (), {"messages": _Messages()})()

            await service.enrich("Tenemos comunión unos con otros.", "We have fellowship with one another.", 2000)

            assert len(calls) == 1
            assert service.metrics["translation_refinement_triggered"] == 0
            assert service.metrics["translation_refinement_skipped"] == 1
            assert service.metrics["repair_skipped_hidden_merge"] == 1

        run(run_())

    def test_translation_refinement_runs_for_visible_scripture_sentence(self):
        async def run_():
            calls = []
            raws = deque([
                make_json_result(
                    "This is the message we have heard from him and are announcing to you.",
                    translation_register="scripture",
                    discourse_tag="scripture_quote",
                    sermon_mode="scripture",
                ),
                make_translation_only_result("This is the message we have heard from him and announce to you."),
            ])

            class _Messages:
                async def create(self, **kwargs):
                    calls.append(kwargs)
                    usage = type(
                        "Usage",
                        (),
                        {
                            "input_tokens": 100,
                            "output_tokens": 40,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    )()
                    return type(
                        "Resp",
                        (),
                        {"content": [type("Block", (), {"text": raws.popleft()})()], "usage": usage},
                    )()

            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=lambda *args: asyncio.sleep(0),
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=lambda *args: asyncio.sleep(0),
                state_tracker=StubStateTracker(),
            )
            service._should_generate_verse_suggestions = lambda *args: False
            service._client = type("Client", (), {"messages": _Messages()})()

            await service.enrich(
                "Este es el mensaje que hemos oído de él y os anunciamos.",
                "This is the message we have heard from him and are announcing to you.",
                1000,
            )

            assert len(calls) == 2
            assert service.metrics["translation_refinement_triggered"] == 1

        run(run_())


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
            session._recorder = None
            session._last_segment_id = 0

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
            session._recorder = None
            session._pending_feed_commits = {}
            session._committed_segment_ids = set()
            session._persisted_segment_ids = set()
            session._segment_text_cache = {}
            session._segment_metadata_cache = {}
            session._pending_segment_metadata = {}
            session._pending_detected_verses = {}
            session._pending_suggested_verses = {}
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
            await session._flush_all_pending_commits()

            assert [event["type"] for event in events] == [
                "live_translation",
                "feed_commit",
                "live_translation_clear",
            ]
            assert events[0]["text"] == "Now, let's understand, phrase by phrase, word by word, what..."
            assert events[1] == {
                "type": "feed_commit",
                "spanish": "Ahora, vamos a entender frase por frase palabra por palabra, lo que",
                "english": "Now, let's understand, phrase by phrase, word by word, what...",
                "source": "google",
                "segment_id": 1000,
                "ts": 1000,
            }
            assert events[2] == {
                "type": "live_translation_clear",
                "reason": "committed",
                "segment_id": 1000,
                "ts": 1000,
            }

        run(run_())


class TestGoogleSttConfig:
    def test_google_defaults_target_chirp_three(self):
        config = STTConfig.from_payload({})

        assert config.model == "chirp_3"
        assert config.language_codes == ("es-US", "en-US")
        assert config.location == "us"
        assert config.recognizer == "_"
        assert config.confidence_hold_threshold == 0.72
        assert config.low_confidence_hold_secs == 2.5

    def test_google_stt_config_accepts_multiple_language_codes(self):
        config = STTConfig.from_payload({
            "languageCodes": ["es-US", "en-US"],
            "diarizationEnabled": True,
            "diarizationMinSpeakers": 2,
            "diarizationMaxSpeakers": 3,
            "utteranceEndMs": 1500,
            "confidenceHoldThreshold": 0.65,
            "lowConfidenceHoldSecs": 3.0,
        })

        assert config.language_codes == ("es-US", "en-US")
        assert config.diarization_enabled is True
        assert config.diarization_min_speakers == 2
        assert config.diarization_max_speakers == 3
        assert config.utterance_end_ms == 1500
        assert config.confidence_hold_threshold == 0.65
        assert config.low_confidence_hold_secs == 3.0


class TestGoogleSpeechConfig:
    def test_google_recognition_config_includes_chirp_and_glossary_adaptation(self):
        config = STTConfig.from_payload({
            "model": "chirp_3",
            "languageCodes": ["es-MX"],
            "location": "us",
        })

        recognition = _build_recognition_config(config, 16000, {"Juan": 9, "Pentecostés": 6})

        assert recognition.model == "chirp_3"
        assert list(recognition.language_codes) == ["es-MX"]
        assert recognition.features.enable_automatic_punctuation is True
        assert recognition.adaptation.phrase_sets[0].inline_phrase_set.phrases[0].value == "Juan"

    def test_google_glossary_adaptation_omits_empty_terms(self):
        adaptation = _build_adaptation({"Juan": 8, "": 5})

        phrases = adaptation.phrase_sets[0].inline_phrase_set.phrases
        assert len(phrases) == 1
        assert phrases[0].value == "Juan"
        assert phrases[0].boost == 8.0

    def test_google_recognition_config_supports_diarization_knobs(self):
        config = STTConfig.from_payload({
            "languageCodes": ["es-US"],
            "diarizationEnabled": True,
            "diarizationMinSpeakers": 2,
            "diarizationMaxSpeakers": 4,
        })

        recognition = _build_recognition_config(config, 16000, {})

        assert recognition.features.diarization_config.min_speaker_count == 2
        assert recognition.features.diarization_config.max_speaker_count == 4

    def test_google_speech_session_is_constructible(self):
        session = GoogleSpeechSession(
            church_id="test",
            on_interim=lambda text, meta: asyncio.sleep(0),
            on_final=lambda text, start, end, meta: asyncio.sleep(0),
            on_utterance_end=lambda: asyncio.sleep(0),
        )

        assert isinstance(session, GoogleSpeechSession)


class _FakeWord:
    def __init__(
        self,
        text: str,
        *,
        speaker_label: int = 0,
        confidence: float = 0.0,
        start_s: float = 0.0,
        end_s: float = 0.0,
    ):
        self.word = text
        self.speaker_label = speaker_label
        self.confidence = confidence
        self.start_offset = SimpleNamespace(seconds=int(start_s), nanos=int((start_s % 1) * 1_000_000_000))
        self.end_offset = SimpleNamespace(seconds=int(end_s), nanos=int((end_s % 1) * 1_000_000_000))


class TestGoogleSpeechMetadata:
    def test_build_speaker_segments_groups_contiguous_words(self):
        words = [
            _FakeWord("Buenos", speaker_label=1, confidence=0.9, start_s=0.0, end_s=0.2),
            _FakeWord("dias", speaker_label=1, confidence=0.7, start_s=0.2, end_s=0.4),
            _FakeWord("church", speaker_label=2, confidence=0.8, start_s=0.4, end_s=0.7),
        ]

        assert _build_speaker_segments(words) == [
            {
                "speaker": 1,
                "start_s": 0.0,
                "end_s": 0.4,
                "text": "Buenos dias",
                "avg_confidence": 0.8,
                "word_start_index": 0,
                "word_end_index": 1,
            },
            {
                "speaker": 2,
                "start_s": 0.4,
                "end_s": 0.7,
                "text": "church",
                "avg_confidence": 0.8,
                "word_start_index": 2,
                "word_end_index": 2,
            },
        ]

    def test_speaker_metadata_exposes_dominant_speaker_and_switches(self):
        words = [
            _FakeWord("Bienvenidos", speaker_label=1, confidence=0.9, start_s=0.0, end_s=0.2),
            _FakeWord("todos", speaker_label=1, confidence=0.9, start_s=0.2, end_s=0.4),
            _FakeWord("amen", speaker_label=2, confidence=0.8, start_s=0.4, end_s=0.5),
            _FakeWord("gracias", speaker_label=1, confidence=0.7, start_s=0.5, end_s=0.7),
        ]

        assert _speaker_metadata(words) == {
            "speaker_tags": [1, 2],
            "speaker_segments": [
                {
                    "speaker": 1,
                    "start_s": 0.0,
                    "end_s": 0.4,
                    "text": "Bienvenidos todos",
                    "avg_confidence": 0.9,
                    "word_start_index": 0,
                    "word_end_index": 1,
                },
                {
                    "speaker": 2,
                    "start_s": 0.4,
                    "end_s": 0.5,
                    "text": "amen",
                    "avg_confidence": 0.8,
                    "word_start_index": 2,
                    "word_end_index": 2,
                },
                {
                    "speaker": 1,
                    "start_s": 0.5,
                    "end_s": 0.7,
                    "text": "gracias",
                    "avg_confidence": 0.7,
                    "word_start_index": 3,
                    "word_end_index": 3,
                },
            ],
            "speaker_count": 2,
            "dominant_speaker": 1,
            "speaker_switch_count": 2,
            "mixed_speaker_segment": True,
        }

    def test_segment_language_mode_detects_mixed_segments(self):
        assert _segment_language_mode("en-US", ["en-US"]) == "english"
        assert _segment_language_mode("es-US", ["es-US"]) == "spanish"
        assert _segment_language_mode("en-US", ["en-US", "es-US"]) == "mixed"
        assert _segment_language_mode("", []) == "unknown"

    def test_diarization_fallback_detects_streaming_unsupported_error(self):
        config = STTConfig.from_payload({"diarizationEnabled": True})

        assert _supports_diarization_fallback(
            RuntimeError("400 StreamingRecognize does not support Speaker Diarization."),
            config,
        ) is True
        assert _supports_diarization_fallback(RuntimeError("some other error"), config) is False
        assert _supports_diarization_fallback(
            RuntimeError("400 StreamingRecognize does not support Speaker Diarization."),
            STTConfig.from_payload({"diarizationEnabled": False}),
        ) is False


class TestSegmentStructurePrompting:
    def test_normalize_segment_structure_prefers_segment_metadata_fields(self):
        assert _normalize_segment_structure({
            "detected_language": "es-US",
            "detected_languages": ["es-US", "en-US"],
            "segment_language_mode": "mixed",
            "dominant_speaker": 2,
            "speaker_switch_count": 1,
            "mixed_speaker_segment": True,
            "speaker_segments": [{"speaker": 2, "text": "Gloria a Dios"}],
        }) == {
            "primary_language": "es-US",
            "detected_languages": ["es-US", "en-US"],
            "segment_language_mode": "mixed",
            "dominant_speaker": 2,
            "speaker_switch_count": 1,
            "mixed_speaker_segment": True,
            "speaker_segments": [{"speaker": 2, "text": "Gloria a Dios"}],
        }

    def test_merge_blocked_by_segment_structure_on_language_flip_or_speaker_change(self):
        assert _merge_blocked_by_segment_structure(
            {"segment_language_mode": "english"},
            {"segment_language_mode": "spanish"},
        ) is True
        assert _merge_blocked_by_segment_structure(
            {"segment_language_mode": "spanish", "dominant_speaker": 2},
            {"segment_language_mode": "spanish", "dominant_speaker": 1},
        ) is True
        assert _merge_blocked_by_segment_structure(
            {"segment_language_mode": "spanish", "dominant_speaker": 1},
            {"segment_language_mode": "spanish", "dominant_speaker": 1},
        ) is False

    def test_build_user_message_includes_segment_structure_blocks(self):
        message = _build_user_message(
            "Bienvenidos todos",
            "Welcome everyone",
            "",
            [],
            None,
            set(),
            "",
            None,
            None,
            {"segment_language_mode": "mixed", "dominant_speaker": 1, "speaker_switch_count": 1},
            {"segment_language_mode": "spanish", "dominant_speaker": 1, "speaker_switch_count": 0},
        )

        assert "[CURRENT SEGMENT STRUCTURE]" in message
        assert "segment_language_mode: mixed" in message
        assert "[PREVIOUS SEGMENT STRUCTURE]" in message

    def test_google_speech_session_restarts_after_unexpected_stream_end(self):
        async def run_():
            import server.services.google_speech_session as speech_module

            os.environ["GOOGLE_CLOUD_PROJECT"] = "test-project"
            finals = []
            client_instances = []

            class _FakeResponseStream:
                def __init__(self, requests, transcript):
                    self._requests = requests
                    self._transcript = transcript
                    self._sent = False

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if self._sent:
                        raise StopAsyncIteration
                    await anext(self._requests)
                    self._sent = True
                    return SimpleNamespace(
                        speech_event_type=0,
                        results=[
                            SimpleNamespace(
                                alternatives=[
                                    SimpleNamespace(
                                        transcript=self._transcript,
                                        confidence=0.91,
                                        words=[],
                                    )
                                ],
                                is_final=True,
                                result_end_offset=SimpleNamespace(seconds=1, nanos=0),
                                language_code="es-US",
                            )
                        ],
                    )

            class _FakeClient:
                def __init__(self, client_options=None):
                    self.client_options = client_options
                    self.transport = SimpleNamespace(close=lambda: None)
                    self.stream_call_count = 0
                    client_instances.append(self)

                async def streaming_recognize(self, requests):
                    self.stream_call_count += 1
                    await anext(requests)  # config request
                    transcript = f"final-{self.stream_call_count}"
                    return _FakeResponseStream(requests, transcript)

            original_client = speech_module.SpeechAsyncClient
            speech_module.SpeechAsyncClient = _FakeClient
            try:
                session = GoogleSpeechSession(
                    church_id="test",
                    on_interim=lambda text, meta: asyncio.sleep(0),
                    on_final=lambda text, start, end, meta: finals.append((text, start, end, meta)) or asyncio.sleep(0),
                    on_utterance_end=lambda: asyncio.sleep(0),
                )

                await session.start(glossary={}, sample_rate=16000)
                await session.send(b"\x01\x02")
                await asyncio.wait_for(_wait_for(lambda: len(finals) >= 1), timeout=2.0)
                await session.send(b"\x03\x04")
                await asyncio.wait_for(_wait_for(lambda: len(finals) >= 2), timeout=2.0)
                await session.stop()

                assert [item[0] for item in finals] == ["final-1", "final-2"]
                assert client_instances[0].stream_call_count >= 2
                stats = session.get_stats()
                assert stats["stream_restart_count"] >= 1
                assert stats["stream_error_count"] == 0
                assert stats["stream_offset_s"] >= 1.0
            finally:
                speech_module.SpeechAsyncClient = original_client

        run(run_())


class TestLowConfidenceHold:
    def test_low_confidence_final_requests_buffer_hold(self):
        async def run_():
            hold_calls = []
            add_calls = []
            translation_calls = []
            broadcasts = []

            class _StubSentenceBuffer:
                def hold_next(self, reason, hold_secs=3.0):
                    hold_calls.append((reason, hold_secs))

                async def add(self, text, audio_start, audio_end, stt_meta=None):
                    add_calls.append((text, audio_start, audio_end, dict(stt_meta or {})))

            class _StubTranslation:
                async def translate_fragment(self, text):
                    translation_calls.append(text)

            class _StubBroadcaster:
                async def publish(self, church_id, event):
                    broadcasts.append(event)

            from server.services.session_manager import ServiceSession

            session = ServiceSession.__new__(ServiceSession)
            session._church_id = "test"
            session._broadcaster = _StubBroadcaster()
            session._recorder = None
            session._translation = _StubTranslation()
            session._sentence_buffer = _StubSentenceBuffer()
            session._stt_config = STTConfig(confidence_hold_threshold=0.72, low_confidence_hold_secs=2.5)
            session._stt_noise_removed_count = 0

            await session._on_final(
                "Vamos al texto biblico",
                1.0,
                2.0,
                {
                    "avg_confidence": 0.5,
                    "word_count": 4,
                    "confidence_threshold": 0.72,
                    "low_confidence": True,
                },
            )

            assert hold_calls == [("low_confidence_stt", 2.5)]
            assert translation_calls == ["Vamos al texto biblico"]
            assert add_calls == [(
                "Vamos al texto biblico",
                1.0,
                2.0,
                {
                    "avg_confidence": 0.5,
                    "word_count": 4,
                    "confidence_threshold": 0.72,
                    "low_confidence": True,
                },
            )]
            assert broadcasts[0]["type"] == "stt_final"
            assert broadcasts[0]["low_confidence"] is True

        run(run_())


class TestSessionStats:
    def test_service_session_stats_include_chain_and_repair_metrics(self):
        from server.services.session_manager import ServiceSession

        session = ServiceSession.__new__(ServiceSession)
        session._sentence_buffer = SimpleNamespace(
            structural_flush_block_count=1,
            forced_release_count=2,
            conditional_flush_block_count=3,
        )
        session._enrichment = SimpleNamespace(metrics={
            "merge_chain_opened": 4,
            "merge_chain_extended": 5,
            "repair_triggered": 6,
            "repair_skipped_hidden_merge": 7,
        })
        session._stt_session = None
        session._stt_noise_removed_count = 8
        session._enrichment_settled = {1000, 2000}
        session._db_session_id = 42
        session._recorder = None

        stats = session.get_stats()

        assert stats["session_id"] == 42
        assert stats["sentence_buffer"] == {
            "structural_flush_block_count": 1,
            "forced_release_count": 2,
            "conditional_flush_block_count": 3,
        }
        assert stats["enrichment"]["merge_chain_opened"] == 4
        assert stats["enrichment"]["merge_chain_extended"] == 5
        assert stats["enrichment"]["repair_triggered"] == 6
        assert stats["enrichment"]["repair_skipped_hidden_merge"] == 7
        assert stats["stt_noise_removed_count"] == 8
        assert stats["_enrichment_settled_size"] == 2


class TestCodeSwitchingPassthrough:
    def test_english_stt_final_bypasses_fragment_translation(self):
        async def run_():
            translation_calls = []
            add_calls = []
            broadcasts = []

            class _StubTranslation:
                async def translate_fragment(self, text):
                    translation_calls.append(text)

            class _StubSentenceBuffer:
                async def add(self, text, audio_start, audio_end, stt_meta=None):
                    add_calls.append((text, audio_start, audio_end, dict(stt_meta or {})))

                def hold_next(self, reason, hold_secs=3.0):
                    return None

            class _StubBroadcaster:
                async def publish(self, church_id, event):
                    broadcasts.append(event)

            from server.services.session_manager import ServiceSession

            session = ServiceSession.__new__(ServiceSession)
            session._church_id = "test"
            session._broadcaster = _StubBroadcaster()
            session._translation = _StubTranslation()
            session._sentence_buffer = _StubSentenceBuffer()
            session._stt_config = STTConfig()
            session._recorder = None
            session._stt_noise_removed_count = 0

            await session._on_final(
                "God is light",
                1.0,
                2.0,
                {
                    "detected_language": "en-US",
                    "detected_languages": ["en-US"],
                    "avg_confidence": 0.95,
                    "word_count": 3,
                    "low_confidence": False,
                },
            )

            assert translation_calls == []
            assert broadcasts[0]["type"] == "stt_final"
            assert broadcasts[1]["type"] == "live_translation"
            assert broadcasts[1]["text"] == "God is light"
            assert broadcasts[1]["source"] == "stt_passthrough"
            assert broadcasts[1]["merge_strategy"] == "replace"
            assert add_calls == [(
                "God is light",
                1.0,
                2.0,
                {
                    "detected_language": "en-US",
                    "detected_languages": ["en-US"],
                    "avg_confidence": 0.95,
                    "word_count": 3,
                    "low_confidence": False,
                },
            )]

        run(run_())

    def test_english_sentence_flush_commits_passthrough_without_translation(self):
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
            session._pending_feed_commits = {}
            session._committed_segment_ids = set()
            session._persisted_segment_ids = set()
            session._segment_text_cache = {}
            session._segment_metadata_cache = {}
            session._pending_segment_metadata = {}
            session._pending_detected_verses = {}
            session._pending_suggested_verses = {}
            session._db_session_id = None
            session._recorder = None
            session._last_segment_id = 0

            await session._on_sentence(
                "God is light",
                10.0,
                12.0,
                "utterance_end",
                {
                    "stt_primary_language": "en-US",
                    "stt_detected_languages": ["en-US"],
                },
            )
            await session._flush_all_pending_commits()

            assert translate_calls == []
            assert [event["type"] for event in events] == [
                "final_spanish",
                "live_translation",
                "feed_commit",
                "live_translation_clear",
            ]
            assert events[1]["text"] == "God is light"
            assert events[1]["source"] == "stt_passthrough"
            assert events[2] == {
                "type": "feed_commit",
                "spanish": "God is light",
                "english": "God is light",
                "source": "passthrough",
                "stt_primary_language": "en-US",
                "stt_detected_languages": ["en-US"],
                "segment_id": events[2]["segment_id"],
                "ts": events[2]["ts"],
            }

        run(run_())


class TestInterimPreview:
    def test_interim_triggers_fast_translation_preview(self):
        async def run_():
            translation_calls = []
            broadcasts = []

            class _StubTranslation:
                async def translate_interim(self, text):
                    translation_calls.append(text)

            class _StubBroadcaster:
                async def publish(self, church_id, event):
                    broadcasts.append(event)

            from server.services.session_manager import ServiceSession

            session = ServiceSession.__new__(ServiceSession)
            session._church_id = "test"
            session._broadcaster = _StubBroadcaster()
            session._translation = _StubTranslation()
            session._recorder = None

            await session._on_interim(
                "Dios es amor",
                {"detected_language": "es-US", "detected_languages": ["es-US"]},
            )

            assert broadcasts == [{
                "type": "interim",
                "text": "Dios es amor",
                "ts": broadcasts[0]["ts"],
                "detected_language": "es-US",
                "detected_languages": ["es-US"],
            }]
            assert translation_calls == ["Dios es amor"]

        run(run_())

    def test_english_interim_uses_passthrough_preview(self):
        async def run_():
            translation_calls = []
            broadcasts = []

            class _StubTranslation:
                async def translate_interim(self, text):
                    translation_calls.append(text)

            class _StubBroadcaster:
                async def publish(self, church_id, event):
                    broadcasts.append(event)

            from server.services.session_manager import ServiceSession

            session = ServiceSession.__new__(ServiceSession)
            session._church_id = "test"
            session._broadcaster = _StubBroadcaster()
            session._translation = _StubTranslation()
            session._recorder = None

            await session._on_interim(
                "God is light",
                {"detected_language": "en-US", "detected_languages": ["en-US"]},
            )

            assert translation_calls == []
            assert [event["type"] for event in broadcasts] == ["interim", "live_translation"]
            assert broadcasts[1]["text"] == "God is light"
            assert broadcasts[1]["source"] == "stt_passthrough"
            assert broadcasts[1]["merge_strategy"] == "replace"

        run(run_())

    def test_live_translation_broadcast_includes_merge_strategy(self):
        async def run_():
            broadcasts = []

            class _StubBroadcaster:
                async def publish(self, church_id, event):
                    broadcasts.append(event)

            from server.services.session_manager import ServiceSession

            session = ServiceSession.__new__(ServiceSession)
            session._church_id = "test"
            session._broadcaster = _StubBroadcaster()

            await session._on_interim_translation("Walking in the light", "google_interim", True)

            assert broadcasts[0]["type"] == "live_translation"
            assert broadcasts[0]["source"] == "google_interim"
            assert broadcasts[0]["merge_strategy"] == "replace"

        run(run_())


class TestFollowUpPhraseAlignment:
    def test_follow_up_alignment_emits_revision_payload(self):
        async def run_():
            alignments = []

            async def on_phrase_alignment(ts, phrase_alignment):
                alignments.append((ts, phrase_alignment))

            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=lambda *args: asyncio.sleep(0),
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=lambda *args: asyncio.sleep(0),
                on_phrase_alignment=on_phrase_alignment,
                state_tracker=StubStateTracker(),
            )
            service._client = SequentialFakeAnthropicClient([
                (
                    0.01,
                    (
                        "{"
                        "\"phrase_alignment\": ["
                        "{\"english_text\": \"If we walk in the light\", \"spanish_text\": \"Si andamos en luz\"}, "
                        "{\"english_text\": \"we have fellowship\", \"spanish_text\": \"tenemos comuniÃ³n\"}, "
                        "{\"english_text\": \"with one another\", \"spanish_text\": \"unos con otros\"}"
                        "]"
                        "}"
                    ),
                ),
            ])

            await service._generate_phrase_alignment(
                ts=1000,
                spanish="Si andamos en luz, tenemos comuniÃ³n unos con otros.",
                english="If we walk in the light, we have fellowship with one another.",
                google_english="If we walk in the light, we have communion with each other.",
                source_quality="clean",
                translation_register="scripture",
                discourse_tag="scripture_quote",
                verse_detected={
                    "reference": "1 John 1:7",
                    "canonical_english": "But if we walk in the light, as he is in the light, we have fellowship one with another.",
                    "spanish_text": "Si andamos en luz",
                },
            )

            assert alignments == [
                (
                    1000,
                    [
                        {"english_text": "If we walk in the light", "spanish_text": "Si andamos en luz"},
                        {"english_text": "we have fellowship", "spanish_text": "tenemos comuniÃ³n"},
                        {"english_text": "with one another", "spanish_text": "unos con otros"},
                    ],
                )
            ]

        run(run_())

    def test_invalid_follow_up_alignment_is_dropped(self):
        async def run_():
            alignments = []

            async def on_phrase_alignment(ts, phrase_alignment):
                alignments.append((ts, phrase_alignment))

            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=lambda *args: asyncio.sleep(0),
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=lambda *args: asyncio.sleep(0),
                on_phrase_alignment=on_phrase_alignment,
                state_tracker=StubStateTracker(),
            )
            service._client = SequentialFakeAnthropicClient([
                (
                    0.01,
                    (
                        "{"
                        "\"phrase_alignment\": ["
                        "{\"english_text\": \"Completely different\", \"spanish_text\": \"tenemos comuniÃ³n\"}, "
                        "{\"english_text\": \"still wrong\", \"spanish_text\": \"unos con otros\"}"
                        "]"
                        "}"
                    ),
                ),
            ])

            await service._generate_phrase_alignment(
                ts=1000,
                spanish="Tenemos comuniÃ³n unos con otros.",
                english="We have fellowship with one another.",
                google_english="We have communion with one another.",
                source_quality="clean",
                translation_register="expository",
                discourse_tag="statement",
            )

            assert alignments == []

        run(run_())

    def test_alignment_request_message_includes_grounding_context(self):
        message = _build_alignment_request_message(
            "Si andamos en luz, tenemos comuniÃ³n unos con otros.",
            "If we walk in the light, we have fellowship with one another.",
            google_english="If we walk in the light, we have communion with each other.",
            source_quality="clean",
            translation_register="scripture",
            discourse_tag="scripture_quote",
            verse_detected={
                "reference": "1 John 1:7",
                "canonical_english": "But if we walk in the light, as he is in the light, we have fellowship one with another.",
                "spanish_text": "Si andamos en luz",
            },
        )

        assert "[GOOGLE ENGLISH BASELINE]" in message
        assert "[SCRIPTURE CONTEXT]" in message
        assert "reference: 1 John 1:7" in message

    def test_noisy_scripture_alignment_is_still_scheduled(self):
        async def run_():
            alignments = []

            async def on_phrase_alignment(ts, phrase_alignment):
                alignments.append((ts, phrase_alignment))

            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=lambda *args: asyncio.sleep(0),
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=lambda *args: asyncio.sleep(0),
                on_phrase_alignment=on_phrase_alignment,
                state_tracker=StubStateTracker(),
            )
            service._client = SequentialFakeAnthropicClient([
                (
                    0.01,
                    (
                        "{"
                        "\"phrase_alignment\": ["
                        "{\"english_text\": \"I have touched him, I have seen him\", \"spanish_text\": \"yo lo he tocado, yo lo he visto\"}, "
                        "{\"english_text\": \"I have heard him\", \"spanish_text\": \"yo lo he oído\"}"
                        "]"
                        "}"
                    ),
                ),
            ])

            service.request_phrase_alignment(
                ts=1000,
                spanish="yo lo he tocado, yo lo he visto, yo lo he oído",
                english="I have touched him, I have seen him, I have heard him.",
                google_english="I have touched him, I have seen him, I have heard him.",
                source_quality="noisy",
                translation_register="expository",
                discourse_tag="statement",
                verse_detected={
                    "reference": "1 John 1:1",
                    "canonical_english": "That which we have heard, which we have seen with our eyes...",
                    "spanish_text": "yo lo he tocado, yo lo he visto, yo lo he oído",
                },
            )
            await asyncio.sleep(0.05)

            assert alignments == [
                (
                    1000,
                    [
                        {"english_text": "I have touched him, I have seen him", "spanish_text": "yo lo he tocado, yo lo he visto"},
                        {"english_text": "I have heard him", "spanish_text": "yo lo he oído"},
                    ],
                )
            ]

        run(run_())

    def test_noisy_non_scripture_alignment_stays_suppressed(self):
        async def run_():
            alignments = []

            async def on_phrase_alignment(ts, phrase_alignment):
                alignments.append((ts, phrase_alignment))

            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=lambda *args: asyncio.sleep(0),
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=lambda *args: asyncio.sleep(0),
                on_phrase_alignment=on_phrase_alignment,
                state_tracker=StubStateTracker(),
            )
            service._client = SequentialFakeAnthropicClient([
                (
                    0.01,
                    "{\"phrase_alignment\": [{\"english_text\": \"unused\", \"spanish_text\": \"unused\"}]}",
                ),
            ])

            service.request_phrase_alignment(
                ts=1000,
                spanish="porque hay mucha gente dice",
                english="Because many people say",
                google_english="Because many people say",
                source_quality="noisy",
                translation_register="expository",
                discourse_tag="statement",
            )
            await asyncio.sleep(0.05)

            assert alignments == []

        run(run_())


class TestMergeAlignmentReschedule:
    def test_caption_merge_requests_fresh_alignment_for_kept_segment(self):
        async def run_():
            events = []
            alignment_requests = []

            class _StubBroadcaster:
                async def publish(self, church_id, event):
                    events.append(event)

            class _StubEnrichment:
                def request_phrase_alignment(self, **kwargs):
                    alignment_requests.append(kwargs)

            from server.services.session_manager import ServiceSession

            session = ServiceSession.__new__(ServiceSession)
            session._church_id = "test"
            session._broadcaster = _StubBroadcaster()
            session._enrichment = _StubEnrichment()
            session._db_session_id = None
            session._recorder = None
            session._pending_feed_commits = {}
            session._committed_segment_ids = {1000}
            session._persisted_segment_ids = set()
            session._segment_text_cache = {
                1000: {"spanish": "previo", "english": "Previous"},
                2000: {"spanish": "actual", "english": "Current"},
            }
            session._segment_metadata_cache = {
                1000: {"translation_register": "scripture", "source_quality": "clean"}
            }
            session._pending_segment_metadata = {}
            session._pending_detected_verses = {}
            session._pending_suggested_verses = {}

            await session._on_caption_merge(
                2000,
                1000,
                "Si decimos que tenemos comuniÃ³n con Ã©l, pero andamos en tinieblas, mentimos.",
                "If we say that we have fellowship with him, but walk in darkness, we lie.",
            )

            assert alignment_requests == [
                {
                    "ts": 1000,
                    "spanish": "Si decimos que tenemos comuniÃ³n con Ã©l, pero andamos en tinieblas, mentimos.",
                    "english": "If we say that we have fellowship with him, but walk in darkness, we lie.",
                    "source_quality": "clean",
                    "translation_register": "scripture",
                }
            ]
            assert any(event["type"] == "caption_merge" for event in events)

        run(run_())


class TestTranslationRepairFallback:
    def test_llm_revision_commits_pending_segment_without_visible_rewrite(self):
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
            session._recorder = None
            session._enrichment_settled = set()
            session._pending_feed_commits = {}
            session._committed_segment_ids = set()
            session._persisted_segment_ids = set()
            session._segment_text_cache = {}
            session._segment_metadata_cache = {}
            session._pending_segment_metadata = {}
            session._pending_detected_verses = {}
            session._pending_suggested_verses = {}
            session._pending_audio_timing = {
                1000: {
                    "audio_start": 0.0,
                    "audio_end": 1.0,
                    "terminal_incomplete": False,
                    "flush_reason": "timer",
                }
            }

            await session._on_translation("uno", "One", 1000)
            await session._on_translation_update(1000, "The first one")

            assert [event["type"] for event in events] == [
                "live_translation",
                "live_translation",
                "feed_commit",
                "live_translation_clear",
            ]
            assert events[2]["english"] == "The first one"
            assert events[2]["source"] == "llm"

        run(run_())

    def test_merge_candidate_is_checked_against_full_merged_unit(self):
        async def run_():
            updates = []
            merges = []

            async def on_translation_update(ts, english, phrase_alignment=None):
                updates.append((ts, english, phrase_alignment))

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
                    make_translation_only_result(
                        "What is the proof that we are in the light? We have fellowship with one another."
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
                None,
            )

        run(run_())

    def test_incomplete_primary_candidate_uses_repair_choice(self):
        async def run_():
            updates = []
            settled = []

            async def on_translation_update(ts, english, phrase_alignment=None):
                updates.append((ts, english, phrase_alignment))

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
                (
                    1000,
                    "If we walk in the light, as he himself is in the light, we have fellowship with one another.",
                    None,
                )
            ]
            assert settled == [1000]

        run(run_())

    def test_unrepaired_incomplete_candidate_falls_back_to_google(self):
        async def run_():
            updates = []
            settled = []

            async def on_translation_update(ts, english, phrase_alignment=None):
                updates.append((ts, english, phrase_alignment))

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
                    make_translation_only_result("We have fellowship with one another."),
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

    def test_phrase_alignment_emits_as_follow_up_revision(self):
        import pytest
        pytest.skip("obsolete alignment coverage replaced by follow-up alignment tests below")
        async def run_():
            updates = []

            async def on_translation_update(ts, english, phrase_alignment=None):
                updates.append((ts, english, phrase_alignment))

            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=on_translation_update,
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=lambda *args: asyncio.sleep(0),
                state_tracker=StubStateTracker(),
            )
            service._should_generate_verse_suggestions = lambda *args: False
            service._client = FakeAnthropicClient({
                "Si andamos en luz, tenemos comunión unos con otros.": (
                    0.01,
                    make_json_result(
                        "If we walk in the light, we have fellowship with one another.",
                        phrase_alignment=[
                            ("If we walk in the light", "Si andamos en luz"),
                            ("we have fellowship", "tenemos comunión"),
                            ("with one another", "unos con otros"),
                        ],
                    ),
                ),
            })

            await service.enrich(
                "Si andamos en luz, tenemos comunión unos con otros.",
                "If we walk in the light, we have fellowship with one another.",
                1000,
            )

            assert updates == [
                (
                    1000,
                    "If we walk in the light, we have fellowship with one another.",
                    [
                        {"english_text": "If we walk in the light", "spanish_text": "Si andamos en luz"},
                        {"english_text": "we have fellowship", "spanish_text": "tenemos comunión"},
                        {"english_text": "with one another", "spanish_text": "unos con otros"},
                    ],
                )
            ]

        run(run_())

    def test_invalid_phrase_alignment_is_dropped(self):
        import pytest
        pytest.skip("obsolete alignment coverage replaced by follow-up alignment tests below")
        async def run_():
            updates = []

            async def on_translation_update(ts, english, phrase_alignment=None):
                updates.append((ts, english, phrase_alignment))

            service = LLMEnrichmentService(
                church_id="test",
                church_terms={},
                topic_tracker=StubTopicTracker(),
                on_translation_update=on_translation_update,
                on_verse_detected=lambda *args: asyncio.sleep(0),
                on_verse_range_update=lambda *args: asyncio.sleep(0),
                on_verse_suggestion=lambda *args: asyncio.sleep(0),
                on_enrichment_settled=lambda *args: asyncio.sleep(0),
                state_tracker=StubStateTracker(),
            )
            service._should_generate_verse_suggestions = lambda *args: False
            service._client = FakeAnthropicClient({
                "Tenemos comunión unos con otros.": (
                    0.01,
                    make_json_result(
                        "We have fellowship with one another.",
                        phrase_alignment=[
                            ("Completely different", "tenemos comunión"),
                            ("still wrong", "unos con otros"),
                        ],
                    ),
                ),
            })

            await service.enrich(
                "Tenemos comunión unos con otros.",
                "We have fellowship with one another.",
                1000,
            )

            assert updates == []

        run(run_())
