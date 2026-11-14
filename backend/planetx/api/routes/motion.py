"""Motion-engine endpoint: movers and variables at a sky position.

The work runs as a background job (see planetx.api.motion); this handler only
starts it or reports its state, so it answers at once and clients poll it.
"""
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from planetx.api import motion as runs
from planetx.api.routes.sky import Dec, Ra
from planetx.api.schemas import MotionResponse

router = APIRouter(tags=["motion"])

DEFAULT_SIZE_DEG = 0.1
MIN_SIZE_DEG = 0.03  # smaller fields hold too few stars to align frames
MAX_SIZE_DEG = 0.2  # the engine's linking slows sharply with the number of sources


@router.get("/motion")
def motion(
    ra: Ra,
    dec: Dec,
    size_deg: Annotated[float, Query(
        ge=MIN_SIZE_DEG, le=MAX_SIZE_DEG, description="Side of the analysed field, deg"
    )] = DEFAULT_SIZE_DEG,
) -> MotionResponse:
    """Start (or poll) a motion-engine run on SPHEREx frames of this position.

    The first call starts the run and returns ``status: "running"``; repeat the
    same call every few seconds until it is "done" (with ``result``) or "failed".
    """
    try:
        return runs.request_job(ra, dec, size_deg)
    except runs.MotionBusy as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "30"}) from exc
