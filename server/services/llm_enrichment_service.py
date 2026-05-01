import asyncio
import json
import logging
import os
import re
from collections import deque
from dataclasses import dataclass
from typing import Callable, Awaitable, TYPE_CHECKING

import anthropic

from server.db.verses import save_verse_detection, save_verse_suggestions
from server.services.translation_deviation import translation_deviation_score

if TYPE_CHECKING:
    from server.services.topic_tracker import TopicTracker
    from server.services.sermon_state_tracker import SermonStateTracker

logger = logging.getLogger(__name__)

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
MAX_ENRICHMENT_TOKENS = 1100
DEFERRED_RELEASE_S = 6.0    # seconds to wait before releasing a suppressed translation if no merge
_VALID_SERMON_MODES = frozenset({
    "scripture", "exposition", "illustration", "application", "exhortation", "procedural"
})
VERSE_GAP_THRESHOLD_S = float(os.getenv("VERSE_GAP_THRESHOLD_S", "45"))


@dataclass
class VerseScratchEntry:
    book: str
    chapter: int
    verse_start: int
    verse_end: int | None
    confidence: str           # "explicit" | "quoted"
    reference: str
    canonical_english: str
    spanish_text: str
    audio_start: float        # STT stream seconds (sermon-relative, reconnect-normalized)
    audio_end: float
    ts: int                   # wall-clock ms for client broadcast keying


_SYSTEM_PROMPT_BASE = """\
You are a bilingual (Spanish/English) theological assistant helping a live church sermon translation system.

{glossary_block}

Your job is to analyze one sentence from a live Spanish sermon and return a JSON object with the fields below.

RULES:
1. Output ONLY valid JSON. No prose, no markdown fences, no code blocks.

2. improved_translation: provide a better English rendering than the Google Translate output if needed.
   Preserve the preaching register — declarative, present tense, active voice where natural.
   If Google's translation is already excellent, return it unchanged.
   Prefer idiomatic English over literal calques for sermon expressions when meaning is clear.
   Example: "por el vil metal" should be rendered as "for money" (or equivalent natural wording),
   not "for the vile metal".
   For anecdotal/narrative spans, resolve disfluencies into coherent spoken English:
   - remove accidental immediate repetitions ("yo yo", "ya que ya que")
   - preserve meaning and emphasis without duplicating filler words
   - keep speaker references clear ("él", "usted") and avoid role confusion.
   For rhetorical question/answer sequences, keep explicit Q/A structure in English:
   - keep questions as clear direct questions
   - when current line answers the prior rhetorical question, make the answer concise
   - avoid blending the answer into unrelated trailing clauses.
   Existential / vague fillers: phrases like "tiene que haber" ("there has to be one/someone")
   assert existence without naming the referent. Do not substitute a concrete noun
   ("a connection", "a reason") unless the Spanish names it — keep the same vagueness.
   Common Spanish Pentecostal sermon interjections ("Santo", "Aleluya", "Gloria", "Amén") that appear
   mid-sentence and disrupt grammatical flow should be silently removed from the translation — they are
   STT artifacts of the preaching register, not content words.
   When [PREVIOUS SENTENCE DISCOURSE] shows thought_complete: false, use that as context to interpret
   the current sentence correctly. Do NOT include prior sentence content in improved_translation
   unless merge_with_previous is true and [PREVIOUS SENTENCE — PENDING MERGE] is provided.
   When [PREVIOUS SENTENCE DISCOURSE] shows discourse_tag: "rhetorical_question", the current sentence
   is likely the answer. Keep improved_translation crisp and direct — it answers the previous question.
   When [PREVIOUS SENTENCE DISCOURSE] shows introduces_quote: true, the current sentence is quoted
   scripture. Use formal, present-tense, verbatim-cadence register in the translation.

   SCRIPTURE FIDELITY: When translation_register is "scripture" or discourse_tag is "scripture_quote",
   minimize stylistic rewriting. Prioritize accuracy over polish. Preserve the cadence and phrasing
   of the original. Do NOT paraphrase, expand, or modernize. A slightly wooden but faithful rendering
   is preferable to a smooth but approximate one.

   CONDITIONAL CLAUSE INTEGRITY: When the source contains "Si..." (if...) constructions, ensure the
   English conditional is structurally complete — the "if" clause must have a matching consequence.
   If the source only contains the protasis (the "if" part) with no apodosis (the consequence),
   set thought_complete: false and continuation_required: true. Do NOT fabricate a consequence.

   SCRIPTURE SPEAKER ATTRIBUTION: When introducing a scripture quote, translate speaker introductions
   naturally: "Juan dice" → "John says", never "Pentecostal John" or "John comes and says".
   Any word that is clearly a STT noise prefix before a speaker name should be silently dropped.

   LONG SENTENCE HANDLING: When [LONG SENTENCE] is flagged, prioritize structural accuracy over
   polish. Preserve all clause relationships. Do not truncate or summarize.

3. discourse_tag: classify the rhetorical function of this sentence. Choose exactly one:
   - "statement":          a declarative theological claim or exposition ("God is light")
   - "rhetorical_question": a question the preacher asks but answers themselves ("Who is he?")
   - "answer_to_question":  a direct answer to the preacher's own question ("Jesus Christ.")
   - "quote_introduction":  the preacher is about to quote scripture ("Juan dice...", "dice aquí...",
                             "la Biblia dice...", "como dice en...", "versículo dice...", "leemos que...")
   - "scripture_quote":     the preacher is reciting or reading Bible text verbatim
   - "transition":          moving between topics, passages, or sermon sections
   - "exhortation_appeal":  direct appeal to the congregation ("Come to him", "Do not be afraid")

4. introduces_quote: true if this sentence contains a quote introduction marker such as:
   "Juan dice", "Pedro dice", "Pablo dice", "dice aquí", "dice la Biblia", "la Biblia dice",
   "como dice en", "versículo dice", "leemos que", "escrito está", "está escrito".
   false otherwise.

5. thought_complete: true if this sentence expresses a complete thought on its own.
   false if it ends mid-clause — a preposition, subordinating conjunction, or relative pronoun
   with no resolution (e.g. "Porque anoche se acuerdan que él", "Si nosotros decimos que").

6. verse_detected: detect a Bible reference if the speaker explicitly announces or clearly quotes one
   in any of these forms:
   - Chapter + verse:   "Juan 3:16", "Romanos 8, versículo 28", "Apocalipsis capítulo 1 versículo 5"
   - Chapter-only:      "primera de Juan, capítulo 1", "Salmos capítulo 22", "vamos a Mateo capítulo cinco"
   - Ordinal book ref:  "primera de Juan dice...", "en segunda de Corintios capítulo 5"
   Spanish ordinal book names →
     primera/segunda/tercera de Juan = 1/2/3 John
     primera/segunda de Pedro = 1/2 Peter
     primera/segunda de Corintios = 1/2 Corinthians
     primera/segunda de Tesalonicenses = 1/2 Thessalonians
     primera/segunda de Timoteo = 1/2 Timothy
     primera/segunda de Samuel = 1/2 Samuel
     primera/segunda de Reyes = 1/2 Kings
     Apocalipsis = Revelation; Efesios = Ephesians; Filipenses = Philippians
     Gálatas = Galatians; Colosenses = Colossians; Filemón = Philemon
   For chapter-only citations: set verse_start=1, verse_end=null, reference="1 John 1", confidence="explicit".
   For clearly quoted verse text: confidence="quoted".
   IMPORTANT: Do NOT infer a verse citation from standalone theological vocabulary words
   ("Pentecostés", "bautismo", "adoración", "gracia") even if they reference a biblical event or holiday.
   A valid citation requires an explicit book + chapter/verse announcement, or a clear quotation of
   specific verse text. The word "Pentecostés" alone is never a citation of Acts 2.
   Never hallucinate — if uncertain return null.

8. sermon_mode: classify this sentence into exactly one of these modes:
   - "scripture":    pastor is directly reading or reciting Bible text verbatim
   - "exposition":   explaining, commenting on, or unpacking a biblical passage
   - "illustration": personal story, anecdote, analogy, or parable — even if theological
                     vocabulary is present. Use this whenever the speaker shifts to past-tense personal narrative.
   - "application":  applying the text to the congregation's situation or behavior
   - "exhortation":  emotional appeal, motivational call, altar invitation
   - "procedural":   logistics, worship direction, prayer cues (e.g. "please stand", "let us pray")

9. Use English book names in all references (e.g. "John", "Romans", "Revelation").
10. Infer chapter/verse from quoted text only when highly confident.
    When matching quoted Spanish verse text, use the Reina-Valera 1960 (RVR1960) as the
    primary reference to identify the correct book and verse before rendering canonical_english.

11. continuation_required: true if the speaker's thought clearly requires more text to complete —
    stronger and more forward-looking than thought_complete.
    true: sentence ends mid-argument, introduces a list without completing it, ends with a
    conjunction or subject pronoun that sets up a predicate not yet delivered
    (e.g. "Porque anoche se acuerdan que él", "Si nosotros decimos que", "Y la razón es").
    false: sentence is a complete unit, even if brief.

12. merge_with_previous: true ONLY when the caption stream was segmented incorrectly and the
    current sentence must be merged with the immediately preceding sentence to repair that split.
    This is a segmentation-repair signal, not a normal discourse or stylistic signal.
    Use true only when the previous and current sentences were split at the wrong boundary and would
    mislead the user if shown as separate captions.
    Strong positive cases:
    - previous [PREVIOUS SENTENCE DISCOURSE] had source_quality: "fragmented" AND this sentence
      grammatically continues it
    - this sentence's source_quality is "fragmented" AND it clearly attaches to the previous
    - this sentence grammatically completes a dangling predicate or unfinished clause from the previous
      sentence, regardless of whether the previous call set continuation_required: true
    - previous [PREVIOUS SENTENCE DISCOURSE] had display_ready: false AND this sentence clearly
      resolves the bad split rather than merely continuing the sermon naturally
    - the current sentence is a very short fragment that only exists because the stream split a single
      utterance incorrectly
    Negative cases — set false:
    - rhetorical question followed by its answer, if both are acceptable standalone captions
    - quote introduction followed by scripture quote, if both are acceptable standalone captions
    - any case where separate stable segments plus in-place revision are acceptable
    - any case where merge would be stylistic preference rather than segmentation repair
    All other cases → false.
    When true, you MUST write improved_translation as a fluent English rendering of the COMPLETE
    repaired unit — the [PREVIOUS SENTENCE — PENDING MERGE] text PLUS the current sentence,
    treated as one utterance. Do not translate only the current sentence.

13. paragraph_break: true if this sentence opens a new major section, topic shift, or
    rhetorical phase. Triggers a visual separator on the display.
    true: shift from exposition to illustration, new scripture passage announced,
    transition from teaching to altar call, return from illustration to main point.
    false: continues the current rhetorical thread.

14. source_quality: assess the apparent quality of the input Spanish text:
    "clean":      normal sermon speech, no obvious STT artifacts
    "noisy":      contains apparent repetitions, garbled tokens, or incomplete phonemes
    "fragmented": clearly an incomplete utterance, a mid-word cut, or structurally broken

15. translation_register: the rendering register appropriate for this sentence:
    "scripture":   verbatim Bible text — formal, present tense, liturgical cadence
    "expository":  teaching or explanation — clear, accessible, present tense
    "narrative":   personal story or illustration — past tense, conversational
    "exhortation": direct appeal to the congregation — imperative, energetic

16. display_ready: the authoritative emission control signal for this sentence.
    Set to false when ANY of the following apply:
    - thought_complete is false
    - continuation_required is true
    - source_quality is "fragmented"
    - discourse_tag is "quote_introduction" (the quoted content has not arrived yet)
    Set to true only when ALL of the above are absent.
    When false, the translation is suppressed until a merge arrives or a fallback timeout fires.
    Be accurate — this drives whether the caption appears on screen.

JSON schema (return exactly this shape):
{
  "improved_translation": "string",
  "discourse_tag": "statement" | "rhetorical_question" | "answer_to_question" | "quote_introduction" | "scripture_quote" | "transition" | "exhortation_appeal",
  "introduces_quote": true | false,
  "thought_complete": true | false,
  "continuation_required": true | false,
  "merge_with_previous": true | false,
  "paragraph_break": true | false,
  "source_quality": "clean" | "noisy" | "fragmented",
  "translation_register": "scripture" | "expository" | "narrative" | "exhortation",
  "sermon_mode": "scripture" | "exposition" | "illustration" | "application" | "exhortation" | "procedural",
  "display_ready": true | false,
  "verse_detected": {
    "book": "string",
    "chapter": integer,
    "verse_start": integer,
    "verse_end": integer | null,
    "spanish_text": "string",
    "canonical_english": "string",
    "reference": "string",
    "confidence": "explicit" | "quoted"
  } | null
}\
"""


_VERSE_SUGGESTIONS_SYSTEM = """\
You are a biblical reference assistant for a live Spanish sermon translation system.
Given a sentence from a sermon, suggest 1-3 specifically relevant Bible verses for the congregation.

STRICT RULES:
- Return ONLY valid JSON: {"suggestions": [...]}. No prose, no markdown fences, no code blocks.
- Return {"suggestions": []} for: procedural, transitional, logistical, or rhetorical filler sentences.
- Return {"suggestions": []} for sentences with source_quality "fragmented" or "noisy".
- Return {"suggestions": []} when the theological content is generic or no cross-reference adds real value.
- NEVER suggest a verse in [ALREADY SUGGESTED] or matching [ACTIVE PASSAGE].
- Prefer thematic cross-references over repeating the same book being expounded.
- Each suggestion must be a SPECIFIC verse or short range, not a chapter or book.
- Only suggest when you are confident the verse meaningfully illuminates THIS sentence.
- Use NIV text for canonical_english.
- When identifying quoted Spanish verse text, compare against the Reina-Valera 1960 (RVR1960) for accurate book/verse matching before rendering the NIV canonical_english.

JSON schema: {"suggestions": [{"reference": "string", "canonical_english": "string", "relevance_note": "string"}]}
"""


# ---------------------------------------------------------------------------
# Translation normalization — applied post-LLM to improve natural English phrasing.
# These are domain-specific substitutions that the LLM frequently gets wrong in a
# live sermon context (e.g. "transmit" is technically correct for "transmitir" but
# sounds robotic; "share" is the natural preaching-register equivalent).
# ---------------------------------------------------------------------------
_TRANSLATION_NORMALIZATION: dict[str, str] = {
    'transmit': 'share',
    'transmits': 'shares',
    'transmitting': 'sharing',
    'transmitted': 'shared',
    'transmission': 'sharing',
}
_TRANSLATION_NORMALIZATION_RE = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in _TRANSLATION_NORMALIZATION) + r')\b',
    re.IGNORECASE,
)


def _scripture_speaker_normalization(text: str) -> str:
    """Fix awkward scripture-speaker constructions from STT artifact translations.

    Handles patterns produced when STT noise ("Pentecostés") precedes a speaker
    name, causing Google to generate constructions like "Pentecostal John comes
    and says" instead of "John says".

    Rules:
    - "[Name] comes and says" → "[Name] says"
    - "Pentecostal [Name] [verb]" → "[Name] [verb]"  (misplaced Pentecostal prefix)
    - "[Name] comes to say" → "[Name] says"
    """
    # "John comes and says" / "Peter comes and says" → "John says"
    text = re.sub(
        r'\b(\w+)\s+comes\s+and\s+says\b',
        r'\1 says',
        text, flags=re.IGNORECASE,
    )
    # "Pentecostal John" when Pentecostal is clearly a misplaced prefix before a name+verb
    text = re.sub(
        r'\bPentecostal\s+(?=[A-Z][a-z]+\s+(?:says|writes|declares|states|tells|warns|teaches)\b)',
        '',
        text, flags=re.IGNORECASE,
    )
    # "[Name] comes to say" → "[Name] says"
    text = re.sub(
        r'\b(\w+)\s+comes\s+to\s+say\b',
        r'\1 says',
        text, flags=re.IGNORECASE,
    )
    return text


def _sermon_register_normalization(text: str) -> str:
    """Apply narrow sermon-register phrase normalizations.

    Keep this intentionally small and exact-match-biased so we do not
    accidentally rewrite ordinary prose into churchy English.
    """
    stripped = text.strip()
    if re.fullmatch(r"(?:That|This|It) is the text\.", stripped):
        subject = stripped.split()[0]
        return f"{subject} is the passage."
    return text


def _preserve_reference_appositives(text: str, reference_english: str) -> str:
    """Restore explicit appositive clarifications already present in the baseline.

    Example:
      reference: "as he, Jesus, is in the light"
      candidate: "as Jesus is in the light"
      result:    "as he, Jesus, is in the light"

    This is intentionally conservative: it only fires when the reference already
    carries a pronoun+name appositive and the candidate keeps the same name in
    the same local verb phrase.
    """
    pattern = re.compile(
        r"\b(he|she|they),\s+([A-Z][a-z]+),\s+(is|are|was|were|says|said|did|does|has|have)\b"
    )
    for match in pattern.finditer(reference_english):
        pronoun, name, verb = match.groups()
        candidate_pattern = re.compile(
            rf"\b{name}\s+{verb}\b"
        )
        candidate_match = candidate_pattern.search(text)
        if not candidate_match:
            continue
        prefix = text[max(0, candidate_match.start() - 6):candidate_match.start()]
        if "," in prefix or pronoun.lower() in prefix.lower():
            continue
        replacement = f"{pronoun}, {name}, {verb}"
        text = text[:candidate_match.start()] + replacement + text[candidate_match.end():]
        break
    return text


def _preserve_reference_speaker_intro(text: str, reference_english: str) -> str:
    """Preserve explicit biblical-speaker framing already present in the reference.

    This only restores a narrow family of source-anchored intros like
    "John says" / "Paul writes" when the candidate otherwise drops the speaker
    and launches directly into the quoted content.
    """
    normalized_reference = _scripture_speaker_normalization(reference_english)
    match = re.search(
        r"\b(John|Peter|Paul|David|Moses|Jesus)\s+(says|writes|declares|states|teaches|warns)\b",
        normalized_reference,
    )
    if not match:
        return text

    name, verb = match.groups()
    if re.search(rf"\b{name}\b", text):
        return text

    stripped = text.lstrip("“\"' ")
    if not re.match(r"^(If|We|I|He|She|They|God|The)\b", stripped):
        return text

    return f"{name} {verb}, {text[0].lower() + text[1:]}" if text else f"{name} {verb}"


def _if_clause_validator(text: str) -> str:
    """Detect and flag logically broken conditional constructions.

    Currently logs a warning for audit but does not auto-correct, since
    auto-correction would risk silently losing theological content. The
    merge_with_previous LLM logic is the segmentation-repair path.

    Detects: "If we say that we have [X], I am [Y]."
    where the subject shifts between protasis and apodosis — a sign that
    two independent fragments were incorrectly merged at the buffer level.
    """
    # Subject-shift pattern: "If [pronoun1] ..., [pronoun2] ..."
    # where pronoun1 ≠ pronoun2 (e.g. "we" → "I")
    m = re.match(
        r'^If\s+(\w+)\b.+?,\s*(I|you|he|she|they|we)\b',
        text, re.IGNORECASE,
    )
    if m:
        subj1 = m.group(1).lower()
        subj2 = m.group(2).lower()
        if subj1 != subj2 and subj1 not in ('we', subj2):
            logger.debug(
                "[translation] Possible subject-shift conditional: subject '%s' → '%s' in: %s",
                subj1, subj2, text[:80],
            )
    return text  # no auto-correction — LLM handles segmentation repair when needed


def _normalize_translation(text: str, reference_english: str = "") -> str:
    """Apply domain normalization to an English translation for natural sermon register."""
    def _replace(m: re.Match) -> str:
        word = m.group(0)
        replacement = _TRANSLATION_NORMALIZATION[word.lower()]
        return replacement[0].upper() + replacement[1:] if word[0].isupper() else replacement
    text = _TRANSLATION_NORMALIZATION_RE.sub(_replace, text)
    text = _sermon_register_normalization(text)
    text = _scripture_speaker_normalization(text)
    if reference_english:
        text = _preserve_reference_appositives(text, reference_english)
        text = _preserve_reference_speaker_intro(text, reference_english)
    text = _if_clause_validator(text)
    return text


def _translation_deviation_score(google: str, improved: str) -> float:
    """Word-level Jaccard similarity between two translations.

    Returns 0.0 (completely different) to 1.0 (identical).
    Used to detect when LLM reconstruction diverges too far from the Google
    baseline for noisy source text, flagging a reconstruction risk.
    """
    return translation_deviation_score(google, improved)


# Threshold below which an improved translation is considered to diverge
# significantly from the Google baseline when source_quality is "noisy".
_RECONSTRUCTION_RISK_THRESHOLD = 0.35
_COMPLETENESS_MIN_LENGTH_RATIO = 0.55
_COMPLETENESS_MIN_COVERAGE_RATIO = 0.45
_COMPLETENESS_MIN_GOOGLE_WORDS = 8
_COMPLETENESS_LONG_SENTENCE_WORDS = 14
_COMPLETENESS_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for",
    "from", "he", "her", "his", "i", "if", "in", "into", "is", "it",
    "me", "my", "of", "on", "or", "our", "she", "so", "that", "the",
    "their", "them", "there", "they", "this", "to", "us", "was", "we",
    "were", "what", "when", "where", "who", "why", "will", "with", "you",
    "your",
})
_ENGLISH_INCOMPLETE_TAIL = re.compile(
    r"\b(?:and|as|because|but|for|from|how|if|in|into|like|of|on|or|that|the|"
    r"this|to|what|when|where|which|who|why|with)\s*$",
    re.IGNORECASE,
)

_REPAIR_TRANSLATION_SYSTEM = """\
You are a bilingual theological translation repair assistant for a live church captioning system.

Return ONLY valid JSON. No prose, no markdown fences, no code blocks.

Goal:
- The first translation candidate looked unsafe or incomplete.
- Produce two repaired English options for the SAME Spanish source:
  1. literal_translation: conservative, clause-complete, faithful to source structure
  2. natural_translation: natural spoken English, but still complete and faithful

Rules:
- Do not omit major clauses, questions, named speakers, or consequences.
- Do not summarize.
- Preserve rhetorical questions as questions.
- For scripture-like material, prefer fidelity over polish.
- If Google is already the safest option, one or both fields may equal the Google translation.

JSON schema:
{
  "literal_translation": "string",
  "natural_translation": "string"
}
"""

_ALIGNMENT_SYSTEM = """\
You are a bilingual phrase-alignment assistant for a live church translation system.

Return ONLY valid JSON. No prose, no markdown fences, no code blocks.

Goal:
- Given the final displayed English sentence and the original Spanish sentence,
  return a short ordered phrase mapping for later reveal UI.
- The displayed English is authoritative. If it is more polished than the Spanish,
  use the Google English baseline and any scripture context only as grounding aids.

Rules:
- Return 1-8 items.
- Prefer 2-6 items for normal sentences; 1 item is allowed only for very short lines.
- Keep phrases short and readable: roughly 1-6 English words per item.
- Preserve order from left to right.
- Cover as much of the displayed English meaning as you reliably can.
- The English side must reuse the displayed English wording, not a literal gloss.
- The Spanish side must be the matching source wording from the spoken sentence.
- Use the Google English baseline as a bridge when the displayed English has been polished.
- If scripture context is provided, use it only to anchor wording that is already present.
- Do not invent Spanish words that are not supported by the source sentence.
- Do not return a whole sentence as one item unless the sentence is extremely short.
- Avoid tiny standalone function-word fragments when they can be grouped naturally.
- If alignment is unclear, noisy, or unreliable, return an empty list.

JSON schema:
{
  "phrase_alignment": [
    {
      "english_text": "string",
      "spanish_text": "string"
    }
  ]
}
"""


def _strip_json_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    return raw


def _parse_json_object(raw: str) -> dict | None:
    raw = _strip_json_fences(raw)
    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        try:
            result = json.loads(match.group(0))
            return result if isinstance(result, dict) else None
        except json.JSONDecodeError:
            return None


def _normalize_alignment_compare(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


def _ordered_alignment_coverage(chunks: list[str], chosen_english: str) -> float:
    normalized_chosen = _normalize_alignment_compare(chosen_english)
    if not normalized_chosen:
        return 0.0
    cursor = 0
    matched = 0
    for chunk in chunks:
        normalized_chunk = _normalize_alignment_compare(chunk)
        if not normalized_chunk:
            continue
        index = normalized_chosen.find(normalized_chunk, cursor)
        if index < 0:
            return 0.0
        matched += len(normalized_chunk)
        cursor = index + len(normalized_chunk)
    return matched / len(normalized_chosen)


def _alignment_allowed(
    *,
    source_quality: str,
    translation_register: str,
    discourse_tag: str,
    verse_detected: dict | None,
) -> bool:
    if source_quality == "clean":
        return True
    if verse_detected:
        return True
    if translation_register == "scripture":
        return True
    if discourse_tag in {"scripture_quote", "quote_introduction"}:
        return True
    return False


def _sanitize_phrase_alignment(raw_alignment: object, chosen_english: str) -> list[dict]:
    if not isinstance(raw_alignment, list):
        return []

    sanitized: list[dict] = []
    for item in raw_alignment:
        if not isinstance(item, dict):
            continue
        english_text = str(item.get("english_text", "")).strip()
        spanish_text = str(item.get("spanish_text", "")).strip()
        if not english_text or not spanish_text:
            continue
        if len(english_text) == 1 and not english_text.isalnum():
            continue
        sanitized.append({
            "english_text": english_text,
            "spanish_text": spanish_text,
        })

    chosen_word_count = len(_translation_word_tokens(chosen_english))
    minimum_items = 1 if chosen_word_count <= 4 else 2
    if len(sanitized) < minimum_items:
        return []

    coverage_ratio = _ordered_alignment_coverage(
        [item["english_text"] for item in sanitized],
        chosen_english,
    )
    if coverage_ratio <= 0:
        return []
    minimum_coverage = 0.35 if chosen_word_count <= 8 else 0.45
    if coverage_ratio < minimum_coverage:
        return []

    return sanitized


def _build_alignment_user_message(spanish: str, english: str) -> str:
    return (
        f"[SOURCE â€” Spanish original]\n{spanish}\n\n"
        f"[DISPLAYED ENGLISH]\n{english}"
    )


def _translation_word_tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", text.lower())


def _translation_keyword_terms(text: str) -> set[str]:
    return {
        token
        for token in _translation_word_tokens(text)
        if len(token) >= 4 and token not in _COMPLETENESS_STOPWORDS
    }


def _sentence_count(text: str) -> int:
    count = len(re.findall(r"[.!?]+", text))
    return count if count > 0 else int(bool(text.strip()))


def _translation_completeness_issues(
    google_english: str,
    candidate: str,
    *,
    translation_register: str,
) -> list[str]:
    issues: list[str] = []
    candidate = candidate.strip()
    if not candidate:
        return ["empty_candidate"]

    google_words = _translation_word_tokens(google_english)
    candidate_words = _translation_word_tokens(candidate)
    if len(google_words) >= _COMPLETENESS_MIN_GOOGLE_WORDS:
        length_ratio = len(candidate_words) / max(len(google_words), 1)
        min_ratio = _COMPLETENESS_MIN_LENGTH_RATIO
        if translation_register == "scripture" or len(google_words) >= _COMPLETENESS_LONG_SENTENCE_WORDS:
            min_ratio = max(min_ratio, 0.65)
        if length_ratio < min_ratio:
            issues.append("length_ratio")

        google_sentences = _sentence_count(google_english)
        candidate_sentences = _sentence_count(candidate)
        if google_sentences >= 2 and candidate_sentences < google_sentences:
            issues.append("sentence_count")

        google_terms = _translation_keyword_terms(google_english)
        if len(google_terms) >= 3:
            candidate_terms = _translation_keyword_terms(candidate)
            coverage_ratio = len(google_terms & candidate_terms) / len(google_terms)
            if coverage_ratio < _COMPLETENESS_MIN_COVERAGE_RATIO:
                issues.append("content_coverage")

    if "?" in google_english and "?" not in candidate:
        issues.append("question_preservation")
    return issues


def _candidate_selection_score(
    google_english: str,
    candidate: str,
    *,
    prefer_natural: bool,
) -> float:
    score = _translation_deviation_score(google_english, candidate)
    if prefer_natural:
        score += 0.05
    return score


def _translation_looks_incomplete(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.endswith(("...", ".", "!", "?", ";", ":")):
        return False
    if stripped.endswith(","):
        return True
    if _ENGLISH_INCOMPLETE_TAIL.search(stripped):
        return True
    if re.search(r"\bif\b", stripped, re.IGNORECASE) and "," not in stripped:
        return True
    return False


def _format_deferred_release_text(english: str, google_english: str) -> str:
    candidate = _normalize_translation(english, google_english) if english else ""
    google = _normalize_translation(google_english, google_english) if google_english else ""
    release_text = candidate or google
    if _translation_looks_incomplete(candidate):
        release_text = google or candidate
    if release_text and _translation_looks_incomplete(release_text):
        release_text = release_text.rstrip(" ,;:") + "..."
    return release_text


def _build_alignment_request_message(
    spanish: str,
    english: str,
    *,
    google_english: str = "",
    source_quality: str = "clean",
    translation_register: str = "expository",
    discourse_tag: str = "statement",
    verse_detected: dict | None = None,
) -> str:
    parts = [
        f"[SOURCE — Spanish original]\n{spanish}",
        f"[DISPLAYED ENGLISH]\n{english}",
    ]
    if google_english:
        parts.append(f"[GOOGLE ENGLISH BASELINE]\n{google_english}")
    parts.append(f"[SOURCE QUALITY]\n{source_quality}")
    parts.append(f"[TRANSLATION REGISTER]\n{translation_register}")
    parts.append(f"[DISCOURSE TAG]\n{discourse_tag}")
    if verse_detected:
        reference = str(verse_detected.get("reference", "")).strip()
        canonical_english = str(verse_detected.get("canonical_english", "")).strip()
        spanish_quote = str(verse_detected.get("spanish_text", "")).strip()
        verse_lines: list[str] = []
        if reference:
            verse_lines.append(f"reference: {reference}")
        if spanish_quote:
            verse_lines.append(f"quoted_spanish: {spanish_quote}")
        if canonical_english:
            verse_lines.append(f"canonical_english: {canonical_english}")
        if verse_lines:
            parts.append("[SCRIPTURE CONTEXT]\n" + "\n".join(verse_lines))
    return "\n\n".join(parts)


def _build_system_prompt(church_terms: dict[str, str]) -> str:
    if church_terms:
        lines = "\n".join(f"  {es} → {en}" for es, en in church_terms.items())
        glossary_block = (
            "THEOLOGICAL GLOSSARY — prefer these translations, adapting grammatical form "
            "and number to match the context (e.g. singular/plural, noun/adjective):\n" + lines
        )
    else:
        glossary_block = ""
    return _SYSTEM_PROMPT_BASE.replace("{glossary_block}", glossary_block)


def _build_user_message(
    spanish: str,
    google_english: str,
    topic_context: str,
    sentence_history: list[tuple[str, str]],
    active_passage: dict | None,
    shown_suggestions: set[str],
    current_mode_label: str = "",
    prev_discourse: dict | None = None,
    recent_modes: list[str] | None = None,
) -> str:
    parts: list[str] = []

    if topic_context:
        parts.append(f"[SERMON CONTEXT]\n{topic_context}")

    if current_mode_label:
        parts.append(f"[CURRENT MODE]\n{current_mode_label}")

    if recent_modes and len(recent_modes) > 1:
        parts.append(f"[MODE TRAJECTORY — most recent last]\n{' → '.join(recent_modes)}")

    if active_passage:
        parts.append(
            f"[ACTIVE PASSAGE]\n"
            f"The preacher is currently expounding: "
            f"{active_passage['reference']} — {active_passage['canonical_english']}"
        )

    if shown_suggestions:
        parts.append(f"[ALREADY SUGGESTED]\n{', '.join(sorted(shown_suggestions))}")

    if sentence_history:
        lines = []
        for sp, en in sentence_history:
            lines.append(f"  ES: {sp}")
            lines.append(f"  EN: {en}")
        parts.append("[RECENT SENTENCES — most recent last]\n" + "\n".join(lines))

    if prev_discourse:
        tag = prev_discourse.get("discourse_tag", "statement")
        introduces = prev_discourse.get("introduces_quote", False)
        complete = prev_discourse.get("thought_complete", True)
        continuation = prev_discourse.get("continuation_required", False)
        quality = prev_discourse.get("source_quality", "clean")
        ready = prev_discourse.get("display_ready", True)
        parts.append(
            f"[PREVIOUS SENTENCE DISCOURSE]\n"
            f"discourse_tag: {tag}\n"
            f"introduces_quote: {str(introduces).lower()}\n"
            f"thought_complete: {str(complete).lower()}\n"
            f"continuation_required: {str(continuation).lower()}\n"
            f"source_quality: {quality}\n"
            f"display_ready: {str(ready).lower()}"
        )
        # When the previous sentence was held (not display_ready), inject its text
        # prominently so the model can write a correct merged improved_translation.
        if not ready and sentence_history:
            prev_sp, prev_en = sentence_history[-1]
            parts.append(
                f"[PREVIOUS SENTENCE — PENDING MERGE]\n"
                f"  ES: {prev_sp}\n"
                f"  EN: {prev_en}\n"
                f"If merge_with_previous is true, treat it as segmentation repair only. "
                f"improved_translation MUST cover both this previous sentence AND the current sentence "
                f"as one repaired unit."
            )

    word_count = len(spanish.split())
    if word_count > 25:
        parts.append(
            f"[LONG SENTENCE — {word_count} words]\n"
            f"This is a long sentence ({word_count} words). "
            f"Prioritize structural accuracy. Preserve all clause relationships. "
            f"Do not truncate or summarize. Prefer coherent segmentation over polish."
        )

    parts.append(f"[SOURCE — Spanish original]\n{spanish}")
    parts.append(f"[GOOGLE TRANSLATION — may need improvement]\n{google_english}")
    return "\n\n".join(parts)


class LLMEnrichmentService:
    """Post-translation enrichment via Claude structured output.

    Called once per committed sentence (accurate track only).
    Always runs as a fire-and-forget background task — never blocks Google translation.

    For each sentence it:
    - Improves the Google translation where possible → fires on_translation_update
    - Detects explicit or quoted Bible verse references → accumulated in scratch pad,
      flushed as consolidated ranges → fires on_verse_detected
    - Suggests 1–3 related verses based on theme → fires on_verse_suggestion
    - Signals enrichment settled → fires on_enrichment_settled (used to suppress
      stale Google dual-pass corrections)
    """

    def __init__(
        self,
        church_id: str,
        church_terms: dict[str, str],
        topic_tracker: "TopicTracker",
        on_translation_update: Callable[[int, str, list[dict] | None], Awaitable[None]],
        on_verse_detected: Callable[[int, dict], Awaitable[None]],
        on_verse_range_update: Callable[[int, dict], Awaitable[None]],
        on_verse_suggestion: Callable[[int, list[dict]], Awaitable[None]],
        on_enrichment_settled: Callable[[int], Awaitable[None]],
        on_phrase_alignment: Callable[[int, list[dict]], Awaitable[None]] | None = None,
        on_buffer_hold: Callable[[str, float], Awaitable[None]] | None = None,
        on_caption_merge: Callable[[int, int, str, str], Awaitable[None]] | None = None,
        on_segment_metadata: Callable[[int, dict], Awaitable[None]] | None = None,
        session_id: int = 0,
        state_tracker: "SermonStateTracker | None" = None,
    ):
        self._church_id = church_id
        self._topic_tracker = topic_tracker
        self._state_tracker = state_tracker
        self._on_translation_update = on_translation_update
        self._on_phrase_alignment = on_phrase_alignment
        self._on_verse_detected = on_verse_detected
        self._on_verse_range_update = on_verse_range_update
        self._on_verse_suggestion = on_verse_suggestion
        self._on_enrichment_settled = on_enrichment_settled
        self._on_buffer_hold = on_buffer_hold
        self._on_caption_merge = on_caption_merge
        self._on_segment_metadata = on_segment_metadata
        self._session_id = session_id
        self._system_prompt = _build_system_prompt(church_terms)
        self._client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._tasks: list[asyncio.Task] = []
        # Serializes post-completion state mutations so concurrent tasks that finish
        # out of order (due to variable API latency) don't corrupt sentence history,
        # active passage, shown suggestions, or sermon mode signals.
        self._mutation_lock = asyncio.Lock()
        # Preserve sentence application order even when API responses complete out
        # of order. Calls may remain concurrent; only the state-application phase
        # waits for its turn.
        self._apply_condition = asyncio.Condition()
        self._scheduled_ts: deque[int] = deque()

        # Rolling sentence context (last 3 sentences, Spanish + best English)
        self._sentence_history: deque[tuple[str, str]] = deque(maxlen=3)
        # Most recent explicit verse citation — injected into every subsequent prompt
        self._active_passage: dict | None = None
        # All references suggested this session — prevents repetition
        self._shown_suggestions: set[str] = set()
        # Discourse output of the previous enriched sentence — injected as forward context
        self._prev_discourse: dict | None = None
        # Timestamp of the previously enriched sentence — used for segmentation-repair targeting
        self._prev_sentence_ts: int | None = None
        # Deferred translation updates: ts → (english, asyncio.Task)
        # When display_ready is false, translation_update is held pending segmentation repair or timeout.
        self._deferred_updates: dict[int, tuple[str, asyncio.Task]] = {}
        # Chain-aware segmentation repair. Anchored to the EARLIEST visible segment so the caption
        # stays at a stable screen position if a bad split must be repaired.
        # {
        #   "head_ts": int,   # oldest visible segment (ts_keep in every merge)
        #   "tail_ts": int,   # most recently absorbed fragment (used to detect chain extension)
        #   "spanish": str,   # full accumulated chain Spanish
        #   "length": int,
        # }
        self._merge_chain_head: dict | None = None
        # Rolling sermon mode trajectory (last 3 modes) — injected into prompt for
        # better mode classification at rhetorical transitions.
        self._recent_modes: deque[str] = deque(maxlen=3)
        # Last translation emitted per ts — guards against redundant segmentation-repair
        # emissions when chain extends (prevents UI flickering on the head segment).
        self._last_emitted_translation: dict[int, str] = {}

        # Verse scratch pad — accumulates detections for temporal range consolidation
        self._verse_scratch: list[VerseScratchEntry] = []

        # Metrics counters — accessible for logging/monitoring
        self.metrics: dict[str, int] = {
            "noisy_input_detected": 0,
            "reconstruction_risk": 0,
            "structural_flush_block": 0,
            "forced_release": 0,
            "verse_suggestion_triggered": 0,
            "verse_suggestion_gated": 0,
            "parse_retry_success": 0,
            "parse_failed": 0,
            "merge_chain_max_length": 0,
            # Precision-phase metrics (added for noise/structure improvements)
            "stt_noise_removed_count": 0,       # incremented per STT final where noise was stripped
            "conditional_flush_block_count": 0, # conditional-clause holds (mirror of sentence_buffer)
            "fragment_merge_count": 0,          # times a fragment was merged into a chain
            "long_sentence_handled_count": 0,   # sentences > 25 words sent to LLM
        }

    def enrich(
        self,
        spanish: str,
        google_english: str,
        ts: int,
        audio_start: float = 0.0,
        audio_end: float = 0.0,
        terminal_incomplete: bool = False,
    ) -> asyncio.Task:
        """Schedule enrichment as a fire-and-forget task. Does not block."""
        self._scheduled_ts.append(ts)
        task = asyncio.create_task(
            self._run_enrichment(
                spanish,
                google_english,
                ts,
                audio_start,
                audio_end,
                terminal_incomplete,
            )
        )
        self._tasks = [t for t in self._tasks if not t.done()]
        self._tasks.append(task)
        return task

    def _schedule_phrase_alignment(
        self,
        *,
        ts: int,
        spanish: str,
        english: str,
        google_english: str = "",
        source_quality: str,
        translation_register: str = "expository",
        discourse_tag: str = "statement",
        verse_detected: dict | None = None,
        merge_with_previous: bool = False,
    ) -> None:
        if not self._on_phrase_alignment:
            return
        if merge_with_previous:
            return
        if not _alignment_allowed(
            source_quality=source_quality,
            translation_register=translation_register,
            discourse_tag=discourse_tag,
            verse_detected=verse_detected,
        ):
            return
        if english.endswith("...") or len(english.split()) < 2:
            return
        task = asyncio.create_task(
            self._generate_phrase_alignment(
                ts=ts,
                spanish=spanish,
                english=english,
                google_english=google_english,
                source_quality=source_quality,
                translation_register=translation_register,
                discourse_tag=discourse_tag,
                verse_detected=verse_detected,
            )
        )
        self._tasks = [t for t in self._tasks if not t.done()]
        self._tasks.append(task)

    def request_phrase_alignment(
        self,
        *,
        ts: int,
        spanish: str,
        english: str,
        google_english: str = "",
        source_quality: str = "clean",
        translation_register: str = "expository",
        discourse_tag: str = "statement",
        verse_detected: dict | None = None,
    ) -> None:
        self._schedule_phrase_alignment(
            ts=ts,
            spanish=spanish,
            english=english,
            google_english=google_english,
            source_quality=source_quality,
            translation_register=translation_register,
            discourse_tag=discourse_tag,
            verse_detected=verse_detected,
            merge_with_previous=False,
        )

    async def _create_json_response(
        self,
        *,
        system: str,
        user_message: str,
        ts: int,
        stage: str,
        max_tokens: int = MAX_ENRICHMENT_TOKENS,
    ) -> dict | None:
        response = await self._client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        raw = response.content[0].text.strip()
        stripped = _strip_json_fences(raw)
        result = _parse_json_object(raw)
        if result is not None:
            if raw != stripped:
                self.metrics["parse_retry_success"] += 1
            return result

        self.metrics["parse_failed"] += 1
        logger.warning(
            "[enrichment:%s] Could not parse %s JSON for ts=%d: %.160s",
            self._church_id, stage, ts, raw,
        )
        return None

    async def _repair_translation_candidates(
        self,
        *,
        spanish: str,
        google_english: str,
        current_candidate: str,
        issues: list[str],
        translation_register: str,
        discourse_tag: str,
        ts: int,
    ) -> dict[str, str] | None:
        issue_lines = "\n".join(f"- {issue}" for issue in issues)
        user_message = (
            f"[SOURCE — Spanish original]\n{spanish}\n\n"
            f"[GOOGLE TRANSLATION]\n{google_english}\n\n"
            f"[REJECTED FIRST CANDIDATE]\n{current_candidate}\n\n"
            f"[FAILURE SIGNALS]\n{issue_lines}\n\n"
            f"[REGISTER]\n{translation_register}\n\n"
            f"[DISCOURSE TAG]\n{discourse_tag}"
        )
        result = await self._create_json_response(
            system=_REPAIR_TRANSLATION_SYSTEM,
            user_message=user_message,
            ts=ts,
            stage="repair",
            max_tokens=600,
        )
        if result is None:
            return None
        return {
            "literal_translation": str(result.get("literal_translation", "")).strip(),
            "natural_translation": str(result.get("natural_translation", "")).strip(),
        }

    async def _generate_phrase_alignment(
        self,
        *,
        ts: int,
        spanish: str,
        english: str,
        google_english: str = "",
        source_quality: str = "clean",
        translation_register: str = "expository",
        discourse_tag: str = "statement",
        verse_detected: dict | None = None,
    ) -> None:
        try:
            result = await self._create_json_response(
                system=_ALIGNMENT_SYSTEM,
                user_message=_build_alignment_request_message(
                    spanish,
                    english,
                    google_english=google_english,
                    source_quality=source_quality,
                    translation_register=translation_register,
                    discourse_tag=discourse_tag,
                    verse_detected=verse_detected,
                ),
                ts=ts,
                stage="alignment",
                max_tokens=500,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[enrichment:%s] alignment generation failed for ts=%d: %s", self._church_id, ts, e)
            return

        if result is None:
            return

        phrase_alignment = _sanitize_phrase_alignment(
            result.get("phrase_alignment"),
            english,
        )
        if not phrase_alignment:
            return

        try:
            await self._on_phrase_alignment(ts, phrase_alignment)
        except Exception as e:
            logger.warning("[enrichment:%s] on_phrase_alignment failed: %s", self._church_id, e)

    def _select_best_translation(
        self,
        *,
        google_english: str,
        improved: str,
        source_quality: str,
        translation_register: str,
        discourse_tag: str,
        ts: int,
        repair_options: dict[str, str] | None = None,
    ) -> tuple[str, str, list[str]]:
        normalized_google = _normalize_translation(google_english, google_english)
        normalized_improved = _normalize_translation(improved, google_english) if improved else ""

        def evaluate(candidate: str, label: str) -> tuple[bool, list[str], float, str]:
            normalized = _normalize_translation(candidate, google_english) if candidate else ""
            if not normalized:
                return False, ["empty_candidate"], -1.0, ""
            if normalized == normalized_google:
                score = _candidate_selection_score(
                    normalized_google,
                    normalized_google,
                    prefer_natural=(label == "natural"),
                )
                return True, [], score, normalized_google

            issues = _translation_completeness_issues(
                normalized_google,
                normalized,
                translation_register=translation_register,
            )
            if source_quality == "noisy":
                deviation = _translation_deviation_score(normalized_google, normalized)
                if deviation < _RECONSTRUCTION_RISK_THRESHOLD:
                    issues.append("reconstruction_risk")
            valid = not issues
            score = _candidate_selection_score(
                normalized_google,
                normalized if valid else normalized_google,
                prefer_natural=(label == "natural"),
            )
            return valid, issues, score, normalized

        candidates: list[tuple[bool, list[str], float, str, str]] = [
            (*evaluate(normalized_improved, "primary"), "primary"),
        ]
        if repair_options:
            for label in ("literal", "natural"):
                candidates.append(
                    (*evaluate(repair_options.get(f"{label}_translation", ""), label), label)
                )

        valid_candidates = [candidate for candidate in candidates if candidate[0] and candidate[3]]
        if valid_candidates:
            prefer_close = translation_register == "scripture" or source_quality != "clean"
            if prefer_close:
                valid_candidates.sort(
                    key=lambda entry: (entry[2], entry[4] == "literal"),
                    reverse=True,
                )
            else:
                valid_candidates.sort(
                    key=lambda entry: (entry[4] == "natural", entry[4] == "primary", entry[2]),
                    reverse=True,
                )
            _, _, _, chosen_text, chosen_label = valid_candidates[0]
            return chosen_text, chosen_label, []

        return normalized_google, "google_fallback", candidates[0][1]

    async def _wait_for_apply_turn(self, ts: int) -> None:
        async with self._apply_condition:
            await self._apply_condition.wait_for(
                lambda: self._scheduled_ts and self._scheduled_ts[0] == ts
            )

    async def _finish_apply_turn(self, ts: int) -> None:
        async with self._apply_condition:
            if self._scheduled_ts and self._scheduled_ts[0] == ts:
                self._scheduled_ts.popleft()
            else:
                try:
                    self._scheduled_ts.remove(ts)
                except ValueError:
                    pass
            self._apply_condition.notify_all()

    async def _run_enrichment(
        self,
        spanish: str,
        google_english: str,
        ts: int,
        audio_start: float,
        audio_end: float,
        terminal_incomplete: bool,
    ) -> None:
        # topic_context is from TopicTracker (updated on an independent schedule,
        # not affected by enrichment apply ordering — snapshot early is fine).
        topic_context = self._topic_tracker.get_context()

        # Wait for our apply turn before snapshotting shared enrichment state.
        # This guarantees that prev_discourse, sentence_history, and active_passage
        # reflect the fully-settled result from the immediately preceding sentence,
        # enabling correct merge decisions (e.g. "answer_to_question" after a
        # "rhetorical_question") even when consecutive sentences arrive rapidly.
        # API calls therefore run sequentially for adjacent sentences, but since
        # the sentence buffer holds sentences for ≥3.5s, only very rapid bursts
        # (Q&A pairs flushed within ~700ms of each other) notice any difference.
        await self._wait_for_apply_turn(ts)

        # Snapshot mutable enrichment state under the mutation lock so concurrent
        # verse-suggestion tasks cannot race against our reads.
        async with self._mutation_lock:
            history = list(self._sentence_history)
            active_passage = self._active_passage
            shown = set(self._shown_suggestions)
            prev_discourse = self._prev_discourse
            current_mode_label = (
                self._state_tracker.get_context_label() if self._state_tracker else ""
            )
            recent_modes = list(self._recent_modes)

        logger.debug(
            "[enrichment:%s] Enriching ts=%d at audio=%.1f–%.1fs | "
            "history=%d prior sentence(s) | active_passage=%s | shown=%d suggestion(s) | mode=%s",
            self._church_id, ts, audio_start, audio_end,
            len(history),
            active_passage["reference"] if active_passage else "none",
            len(shown),
            current_mode_label or "unknown",
        )

        user_msg = _build_user_message(
            spanish, google_english, topic_context, history, active_passage, shown,
            current_mode_label, prev_discourse, recent_modes,
        )

        try:
            result = await self._create_json_response(
                system=self._system_prompt,
                user_message=user_msg,
                ts=ts,
                stage="enrichment",
            )
        except asyncio.CancelledError:
            await self._finish_apply_turn(ts)
            raise
        except Exception as e:
            logger.warning("[enrichment:%s] Claude call failed for ts=%d: %s", self._church_id, ts, e)
            # Fire enrichment_settled so the Google correction guard is not permanently blocked,
            # and the original Google translation remains visible (no frozen caption).
            try:
                await self._on_enrichment_settled(ts)
            except Exception:
                pass
            await self._finish_apply_turn(ts)
            return

        if result is None:
            await self._finish_apply_turn(ts)
            return

        discourse_tag = result.get("discourse_tag", "statement")
        introduces_quote = bool(result.get("introduces_quote", False))
        thought_complete = bool(result.get("thought_complete", True))
        continuation_required = bool(result.get("continuation_required", False))
        merge_with_previous = bool(result.get("merge_with_previous", False))
        paragraph_break = bool(result.get("paragraph_break", False))
        source_quality = result.get("source_quality", "clean")
        if source_quality not in ("clean", "noisy", "fragmented"):
            source_quality = "clean"
        translation_register = result.get("translation_register", "expository")
        if translation_register not in ("scripture", "expository", "narrative", "exhortation"):
            translation_register = "expository"
        display_ready = (
            thought_complete
            and not continuation_required
            and source_quality != "fragmented"
            and discourse_tag != "quote_introduction"
        )
        display_ready_from_llm = result.get("display_ready")
        if isinstance(display_ready_from_llm, bool) and not display_ready_from_llm:
            display_ready = False
        if terminal_incomplete:
            display_ready = False

        sermon_mode = result.get("sermon_mode", "exposition")
        if sermon_mode not in _VALID_SERMON_MODES:
            sermon_mode = "exposition"

        guard_google_english = google_english
        if merge_with_previous and history:
            guard_google_english = f"{history[-1][1]} {google_english}".strip()

        improved = result.get("improved_translation", "").strip()
        chosen_english, chosen_source, candidate_issues = self._select_best_translation(
            google_english=guard_google_english,
            improved=improved,
            source_quality=source_quality,
            translation_register=translation_register,
            discourse_tag=discourse_tag,
            ts=ts,
        )
        if candidate_issues:
            repair_options = await self._repair_translation_candidates(
                spanish=spanish,
                google_english=guard_google_english,
                current_candidate=improved,
                issues=candidate_issues,
                translation_register=translation_register,
                discourse_tag=discourse_tag,
                ts=ts,
            )
            chosen_english, chosen_source, candidate_issues = self._select_best_translation(
                google_english=guard_google_english,
                improved=improved,
                source_quality=source_quality,
                translation_register=translation_register,
                discourse_tag=discourse_tag,
                ts=ts,
                repair_options=repair_options,
            )

        best_english = chosen_english
        if terminal_incomplete:
            best_english = _format_deferred_release_text(best_english, google_english)
        if chosen_source == "google_fallback":
            logger.warning(
                "[enrichment:%s] translation_guard_fallback_google ts=%d "
                "register=%s tag=%s issues=%s",
                self._church_id, ts, translation_register, discourse_tag, ",".join(candidate_issues),
            )
        if "reconstruction_risk" in candidate_issues:
            self.metrics["reconstruction_risk"] += 1

        # (apply turn already acquired above — proceed directly to state mutation)

        # Acquire the mutation lock before touching any shared state.
        # Concurrent enrichment tasks complete in arbitrary order due to variable
        # API latency; the lock ensures sentence history, active passage, shown
        # suggestions, and mode signals are always updated in arrival order.
        async with self._mutation_lock:
            logger.info(
                "[enrichment:%s] Discourse ts=%d: tag=%s introduces_quote=%s "
                "complete=%s continuation=%s merge=%s break=%s quality=%s "
                "register=%s display_ready=%s",
                self._church_id, ts, discourse_tag, introduces_quote,
                thought_complete, continuation_required, merge_with_previous,
                paragraph_break, source_quality, translation_register, display_ready,
            )
            # Store for injection into the next sentence's prompt
            self._prev_discourse = {
                "discourse_tag": discourse_tag,
                "introduces_quote": introduces_quote,
                "thought_complete": thought_complete,
                "continuation_required": continuation_required,
                "source_quality": source_quality,
                "display_ready": display_ready,
            }

            # Feedback hold: if this sentence was incomplete, ask the buffer to
            # hold the next sentence longer so its continuation can accumulate.
            # continuation_required takes precedence (stronger signal); fall back
            # to thought_complete. This is a forward correction — it can't un-flush
            # the current sentence but prevents the same pattern on the next boundary.
            if (continuation_required or not thought_complete) and self._on_buffer_hold:
                hold_secs = 4.5 if continuation_required else 3.5
                reason = "continuation_required" if continuation_required else "incomplete_thought"
                try:
                    await self._on_buffer_hold(reason, hold_secs)
                except Exception as e:
                    logger.warning("[enrichment:%s] on_buffer_hold failed: %s", self._church_id, e)

            # Hold longer when audio is fragmented — the speaker may have been cut mid-word.
            if source_quality == "fragmented" and self._on_buffer_hold:
                try:
                    await self._on_buffer_hold("fragmented_audio", 3.0)
                except Exception as e:
                    logger.warning("[enrichment:%s] on_buffer_hold (fragmented) failed: %s", self._church_id, e)

            # Also hold for quote introductions detected by the LLM (catches cases
            # the synchronous regex in _on_sentence may have missed).
            if introduces_quote and self._on_buffer_hold:
                try:
                    await self._on_buffer_hold("quote_introduction_llm", 4.0)
                except Exception as e:
                    logger.warning("[enrichment:%s] on_buffer_hold (quote) failed: %s", self._church_id, e)

            logger.info("[enrichment:%s] Sermon mode ts=%d: %s", self._church_id, ts, sermon_mode)
            if self._state_tracker:
                await self._state_tracker.add_signal(sermon_mode, ts)
            self._recent_modes.append(sermon_mode)

            # Track noisy input
            if source_quality == "noisy":
                self.metrics["noisy_input_detected"] += 1

            # Track long sentence handling
            if len(spanish.split()) > 25:
                self.metrics["long_sentence_handled_count"] += 1

            if display_ready:
                # Sentence is finalised — emit translation update immediately.
                if best_english != _normalize_translation(google_english):
                    logger.info(
                        "[enrichment:%s] decision=immediate_translation_update ts=%d "
                        "source=%s:\n"
                        "  google: %s\n     llm: %s",
                        self._church_id, ts, chosen_source, google_english[:80], best_english[:80],
                    )
                    try:
                        await self._on_translation_update(ts, best_english, None)
                        self._last_emitted_translation[ts] = best_english
                    except Exception as e:
                        logger.warning("[enrichment:%s] on_translation_update failed: %s", self._church_id, e)
                else:
                    logger.info(
                        "[enrichment:%s] decision=immediate_translation_update ts=%d — no change",
                        self._church_id, ts,
                    )
                    self._last_emitted_translation[ts] = _normalize_translation(google_english)
            else:
                # Sentence is not display_ready — suppress translation update and defer.
                # The deferred release fires after DEFERRED_RELEASE_S if no merge arrives.
                defer_task = asyncio.create_task(
                    self._deferred_translation_release(
                        ts,
                        best_english,
                        google_english,
                        phrase_alignment=None,
                        spanish=spanish,
                        allow_alignment=(
                            not merge_with_previous
                            and _alignment_allowed(
                                source_quality=source_quality,
                                translation_register=translation_register,
                                discourse_tag=discourse_tag,
                                verse_detected=result.get("verse_detected") if isinstance(result.get("verse_detected"), dict) else None,
                            )
                        ),
                        translation_register=translation_register,
                        discourse_tag=discourse_tag,
                        verse_detected=result.get("verse_detected") if isinstance(result.get("verse_detected"), dict) else None,
                    )
                )
                self._deferred_updates[ts] = (best_english, defer_task)
                logger.info(
                    "[enrichment:%s] decision=suppressed_translation_update ts=%d "
                    "(display_ready=false continuation=%s quality=%s)",
                    self._church_id, ts, continuation_required, source_quality,
                )

            # Signal enrichment settled immediately in all cases so Google correction guard fires.
            try:
                await self._on_enrichment_settled(ts)
            except Exception as e:
                logger.warning("[enrichment:%s] on_enrichment_settled failed: %s", self._church_id, e)

            # Append to sentence history using the best available translation
            self._sentence_history.append((spanish, best_english))

            if display_ready:
                self._schedule_phrase_alignment(
                    ts=ts,
                    spanish=spanish,
                    english=best_english,
                    google_english=google_english,
                    source_quality=source_quality,
                    translation_register=translation_register,
                    discourse_tag=discourse_tag,
                    verse_detected=result.get("verse_detected") if isinstance(result.get("verse_detected"), dict) else None,
                    merge_with_previous=merge_with_previous,
                )

            # --- Caption merge (head-anchored chain) ---
            # The repair chain is always anchored to the EARLIEST visible segment (head_ts = ts_keep).
            # Every subsequent fragment is absorbed INTO the head so the caption stays at a
            # stable screen position as the chain grows.
            #
            # on_caption_merge(absorb_ts, keep_ts, ...) → ts_absorb=absorb_ts, ts_keep=keep_ts
            prev_ts = self._prev_sentence_ts
            if merge_with_previous and prev_ts is not None and self._on_caption_merge:
                self.metrics["fragment_merge_count"] += 1
                hist = list(self._sentence_history)

                chain = self._merge_chain_head
                if chain is not None and prev_ts == chain["tail_ts"]:
                    # Extending an active segmentation-repair chain — absorb current ts into the head anchor.
                    chain_spanish = chain["spanish"] + " " + spanish
                    chain_len = chain["length"] + 1
                    head_ts = chain["head_ts"]

                    # Cancel deferred update for the fragment being absorbed (current ts).
                    if ts in self._deferred_updates:
                        _, dt = self._deferred_updates.pop(ts)
                        dt.cancel()
                        logger.info(
                            "[enrichment:%s] decision=merge_cancelled_deferred_update absorbed=%d",
                            self._church_id, ts,
                        )

                    self._merge_chain_head = {
                        "head_ts": head_ts,
                        "tail_ts": ts,
                        "spanish": chain_spanish,
                        "length": chain_len,
                    }
                    if chain_len > self.metrics["merge_chain_max_length"]:
                        self.metrics["merge_chain_max_length"] = chain_len
                    logger.debug(
                        "[enrichment:%s] decision=merge_chain_extended ts=%d absorbed_by_head=%d "
                        "chain_len=%d spanish_len=%d",
                        self._church_id, ts, head_ts, chain_len, len(chain_spanish),
                    )
                    logger.info(
                        "[enrichment:%s] decision=merge_applied ts=%d absorbed_by_head=%d "
                        "chain_len=%d",
                        self._church_id, ts, head_ts, chain_len,
                    )
                    # Lock-in guard: skip emission if the merged translation is identical
                    # to what was last emitted for the head, preventing UI flickering.
                    if self._last_emitted_translation.get(head_ts) != best_english:
                        try:
                            await self._on_caption_merge(ts, head_ts, chain_spanish, best_english)
                            self._last_emitted_translation[head_ts] = best_english
                        except Exception as e:
                            logger.warning("[enrichment:%s] on_caption_merge failed: %s", self._church_id, e)
                    else:
                        logger.debug(
                            "[enrichment:%s] decision=merge_skipped_no_change head=%d ts=%d",
                            self._church_id, head_ts, ts,
                        )

                else:
                    # Starting a new segmentation-repair chain — prev_ts becomes the head anchor; current ts absorbed.
                    prev_spanish = hist[-2][0] if len(hist) >= 2 else spanish
                    chain_spanish = prev_spanish + " " + spanish

                    # Cancel deferreds for both the head (prev_ts) and the absorbed sentence (ts).
                    for cancel_ts in (prev_ts, ts):
                        if cancel_ts in self._deferred_updates:
                            _, dt = self._deferred_updates.pop(cancel_ts)
                            dt.cancel()
                            logger.info(
                                "[enrichment:%s] decision=merge_cancelled_deferred_update "
                                "cancelled=%d",
                                self._church_id, cancel_ts,
                            )

                    self._merge_chain_head = {
                        "head_ts": prev_ts,
                        "tail_ts": ts,
                        "spanish": chain_spanish,
                        "length": 2,
                    }
                    logger.info(
                        "[enrichment:%s] decision=merge_applied ts=%d absorbed_by_head=%d "
                        "chain_len=2",
                        self._church_id, ts, prev_ts,
                    )
                    # Lock-in guard: only emit if the merged translation differs from
                    # what was last shown for the head, preventing redundant UI updates.
                    if self._last_emitted_translation.get(prev_ts) != best_english:
                        try:
                            await self._on_caption_merge(ts, prev_ts, chain_spanish, best_english)
                            self._last_emitted_translation[prev_ts] = best_english
                        except Exception as e:
                            logger.warning("[enrichment:%s] on_caption_merge failed: %s", self._church_id, e)
                    else:
                        logger.debug(
                            "[enrichment:%s] decision=merge_skipped_no_change head=%d ts=%d",
                            self._church_id, prev_ts, ts,
                        )

            else:
                # No merge — reset chain.
                if self._merge_chain_head is not None:
                    logger.debug(
                        "[enrichment:%s] Chain closed at ts=%d (display_ready=%s, merge=%s)",
                        self._church_id, ts, display_ready, merge_with_previous,
                    )
                self._merge_chain_head = None

            # --- Segment metadata ---
            # pending_completion signals that this segment may be updated or merged.
            # The client can dim/italicise it until a translation_update or caption_merge arrives.
            pending_completion = (not display_ready) or terminal_incomplete
            if self._on_segment_metadata:
                metadata = {
                    "translation_register": translation_register,
                    "paragraph_break": paragraph_break,
                    "source_quality": source_quality,
                    "pending_completion": pending_completion,
                    "terminal_incomplete": terminal_incomplete,
                }
                try:
                    await self._on_segment_metadata(ts, metadata)
                except Exception as e:
                    logger.warning("[enrichment:%s] on_segment_metadata failed: %s", self._church_id, e)

            # Advance prev_sentence_ts for the next enrichment's merge targeting
            self._prev_sentence_ts = ts

            # --- Verse suggestions (scheduled outside mutation lock) ---
            # Capture context snapshots now while under the lock.
            _vs_active = self._active_passage
            _vs_shown = set(self._shown_suggestions)

            # --- Verse detection ---
            verse = result.get("verse_detected")
            if verse and isinstance(verse, dict):
                if _is_valid_verse(verse):
                    logger.info(
                        "[enrichment:%s] Verse detected ts=%d: %s (%s) at audio=%.1f–%.1fs",
                        self._church_id, ts, verse["reference"], verse["confidence"],
                        audio_start, audio_end,
                    )
                    # Advance active passage on explicit citations (chapter announcements or full refs)
                    if verse.get("confidence") == "explicit":
                        prev = self._active_passage
                        if (prev is None
                                or prev["book"] != verse["book"]
                                or prev["chapter"] != verse["chapter"]):
                            self._active_passage = verse
                            logger.info(
                                "[enrichment:%s] Active passage → %s (was: %s)",
                                self._church_id, verse["reference"],
                                prev["reference"] if prev else "none",
                            )
                            self._topic_tracker.set_active_passage(
                                verse["reference"], verse["canonical_english"]
                            )
                    await self._handle_verse_detection(verse, audio_start, audio_end, ts)
                else:
                    logger.debug(
                        "[enrichment:%s] Verse shape failed validation ts=%d: %s",
                        self._church_id, ts, _describe_invalid_verse(verse),
                    )
                    logger.info("[enrichment:%s] No verse detected ts=%d", self._church_id, ts)
            else:
                logger.info("[enrichment:%s] No verse detected ts=%d", self._church_id, ts)

        await self._finish_apply_turn(ts)

        # --- Verse suggestions (separate async call, outside mutation lock) ---
        # Runs as a fire-and-forget task so it cannot delay structural decisions.
        # Uses only the context snapshots captured inside the lock above.
        _suggest_eligible = (
            self._should_suggest()
            and display_ready
            and self._should_generate_verse_suggestions(
                spanish, source_quality, discourse_tag, _vs_active
            )
        )
        async with self._mutation_lock:
            if _suggest_eligible:
                self.metrics["verse_suggestion_triggered"] += 1
            else:
                self.metrics["verse_suggestion_gated"] += 1
        if _suggest_eligible:
            suggest_task = asyncio.create_task(
                self._run_verse_suggestions(
                    spanish, google_english, ts,
                    topic_context, _vs_active, _vs_shown,
                )
            )
            self._tasks = [t for t in self._tasks if not t.done()]
            self._tasks.append(suggest_task)

    def _should_suggest(self) -> bool:
        """Return True when the current sermon mode warrants verse suggestions."""
        if not self._state_tracker:
            return True
        return self._state_tracker.settled_mode not in (
            "illustration", "exhortation", "procedural"
        )

    def _should_generate_verse_suggestions(
        self,
        spanish: str,
        source_quality: str,
        discourse_tag: str,
        active_passage: dict | None,
    ) -> bool:
        """Additional content-level gate for verse suggestions.

        Only generates suggestions when there is meaningful, clean theological
        content to cross-reference. Prevents over-triggering on noise, short
        fragments, and casual filler sentences.

        Gates:
        - Noisy or fragmented source: skip (system prompt also says this, but
          avoid the API call entirely).
        - Short fragments (< 7 words): skip unless an active passage is in progress.
        - Procedural or transitional discourse: skip.
        - Casual exhortation filler without a supporting active passage: skip.
        """
        if source_quality in ("noisy", "fragmented"):
            return False
        word_count = len(spanish.split())
        if word_count < 7 and active_passage is None:
            return False
        if discourse_tag in ("transition", "quote_introduction"):
            return False
        # Exhortation-only filler without an active passage context is too generic
        if discourse_tag == "exhortation_appeal" and active_passage is None and word_count < 12:
            return False
        return True

    async def _deferred_translation_release(
        self,
        ts: int,
        english: str,
        google_english: str,
        phrase_alignment: list[dict] | None = None,
        spanish: str | None = None,
        allow_alignment: bool = False,
        translation_register: str = "expository",
        discourse_tag: str = "statement",
        verse_detected: dict | None = None,
    ) -> None:
        """Fallback: release a suppressed translation after DEFERRED_RELEASE_S if no segmentation repair arrived.

        Called when display_ready was false. If caption_merge fires first as segmentation repair, this task
        is cancelled. If no merge arrives within the timeout, the best available
        translation is emitted so the caption is not permanently blank.
        """
        try:
            await asyncio.sleep(DEFERRED_RELEASE_S)
            if ts in self._deferred_updates:
                del self._deferred_updates[ts]
                logger.info(
                    "[enrichment:%s] decision=deferred_translation_released ts=%d "
                    "(no merge in %.1fs)",
                    self._church_id, ts, DEFERRED_RELEASE_S,
                )
                # Always emit a release event when we time out a deferred caption,
                # even if the text matches Google's original output. The client
                # uses this event to clear pending-completion UI state.
                release_text = _format_deferred_release_text(english, google_english)
                if release_text:
                    try:
                        await self._on_translation_update(ts, release_text, phrase_alignment)
                        if allow_alignment and spanish:
                            self._schedule_phrase_alignment(
                                ts=ts,
                                spanish=spanish,
                                english=release_text,
                                google_english=google_english,
                                source_quality="clean",
                                translation_register=translation_register,
                                discourse_tag=discourse_tag,
                                verse_detected=verse_detected,
                                merge_with_previous=False,
                            )
                    except Exception as e:
                        logger.warning(
                            "[enrichment:%s] deferred translation_update failed ts=%d: %s",
                            self._church_id, ts, e,
                        )
        except asyncio.CancelledError:
            pass

    async def _run_verse_suggestions(
        self,
        spanish: str,
        google_english: str,
        ts: int,
        topic_context: str,
        active_passage: dict | None,
        shown: set[str],
    ) -> None:
        """Separate lightweight async call for verse suggestions.

        Runs after (and independent of) the main enrichment call so it cannot
        compete with structural decisions for prompt attention or cause delays.
        """
        parts: list[str] = []
        if topic_context:
            parts.append(f"[SERMON CONTEXT]\n{topic_context}")
        if active_passage:
            parts.append(
                f"[ACTIVE PASSAGE]\n"
                f"{active_passage['reference']} — {active_passage['canonical_english']}"
            )
        if shown:
            parts.append(f"[ALREADY SUGGESTED]\n{', '.join(sorted(shown))}")
        parts.append(f"[SOURCE]\n{spanish}")
        parts.append(f"[TRANSLATION]\n{google_english}")
        user_msg = "\n\n".join(parts)

        try:
            response = await self._client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=400,
                temperature=0,
                system=_VERSE_SUGGESTIONS_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = response.content[0].text.strip()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "[enrichment:%s] Verse suggestions call failed ts=%d: %s",
                self._church_id, ts, e,
            )
            return

        suggestions = _extract_suggestions(raw, self._church_id, ts)
        if suggestions is None:
            return

        async with self._mutation_lock:
            active_ref = (self._active_passage or {}).get("reference")
            valid = []
            suppressed = []
            for s in suggestions:
                if not _is_valid_suggestion(s):
                    continue
                if s["reference"] in self._shown_suggestions:
                    suppressed.append((s["reference"], "already shown"))
                elif s["reference"] == active_ref:
                    suppressed.append((s["reference"], "active passage"))
                else:
                    valid.append(s)

            if suppressed:
                logger.debug(
                    "[enrichment:%s] Suggestions suppressed ts=%d: %s",
                    self._church_id, ts,
                    ", ".join(f"{ref} ({reason})" for ref, reason in suppressed),
                )

            if valid:
                for s in valid:
                    self._shown_suggestions.add(s["reference"])
                logger.info(
                    "[enrichment:%s] Verse suggestions ts=%d: %s",
                    self._church_id, ts,
                    [(s["reference"], s["relevance_note"][:50]) for s in valid],
                )
                try:
                    await self._on_verse_suggestion(ts, valid)
                    await save_verse_suggestions(self._session_id, ts, valid)
                except Exception as e:
                    logger.warning(
                        "[enrichment:%s] on_verse_suggestion failed ts=%d: %s",
                        self._church_id, ts, e,
                    )
            else:
                logger.info("[enrichment:%s] No verse suggestions ts=%d", self._church_id, ts)

    # --- Verse scratch pad ---

    async def _handle_verse_detection(
        self, verse: dict, audio_start: float, audio_end: float, ts: int
    ) -> None:
        """Add a detection to the scratch pad; flush if the passage has changed.

        Quoted detections are skipped during illustration mode — story vocabulary
        frequently produces false positives. Explicit chapter announcements are
        always accepted regardless of mode.
        """
        if (self._state_tracker
                and self._state_tracker.is_narrative()
                and verse.get("confidence") == "quoted"):
            logger.info(
                "[enrichment:%s] Verse detection skipped ts=%d — quoted during illustration",
                self._church_id, ts,
            )
            return

        entry = VerseScratchEntry(
            book=verse["book"],
            chapter=verse["chapter"],
            verse_start=verse["verse_start"],
            verse_end=verse.get("verse_end"),
            confidence=verse["confidence"],
            reference=verse["reference"],
            canonical_english=verse["canonical_english"],
            spanish_text=verse["spanish_text"],
            audio_start=audio_start,
            audio_end=audio_end,
            ts=ts,
        )

        if not self._verse_scratch:
            # First detection for this passage — emit immediately so the client
            # knows what's being read without waiting for the passage to change.
            logger.info(
                "[enrichment:%s] Scratch pad started: %s — emitting tentative verse_detected",
                self._church_id, entry.reference,
            )
            self._verse_scratch.append(entry)
            tentative = _consolidate_scratch(self._verse_scratch)
            try:
                await self._on_verse_detected(ts, tentative)
                await save_verse_detection(self._session_id, ts, tentative)
            except Exception as e:
                logger.warning("[enrichment:%s] on_verse_detected (tentative) failed: %s", self._church_id, e)
            return

        last = self._verse_scratch[-1]
        same_chapter = (last.book == entry.book and last.chapter == entry.chapter)
        # Use STT audio timeline for gap — unaffected by server processing lag
        audio_gap = entry.audio_start - last.audio_end
        within_gap = audio_gap <= VERSE_GAP_THRESHOLD_S

        if same_chapter and within_gap:
            # Still in the same passage — extend the range and push an update to the client.
            self._verse_scratch.append(entry)
            running_range = _scratch_range_str(self._verse_scratch)
            logger.info(
                "[enrichment:%s] Scratch pad extended: %s → %s (gap=%.1fs, %d entry(s))",
                self._church_id, entry.reference, running_range,
                audio_gap, len(self._verse_scratch),
            )
            updated = _consolidate_scratch(self._verse_scratch)
            try:
                await self._on_verse_range_update(self._verse_scratch[0].ts, updated)
            except Exception as e:
                logger.warning("[enrichment:%s] on_verse_range_update failed: %s", self._church_id, e)
        else:
            # Passage changed or gap too large — emit the accumulated range and start fresh
            reason = (
                f"gap {audio_gap:.1f}s > {VERSE_GAP_THRESHOLD_S:.0f}s"
                if same_chapter
                else f"passage change ({last.book} {last.chapter} → {entry.book} {entry.chapter})"
            )
            logger.info(
                "[enrichment:%s] Scratch pad flushing (%s)",
                self._church_id, reason,
            )
            await self._flush_scratch("passage change" if not same_chapter else f"gap {audio_gap:.1f}s")
            self._verse_scratch = [entry]
            logger.debug(
                "[enrichment:%s] Scratch pad restarted: %s",
                self._church_id, entry.reference,
            )

    async def _flush_scratch(self, reason: str = "unknown") -> None:
        """Emit the final consolidated verse range when the passage ends.

        The initial verse_detected was already fired when the first entry arrived.
        This flush sends a verse_range_update with the fully consolidated range so
        the client can replace the tentative entry with the accurate span.
        """
        if not self._verse_scratch:
            return
        merged = _consolidate_scratch(self._verse_scratch)
        self._verse_scratch = []
        ts = merged["ts"]
        logger.info(
            "[enrichment:%s] Verse range finalised: %s | reason=%s | "
            "audio=%.1f–%.1fs | %d detection(s) | confidence=%s",
            self._church_id, merged["reference"], reason,
            merged["audio_start"], merged["audio_end"],
            merged["_count"], merged["confidence"],
        )
        try:
            await self._on_verse_range_update(ts, merged)
        except Exception as e:
            logger.warning("[enrichment:%s] on_verse_range_update (flush) failed: %s", self._church_id, e)

    async def close(self) -> None:
        # Flush any pending verse range before shutting down
        await self._flush_scratch("session close")
        # Cancel deferred translation updates
        for _, (_, defer_task) in list(self._deferred_updates.items()):
            defer_task.cancel()
        self._deferred_updates.clear()
        self._last_emitted_translation.clear()
        self._tasks = [task for task in self._tasks if not task.done()]
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()


def _consolidate_scratch(entries: list[VerseScratchEntry]) -> dict:
    """Merge same-chapter scratch entries into a single verse range dict."""
    starts = [e.verse_start for e in entries if isinstance(e.verse_start, int)]
    ends   = [e.verse_end or e.verse_start for e in entries if isinstance(e.verse_start, int)]
    v_start = min(starts) if starts else entries[0].verse_start
    v_end_raw = max(ends) if ends else None
    v_end = v_end_raw if (v_end_raw and v_end_raw != v_start) else None

    base = entries[0]
    ref = f"{base.book} {base.chapter}:{v_start}"
    if v_end:
        ref += f"\u2013{v_end}"  # en-dash range: "1 John 1:5–7"

    return {
        "book": base.book,
        "chapter": base.chapter,
        "verse_start": v_start,
        "verse_end": v_end,
        "reference": ref,
        "canonical_english": base.canonical_english,
        "spanish_text": entries[-1].spanish_text,  # most recently quoted text
        "confidence": "explicit" if any(e.confidence == "explicit" for e in entries) else "quoted",
        "ts": base.ts,
        "audio_start": base.audio_start,
        "audio_end": entries[-1].audio_end,
        "_count": len(entries),
    }


def _scratch_range_str(entries: list[VerseScratchEntry]) -> str:
    """Return a human-readable range for the current scratch pad, e.g. '1 John 1:1–5'."""
    if not entries:
        return "(empty)"
    starts = [e.verse_start for e in entries if isinstance(e.verse_start, int)]
    ends   = [e.verse_end or e.verse_start for e in entries if isinstance(e.verse_start, int)]
    base = entries[0]
    lo = min(starts) if starts else base.verse_start
    hi = max(ends) if ends else lo
    ref = f"{base.book} {base.chapter}:{lo}"
    if hi != lo:
        ref += f"\u2013{hi}"
    return ref


def _describe_invalid_verse(v: dict) -> str:
    """Return a short description of why a verse dict failed validation."""
    problems = []
    if not (isinstance(v.get("book"), str) and v.get("book")):
        problems.append(f"book={v.get('book')!r}")
    if not isinstance(v.get("chapter"), int):
        problems.append(f"chapter={v.get('chapter')!r}")
    if not isinstance(v.get("verse_start"), int):
        problems.append(f"verse_start={v.get('verse_start')!r}")
    if not (isinstance(v.get("reference"), str) and v.get("reference")):
        problems.append(f"reference={v.get('reference')!r}")
    if not (isinstance(v.get("canonical_english"), str) and v.get("canonical_english")):
        problems.append(f"canonical_english missing")
    if not isinstance(v.get("spanish_text"), str):
        problems.append(f"spanish_text missing")
    if v.get("confidence") not in ("explicit", "quoted"):
        problems.append(f"confidence={v.get('confidence')!r}")
    return "; ".join(problems) if problems else "unknown"


def _is_valid_verse(v: dict) -> bool:
    return (
        isinstance(v.get("book"), str) and v["book"]
        and isinstance(v.get("chapter"), int)
        and isinstance(v.get("verse_start"), int)
        and isinstance(v.get("reference"), str) and v["reference"]
        and isinstance(v.get("canonical_english"), str) and v["canonical_english"]
        and isinstance(v.get("spanish_text"), str)
        and v.get("confidence") in ("explicit", "quoted")
    )


def _is_valid_suggestion(s: dict) -> bool:
    return (
        isinstance(s.get("reference"), str) and s["reference"]
        and isinstance(s.get("canonical_english"), str) and s["canonical_english"]
        and isinstance(s.get("relevance_note"), str)
    )


def _extract_suggestions(raw: str, church_id: str, ts: int) -> list[dict] | None:
    """Parse the verse suggestions response.

    Expects {"suggestions": [...]} but falls back to a bare array for robustness.
    Strips markdown fences, then attempts JSON extraction via regex if direct parse fails.
    Returns None on unrecoverable parse failure (caller should skip the call).
    Returns an empty list if the model returned a valid empty response.
    """
    # Strip markdown fences
    text = raw
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()

    # Attempt 1: direct parse
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "suggestions" in parsed:
            result = parsed["suggestions"]
            if isinstance(result, list):
                return result
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    # Attempt 2: extract first JSON object containing "suggestions"
    m = re.search(r'\{[^{}]*"suggestions"\s*:\s*\[.*?\]\s*\}', text, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(0))
            result = parsed.get("suggestions", [])
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # Attempt 3: extract bare JSON array
    m = re.search(r'\[.*?\]', text, re.DOTALL)
    if m:
        try:
            result = json.loads(m.group(0))
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    logger.warning(
        "[enrichment:%s] Verse suggestions parse failed ts=%d: %.80s",
        church_id, ts, raw,
    )
    return None
