"""Candidates found by the offline survey crawler (`python -m planetx.survey run --processor motion`).

Reads the survey database read-only, like routes/survey.py: an empty list (not
an error) until the crawler has stored something, or when the database cannot
be opened for reading (logged).
"""
import logging
import sqlite3
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from planetx.api.routes.survey import DbPath, cannot_open
from planetx.api.schemas import SurveyCandidate, SurveyCandidates
from planetx.classify.rules import CLASSIFICATIONS
from planetx.survey.candidates import CandidateStore

log = logging.getLogger(__name__)
router = APIRouter(tags=["survey"])

MAX_LIMIT = 500


@router.get("/candidates")
def list_candidates(
    db_path: DbPath,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    classification: Annotated[str, Query(description=f"One of {', '.join(CLASSIFICATIONS)}; empty for all")] = "",
) -> SurveyCandidates:
    if classification and classification not in CLASSIFICATIONS:
        raise HTTPException(422, f"classification must be one of {', '.join(CLASSIFICATIONS)} (or empty)")
    if not db_path.is_file():
        return SurveyCandidates(total=0, candidates=[])
    try:
        with CandidateStore(db_path, readonly=True) as store:
            total, rows = store.page(limit=limit, offset=offset, classification=classification or None)
    except sqlite3.OperationalError as exc:
        if not cannot_open(exc):
            raise
        log.warning("survey database %s cannot be opened read-only: %s", db_path, exc)
        return SurveyCandidates(total=0, candidates=[])
    return SurveyCandidates(
        total=total,
        candidates=[
            SurveyCandidate(
                id=r.id, ra=r.candidate.ra, dec=r.candidate.dec, name=r.candidate.name,
                classification=r.candidate.classification, is_variable=r.candidate.is_variable,
                pm_total=r.candidate.pm_total, pm_ra_cosdec=r.candidate.pm_ra_cosdec, pm_dec=r.candidate.pm_dec,
                significance=r.candidate.significance, position_angle_deg=r.candidate.position_angle_deg,
                score=r.candidate.score, nside=r.nside, ipix=r.ipix, found=r.found,
            )
            for r in rows
        ],
    )
