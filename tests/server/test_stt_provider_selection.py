from server.services.deepgram_speech_session import _build_deepgram_listen_options
from server.services.stt import STTConfig, deepgram_language_option, infer_stt_provider


def test_infers_google_provider_for_chirp_three() -> None:
    assert infer_stt_provider("chirp_3") == "google"


def test_infers_deepgram_provider_for_nova_three() -> None:
    assert infer_stt_provider("nova-3") == "deepgram"


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


def test_deepgram_listen_options_include_keyterms_and_live_flags() -> None:
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
    assert options["interim_results"] is True
    assert options["utterance_end_ms"] == 1800
    assert options["vad_events"] is True
    assert options["smart_format"] is True
    assert options["punctuate"] is True
    assert options["keyterm"] == ["Espiritu Santo", "Pentecostes"]
