"""Candidates found by the offline survey crawler (`python -m planetx.survey run --processor motion`).

Reads the survey database read-only, like routes/survey.py: an empty list (not
an error) until the crawler has stored something, or when the database cannot
be opened for reading (logged).
"""
import logging
import sqlite3
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Response

from planetx import pipeline
from planetx.api.routes.survey import DbPath, cannot_open
from planetx.api.schemas import SurveyCandidate, SurveyCandidates
from planetx.classify.rules import CLASSIFICATIONS
from planetx.spherex import archive, cutouts
from planetx.survey.candidates import CandidateStore, StoredCandidate
from planetx.survey.motion import FIELD_SIZE_DEG

log = logging.getLogger(__name__)
router = APIRouter(tags=["survey"])

MAX_LIMIT = 500
# Side of a list thumbnail: ~18 SPHEREx pixels, enough to see the object and its neighbours.
THUMBNAIL_SIZE_DEG = 0.03
# Exposures tried (newest first) before giving up on a thumbnail: each can be a ~4 MB download.
THUMBNAIL_TRIES = 3


def _stored_or_none(db_path: Path, read):
    """`read(store)` on a read-only candidate store, or None if the database is missing or unreadable."""
    if not db_path.is_file():
        return None
    try:
        with CandidateStore(db_path, readonly=True) as store:
            return read(store)
    except sqlite3.OperationalError as exc:
        if not cannot_open(exc):
            raise
        log.warning("survey database %s cannot be opened read-only: %s", db_path, exc)
        return None


@router.get("/candidates")
def list_candidates(
    db_path: DbPath,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    classification: Annotated[str, Query(description=f"One of {', '.join(CLASSIFICATIONS)}; empty for all")] = "",
    sort: Annotated[Literal["score", "date"], Query(
        description="score: best first (default); date: newest survey run first, then best",
    )] = "score",
) -> SurveyCandidates:
    if classification and classification not in CLASSIFICATIONS:
        raise HTTPException(422, f"classification must be one of {', '.join(CLASSIFICATIONS)} (or empty)")
    page = _stored_or_none(
        db_path, lambda s: s.page(limit=limit, offset=offset, classification=classification or None, sort=sort)
    )
    total, rows = page if page else (0, [])
    return SurveyCandidates(total=total, candidates=[_candidate(r) for r in rows])


def _candidate(r: StoredCandidate) -> SurveyCandidate:
    c = r.candidate
    return SurveyCandidate(
        id=r.id, ra=c.ra, dec=c.dec, name=c.name, classification=c.classification, kind=c.kind,
        is_variable=c.is_variable, pm_total=c.pm_total, pm_ra_cosdec=c.pm_ra_cosdec, pm_dec=c.pm_dec,
        significance=c.significance, position_angle_deg=c.position_angle_deg, score=c.score,
        nside=r.nside, ipix=r.ipix, found=r.found, field_ra=c.field_ra, field_dec=c.field_dec,
    )


@router.get(
    "/candidates/{candidate_id}/thumbnail.png",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
)
def candidate_thumbnail(candidate_id: int, db_path: DbPath) -> Response:
    """A small SPHEREx picture centred on a candidate, from the newest exposure the survey used.

    The survey's own cutouts are reused: the frame is cut from the raw cutout of the
    candidate's field (same request as `/api/motion` on that field), so this downloads
    nothing when the crawler shares the API's cache. Otherwise one ~4 MB download.
    """
    row = _stored_or_none(db_path, lambda s: s.get(candidate_id))
    if row is None:
        raise HTTPException(404, "no such candidate (ids change when the survey re-processes a patch)")
    c = row.candidate
    try:
        visits = pipeline.pick_visits(archive.query_visits(c.field_ra, c.field_dec, pipeline.VISIT_RADIUS_ARCSEC))
        request = cutouts.cutout_request(c.field_ra, c.field_dec, FIELD_SIZE_DEG)
        # Newest first: the picture shows where the object is now.
        for visit in sorted(visits, key=lambda v: v.mjd, reverse=True)[:THUMBNAIL_TRIES]:
            frame = _aligned(visit.access_url, request, c.ra, c.dec)
            if np.isfinite(frame.data).any():
                return Response(
                    cutouts.render_png(frame.data), media_type="image/png",
                    # Ids are reused after re-processing, so cache only briefly.
                    headers={"Cache-Control": "public, max-age=3600"},
                )
    except archive.UpstreamError as exc:
        if isinstance(exc, archive.IrsaBusy):
            raise HTTPException(503, str(exc), headers={"Retry-After": "5"}) from exc
        if exc.status in pipeline.NO_CUTOUT_STATUS:
            raise HTTPException(404, f"no SPHEREx picture at this candidate: {exc}") from exc
        raise HTTPException(502, f"IRSA request failed: {exc}") from exc
    raise HTTPException(404, "no SPHEREx exposure has data at this candidate")


def _aligned(url: str, request: tuple[float, float, float], ra: float, dec: float) -> cutouts.AlignedCutout:
    try:
        return cutouts.align(cutouts.cached_cutout(url, *request), ra, dec, THUMBNAIL_SIZE_DEG)
    except FileNotFoundError:
        # Evicted from the cache between download and open: fetch it again once.
        return cutouts.align(cutouts.cached_cutout(url, *request), ra, dec, THUMBNAIL_SIZE_DEG)
