import asyncio
from urllib.parse import parse_qs, urlparse

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


def test_deepgram_listen_options_restore_live_flags_and_keyterms() -> None:
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
    assert options["channels"] == 1
    assert options["language"] == "multi"
    assert options["interim_results"] is True
    assert options["utterance_end_ms"] == 1800
    assert options["vad_events"] is True
    assert options["smart_format"] is True
    assert options["punctuate"] is True
    assert options["keyterms"] == ["Espiritu Santo", "Pentecostes"]


def test_deepgram_session_start_uses_raw_websocket_profile(monkeypatch) -> None:
    connect_calls: list[dict[str, object]] = []

    class _FakeSocket:
        def __init__(self) -> None:
            self.closed = False

        async def send(self, _message) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

        async def __aiter__(self):
            while not self.closed:
                await asyncio.sleep(0.01)
                if False:
                    yield None

    class _FakeConnect:
        def __init__(self, url: str, **kwargs) -> None:
            connect_calls.append({"url": url, **kwargs})
            self._socket = _FakeSocket()

        async def __aenter__(self):
            return self._socket

        async def __aexit__(self, exc_type, exc, tb):
            self._socket.closed = True
            return False

    async def _run() -> None:
        monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
        from server.services import deepgram_speech_session as module

        monkeypatch.setattr(module.websockets, "connect", lambda url, **kwargs: _FakeConnect(url, **kwargs))
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
    query = parse_qs(urlparse(str(connect_calls[0]["url"])).query)
    assert query["model"] == ["nova-3"]
    assert query["language"] == ["multi"]
    assert query["interim_results"] == ["true"]
    assert query["utterance_end_ms"] == ["2000"]
    assert query["vad_events"] == ["true"]
    assert query["smart_format"] == ["true"]
    assert query["punctuate"] == ["true"]
    assert query["keyterms"] == ["Pentecostes"]
    assert connect_calls[0]["additional_headers"] == {"Authorization": "Token test-key"}
