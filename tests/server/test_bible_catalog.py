"""
Tests for Bible book-name resolution.

Book names reach the catalog from LLM verse detection over Spanish speech, from
the iOS app, and from hand-typed lookups, so resolution has to tolerate the
spelling each of those produces.

Run with:  python -m pytest tests/server/test_bible_catalog.py -v
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import pytest

from server.db.bible_catalog import (
    BOOK_ALIAS_MAP,
    BOOK_METADATA,
    canonicalize_book_name,
    parse_reference,
)


# ---------------------------------------------------------------------------
# The regression this file exists for
# ---------------------------------------------------------------------------

def test_psalms_resolves_from_the_singular_form():
    """"Psalm 42:8" is how the reference is normally written.

    The catalog only held the plural "Psalms", so the singular raised
    KeyError and the app showed nothing for the passage.
    """
    assert canonicalize_book_name("Psalm")[1] == "Psalms"
    assert canonicalize_book_name("Salmo")[1] == "Psalms"


def test_psalm_reference_parses():
    parsed = parse_reference("Psalm 42:8")
    assert parsed["book"] == "Psalms"
    assert parsed["chapter"] == 42
    assert parsed["verse_start"] == 8


# ---------------------------------------------------------------------------
# Spelling tolerance
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "written,expected",
    [
        ("Psalms", "Psalms"),
        ("psalms", "Psalms"),
        ("PSALMS", "Psalms"),
        ("  Psalms  ", "Psalms"),
        ("Ps", "Psalms"),
        ("Sal", "Psalms"),
        ("Salmos", "Psalms"),
        ("Juan", "John"),
        ("S. Juan", "John"),
        ("S.Juan", "John"),
        ("1 Juan", "1 John"),
        ("1Juan", "1 John"),
        ("Génesis", "Genesis"),
        ("Apocalipsis", "Revelation"),
    ],
)
def test_book_names_resolve_regardless_of_case_spacing_or_punctuation(written, expected):
    assert canonicalize_book_name(written)[1] == expected


@pytest.mark.parametrize("junk", ["", "   ", "Nonsense Book", "42"])
def test_unknown_book_names_still_raise(junk):
    with pytest.raises(KeyError):
        canonicalize_book_name(junk)


# ---------------------------------------------------------------------------
# Catalog integrity
# ---------------------------------------------------------------------------

def test_every_alias_points_at_a_real_book():
    for alias, book_id in BOOK_ALIAS_MAP.items():
        assert book_id in BOOK_METADATA, f"{alias!r} maps to unknown id {book_id}"


def test_every_canonical_name_resolves_to_itself():
    """A round trip through the resolver must be stable for all 66 books."""
    for book_id, meta in BOOK_METADATA.items():
        name = meta["canonical_name"]
        resolved_id, resolved_name = canonicalize_book_name(name)
        assert resolved_id == book_id
        assert resolved_name == name


def test_reference_parsing_handles_ranges_and_spanish_books():
    assert parse_reference("Juan 3:16")["book"] == "John"
    ranged = parse_reference("Salmos 42:8-11")
    assert ranged["book"] == "Psalms"
    assert ranged["verse_start"] == 8
    assert ranged["verse_end"] == 11
