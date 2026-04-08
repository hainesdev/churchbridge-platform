import pytest

from tests.benchmark.llm_translation_quality import (
    _build_enriched_pairs,
    _close_truncated_json_object,
    _deviation_score,
    _extract_first_json_object,
    _parse_claude_json,
    _parse_claude_json_with_repair,
)


def test_build_enriched_pairs_replays_caption_merge_as_visible_caption():
    result = {
        "all_messages": [
            {"type": "final_spanish", "ts": 1000, "text": "Primera de Juan capÃ­tulo 1.", "_elapsed_s": 1.0},
            {"type": "translation", "ts": 1000, "spanish": "Primera de Juan capÃ­tulo 1.", "english": "First John chapter 1.", "_elapsed_s": 1.2},
            {"type": "segment_metadata", "ts": 1000, "source_quality": "clean", "translation_register": "scripture", "_elapsed_s": 1.3},
            {"type": "final_spanish", "ts": 2000, "text": "Cristo.", "_elapsed_s": 2.0},
            {"type": "translation", "ts": 2000, "spanish": "Cristo.", "english": "Christ.", "_elapsed_s": 2.1},
            {"type": "translation_update", "ts": 2000, "english": "First John chapter 1. Christ.", "_elapsed_s": 2.4},
            {"type": "segment_metadata", "ts": 2000, "source_quality": "clean", "translation_register": "scripture", "_elapsed_s": 2.4},
            {
                "type": "caption_merge",
                "ts_keep": 1000,
                "ts_absorb": 2000,
                "spanish": "Primera de Juan capÃ­tulo 1. Cristo.",
                "english": "First John chapter 1. Christ.",
                "_elapsed_s": 2.5,
            },
        ]
    }

    pairs = _build_enriched_pairs(result)

    assert len(pairs) == 1
    assert pairs[0]["ts"] == 1000
    assert pairs[0]["spanish"] == "Primera de Juan capÃ­tulo 1. Cristo."
    assert pairs[0]["google_english"] == "First John chapter 1. Christ."
    assert pairs[0]["llm_english"] == "First John chapter 1. Christ."
    assert pairs[0]["deviation_score"] == 1.0


def test_build_enriched_pairs_uses_worst_source_quality_across_merge_chain():
    result = {
        "all_messages": [
            {"type": "final_spanish", "ts": 1000, "text": "A", "_elapsed_s": 1.0},
            {"type": "translation", "ts": 1000, "spanish": "A", "english": "A", "_elapsed_s": 1.1},
            {"type": "segment_metadata", "ts": 1000, "source_quality": "clean", "translation_register": "expository", "_elapsed_s": 1.2},
            {"type": "final_spanish", "ts": 2000, "text": "B", "_elapsed_s": 2.0},
            {"type": "translation", "ts": 2000, "spanish": "B", "english": "B", "_elapsed_s": 2.1},
            {"type": "segment_metadata", "ts": 2000, "source_quality": "noisy", "translation_register": "expository", "_elapsed_s": 2.2},
            {
                "type": "caption_merge",
                "ts_keep": 1000,
                "ts_absorb": 2000,
                "spanish": "A B",
                "english": "A B",
                "_elapsed_s": 2.3,
            },
        ]
    }

    pairs = _build_enriched_pairs(result)

    assert len(pairs) == 1
    assert pairs[0]["source_quality"] == "noisy"


def test_build_enriched_pairs_keeps_distinct_instances_when_ts_is_reused():
    result = {
        "all_messages": [
            {"type": "final_spanish", "ts": 1000, "text": "Primera de Juan capÃ­tulo 1, dice asÃ­, es el mensaje que hemos oÃ­do de Ã©l.", "_elapsed_s": 1.0},
            {"type": "final_spanish", "ts": 1000, "text": "Â¿QuiÃ©n es Ã©l?", "_elapsed_s": 1.01},
            {"type": "translation", "ts": 1000, "spanish": "Primera de Juan capÃ­tulo 1, dice asÃ­, es el mensaje que hemos oÃ­do de Ã©l.", "english": "First John chapter 1 says this, it is the message we have heard from him.", "_elapsed_s": 1.1},
            {"type": "translation", "ts": 1000, "spanish": "Â¿QuiÃ©n es Ã©l?", "english": "Who is he?", "_elapsed_s": 1.2},
            {"type": "segment_metadata", "ts": 1000, "source_quality": "clean", "translation_register": "expository", "_elapsed_s": 1.3},
            {"type": "final_spanish", "ts": 2000, "text": "Cristo.", "_elapsed_s": 2.0},
            {"type": "translation", "ts": 2000, "spanish": "Cristo.", "english": "Christ.", "_elapsed_s": 2.1},
            {"type": "translation_update", "ts": 1000, "english": "Who is he?", "_elapsed_s": 2.2},
            {"type": "translation_update", "ts": 2000, "english": "First John, chapter 1â€”it says this: it is the message we have heard from him. Christ.", "_elapsed_s": 2.3},
            {
                "type": "caption_merge",
                "ts_keep": 1000,
                "ts_absorb": 2000,
                "spanish": "Primera de Juan capÃ­tulo 1, dice asÃ­, es el mensaje que hemos oÃ­do de Ã©l. Cristo.",
                "english": "First John, chapter 1â€”it says this: it is the message we have heard from him. Christ.",
                "_elapsed_s": 2.4,
            },
        ]
    }

    pairs = _build_enriched_pairs(result)

    assert len(pairs) == 2
    merged_pair = next(p for p in pairs if p["spanish"].endswith("Cristo."))
    question_pair = next(p for p in pairs if p["spanish"] == "Â¿QuiÃ©n es Ã©l?")

    assert merged_pair["google_english"] == "First John chapter 1 says this, it is the message we have heard from him. Christ."
    assert merged_pair["llm_english"] == "First John, chapter 1â€”it says this: it is the message we have heard from him. Christ."
    assert question_pair["google_english"] == "Who is he?"
    assert question_pair["llm_english"] == "Who is he?"


def test_extract_first_json_object_ignores_wrapper_text_and_fences():
    raw = """```json
Here is the result you requested:
{"quality_rating": 4.0, "issues": [], "notes": "ok"}
```"""

    assert _extract_first_json_object(raw) == '{"quality_rating": 4.0, "issues": [], "notes": "ok"}'


def test_parse_claude_json_rejects_unterminated_object():
    with pytest.raises(ValueError, match="Unterminated JSON object"):
        _parse_claude_json('{"quality_rating": 4.0, "issues": ["oops"]')


def test_parse_claude_json_with_repair_recovers_truncated_fenced_response():
    raw = """```json
{
  "chunk_index": 0,
  "sentence_indices": [0, 1, 2, 3],
  "quality_rating": 4.0,
  "llm_vs_google_winner": "llm",
  "issues": [
    "Sentence 0 dropped a meaningful interjection."
  ],
  "highlights": [
    "Sentence 2 improves clause structure.",
    "LLM handles noisy material gracefully
```"""

    repaired = _close_truncated_json_object(raw)
    assert repaired is not None

    parsed = _parse_claude_json_with_repair(raw)
    assert parsed["chunk_index"] == 0
    assert parsed["quality_rating"] == 4.0
    assert parsed["highlights"][-1] == "LLM handles noisy material gracefully"


def test_deviation_score_treats_text_and_passage_as_equivalent_register_wording():
    score = _deviation_score("It is the text.", "That is the passage.")

    assert score >= 0.5
