"""Run the motion engine on one sky position for the website.

Glue only: list the visits (planetx.spherex.archive), download a sample of raw
cutouts (planetx.spherex.cutouts), run `find_motion` (planetx.engine) and the
archival cross-check (planetx.observatories), and shape the answer for the API.

A run downloads tens of ~4 MB cutouts on first use, far longer than one HTTP
request should take, so runs are background jobs: `request_job` starts one (or
returns the one already known for that position) and the client polls.
Jobs live in this process's memory: a deployment with several API worker
processes would need a shared store instead.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np
from astropy.io import fits

from planetx.api.schemas import (
    ArchiveCheck,
    MotionObject,
    MotionPass,
    MotionResponse,
    MotionResult,
    TrackPoint,
    Variability,
    Visit,
)
from planetx.engine import (
    EngineResult,
    Frame,
    TrackedObject,
    find_motion,
    frame_from_hdul,
    group_into_passes,
)
from planetx.engine.motion import DAYS_PER_YEAR
from planetx.observatories import FusedMotion, refine_objects
from planetx.spherex import archive, cutouts

log = logging.getLogger(__name__)

VISIT_RADIUS_ARCSEC = 10.0
# The sample the engine was validated on (tools/devview.py defaults): detectors
# 1-3, up to 6 frames per pass and detector, about 54 frames for three passes.
DETECTORS = (1, 2, 3)
FRAMES_PER_PASS_DETECTOR = 6
PASS_GAP_DAYS = 60.0  # same split as find_motion's default
MIN_FRAMES = 2
# IRSA's image search fails in bursts of 502/503 that outlast the archive client's own
# quick retries. A background run can afford to wait them out: extra rounds after these pauses.
LIST_RETRY_WAITS_S = (5.0, 15.0, 30.0)
# Leave one of archive.MAX_CONCURRENT_REQUESTS free for the interactive endpoints.
DOWNLOAD_WORKERS = 3
# IRSA statuses that mean "this file has no data at this position".
NO_CUTOUT_STATUS = frozenset({400, 404, 422})
MAX_WAITING_JOBS = 4  # queued behind the one that is running
MAX_KEPT_JOBS = 32
JOB_TTL_S = 3600.0
# The engine's own data (frames, detections) is kept only for the newest finished runs:
# about 15 MB each, needed by follow-up requests such as evidence images.
MAX_ENGINE_KEPT = 4
# A failure stays visible this long, so everyone polling the run sees it; after that a
# new request retries.
FAILED_TTL_S = 10.0
# Clients poll every few seconds. A run nobody has asked about for this long is dropped
# (the visitor moved on), so it stops holding the worker and IRSA download slots.
ABANDON_S = 30.0
# A track short of the engine's mover test but still worth a look: this
# significant, seen in this many passes (as in tools/devview.py).
CANDIDATE_SIGMA = 3.0
CANDIDATE_MIN_PASSES = 3


class MotionBusy(Exception):
    """Too many motion runs are already waiting."""


class Abandoned(Exception):
    """Nobody is polling this run any more."""


class JobFailed(Exception):
    """A run could not produce a result; the message is safe to show to users."""


@dataclass(frozen=True)
class EngineRun:
    """What the engine saw in a finished run, for follow-up requests about its objects."""

    result: EngineResult
    tracked: list[TrackedObject]  # tracked[i] is MotionResult.objects[i] (MotionObject.id == i)


@dataclass
class Job:
    ra: float
    dec: float
    size_deg: float
    status: str = "running"  # running | done | failed
    stage: str = "queued"  # queued | listing | downloading | analysing | checking
    frames_done: int = 0
    frames_total: int = 0
    error: str | None = None
    result: MotionResult | None = None
    engine: EngineRun | None = None  # dropped once MAX_ENGINE_KEPT newer runs finished
    finished: float | None = None  # time.monotonic() when it ended
    last_polled: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def response(self) -> MotionResponse:
        with self.lock:
            return MotionResponse(
                ra=self.ra, dec=self.dec, size_deg=self.size_deg, status=self.status,
                stage=self.stage, frames_done=self.frames_done, frames_total=self.frames_total,
                error=self.error, result=self.result,
            )


_jobs: OrderedDict[tuple[float, float, float], Job] = OrderedDict()
_jobs_lock = threading.Lock()
# One run at a time: the engine is CPU-bound and each run already downloads in parallel.
_runner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="motion")


def request_job(ra: float, dec: float, size_deg: float) -> MotionResponse:
    """State of the run for this position, starting one if there is none.

    The position is snapped like the cutout cache (0.001 deg), so nearby
    requests share a run and its downloads. A failed run is reported for
    FAILED_TTL_S and then forgotten, so asking again retries it.
    """
    ra, dec, _ = cutouts.cutout_request(ra, dec, size_deg)
    key = (ra, dec, round(size_deg, 5))
    now = time.monotonic()
    created = False
    with _jobs_lock:
        for k, j in list(_jobs.items()):
            ttl = FAILED_TTL_S if j.status == "failed" else JOB_TTL_S
            if j.finished is not None and now - j.finished > ttl:
                del _jobs[k]
        job = _jobs.get(key)
        if job is None:
            if sum(j.status == "running" for j in _jobs.values()) > MAX_WAITING_JOBS:
                raise MotionBusy("too many motion runs are waiting; try again shortly")
            finished = [k for k, j in _jobs.items() if j.status != "running"]
            for k in finished[: max(0, len(_jobs) + 1 - MAX_KEPT_JOBS)]:
                del _jobs[k]
            job = _jobs[key] = Job(ra=ra, dec=dec, size_deg=key[2])
            created = True
        _set(job, last_polled=now)
    if created:
        _runner.submit(_run, job)  # outside the lock: the run takes it when it finishes
    return job.response()


def finished_run(ra: float, dec: float, size_deg: float) -> EngineRun | None:
    """The engine data of the finished run for this position, if it is still kept.

    Takes the same (ra, dec, size_deg) as `request_job`. None means there is no
    finished run or its data was dropped: ask for the run again with `request_job`.
    """
    ra, dec, _ = cutouts.cutout_request(ra, dec, size_deg)
    with _jobs_lock:
        job = _jobs.get((ra, dec, round(size_deg, 5)))
        return job.engine if job is not None else None


def shutdown() -> None:
    """Drop queued runs so the process can exit without working through them."""
    _runner.shutdown(wait=False, cancel_futures=True)


def _check_wanted(job: Job) -> None:
    with job.lock:
        idle = time.monotonic() - job.last_polled
    if idle > ABANDON_S:
        raise Abandoned()


def _run(job: Job) -> None:
    try:
        result, engine = _analyse(job)
    except Abandoned:
        _finish(job, error="the run was dropped because nobody was waiting for it")
    except JobFailed as exc:
        _finish(job, error=str(exc))
    except archive.UpstreamError as exc:
        log.warning("motion run ra=%s dec=%s: %s", job.ra, job.dec, exc)
        _finish(job, error=_upstream_message(exc))
    except Exception as exc:  # a job must never stay "running" after its thread died
        log.exception("motion run failed at ra=%s dec=%s", job.ra, job.dec)
        _finish(job, error=f"motion engine error ({type(exc).__name__})")
    else:
        _finish(job, result=result, engine=engine)
        with _jobs_lock:
            holding = [j for j in _jobs.values() if j.engine is not None and j is not job]
            for old in holding[: max(0, len(holding) + 1 - MAX_ENGINE_KEPT)]:
                old.engine = None


def _finish(
    job: Job, result: MotionResult | None = None, engine: EngineRun | None = None, error: str | None = None
) -> None:
    with job.lock:
        job.status = "done" if error is None else "failed"
        job.result, job.engine, job.error, job.finished = result, engine, error, time.monotonic()


def _upstream_message(exc: archive.UpstreamError) -> str:
    """A short reason for users; the full upstream text (often an HTML error page) goes to the log."""
    status = f"HTTP {exc.status}" if exc.status else "no answer"
    if exc.transient:
        return f"IRSA, the SPHEREx archive, is temporarily unavailable ({status}); it usually recovers within minutes"
    return f"IRSA, the SPHEREx archive, refused the request ({status})"


def _list_visits(job: Job) -> list[Visit]:
    """The visits at the job's position, waiting out short IRSA outages."""
    for wait in (*LIST_RETRY_WAITS_S, None):
        try:
            return archive.query_visits(job.ra, job.dec, VISIT_RADIUS_ARCSEC)
        except archive.UpstreamError as exc:
            if wait is None or not exc.transient:
                raise
            log.warning("motion run ra=%s dec=%s: visit listing failed (%s); retrying in %gs", job.ra, job.dec, exc, wait)
        time.sleep(wait)
        _check_wanted(job)
    raise AssertionError("unreachable")


def _set(job: Job, **changes) -> None:
    with job.lock:
        for name, value in changes.items():
            setattr(job, name, value)


def pick_visits(visits: list[Visit]) -> list[Visit]:
    """At most FRAMES_PER_PASS_DETECTOR visits per (pass, detector), spread evenly in time."""
    visits = sorted(
        (v for v in visits if v.detector in DETECTORS), key=lambda v: (v.mjd, v.detector, v.access_url)
    )
    picked: list[Visit] = []
    for ids in group_into_passes([v.mjd for v in visits], PASS_GAP_DAYS):
        for detector in DETECTORS:
            mine = [visits[i] for i in ids if visits[i].detector == detector]
            if not mine:
                continue
            n = min(FRAMES_PER_PASS_DETECTOR, len(mine))
            for i in np.unique(np.linspace(0, len(mine) - 1, n).round().astype(int)):
                picked.append(mine[i])
    return picked


def _load_frame(visit: Visit, request: tuple[float, float, float]) -> Frame:
    for attempt in (1, 2):
        try:
            with fits.open(cutouts.cached_cutout(visit.access_url, *request)) as hdul:
                return frame_from_hdul(hdul, obs_id=visit.obs_id)
        except FileNotFoundError:
            # Evicted from the cache between download and open: fetch it again once.
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


def _analyse(job: Job) -> tuple[MotionResult, EngineRun]:
    _check_wanted(job)
    _set(job, stage="listing")
    visits = pick_visits(_list_visits(job))
    if len(visits) < MIN_FRAMES:
        raise JobFailed("SPHEREx has too few exposures of this position in detectors 1-3")
    _set(job, stage="downloading", frames_total=len(visits))
    request = cutouts.cutout_request(job.ra, job.dec, job.size_deg)

    frames: list[Frame] = []
    no_data, failures = 0, []

    def load(visit: Visit) -> None:
        nonlocal no_data
        try:
            _check_wanted(job)
            frame = _load_frame(visit, request)
        except Abandoned:
            return
        except archive.UpstreamError as exc:
            with job.lock:
                if exc.status in NO_CUTOUT_STATUS and not isinstance(exc, archive.IrsaBusy):
                    no_data += 1
                else:
                    failures.append(str(exc))
        except (KeyError, ValueError, OSError) as exc:  # a cutout the engine cannot read
            with job.lock:
                failures.append(f"{type(exc).__name__}: {exc}")
        else:
            with job.lock:
                frames.append(frame)
        finally:
            with job.lock:
                job.frames_done += 1

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS, thread_name_prefix="motion-dl") as pool:
        list(pool.map(load, visits))
    _check_wanted(job)
    if failures:
        log.warning("motion run ra=%s dec=%s: %d frames failed, e.g. %s", job.ra, job.dec, len(failures), failures[0])
    if len(frames) < MIN_FRAMES:
        reason = failures[0] if failures else "no exposure has data at this position"
        raise JobFailed(f"only {len(frames)} of {len(visits)} frames could be loaded ({reason})")

    _set(job, stage="analysing")
    # The engine's result depends on input order; a fixed order makes runs repeatable.
    frames.sort(key=lambda f: (f.mjd, f.detector, f.obs_id))
    try:
        result = find_motion(frames)
    except ValueError as exc:
        raise JobFailed(str(exc)) from exc

    _set(job, stage="checking")
    fusion = refine_objects(result.objects)

    notes = list(result.notes) + list(fusion.notes)
    if no_data:
        notes.append(f"{no_data} exposures had no data at this position")
    if failures:
        notes.append(f"{len(failures)} frames could not be downloaded and were left out")
    return build_result(result, fusion.fused, notes)


def _date(mjd: float) -> str:
    return archive.mjd_to_date(mjd)


def classify(obj: TrackedObject, fused: FusedMotion | None, pixel_arcsec: float) -> str:
    """mover | candidate | static. The archive's verdict, when it has one, overrides SPHEREx alone."""
    archive_says = fused.is_mover if fused is not None else None
    if archive_says if archive_says is not None else obj.is_mover:
        return "mover"
    span_yr = (obj.sources[-1].mjd - obj.sources[0].mjd) / DAYS_PER_YEAR
    if (
        archive_says is None
        and len(obj.sources) >= CANDIDATE_MIN_PASSES
        and obj.pm_significance >= CANDIDATE_SIGMA
        and obj.pm_total * span_yr >= pixel_arcsec
    ):
        return "candidate"
    return "static"


def _finite(value: float | None) -> float | None:
    return float(value) if value is not None and np.isfinite(value) else None


def build_result(
    result: EngineResult, fused: list[FusedMotion], notes: list[str]
) -> tuple[MotionResult, EngineRun]:
    pixel = float(np.median([f.pixel_scale_arcsec for f in result.frames]))
    pairs: list[tuple[MotionObject, TrackedObject]] = []
    for obj, fm in zip(result.objects, fused, strict=True):
        var = obj.variability if obj.is_variable else None
        pairs.append((MotionObject(
            kind=classify(obj, fm, pixel),
            is_variable=obj.is_variable,
            ra=obj.ra, dec=obj.dec, ref_mjd=obj.ref_mjd,
            pm_ra_cosdec=obj.pm_ra_cosdec, pm_dec=obj.pm_dec, pm_total=obj.pm_total,
            pm_err=obj.pm_err, significance=obj.pm_significance,
            position_angle_deg=obj.position_angle_deg,
            parallax_arcsec=_finite(obj.parallax), parallax_err_arcsec=_finite(obj.parallax_err),
            saturated=any(d.saturated for s in obj.sources for d in s.detections),
            track=[
                TrackPoint(ra=s.ra, dec=s.dec, mjd=s.mjd, date=_date(s.mjd),
                           sigma_arcsec=s.sigma_arcsec, n_frames=s.n_det)
                for s in obj.sources
            ],
            variability=Variability(
                band=var["detector"], ratio=var["ratio"], significance=var["significance"],
                change=var["change"], caution=var.get("caution"),
            ) if var else None,
            archive=ArchiveCheck(
                verdict=fm.verdict.value, pm_ra_cosdec=fm.pm_ra_cosdec, pm_dec=fm.pm_dec,
                pm_err=_finite(fm.pm_err), baseline_yr=fm.baseline_yr,
                observatories=[o.value for o in fm.observatories],
            ),
        ), obj))
    rank = {"mover": 0, "candidate": 1, "static": 2}
    pairs.sort(key=lambda p: (rank[p[0].kind], not p[0].is_variable, -p[0].significance))
    objects = [o for o, _ in pairs]
    for i, o in enumerate(objects):
        o.id = i

    passes = []
    for ids in result.input_passes:
        mjds = [result.input_frames[i].mjd for i in ids]
        passes.append(MotionPass(start=_date(min(mjds)), end=_date(max(mjds)), n_frames=len(ids)))
    summary = MotionResult(
        n_frames=len(result.input_frames), passes=passes,
        detectors=sorted({f.detector for f in result.input_frames}),
        pm_floor_arcsec_yr=result.pm_floor_arcsec_yr, objects=objects, notes=notes,
    )
    return summary, EngineRun(result=result, tracked=[t for _, t in pairs])
