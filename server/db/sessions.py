from server.db.index import get_db


async def create_service_session(church_id: str) -> int:
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO service_sessions (church_id) VALUES (?)",
            (church_id,),
        )
        await db.commit()
        return cursor.lastrowid


async def close_service_session(session_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE service_sessions SET ended_at = datetime('now') WHERE id = ?",
            (session_id,),
        )
        await db.commit()


async def append_segment(session_id: int, spanish: str, english: str) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO transcript_segments (session_id, spanish, english) VALUES (?, ?, ?)",
            (session_id, spanish, english),
        )
        await db.commit()


async def get_full_transcript(session_id: int) -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT spanish, english, ts FROM transcript_segments "
            "WHERE session_id = ? ORDER BY ts ASC",
            (session_id,),
        )
        rows = await cursor.fetchall()
    return [{"spanish": r[0], "english": r[1], "ts": r[2]} for r in rows]


async def save_summary(session_id: int, summary: str) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE service_sessions SET summary = ? WHERE id = ?",
            (summary, session_id),
        )
        await db.commit()


async def create_capture_record(session_id: int) -> int:
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO session_captures (session_id) VALUES (?)",
            (session_id,),
        )
        await db.commit()
        return cursor.lastrowid


async def finalize_capture(
    capture_id: int,
    audio_path: str,
    events_path: str,
    duration_s: float,
    segment_count: int,
) -> None:
    async with get_db() as db:
        await db.execute(
            """UPDATE session_captures
               SET audio_path = ?, events_path = ?, duration_s = ?,
                   segment_count = ?, ended_at = datetime('now')
               WHERE id = ?""",
            (audio_path, events_path, duration_s, segment_count, capture_id),
        )
        await db.commit()


async def list_captures_for_church(church_id: str, limit: int = 20) -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT sc.id, sc.session_id, sc.audio_path, sc.events_path,
                      sc.duration_s, sc.segment_count, sc.started_at, sc.ended_at
               FROM session_captures sc
               JOIN service_sessions ss ON ss.id = sc.session_id
               WHERE ss.church_id = ?
               ORDER BY sc.started_at DESC
               LIMIT ?""",
            (church_id, limit),
        )
        rows = await cursor.fetchall()
    return [
        {
            "id": r[0], "session_id": r[1], "audio_path": r[2],
            "events_path": r[3], "duration_s": r[4], "segment_count": r[5],
            "started_at": r[6], "ended_at": r[7],
        }
        for r in rows
    ]
