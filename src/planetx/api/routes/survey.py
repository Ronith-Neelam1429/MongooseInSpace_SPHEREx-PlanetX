"""Survey endpoints: crawler progress and patch coverage for the sky-map overlay.

Both read the survey database read-only and report an empty survey (not an
error) until the worker has created it, or if it cannot be opened for
reading (e.g. its directory is not writable, so SQLite cannot create the
WAL index it needs); the latter is logged.
"""
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends

from planetx.api.schemas import SurveyCounts, SurveyCoverage, SurveyStatus
from planetx.config import settings
from planetx.survey.patches import DEFAULT_NSIDE, moc_order, nep_distance, patch_center
from planetx.survey.queue import PatchQueue

log = logging.getLogger(__name__)
router = APIRouter(prefix="/survey", tags=["survey"])


def get_db_path() -> Path:
    """Survey database location (a dependency so tests can point it elsewhere)."""
    return settings.db_path


DbPath = Annotated[Path, Depends(get_db_path)]


def _cannot_open(exc: sqlite3.OperationalError) -> bool:
    """True for SQLite errors meaning the file can't be opened read-only, as opposed to bugs."""
    name = getattr(exc, "sqlite_errorname", "")
    return name.startswith(("SQLITE_CANTOPEN", "SQLITE_READONLY"))


def _read[T](db_path: Path, read: Callable[[PatchQueue], T]) -> T | None:
    """`read(queue)` on a read-only queue, or None if the database is missing or unreadable."""
    if not db_path.is_file():
        return None
    try:
        with PatchQueue(db_path, readonly=True) as queue:
            return read(queue)
    except sqlite3.OperationalError as exc:
        if not _cannot_open(exc):
            raise
        log.warning("survey database %s cannot be opened read-only: %s", db_path, exc)
        return None


@router.get("/status")
def survey_status(db_path: DbPath) -> SurveyStatus:
    stats = _read(db_path, PatchQueue.stats)
    if stats is None:
        return SurveyStatus(total=0, counts=SurveyCounts(), readable=not db_path.is_file())
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
    data = _read(db_path, lambda q: (q.nside(), q.done_patches()))
    nside, done = data if data else (None, [])
    return {str(moc_order(nside or DEFAULT_NSIDE)): done}
