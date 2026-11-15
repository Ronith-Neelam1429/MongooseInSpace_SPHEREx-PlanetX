"""The `motion` survey processor: the field pipeline over every field of one patch.

A patch (nside 64, about 0.92 deg) is far larger than one pipeline field
(`planetx.pipeline.analyse_field`, validated up to 0.2 deg), so it is tiled:

  * Fields are centred on the patch's HEALPix sub-pixels at FIELD_NSIDE (512:
    64 fields per nside-64 patch). Every point of a sub-pixel lies within
    0.12 deg of its centre (measured over the whole sky), and a field's frames
    reach at least FIELD_REACH_DEG from its centre in every direction (the raw
    cutout is 1.5x the field, whatever its rotation), so the fields cover the
    patch with margin.
  * The fields' frames come from one raw cutout per SPHEREx visit for the whole
    patch, cropped locally (`planetx.survey.patch_frames`), then go through the
    same per-field pipeline as the website's (`pipeline.analyse_frames`).
  * Fields are analysed FIELD_PROCESSES at a time, one process each (the
    engine is CPU-bound), and their results are taken in field order, so the
    output is the same as one field after another.
  * Neighbouring fields overlap, so the same star is tracked more than once.
    Each object belongs to the field whose centre is nearest to it, and to the
    patch that contains it; every other copy is dropped. That keeps exactly
    one copy, measured in the field where it sits most centrally.

Everything stored for a patch replaces that patch's previous output in one
transaction (processing is at-least-once), and only once every field is done:
a patch given up halfway leaves its previous output as it was.
"""
from __future__ import annotations

import logging
import multiprocessing
import signal
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from planetx import pipeline
from planetx.spherex import cutouts
from planetx.survey import patch_frames
from planetx.survey.candidates import (
    Candidate,
    CandidateStore,
    TrackPoint,
    is_notable,
    score,
)
from planetx.survey.patches import centers, children, patches_containing, validate_nside
from planetx.survey.queue import Patch
from planetx.survey.worker import PatchAbandoned, PatchContext

log = logging.getLogger(__name__)

FIELD_NSIDE = 512
# Fields analysed at once, one process each. A field takes ~20 s of CPU and ~0.5 GB of
# memory; the M1 this runs on has 4 performance cores (and 4 slower ones) and 16 GB.
FIELD_PROCESSES = 4
STOP_POLL_S = 1.0  # how often a patch waiting on its field processes checks should_stop
FIELD_TIMEOUT_S = 1800.0  # a field process that gives no result for this long has died
FIELD_SIZE_DEG = 0.2  # the largest field the pipeline is validated on
# Radius every field's frames cover: half the raw cutout's side, less the 0.001 deg
# by which the cutout centre may be snapped away from the field centre.
FIELD_REACH_DEG = FIELD_SIZE_DEG * 1.5 / 2 - 0.001


@dataclass(frozen=True)
class Field:
    ra: float
    dec: float
    size_deg: float = FIELD_SIZE_DEG


def patch_fields(ipix: int, nside: int) -> list[Field]:
    """The fields tiling patch `ipix`: one per sub-pixel at FIELD_NSIDE, in NESTED order.

    A patch already smaller than a sub-pixel (nside above FIELD_NSIDE) is one field.
    """
    validate_nside(nside)
    ra, dec = centers(children(ipix, nside, max(nside, FIELD_NSIDE)), max(nside, FIELD_NSIDE))
    return [Field(float(r), float(d)) for r, d in zip(ra, dec, strict=True)]


def _unit(ra: np.ndarray, dec: np.ndarray) -> np.ndarray:
    ra, dec = np.radians(ra), np.radians(dec)
    return np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=-1)


def nearest_field(ra: np.ndarray, dec: np.ndarray, fields: Sequence[Field]) -> np.ndarray:
    """Index into `fields` of the field centre nearest to each position (ties: lowest index)."""
    centres = _unit(np.array([f.ra for f in fields]), np.array([f.dec for f in fields]))
    return np.argmax(_unit(np.asarray(ra, float), np.asarray(dec, float)) @ centres.T, axis=1)


def _candidate(found: pipeline.FieldObject, field: Field) -> Candidate:
    t, fused, known = found.tracked, found.fused, found.known
    var = t.variability if t.is_variable else None
    var_sigma = float(var["significance"]) if var else None
    return Candidate(
        ra=float(t.ra), dec=float(t.dec), ref_mjd=float(t.ref_mjd),
        kind=known.kind, classification=known.classification, name=known.name,
        is_variable=bool(t.is_variable),
        pm_ra_cosdec=float(t.pm_ra_cosdec), pm_dec=float(t.pm_dec), pm_total=t.pm_total,
        pm_err=float(t.pm_err), significance=t.pm_significance, position_angle_deg=t.position_angle_deg,
        # As the website reports it: a "confirmation" built on other stars' positions is none.
        archive_verdict="unconfirmed" if known.archive_void else fused.verdict.value,
        track=tuple(
            TrackPoint(ra=float(s.ra), dec=float(s.dec), mjd=float(s.mjd),
                       sigma_arcsec=float(s.sigma_arcsec), n_frames=int(s.n_det))
            for s in t.sources
        ),
        score=score(known.kind, known.classification, significance=t.pm_significance,
                    variability_significance=var_sigma),
        field_ra=field.ra, field_dec=field.dec,
    )


FieldOutcome = tuple[int, list[Candidate]] | str
"""One field's result: (objects owned by the patch, its notable ones), or why it has no data."""


def _field_outcome(
    patch: Patch, fields: Sequence[Field], frames: Any, i: int, should_stop: Callable[[], bool]
) -> FieldOutcome:
    """Run field i and keep the objects it owns (see the module docstring)."""
    try:
        run = frames.field_run(i, should_stop)
    except pipeline.FieldFailed as exc:
        return str(exc)
    if not run.objects:
        return 0, []
    ra = np.array([o.tracked.ra for o in run.objects])
    dec = np.array([o.tracked.dec for o in run.objects])
    mine = (patches_containing(ra, dec, patch.nside) == patch.ipix) & (nearest_field(ra, dec, fields) == i)
    owned = [o for o, keep in zip(run.objects, mine, strict=True) if keep]
    log.debug("patch %d field %d/%d: %d objects, %d here", patch.ipix, i + 1, len(fields), len(run.objects), len(owned))
    return len(owned), [_candidate(o, fields[i]) for o in owned if is_notable(o.known.kind, o.tracked.is_variable)]


# Set in each field process by _start_field_process: its inputs, or why it could not start.
_process_state: tuple[Patch, Sequence[Field], patch_frames.PatchFrames] | BaseException | None = None


def _start_field_process(patch: Patch, fields: Sequence[Field], frames: patch_frames.PatchFrames,
                         log_level: int) -> None:
    global _process_state
    try:
        # Ctrl-C reaches the whole process group; the worker decides what it means (see worker.py).
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s[%(process)d]: %(message)s")
        # This process may download a cutout another process evicted; keep the patch's own.
        cutouts._pinned.update(frames.paths_to_pin())
        _process_state = (patch, fields, frames)
    except BaseException as exc:  # noqa: BLE001 - a raising initializer makes Pool respawn it forever
        _process_state = exc


def _field_in_process(i: int) -> FieldOutcome:
    if not isinstance(_process_state, tuple):
        raise RuntimeError(f"field process failed to start: {_process_state!r}")
    patch, fields, frames = _process_state
    return _field_outcome(patch, fields, frames, i, lambda: False)


def _outcomes(
    patch: Patch, fields: Sequence[Field], frames: Any, should_stop: Callable[[], bool], processes: int
) -> Iterator[FieldOutcome]:
    """Each field's outcome, in field order; fields run in `processes` processes when
    `frames` is a real PatchFrames (the analysis is CPU-bound: ~20 s per field)."""
    if processes <= 1 or not isinstance(frames, patch_frames.PatchFrames) or len(fields) == 1:
        for i in range(len(fields)):
            if should_stop():
                raise PatchAbandoned(f"stopped before field {i + 1} of {len(fields)}")
            try:
                yield _field_outcome(patch, fields, frames, i, should_stop)
            except pipeline.Stopped:
                raise PatchAbandoned(f"stopped during field {i + 1} of {len(fields)}") from None
        return
    ctx = multiprocessing.get_context("spawn")  # no fork: the worker has threads (lease heartbeat)
    pool = ctx.Pool(min(processes, len(fields)), initializer=_start_field_process,
                    initargs=(patch, fields, frames, logging.getLogger().getEffectiveLevel()))
    try:
        results = pool.imap(_field_in_process, range(len(fields)))
        for i in range(len(fields)):
            waited = 0.0
            while True:
                if should_stop():
                    raise PatchAbandoned(f"stopped while analysing field {i + 1} of {len(fields)}")
                try:
                    outcome = results.next(timeout=STOP_POLL_S)
                    break
                except multiprocessing.TimeoutError:
                    waited += STOP_POLL_S
                    # A field process that died (e.g. killed for memory) never answers.
                    if waited >= FIELD_TIMEOUT_S:
                        raise RuntimeError(f"field {i + 1} gave no result in {FIELD_TIMEOUT_S:.0f}s") from None
            yield outcome
    finally:
        pool.terminate()  # all done, or giving up: either way nothing left to wait for
        pool.join()


def _analyse_fields(
    patch: Patch, fields: Sequence[Field], frames: Any, should_stop: Callable[[], bool], processes: int
) -> tuple[list[Candidate], int, int]:
    """(notable objects owned by the patch, objects owned, fields without data)."""
    found: list[Candidate] = []
    owned_objects = 0
    no_data = 0
    for i, outcome in enumerate(_outcomes(patch, fields, frames, should_stop, processes)):
        field = fields[i]
        if isinstance(outcome, str):
            log.info("patch %d field %d/%d (ra %.4f dec %.4f): %s",
                     patch.ipix, i + 1, len(fields), field.ra, field.dec, outcome)
            no_data += 1
            continue
        owned, notable = outcome
        owned_objects += owned
        found += notable
        log.info("patch %d field %d/%d: %d objects here, %d notable so far",
                 patch.ipix, i + 1, len(fields), owned, len(found))
    return found, owned_objects, no_data


def analyse_patch(
    patch: Patch, fields: Sequence[Field], should_stop: Callable[[], bool], processes: int = FIELD_PROCESSES
) -> tuple[list[Candidate], dict[str, Any]]:
    """Run the pipeline on each field; (the patch's notable objects, a JSON summary).

    Raises PatchAbandoned when `should_stop()` turns true, and lets
    `archive.UpstreamError` (IRSA down after the retries) through, so the
    queue retries the patch later. A field with too little data
    (`pipeline.FieldFailed`) is counted and skipped.
    """
    if not fields:
        raise ValueError("a patch needs at least one field")
    started = time.monotonic()
    requests = [cutouts.cutout_request(f.ra, f.dec, f.size_deg) for f in fields]
    try:
        with patch_frames.load(requests, should_stop) as frames:
            found, owned_objects, no_data = _analyse_fields(patch, fields, frames, should_stop, processes)
    except pipeline.Stopped:
        raise PatchAbandoned("stopped while listing or downloading the patch's cutouts") from None
    log.info("patch %d: %d fields analysed in %.0fs", patch.ipix, len(fields), time.monotonic() - started)
    found.sort(key=lambda c: -c.score)
    summary = {
        "processor": "motion",
        "fields": len(fields),
        "fields_analysed": len(fields) - no_data,
        "fields_without_data": no_data,
        "objects": owned_objects,
        "candidates": len(found),
        "by_classification": dict(Counter(c.classification or "unchecked" for c in found)),
        "top_score": found[0].score if found else None,
    }
    return found, summary


def store(db_path: Path, patch: Patch, found: Sequence[Candidate]) -> None:
    """Replace the patch's stored candidates with `found`."""
    with CandidateStore(db_path) as candidates:
        candidates.replace_patch(patch.nside, patch.ipix, found, found_at=time.time())


def process_patch(ctx: PatchContext, processes: int = FIELD_PROCESSES) -> dict[str, Any]:
    """The `motion` processor (see the module docstring)."""
    patch = ctx.patch
    found, summary = analyse_patch(patch, patch_fields(patch.ipix, patch.nside), ctx.should_stop, processes)
    if ctx.should_stop():  # lost the lease while finishing: another worker may be writing
        raise PatchAbandoned("stopped before storing the results")
    store(ctx.db_path, patch, found)
    return summary

