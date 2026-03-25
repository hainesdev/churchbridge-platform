import asyncio
import logging
import os

logger = logging.getLogger(__name__)


async def get_or_create_glossary(church_id: str, terms: dict[str, str]) -> str | None:
    """Return a DeepL glossary_id for church-specific terminology.

    Creates the glossary on first call. Reuses an existing glossary if one with
    the same name already exists (identified by `churchbridge_{church_id}`).
    Returns None if no terms are provided or if the DeepL SDK call fails.
    """
    if not terms:
        return None

    api_key = os.environ.get("DEEPL_API_KEY")
    if not api_key:
        return None

    def _sync() -> str | None:
        import deepl  # lazy import — only required for glossary management
        translator = deepl.Translator(api_key)
        name = f"churchbridge_{church_id}"

        # Reuse existing glossary to avoid hitting creation limits
        for g in translator.list_glossaries():
            if g.name == name:
                logger.info(
                    "[glossary] Reusing existing glossary %s for church %s",
                    g.glossary_id, church_id,
                )
                return g.glossary_id

        glossary = translator.create_glossary(
            name=name,
            source_lang="ES",
            target_lang="EN-US",
            entries=deepl.GlossaryEntries(entries=terms),
        )
        logger.info(
            "[glossary] Created glossary %s for church %s (%d terms)",
            glossary.glossary_id, church_id, len(terms),
        )
        return glossary.glossary_id

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        logger.warning(
            "[glossary] Could not create glossary for church %s: %s — continuing without glossary",
            church_id, e,
        )
        return None
