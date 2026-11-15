"""Spectrum endpoint: a SPHEREx spectrum of one object, measured on the fly.

The work runs as a background job (see planetx.api.spectrum); this handler only
starts it or reports its state, so it answers at once and clients poll it.
"""
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from planetx.api import spectrum as runs
from planetx.api.routes.sky import Dec, Ra
from planetx.api.schemas import SpectrumResponse

router = APIRouter(tags=["spectrum"])

ProperMotion = Annotated[float, Query(
    ge=-runs.MAX_PM_MAS, le=runs.MAX_PM_MAS, allow_inf_nan=False,
    description="Proper motion, mas/yr (the RA component includes cos dec)",
)]


@router.get("/spectrum")
def spectrum(
    ra: Ra,
    dec: Dec,
    pmra: ProperMotion = 0.0,
    pmdec: ProperMotion = 0.0,
    epoch: Annotated[float | None, Query(
        ge=1900, le=2100, allow_inf_nan=False,
        description="Julian year of ra/dec (2016.0 for Gaia DR3); default now. Used only with a proper motion",
    )] = None,
) -> SpectrumResponse:
    """Start (or poll) a spectrum run: forced photometry of SPHEREx exposures at this position.

    The first call starts the run and returns ``status: "running"``; repeat the same
    call every few seconds until it is "done" (with ``result``) or "failed".
    """
    try:
        return runs.request_job(ra, dec, pmra, pmdec, epoch)
    except runs.SpectrumBusy as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "30"}) from exc
