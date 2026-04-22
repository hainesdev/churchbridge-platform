import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from server.db.bible_catalog import canonicalize_book_name
from server.db.bible_text import (
    get_chapter,
    get_passage,
    list_bible_versions,
    list_books_for_version,
    search_bible_text,
)
from server.db.glossary import get_glossary, upsert_glossary_term, delete_glossary_term
from server.db.church_terms import load_church_terms, upsert_term
from server.db.sessions import get_full_transcript, list_captures_for_church

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


@router.get("/captures")
async def list_captures(church_id: str, limit: int = 20):
    """Return past session captures (audio + event log paths) for this church."""
    captures = await list_captures_for_church(church_id, limit=min(limit, 100))
    return {"captures": captures}


@router.post("/capture/start")
async def start_capture(church_id: str):
    """Enable recording for the active session. No-op if already recording."""
    if _session_manager is None:
        raise HTTPException(status_code=503, detail="Session manager not initialized")
    session = _session_manager.get(church_id)
    if session is None:
        raise HTTPException(status_code=404, detail="No active session for this church")
    if session._recorder is not None:
        return {"ok": True, "already_active": True}
    from server.services.session_recorder import SessionRecorder
    session._recorder = SessionRecorder(session._db_session_id, session._church_id)
    session._recorder.record_event("session_start", {
        "church_id": church_id, "sample_rate": session._sample_rate,
    })
    return {"ok": True, "already_active": False}


@router.post("/capture/stop")
async def stop_capture(church_id: str):
    """Stop and flush the active capture for this church's session."""
    if _session_manager is None:
        raise HTTPException(status_code=503, detail="Session manager not initialized")
    session = _session_manager.get(church_id)
    if session is None:
        raise HTTPException(status_code=404, detail="No active session for this church")
    if session._recorder is None:
        raise HTTPException(status_code=409, detail="No capture is active")
    result = session._recorder.stop()
    session._recorder = None
    from server.services.session_manager import _finalize_capture_in_db
    await _finalize_capture_in_db(result, session._db_session_id)
    return {
        "ok": True,
        "audio_path": result.audio_path,
        "events_path": result.events_path,
        "duration_s": result.duration_s,
        "segment_count": result.segment_count,
        "latency_ms": result.latency_ms,
    }


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


@router.get("/sessions/{session_id}/verses")
async def get_session_verses(church_id: str, session_id: int):
    """Return all verse detections for a past session."""
    from server.db.index import get_db
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT church_id FROM service_sessions WHERE id = ?", (session_id,)
        )
        row = await cursor.fetchone()
    if not row or row[0] != church_id:
        raise HTTPException(status_code=404, detail="Session not found")
    from server.db.verses import get_session_verse_detections
    verses = await get_session_verse_detections(session_id)
    return {"session_id": session_id, "verses": verses}


@router.get("/sessions/{session_id}/modes")
async def get_session_modes(church_id: str, session_id: int):
    """Return all sermon mode transitions for a past session."""
    from server.db.index import get_db
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT church_id FROM service_sessions WHERE id = ?", (session_id,)
        )
        row = await cursor.fetchone()
    if not row or row[0] != church_id:
        raise HTTPException(status_code=404, detail="Session not found")
    from server.db.modes import get_session_mode_transitions
    transitions = await get_session_mode_transitions(session_id)
    return {"session_id": session_id, "transitions": transitions}


# —— Bible corpus API ————————————————————————————————————————————————————————————————

@router.get("/bibles")
async def get_bibles(church_id: str):
    """Return imported Bible versions available for query."""
    return {"versions": await list_bible_versions()}


@router.get("/bibles/{version_slug}/books")
async def get_bible_books(church_id: str, version_slug: str):
    books = await list_books_for_version(version_slug)
    if not books:
        raise HTTPException(status_code=404, detail="Bible version not found")
    return {"version_slug": version_slug, "books": books}


@router.get("/bibles/{version_slug}/chapter")
async def get_bible_chapter(church_id: str, version_slug: str, book: str, chapter: int):
    try:
        canonicalize_book_name(book)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = await get_chapter(version_slug, book, chapter)
    if result is None:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return result


@router.get("/bibles/{version_slug}/passage")
async def get_bible_passage(
    church_id: str,
    version_slug: str,
    book: str,
    chapter: int,
    verse_start: int,
    verse_end: int | None = None,
):
    try:
        canonicalize_book_name(book)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = await get_passage(version_slug, book, chapter, verse_start, verse_end)
    if result is None:
        raise HTTPException(status_code=404, detail="Passage not found")
    return result


@router.get("/bibles/{version_slug}/search")
async def search_bible(church_id: str, version_slug: str, q: str, limit: int = 10):
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query must not be empty")
    limit = max(1, min(limit, 50))
    results = await search_bible_text(version_slug, q.strip(), limit=limit)
    return {"version_slug": version_slug, "query": q.strip(), "results": results}
