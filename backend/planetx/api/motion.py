"""Run the motion pipeline on one sky position for the website.

The work itself is `planetx.pipeline.analyse_field` (shared with the survey
crawler): visits, cutouts, the engine, the archival refit and the catalogue
check. This module keeps the background jobs and shapes the answer for the API.

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

from planetx import pipeline
from planetx.api.schemas import (
    ArchiveCheck,
    KnownMatch,
    MotionObject,
    MotionPass,
    MotionResponse,
    MotionResult,
    MotionVerdict,
    TrackPoint,
    Variability,
)
from planetx.classify import reliability
from planetx.engine import EngineResult, TrackedObject
from planetx.spherex import archive, cutouts

log = logging.getLogger(__name__)

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


class MotionBusy(Exception):
    """Too many motion runs are already waiting."""


@dataclass(frozen=True)
class EngineRun:
    """What the engine saw in a finished run, for follow-up requests about its objects."""

    result: EngineResult
    tracked: list[TrackedObject]  # tracked[i] is MotionResult.objects[i] (MotionObject.id == i)
    # verdicts[i] is MotionResult.objects[i].motion_verdict, before the evidence view's image tests;
    # empty when unknown.
    verdicts: list[reliability.MotionVerdict] = field(default_factory=list)


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


def request_job(ra: float, dec: float, size_deg: float, need_engine: bool = False) -> MotionResponse:
    """State of the run for this position, starting one if there is none.

    The position is snapped like the cutout cache (0.001 deg), so nearby
    requests share a run and its downloads. A failed run is reported for
    FAILED_TTL_S and then forgotten, so asking again retries it.
    `need_engine` is for follow-up requests that need the engine's data (evidence
    images): a finished run whose data was dropped (MAX_ENGINE_KEPT) is run again,
    where plain polling keeps getting its summary.
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
        if need_engine and job is not None and job.status == "done" and job.engine is None:
            del _jobs[key]
            job = None
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


def _abandoned(job: Job) -> bool:
    """True when nobody has polled the run for ABANDON_S."""
    with job.lock:
        return time.monotonic() - job.last_polled > ABANDON_S


def _run(job: Job) -> None:
    def progress(stage: str, done: int, total: int) -> None:
        _set(job, stage=stage, frames_done=done, frames_total=total)

    try:
        run = pipeline.analyse_field(
            job.ra, job.dec, job.size_deg, should_stop=lambda: _abandoned(job), on_progress=progress
        )
        result, engine = build_result(run)
    except pipeline.Stopped:
        _finish(job, error="the run was dropped because nobody was waiting for it")
    except pipeline.FieldFailed as exc:
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


def _set(job: Job, **changes) -> None:
    with job.lock:
        for name, value in changes.items():
            setattr(job, name, value)


def _date(mjd: float) -> str:
    return archive.mjd_to_date(mjd)


def _finite(value: float | None) -> float | None:
    return float(value) if value is not None and np.isfinite(value) else None


def motion_verdict(v: reliability.MotionVerdict) -> MotionVerdict:
    return MotionVerdict(
        verdict=v.verdict, certainty=v.certainty, cause=v.cause, reason=v.reason, decided_by=v.decided_by,
    )


def motion_object(found: pipeline.FieldObject, verdict: reliability.MotionVerdict | None = None) -> MotionObject:
    """One pipeline object in the API's shape (`id` is left for the caller to number).

    `verdict` is the object's from `reliability.field_verdicts` (which knows the field's
    bright stars); without it the object is judged on its own.
    """
    obj, fm, known = found.tracked, found.fused, found.known
    var = obj.variability if obj.is_variable else None
    return MotionObject(
        kind=known.kind,
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
        # A confirmation built on other stars' positions is no evidence: report it as
        # unconfirmed, without the refit's motion, so it cannot be read as a measurement.
        archive=ArchiveCheck(verdict="unconfirmed", observatories=["spherex"]) if known.archive_void else ArchiveCheck(
            verdict=fm.verdict.value, pm_ra_cosdec=fm.pm_ra_cosdec, pm_dec=fm.pm_dec,
            pm_err=_finite(fm.pm_err), baseline_yr=fm.baseline_yr,
            observatories=[o.value for o in fm.observatories], candidate=fm.archive_candidate, notes=list(fm.notes),
        ),
        name=known.name,
        classification=known.classification,
        matches=[
            KnownMatch(
                catalog=m.catalog, name=m.name, kind=m.kind, sep_arcsec=m.sep_arcsec,
                pm_ra_cosdec=m.pm_ra_cosdec, pm_dec=m.pm_dec, magnitude=m.magnitude,
            )
            for m in known.matches
        ],
        motion_verdict=motion_verdict(verdict or reliability.object_verdict(found)),
    )


def build_result(run: pipeline.FieldRun) -> tuple[MotionResult, EngineRun]:
    """The API summary of a pipeline run, plus the engine data kept for follow-up requests."""
    result = run.result
    pixel = (
        float(np.median([f.pixel_scale_arcsec for f in result.frames])) if result.frames
        else reliability.SPHEREX_PIXEL_ARCSEC
    )
    verdicts = reliability.field_verdicts(run.objects, pixel_arcsec=pixel)
    objects = [motion_object(found, v) for found, v in zip(run.objects, verdicts, strict=True)]
    for i, o in enumerate(objects):
        o.id = i

    passes = []
    for ids in result.input_passes:
        mjds = [result.input_frames[i].mjd for i in ids]
        passes.append(MotionPass(start=_date(min(mjds)), end=_date(max(mjds)), n_frames=len(ids)))
    summary = MotionResult(
        n_frames=len(result.input_frames), passes=passes,
        detectors=sorted({f.detector for f in result.input_frames}),
        pm_floor_arcsec_yr=result.pm_floor_arcsec_yr, objects=objects, notes=run.notes,
    )
    return summary, EngineRun(result=result, tracked=[found.tracked for found in run.objects], verdicts=verdicts)
