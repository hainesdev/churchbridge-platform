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
