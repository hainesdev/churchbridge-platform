from server.db.index import get_db


async def save_mode_transition(
    session_id: int,
    from_mode: str,
    to_mode: str,
    segment_ts: int,
) -> None:
    """Persist a sermon mode transition event for post-session analysis."""
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO sermon_mode_transitions (session_id, from_mode, to_mode, segment_ts)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, from_mode, to_mode, segment_ts),
        )
        await db.commit()


async def get_session_mode_transitions(session_id: int) -> list[dict]:
    """Return all mode transitions for a session, ordered chronologically."""
    async with get_db() as db:
        db.row_factory = lambda c, r: {
            col[0]: r[i] for i, col in enumerate(c.description)
        }
        async with db.execute(
            """
            SELECT from_mode, to_mode, segment_ts, occurred_at
            FROM sermon_mode_transitions
            WHERE session_id = ?
            ORDER BY occurred_at
            """,
            (session_id,),
        ) as cursor:
            return await cursor.fetchall()
