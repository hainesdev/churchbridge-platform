import sqlite3
import sys
from pathlib import Path


DB_PATH = Path("data/churchbridge.db")


QUERIES = [
    (
        "version_counts",
        """
        SELECT slug, language_code, book_count, chapter_count, verse_count
        FROM bible_versions
        ORDER BY slug
        """,
    ),
    (
        "duplicate_file_hashes",
        """
        SELECT file_sha256, COUNT(*) AS versions, GROUP_CONCAT(slug, ', ')
        FROM bible_versions
        GROUP BY file_sha256
        HAVING COUNT(*) > 1
        """,
    ),
    (
        "john_3_16",
        """
        SELECT v.slug, b.text
        FROM bible_verses b
        JOIN bible_versions v ON v.id = b.version_id
        WHERE b.canonical_book_name = 'John'
          AND b.chapter_num = 3
          AND b.verse_num = 16
        ORDER BY v.slug
        """,
    ),
    (
        "genesis_1_1",
        """
        SELECT v.slug, b.canonical_book_name, b.book_name, b.text
        FROM bible_verses b
        JOIN bible_versions v ON v.id = b.version_id
        WHERE b.canonical_book_name = 'Genesis'
          AND b.chapter_num = 1
          AND b.verse_num = 1
        ORDER BY v.slug
        """,
    ),
    (
        "version_first_last_reference",
        """
        SELECT
            v.slug,
            (
                SELECT b1.reference
                FROM bible_verses b1
                WHERE b1.version_id = v.id
                ORDER BY b1.book_order, b1.chapter_num, b1.verse_num
                LIMIT 1
            ) AS first_reference,
            (
                SELECT b2.reference
                FROM bible_verses b2
                WHERE b2.version_id = v.id
                ORDER BY b2.book_order DESC, b2.chapter_num DESC, b2.verse_num DESC
                LIMIT 1
            ) AS last_reference
        FROM bible_versions v
        ORDER BY v.slug
        """,
    ),
    (
        "source_vs_canonical_order_samples",
        """
        SELECT v.slug, b.source_book_order, b.book_order, b.book_name, b.canonical_book_name
        FROM bible_verses b
        JOIN bible_versions v ON v.id = b.version_id
        WHERE b.chapter_num = 1
          AND b.verse_num = 1
          AND (b.source_book_order IN (1, 66) OR b.book_order IN (1, 66))
        ORDER BY v.slug, b.source_book_order, b.book_order
        """,
    ),
]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    with sqlite3.connect(DB_PATH) as conn:
        for label, query in QUERIES:
            print(f"== {label} ==")
            cur = conn.execute(query)
            rows = cur.fetchall()
            for row in rows:
                print(row)
            if not rows:
                print("(no rows)")
            print()


if __name__ == "__main__":
    main()
