"""POST /feedback — user judgement on delivered verdicts.

The overlay's thumbs-up/down buttons land here (routed through the extension
service worker, whose ``chrome-extension://`` origin the CORS regex already
admits). Feedback is the seed of the self-growing eval set (report §3.5):
each row ties a wire ``verdict_id`` to a human rating.

Deliberately NOT gated behind ``DEBUG_ENDPOINTS`` — this is a product
surface, not a debug tool.
"""

import logging

from fastapi import APIRouter, HTTPException, Request, Response

from app.db import Database
from app.models import FeedbackRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/feedback", status_code=204)
async def submit_feedback(body: FeedbackRequest, request: Request) -> Response:
    """Upsert feedback for one verdict; repeat submissions replace the row.

    Responses: 204 recorded, 404 unknown ``verdict_id``, 422 invalid body
    (FastAPI validation).
    """
    db: Database = request.app.state.db
    recorded = await db.record_feedback(
        body.verdict_id, body.rating, body.corrected_label, body.note
    )
    if not recorded:
        raise HTTPException(status_code=404, detail="unknown verdict_id")
    return Response(status_code=204)
