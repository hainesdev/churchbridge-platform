from server.db.index import get_db


async def save_verse_detection(session_id: int, segment_ts: int, verse: dict) -> int:
    async with get_db() as db:
        cursor = await db.execute(
            """INSERT INTO verse_detections
               (session_id, segment_ts, book, chapter, verse_start, verse_end,
                reference, spanish_text, canonical_english, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                segment_ts,
                verse["book"],
                verse["chapter"],
                verse["verse_start"],
                verse.get("verse_end"),
                verse["reference"],
                verse["spanish_text"],
                verse["canonical_english"],
                verse.get("confidence", "explicit"),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def save_verse_suggestions(
    session_id: int, segment_ts: int, suggestions: list[dict]
) -> None:
    async with get_db() as db:
        await db.executemany(
            """INSERT INTO verse_suggestions
               (session_id, segment_ts, reference, canonical_english, relevance_note)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (session_id, segment_ts, s["reference"], s["canonical_english"], s["relevance_note"])
                for s in suggestions
            ],
        )
        await db.commit()


async def get_session_verse_detections(session_id: int) -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT book, chapter, verse_start, verse_end, reference, "
            "spanish_text, canonical_english, confidence, segment_ts, detected_at "
            "FROM verse_detections WHERE session_id = ? ORDER BY detected_at ASC",
            (session_id,),
        )
        rows = await cursor.fetchall()
    return [
        {
            "book": r[0], "chapter": r[1], "verse_start": r[2], "verse_end": r[3],
            "reference": r[4], "spanish_text": r[5], "canonical_english": r[6],
            "confidence": r[7], "segment_ts": r[8], "detected_at": r[9],
        }
        for r in rows
    ]


async def get_session_verse_suggestions(session_id: int) -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT reference, canonical_english, relevance_note, segment_ts, suggested_at "
            "FROM verse_suggestions WHERE session_id = ? ORDER BY suggested_at ASC",
            (session_id,),
        )
        rows = await cursor.fetchall()
    return [
        {
            "reference": r[0], "canonical_english": r[1],
            "relevance_note": r[2], "segment_ts": r[3], "suggested_at": r[4],
        }
        for r in rows
    ]
