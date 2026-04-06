"""
Replay tests for the precision phase improvements.

Tests the pure functions in session_manager (STT noise cleaning),
sentence_buffer (incomplete detection), and llm_enrichment_service
(translation normalization). No external API calls.

Run with:  python -m pytest tests/server/test_precision_phase.py -v
"""
import sys
import os

# Allow imports from the server package without installing
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import pytest
from server.services.session_manager import _clean_stt, _normalize_pentecostes
from server.services.sentence_buffer import _is_incomplete, _is_conditional_incomplete
from server.services.llm_enrichment_service import (
    _normalize_translation,
    _scripture_speaker_normalization,
    _if_clause_validator,
)


# ---------------------------------------------------------------------------
# PRIORITY 1 — Pentecostés STT noise removal
# ---------------------------------------------------------------------------

class TestPentecostesNoise:
    """'Pentecostés' as sentence-initial STT noise must be stripped."""

    def test_initial_noise_before_noun_removed(self):
        """'Pentecostés comunión...' → 'comunión...' (no Pentecost in output)."""
        result = _clean_stt("Pentecostés comunión unos con otros")
        assert "Pentecostés" not in result
        assert "Pentecost" not in result   # Google won't see it
        assert "comunión" in result

    def test_initial_noise_stripped_completely(self):
        """Standalone initial Pentecostés before content noun."""
        result = _clean_stt("Pentecostés Juan dice algo")
        assert "Pentecostés" not in result

    def test_whitelisted_preposition_preserved(self):
        """'de Pentecostés' — feast reference, must NOT be removed."""
        result = _clean_stt("El día de Pentecostés fue especial")
        assert "Pentecostés" in result

    def test_whitelisted_en_preserved(self):
        """'en Pentecostés' — feast reference, must NOT be removed."""
        result = _clean_stt("En Pentecostés llegó el Espíritu Santo")
        assert "Pentecostés" in result

    def test_whitelisted_copula_preserved(self):
        """'Pentecostés fue cuando...' — doctrinal subject, must NOT be removed."""
        result = _clean_stt("Pentecostés fue el día cuando Dios envió el Espíritu")
        assert "Pentecostés" in result

    def test_people_reference_rewritten_not_removed(self):
        """'somos Pentecostés' → 'somos Pentecostales' (rewrite, not remove)."""
        result = _clean_stt("somos Pentecostés y creemos en el Espíritu")
        assert "Pentecostales" in result
        assert "Pentecostés" not in result

    def test_no_pentecost_hallucination_in_google_input(self):
        """After cleaning, 'Pentecostés' prefix should not reach Google Translate."""
        raw = "Pentecostés comunión unos con otros en la fe"
        cleaned = _clean_stt(raw)
        # Google should translate 'comunión' → 'fellowship', not see 'Pentecost'
        assert not cleaned.startswith("Pentecostés")


# ---------------------------------------------------------------------------
# PRIORITY 2 — Scripture speaker normalization
# ---------------------------------------------------------------------------

class TestScriptureSpeakerNormalization:
    """Awkward speaker constructions from STT artifacts must be fixed."""

    def test_comes_and_says_normalized(self):
        """'John comes and says' → 'John says'."""
        result = _scripture_speaker_normalization("John comes and says, let us walk in the light")
        assert "comes and says" not in result
        assert "John says" in result

    def test_peter_comes_and_says_normalized(self):
        result = _scripture_speaker_normalization("Peter comes and says that we must repent")
        assert "comes and says" not in result

    def test_pentecostal_name_prefix_removed(self):
        """'Pentecostal John says' → 'John says'."""
        result = _scripture_speaker_normalization("Pentecostal John says we must walk in truth")
        assert "Pentecostal John" not in result
        assert "John says" in result

    def test_comes_to_say_normalized(self):
        """'John comes to say' → 'John says'."""
        result = _scripture_speaker_normalization("John comes to say that God is light")
        assert "comes to say" not in result
        assert "John says" in result

    def test_normal_attribution_unchanged(self):
        """'John says' already correct — must not be altered."""
        result = _scripture_speaker_normalization("John says: if we confess our sins")
        assert result == "John says: if we confess our sins"


# ---------------------------------------------------------------------------
# PRIORITY 3 — Partial sentence flush (conditional clauses)
# ---------------------------------------------------------------------------

class TestConditionalIncomplete:
    """'Si...' conditionals without an apodosis must not be flushed."""

    def test_si_without_apodosis_is_incomplete(self):
        """'Si decimos que tenemos como Jesucristo' → incomplete."""
        assert _is_incomplete("Si decimos que tenemos como Jesucristo") is True

    def test_si_without_apodosis_detected_by_conditional_check(self):
        assert _is_conditional_incomplete("Si decimos que tenemos como Jesucristo") is True

    def test_si_with_comma_apodosis_is_complete(self):
        """'Si tienes fe, todo es posible.' → complete."""
        assert _is_conditional_incomplete("Si tienes fe, todo es posible") is False

    def test_si_with_entonces_is_complete(self):
        """'Si buscas a Dios, entonces lo encontrarás.' → complete."""
        assert _is_conditional_incomplete(
            "Si buscas a Dios, entonces lo encontrarás"
        ) is False

    def test_non_conditional_unchanged(self):
        """Non-conditional sentences are not affected by the conditional check."""
        assert _is_conditional_incomplete("Dios es luz y en él no hay tinieblas.") is False

    def test_complete_si_sentence_not_blocked(self):
        """A properly resolved conditional should not be held."""
        # Has comma + substantial apodosis — should not be flagged
        assert _is_conditional_incomplete(
            "Si nosotros decimos que tenemos comunión con él, debemos andar en la luz"
        ) is False


# ---------------------------------------------------------------------------
# PRIORITY 4 — Small fragment behavior
# ---------------------------------------------------------------------------

class TestSmallFragmentIncompleteness:
    """Fragments ≤ 3 words must be flagged as incomplete so they await merge."""

    def test_single_word_fragment_incomplete(self):
        assert _is_incomplete("Jesucristo") is True

    def test_two_word_fragment_incomplete(self):
        assert _is_incomplete("Jesús Cristo") is True

    def test_three_word_fragment_incomplete(self):
        assert _is_incomplete("el amor de") is True  # also ends with preposition

    def test_complete_sentence_not_flagged(self):
        assert _is_incomplete("Dios es amor y su misericordia es eterna.") is False


# ---------------------------------------------------------------------------
# PRIORITY 7 — Conditional logic translation validation
# ---------------------------------------------------------------------------

class TestIfClauseValidator:
    """Subject-shift conditionals should be logged (not auto-corrected)."""

    def test_subject_shift_detected_no_crash(self):
        """Subject-shift conditional runs without raising."""
        result = _if_clause_validator(
            "If we say that we have Jesus Christ, I am a Christian."
        )
        # No auto-correction — text returned as-is
        assert "If we say" in result

    def test_valid_conditional_unchanged(self):
        result = _if_clause_validator(
            "If we walk in the light, we have fellowship with one another."
        )
        assert "If we walk in the light" in result


# ---------------------------------------------------------------------------
# Integration: _normalize_translation applies all post-processing
# ---------------------------------------------------------------------------

class TestNormalizeTranslationIntegration:
    """_normalize_translation must apply all normalization layers."""

    def test_transmit_replaced(self):
        result = _normalize_translation("He transmits the Word of God every Sunday.")
        assert "transmit" not in result.lower()
        assert "share" in result.lower()

    def test_comes_and_says_fixed(self):
        result = _normalize_translation("John comes and says that God is light")
        assert "comes and says" not in result

    def test_pentecostal_prefix_cleaned(self):
        result = _normalize_translation("Pentecostal John says we must repent")
        assert "Pentecostal John" not in result
