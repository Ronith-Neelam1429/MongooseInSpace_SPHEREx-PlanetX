"""Survey-scale results for the website's Results page (file contract: docs/RESULTS.md).

- `GET /api/results`: which result files exist, with their `generated_at`.
- `GET /api/results/survey_stats`: live numbers from the survey database.
- `GET /api/results/{name}`: `results/{name}.json`, for allow-listed names only.

Result files are written by the analysis tools in `tools/`, which may rewrite
a file while the API runs: a file that is not valid JSON (half written, or
broken) is reported as such, never served. Parsed files are cached by
modification time and size, so the index does not re-read them every call.

The results directory is `settings.results_dir` (env `PLANETX_RESULTS_DIR`) when
set, else `results/` at the repository root. The fallback only works in a source
checkout: a non-editable install (the Docker image) has no repository around the
package, so the image copies `results/` and sets `PLANETX_RESULTS_DIR`.

The survey database is opened read-only (`mode=ro`): the survey worker writes it.
"""
import json
import logging
import math
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from planetx.api.routes.survey import DbPath, cannot_open
from planetx.config import settings
from planetx.survey.queue import BUSY_TIMEOUT_S, STATUSES

log = logging.getLogger(__name__)
router = APIRouter(prefix="/results", tags=["results"])

# Every file a tool may write (docs/RESULTS.md). Nothing else under results/ is served.
RESULT_NAMES = (
    "gaia_validation",
    "false_movers",
    "brown_dwarfs",
    "light_curve_r_dra",
    "injection_recovery",
)
# A result file is a summary for a web page; anything bigger is a mistake, not data to send.
MAX_RESULT_BYTES = 20 * 1024 * 1024
HOURS_SHOWN = 24

_REPO_RESULTS = Path(__file__).resolve().parents[4] / "results"


def get_results_dir() -> Path:
    """Where result files live (a dependency so tests can point it elsewhere)."""
    return settings.results_dir or _REPO_RESULTS


ResultsDir = Annotated[Path, Depends(get_results_dir)]


# -- response models (kept here: api/schemas.py belongs to the motion API) ---------------------


class ResultInfo(BaseModel):
    name: str
    available: bool = Field(description="The file exists and is valid JSON")
    generated_at: str | None = None
    version: int | None = None
    command: str | None = None
    error: str | None = Field(default=None, description="Why an existing file cannot be served (e.g. not valid JSON)")


class ResultsIndex(BaseModel):
    results: list[ResultInfo]


class ClassCount(BaseModel):
    classification: str | None = Field(description="null: the catalogue check did not run")
    kind: str
    count: int


class HourCount(BaseModel):
    start: str = Field(description="UTC start of the hour, ISO 8601")
    done: int
    failed: int


class SurveyStats(BaseModel):
    available: bool = Field(description="The survey database exists and could be read")
    generated_at: str
    patches: dict[str, int] = Field(description="Patches by status (every status, zero if absent)")
    total_patches: int
    objects_measured: int = Field(description="Objects the engine tracked in finished patches")
    candidates_total: int
    candidates: list[ClassCount] = Field(description="Stored candidates by classification and kind")
    by_classification: dict[str, int] = Field(description="Candidates per classification ('unchecked' for null)")
    by_kind: dict[str, int]
    last_day: list[HourCount] = Field(description=f"Patches finished per hour, the last {HOURS_SHOWN} hours, oldest first")
    first_finished_at: str | None = None
    last_finished_at: str | None = None


# -- result files ------------------------------------------------------------------------------

_cache: dict[Path, tuple[tuple[int, int], bytes, dict[str, Any] | None, str | None]] = {}
_cache_lock = threading.Lock()


def _load(path: Path) -> tuple[bytes, dict[str, Any] | None, str | None] | None:
    """(raw bytes, parsed object or None, error or None) for a result file; None if it is missing."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    if not path.is_file():
        return b"", None, "not a regular file"
    key = (stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        hit = _cache.get(path)
    if hit and hit[0] == key:
        return hit[1:]
    if stat.st_size > MAX_RESULT_BYTES:
        raw, parsed, error = b"", None, f"file is {stat.st_size} bytes; limit is {MAX_RESULT_BYTES}"
    else:
        raw = path.read_bytes()
        try:
            parsed = json.loads(raw)
            error = None if isinstance(parsed, dict) else "not a JSON object"
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            parsed, error = None, f"not valid JSON ({exc.__class__.__name__}): being written, or broken"
        if error:
            parsed = None
            log.warning("result file %s cannot be served: %s", path, error)
    with _cache_lock:
        _cache[path] = (key, raw, parsed, error)
    return raw, parsed, error


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


@router.get("")
def results_index(results_dir: ResultsDir) -> ResultsIndex:
    out = []
    for name in RESULT_NAMES:
        loaded = _load(results_dir / f"{name}.json")
        if loaded is None:
            out.append(ResultInfo(name=name, available=False))
            continue
        _, parsed, error = loaded
        if parsed is None:
            out.append(ResultInfo(name=name, available=False, error=error))
            continue
        version = parsed.get("version")
        out.append(ResultInfo(
            name=name,
            available=True,
            generated_at=_text(parsed.get("generated_at")),
            version=version if isinstance(version, int) and not isinstance(version, bool) else None,
            command=_text(parsed.get("command")),
        ))
    return ResultsIndex(results=out)


# -- live survey numbers (declared before /{name} so it is never read as a file name) ------------


def _iso(ts: float) -> str:
    """Unix seconds as ISO 8601 UTC with a Z."""
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def _iso_or_none(ts: float | None) -> str | None:
    return None if ts is None or not math.isfinite(ts) else _iso(ts)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _empty_stats(now: float, available: bool) -> SurveyStats:
    return SurveyStats(
        available=available, generated_at=_iso(now), patches=dict.fromkeys(STATUSES, 0), total_patches=0,
        objects_measured=0, candidates_total=0, candidates=[], by_classification={}, by_kind={},
        last_day=_hours(now, []),
    )


def _hours(now: float, rows: list[tuple[str, float]]) -> list[HourCount]:
    """Bucket (status, finished_at) rows into the last HOURS_SHOWN whole UTC hours, the current one last."""
    current = math.floor(now / 3600) * 3600
    starts = [current - 3600 * i for i in range(HOURS_SHOWN - 1, -1, -1)]
    counts = {s: {"done": 0, "failed": 0} for s in starts}
    for status, finished in rows:
        bucket = math.floor(finished / 3600) * 3600
        if bucket in counts and status in ("done", "failed"):
            counts[bucket][status] += 1
    return [HourCount(start=_iso(s), **counts[s]) for s in starts]


def _survey_stats(conn: sqlite3.Connection, now: float) -> SurveyStats:
    stats = _empty_stats(now, available=True)
    if not _has_table(conn, "patches"):
        return stats
    for status, n in conn.execute("SELECT status, COUNT(*) FROM patches GROUP BY status"):
        stats.patches[status] = n
    stats.total_patches = sum(stats.patches.values())
    # The motion processor's summary carries `objects`; other processors' summaries do not.
    objects = conn.execute(
        "SELECT SUM(CAST(json_extract(summary, '$.objects') AS INTEGER)) FROM patches"
        " WHERE status = 'done' AND json_valid(summary)"
    ).fetchone()[0]
    stats.objects_measured = int(objects or 0)
    first, last = conn.execute(
        "SELECT MIN(finished_at), MAX(finished_at) FROM patches WHERE status = 'done'"
    ).fetchone()
    stats.first_finished_at, stats.last_finished_at = _iso_or_none(first), _iso_or_none(last)
    since = math.floor(now / 3600) * 3600 - 3600 * (HOURS_SHOWN - 1)
    rows = conn.execute(
        "SELECT status, finished_at FROM patches WHERE finished_at >= ? AND status IN ('done', 'failed')", (since,)
    ).fetchall()
    stats.last_day = _hours(now, rows)

    if _has_table(conn, "candidates"):
        for classification, kind, n in conn.execute(
            "SELECT classification, kind, COUNT(*) FROM candidates GROUP BY classification, kind ORDER BY 3 DESC"
        ):
            stats.candidates.append(ClassCount(classification=classification, kind=kind, count=n))
            label = classification or "unchecked"
            stats.by_classification[label] = stats.by_classification.get(label, 0) + n
            stats.by_kind[kind] = stats.by_kind.get(kind, 0) + n
        stats.candidates_total = sum(c.count for c in stats.candidates)
    return stats


@router.get("/survey_stats")
def survey_stats(db_path: DbPath) -> SurveyStats:
    now = time.time()
    if not db_path.is_file():
        return _empty_stats(now, available=False)
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_S)
        try:
            return _survey_stats(conn, now)
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if not cannot_open(exc):
            raise
        log.warning("survey database %s cannot be opened read-only: %s", db_path, exc)
        return _empty_stats(now, available=False)


@router.get(
    "/{name}",
    responses={404: {"description": "Unknown result, or not run yet"}, 503: {"description": "File not valid JSON"}},
)
def result_file(name: str, results_dir: ResultsDir) -> Response:
    # Strict allow-list: the name never reaches the filesystem unless it is one of ours.
    if name not in RESULT_NAMES:
        raise HTTPException(404, f"unknown result {name!r}; known: {', '.join(RESULT_NAMES)}")
    loaded = _load(results_dir / f"{name}.json")
    if loaded is None:
        raise HTTPException(404, f"{name} has not been run yet")
    raw, parsed, error = loaded
    if parsed is None:
        raise HTTPException(503, f"{name} cannot be read: {error}")
    return Response(content=raw, media_type="application/json")
