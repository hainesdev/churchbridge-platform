import asyncio
from contextlib import asynccontextmanager

from server.services.deepgram_speech_session import _build_deepgram_listen_options
from server.services.deepgram_speech_session import DeepgramSpeechSession
from server.services.stt import STTConfig, _default_model, deepgram_language_option, infer_stt_provider


def test_infers_google_provider_for_chirp_three() -> None:
    assert infer_stt_provider("chirp_3") == "google"


def test_infers_deepgram_provider_for_nova_three() -> None:
    assert infer_stt_provider("nova-3") == "deepgram"


def test_default_model_prefers_nova_three_when_no_env_override(monkeypatch) -> None:
    monkeypatch.delenv("STT_MODEL", raising=False)
    monkeypatch.delenv("GOOGLE_SPEECH_MODEL", raising=False)

    assert _default_model() == "nova-3"


def test_multilingual_codes_switches_nova_three_to_multi_language() -> None:
    config = STTConfig.from_payload(
        {
            "model": "nova-3",
            "languageCodes": ["es-US", "en-US"],
        }
    )

    assert deepgram_language_option(config) == "multi"


def test_single_language_codes_collapse_to_deepgram_family_code() -> None:
    config = STTConfig.from_payload(
        {
            "model": "nova-3",
            "languageCodes": ["es-US"],
        }
    )

    assert deepgram_language_option(config) == "es"


def test_deepgram_listen_options_use_safe_minimal_live_profile() -> None:
    config = STTConfig.from_payload(
        {
            "model": "nova-3",
            "languageCodes": ["es-US", "en-US"],
            "interimResults": True,
            "utteranceEndMs": 1800,
            "vadEvents": True,
            "smartFormat": True,
            "punctuate": True,
        }
    )

    options = _build_deepgram_listen_options(
        config,
        sample_rate=16_000,
        glossary={
            "Espiritu Santo": 10,
            "Pentecostes": 5,
        },
    )

    assert options["model"] == "nova-3"
    assert options["encoding"] == "linear16"
    assert options["sample_rate"] == 16_000
    assert options["language"] == "multi"
    assert set(options.keys()) == {"model", "encoding", "sample_rate", "language"}


def test_deepgram_session_start_uses_async_context_manager(monkeypatch) -> None:
    connect_calls: list[dict[str, object]] = []

    class _FakeSocket:
        def __init__(self) -> None:
            self.closed = False

        async def send_media(self, message: bytes) -> None:
            return None

        async def send_finalize(self) -> None:
            self.closed = True

        async def send_close_stream(self) -> None:
            self.closed = True

        async def recv(self):
            while not self.closed:
                await asyncio.sleep(0.01)
            raise RuntimeError("socket closed")

    class _FakeListenV1:
        @asynccontextmanager
        async def connect(self, **kwargs):
            connect_calls.append(kwargs)
            yield _FakeSocket()

    class _FakeAsyncDeepgramClient:
        def __init__(self, api_key: str) -> None:
            self.listen = type("Listen", (), {"v1": _FakeListenV1()})()

    async def _run() -> None:
        monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
        from server.services import deepgram_speech_session as module

        monkeypatch.setattr(module, "AsyncDeepgramClient", _FakeAsyncDeepgramClient)
        session = DeepgramSpeechSession(
            church_id="benchmark-lab",
            on_interim=lambda *_args, **_kwargs: asyncio.sleep(0),
            on_final=lambda *_args, **_kwargs: asyncio.sleep(0),
            on_utterance_end=lambda: asyncio.sleep(0),
        )
        await session.start(
            glossary={"Pentecostes": 5},
            sample_rate=16_000,
            stt_config=STTConfig.from_payload({"model": "nova-3", "languageCodes": ["es-US", "en-US"]}),
        )
        await session.stop()

    asyncio.run(_run())

    assert connect_calls
    assert connect_calls[0]["model"] == "nova-3"
    assert connect_calls[0]["language"] == "multi"
