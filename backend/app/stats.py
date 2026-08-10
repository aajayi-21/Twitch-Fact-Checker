"""Read-only analytics surface: GET /stats/* and the /dashboard page.

All aggregates are computed in :class:`app.db.Database` fetch methods (SQL
runs on the db executor thread). Responses are plain dicts — the stats
surface is a local read-only API, not part of the WS/LLM wire vocabulary in
:mod:`app.models`.

Honest-metrics doctrine (report §3.6): per-channel percentages are computed
over ADJUDICATED verdicts only (TRUE/FALSE/MISLEADING) and the sample size
``n`` is always returned so the UI can apply its n>=30 floor; the UNVERIFIED
share is a coverage metric of the pipeline, never a property of the channel.
"""

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.config import Settings
from app.db import Database

logger = logging.getLogger(__name__)

router = APIRouter()

_DASHBOARD_PATH = Path(__file__).resolve().parent / "static" / "dashboard.html"


@router.get("/stats/summary")
async def stats_summary(request: Request) -> dict[str, Any]:
    """Lifetime + today totals: sessions, claims, funnel, labels, cost."""
    db: Database = request.app.state.db
    settings: Settings = request.app.state.settings
    return await db.fetch_summary(settings.cost_per_verify_usd)


@router.get("/stats/channels")
async def stats_channels(request: Request) -> list[dict[str, Any]]:
    """Per-(platform, channel) aggregates; sessions without a channel are
    excluded (no identity, nothing to aggregate under)."""
    db: Database = request.app.state.db
    return await db.fetch_channels()


@router.get("/stats/sessions")
async def stats_sessions(
    request: Request, limit: int = Query(default=50, ge=1, le=500)
) -> list[dict[str, Any]]:
    """Most recent sessions, newest first."""
    db: Database = request.app.state.db
    return await db.fetch_sessions(limit)


@router.get("/stats/sessions/{session_id}")
async def stats_session_detail(session_id: str, request: Request) -> dict[str, Any]:
    """One session's full report: claims, verdicts, sources, feedback."""
    db: Database = request.app.state.db
    detail = await db.fetch_session_detail(session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="unknown session id")
    return detail


@router.get("/dashboard")
async def dashboard() -> FileResponse:
    """The single-file analytics dashboard (vanilla JS, no build step)."""
    return FileResponse(_DASHBOARD_PATH, media_type="text/html")
