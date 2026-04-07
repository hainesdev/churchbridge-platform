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

import asyncio
import pytest
from server.services.session_manager import _clean_stt, _normalize_pentecostes, _split_segments
from server.services.sentence_buffer import _is_incomplete, _is_conditional_incomplete
from server.services.llm_enrichment_service import (
    _normalize_translation,
    _scripture_speaker_normalization,
    _if_clause_validator,
)
from server.services.google_translate_service import GoogleTranslateService


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


# ---------------------------------------------------------------------------
# _split_segments — internal sentence splitting with short-fragment merging
# ---------------------------------------------------------------------------

class TestSplitSegments:
    """_split_segments splits at sentence boundaries and merges short trailing
    fragments back into the preceding sentence."""

    def test_single_sentence_returns_one_part(self):
        result = _split_segments("Dios es amor y su gracia es eterna.")
        assert result == ["Dios es amor y su gracia es eterna."]

    def test_two_complete_sentences_split(self):
        result = _split_segments("Dios es luz. Y en él no hay tinieblas.")
        assert len(result) == 2
        assert result[0] == "Dios es luz."
        assert result[1] == "Y en él no hay tinieblas."

    def test_short_answer_fragment_merges_back(self):
        """'¿Quién es él? Jesucristo.' — answer is < MIN_SPLIT_WORDS, merges."""
        result = _split_segments("¿Quién es él? Jesucristo.")
        assert len(result) == 1
        assert "Jesucristo" in result[0]
        assert "¿Quién es él?" in result[0]

    def test_question_fragment_not_merged(self):
        """Fragment starting with ¿ is a new interrogative — never merged back."""
        result = _split_segments("Él es la luz. ¿Y tú?")
        assert len(result) == 2
        assert result[1].startswith("¿")

    def test_verse_number_does_not_split(self):
        """'Juan 3:16' contains no sentence-ending punctuation — not split."""
        result = _split_segments("vamos a Juan 3:16 que dice que Dios amó al mundo")
        assert len(result) == 1

    def test_three_part_short_middle_merges_into_preceding(self):
        """Three sentences where the middle is short: middle merges into first."""
        # Build a case: long sentence, then short answer, then another full sentence
        text = "Y no hay tinieblas en él. Ninguna. Porque él es completamente puro y santo."
        result = _split_segments(text)
        # "Ninguna." is short — merges into "Y no hay tinieblas en él."
        assert len(result) == 2
        assert "Ninguna" in result[0]
        assert "completamente puro" in result[1]

    def test_long_second_sentence_does_not_merge(self):
        """A properly long second sentence stands on its own."""
        result = _split_segments(
            "Dios es luz. Y en él no hay absolutamente ninguna tiniebla ni oscuridad."
        )
        assert len(result) == 2

    def test_no_split_on_abbreviation_period(self):
        """Abbreviation followed by lowercase should not split."""
        # The regex splits only before uppercase — so "cap. siguiente" should not split
        result = _split_segments("en el cap. siguiente vemos que Dios habló a Moisés")
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Dual-pass correction — GoogleTranslateService
# ---------------------------------------------------------------------------

class FakeGoogleTranslateService(GoogleTranslateService):
    """Subclass that replaces _call_api with controllable canned responses."""

    def __init__(self, responses: list[str], **kwargs):
        # Bypass __init__ env var requirement by patching key directly
        self._api_key = "FAKE_KEY"
        self._context = __import__('collections').deque(maxlen=2)
        self._active_task = None
        self._fragment_task = None
        self._sentence_lock = asyncio.Lock()
        self._fragment_context = []
        self._http = None
        self._responses = list(responses)
        self._call_count = 0
        # Store callbacks
        self._on_translation = kwargs["on_translation"]
        self._on_correction = kwargs["on_correction"]
        self._on_interim_translation = kwargs.get("on_interim_translation", lambda *a: None)

    async def _call_api(self, html_body: str) -> str:
        result = self._responses[self._call_count % len(self._responses)]
        self._call_count += 1
        return result

    async def close(self):
        pass


class TestDualPassCorrection:
    """Dual-pass correction fires when the retranslated prior sentence differs."""

    def _make_p(self, *texts):
        """Wrap texts in <p> tags as Google would return."""
        return "".join(f"<p>{t}</p>" for t in texts)

    def test_correction_fires_when_prior_differs(self):
        async def run():
            corrections = []
            translations = []

            async def on_translation(spanish, english, ts):
                translations.append((spanish, english, ts))

            async def on_correction(ts, english):
                corrections.append((ts, english))

            # First call: translate sentence 1 → "God is light"
            svc = FakeGoogleTranslateService(
                responses=[
                    self._make_p("God is light"),       # sentence 1 (no prior)
                    self._make_p("In him is light", "And we walk together"),  # sentence 2 with correction
                ],
                on_translation=on_translation,
                on_correction=on_correction,
            )
            await svc.translate("Dios es luz.", ts=1000)
            await asyncio.sleep(0.05)
            assert translations[0][1] == "God is light"
            assert corrections == []  # no prior to correct

            # Second call: returns corrected prior + current
            await svc.translate("Y caminamos juntos.", ts=2000)
            await asyncio.sleep(0.05)
            assert len(corrections) == 1
            assert corrections[0][0] == 1000          # prior ts
            assert corrections[0][1] == "In him is light"  # corrected prior
            assert translations[1][1] == "And we walk together"

        asyncio.run(run())

    def test_no_correction_when_prior_unchanged(self):
        async def run():
            corrections = []
            translations = []

            async def on_translation(spanish, english, ts):
                translations.append((spanish, english, ts))

            async def on_correction(ts, english):
                corrections.append((ts, english))

            svc = FakeGoogleTranslateService(
                responses=[
                    self._make_p("God is light"),
                    self._make_p("God is light", "And we walk together"),  # prior unchanged
                ],
                on_translation=on_translation,
                on_correction=on_correction,
            )
            await svc.translate("Dios es luz.", ts=1000)
            await asyncio.sleep(0.05)
            await svc.translate("Y caminamos juntos.", ts=2000)
            await asyncio.sleep(0.05)
            assert corrections == []  # prior text identical — no correction

        asyncio.run(run())

    def test_no_correction_on_first_sentence(self):
        async def run():
            corrections = []

            async def on_translation(spanish, english, ts):
                pass

            async def on_correction(ts, english):
                corrections.append((ts, english))

            svc = FakeGoogleTranslateService(
                responses=[self._make_p("God is light")],
                on_translation=on_translation,
                on_correction=on_correction,
            )
            await svc.translate("Dios es luz.", ts=1000)
            await asyncio.sleep(0.05)
            assert corrections == []  # empty context — nothing to correct

        asyncio.run(run())

    def test_context_appended_after_translate(self):
        async def run():
            translations = []

            async def on_translation(spanish, english, ts):
                translations.append((spanish, english, ts))

            svc = FakeGoogleTranslateService(
                responses=[
                    self._make_p("God is light"),
                    self._make_p("God is light", "And he loves us"),
                ],
                on_translation=on_translation,
                on_correction=lambda *a: None,
            )
            assert len(svc._context) == 0
            await svc.translate("Dios es luz.", ts=1000)
            await asyncio.sleep(0.05)
            assert len(svc._context) == 1
            await svc.translate("Y él nos ama.", ts=2000)
            await asyncio.sleep(0.05)
            assert len(svc._context) == 2

        asyncio.run(run())
