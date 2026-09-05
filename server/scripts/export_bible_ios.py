"""Export a compact bible_ios.sqlite for the iOS app.

Reads the main ChurchBridge database and writes a minimal, read-only Bible
database containing only ASV, KJV, and RVR1960 — no FTS indexes, no session
data, no other tables. Roughly 15-20 MB.

The output is written into the static directory that the API serves at
`/api/static/bible_ios.sqlite`, which is where the iOS app downloads it from.
That directory lives beside the database on the persistent volume, so the file
survives container rebuilds.

Run inside the API container after a Bible import, or any time the corpus
changes:

    docker exec deploy-api-1 python -m server.scripts.export_bible_ios
"""

import os
import sqlite3

DB_PATH = os.getenv("DATABASE_URL", "data/churchbridge.db").replace("sqlite:///./", "")
STATIC_DIR = os.getenv(
    "STATIC_DIR", os.path.join(os.path.dirname(DB_PATH) or ".", "static")
)
DST = os.path.join(STATIC_DIR, "bible_ios.sqlite")

EXPORT_SLUGS = ("asv", "kjv", "rvr1960")


def main() -> None:
    os.makedirs(STATIC_DIR, exist_ok=True)
    print(f"Source: {DB_PATH}")
    print(f"Output: {DST}")

    tmp = DST + ".tmp"
    for path in (tmp, tmp + "-journal"):
        if os.path.exists(path):
            os.remove(path)

    src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(tmp)

    dst.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous  = OFF;

        CREATE TABLE bible_versions (
            id            INTEGER PRIMARY KEY,
            slug          TEXT    NOT NULL UNIQUE,
            name          TEXT    NOT NULL,
            language_code TEXT    NOT NULL
        );

        CREATE TABLE bible_books (
            id             INTEGER PRIMARY KEY,
            osis_id        TEXT    NOT NULL,
            canonical_name TEXT    NOT NULL UNIQUE,
            testament      TEXT    NOT NULL
        );

        CREATE TABLE bible_verses (
            version_id INTEGER NOT NULL,
            book_id    INTEGER NOT NULL,
            chapter    INTEGER NOT NULL,
            verse      INTEGER NOT NULL,
            text       TEXT    NOT NULL
        );
        """
    )

    placeholders = ",".join("?" for _ in EXPORT_SLUGS)

    id_map: dict[int, int] = {}
    rows = src.execute(
        "SELECT id, slug, name, language_code FROM bible_versions "
        f"WHERE slug IN ({placeholders}) ORDER BY slug",
        EXPORT_SLUGS,
    ).fetchall()
    if not rows:
        raise SystemExit(
            "No matching Bible versions found. Import the corpus before exporting."
        )
    for new_id, row in enumerate(rows, start=1):
        id_map[row["id"]] = new_id
        dst.execute(
            "INSERT INTO bible_versions VALUES (?,?,?,?)",
            (new_id, row["slug"], row["name"], row["language_code"]),
        )
    print(f"  Versions: {[r['slug'] for r in rows]}")

    book_rows = src.execute(
        "SELECT book_order, osis_id, canonical_name, testament "
        "FROM bible_books ORDER BY book_order"
    ).fetchall()
    dst.executemany(
        "INSERT INTO bible_books VALUES (?,?,?,?)",
        [
            (r["book_order"], r["osis_id"], r["canonical_name"], r["testament"])
            for r in book_rows
        ],
    )
    print(f"  Books: {len(book_rows)} rows")

    verse_rows = src.execute(
        f"""
        SELECT
            bv.version_id,
            bb.book_order  AS book_id,
            bv.chapter_num AS chapter,
            bv.verse_num   AS verse,
            TRIM(bv.text)  AS text
        FROM bible_verses bv
        JOIN bible_versions v  ON bv.version_id        = v.id
        JOIN bible_books    bb ON bv.canonical_book_id = bb.id
        WHERE v.slug IN ({placeholders})
        ORDER BY bv.version_id, bb.book_order, bv.chapter_num, bv.verse_num
        """,
        EXPORT_SLUGS,
    ).fetchall()
    dst.executemany(
        "INSERT INTO bible_verses VALUES (?,?,?,?,?)",
        [
            (id_map[r["version_id"]], r["book_id"], r["chapter"], r["verse"], r["text"])
            for r in verse_rows
        ],
    )
    print(f"  Verses: {len(verse_rows)} rows")

    dst.execute(
        "CREATE INDEX idx_chapter ON bible_verses (version_id, book_id, chapter, verse)"
    )
    dst.commit()
    dst.execute("VACUUM")
    dst.commit()
    src.close()
    dst.close()

    # Swap into place only once the export has fully succeeded, so a failed run
    # never leaves clients downloading a truncated database.
    os.replace(tmp, DST)
    print(f"\nDone. {os.path.getsize(DST) / 1024 / 1024:.1f} MB -> {DST}")


if __name__ == "__main__":
    main()
