def build_system_prompt(church_terms: dict[str, str]) -> str:
    """Build the static base system prompt with church-specific terminology.

    Topic context and rolling sermon summary are injected dynamically per
    call in TranslationService — they change during the session so they
    cannot be baked into this static prompt.
    """
    if church_terms:
        terms_lines = "\n".join(f"  {k} → {v}" for k, v in church_terms.items())
        glossary_block = f"THEOLOGICAL GLOSSARY — always use these exact translations:\n{terms_lines}"
    else:
        glossary_block = ""

    return f"""You are a professional simultaneous interpreter translating a live Spanish church sermon to English.

{glossary_block}

RULES:
1. Output ONLY the English translation. No notes, brackets, or explanations.
2. Preserve the preaching tone — declarative, present tense, active voice where natural.
3. Preserve sentence fragments as fragments. Do not complete partial sentences.
4. Preserve repetition and exclamations exactly — never substitute synonyms.
5. If the speaker uses an English word mid-sentence, pass it through unchanged.
6. When a term appears in the glossary, use that translation without exception."""


def build_user_message(
    current_text: str,
    recent_utterances: list[str],
    topic_context: str,
) -> str:
    """Build the per-call user message.

    Structure (research-backed):
    - Topic context first — sets the frame for disambiguation
    - Recent verbatim utterances — gives pronoun/term continuity
    - Source text last — model's next token is the translation

    The source text is placed at the very end with nothing after it so the
    model's natural continuation is the English translation.
    """
    parts: list[str] = []

    if topic_context:
        parts.append(f"[SERMON CONTEXT]\n{topic_context}")

    if recent_utterances:
        lines = "\n".join(f'  "{u}"' for u in recent_utterances)
        parts.append(f"[RECENT UTTERANCES — for reference only, do not translate]\n{lines}")

    parts.append(f"[TRANSLATE THIS]\n{current_text}")

    return "\n\n".join(parts)
