from __future__ import annotations

from server.db.bible_catalog import canonicalize_book_name, parse_reference
from server.db.index import get_db


async def list_bible_versions() -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT slug, name, language_code, source_url, license_note,
                   book_count, chapter_count, verse_count, imported_at
            FROM bible_versions
            ORDER BY language_code, name
            """
        )
        rows = await cursor.fetchall()
    return [
        {
            "slug": r[0],
            "name": r[1],
            "language_code": r[2],
            "source_url": r[3],
            "license_note": r[4],
            "book_count": r[5],
            "chapter_count": r[6],
            "verse_count": r[7],
            "imported_at": r[8],
        }
        for r in rows
    ]


async def list_books_for_version(version_slug: str) -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT DISTINCT
                b.canonical_book_id,
                b.canonical_book_name,
                b.book_order,
                bb.osis_id,
                bb.testament,
                MAX(b.chapter_num) AS chapter_count
            FROM bible_verses b
            JOIN bible_versions v ON v.id = b.version_id
            JOIN bible_books bb ON bb.id = b.canonical_book_id
            WHERE v.slug = ?
            GROUP BY b.canonical_book_id, b.canonical_book_name, b.book_order, bb.osis_id, bb.testament
            ORDER BY b.book_order
            """,
            (version_slug,),
        )
        rows = await cursor.fetchall()
    return [
        {
            "book_id": r[0],
            "book_name": r[1],
            "book_order": r[2],
            "osis_id": r[3],
            "testament": r[4],
            "chapter_count": r[5],
        }
        for r in rows
    ]


async def get_chapter(version_slug: str, book: str, chapter: int) -> dict | None:
    canonical_book_id, canonical_book_name = canonicalize_book_name(book)
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT v.slug, v.name, b.canonical_book_name, b.chapter_num,
                   b.verse_num, b.text, b.reference
            FROM bible_verses b
            JOIN bible_versions v ON v.id = b.version_id
            WHERE v.slug = ?
              AND b.canonical_book_id = ?
              AND b.chapter_num = ?
            ORDER BY b.verse_num
            """,
            (version_slug, canonical_book_id, chapter),
        )
        rows = await cursor.fetchall()
    if not rows:
        return None
    return {
        "version": {"slug": rows[0][0], "name": rows[0][1]},
        "book": canonical_book_name,
        "chapter": chapter,
        "verses": [
            {
                "verse": r[4],
                "text": r[5],
                "reference": r[6],
            }
            for r in rows
        ],
    }


async def get_passage(
    version_slug: str,
    book: str,
    chapter: int,
    verse_start: int,
    verse_end: int | None = None,
) -> dict | None:
    canonical_book_id, canonical_book_name = canonicalize_book_name(book)
    verse_end = verse_end or verse_start
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT v.slug, v.name, b.verse_num, b.text, b.reference
            FROM bible_verses b
            JOIN bible_versions v ON v.id = b.version_id
            WHERE v.slug = ?
              AND b.canonical_book_id = ?
              AND b.chapter_num = ?
              AND b.verse_num BETWEEN ? AND ?
            ORDER BY b.verse_num
            """,
            (version_slug, canonical_book_id, chapter, verse_start, verse_end),
        )
        rows = await cursor.fetchall()
    if not rows:
        return None
    return {
        "version": {"slug": rows[0][0], "name": rows[0][1]},
        "book": canonical_book_name,
        "chapter": chapter,
        "verse_start": verse_start,
        "verse_end": verse_end,
        "reference": f"{canonical_book_name} {chapter}:{verse_start}" if verse_start == verse_end else f"{canonical_book_name} {chapter}:{verse_start}-{verse_end}",
        "verses": [{"verse": r[2], "text": r[3], "reference": r[4]} for r in rows],
    }


async def search_bible_text(version_slug: str, query: str, limit: int = 10) -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT v.slug, b.canonical_book_name, b.chapter_num, b.verse_num, b.reference, b.text
            FROM bible_verses_fts f
            JOIN bible_verses b ON b.id = f.rowid
            JOIN bible_versions v ON v.id = b.version_id
            WHERE v.slug = ?
              AND bible_verses_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (version_slug, query, limit),
        )
        rows = await cursor.fetchall()
    return [
        {
            "version_slug": r[0],
            "book": r[1],
            "chapter": r[2],
            "verse": r[3],
            "reference": r[4],
            "text": r[5],
        }
        for r in rows
    ]


async def get_passage_by_reference(version_slug: str, reference: str) -> dict | None:
    parsed = parse_reference(reference)
    return await get_passage(
        version_slug,
        parsed["book"],
        parsed["chapter"],
        parsed["verse_start"],
        parsed["verse_end"],
    )
