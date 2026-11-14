"""Survey endpoints: crawler progress and patch coverage for the sky-map overlay.

Both read the survey database read-only and report an empty survey (not an
error) until the worker has created it.
"""
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends

from planetx.api.schemas import SurveyCounts, SurveyCoverage, SurveyStatus
from planetx.config import settings
from planetx.survey.patches import DEFAULT_NSIDE, moc_order, nep_distance, patch_center
from planetx.survey.queue import PatchQueue

router = APIRouter(prefix="/survey", tags=["survey"])


def get_db_path() -> Path:
    """Survey database location (a dependency so tests can point it elsewhere)."""
    return settings.db_path


DbPath = Annotated[Path, Depends(get_db_path)]


@router.get("/status")
def survey_status(db_path: DbPath) -> SurveyStatus:
    if not db_path.is_file():
        return SurveyStatus(total=0, counts=SurveyCounts())
    with PatchQueue(db_path, readonly=True) as queue:
        stats = queue.stats()
    status = SurveyStatus(
        nside=stats.nside,
        moc_order=None if stats.nside is None else moc_order(stats.nside),
        total=stats.total,
        counts=SurveyCounts(**stats.counts),
        current_rank=stats.current_rank,
    )
    if stats.nside is not None and stats.current_ipix is not None:
        status.current_ra, status.current_dec = patch_center(stats.current_ipix, stats.nside)
        status.current_radius_deg = float(nep_distance([stats.current_ipix], stats.nside)[0])
    return status


@router.get("/coverage")
def survey_coverage(db_path: DbPath) -> SurveyCoverage:
    nside, done = DEFAULT_NSIDE, []
    if db_path.is_file():
        with PatchQueue(db_path, readonly=True) as queue:
            nside = queue.nside() or DEFAULT_NSIDE
            done = queue.done_patches()
    return {str(moc_order(nside)): done}
