"""One field through the whole pipeline: a sky position in, classified objects out.

The steps, each owned by its own package: list the SPHEREx visits and download
a sample of raw cutouts (planetx.spherex), find what moves or varies
(planetx.engine), refit the tracks with archival positions
(planetx.observatories), and check them against Gaia, SIMBAD and SkyBoT
(planetx.classify).

Both the API's motion jobs and the survey processor call `analyse_field`, so a
candidate found by the crawler is exactly what the website would show for that
position. It blocks for as long as the downloads take (minutes on first use),
so callers pass `should_stop` to be able to give up and `on_progress` to watch.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from astropy.io import fits

from planetx.classify import Known, check_objects, unchecked
from planetx.engine import (
    EngineResult,
    Frame,
    TrackedObject,
    find_motion,
    frame_from_hdul,
    group_into_passes,
)
from planetx.observatories import SPHEREX_ONLY, FusedMotion, parse_observatories, refine_objects
from planetx.spherex import archive, cutouts

if TYPE_CHECKING:
    # The visit model `archive.query_visits` returns; it is defined with the API contract.
    from planetx.api.schemas import Visit

log = logging.getLogger(__name__)

VISIT_RADIUS_ARCSEC = 10.0
# The sample the engine was validated on (tools/devview.py defaults): detectors
# 1-3, up to 6 frames per pass and detector, about 54 frames for three passes.
DETECTORS = (1, 2, 3)
FRAMES_PER_PASS_DETECTOR = 6
MIN_FRAMES = 2
# IRSA's image search fails in bursts of 502/503 that outlast the archive client's own
# quick retries. A background run can afford to wait them out: extra rounds after these pauses.
LIST_RETRY_WAITS_S = (5.0, 15.0, 30.0)
# Leave one of archive.MAX_CONCURRENT_REQUESTS free for the interactive endpoints.
DOWNLOAD_WORKERS = 3
# IRSA statuses that mean "this file has no data at this position".
NO_CUTOUT_STATUS = frozenset({400, 404, 422})

STAGES = ("listing", "downloading", "analysing", "checking")
Progress = Callable[[str, int, int], None]
"""Called as (stage, frames_done, frames_total) whenever one of them changes; may be called from several threads."""


class Stopped(Exception):
    """`should_stop()` turned true: the run was given up between two steps."""


class FieldFailed(Exception):
    """The field cannot produce a result (too few exposures, unreadable frames).

    The message is safe to show to users. Trying again will fail the same way
    until the archive holds more data, unlike `archive.UpstreamError`.
    """


@dataclass(frozen=True)
class FieldObject:
    """One tracked object with everything the pipeline learned about it."""

    tracked: TrackedObject  # the engine's SPHEREx-only track
    fused: FusedMotion  # the refit with archival positions
    known: Known  # motion class, name and classification after the catalogue check


@dataclass(frozen=True)
class FieldRun:
    result: EngineResult
    objects: list[FieldObject]  # movers first, then candidates, then the rest
    notes: list[str]  # caveats from the engine, the archival refit and the catalogue check


def pick_visits(visits: list[Visit]) -> list[Visit]:
    """At most FRAMES_PER_PASS_DETECTOR visits per (pass, detector), spread evenly in time.

    Passes are split by the engine's own `group_into_passes`, so the sample
    follows whatever the engine takes a pass to be.
    """
    visits = sorted(
        (v for v in visits if v.detector in DETECTORS), key=lambda v: (v.mjd, v.detector, v.access_url)
    )
    picked: list[Visit] = []
    for ids in group_into_passes([v.mjd for v in visits]):
        for detector in DETECTORS:
            mine = [visits[i] for i in ids if visits[i].detector == detector]
            if not mine:
                continue
            n = min(FRAMES_PER_PASS_DETECTOR, len(mine))
            for i in np.unique(np.linspace(0, len(mine) - 1, n).round().astype(int)):
                picked.append(mine[i])
    return picked


def _check(should_stop: Callable[[], bool]) -> None:
    if should_stop():
        raise Stopped()


def _list_visits(ra: float, dec: float, should_stop: Callable[[], bool]) -> list[Visit]:
    """The visits at a position, waiting out short IRSA outages."""
    for wait in (*LIST_RETRY_WAITS_S, None):
        try:
            return archive.query_visits(ra, dec, VISIT_RADIUS_ARCSEC)
        except archive.UpstreamError as exc:
            if wait is None or not exc.transient:
                raise
            log.warning("field ra=%s dec=%s: visit listing failed (%s); retrying in %gs", ra, dec, exc, wait)
        time.sleep(wait)
        _check(should_stop)
    raise AssertionError("unreachable")


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


def _load_frames(
    visits: list[Visit], request: tuple[float, float, float], should_stop: Callable[[], bool], on_progress: Progress
) -> tuple[list[Frame], int, list[str]]:
    """Download and read the frames in parallel: (frames, exposures without data here, failures)."""
    frames: list[Frame] = []
    failures: list[str] = []
    counts = {"no_data": 0, "done": 0}
    lock = threading.Lock()

    def load(visit: Visit) -> None:
        try:
            if should_stop():
                return
            frame = _load_frame(visit, request)
        except archive.UpstreamError as exc:
            with lock:
                if exc.status in NO_CUTOUT_STATUS and not isinstance(exc, archive.IrsaBusy):
                    counts["no_data"] += 1
                else:
                    failures.append(str(exc))
        except (KeyError, ValueError, OSError) as exc:  # a cutout the engine cannot read
            with lock:
                failures.append(f"{type(exc).__name__}: {exc}")
        else:
            with lock:
                frames.append(frame)
        finally:
            with lock:
                counts["done"] += 1
                on_progress("downloading", counts["done"], len(visits))

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS, thread_name_prefix="field-dl") as pool:
        list(pool.map(load, visits))
    return frames, counts["no_data"], failures


def _cross_check(result: EngineResult) -> tuple[list[FusedMotion], list[Known], list[str]]:
    """Archival refit and catalogue check of the engine's objects.

    PLANETX_OBSERVATORIES=spherex means SPHEREx only, no other service: the
    catalogue check is skipped too and every classification stays None.
    """
    observatories = parse_observatories()
    fusion = refine_objects(result.objects, observatories=observatories)
    notes = list(fusion.notes)
    pixel = float(np.median([f.pixel_scale_arcsec for f in result.frames]))
    if observatories == SPHEREX_ONLY:
        return fusion.fused, unchecked(result.objects, fusion.fused, pixel), notes
    checked = check_objects(result.objects, fusion.fused, pixel, stacked=result.stacked)
    return fusion.fused, checked.known, notes + checked.notes


def analyse_field(
    ra: float,
    dec: float,
    size_deg: float,
    *,
    should_stop: Callable[[], bool] = lambda: False,
    on_progress: Progress = lambda stage, done, total: None,
) -> FieldRun:
    """Run the whole pipeline on the field of side `size_deg` centred on (ra, dec).

    Raises `Stopped` when `should_stop()` turns true between steps, `FieldFailed`
    when the field has too little usable data, and `archive.UpstreamError` when
    IRSA cannot list the visits. Outages of the cross-check services do not
    raise: they are reported in `FieldRun.notes`.
    """
    _check(should_stop)
    on_progress("listing", 0, 0)
    visits = pick_visits(_list_visits(ra, dec, should_stop))
    if len(visits) < MIN_FRAMES:
        raise FieldFailed("SPHEREx has too few exposures of this position in detectors 1-3")
    on_progress("downloading", 0, len(visits))
    frames, no_data, failures = _load_frames(visits, cutouts.cutout_request(ra, dec, size_deg), should_stop, on_progress)
    _check(should_stop)
    if failures:
        log.warning("field ra=%s dec=%s: %d frames failed, e.g. %s", ra, dec, len(failures), failures[0])
    if len(frames) < MIN_FRAMES:
        reason = failures[0] if failures else "no exposure has data at this position"
        raise FieldFailed(f"only {len(frames)} of {len(visits)} frames could be loaded ({reason})")

    on_progress("analysing", len(visits), len(visits))
    # The engine's result depends on input order; a fixed order makes runs repeatable.
    frames.sort(key=lambda f: (f.mjd, f.detector, f.obs_id))
    try:
        result = find_motion(frames)
    except ValueError as exc:
        raise FieldFailed(str(exc)) from exc

    _check(should_stop)
    on_progress("checking", len(visits), len(visits))
    fused, known, notes = _cross_check(result)
    notes = [*result.notes, *notes]
    if no_data:
        notes.append(f"{no_data} exposures had no data at this position")
    if failures:
        notes.append(f"{len(failures)} frames could not be downloaded and were left out")

    rank = {"mover": 0, "candidate": 1, "static": 2}
    objects = [FieldObject(t, f, k) for t, f, k in zip(result.objects, fused, known, strict=True)]
    objects.sort(key=lambda o: (rank[o.known.kind], not o.tracked.is_variable, -o.tracked.pm_significance))
    return FieldRun(result=result, objects=objects, notes=notes)
