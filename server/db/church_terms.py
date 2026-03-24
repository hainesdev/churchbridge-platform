from server.db.index import get_db


async def load_church_terms(church_id: str) -> dict[str, str]:
    """Returns {spanish: english} overrides. Church-specific wins over 'default'."""
    async with await get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT spanish_term, english_term, church_id FROM church_terms "
            "WHERE church_id = ? OR church_id = 'default' "
            "ORDER BY church_id DESC",
            (church_id,),
        )
    seen: dict[str, str] = {}
    for spanish, english, _ in rows:
        if spanish not in seen:
            seen[spanish] = english
    return seen


async def upsert_term(church_id: str, spanish: str, english: str) -> None:
    async with await get_db() as db:
        await db.execute(
            "INSERT INTO church_terms (church_id, spanish_term, english_term) VALUES (?, ?, ?) "
            "ON CONFLICT(church_id, spanish_term) DO UPDATE SET english_term = excluded.english_term",
            (church_id, spanish, english),
        )
        await db.commit()
