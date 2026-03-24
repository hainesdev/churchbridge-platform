from server.db.index import get_db

DEFAULT_GLOSSARY = {
    "Jesucristo": 10,
    "Espíritu Santo": 10,
    "Pentecostés": 10,
    "evangelio": 8,
    "salvación": 8,
    "alabanza": 7,
    "adoración": 7,
}


async def get_glossary(church_id: str) -> dict[str, int]:
    """Returns {term: boost_weight} for Deepgram keyword injection."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT term, boost FROM church_glossary WHERE church_id = ? OR church_id = 'default' "
            "ORDER BY church_id DESC",
            (church_id,),
        )
        rows = await cursor.fetchall()
    if not rows:
        return DEFAULT_GLOSSARY
    seen: dict[str, int] = {}
    for term, boost in rows:
        if term not in seen:
            seen[term] = boost
    return seen


async def upsert_glossary_term(church_id: str, term: str, boost: int) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO church_glossary (church_id, term, boost) VALUES (?, ?, ?) "
            "ON CONFLICT(church_id, term) DO UPDATE SET boost = excluded.boost",
            (church_id, term, boost),
        )
        await db.commit()


async def delete_glossary_term(church_id: str, term: str) -> None:
    async with get_db() as db:
        await db.execute(
            "DELETE FROM church_glossary WHERE church_id = ? AND term = ?",
            (church_id, term),
        )
        await db.commit()
