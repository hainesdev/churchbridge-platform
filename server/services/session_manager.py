import asyncio
import logging
import os
import re
import time
from fastapi import WebSocket

from server.db.bible_text import get_passage, get_passage_by_reference
from server.db.glossary import get_glossary
from server.db.church_terms import load_church_terms
from server.db.modes import save_mode_transition
from server.db.sessions import (
    create_service_session,
    close_service_session,
    append_segment,
)
from server.services.audio_utils import resample_float32_to_pcm16, base64_to_float32_bytes
from server.services.deepgram_speech_session import DeepgramSpeechSession
from server.services.google_speech_session import GoogleSpeechSession
from server.services.google_translate_service import GoogleTranslateService
from server.services.llm_enrichment_service import LLMEnrichmentService, _format_deferred_release_text
from server.services.sentence_buffer import SentenceBuffer, _is_incomplete
from server.services.stt import STTConfig, infer_stt_provider
from server.services.sermon_state_tracker import SermonStateTracker
from server.services.topic_tracker import TopicTracker
from server.services.broadcaster import Broadcaster
from server.services.session_recorder import SessionRecorder, CaptureResult, BenchmarkCaptureMetadata

logger = logging.getLogger(__name__)

# Hold the dock-to-feed handoff briefly so the interpreted area can prefer
# enriched English when it lands soon after the Google sentence.
PREFERRED_COMMIT_DELAY_S = 0.85
SHORT_FRAGMENT_COMMIT_DELAY_S = 1.5
TERMINAL_INCOMPLETE_COMMIT_DELAY_S = 0.35


def _session_capture_enabled() -> bool:
    """Default session capture to on unless explicitly disabled."""
    value = os.getenv("SESSION_CAPTURE_ENABLED")
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "off", "no"}

# Splits an STT final at internal sentence boundaries — e.g.
# "yo soy un cristiano. Pentecostés viene Juan y dice," becomes two parts.
# Lookbehind: must follow [.!?]
# Lookahead: must precede an uppercase letter or opening punctuation (¿ ¡ ")
# This avoids splitting on verse numbers ("Juan 3:16") and abbreviations ("cap.").
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÜÑ¿¡"])')

# Minimum word count for a split fragment to stand alone. Parts shorter than
# this are merged back into the preceding fragment — keeps Q&A pairs together:
# "¿Quién es él? Jesucristo." stays as one entry rather than splitting off the answer.
_MIN_SPLIT_WORDS = 5

# --- STT noise cleanup (applied before segmentation; raw text is still broadcast) ---

# Multi-character filler sounds: "AAA", "Mmm", "Uh", "Um", "Eh", "Este", "Eeh"
_STT_FILLER = re.compile(r'\b(?:A{2,}|M{2,}|Uh+|Um+|Eh+|Eeh+|Mmm+|Este+)\b', re.IGNORECASE)

# Same single character repeated (stutter): "A a Cristo" → "a Cristo"
# Matches when the SAME character appears 2+ times (case-insensitive).
# Replacement keeps the LAST instance so "A a" → "a" (the article/preposition,
# not the filler uppercase "A"). Avoids removing valid Spanish sequences like
# "y a la fe" (different single-char words in sequence).
_STT_SINGLE_REPEAT = re.compile(r'\b(\w)(?:\s+\1)+\b', re.IGNORECASE)

# Repeated function word (stutter): "que que" → "que", "el el" → "el"
# Uses an explicit allowlist of Spanish function words — prepositions, articles,
# conjunctions, and relative pronouns — rather than a character-count heuristic.
# This protects deliberate emphasis repetition of lexical words ("muy muy",
# "bien bien", "más más") which are common in Pentecostal preaching register.
_STT_WORD_REPEAT = re.compile(
    r'\b(que|el|la|los|las|un|una|de|en|con|por|para|a|al|del|'
    r'y|o|ni|si|pero|como|cuando|donde|quien|cual|ya)\s+\1\b',
    re.IGNORECASE,
)

# "Santo" as STT noise: preaching-register exclamation misclassified as sentence start.
# Patterns: "Santo tú...", "Santo él...", "Santo ella..." etc.
# PRESERVE: "Espíritu Santo", "Padre Santo", "Dios Santo" — noun-adjective order is fine
# because we only strip when "Santo" PRECEDES a personal pronoun.
_STT_SANTO_PRONOUN = re.compile(
    r'\bSanto\b\s+(?=(?:tú|él|ella|yo|usted|nosotros|nosotras|ellos|ellas|'
    r'me|te|se|lo|la|le|les|nos)\b)',
    re.IGNORECASE,
)
# "Santo" at the very start of a fragment when followed by content that is
# clearly not a predicate of "Santo" (i.e. it's a noise prefix).
# NOT stripped when followed by theological noun phrases like "Espíritu", "Padre", etc.
_STT_SANTO_INITIAL = re.compile(
    r'^Santo\s+(?!(?:Espíritu|Padre|Hijo|Tomás|Domingo|de\s+los|de\s+las|es\b|'
    r'y\s+justo|y\s+poderoso|señor|dios))',
    re.IGNORECASE,
)

# Pentecostés context normalization.
# "Pentecostés" (the biblical feast) vs "Pentecostales" (the people/movement).
# Two detection strategies — either is sufficient to trigger the rewrite:

# Strategy 1: direct possessive/copula prefix ("los Pentecostés", "somos Pentecostés")
_PENTECOSTES_PEOPLE = re.compile(
    r'\b(los|las|somos|éramos|eran|son|como|otros|iglesia|pueblo|movimiento|'
    r'hablan|hablar|decimos|dicen)\s+(Pentecostés)\b',
    re.IGNORECASE,
)

# Strategy 2: discourse context — if the sentence contains narrative/conversational
# markers alongside "Pentecostés", the preacher is almost certainly referring to
# Pentecostal people or culture, not the feast day.
# Matches first-person speech, reported speech, present-day location markers, etc.
_PENTECOSTES_RE = re.compile(r'\bPentecostés\b', re.IGNORECASE)
_PENTECOSTES_DISCOURSE = re.compile(
    r'\b(?:dice|digo|decimos|dicen|dijo|dijeron|'
    r'viene|vengo|venimos|vienen|'
    r'anoche|hoy|aquí|ahora|nosotros|'
    r'somos|éramos|eran|son|'
    r'hablan|hablar|hablamos|llamamos|llaman|se\s+llaman|'
    r'yo\s+soy|como\s+nosotros|entre\s+nosotros)\b',
    re.IGNORECASE,
)


def _normalize_pentecostes(text: str) -> str:
    """Rewrite or remove 'Pentecostés' based on structural context.

    Three strategies applied in order:
    1. Direct possessive/copula prefix → rewrite to 'Pentecostales'
    2. Discourse context markers → rewrite to 'Pentecostales'
    3. Remaining isolated 'Pentecostés' with no structural anchor → remove as STT noise
       (e.g. "Pentecostés comunión unos con otros" → "comunión unos con otros")

    Whitelist (never removed):
    - Preceded by a preposition/article: "de Pentecostés", "en Pentecostés", "el día de Pentecostés"
    - Followed by a copula/verb making it the grammatical subject: "Pentecostés fue cuando..."
    """
    # Strategy 1: direct prefix match
    text = _PENTECOSTES_PEOPLE.sub(lambda m: m.group(1) + ' Pentecostales', text)
    # Strategy 2: discourse context — if ANY discourse marker co-occurs with Pentecostés
    if _PENTECOSTES_RE.search(text) and _PENTECOSTES_DISCOURSE.search(text):
        text = _PENTECOSTES_RE.sub('Pentecostales', text)
    # Strategy 3: remove remaining isolated noise instances
    if _PENTECOSTES_RE.search(text):
        def _remove_noise(m: re.Match) -> str:
            start = m.start()
            before = text[max(0, start - 10):start]
            after = text[m.end():].lstrip()
            # Protected: preceded by preposition/article
            if re.search(r'\b(?:de|en|el|la|los|las|del|durante|desde)\s*$', before, re.IGNORECASE):
                return m.group(0)
            # Protected: followed by copula or verb making Pentecostés the subject
            if re.match(r'\b(?:es\b|era\b|fue\b|son\b|eran\b|fueron\b|será\b|ha\b|han\b|'
                        r'significa|representa|se\b|celebra|ocurrió)', after, re.IGNORECASE):
                return m.group(0)
            # Noise — remove
            return ''
        text = _PENTECOSTES_RE.sub(_remove_noise, text)
    return text


def _clean_stt(text: str) -> str:
    """Normalize STT output before segmentation, translation, and buffering.

    Applied in order, each pass targeted:
    1. Remove multi-char filler sounds (AAA, Uh, Mmm, Este...).
    2. Collapse repeated short function words ("que que" → "que").
    3. Collapse same-character stutters ("a a Cristo" → "a Cristo").
    4. Remove "Santo" when used as sentence-initial noise before a pronoun.
    5. Context-aware Pentecostés normalization/removal.
    6. Normalize internal whitespace.

    The original raw text is still broadcast as stt_final so the operator stream
    is unmodified; only the pipeline-facing text is cleaned.
    """
    text = _STT_FILLER.sub('', text)
    text = _STT_WORD_REPEAT.sub(r'\1', text)
    # Keep the LAST instance of a stuttered single character so "A a Cristo" → "a Cristo"
    # (the article/preposition "a", not the filler "A").
    text = _STT_SINGLE_REPEAT.sub(lambda m: m.group(0).split()[-1], text)
    # Strip "Santo" when it appears as STT noise before a personal pronoun
    # ("Santo tú transmites" → "tú transmites") or as a bare sentence-initial exclamation.
    # Must come before Pentecostés normalization so whitespace is clean.
    text = _STT_SANTO_PRONOUN.sub('', text)
    text = _STT_SANTO_INITIAL.sub('', text)
    text = _normalize_pentecostes(text)
    return ' '.join(text.split())


def _language_family(code: str) -> str:
    normalized = str(code or "").strip().lower()
    if normalized.startswith("es"):
        return "es"
    if normalized.startswith("en"):
        return "en"
    return ""


def _translation_mode(stt_context: dict | None) -> str:
    stt_context = dict(stt_context or {})
    primary_family = _language_family(
        stt_context.get("stt_primary_language", "") or stt_context.get("detected_language", "")
    )
    if primary_family == "en":
        return "english"
    if primary_family == "es":
        return "spanish"

    detected = stt_context.get("stt_detected_languages") or stt_context.get("detected_languages") or []
    for code in detected:
        family = _language_family(code)
        if family == "en":
            return "english"
        if family == "es":
            return "spanish"
    return "unknown"


# --- Discourse-based buffer hold detection ---
# Applied synchronously in _on_sentence (after flush, before the next fragment
# arrives) so there is no race with LLM enrichment timing.

# Quote introductions: the next sentence is almost certainly scripture text.
_QUOTE_INTRO = re.compile(
    r'\b(?:'
    r'(?:Juan|Pedro|Pablo|Jesús|Dios|David|Moisés|el\s+Señor|la\s+Biblia|'
    r'la\s+Palabra|el\s+versículo|el\s+apóstol)\s+dic[ei]'
    r'|dice\s+(?:aquí|ahí|la\s+Biblia|la\s+Palabra)'
    r'|como\s+dice\s+en'
    r'|leemos\s+que'
    r'|está\s+escrito'
    r'|escrito\s+está'
    r'|la\s+Biblia\s+dice'
    r'|la\s+Palabra\s+dice'
    r')',
    re.IGNORECASE,
)


def _split_segments(text: str) -> list[str]:
    """Split an STT final at internal sentence boundaries, then merge back
    any trailing fragment that is too short to stand alone.

    This keeps rhetorical Q&A pairs together — "¿Quién es él? Jesucristo." is
    one sentence for LLM and buffer purposes, while a longer follow-on sentence
    like "Y no hay tinieblas en él." correctly splits off as its own entry.
    """
    parts = _SENTENCE_SPLIT.split(text)
    if len(parts) == 1:
        return parts
    merged: list[str] = [parts[0]]
    for part in parts[1:]:
        # A part that opens its own question (¿) is a distinct interrogative — never
        # merge it back even if it is short, to avoid nonsensical question chains.
        if len(part.split()) < _MIN_SPLIT_WORDS and not part.lstrip().startswith('¿'):
            # Short answer or fragment — attach to the preceding sentence.
            merged[-1] = merged[-1] + ' ' + part
        else:
            merged.append(part)
    return merged


def _preferred_commit_delay_s(text: str, *, terminal_incomplete: bool) -> float:
    """Keep clearly complete captions snappy while giving tiny bridge fragments
    a bit more time for merge repair to cancel the pending commit."""
    if terminal_incomplete:
        return TERMINAL_INCOMPLETE_COMMIT_DELAY_S
    word_count = len(text.split())
    if word_count <= 3:
        return SHORT_FRAGMENT_COMMIT_DELAY_S
    return PREFERRED_COMMIT_DELAY_S


def _interim_alignment_hints_enabled() -> bool:
    value = os.getenv("CHURCHBRIDGE_INTERIM_ALIGNMENT_HINTS")
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "off", "no"}


def _english_preview_tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", (text or "").lower())


def _choose_stronger_interim_hint(current: str, candidate: str, *, replace: bool) -> str:
    current = (current or "").strip()
    candidate = (candidate or "").strip()
    if not candidate:
        return current
    if not current:
        return candidate
    if replace:
        return candidate
    current_tokens = _english_preview_tokens(current)
    candidate_tokens = _english_preview_tokens(candidate)
    if len(candidate_tokens) > len(current_tokens):
        return candidate
    if len(candidate) > len(current):
        return candidate
    return current


def _select_alignment_hint_english(
    *,
    english: str,
    google_english: str,
    interim_english_hint: str,
) -> tuple[str, str]:
    hint = (interim_english_hint or "").strip()
    if not hint:
        return "", "missing"

    normalized_hint = " ".join(hint.split()).lower()
    normalized_english = " ".join((english or "").split()).lower()
    normalized_google = " ".join((google_english or "").split()).lower()
    if normalized_hint in {"", normalized_english, normalized_google}:
        return "", "duplicate_of_final"

    hint_tokens = _english_preview_tokens(hint)
    if len(hint_tokens) < 4:
        return "", "too_short"

    baseline_tokens = _english_preview_tokens(english) or _english_preview_tokens(google_english)
    if not baseline_tokens:
        return "", "missing_baseline"

    baseline_vocab = set(baseline_tokens)
    hint_vocab = set(hint_tokens)
    overlap_ratio = len(hint_vocab & baseline_vocab) / max(min(len(hint_vocab), len(baseline_vocab)), 1)
    if overlap_ratio < 0.5:
        return "", "low_overlap"

    baseline_word_count = max(
        len(_english_preview_tokens(english)),
        len(_english_preview_tokens(google_english)),
    )
    if len(hint_tokens) < baseline_word_count + 2 and len(hint_tokens) <= 8:
        return "", "not_meaningfully_longer"

    return hint, "accepted"


def _normalize_alignment_text(text: str) -> str:
    return re.sub(r"[^a-z0-9']+", " ", (text or "").lower()).strip()


def _segment_alignment_text_overlap(left: str, right: str) -> float:
    left_tokens = {token for token in _normalize_alignment_text(left).split() if token}
    right_tokens = {token for token in _normalize_alignment_text(right).split() if token}
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(min(len(left_tokens), len(right_tokens)), 1)


def _span_overlap_ratio(
    left: dict[str, int] | None,
    right: dict[str, int] | None,
) -> float:
    if not left or not right:
        return 0.0
    left_start = int(left.get("start", -1))
    left_end = int(left.get("end", -1))
    right_start = int(right.get("start", -1))
    right_end = int(right.get("end", -1))
    if left_start < 0 or right_start < 0 or left_end <= left_start or right_end <= right_start:
        return 0.0
    overlap_start = max(left_start, right_start)
    overlap_end = min(left_end, right_end)
    if overlap_end <= overlap_start:
        return 0.0
    overlap = overlap_end - overlap_start
    left_len = left_end - left_start
    right_len = right_end - right_start
    return overlap / max(min(left_len, right_len), 1)


def _find_alignment_span(text: str, chunk_text: str) -> dict[str, int] | None:
    source = text or ""
    target = (chunk_text or "").strip()
    if not source or not target:
        return None

    direct_index = source.lower().find(target.lower())
    if direct_index >= 0:
        return {
            "start": direct_index,
            "end": direct_index + len(target),
        }

    tokens = re.findall(r"[A-Za-z0-9']+|[^A-Za-z0-9'\s]+", target)
    if not tokens:
        return None
    pattern = "".join(
        (
            re.escape(token)
            if re.fullmatch(r"[A-Za-z0-9']+", token)
            else re.escape(token).replace(r"\.", ".?")
        )
        + (r"[\s\u00A0,.;:!?\"'“”‘’()\-\u2013\u2014]*" if index < len(tokens) - 1 else "")
        for index, token in enumerate(tokens)
    )
    match = re.search(pattern, source, re.IGNORECASE)
    if not match:
        return None
    return {
        "start": match.start(),
        "end": match.end(),
    }


def _union_alignment_spans(*spans: dict[str, int] | None) -> dict[str, int] | None:
    valid_spans: list[tuple[int, int]] = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        start = int(span.get("start", -1))
        end = int(span.get("end", -1))
        if start < 0 or end <= start:
            continue
        valid_spans.append((start, end))
    if not valid_spans:
        return None
    return {
        "start": min(start for start, _ in valid_spans),
        "end": max(end for _, end in valid_spans),
    }


def _select_adjacent_merge_lineage(
    *,
    current_english_text: str,
    current_spanish_text: str,
    current_english_span: dict[str, int] | None,
    current_spanish_span: dict[str, int] | None,
    current_ordinal: int,
    overlap_candidates: list[tuple[float, float, float, int, str]],
    prior_by_id: dict[str, dict],
) -> tuple[list[str], str | None]:
    if len(overlap_candidates) < 2:
        return [], None

    candidate_ids = [candidate_id for *_, candidate_id in overlap_candidates[:3]]
    candidate_pairs: list[tuple[float, list[str]]] = []
    for left_index, left_id in enumerate(candidate_ids):
        left_prior = prior_by_id.get(left_id) or {}
        left_ordinal = int(left_prior.get("ordinal", current_ordinal))
        for right_id in candidate_ids[left_index + 1:]:
            right_prior = prior_by_id.get(right_id) or {}
            right_ordinal = int(right_prior.get("ordinal", current_ordinal))
            if abs(left_ordinal - right_ordinal) != 1:
                continue
            prior_english_overlap = _span_overlap_ratio(
                left_prior.get("english_span") if isinstance(left_prior.get("english_span"), dict) else None,
                right_prior.get("english_span") if isinstance(right_prior.get("english_span"), dict) else None,
            )
            prior_spanish_overlap = _span_overlap_ratio(
                left_prior.get("spanish_span") if isinstance(left_prior.get("spanish_span"), dict) else None,
                right_prior.get("spanish_span") if isinstance(right_prior.get("spanish_span"), dict) else None,
            )
            prior_chunk_overlap = min(prior_english_overlap, prior_spanish_overlap)
            if prior_chunk_overlap >= 0.2:
                continue
            ordered = sorted(
                ((left_ordinal, left_id, left_prior), (right_ordinal, right_id, right_prior)),
                key=lambda item: (item[0], item[1]),
            )
            ordered_ids = [item[1] for item in ordered]
            combined_english = " ".join(
                str(item[2].get("english_text", "")).strip()
                for item in ordered
                if str(item[2].get("english_text", "")).strip()
            ).strip()
            combined_spanish = " ".join(
                str(item[2].get("spanish_text", "")).strip()
                for item in ordered
                if str(item[2].get("spanish_text", "")).strip()
            ).strip()
            if not combined_english or not combined_spanish:
                continue
            combined_bilingual_overlap = min(
                _segment_alignment_text_overlap(current_english_text, combined_english),
                _segment_alignment_text_overlap(current_spanish_text, combined_spanish),
            )
            combined_span_overlap = min(
                _span_overlap_ratio(
                    current_english_span,
                    _union_alignment_spans(
                        left_prior.get("english_span") if isinstance(left_prior.get("english_span"), dict) else None,
                        right_prior.get("english_span") if isinstance(right_prior.get("english_span"), dict) else None,
                    ),
                ),
                _span_overlap_ratio(
                    current_spanish_span,
                    _union_alignment_spans(
                        left_prior.get("spanish_span") if isinstance(left_prior.get("spanish_span"), dict) else None,
                        right_prior.get("spanish_span") if isinstance(right_prior.get("spanish_span"), dict) else None,
                    ),
                ),
            )
            combined_score = max(combined_bilingual_overlap, combined_span_overlap)
            if combined_bilingual_overlap < 0.8 and combined_span_overlap < 0.8:
                continue
            candidate_pairs.append((combined_score, ordered_ids))

    if not candidate_pairs:
        return [], None

    candidate_pairs.sort(key=lambda item: (-item[0], item[1]))
    return candidate_pairs[0][1], "adjacent_merge"


# Co-incident `feed_revision` debounce window. 150 ms is short enough to stay
# below the perceptual threshold for revision-event arrival but long enough to
# catch the structural collision pattern (`context_repair` → `phrase_alignment`
# → `segmentation_repair` for the same head segment within the same enrichment
# turn).
FEED_REVISION_DEBOUNCE_S = 0.15

# Higher = more structurally significant. When two payloads collapse, the
# higher-priority `reason` wins so the client UI can still distinguish what
# kind of change shipped.
_FEED_REVISION_REASON_PRIORITY: dict[str, int] = {
    "segmentation_repair": 4,
    "context_repair": 3,
    "phrase_alignment": 2,
    "forward_context_correction": 1,
}


def _select_higher_priority_feed_revision_reason(existing: str, incoming: str) -> str:
    existing_priority = _FEED_REVISION_REASON_PRIORITY.get(existing, 0)
    incoming_priority = _FEED_REVISION_REASON_PRIORITY.get(incoming, 0)
    return incoming if incoming_priority > existing_priority else existing


class ServiceSession:
    """One active session per church_id. Owns the STT session, sentence
    buffer, translation, enrichment, topic tracking, and the admin WebSocket."""

    def __init__(self, church_id: str, ws: WebSocket, broadcaster: Broadcaster):
        self._church_id = church_id
        self._ws = ws
        self._broadcaster = broadcaster
        self._sample_rate = 48000
        self._db_session_id: int | None = None
        self._stt_session = None
        self._sentence_buffer: SentenceBuffer | None = None
        self._translation: GoogleTranslateService | None = None
        self._enrichment: LLMEnrichmentService | None = None
        self._topic_tracker: TopicTracker | None = None
        self._state_tracker: SermonStateTracker | None = None
        # Maps sentence ts → timing/flush metadata so enrichment can distinguish
        # normal committed captions from session-end truncated tails.
        self._pending_audio_timing: dict[int, dict[str, float | bool | str]] = {}
        # ts values for which LLM enrichment has completed — used to suppress
        # stale Google dual-pass corrections that arrive after the LLM has settled.
        self._enrichment_settled: set[int] = set()
        # Session-level STT noise removal counter (Pentecostés, Santo, etc.)
        self._stt_noise_removed_count: int = 0
        self._source_scripture_version: str = "rvr1960"
        self._display_scripture_version: str = "kjv"
        self._recorder: SessionRecorder | None = None
        self._pending_feed_commits: dict[int, dict] = {}
        self._committed_segment_ids: set[int] = set()
        self._persisted_segment_ids: set[int] = set()
        self._segment_text_cache: dict[int, dict] = {}
        self._segment_stt_cache: dict[int, dict] = {}
        self._segment_metadata_cache: dict[int, dict] = {}
        self._segment_alignment_hint_cache: dict[int, str] = {}
        self._segment_alignment_state_cache: dict[int, dict] = {}
        self._segment_alignment_version_cache: dict[int, int] = {}
        self._segment_root_id_cache: dict[int, int] = {}
        self._segment_merge_lineage_cache: dict[int, list[int]] = {}
        self._current_interim_alignment_hint: str = ""
        self._benchmark_capture: BenchmarkCaptureMetadata | None = None
        # Per-reason feed_revision broadcast counters. Bumped inside
        # `_emit_feed_revision_now` so the values reflect what actually shipped
        # to the client (post-coalesce volume), not what producers tried to send.
        # Surfaced via `get_stats()` under the "feed_revision" block so downstream
        # optimization work can target the dominant reason without parsing the
        # raw event log.
        self._feed_revision_metrics: dict[str, int] = {
            "emitted_total": 0,
            "emitted_context_repair": 0,
            "emitted_segmentation_repair": 0,
            "emitted_phrase_alignment": 0,
            "emitted_forward_context_correction": 0,
            "emitted_other": 0,
            "coalesced_count": 0,
            "suppressed_alignment_unchanged": 0,
        }
        self._chunk_alignment_metrics: dict[str, int] = {
            "chunk_id_reused_count": 0,
            "chunk_lineage_only_count": 0,
            "chunk_ambiguous_match_count": 0,
            "chunk_fresh_after_merge_count": 0,
            "chunk_span_missing_count": 0,
        }
        # Last-broadcast phrase-alignment signature per segment_id, keyed by
        # `(english, ((en_text, es_text), ...))`. A repeated alignment payload
        # whose signature matches the previous emit is dropped — those revisions
        # are pure no-ops on the wire and the user sees no change.
        self._segment_alignment_signature: dict[int, tuple] = {}
        # Co-incident `feed_revision` payloads for the same segment_id are
        # collapsed inside a short debounce window so the head's
        # context_repair / phrase_alignment / segmentation_repair triple does not
        # ship as three distinct revisions for what the client experiences as
        # one update. The window is intentionally short (sub-perceptual) so it
        # never delays first-visible captions; only revisions are buffered.
        self._pending_feed_revisions: dict[int, dict] = {}
        self._feed_revision_timers: dict[int, asyncio.TimerHandle] = {}
        self._pending_segment_metadata: dict[int, dict] = {}
        self._pending_detected_verses: dict[int, dict] = {}
        self._detected_verse_cache: dict[int, dict] = {}
        self._pending_suggested_verses: dict[int, list[dict]] = {}
        self._last_segment_id: int = 0
        self._stt_config: STTConfig = STTConfig()

    def _ensure_segment_stt_cache(self) -> dict[int, dict]:
        cache = getattr(self, "_segment_stt_cache", None)
        if cache is None:
            cache = {}
            self._segment_stt_cache = cache
        return cache

    def _ensure_segment_alignment_hint_cache(self) -> dict[int, str]:
        cache = getattr(self, "_segment_alignment_hint_cache", None)
        if cache is None:
            cache = {}
            self._segment_alignment_hint_cache = cache
        return cache

    def _ensure_segment_alignment_state_cache(self) -> dict[int, dict]:
        cache = getattr(self, "_segment_alignment_state_cache", None)
        if cache is None:
            cache = {}
            self._segment_alignment_state_cache = cache
        return cache

    def _ensure_segment_alignment_version_cache(self) -> dict[int, int]:
        cache = getattr(self, "_segment_alignment_version_cache", None)
        if cache is None:
            cache = {}
            self._segment_alignment_version_cache = cache
        return cache

    def _ensure_segment_root_id_cache(self) -> dict[int, int]:
        cache = getattr(self, "_segment_root_id_cache", None)
        if cache is None:
            cache = {}
            self._segment_root_id_cache = cache
        return cache

    def _ensure_segment_merge_lineage_cache(self) -> dict[int, list[int]]:
        cache = getattr(self, "_segment_merge_lineage_cache", None)
        if cache is None:
            cache = {}
            self._segment_merge_lineage_cache = cache
        return cache

    async def start(
        self,
        sample_rate: int,
        sermon_topic: str = "",
        source_scripture_version: str = "rvr1960",
        display_scripture_version: str = "kjv",
        stt_config: STTConfig | None = None,
        benchmark_capture: BenchmarkCaptureMetadata | None = None,
    ):
        self._sample_rate = sample_rate
        self._source_scripture_version = source_scripture_version or "rvr1960"
        self._display_scripture_version = display_scripture_version or "kjv"
        self._stt_config = stt_config or STTConfig()
        self._benchmark_capture = benchmark_capture
        self._db_session_id = await create_service_session(self._church_id)

        if self._capture_enabled_for_session():
            self._recorder = SessionRecorder(
                self._db_session_id,
                self._church_id,
                benchmark_capture=self._benchmark_capture,
            )
            self._recorder.record_event("session_start", {
                "church_id": self._church_id,
                "sample_rate": sample_rate,
                "benchmark_capture": self._benchmark_capture.as_dict() if self._benchmark_capture else None,
            })

        glossary = await get_glossary(self._church_id)
        church_terms = await load_church_terms(self._church_id)

        self._topic_tracker = TopicTracker(
            church_id=self._church_id,
            sermon_topic=sermon_topic,
            on_observability_event=self._on_observability_event,
        )

        self._state_tracker = SermonStateTracker(
            on_mode_change=self._on_mode_change,
        )

        self._sentence_buffer = SentenceBuffer(on_sentence=self._on_sentence)

        self._translation = GoogleTranslateService(
            on_translation=self._on_translation,
            on_correction=self._on_correction,
            on_interim_translation=self._on_interim_translation,
        )

        self._enrichment = LLMEnrichmentService(
            church_id=self._church_id,
            church_terms=church_terms,
            topic_tracker=self._topic_tracker,
            on_translation_update=self._on_translation_update,
            on_phrase_alignment=self._on_phrase_alignment,
            on_verse_detected=self._on_verse_detected,
            on_verse_range_update=self._on_verse_range_update,
            on_verse_suggestion=self._on_verse_suggestion,
            on_enrichment_settled=self._on_enrichment_settled,
            on_buffer_hold=self._on_buffer_hold,
            on_caption_merge=self._on_caption_merge,
            on_segment_metadata=self._on_segment_metadata,
            on_observability_event=self._on_observability_event,
            session_id=self._db_session_id,
            state_tracker=self._state_tracker,
        )

        stt_provider = infer_stt_provider(self._stt_config.model)
        stt_session_cls = DeepgramSpeechSession if stt_provider == "deepgram" else GoogleSpeechSession
        self._stt_session = stt_session_cls(
            church_id=self._church_id,
            on_interim=self._on_interim,
            on_final=self._on_final,
            on_utterance_end=self._on_utterance_end,
        )
        await self._stt_session.start(glossary=glossary, sample_rate=16000, stt_config=self._stt_config)

        await self._send({
            "type": "session_started",
            "sessionId": self._db_session_id,
            "sourceScriptureVersion": self._source_scripture_version,
            "displayScriptureVersion": self._display_scripture_version,
            "sttConfig": self._stt_config.public_payload(),
            "benchmarkCapture": self._benchmark_capture.as_dict() if self._benchmark_capture else None,
            "captureActive": self._recorder is not None,
        })
        await self._broadcast_pipeline_trace(
            stage="session.start",
            summary="session initialized",
            trace_kind="lifecycle",
            data={
                "session_id": self._db_session_id,
                "sample_rate": sample_rate,
                "source_scripture_version": self._source_scripture_version,
                "display_scripture_version": self._display_scripture_version,
                "stt_config": self._stt_config.public_payload(),
                "stt_provider": stt_provider,
                "benchmark_capture": self._benchmark_capture.as_dict() if self._benchmark_capture else None,
                "capture_active": self._recorder is not None,
            },
        )
        logger.info(
            "[session] Started for church %s (db_id=%s, topic=%r, source_version=%s, display_version=%s, stt_provider=%s, stt_model=%s, stt_languages=%s)",
            self._church_id,
            self._db_session_id,
            sermon_topic or "(none)",
            self._source_scripture_version,
            self._display_scripture_version,
            stt_provider,
            self._stt_config.model,
            ",".join(self._stt_config.language_codes),
        )

    async def ingest(self, audio_b64: str):
        """Receive a base64 Float32 chunk from the browser, resample, forward to STT."""
        raw = base64_to_float32_bytes(audio_b64)
        pcm16 = resample_float32_to_pcm16(raw, self._sample_rate, dst_rate=16000)
        if self._recorder:
            self._recorder.record_audio(pcm16)
        if self._stt_session:
            await self._stt_session.send(pcm16)

    async def close(self):
        if self._recorder:
            try:
                self._recorder.record_event("session_stop", {"duration_s": 0})
                result = self._recorder.stop()
                await _finalize_capture_in_db(result, self._db_session_id, self._benchmark_capture)
            except Exception as e:
                logger.warning("[session] Recorder stop failed: %s", e)
            self._recorder = None
        if self._stt_session:
            await self._stt_session.stop()
        if self._sentence_buffer:
            await self._sentence_buffer.stop()
        if self._translation:
            await self._translation.close()
        if self._enrichment:
            await self._enrichment.close()
        await self._flush_all_pending_commits()
        await self._flush_all_pending_feed_revisions()
        if self._topic_tracker:
            await self._topic_tracker.stop()
        if self._db_session_id:
            await close_service_session(self._db_session_id)
        await self._broadcast_pipeline_trace(
            stage="session.stop",
            summary="session closed",
            trace_kind="lifecycle",
            data={"session_id": self._db_session_id},
        )
        logger.info("[session] Closed for church %s", self._church_id)

    # --- STT callbacks ---

    async def _on_utterance_end(self):
        """STT VAD fired utterance end — speaker paused long enough that the
        current buffered fragments form a complete thought. Hard-flush the buffer."""
        if self._sentence_buffer:
            await self._sentence_buffer.utterance_end()

    async def _on_interim(self, text: str, stt_meta: dict | None = None):
        stt_meta = dict(stt_meta or {})
        await self._broadcast({"type": "interim", "text": text, "ts": _now(), **stt_meta})
        preview = _clean_stt(text)
        if preview and self._translation:
            if _translation_mode(stt_meta) == "english":
                await self._on_interim_translation(preview, "stt_passthrough", True)
            else:
                await self._translation.translate_interim(preview)

    async def _on_final(self, text: str, audio_start: float, audio_end: float, stt_meta: dict):
        logger.info("[session:%s] STT final: %s", self._church_id, text)
        await self._broadcast({"type": "stt_final", "text": text, "ts": _now(), **stt_meta})
        if self._recorder:
            _stt_ts = _now()
            self._recorder.record_event("stt_final", {
                "text": text,
                "audio_start": audio_start,
                "audio_end": audio_end,
                "ts": _stt_ts,
                **stt_meta,
            })
            self._recorder.record_timing("stt", _stt_ts)
        # Clean noise artifacts before segmentation; broadcast keeps the raw text.
        clean = _clean_stt(text)
        if clean != text:
            self._stt_noise_removed_count += 1
            logger.debug(
                "[session:%s] STT noise removed (count=%d): %r → %r",
                self._church_id, self._stt_noise_removed_count, text[:60], clean[:60],
            )
        await self._broadcast_pipeline_trace(
            stage="stt.final",
            summary="speech-to-text final received",
            trace_kind="ingest",
            data={
                "raw_text": text,
                "cleaned_text": clean,
                "audio_start": audio_start,
                "audio_end": audio_end,
                "noise_removed": clean != text,
                "stt_meta": stt_meta,
            },
        )
        if not clean:
            return
        if self._translation:
            if _translation_mode(stt_meta) == "english":
                await self._on_interim_translation(clean, "stt_passthrough", True)
            else:
                await self._translation.translate_fragment(clean)
        if self._sentence_buffer:
            if stt_meta.get("low_confidence"):
                self._sentence_buffer.hold_next(
                    "low_confidence_stt",
                    hold_secs=self._stt_config.low_confidence_hold_secs,
                )
                logger.debug(
                    "[session:%s] Hold set: low_confidence_stt avg_conf=%.3f threshold=%.3f",
                    self._church_id,
                    float(stt_meta.get("avg_confidence", 0.0)),
                    self._stt_config.confidence_hold_threshold,
                )
            # Proactive hold: if this fragment contains a quote introduction, set
            # a hold BEFORE adding it so the buffer's next timer waits for the
            # actual quote content to arrive. This covers the case where the intro
            # and the quote span separate STT finals — the intro accumulates
            # in the buffer with extra time for the quote to join it.
            if _QUOTE_INTRO.search(clean):
                self._sentence_buffer.hold_next("quote_introduction_proactive", hold_secs=4.0)
                logger.debug("[session:%s] Proactive hold: quote_introduction", self._church_id)
            parts = _split_segments(clean)
            if len(parts) == 1:
                await self._sentence_buffer.add(clean, audio_start, audio_end, stt_meta=stt_meta)
            else:
                # Distribute audio timing across sub-sentences proportionally by word count.
                total_words = max(sum(len(p.split()) for p in parts), 1)
                t = audio_start
                for part in parts:
                    part_end = t + (audio_end - audio_start) * len(part.split()) / total_words
                    await self._sentence_buffer.add(part, t, min(part_end, audio_end), stt_meta=stt_meta)
                    t = part_end

    # --- Sentence buffer callback ---

    async def _on_sentence(
        self,
        text: str,
        audio_start: float,
        audio_end: float,
        flush_reason: str,
        stt_context: dict | None = None,
    ):
        ts = self._next_segment_id()
        terminal_incomplete = flush_reason == "session_close" and _is_incomplete(text)
        stt_context = dict(stt_context or {})
        self._ensure_segment_stt_cache()[ts] = stt_context
        if self._recorder:
            self._recorder.record_event("sentence_flush", {
                "text": text, "flush_reason": flush_reason,
                "audio_start": audio_start, "audio_end": audio_end, "ts": ts, "segment_id": ts,
                **stt_context,
            })
            self._recorder.record_timing("sentence", ts)
        logger.info("[session:%s] Sentence flushed: %s", self._church_id, text)
        await self._broadcast({
            "type": "final_spanish",
            "text": text,
            "flush_reason": flush_reason,
            "terminal_incomplete": terminal_incomplete,
            **stt_context,
            **self._segment_ref(ts),
        })
        await self._broadcast_pipeline_trace(
            stage="sentence.flush",
            summary=f"sentence buffered and flushed via {flush_reason}",
            segment_id=ts,
            trace_kind="buffer",
            data={
                "text": text,
                "audio_start": audio_start,
                "audio_end": audio_end,
                "flush_reason": flush_reason,
                "terminal_incomplete": terminal_incomplete,
                "stt_context": stt_context,
            },
        )
        if self._topic_tracker:
            mode = self._state_tracker.settled_mode if self._state_tracker else "exposition"
            self._topic_tracker.add_segment(text, mode=mode)

        # Discourse-based holds — applied synchronously here (no LLM wait, no race).
        # We analyse the just-flushed Spanish text and ask the buffer to extend its
        # timer for the next sentence if we can predict what kind of content follows.
        if self._sentence_buffer:
            stripped = text.rstrip()
            if _QUOTE_INTRO.search(text):
                # The preacher just introduced a quotation. The next sentence is
                # almost certainly scripture — give it extra time to arrive in full.
                self._sentence_buffer.hold_next("quote_introduction", hold_secs=4.0)
                logger.debug("[session:%s] Hold set: quote_introduction", self._church_id)
            elif stripped.endswith('?'):
                # Rhetorical question — the preacher will likely answer it immediately.
                # Hold briefly so the answer arrives before we flush the question.
                self._sentence_buffer.hold_next("rhetorical_question", hold_secs=2.0)
                logger.debug("[session:%s] Hold set: rhetorical_question", self._church_id)

        if self._translation:
            interim_hint = getattr(self, "_current_interim_alignment_hint", "").strip()
            self._current_interim_alignment_hint = ""
            commit_delay_s = _preferred_commit_delay_s(
                text,
                terminal_incomplete=terminal_incomplete,
            )
            self._pending_audio_timing[ts] = {
                "audio_start": audio_start,
                "audio_end": audio_end,
                "terminal_incomplete": terminal_incomplete,
                "flush_reason": flush_reason,
                "stt_context": stt_context,
                "commit_delay_s": commit_delay_s,
                "interim_english_hint": interim_hint,
            }
            # Prune entries older than 120s — these belong to sentences whose
            # translation failed after all retries and will never be consumed.
            cutoff = ts - 120_000
            stale = [k for k in self._pending_audio_timing if k < cutoff]
            for k in stale:
                del self._pending_audio_timing[k]
            stale_settled = [k for k in self._enrichment_settled if k < cutoff]
            for k in stale_settled:
                self._enrichment_settled.discard(k)
            if _translation_mode(stt_context) == "english":
                await self._emit_passthrough_sentence(text, ts, stt_context)
            else:
                await self._translation.translate(text, ts)

    # --- Google Translation callbacks ---

    async def _emit_passthrough_sentence(self, text: str, ts: int, stt_context: dict | None = None):
        timing = self._pending_audio_timing.pop(
            ts,
            {
                "audio_start": 0.0,
                "audio_end": 0.0,
                "terminal_incomplete": False,
                "flush_reason": "",
                "stt_context": stt_context or {},
                "commit_delay_s": PREFERRED_COMMIT_DELAY_S,
                "interim_english_hint": "",
            },
        )
        stt_context = dict(stt_context or timing.get("stt_context") or {})
        english = text
        if timing.get("terminal_incomplete"):
            english = _format_deferred_release_text(english, english)
        logger.info("[session:%s] English passthrough: %s", self._church_id, english[:200])
        if self._recorder:
            self._recorder.record_event(
                "translation",
                {"spanish": text, "english": english, "ts": ts, "source": "passthrough"},
            )
            self._recorder.record_timing("translation", ts)
        await self._broadcast_pipeline_trace(
            stage="translation.passthrough",
            summary="english passthrough emitted",
            segment_id=ts,
            trace_kind="translation",
            data={
                "spanish": text,
                "english": english,
                "source": "stt_passthrough",
                "stt_context": stt_context,
            },
        )
        await self._broadcast_live_translation(
            text=english,
            source="stt_passthrough",
            display_ready=False,
            segment_id=ts,
            merge_strategy="replace",
        )
        await self._queue_feed_commit(
            segment_id=ts,
            spanish=text,
            english=english,
            source="passthrough",
            phrase_alignment=None,
            google_english=english,
            interim_english_hint=str(timing.get("interim_english_hint") or ""),
            delay_s=float(timing.get("commit_delay_s", PREFERRED_COMMIT_DELAY_S)),
            stt_context=stt_context,
        )

    async def _on_translation(self, spanish: str, english: str, ts: int):
        timing = self._pending_audio_timing.get(
            ts,
            {
                "audio_start": 0.0,
                "audio_end": 0.0,
                "terminal_incomplete": False,
                "flush_reason": "",
                "stt_context": {},
                "commit_delay_s": PREFERRED_COMMIT_DELAY_S,
                "interim_english_hint": "",
            },
        )
        stt_context = dict(timing.get("stt_context") or {})
        if timing.get("terminal_incomplete"):
            english = _format_deferred_release_text(english, english)
        logger.info("[session:%s] Translation: %s -> %s", self._church_id, spanish[:200], english[:200])
        if self._recorder:
            self._recorder.record_event("translation", {"spanish": spanish, "english": english, "ts": ts})
            self._recorder.record_timing("translation", ts)
        await self._broadcast_pipeline_trace(
            stage="translation.google",
            summary="google sentence translation returned",
            segment_id=ts,
            trace_kind="translation",
            data={
                "spanish": spanish,
                "english": english,
                "source": "google_sentence",
                "stt_context": stt_context,
            },
        )
        await self._broadcast_live_translation(
            text=english,
            source="google_sentence",
            display_ready=False,
            segment_id=ts,
            merge_strategy="replace",
        )
        await self._queue_feed_commit(
            segment_id=ts,
            spanish=spanish,
            english=english,
            source="google",
            phrase_alignment=None,
            google_english=english,
            interim_english_hint=str(timing.get("interim_english_hint") or ""),
            delay_s=float(timing.get("commit_delay_s", PREFERRED_COMMIT_DELAY_S)),
            stt_context=stt_context,
        )
        if self._enrichment:
            # Pop timing; defaults to (0.0, 0.0) if translation was retried after
            # the entry aged out (extremely rare — session would need to be very long).
            timing = self._pending_audio_timing.pop(
                ts,
                {
                    "audio_start": 0.0,
                    "audio_end": 0.0,
                    "terminal_incomplete": False,
                    "flush_reason": "",
                    "stt_context": {},
                    "commit_delay_s": PREFERRED_COMMIT_DELAY_S,
                    "interim_english_hint": "",
                },
            )
            audio_start = float(timing.get("audio_start", 0.0))
            audio_end = float(timing.get("audio_end", 0.0))
            self._enrichment.enrich(
                spanish,
                english,
                ts,
                audio_start,
                audio_end,
                terminal_incomplete=bool(timing.get("terminal_incomplete")),
                stt_context=stt_context,
            )

    async def _on_interim_translation(
        self,
        text: str,
        source: str = "google_fragment",
        replace: bool = False,
    ):
        if source in {"google_fragment", "google_interim"}:
            self._current_interim_alignment_hint = _choose_stronger_interim_hint(
                getattr(self, "_current_interim_alignment_hint", ""),
                text,
                replace=replace,
            )
        await self._broadcast_live_translation(
            text=text,
            source=source,
            display_ready=False,
            live_ts=_now(),
            merge_strategy="replace" if replace else "append",
        )

    def _segment_root_id(self, segment_id: int) -> int:
        return self._ensure_segment_root_id_cache().get(segment_id, segment_id)

    def _segment_merge_lineage(self, segment_id: int) -> list[int]:
        lineage = self._ensure_segment_merge_lineage_cache().get(segment_id)
        if lineage:
            return list(lineage)
        return [segment_id]

    def _set_segment_lineage(
        self,
        segment_id: int,
        *,
        root_segment_id: int | None = None,
        merged_from_segment_ids: list[int] | None = None,
    ) -> None:
        root_id = root_segment_id if root_segment_id is not None else self._segment_root_id(segment_id)
        self._ensure_segment_root_id_cache()[segment_id] = root_id
        lineage = merged_from_segment_ids or self._segment_merge_lineage(segment_id)
        deduped: list[int] = []
        for item in lineage:
            if item not in deduped:
                deduped.append(item)
        self._ensure_segment_merge_lineage_cache()[segment_id] = deduped

    def _build_alignment_payload(
        self,
        *,
        segment_id: int,
        english: str,
        spanish: str,
        phrase_alignment: list[dict],
        root_segment_id: int | None = None,
        merged_from_segment_ids: list[int] | None = None,
    ) -> dict:
        prior_state = self._ensure_segment_alignment_state_cache().get(segment_id) or {}
        previous_items = prior_state.get("phrase_alignment") if isinstance(prior_state, dict) else None
        previous_items = previous_items if isinstance(previous_items, list) else []
        previous_version = int(prior_state.get("alignment_version") or 0) if isinstance(prior_state, dict) else 0
        version = previous_version + 1
        resolved_root_segment_id = root_segment_id if root_segment_id is not None else self._segment_root_id(segment_id)
        resolved_merged_from = merged_from_segment_ids or self._segment_merge_lineage(segment_id)
        chunk_metrics = getattr(self, "_chunk_alignment_metrics", None)

        available_exact: dict[tuple[str, str], list[dict]] = {}
        prior_by_id: dict[str, dict] = {}
        for prior in previous_items:
            if not isinstance(prior, dict):
                continue
            chunk_id = str(prior.get("chunk_id") or "").strip()
            if chunk_id:
                prior_by_id[chunk_id] = prior
            exact_key = (
                _normalize_alignment_text(str(prior.get("english_text", ""))),
                _normalize_alignment_text(str(prior.get("spanish_text", ""))),
            )
            if exact_key[0] and exact_key[1]:
                available_exact.setdefault(exact_key, []).append(prior)

        current_chunks: list[dict] = []
        for ordinal, item in enumerate(phrase_alignment):
            english_text = str(item.get("english_text", "")).strip()
            spanish_text = str(item.get("spanish_text", "")).strip()
            if not english_text or not spanish_text:
                continue
            current_chunks.append({
                "ordinal": ordinal,
                "english_text": english_text,
                "spanish_text": spanish_text,
                "english_span": _find_alignment_span(english, english_text),
                "spanish_span": _find_alignment_span(spanish, spanish_text),
            })

        used_prior_ids: set[str] = set()
        hydrated_items: list[dict] = []
        for current in current_chunks:
            ordinal = int(current["ordinal"])
            english_text = str(current["english_text"])
            spanish_text = str(current["spanish_text"])
            english_span = current.get("english_span")
            spanish_span = current.get("spanish_span")
            if chunk_metrics is not None and (english_span is None or spanish_span is None):
                chunk_metrics["chunk_span_missing_count"] = chunk_metrics.get("chunk_span_missing_count", 0) + 1

            exact_key = (
                _normalize_alignment_text(english_text),
                _normalize_alignment_text(spanish_text),
            )
            matching_prior = available_exact.get(exact_key, [])
            reused_prior = None
            while matching_prior:
                candidate = matching_prior.pop(0)
                candidate_id = str(candidate.get("chunk_id") or "").strip()
                if candidate_id and candidate_id not in used_prior_ids:
                    reused_prior = candidate
                    used_prior_ids.add(candidate_id)
                    break
            derived_from_chunk_ids: list[str] = []
            chunk_id = ""
            remap_decision = "fresh"
            ambiguity_reason = None

            if reused_prior is not None:
                chunk_id = str(reused_prior.get("chunk_id") or "").strip()
                if chunk_id:
                    derived_from_chunk_ids = [chunk_id]
                    remap_decision = "exact_reuse"
                    if chunk_metrics is not None:
                        chunk_metrics["chunk_id_reused_count"] = chunk_metrics.get("chunk_id_reused_count", 0) + 1
            else:
                overlap_candidates: list[tuple[float, float, float, int, str]] = []
                for prior_id, prior in prior_by_id.items():
                    if prior_id in used_prior_ids:
                        continue
                    english_overlap = _segment_alignment_text_overlap(
                        english_text,
                        str(prior.get("english_text", "")),
                    )
                    spanish_overlap = _segment_alignment_text_overlap(
                        spanish_text,
                        str(prior.get("spanish_text", "")),
                    )
                    english_span_overlap = _span_overlap_ratio(
                        english_span if isinstance(english_span, dict) else None,
                        prior.get("english_span") if isinstance(prior.get("english_span"), dict) else None,
                    )
                    spanish_span_overlap = _span_overlap_ratio(
                        spanish_span if isinstance(spanish_span, dict) else None,
                        prior.get("spanish_span") if isinstance(prior.get("spanish_span"), dict) else None,
                    )
                    bilingual_overlap = min(english_overlap, spanish_overlap)
                    span_overlap = min(english_span_overlap, spanish_span_overlap)
                    score = max(bilingual_overlap, span_overlap)
                    if score >= 0.55:
                        overlap_candidates.append((
                            score,
                            bilingual_overlap,
                            span_overlap,
                            abs(ordinal - int(prior.get("ordinal", ordinal))),
                            prior_id,
                        ))
                overlap_candidates.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3], item[4]))
                if overlap_candidates:
                    top_score, top_bilingual, top_span, _, top_prior_id = overlap_candidates[0]
                    second_score = overlap_candidates[1][0] if len(overlap_candidates) > 1 else 0.0
                    strong_one_to_one = (
                        top_bilingual >= 0.8
                        and (top_span >= 0.6 or top_bilingual >= 0.9)
                        and (top_score - second_score >= 0.15 or len(overlap_candidates) == 1)
                    )
                    if strong_one_to_one:
                        chunk_id = top_prior_id
                        derived_from_chunk_ids = [top_prior_id]
                        used_prior_ids.add(top_prior_id)
                        remap_decision = "strong_reuse"
                        if chunk_metrics is not None:
                            chunk_metrics["chunk_id_reused_count"] = chunk_metrics.get("chunk_id_reused_count", 0) + 1
                    else:
                        adjacent_merge_ids, adjacent_merge_reason = _select_adjacent_merge_lineage(
                            current_english_text=english_text,
                            current_spanish_text=spanish_text,
                            current_english_span=english_span if isinstance(english_span, dict) else None,
                            current_spanish_span=spanish_span if isinstance(spanish_span, dict) else None,
                            current_ordinal=ordinal,
                            overlap_candidates=overlap_candidates,
                            prior_by_id=prior_by_id,
                        )
                        if adjacent_merge_ids:
                            derived_from_chunk_ids = adjacent_merge_ids
                            ambiguity_reason = adjacent_merge_reason
                        else:
                            derived_from_chunk_ids = [candidate_id for *_, candidate_id in overlap_candidates[:2]]
                        remap_decision = "lineage_only"
                        if (
                            not adjacent_merge_ids
                            and len(overlap_candidates) > 1
                            and abs(top_score - second_score) < 0.15
                        ):
                            ambiguity_reason = "close_competition"
                            if chunk_metrics is not None:
                                chunk_metrics["chunk_ambiguous_match_count"] = (
                                    chunk_metrics.get("chunk_ambiguous_match_count", 0) + 1
                                )
                        if chunk_metrics is not None and derived_from_chunk_ids:
                            chunk_metrics["chunk_lineage_only_count"] = (
                                chunk_metrics.get("chunk_lineage_only_count", 0) + 1
                            )
                if not chunk_id:
                    chunk_id = f"seg{resolved_root_segment_id}-v{version}-c{ordinal + 1}"
                if (
                    chunk_metrics is not None
                    and len(resolved_merged_from) > 1
                    and remap_decision in {"fresh", "lineage_only"}
                ):
                    chunk_metrics["chunk_fresh_after_merge_count"] = (
                        chunk_metrics.get("chunk_fresh_after_merge_count", 0) + 1
                    )

            if not chunk_id:
                chunk_id = f"seg{resolved_root_segment_id}-v{version}-c{ordinal + 1}"

            hydrated_items.append({
                "chunk_id": chunk_id,
                "english_text": english_text,
                "spanish_text": spanish_text,
                "english_span": english_span,
                "spanish_span": spanish_span,
                "ordinal": ordinal,
                "derived_from_chunk_ids": derived_from_chunk_ids,
                "remap_decision": remap_decision,
                "ambiguity_reason": ambiguity_reason,
            })

        payload = {
            "alignment_version": version,
            "previous_alignment_version": previous_version or None,
            "root_segment_id": resolved_root_segment_id,
            "merged_from_segment_ids": resolved_merged_from,
            "phrase_alignment": hydrated_items,
        }
        self._ensure_segment_alignment_version_cache()[segment_id] = version
        self._ensure_segment_alignment_state_cache()[segment_id] = payload
        self._set_segment_lineage(
            segment_id,
            root_segment_id=resolved_root_segment_id,
            merged_from_segment_ids=resolved_merged_from,
        )
        return payload

    async def _on_correction(self, ts: int, english: str):
        """Silently update a previously broadcast translation with better context.

        Suppressed if LLM enrichment has already settled for this ts — the LLM
        output always takes priority over Google's dual-pass correction.
        """
        if ts in self._enrichment_settled:
            logger.info(
                "[session:%s] Correction suppressed ts=%d — enrichment already settled",
                self._church_id, ts,
            )
            await self._broadcast_pipeline_trace(
                stage="translation.google_correction",
                summary="google correction suppressed after llm settled",
                segment_id=ts,
                trace_kind="decision",
                data={"english": english},
            )
            await self._broadcast({"type": "correction_suppressed", **self._segment_ref(ts)})
            return
        if ts in self._pending_feed_commits:
            self._pending_feed_commits[ts]["english"] = english
            self._pending_feed_commits[ts]["source"] = "google"
            self._pending_feed_commits[ts]["phrase_alignment"] = None
            await self._broadcast_pipeline_trace(
                stage="translation.google_correction",
                summary="google correction updated pending segment",
                segment_id=ts,
                trace_kind="translation",
                data={"english": english},
            )
            await self._broadcast_live_translation(
                text=english,
                source="google_correction",
                display_ready=False,
                segment_id=ts,
                merge_strategy="replace",
            )
            return
        await self._broadcast_pipeline_trace(
            stage="translation.google_correction",
            summary="google correction revised committed segment",
            segment_id=ts,
            trace_kind="translation",
            data={"english": english},
        )
        await self._broadcast_feed_revision(
            segment_id=ts,
            english=english,
            source="google",
            reason="forward_context_correction",
            phrase_alignment=None,
        )

    # --- LLM Enrichment callbacks ---

    async def _on_buffer_hold(self, reason: str, hold_secs: float):
        """LLM enrichment signals that the previous sentence was incomplete.

        Called when thought_complete=false — the buffer should hold the next
        sentence longer, giving the speaker's continuation more time to arrive
        and accumulate before flushing. This is a forward correction: it can't
        un-flush the incomplete sentence, but it prevents the same pattern from
        cascading into the next sentence boundary.
        """
        if self._sentence_buffer:
            self._sentence_buffer.hold_next(reason, hold_secs)
            logger.info(
                "[session:%s] Buffer hold from enrichment: %s (%.1fs)", self._church_id, reason, hold_secs
            )
            await self._broadcast_pipeline_trace(
                stage="buffer.hold",
                summary=f"buffer hold requested: {reason}",
                trace_kind="decision",
                data={"reason": reason, "hold_secs": hold_secs},
            )

    async def _on_translation_update(self, ts: int, english: str, phrase_alignment: list[dict] | None = None):
        """LLM-improved translation; replaces the Google translation on the display."""
        logger.info("[session:%s] Translation update ts=%d: %s", self._church_id, ts, english[:200])
        self._enrichment_settled.add(ts)
        await self._broadcast_pipeline_trace(
            stage="translation.llm_update",
            summary="llm translation update applied",
            segment_id=ts,
            trace_kind="translation",
            data={
                "english": english,
                "has_phrase_alignment": bool(phrase_alignment),
                "phrase_alignment": phrase_alignment,
            },
        )
        if ts in self._pending_feed_commits:
            pending = self._pending_feed_commits[ts]
            pending["english"] = english
            pending["source"] = "llm"
            pending["phrase_alignment"] = phrase_alignment
            await self._broadcast_live_translation(
                text=english,
                source="llm",
                display_ready=True,
                segment_id=ts,
                merge_strategy="replace",
            )
            await self._commit_pending_segment(ts)
            return
        await self._broadcast_feed_revision(
            segment_id=ts,
            english=english,
            source="llm",
            reason="context_repair",
            phrase_alignment=phrase_alignment,
            root_segment_id=self._segment_root_id(ts),
            merged_from_segment_ids=self._segment_merge_lineage(ts),
        )
        if not phrase_alignment:
            cached = self._segment_text_cache.get(ts, {})
            await self._request_phrase_alignment_for_segment(
                segment_id=ts,
                spanish=str(cached.get("spanish", "")),
                english=english,
                google_english=str(cached.get("english", "") or english),
                source="llm",
                prior_phrase_alignment=(
                    list(
                        self._ensure_segment_alignment_state_cache()
                        .get(ts, {})
                        .get("phrase_alignment", [])
                    )
                ),
            )

    async def _on_phrase_alignment(self, ts: int, phrase_alignment: list[dict]):
        if not phrase_alignment:
            return
        if ts in self._pending_feed_commits:
            self._pending_feed_commits[ts]["phrase_alignment"] = phrase_alignment
            return
        cached = self._segment_text_cache.get(ts)
        if not cached:
            return
        english = cached.get("english", "")
        payload = self._build_alignment_payload(
            segment_id=ts,
            english=english,
            spanish=str(cached.get("spanish", "")),
            phrase_alignment=phrase_alignment,
        )
        hydrated_alignment = payload.get("phrase_alignment", [])
        signature = self._build_alignment_signature(english, hydrated_alignment)
        signatures = getattr(self, "_segment_alignment_signature", None)
        if signatures is None:
            signatures = {}
            self._segment_alignment_signature = signatures
        if signatures.get(ts) == signature:
            metrics = getattr(self, "_feed_revision_metrics", None)
            if metrics is not None:
                metrics["suppressed_alignment_unchanged"] = (
                    metrics.get("suppressed_alignment_unchanged", 0) + 1
                )
            logger.debug(
                "[session:%s] phrase_alignment dedup ts=%d (signature unchanged)",
                self._church_id,
                ts,
            )
            return
        signatures[ts] = signature
        await self._broadcast_pipeline_trace(
            stage="alignment.emit",
            summary="phrase alignment emitted",
            segment_id=ts,
            trace_kind="alignment",
            data={
                "alignment_version": payload.get("alignment_version"),
                "previous_alignment_version": payload.get("previous_alignment_version"),
                "root_segment_id": payload.get("root_segment_id"),
                "merged_from_segment_ids": payload.get("merged_from_segment_ids"),
                "phrase_alignment": hydrated_alignment,
                "phrase_count": len(hydrated_alignment),
            },
        )
        await self._broadcast_feed_revision(
            segment_id=ts,
            english=english,
            source="llm",
            reason="phrase_alignment",
            phrase_alignment=hydrated_alignment,
            alignment_version=int(payload.get("alignment_version") or 0),
            previous_alignment_version=payload.get("previous_alignment_version"),
            root_segment_id=int(payload.get("root_segment_id") or ts),
            merged_from_segment_ids=list(payload.get("merged_from_segment_ids") or [ts]),
        )

    @staticmethod
    def _build_alignment_signature(english: str, phrase_alignment: list[dict]) -> tuple:
        return (
            english,
            tuple(
                (str(item.get("english_text", "")), str(item.get("spanish_text", "")))
                for item in phrase_alignment
            ),
        )

    async def _on_enrichment_settled(self, ts: int):
        """LLM enrichment completed (with or without a translation change).
        Marks the sentence settled so late-arriving corrections are suppressed."""
        self._enrichment_settled.add(ts)
        if self._recorder:
            self._recorder.record_event("enrichment_settled", {"ts": ts})
            self._recorder.record_timing("enrichment", ts)

    async def _hydrate_detected_verse(self, verse: dict) -> dict:
        payload = dict(verse)
        payload["explanation"] = (
            "Detected as an explicit scripture citation."
            if payload.get("confidence") == "explicit"
            else "Detected as quoted scripture based on the sermon wording."
        )
        payload["source_version_slug"] = self._source_scripture_version
        payload["display_version_slug"] = self._display_scripture_version
        try:
            source_passage = await get_passage(
                self._source_scripture_version,
                payload["book"],
                int(payload["chapter"]),
                int(payload["verse_start"]),
                payload.get("verse_end"),
            )
            display_passage = await get_passage(
                self._display_scripture_version,
                payload["book"],
                int(payload["chapter"]),
                int(payload["verse_start"]),
                payload.get("verse_end"),
            )
            payload["source_passage"] = source_passage
            payload["display_passage"] = display_passage
            if display_passage:
                payload["canonical_english"] = " ".join(v["text"] for v in display_passage["verses"])
        except Exception as e:
            logger.warning(
                "[session:%s] Failed to hydrate detected verse %s: %s",
                self._church_id,
                payload.get("reference"),
                e,
            )
            payload["source_passage"] = None
            payload["display_passage"] = None
        return payload

    async def _hydrate_suggested_verse(self, suggestion: dict) -> dict:
        payload = dict(suggestion)
        payload["explanation"] = payload.get("relevance_note", "")
        payload["source_version_slug"] = self._source_scripture_version
        payload["display_version_slug"] = self._display_scripture_version
        try:
            source_passage = await get_passage_by_reference(
                self._source_scripture_version,
                payload["reference"],
            )
            display_passage = await get_passage_by_reference(
                self._display_scripture_version,
                payload["reference"],
            )
            payload["source_passage"] = source_passage
            payload["display_passage"] = display_passage
            if display_passage:
                payload["canonical_english"] = " ".join(v["text"] for v in display_passage["verses"])
        except Exception as e:
            logger.warning(
                "[session:%s] Failed to hydrate suggested verse %s: %s",
                self._church_id,
                payload.get("reference"),
                e,
            )
            payload["source_passage"] = None
            payload["display_passage"] = None
        return payload

    async def _on_verse_detected(self, ts: int, verse: dict):
        verse = await self._hydrate_detected_verse(verse)
        logger.info("[session:%s] Verse detected: %s", self._church_id, verse.get("reference"))
        detected_cache = getattr(self, "_detected_verse_cache", None)
        if detected_cache is None:
            detected_cache = {}
            self._detected_verse_cache = detected_cache
        detected_cache[ts] = verse
        await self._broadcast_pipeline_trace(
            stage="verse.detected",
            summary=f"verse detected: {verse.get('reference')}",
            segment_id=ts,
            trace_kind="decision",
            data={"verse": verse},
        )
        if ts not in self._committed_segment_ids:
            self._pending_detected_verses[ts] = verse
            return
        await self._broadcast({"type": "verse_detected", "verse": verse, **self._segment_ref(ts)})

    async def _on_verse_range_update(self, ts: int, verse: dict):
        verse = await self._hydrate_detected_verse(verse)
        logger.info("[session:%s] Verse range update: %s", self._church_id, verse.get("reference"))
        detected_cache = getattr(self, "_detected_verse_cache", None)
        if detected_cache is None:
            detected_cache = {}
            self._detected_verse_cache = detected_cache
        detected_cache[ts] = verse
        if ts not in self._committed_segment_ids:
            self._pending_detected_verses[ts] = verse
            return
        await self._broadcast({"type": "verse_range_update", "verse": verse, **self._segment_ref(ts)})

    async def _on_verse_suggestion(self, ts: int, suggestions: list[dict]):
        suggestions = [await self._hydrate_suggested_verse(s) for s in suggestions]
        logger.info(
            "[session:%s] Verse suggestions for ts=%d: %s",
            self._church_id, ts, [s["reference"] for s in suggestions],
        )
        if ts not in self._committed_segment_ids:
            self._pending_suggested_verses[ts] = suggestions
            return
        await self._broadcast({"type": "verse_suggestion", "suggestions": suggestions, **self._segment_ref(ts)})

    async def _on_caption_merge(self, absorb_ts: int, keep_ts: int, merged_spanish: str, merged_english: str):
        """LLM signals that two segments should be merged to repair a bad stream split.

        The chain is head-anchored: keep_ts is always the earliest visible segment
        (the anchor); absorb_ts is the fragment being folded into it.
        Broadcasts caption_merge as a segmentation-repair event.
        """
        logger.info(
            "[session:%s] Caption merge: keep=%d absorbs=%d",
            self._church_id, keep_ts, absorb_ts,
        )
        await self._broadcast_pipeline_trace(
            stage="caption.merge",
            summary=f"segment {keep_ts} absorbed {absorb_ts}",
            segment_id=keep_ts,
            trace_kind="merge",
            data={
                "segment_id_keep": keep_ts,
                "segment_id_absorb": absorb_ts,
                "merged_spanish": merged_spanish,
                "merged_english": merged_english,
            },
        )
        keep_was_committed = keep_ts in self._committed_segment_ids
        await self._drop_pending_commit(absorb_ts)
        # Cancel any in-flight debounced revision targeting the absorbed
        # segment — its visible identity is gone, so a late flush would emit
        # a revision for a segment the client no longer renders.
        self._discard_pending_feed_revision(absorb_ts)
        # The alignment signature for the absorbed ts is no longer addressable;
        # the head's signature must also be invalidated since its English is
        # about to change.
        signatures = getattr(self, "_segment_alignment_signature", None)
        if signatures is not None:
            signatures.pop(absorb_ts, None)
            signatures.pop(keep_ts, None)
        prior_alignment_state = self._ensure_segment_alignment_state_cache().get(keep_ts)
        merged_lineage: list[int] = []
        for item in (
            self._segment_merge_lineage(keep_ts)
            + self._segment_merge_lineage(absorb_ts)
            + [keep_ts, absorb_ts]
        ):
            if item not in merged_lineage:
                merged_lineage.append(item)
        self._set_segment_lineage(
            keep_ts,
            root_segment_id=self._segment_root_id(keep_ts),
            merged_from_segment_ids=merged_lineage,
        )
        self._ensure_segment_alignment_state_cache().pop(absorb_ts, None)
        self._ensure_segment_alignment_version_cache().pop(absorb_ts, None)
        self._ensure_segment_root_id_cache().pop(absorb_ts, None)
        self._ensure_segment_merge_lineage_cache().pop(absorb_ts, None)
        self._segment_text_cache.pop(absorb_ts, None)
        self._ensure_segment_stt_cache().pop(absorb_ts, None)
        self._segment_metadata_cache.pop(absorb_ts, None)
        self._ensure_segment_alignment_hint_cache().pop(absorb_ts, None)
        detected_cache = getattr(self, "_detected_verse_cache", None)
        if detected_cache is not None:
            detected_cache.pop(absorb_ts, None)
        self._pending_segment_metadata.pop(absorb_ts, None)
        self._pending_detected_verses.pop(absorb_ts, None)
        self._pending_suggested_verses.pop(absorb_ts, None)
        if keep_ts in self._pending_feed_commits:
            pending = self._pending_feed_commits[keep_ts]
            pending["spanish"] = merged_spanish
            pending["english"] = merged_english
            pending["source"] = "llm"
            pending["phrase_alignment"] = None
            pending["google_english"] = merged_english
            pending["interim_english_hint"] = _choose_stronger_interim_hint(
                str(pending.get("interim_english_hint") or ""),
                self._ensure_segment_alignment_hint_cache().get(absorb_ts, ""),
                replace=False,
            )
            await self._broadcast_live_translation(
                text=merged_english,
                source="llm",
                display_ready=True,
                segment_id=keep_ts,
                merge_strategy="replace",
            )
            await self._commit_pending_segment(keep_ts)
        await self._broadcast({
            "type": "caption_merge",
            "reason": "segmentation_repair",
            "spanish": merged_spanish,
            "english": merged_english,
            "root_segment_id": self._segment_root_id(keep_ts),
            "merged_from_segment_ids": merged_lineage,
            **self._merge_ref(keep_ts, absorb_ts),
        })
        if keep_was_committed:
            await self._broadcast_feed_revision(
                segment_id=keep_ts,
                english=merged_english,
                spanish=merged_spanish,
                source="llm",
                reason="segmentation_repair",
                phrase_alignment=None,
                root_segment_id=self._segment_root_id(keep_ts),
                merged_from_segment_ids=merged_lineage,
            )
            await self._request_phrase_alignment_for_segment(
                segment_id=keep_ts,
                spanish=merged_spanish,
                english=merged_english,
                google_english=merged_english,
                source="llm",
                interim_english_hint=self._ensure_segment_alignment_hint_cache().get(keep_ts, ""),
                prior_phrase_alignment=(
                    list(prior_alignment_state.get("phrase_alignment", []))
                    if isinstance(prior_alignment_state, dict) else None
                ),
            )

    async def _on_segment_metadata(self, ts: int, metadata: dict):
        """Broadcast scaffolding metadata for a committed segment.

        Carries translation_register, paragraph_break, and source_quality.
        The frontend stores these for future display logic (e.g. register will
        drive exact Bible verse text lookup once Bible versions are stored).
        """
        metadata = {
            **metadata,
            **self._ensure_segment_stt_cache().get(ts, {}),
        }
        await self._broadcast_pipeline_trace(
            stage="segment.metadata",
            summary="segment metadata updated",
            segment_id=ts,
            trace_kind="metadata",
            data=metadata,
        )
        self._pending_segment_metadata[ts] = metadata
        self._segment_metadata_cache[ts] = dict(metadata)
        if metadata.get("pending_completion") and ts in self._pending_feed_commits:
            pending = self._pending_feed_commits[ts]
            task = pending.get("task")
            if task:
                task.cancel()
                pending["task"] = None
        if ts in self._committed_segment_ids:
            await self._broadcast({
                "type": "segment_metadata",
                **metadata,
                **self._segment_ref(ts),
            })

    async def _on_mode_change(self, old_mode: str, new_mode: str, ts: int):
        """Fired when the settled sermon mode transitions."""
        logger.info(
            "[session:%s] Mode transition: %s → %s (ts=%d)",
            self._church_id, old_mode, new_mode, ts,
        )
        if self._db_session_id:
            try:
                await save_mode_transition(self._db_session_id, old_mode, new_mode, ts)
            except Exception as e:
                logger.warning("[session:%s] save_mode_transition failed: %s", self._church_id, e)
        await self._broadcast({
            "type": "mode_change",
            "from": old_mode,
            "to": new_mode,
            **self._segment_ref(ts),
        })

    # --- Helpers ---

    def get_stats(self) -> dict:
        """Return operational metrics for this session. Used by the stats endpoint."""
        return {
            "sentence_buffer": {
                "structural_flush_block_count": (
                    self._sentence_buffer.structural_flush_block_count
                    if self._sentence_buffer else 0
                ),
                "forced_release_count": (
                    self._sentence_buffer.forced_release_count
                    if self._sentence_buffer else 0
                ),
                "conditional_flush_block_count": (
                    self._sentence_buffer.conditional_flush_block_count
                    if self._sentence_buffer else 0
                ),
            },
            "enrichment": dict(self._enrichment.metrics) if self._enrichment else {},
            "feed_revision": dict(getattr(self, "_feed_revision_metrics", {}) or {}),
            "chunk_alignment": dict(getattr(self, "_chunk_alignment_metrics", {}) or {}),
            "stt_session": self._stt_session.get_stats() if self._stt_session else {},
            "stt_noise_removed_count": self._stt_noise_removed_count,
            "_enrichment_settled_size": len(self._enrichment_settled),
            "session_id": self._db_session_id,
            "latency_ms": self._recorder.compute_latency() if self._recorder else {
                "stt_to_sentence": {"p50": None, "p90": None, "count": 0},
                "sentence_to_translation": {"p50": None, "p90": None, "count": 0},
                "translation_to_enrichment": {"p50": None, "p90": None, "count": 0},
            },
            "capture_active": self._recorder is not None,
            "benchmark_capture": self._benchmark_capture.as_dict() if self._benchmark_capture else None,
        }

    async def _on_observability_event(self, event: dict) -> None:
        await self._broadcast_pipeline_trace(
            stage=str(event.get("trace_stage") or "pipeline"),
            summary=str(event.get("summary") or "trace"),
            segment_id=event.get("segment_id"),
            trace_kind=str(event.get("trace_kind") or "event"),
            data=event.get("data") if isinstance(event.get("data"), dict) else None,
            call_id=str(event["call_id"]) if event.get("call_id") is not None else None,
        )

    async def _broadcast_pipeline_trace(
        self,
        *,
        stage: str,
        summary: str,
        segment_id: int | None = None,
        trace_kind: str = "event",
        data: dict | None = None,
        call_id: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "type": "pipeline_trace",
            "trace_stage": stage,
            "trace_kind": trace_kind,
            "summary": summary,
            "ts": _now(),
        }
        if segment_id is not None:
            payload["segment_id"] = segment_id
        if call_id is not None:
            payload["call_id"] = call_id
        if data:
            payload["data"] = data
        await self._broadcast(payload)

    async def _broadcast(self, event: dict):
        await self._broadcaster.publish(self._church_id, event)

    async def _queue_feed_commit(
        self,
        segment_id: int,
        spanish: str,
        english: str,
        source: str,
        phrase_alignment: list[dict] | None,
        google_english: str | None,
        delay_s: float,
        interim_english_hint: str | None = None,
        stt_context: dict | None = None,
    ) -> None:
        await self._drop_pending_commit(segment_id)
        task = asyncio.create_task(self._delayed_feed_commit(segment_id, delay_s))
        self._pending_feed_commits[segment_id] = {
            "spanish": spanish,
            "english": english,
            "source": source,
            "phrase_alignment": phrase_alignment,
            "google_english": google_english if google_english is not None else english,
            "interim_english_hint": interim_english_hint or "",
            "stt_context": dict(stt_context or {}),
            "task": task,
        }

    async def _drop_pending_commit(self, segment_id: int) -> None:
        pending = self._pending_feed_commits.pop(segment_id, None)
        if not pending:
            return
        task = pending.get("task")
        if task:
            task.cancel()

    async def _delayed_feed_commit(self, segment_id: int, delay_s: float) -> None:
        try:
            await asyncio.sleep(delay_s)
            await self._commit_pending_segment(segment_id)
        except asyncio.CancelledError:
            return

    async def _commit_pending_segment(self, segment_id: int) -> None:
        pending = self._pending_feed_commits.pop(segment_id, None)
        if not pending:
            return
        is_first_commit = segment_id not in self._committed_segment_ids
        task = pending.get("task")
        if task and task is not asyncio.current_task():
            task.cancel()
        await self._broadcast_feed_commit(
            segment_id=segment_id,
            spanish=pending["spanish"],
            english=pending["english"],
            source=pending["source"],
            phrase_alignment=pending.get("phrase_alignment"),
            stt_context=pending.get("stt_context"),
        )
        hint_enabled = _interim_alignment_hints_enabled()
        if hint_enabled:
            interim_hint, interim_hint_reason = _select_alignment_hint_english(
                english=pending["english"],
                google_english=str(pending.get("google_english") or pending["english"]),
                interim_english_hint=str(pending.get("interim_english_hint") or ""),
            )
        else:
            interim_hint = ""
            interim_hint_reason = "disabled"
        if interim_hint:
            self._ensure_segment_alignment_hint_cache()[segment_id] = interim_hint
        else:
            self._ensure_segment_alignment_hint_cache().pop(segment_id, None)
        if not pending.get("phrase_alignment"):
            await self._request_phrase_alignment_for_segment(
                segment_id=segment_id,
                spanish=pending["spanish"],
                english=pending["english"],
                google_english=str(pending.get("google_english") or pending["english"]),
                source=pending["source"],
                interim_english_hint=interim_hint,
                interim_hint_reason=interim_hint_reason,
                prior_phrase_alignment=(
                    list(
                        self._ensure_segment_alignment_state_cache()
                        .get(segment_id, {})
                        .get("phrase_alignment", [])
                    )
                ),
            )
        await self._broadcast_live_translation_clear(reason="committed", segment_id=segment_id)
        self._committed_segment_ids.add(segment_id)
        if self._db_session_id and is_first_commit and segment_id not in self._persisted_segment_ids:
            await append_segment(self._db_session_id, pending["spanish"], pending["english"])
            self._persisted_segment_ids.add(segment_id)
        await self._flush_buffered_segment_state(segment_id)

    async def _request_phrase_alignment_for_segment(
        self,
        *,
        segment_id: int,
        spanish: str,
        english: str,
        google_english: str,
        source: str,
        interim_english_hint: str = "",
        interim_hint_reason: str = "",
        prior_phrase_alignment: list[dict] | None = None,
    ) -> None:
        enrichment = getattr(self, "_enrichment", None)
        if enrichment is None or source == "passthrough":
            return
        if not spanish.strip() or not english.strip():
            return

        metadata = (
            getattr(self, "_pending_segment_metadata", {}).get(segment_id)
            or getattr(self, "_segment_metadata_cache", {}).get(segment_id)
            or {}
        )
        if metadata.get("pending_completion"):
            return

        verse_detected = (
            getattr(self, "_pending_detected_verses", {}).get(segment_id)
            or getattr(self, "_detected_verse_cache", {}).get(segment_id)
        )

        effective_interim_hint = (
            interim_english_hint
            or self._ensure_segment_alignment_hint_cache().get(segment_id, "")
        )
        await self._broadcast_pipeline_trace(
            stage="alignment.request",
            summary=f"phrase alignment requested ({interim_hint_reason or ('accepted' if effective_interim_hint else 'none')})",
            segment_id=segment_id,
            trace_kind="decision",
            data={
                "source": source,
                "source_quality": str(metadata.get('source_quality') or 'clean'),
                "translation_register": str(metadata.get('translation_register') or 'expository'),
                "discourse_tag": str(metadata.get('discourse_tag') or 'statement'),
                "interim_hint_enabled": _interim_alignment_hints_enabled(),
                "interim_hint_reason": interim_hint_reason or ("accepted" if effective_interim_hint else "none"),
                "interim_hint_used": bool(effective_interim_hint),
                "interim_hint_text": effective_interim_hint,
                "prior_phrase_alignment_count": len(prior_phrase_alignment or []),
            },
        )

        enrichment.request_phrase_alignment(
            ts=segment_id,
            spanish=spanish,
            english=english,
            google_english=google_english or english,
            interim_english_hint=effective_interim_hint,
            source_quality=str(metadata.get("source_quality") or "clean"),
            translation_register=str(metadata.get("translation_register") or "expository"),
            discourse_tag=str(metadata.get("discourse_tag") or "statement"),
            verse_detected=verse_detected if isinstance(verse_detected, dict) else None,
            prior_phrase_alignment=prior_phrase_alignment or None,
        )

    async def _flush_buffered_segment_state(self, segment_id: int) -> None:
        metadata = self._pending_segment_metadata.pop(segment_id, None)
        if metadata is not None:
            await self._broadcast({
                "type": "segment_metadata",
                **metadata,
                **self._segment_ref(segment_id),
            })
        verse = self._pending_detected_verses.pop(segment_id, None)
        if verse is not None:
            await self._broadcast({"type": "verse_detected", "verse": verse, **self._segment_ref(segment_id)})
        suggestions = self._pending_suggested_verses.pop(segment_id, None)
        if suggestions is not None:
            await self._broadcast({"type": "verse_suggestion", "suggestions": suggestions, **self._segment_ref(segment_id)})

    async def _flush_all_pending_commits(self) -> None:
        for segment_id in list(self._pending_feed_commits.keys()):
            await self._commit_pending_segment(segment_id)

    async def _broadcast_live_translation(
        self,
        text: str,
        source: str,
        display_ready: bool,
        live_ts: int | None = None,
        segment_id: int | None = None,
        merge_strategy: str = "append",
    ) -> None:
        payload = {
            "type": "live_translation",
            "text": text,
            "source": source,
            "display_ready": display_ready,
            "merge_strategy": merge_strategy,
        }
        if segment_id is not None:
            payload.update(self._segment_ref(segment_id))
        else:
            payload["ts"] = live_ts if live_ts is not None else _now()
        await self._broadcast(payload)

    async def _broadcast_live_translation_clear(
        self,
        reason: str,
        segment_id: int | None = None,
    ) -> None:
        payload = {
            "type": "live_translation_clear",
            "reason": reason,
        }
        if segment_id is not None:
            payload.update(self._segment_ref(segment_id))
        else:
            payload["ts"] = _now()
        await self._broadcast(payload)

    async def _broadcast_feed_commit(
        self,
        segment_id: int,
        spanish: str,
        english: str,
        source: str,
        phrase_alignment: list[dict] | None,
        stt_context: dict | None = None,
    ) -> None:
        stt_context = dict(stt_context or {})
        if phrase_alignment:
            payload_alignment = self._build_alignment_payload(
                segment_id=segment_id,
                english=english,
                spanish=spanish,
                phrase_alignment=phrase_alignment,
            )
            emitted_alignment = payload_alignment.get("phrase_alignment", [])
            alignment_version = int(payload_alignment.get("alignment_version") or 0)
            previous_alignment_version = payload_alignment.get("previous_alignment_version")
            root_segment_id = int(payload_alignment.get("root_segment_id") or segment_id)
            merged_from_segment_ids = list(payload_alignment.get("merged_from_segment_ids") or [segment_id])
            self._segment_alignment_signature[segment_id] = self._build_alignment_signature(
                english,
                emitted_alignment,
            )
        else:
            emitted_alignment = None
            alignment_version = None
            previous_alignment_version = None
            root_segment_id = self._segment_root_id(segment_id)
            merged_from_segment_ids = self._segment_merge_lineage(segment_id)
            self._set_segment_lineage(
                segment_id,
                root_segment_id=root_segment_id,
                merged_from_segment_ids=merged_from_segment_ids,
            )
        self._segment_text_cache[segment_id] = {
            "spanish": spanish,
            "english": english,
        }
        self._ensure_segment_stt_cache()[segment_id] = stt_context
        payload = {
            "type": "feed_commit",
            "spanish": spanish,
            "english": english,
            "source": source,
            **stt_context,
            **self._segment_ref(segment_id),
        }
        payload["root_segment_id"] = root_segment_id
        payload["merged_from_segment_ids"] = merged_from_segment_ids
        if emitted_alignment:
            payload["phrase_alignment"] = emitted_alignment
            payload["alignment_version"] = alignment_version
            payload["previous_alignment_version"] = previous_alignment_version
        await self._broadcast_pipeline_trace(
            stage="display.feed_commit",
            summary="feed commit broadcast",
            segment_id=segment_id,
            trace_kind="emit",
            data={
                "spanish": spanish,
                "english": english,
                "source": source,
                "phrase_alignment": emitted_alignment,
                "alignment_version": alignment_version,
                "previous_alignment_version": previous_alignment_version,
                "root_segment_id": root_segment_id,
                "merged_from_segment_ids": merged_from_segment_ids,
                "stt_context": stt_context,
            },
        )
        await self._broadcast(payload)

    async def _broadcast_feed_revision(
        self,
        segment_id: int,
        english: str,
        source: str,
        reason: str,
        spanish: str | None = None,
        phrase_alignment: list[dict] | None = None,
        alignment_version: int | None = None,
        previous_alignment_version: int | None = None,
        root_segment_id: int | None = None,
        merged_from_segment_ids: list[int] | None = None,
    ) -> None:
        # Update the segment text cache eagerly so other producers (e.g. the
        # phrase_alignment handler reading `_segment_text_cache.get(ts)`) see
        # the latest English even while the broadcast is debounced.
        cached = self._segment_text_cache.get(segment_id, {})
        self._segment_text_cache[segment_id] = {
            "spanish": spanish if spanish is not None else cached.get("spanish", ""),
            "english": english,
        }
        self._enqueue_feed_revision(
            segment_id=segment_id,
            english=english,
            source=source,
            reason=reason,
            spanish=spanish,
            phrase_alignment=phrase_alignment,
            alignment_version=alignment_version,
            previous_alignment_version=previous_alignment_version,
            root_segment_id=root_segment_id,
            merged_from_segment_ids=merged_from_segment_ids,
        )

    def _enqueue_feed_revision(
        self,
        *,
        segment_id: int,
        english: str,
        source: str,
        reason: str,
        spanish: str | None,
        phrase_alignment: list[dict] | None,
        alignment_version: int | None = None,
        previous_alignment_version: int | None = None,
        root_segment_id: int | None = None,
        merged_from_segment_ids: list[int] | None = None,
    ) -> None:
        pending_map = getattr(self, "_pending_feed_revisions", None)
        if pending_map is None:
            pending_map = {}
            self._pending_feed_revisions = pending_map
        timer_map = getattr(self, "_feed_revision_timers", None)
        if timer_map is None:
            timer_map = {}
            self._feed_revision_timers = timer_map

        existing = pending_map.get(segment_id)
        if existing is None:
            pending_map[segment_id] = {
                "segment_id": segment_id,
                "english": english,
                "source": source,
                "reason": reason,
                "spanish": spanish,
                "phrase_alignment": phrase_alignment,
                "alignment_version": alignment_version,
                "previous_alignment_version": previous_alignment_version,
                "root_segment_id": root_segment_id,
                "merged_from_segment_ids": merged_from_segment_ids,
            }
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop (e.g. unit-test harness invoking the
                # producer outside an event loop). Emit synchronously.
                pending_map.pop(segment_id, None)
                asyncio.run(
                    self._emit_feed_revision_now(
                        segment_id=segment_id,
                        english=english,
                        source=source,
                        reason=reason,
                        spanish=spanish,
                        phrase_alignment=phrase_alignment,
                        alignment_version=alignment_version,
                        previous_alignment_version=previous_alignment_version,
                        root_segment_id=root_segment_id,
                        merged_from_segment_ids=merged_from_segment_ids,
                    )
                )
                return
            timer_map[segment_id] = loop.call_later(
                FEED_REVISION_DEBOUNCE_S,
                self._schedule_feed_revision_flush,
                segment_id,
            )
            return

        # Coalesce with the existing pending payload.
        existing_alignment = existing.get("phrase_alignment")
        existing["english"] = english
        existing["source"] = source
        existing["reason"] = _select_higher_priority_feed_revision_reason(
            existing["reason"], reason
        )
        if spanish is not None:
            existing["spanish"] = spanish
        # Keep an alignment that was already attached when a follow-up
        # segmentation_repair / context_repair drops by without one.
        if phrase_alignment:
            existing["phrase_alignment"] = phrase_alignment
            existing["alignment_version"] = alignment_version
            existing["previous_alignment_version"] = previous_alignment_version
            existing["root_segment_id"] = root_segment_id
            existing["merged_from_segment_ids"] = merged_from_segment_ids
        elif existing_alignment is not None:
            existing["phrase_alignment"] = existing_alignment
        if existing.get("root_segment_id") is None and root_segment_id is not None:
            existing["root_segment_id"] = root_segment_id
        if existing.get("merged_from_segment_ids") is None and merged_from_segment_ids is not None:
            existing["merged_from_segment_ids"] = merged_from_segment_ids
        metrics = getattr(self, "_feed_revision_metrics", None)
        if metrics is not None:
            metrics["coalesced_count"] = metrics.get("coalesced_count", 0) + 1

    def _schedule_feed_revision_flush(self, segment_id: int) -> None:
        try:
            asyncio.get_running_loop().create_task(
                self._flush_pending_feed_revision(segment_id)
            )
        except RuntimeError:
            # Loop already torn down — let `close()` flush via the sync path.
            return

    async def _flush_pending_feed_revision(self, segment_id: int) -> None:
        timer_map = getattr(self, "_feed_revision_timers", None)
        if timer_map is not None:
            timer = timer_map.pop(segment_id, None)
            if timer is not None:
                timer.cancel()
        pending_map = getattr(self, "_pending_feed_revisions", None)
        if pending_map is None:
            return
        pending = pending_map.pop(segment_id, None)
        if pending is None:
            return
        await self._emit_feed_revision_now(**pending)

    async def _flush_all_pending_feed_revisions(self) -> None:
        pending_map = getattr(self, "_pending_feed_revisions", None)
        if not pending_map:
            return
        for segment_id in list(pending_map.keys()):
            await self._flush_pending_feed_revision(segment_id)

    def _discard_pending_feed_revision(self, segment_id: int) -> None:
        timer_map = getattr(self, "_feed_revision_timers", None)
        if timer_map is not None:
            timer = timer_map.pop(segment_id, None)
            if timer is not None:
                timer.cancel()
        pending_map = getattr(self, "_pending_feed_revisions", None)
        if pending_map is not None:
            pending_map.pop(segment_id, None)

    async def _emit_feed_revision_now(
        self,
        *,
        segment_id: int,
        english: str,
        source: str,
        reason: str,
        spanish: str | None = None,
        phrase_alignment: list[dict] | None = None,
        alignment_version: int | None = None,
        previous_alignment_version: int | None = None,
        root_segment_id: int | None = None,
        merged_from_segment_ids: list[int] | None = None,
    ) -> None:
        stt_context = self._ensure_segment_stt_cache().get(segment_id, {})
        payload = {
            "type": "feed_revision",
            "english": english,
            "source": source,
            "reason": reason,
            "root_segment_id": root_segment_id if root_segment_id is not None else self._segment_root_id(segment_id),
            "merged_from_segment_ids": (
                merged_from_segment_ids if merged_from_segment_ids is not None else self._segment_merge_lineage(segment_id)
            ),
            **stt_context,
            **self._segment_ref(segment_id),
        }
        if spanish is not None:
            payload["spanish"] = spanish
        if phrase_alignment:
            payload["phrase_alignment"] = phrase_alignment
            payload["alignment_version"] = alignment_version
            payload["previous_alignment_version"] = previous_alignment_version
        self._bump_feed_revision_metric(reason)
        await self._broadcast_pipeline_trace(
            stage="display.feed_revision",
            summary=f"feed revision broadcast: {reason}",
            segment_id=segment_id,
            trace_kind="emit",
            data={
                "english": english,
                "spanish": spanish,
                "source": source,
                "reason": reason,
                "phrase_alignment": phrase_alignment,
                "alignment_version": alignment_version,
                "previous_alignment_version": previous_alignment_version,
                "root_segment_id": payload["root_segment_id"],
                "merged_from_segment_ids": payload["merged_from_segment_ids"],
                "stt_context": stt_context,
            },
        )
        await self._broadcast(payload)

    def _bump_feed_revision_metric(self, reason: str) -> None:
        metrics = getattr(self, "_feed_revision_metrics", None)
        if metrics is None:
            return
        metrics["emitted_total"] = metrics.get("emitted_total", 0) + 1
        key = f"emitted_{reason}"
        if key in metrics:
            metrics[key] = metrics.get(key, 0) + 1
        else:
            metrics["emitted_other"] = metrics.get("emitted_other", 0) + 1

    async def _send(self, msg: dict):
        try:
            await self._ws.send_json(msg)
        except Exception:
            pass

    def _capture_enabled_for_session(self) -> bool:
        if self._benchmark_capture and self._benchmark_capture.enabled is not None:
            return bool(self._benchmark_capture.enabled)
        return _session_capture_enabled()

    def _next_segment_id(self) -> int:
        now = _now()
        if now <= self._last_segment_id:
            now = self._last_segment_id + 1
        self._last_segment_id = now
        return now

    def _segment_ref(self, segment_id: int) -> dict:
        """Emit canonical segment identity alongside the legacy timestamp field."""
        return {
            "segment_id": segment_id,
            "ts": segment_id,
        }

    def _merge_ref(self, keep_segment_id: int, absorb_segment_id: int) -> dict:
        """Emit merge compatibility fields while preserving canonical IDs."""
        return {
            "segment_id_keep": keep_segment_id,
            "segment_id_absorb": absorb_segment_id,
            "ts_keep": keep_segment_id,
            "ts_absorb": absorb_segment_id,
        }


async def _finalize_capture_in_db(
    result: CaptureResult,
    session_id: int | None,
    benchmark_capture: BenchmarkCaptureMetadata | None = None,
) -> None:
    """Persist capture file paths and metrics to the session_captures table."""
    if not session_id:
        return
    from server.db.sessions import create_capture_record, finalize_capture
    try:
        capture_id = await create_capture_record(session_id)
        await finalize_capture(
            capture_id,
            audio_path=result.audio_path or "",
            events_path=result.events_path or "",
            metadata_path=result.metadata_path or "",
            duration_s=result.duration_s,
            segment_count=result.segment_count,
            benchmark_session_id=benchmark_capture.benchmark_session_id if benchmark_capture else "",
            benchmark_run_id=benchmark_capture.benchmark_run_id if benchmark_capture else "",
            benchmark_scenario_id=benchmark_capture.benchmark_scenario_id if benchmark_capture else "",
            benchmark_pipeline_id=benchmark_capture.benchmark_pipeline_id if benchmark_capture else "",
            benchmark_capture_label=benchmark_capture.benchmark_capture_label if benchmark_capture else "",
        )
    except Exception as e:
        logger.warning("[session] DB capture finalize failed: %s", e)


class SessionManager:
    """Tracks one active ServiceSession per church_id."""

    def __init__(self, broadcaster: Broadcaster):
        self._broadcaster = broadcaster
        self._sessions: dict[str, ServiceSession] = {}
        self._recent_stats: dict[str, dict] = {}

    async def create(
        self,
        church_id: str,
        ws: WebSocket,
        sample_rate: int,
        sermon_topic: str = "",
        source_scripture_version: str = "rvr1960",
        display_scripture_version: str = "kjv",
        stt_config: STTConfig | None = None,
        benchmark_capture: dict | None = None,
    ) -> ServiceSession:
        if church_id in self._sessions:
            prior = self._sessions[church_id]
            self._recent_stats[church_id] = prior.get_stats()
            await prior.close()

        session = ServiceSession(church_id, ws, self._broadcaster)
        self._sessions[church_id] = session
        await session.start(
            sample_rate,
            sermon_topic=sermon_topic,
            source_scripture_version=source_scripture_version,
            display_scripture_version=display_scripture_version,
            stt_config=stt_config,
            benchmark_capture=BenchmarkCaptureMetadata.from_payload(benchmark_capture),
        )
        return session

    async def remove(self, church_id: str):
        session = self._sessions.pop(church_id, None)
        if session:
            self._recent_stats[church_id] = session.get_stats()
            await session.close()

    def get(self, church_id: str) -> ServiceSession | None:
        return self._sessions.get(church_id)

    def get_last_stats(self, church_id: str) -> dict:
        return dict(self._recent_stats.get(church_id, {}))


def _now() -> int:
    return int(time.time() * 1000)
