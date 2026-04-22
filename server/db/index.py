import os
import aiosqlite

DB_PATH = os.getenv("DATABASE_URL", "data/churchbridge.db").replace("sqlite:///./", "")

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS church_glossary (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    church_id   TEXT NOT NULL,
    term        TEXT NOT NULL,
    boost       INTEGER NOT NULL DEFAULT 5,
    UNIQUE(church_id, term)
);

CREATE TABLE IF NOT EXISTS church_terms (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    church_id     TEXT NOT NULL DEFAULT 'default',
    spanish_term  TEXT NOT NULL,
    english_term  TEXT NOT NULL,
    UNIQUE(church_id, spanish_term)
);

CREATE TABLE IF NOT EXISTS service_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    church_id   TEXT NOT NULL,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at    TEXT,
    summary     TEXT
);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES service_sessions(id),
    spanish     TEXT NOT NULL,
    english     TEXT NOT NULL,
    ts          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS verse_detections (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        INTEGER NOT NULL REFERENCES service_sessions(id),
    segment_ts        INTEGER NOT NULL,
    book              TEXT NOT NULL,
    chapter           INTEGER NOT NULL,
    verse_start       INTEGER NOT NULL,
    verse_end         INTEGER,
    reference         TEXT NOT NULL,
    spanish_text      TEXT NOT NULL,
    canonical_english TEXT NOT NULL,
    confidence        TEXT NOT NULL DEFAULT 'explicit',
    audio_start       REAL,
    audio_end         REAL,
    detected_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS verse_suggestions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        INTEGER NOT NULL REFERENCES service_sessions(id),
    segment_ts        INTEGER NOT NULL,
    reference         TEXT NOT NULL,
    canonical_english TEXT NOT NULL,
    relevance_note    TEXT NOT NULL,
    suggested_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sermon_mode_transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES service_sessions(id),
    from_mode   TEXT NOT NULL,
    to_mode     TEXT NOT NULL,
    segment_ts  INTEGER NOT NULL,
    occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS session_captures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES service_sessions(id),
    audio_path      TEXT NOT NULL DEFAULT '',
    events_path     TEXT NOT NULL DEFAULT '',
    duration_s      REAL,
    segment_count   INTEGER,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at        TEXT
);

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
);

CREATE TABLE IF NOT EXISTS bible_books (
    id                INTEGER PRIMARY KEY,
    osis_id           TEXT NOT NULL UNIQUE,
    canonical_name    TEXT NOT NULL UNIQUE,
    book_order        INTEGER NOT NULL UNIQUE,
    testament         TEXT NOT NULL
);

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
);

CREATE INDEX IF NOT EXISTS idx_bible_verses_lookup
    ON bible_verses(version_id, canonical_book_id, chapter_num, verse_num);

CREATE INDEX IF NOT EXISTS idx_bible_verses_reference
    ON bible_verses(version_id, reference);

CREATE INDEX IF NOT EXISTS idx_bible_verses_canonical_name
    ON bible_verses(version_id, canonical_book_name, chapter_num, verse_num);

CREATE VIRTUAL TABLE IF NOT EXISTS bible_verses_fts USING fts5(
    text,
    normalized_text,
    content='bible_verses',
    content_rowid='id'
);
"""

SEED_DEFAULT_TERMS = """
INSERT OR IGNORE INTO church_terms (church_id, spanish_term, english_term) VALUES
    ('default', 'Jesucristo',       'Jesus Christ'),
    ('default', 'Espíritu Santo',   'Holy Spirit'),
    ('default', 'Pentecostés',      'Pentecost'),
    ('default', 'Pentecostales',    'Pentecostals'),
    ('default', 'pentecostal',      'Pentecostal'),
    ('default', 'comunión',         'fellowship'),
    ('default', 'hermanos',         'brothers and sisters'),
    ('default', 'hermanas',         'sisters'),
    ('default', 'evangelio',        'gospel'),
    ('default', 'salvación',        'salvation'),
    ('default', 'Gran Misión',      'Great Commission'),
    ('default', 'misión',           'mission'),
    ('default', 'alabanza',         'praise'),
    ('default', 'adoración',        'worship'),
    ('default', 'oración',          'prayer'),
    ('default', 'gracia',           'grace'),
    ('default', 'fe',               'faith'),
    ('default', 'bautismo',         'baptism'),
    ('default', 'redención',        'redemption'),
    ('default', 'profecía',         'prophecy'),
    ('default', 'Salvador',         'Savior'),
    ('default', 'Señor',            'Lord'),
    ('default', 'pecado',           'sin'),
    ('default', 'arrepentimiento',  'repentance'),
    ('default', 'misericordia',     'mercy'),
    ('default', 'santidad',         'holiness'),
    ('default', 'ungido',           'anointed'),
    ('default', 'unción',           'anointing');

INSERT OR IGNORE INTO church_glossary (church_id, term, boost) VALUES
    ('default', 'Jesucristo',     10),
    ('default', 'Espíritu Santo', 10),
    ('default', 'Pentecostés',    10),
    ('default', 'evangelio',       8),
    ('default', 'salvación',       8),
    ('default', 'alabanza',        7),
    ('default', 'adoración',       7);
"""


async def _migrate(db) -> None:
    """Apply incremental schema changes to existing databases.

    CREATE TABLE IF NOT EXISTS won't alter tables that already exist, so new
    columns are added here with ALTER TABLE … ADD COLUMN. SQLite raises
    OperationalError if a column already exists; we catch and ignore that.
    """
    migrations = [
        "ALTER TABLE verse_detections ADD COLUMN audio_start REAL",
        "ALTER TABLE verse_detections ADD COLUMN audio_end REAL",
        "ALTER TABLE bible_verses ADD COLUMN canonical_book_id INTEGER REFERENCES bible_books(id)",
        "ALTER TABLE bible_verses ADD COLUMN canonical_book_name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE bible_verses ADD COLUMN source_book_order INTEGER NOT NULL DEFAULT 0",
    ]
    for stmt in migrations:
        try:
            await db.execute(stmt)
        except Exception:
            pass  # column already exists
    try:
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_bible_verses_canonical_name "
            "ON bible_verses(version_id, canonical_book_name, chapter_num, verse_num)"
        )
    except Exception:
        pass


async def init_db():
    import os
    os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_TABLES)
        await db.executescript(SEED_DEFAULT_TERMS)
        await _migrate(db)
        await db.execute("PRAGMA journal_mode=WAL")
        await db.commit()


def get_db() -> aiosqlite.Connection:
    """Return an aiosqlite connection context manager.

    Usage: `async with get_db() as db:`
    Do NOT await this — aiosqlite.connect() returns a context manager that
    starts its background thread in __aenter__. Awaiting it first and then
    using async-with calls __aenter__ again, raising 'threads can only be
    started once'.
    """
    return aiosqlite.connect(DB_PATH)
