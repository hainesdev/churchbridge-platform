import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from server.db.glossary import get_glossary, upsert_glossary_term, delete_glossary_term
from server.db.church_terms import load_church_terms, upsert_term
from server.db.sessions import get_full_transcript

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/churches/{church_id}", tags=["services"])

_session_manager = None


def set_session_manager(mgr):
    global _session_manager
    _session_manager = mgr


# ── Glossary (Deepgram keyword boosting) ──────────────────────────────────────

class GlossaryTermIn(BaseModel):
    boost: int = Field(default=5, ge=1, le=10)


@router.get("/glossary")
async def list_glossary(church_id: str):
    """Return all Deepgram keyword-boost terms for this church."""
    terms = await get_glossary(church_id)
    return {"terms": [{"term": t, "boost": b} for t, b in terms.items()]}


@router.put("/glossary/{term}")
async def set_glossary_term(church_id: str, term: str, body: GlossaryTermIn):
    """Add or update a keyword-boost term."""
    await upsert_glossary_term(church_id, term, body.boost)
    return {"ok": True, "term": term, "boost": body.boost}


@router.delete("/glossary/{term}")
async def remove_glossary_term(church_id: str, term: str):
    """Remove a keyword-boost term."""
    await delete_glossary_term(church_id, term)
    return {"ok": True}


# ── Church terms (translation overrides) ─────────────────────────────────────

class ChurchTermIn(BaseModel):
    english: str = Field(..., min_length=1)


@router.get("/terms")
async def list_terms(church_id: str):
    """Return all Spanish→English translation overrides for this church."""
    terms = await load_church_terms(church_id)
    return {"terms": [{"spanish": s, "english": e} for s, e in terms.items()]}


@router.put("/terms/{spanish_term}")
async def set_term(church_id: str, spanish_term: str, body: ChurchTermIn):
    """Add or update a translation override."""
    await upsert_term(church_id, spanish_term, body.english)
    return {"ok": True, "spanish": spanish_term, "english": body.english}


@router.delete("/terms/{spanish_term}")
async def remove_term(church_id: str, spanish_term: str):
    """Remove a translation override."""
    from server.db.index import get_db
    async with get_db() as db:
        await db.execute(
            "DELETE FROM church_terms WHERE church_id = ? AND spanish_term = ?",
            (church_id, spanish_term),
        )
        await db.commit()
    return {"ok": True}


# ── Session history ───────────────────────────────────────────────────────────

@router.get("/sessions")
async def list_sessions(church_id: str, limit: int = 10):
    """Return the most recent service sessions for this church."""
    from server.db.index import get_db
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, started_at, ended_at, summary FROM service_sessions "
            "WHERE church_id = ? ORDER BY started_at DESC LIMIT ?",
            (church_id, limit),
        )
        rows = await cursor.fetchall()
    return {
        "sessions": [
            {"id": r[0], "started_at": r[1], "ended_at": r[2], "summary": r[3]}
            for r in rows
        ]
    }


@router.get("/stats")
async def get_session_stats(church_id: str):
    """Return live operational metrics for the active session of this church.

    Exposes sentence buffer flush counters, LLM enrichment metrics,
    STT noise removal count, and enrichment_settled set size.
    Returns 404 if no active session exists for this church.
    """
    if _session_manager is None:
        raise HTTPException(status_code=503, detail="Session manager not initialized")
    session = _session_manager.get(church_id)
    if session is None:
        raise HTTPException(status_code=404, detail="No active session for this church")
    return session.get_stats()


@router.get("/sessions/{session_id}/transcript")
async def get_transcript(church_id: str, session_id: int):
    """Return the full transcript for a session."""
    from server.db.index import get_db
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT church_id FROM service_sessions WHERE id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
    if not row or row[0] != church_id:
        raise HTTPException(status_code=404, detail="Session not found")
    segments = await get_full_transcript(session_id)
    return {"session_id": session_id, "segments": segments}
