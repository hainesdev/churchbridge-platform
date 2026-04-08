import re

_DEVIATION_TOKEN_RE = re.compile(r"[a-z0-9']+")
_DEVIATION_TOKEN_CANONICAL = {
    "text": "passage",
    "texts": "passage",
    "passage": "passage",
    "passages": "passage",
}


def deviation_tokens(text: str) -> set[str]:
    tokens = {
        _DEVIATION_TOKEN_CANONICAL.get(token, token)
        for token in _DEVIATION_TOKEN_RE.findall(text.lower())
    }
    return {token for token in tokens if token}


def translation_deviation_score(reference: str, candidate: str) -> float:
    """Return a conservative word-level similarity score for two English renderings.

    The canonicalization layer is intentionally tiny and church-domain-aware so
    equivalent sermon-register wording like "text" and "passage" does not look
    like a harmful semantic divergence.
    """
    reference_words = deviation_tokens(reference)
    candidate_words = deviation_tokens(candidate)
    if not reference_words and not candidate_words:
        return 1.0
    union = reference_words | candidate_words
    intersection = reference_words & candidate_words
    return len(intersection) / len(union)
