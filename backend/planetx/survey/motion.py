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
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from planetx import pipeline
from planetx.spherex import cutouts
from planetx.survey import patch_frames
from planetx.survey.candidates import Candidate, CandidateStore, TrackPoint, is_notable, score
from planetx.survey.patches import centers, children, patches_containing, validate_nside
from planetx.survey.queue import Patch
from planetx.survey.worker import PatchAbandoned, PatchContext

log = logging.getLogger(__name__)

FIELD_NSIDE = 512
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


def _analyse_fields(
    patch: Patch, fields: Sequence[Field], frames: patch_frames.PatchFrames, should_stop: Callable[[], bool]
) -> tuple[list[Candidate], int, int]:
    """(notable objects owned by the patch, objects owned, fields without data)."""
    found: list[Candidate] = []
    owned_objects = 0
    no_data = 0
    for i, field in enumerate(fields):
        if should_stop():
            raise PatchAbandoned(f"stopped before field {i + 1} of {len(fields)}")
        try:
            run = frames.field_run(i, should_stop)
        except pipeline.Stopped:
            raise PatchAbandoned(f"stopped during field {i + 1} of {len(fields)}") from None
        except pipeline.FieldFailed as exc:
            log.info("patch %d field %d/%d (ra %.4f dec %.4f): %s",
                     patch.ipix, i + 1, len(fields), field.ra, field.dec, exc)
            no_data += 1
            continue
        if not run.objects:
            continue
        ra = np.array([o.tracked.ra for o in run.objects])
        dec = np.array([o.tracked.dec for o in run.objects])
        mine = (patches_containing(ra, dec, patch.nside) == patch.ipix) & (nearest_field(ra, dec, fields) == i)
        owned = [o for o, keep in zip(run.objects, mine, strict=True) if keep]
        owned_objects += len(owned)
        found += [_candidate(o, field) for o in owned if is_notable(o.known.kind, o.tracked.is_variable)]
        log.info("patch %d field %d/%d: %d objects, %d here, %d notable",
                 patch.ipix, i + 1, len(fields), len(run.objects), len(owned), len(found))

    return found, owned_objects, no_data


def analyse_patch(
    patch: Patch, fields: Sequence[Field], should_stop: Callable[[], bool]
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
            found, owned_objects, no_data = _analyse_fields(patch, fields, frames, should_stop)
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


def process_patch(ctx: PatchContext) -> dict[str, Any]:
    """The `motion` processor (see the module docstring)."""
    patch = ctx.patch
    found, summary = analyse_patch(patch, patch_fields(patch.ipix, patch.nside), ctx.should_stop)
    if ctx.should_stop():  # lost the lease while finishing: another worker may be writing
        raise PatchAbandoned("stopped before storing the results")
    store(ctx.db_path, patch, found)
    return summary

