"""SPHEREx spectra of one sky position for the website, as background jobs.

The measurement itself is `planetx.spherex.spectrum` (pure functions). This module
picks the exposures, fetches their cutouts through the shared disk cache, measures
them and shapes the answer. A first run downloads tens of ~4 MB cutouts, so runs
are background jobs polled by the client, as for /api/motion (same rules: jobs in
this process's memory, one run at a time, dropped when nobody polls).

Exposures: every exposure already in the cutout cache at this position is used
(free); a detector with fewer than NEW_PER_DETECTOR cached ones is topped up with
new downloads spread evenly over time, so asking again downloads nothing new. Each exposure sees the object at its own wavelength, so spreading them
spreads the wavelengths; ~12 per detector fill about half of its 17 bands.
"""
from __future__ import annotations

import logging
import threading
import time
import warnings
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np
from astropy.io import fits
from astropy.time import Time

from planetx.api.motion import _upstream_message
from planetx.api.schemas import (
    SpectrumBand,
    SpectrumCounts,
    SpectrumFeature,
    SpectrumPoint,
    SpectrumResponse,
    SpectrumResult,
    Visit,
)
from planetx.spherex import archive, cutouts
from planetx.spherex import spectrum as sp

log = logging.getLogger(__name__)

NEW_PER_DETECTOR = 12
MAX_CACHED_FRAMES = 300  # cached exposures are free to fetch but still cost ~0.1 s each to measure
VISIT_RADIUS_ARCSEC = 10.0
# Raw cutout side: the background annulus (8 px), the object's motion over the survey
# and the 0.001 deg centre snapping, with margin.
CUTOUT_DEG = 0.045
# Cutouts that /api/frames and /api/motion fetch at their default size (0.1 deg) are
# also used when they exist: they are larger, so they hold the aperture too.
SHARED_SIZE_DEG = 0.1
DOWNLOAD_WORKERS = 3
NO_CUTOUT_STATUS = frozenset({400, 404, 422})
MAX_PM_MAS = 20000.0  # Barnard's Star is 10,400 mas/yr

MAX_WAITING_JOBS = 4
MAX_KEPT_JOBS = 32
JOB_TTL_S = 3600.0
FAILED_TTL_S = 10.0
ABANDON_S = 30.0


class SpectrumBusy(Exception):
    """Too many spectrum runs are already waiting."""


class _Stopped(Exception):
    pass


@dataclass
class Job:
    ra: float
    dec: float
    pmra: float
    pmdec: float
    epoch: float
    status: str = "running"
    stage: str = "queued"
    frames_done: int = 0
    frames_total: int = 0
    error: str | None = None
    result: SpectrumResult | None = None
    finished: float | None = None
    last_polled: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def response(self) -> SpectrumResponse:
        with self.lock:
            return SpectrumResponse(
                ra=self.ra, dec=self.dec, pmra=self.pmra, pmdec=self.pmdec, epoch=self.epoch,
                status=self.status, stage=self.stage, frames_done=self.frames_done,
                frames_total=self.frames_total, error=self.error, result=self.result,
            )


_jobs: OrderedDict[tuple, Job] = OrderedDict()
_jobs_lock = threading.Lock()
_runner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="spectrum")


def request_job(ra: float, dec: float, pmra: float = 0.0, pmdec: float = 0.0, epoch: float | None = None) -> SpectrumResponse:
    """State of the spectrum run for this object, starting one if there is none.

    Unlike the motion runs the position is NOT snapped to the cutout grid: the
    aperture must sit on the object. Keys are rounded to 0.036" and 0.1 mas/yr.
    `epoch` (Julian year of ra/dec) only matters with a proper motion; it defaults to now.
    """
    if epoch is None:
        epoch = round(Time.now().jyear, 3)
    if pmra == 0 and pmdec == 0:
        epoch = 2000.0  # a fixed object: the epoch is irrelevant, so it must not split the key
    key = (round(ra, 5), round(dec, 5), round(pmra, 1), round(pmdec, 1), round(epoch, 3))
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
                raise SpectrumBusy("too many spectrum runs are waiting; try again shortly")
            finished = [k for k, j in _jobs.items() if j.status != "running"]
            for k in finished[: max(0, len(_jobs) + 1 - MAX_KEPT_JOBS)]:
                del _jobs[k]
            job = _jobs[key] = Job(*key)
            created = True
        with job.lock:
            job.last_polled = now
    if created:
        _runner.submit(_run, job)
    return job.response()


def shutdown() -> None:
    _runner.shutdown(wait=False, cancel_futures=True)


def _set(job: Job, **changes) -> None:
    with job.lock:
        for name, value in changes.items():
            setattr(job, name, value)


def _abandoned(job: Job) -> bool:
    with job.lock:
        return time.monotonic() - job.last_polled > ABANDON_S


def _finish(job: Job, result: SpectrumResult | None = None, error: str | None = None) -> None:
    with job.lock:
        job.status = "done" if error is None else "failed"
        job.result, job.error, job.finished = result, error, time.monotonic()


def _run(job: Job) -> None:
    try:
        result = build_spectrum(job)
    except _Stopped:
        _finish(job, error="the run was dropped because nobody was waiting for it")
    except archive.UpstreamError as exc:
        log.warning("spectrum ra=%s dec=%s: %s", job.ra, job.dec, exc)
        _finish(job, error=_upstream_message(exc))
    except Exception as exc:  # a job must never stay "running" after its thread died
        log.exception("spectrum failed at ra=%s dec=%s", job.ra, job.dec)
        _finish(job, error=f"spectrum error ({type(exc).__name__})")
    else:
        _finish(job, result=result)


def _where(job: Job, mjd: float) -> tuple[float, float]:
    return sp.position_at(job.ra, job.dec, job.pmra, job.pmdec, job.epoch, mjd)


def _cached_request(visit: Visit, requests: list[tuple[float, float, float]]) -> tuple[float, float, float] | None:
    for req in requests:
        if cutouts.cache_path(visit.access_url, *req).exists():
            return req
    return None


def select_visits(
    visits: list[Visit], requests: list[tuple[float, float, float]], per_detector: int | None = None,
) -> list[tuple[Visit, tuple[float, float, float]]]:
    """The exposures to measure, each with the cutout request to read it from.

    All cached ones (up to MAX_CACHED_FRAMES); a detector with fewer than
    `per_detector` cached is topped up with others spread evenly over time. `requests[0]` is the one new downloads use.
    """
    picked: list[tuple[Visit, tuple[float, float, float]]] = []
    cached: dict[int, int] = {}
    uncached: dict[int, list[Visit]] = {}
    for v in visits:
        if v.detector is None:
            continue
        req = _cached_request(v, requests) if len(picked) < MAX_CACHED_FRAMES else None
        if req is not None:
            picked.append((v, req))
            cached[v.detector] = cached.get(v.detector, 0) + 1
        else:
            uncached.setdefault(v.detector, []).append(v)
    for det in sorted(uncached):
        mine = uncached[det]  # visits come sorted by mjd
        n = min((NEW_PER_DETECTOR if per_detector is None else per_detector) - cached.get(det, 0), len(mine))
        if n > 0:
            for i in np.unique(np.linspace(0, len(mine) - 1, n).round().astype(int)):
                picked.append((mine[i], requests[0]))
    return picked


def _measure_one(job: Job, visit: Visit, request: tuple[float, float, float]) -> sp.Measurement:
    ra, dec = _where(job, visit.mjd)
    for attempt in (1, 2):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # astropy's INFO/warnings about SPHEREx headers
                with fits.open(cutouts.cached_cutout(visit.access_url, *request)) as hdul:
                    return sp.measure(hdul, ra, dec)
        except FileNotFoundError:  # evicted between download and open: fetch it again once
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


def build_spectrum(job: Job, should_stop=None) -> SpectrumResult:
    """List the exposures, measure them and combine them into a spectrum."""
    stop = should_stop or (lambda: _abandoned(job))
    _set(job, stage="listing")
    mid_ra, mid_dec = _where(job, Time.now().mjd - 180)  # roughly mid-survey, for the listing
    visits = archive.query_visits(mid_ra, mid_dec, VISIT_RADIUS_ARCSEC)
    if not visits:
        return _result([], SpectrumCounts(), ["SPHEREx has no exposures at this position yet."])
    mid_mjd = float(np.median([v.mjd for v in visits]))
    c_ra, c_dec = _where(job, mid_mjd)
    snapped_ra, snapped_dec, _ = cutouts.cutout_request(c_ra, c_dec, CUTOUT_DEG)
    requests = [(snapped_ra, snapped_dec, CUTOUT_DEG)]
    for ra, dec in {(job.ra, job.dec), (c_ra, c_dec)}:
        requests.append(cutouts.cutout_request(ra, dec, SHARED_SIZE_DEG))
    chosen = select_visits(visits, requests)
    _set(job, stage="measuring", frames_total=len(chosen))

    counts = SpectrumCounts()
    results: list[tuple[Visit, sp.Measurement]] = []
    lock = threading.Lock()
    done = 0

    def work(item: tuple[Visit, tuple[float, float, float]]) -> None:
        nonlocal done
        visit, request = item
        outcome = None
        try:
            if stop():
                return
            m = _measure_one(job, visit, request)
            outcome = m.status if m.status in ("ok", "saturated", "masked", "off_image") else "masked"
        except archive.UpstreamError as exc:
            no_data = exc.status in NO_CUTOUT_STATUS and not isinstance(exc, archive.IrsaBusy)
            outcome = "no_data" if no_data else "failed"
            if not no_data:
                log.warning("spectrum cutout %s: %s", visit.access_url, exc)
        except (KeyError, ValueError, OSError) as exc:
            log.warning("spectrum cutout %s unreadable: %s", visit.access_url, exc)
            outcome = "failed"
        else:
            with lock:
                results.append((visit, m))
        finally:
            with lock:
                if outcome:
                    setattr(counts, outcome, getattr(counts, outcome) + 1)
                done += 1
                _set(job, frames_done=done)

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS, thread_name_prefix="spectrum-dl") as pool:
        list(pool.map(work, chosen))
    if stop():
        raise _Stopped()
    if counts.failed and not counts.ok and not counts.saturated:
        raise archive.UpstreamError(f"no exposure could be read ({counts.failed} failed)", transient=True)
    notes = []
    if counts.failed:
        notes.append(f"{counts.failed} exposure(s) could not be fetched from IRSA and were left out.")
    return _result(results, counts, notes)


def _result(results: list[tuple[Visit, sp.Measurement]], counts: SpectrumCounts, notes: list[str]) -> SpectrumResult:
    results = sorted(results, key=lambda r: (r[1].wavelength_um or 0.0, r[0].access_url))
    ms = [m for _, m in results]
    bands, dropped = sp.combine(ms)
    tried = counts.ok + counts.saturated + counts.masked
    saturated = counts.saturated > 0 and counts.saturated >= max(counts.ok, 1)
    if counts.saturated:
        notes.append(
            f"{counts.saturated} of {tried} exposures were saturated at the object (OVERFLOW, BLOOM or "
            "NONLINEAR flags in the aperture) and were left out."
        )
    if counts.masked:
        notes.append(f"{counts.masked} exposure(s) had flagged pixels in the aperture (e.g. persistence) and were left out.")
    if counts.off_image:
        notes.append(f"{counts.off_image} exposure(s) had the object too close to the image edge.")
    notes.append(
        "Exposures from all survey passes are combined: a variable object's spectrum mixes its states, "
        "and a neighbour within ~10 arcsec (SPHEREx pixels are 6.2 arcsec) adds to the flux."
    )
    features = sp.absorption_features(bands)
    code, summary = sp.interpret(features, len(bands), saturated)
    points = []
    for i, (visit, m) in enumerate(results):
        if m.status != "ok":
            continue
        points.append(SpectrumPoint(
            detector=m.detector, mjd=m.mjd, date=archive.mjd_to_date(m.mjd), wavelength_um=m.wavelength_um,
            bandwidth_um=m.bandwidth_um, flux_mjy=m.flux_mjy, flux_err_mjy=m.flux_err_mjy,
            band=sp.band_index(m.detector, m.wavelength_um), outlier=i in dropped,
        ))
    return SpectrumResult(
        points=points,
        bands=[
            SpectrumBand(
                band=b.band.index, detector=b.band.detector, wavelength_um=b.band.center_um,
                lo_um=b.band.lo_um, hi_um=b.band.hi_um, flux_mjy=b.flux_mjy, flux_err_mjy=b.flux_err_mjy,
                n_used=b.n_used, n_rejected=b.n_rejected,
            )
            for b in bands
        ],
        features=[
            SpectrumFeature(
                key=f.definition.key, molecule=f.definition.molecule, label=f.definition.label,
                lo_um=f.definition.window_um[0], hi_um=f.definition.window_um[1],
                depth=f.depth, depth_err=f.depth_err, significance=f.significance,
                continuum=f.continuum, detected=f.detected,
            )
            for f in features
        ],
        suggestion=code, summary=summary, saturated=saturated, counts=counts,
        aperture_arcsec=sp.APERTURE_PX * cutouts.PIXEL_SCALE_ARCSEC, notes=notes,
    )
