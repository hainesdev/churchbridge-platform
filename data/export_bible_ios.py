"""
Export a compact bible_ios.sqlite for iOS bundling from the main churchbridge.db.
Includes only ASV, KJV, and RVR1960. No FTS indexes, no other tables.

Output: ChurchBridgeTranslation/bible_ios.sqlite (~15-20 MB)
"""

import os
import sqlite3

SRC = os.path.join(os.path.dirname(__file__), "churchbridge.db")
DST = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "macOS-ios-dev", "shared", "churchbridge-ios",
    "ChurchBridgeTranslation", "bible_ios.sqlite"
)
DST = os.path.normpath(DST)

EXPORT_SLUGS = ("asv", "kjv", "rvr1960")

print(f"Source: {SRC}")
print(f"Output: {DST}")

if os.path.exists(DST):
    os.remove(DST)

src = sqlite3.connect(SRC)
dst = sqlite3.connect(DST)
src.row_factory = sqlite3.Row

dst.executescript("""
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
""")

# --- bible_versions (remap IDs to 1..N) ---
src_cur = src.execute(
    "SELECT id, slug, name, language_code FROM bible_versions "
    "WHERE slug IN ('asv','kjv','rvr1960') ORDER BY slug"
)
id_map = {}  # old_id -> new_id
for new_id, row in enumerate(src_cur.fetchall(), start=1):
    id_map[row["id"]] = new_id
    dst.execute(
        "INSERT INTO bible_versions VALUES (?,?,?,?)",
        (new_id, row["slug"], row["name"], row["language_code"])
    )
print(f"  Versions: {list(id_map.items())}")

# --- bible_books (canonical, all 66) ---
for row in src.execute(
    "SELECT book_order, osis_id, canonical_name, testament FROM bible_books ORDER BY book_order"
).fetchall():
    dst.execute(
        "INSERT INTO bible_books VALUES (?,?,?,?)",
        (row["book_order"], row["osis_id"], row["canonical_name"], row["testament"])
    )
print("  Books: 66 rows")

# --- bible_verses (only the 3 versions, minimal columns) ---
rows = src.execute("""
    SELECT
        bv.version_id,
        bb.book_order  AS book_id,
        bv.chapter_num AS chapter,
        bv.verse_num   AS verse,
        TRIM(bv.text)  AS text
    FROM bible_verses bv
    JOIN bible_versions v  ON bv.version_id      = v.id
    JOIN bible_books    bb ON bv.canonical_book_id = bb.id
    WHERE v.slug IN ('asv','kjv','rvr1960')
    ORDER BY bv.version_id, bb.book_order, bv.chapter_num, bv.verse_num
""").fetchall()

batch = [
    (id_map[r["version_id"]], r["book_id"], r["chapter"], r["verse"], r["text"])
    for r in rows
]
dst.executemany("INSERT INTO bible_verses VALUES (?,?,?,?,?)", batch)
print(f"  Verses: {len(batch)} rows")

# --- index for fast chapter lookups ---
dst.execute(
    "CREATE INDEX idx_chapter ON bible_verses (version_id, book_id, chapter, verse)"
)

dst.commit()
dst.execute("VACUUM")
dst.commit()

src.close()
dst.close()

size_mb = os.path.getsize(DST) / 1024 / 1024
print(f"\nDone. File size: {size_mb:.1f} MB  →  {DST}")
