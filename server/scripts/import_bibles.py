import argparse
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from server.db.bible_catalog import BOOK_METADATA, CANONICAL_BOOKS, canonicalize_book_name


DB_PATH = Path("data/churchbridge.db")
SOURCE_ROOT = Path("data/source_bibles")


@dataclass(frozen=True)
class VersionSpec:
    filename: str
    source_key: str
    slug: str
    name: str
    language_code: str
    source_url: str
    license_note: str


VERSION_SPECS = [
    VersionSpec(
        filename="ASV.json",
        source_key="dscottpi:ASV.json",
        slug="asv",
        name="American Standard Version",
        language_code="en",
        source_url="https://github.com/dscottpi/bibles/blob/master/ASV.json",
        license_note="User-approved source for local testing/import.",
    ),
    VersionSpec(
        filename="KJV.json",
        source_key="dscottpi:KJV.json",
        slug="kjv",
        name="King James Version",
        language_code="en",
        source_url="https://github.com/dscottpi/bibles/blob/master/KJV.json",
        license_note="User-approved source for local testing/import.",
    ),
    VersionSpec(
        filename="WEB.json",
        source_key="dscottpi:WEB.json",
        slug="web",
        name="World English Bible",
        language_code="en",
        source_url="https://github.com/dscottpi/bibles/blob/master/WEB.json",
        license_note="User-approved source for local testing/import.",
    ),
    VersionSpec(
        filename="RVA.json",
        source_key="dscottpi:RVA.json",
        slug="rva",
        name="Reina-Valera Antigua",
        language_code="es",
        source_url="https://github.com/dscottpi/bibles/blob/master/RVA.json",
        license_note="User-approved source for local testing/import.",
    ),
    VersionSpec(
        filename="RVR1960 - Spanish.json",
        source_key="dscottpi:RVR1960-spanish.json",
        slug="rvr1960",
        name="Reina-Valera 1960",
        language_code="es",
        source_url="https://github.com/dscottpi/bibles/blob/master/RVR1960%20-%20Spanish.json",
        license_note="User-approved source for local testing/import.",
    ),
    VersionSpec(
        filename="RVR1960-Spanish.json",
        source_key="dscottpi:RVR1960-spanish-dup.json",
        slug="rvr1960_dup",
        name="Reina-Valera 1960 (Duplicate Source File)",
        language_code="es",
        source_url="https://github.com/dscottpi/bibles/blob/master/RVR1960-Spanish.json",
        license_note="User-approved source for local testing/import. Duplicate file retained intentionally.",
    ),
    VersionSpec(
        filename="CARS - Russian.json",
        source_key="dscottpi:CARS-russian.json",
        slug="cars",
        name="Contemporary Russian Translation",
        language_code="ru",
        source_url="https://github.com/dscottpi/bibles/blob/master/CARS%20-%20Russian.json",
        license_note="User-approved source for local testing/import.",
    ),
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_NORMALIZE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    return _NORMALIZE_RE.sub(" ", text.strip())


def load_version_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bible_versions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            source_key        TEXT NOT NULL UNIQUE,
            slug              TEXT NOT NULL UNIQUE,
            name              TEXT NOT NULL,
            language_code     TEXT NOT NULL,
            source_url        TEXT NOT NULL,
            license_note      TEXT,
            import_path       TEXT NOT NULL,
            file_sha256       TEXT NOT NULL,
            book_count        INTEGER NOT NULL DEFAULT 0,
            chapter_count     INTEGER NOT NULL DEFAULT 0,
            verse_count       INTEGER NOT NULL DEFAULT 0,
            imported_at       TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bible_books (
            id                INTEGER PRIMARY KEY,
            osis_id           TEXT NOT NULL UNIQUE,
            canonical_name    TEXT NOT NULL UNIQUE,
            book_order        INTEGER NOT NULL UNIQUE,
            testament         TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bible_verses (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id        INTEGER NOT NULL REFERENCES bible_versions(id) ON DELETE CASCADE,
            canonical_book_id INTEGER NOT NULL REFERENCES bible_books(id),
            canonical_book_name TEXT NOT NULL,
            book_name         TEXT NOT NULL,
            book_order        INTEGER NOT NULL,
            source_book_order INTEGER NOT NULL DEFAULT 0,
            chapter_num       INTEGER NOT NULL,
            verse_num         INTEGER NOT NULL,
            reference         TEXT NOT NULL,
            text              TEXT NOT NULL,
            normalized_text   TEXT NOT NULL,
            UNIQUE(version_id, book_name, chapter_num, verse_num)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bible_verses_lookup
            ON bible_verses(version_id, canonical_book_id, chapter_num, verse_num)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bible_verses_reference
            ON bible_verses(version_id, reference)
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS bible_verses_fts USING fts5(
            text,
            normalized_text,
            content='bible_verses',
            content_rowid='id'
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO bible_books (id, osis_id, canonical_name, book_order, testament)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            osis_id=excluded.osis_id,
            canonical_name=excluded.canonical_name,
            book_order=excluded.book_order,
            testament=excluded.testament
        """,
        [(book_id, osis_id, canonical_name, book_id, testament) for book_id, osis_id, canonical_name, testament in CANONICAL_BOOKS],
    )
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(bible_verses)").fetchall()
    }
    migrations = [
        ("canonical_book_id", "ALTER TABLE bible_verses ADD COLUMN canonical_book_id INTEGER REFERENCES bible_books(id)"),
        ("canonical_book_name", "ALTER TABLE bible_verses ADD COLUMN canonical_book_name TEXT NOT NULL DEFAULT ''"),
        ("source_book_order", "ALTER TABLE bible_verses ADD COLUMN source_book_order INTEGER NOT NULL DEFAULT 0"),
    ]
    for col_name, stmt in migrations:
        if col_name not in existing_cols:
            conn.execute(stmt)
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bible_verses_canonical_name
            ON bible_verses(version_id, canonical_book_name, chapter_num, verse_num)
        """
    )


def import_version(conn: sqlite3.Connection, spec: VersionSpec) -> dict:
    path = SOURCE_ROOT / spec.filename
    if not path.exists():
        raise FileNotFoundError(f"Missing source file: {path}")

    payload = load_version_json(path)
    file_sha = _sha256(path)
    books = [(book_name, chapters) for book_name, chapters in payload.items() if isinstance(chapters, dict)]
    chapter_count = sum(len(chapters) for _, chapters in books)
    verse_count = sum(len(verses) for _, chapters in books for verses in chapters.values())

    cur = conn.execute(
        """
        INSERT INTO bible_versions (
            source_key, slug, name, language_code, source_url, license_note,
            import_path, file_sha256, book_count, chapter_count, verse_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            slug=excluded.slug,
            name=excluded.name,
            language_code=excluded.language_code,
            source_url=excluded.source_url,
            license_note=excluded.license_note,
            import_path=excluded.import_path,
            file_sha256=excluded.file_sha256,
            book_count=excluded.book_count,
            chapter_count=excluded.chapter_count,
            verse_count=excluded.verse_count,
            imported_at=datetime('now')
        RETURNING id
        """,
        (
            spec.source_key,
            spec.slug,
            spec.name,
            spec.language_code,
            spec.source_url,
            spec.license_note,
            str(path),
            file_sha,
            len(books),
            chapter_count,
            verse_count,
        ),
    )
    version_id = cur.fetchone()[0]

    conn.execute("DELETE FROM bible_verses WHERE version_id = ?", (version_id,))

    verse_rows: list[tuple] = []
    for source_book_order, (book_name, chapters) in enumerate(books, start=1):
        canonical_book_id, canonical_book_name = canonicalize_book_name(book_name)
        for chapter_str, verses in chapters.items():
            chapter_num = int(chapter_str)
            for verse_str, text in verses.items():
                verse_num = int(verse_str)
                clean_text = str(text).strip()
                verse_rows.append(
                    (
                        version_id,
                        canonical_book_id,
                        canonical_book_name,
                        book_name,
                        canonical_book_id,
                        source_book_order,
                        chapter_num,
                        verse_num,
                        f"{canonical_book_name} {chapter_num}:{verse_num}",
                        clean_text,
                        normalize_text(clean_text),
                    )
                )

    conn.executemany(
        """
        INSERT INTO bible_verses (
            version_id, canonical_book_id, canonical_book_name, book_name, book_order,
            source_book_order, chapter_num, verse_num,
            reference, text, normalized_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        verse_rows,
    )

    conn.execute(
        "INSERT INTO bible_verses_fts(bible_verses_fts) VALUES ('rebuild')"
    )

    return {
        "slug": spec.slug,
        "books": len(books),
        "chapters": chapter_count,
        "verses": verse_count,
        "sha256": file_sha,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Import Bible JSON corpora into SQLite.")
    parser.add_argument(
        "--db-path",
        default=str(DB_PATH),
        help="Path to the SQLite database file.",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        ensure_schema(conn)
        summaries = []
        for spec in VERSION_SPECS:
            summaries.append(import_version(conn, spec))
        conn.commit()

    for summary in summaries:
        print(
            f"{summary['slug']}: books={summary['books']} chapters={summary['chapters']} "
            f"verses={summary['verses']} sha256={summary['sha256']}"
        )


if __name__ == "__main__":
    main()
