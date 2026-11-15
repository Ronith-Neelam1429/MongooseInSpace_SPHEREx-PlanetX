"""Combine SPHEREx tracks with archival positions from other observatories.

SPHEREx alone sees a source 2-4 times over 1-2 years with 6.2" pixels, so its
motions are uncertain by a few tenths of an arcsec per year and a pairing of
two unrelated sources can pass for a fast mover. Older surveys saw the same
sky 10-27 years earlier with ~0.1" positions. For every SPHEREx track this
module:

1. Walks back in time one archival epoch at a time (nearest first), predicts
   where the object was from the motion fitted so far, and accepts the catalog
   source found there. Each accepted epoch tightens the prediction for the
   others, and epochs that could not be matched yet are then tried again.
2. Refits the motion over all accepted epochs: the error shrinks roughly in
   proportion to the baseline (about 20x for a 2MASS match).
3. Gives a verdict: motion confirmed (when SPHEREx alone does not see it, only
   with secure epochs from two independent surveys), object stationary, track refuted (each
   SPHEREx position has its own stationary archival source, so the "mover" was
   two objects), or unconfirmed (nothing found: too faint, blended or new).

With only SPHEREx selected nothing is fetched and the SPHEREx-only fit is
returned, so the same call serves both modes.

Known limits: AllWISE, CatWISE and NEOWISE share exposures, so when several
of them match, the quoted motion error is somewhat optimistic. The NEOWISE
cone (30" around the latest SPHEREx position) only reaches back to 2014 for
motions under about 2.5"/yr; faster objects get their most recent visits only.

Pure computation except `refine_objects`, which fetches through the `fetch`
callable it is given. Parallax is not modelled; for nearby stars it shows up
as chi2 > 1 and inflates the quoted motion error.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

import numpy as np
from scipy.stats import chi2 as chi2_dist

from . import catalogs
from .sources import (
    SPHEREX_ONLY,
    ArchivalSource,
    Observatory,
    family,
    parse_observatories,
)

log = logging.getLogger(__name__)

DAYS_PER_YEAR = 365.25
ARCSEC_PER_RAD = 180.0 / math.pi * 3600.0


class PositionLike(Protocol):
    """One SPHEREx epoch of a track (the engine's `PassSource` fits)."""

    ra: float
    dec: float
    mjd: float
    sigma_arcsec: float


class TrackLike(Protocol):
    """A SPHEREx track (the engine's `TrackedObject` fits)."""

    sources: Sequence[PositionLike]


class Verdict(StrEnum):
    SPHEREX_ONLY = "spherex_only"  # no other observatory was consulted
    CONFIRMED = "confirmed"  # archival epochs lie on the track; motion is significant
    STATIONARY = "stationary"  # archival epochs found; no motion beyond one SPHEREx pixel
    REFUTED = "refuted"  # each SPHEREx position has its own stationary archival source
    UNCONFIRMED = "unconfirmed"  # no (or not enough) archival evidence; engine's flag stands


@dataclass(frozen=True)
class FusionParams:
    # Added in quadrature to every SPHEREx position error (alignment and
    # undersampled-PSF systematics; 0.3" is about 0.05 px).
    spherex_floor_arcsec: float = 0.3
    # Same for catalog positions (reference-frame differences between surveys).
    archive_floor_arcsec: float = 0.1
    # An archival source matches when within gate_sigma x (prediction error +
    # its own error), but never tighter than min_gate nor wider than max_gate.
    gate_sigma: float = 3.0
    min_gate_arcsec: float = 2.0
    max_gate_arcsec: float = 60.0
    # A mover needs motion this significant and at least this large. The lower
    # bound guards against residual frame offsets between surveys.
    mover_sigma: float = 5.0
    min_pm_arcsec_yr: float = 0.05
    # Two catalogs of different families showing a source within this distance
    # are taken to be one (nearly) stationary object.
    anchor_arcsec: float = 3.0
    # Entries of one catalog closer than this are the same object: 2MASS (2"
    # pixels) and WISE (6" beam) cannot separate sources this close.
    duplicate_arcsec: float = 2.0
    # Motion is only believed when the total shift exceeds this (one SPHEREx pixel).
    min_shift_arcsec: float = 6.2
    # A single archival epoch confirms a mover only if SPHEREx alone already
    # sees the motion at this significance; otherwise two epochs are needed.
    spherex_support_sigma: float = 3.0
    # A match counts as secure when fewer than this many unrelated sources, at
    # least as bright, are expected that close to the predicted position.
    max_chance: float = 0.05
    # A fit is inconsistent (its positions do not lie on one line within their
    # errors) when its chi2 is this improbable.
    min_fit_probability: float = 1e-3


@dataclass
class FusedMotion:
    """Motion of one track after combining the selected observatories."""

    verdict: Verdict
    ra: float  # position at `ref_mjd` (the mean SPHEREx date)
    dec: float
    ref_mjd: float
    pm_ra_cosdec: float  # arcsec/yr, east positive
    pm_dec: float  # arcsec/yr, north positive
    pm_err: float  # arcsec/yr per axis
    chi2_dof: float  # nan when the fit has no spare degrees of freedom
    baseline_yr: float
    n_epochs: int  # SPHEREx epochs + matched archival epochs
    matched: list[ArchivalSource] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # SPHEREx position minus the most recent matched archival position
    # (east, north), used to tie the patch to the archival reference frame.
    spherex_minus_archive_arcsec: tuple[float, float] | None = None
    # Verdict UNCONFIRMED, but the secure archival epochs alone show significant
    # motion consistent with the track; they come from one survey family (AllWISE
    # and CatWISE share exposures), so it is a candidate, never a confirmation.
    archive_candidate: bool = False

    @property
    def pm_total(self) -> float:
        return float(math.hypot(self.pm_ra_cosdec, self.pm_dec))

    @property
    def pm_significance(self) -> float:
        return self.pm_total / self.pm_err if self.pm_err > 0 else 0.0

    @property
    def observatories(self) -> tuple[Observatory, ...]:
        seen = dict.fromkeys(s.observatory for s in self.matched)
        return (Observatory.SPHEREX, *seen)

    @property
    def is_mover(self) -> bool | None:
        """True/False when the archive settles it; None = keep the SPHEREx-only flag."""
        if self.verdict is Verdict.CONFIRMED:
            return True
        if self.verdict in (Verdict.STATIONARY, Verdict.REFUTED):
            return False
        return None


@dataclass
class FusionResult:
    observatories: frozenset[Observatory]
    fused: list[FusedMotion]  # one per input track, same order
    # Common SPHEREx-minus-archive shift (east, north) removed before fitting.
    frame_offset_arcsec: tuple[float, float] = (0.0, 0.0)
    archive_counts: dict[Observatory, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# --- geometry -----------------------------------------------------------------


def _project(ra, dec, ra0: float, dec0: float) -> tuple[np.ndarray, np.ndarray]:
    """Gnomonic (tangent-plane) offsets (east, north) in arcsec from (ra0, dec0)."""
    ra, dec = np.radians(np.atleast_1d(ra)), np.radians(np.atleast_1d(dec))
    a0, d0 = math.radians(ra0), math.radians(dec0)
    da = ra - a0
    cosc = math.sin(d0) * np.sin(dec) + math.cos(d0) * np.cos(dec) * np.cos(da)
    x = np.cos(dec) * np.sin(da) / cosc
    y = (math.cos(d0) * np.sin(dec) - math.sin(d0) * np.cos(dec) * np.cos(da)) / cosc
    return x * ARCSEC_PER_RAD, y * ARCSEC_PER_RAD


def _deproject(x: float, y: float, ra0: float, dec0: float) -> tuple[float, float]:
    xi, eta = x / ARCSEC_PER_RAD, y / ARCSEC_PER_RAD
    a0, d0 = math.radians(ra0), math.radians(dec0)
    denom = math.cos(d0) - eta * math.sin(d0)
    ra = a0 + math.atan2(xi, denom)
    dec = math.atan2(math.sin(d0) + eta * math.cos(d0), math.hypot(xi, denom))
    ra_deg = math.degrees(ra) % 360.0
    return (0.0 if ra_deg >= 360.0 else ra_deg), math.degrees(dec)


@dataclass(frozen=True)
class _Fit:
    """Weighted straight-line fit of tangent-plane position against time (years)."""

    tbar: float
    x0: float
    y0: float
    pmx: float
    pmy: float
    wsum: float
    sxx: float
    chi2: float
    n: int

    @property
    def dof(self) -> int:
        return 2 * (self.n - 2)

    def predict(self, t) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        dt = np.asarray(t, dtype=float) - self.tbar
        sigma = np.sqrt(1.0 / self.wsum + dt**2 / self.sxx)
        return self.x0 + self.pmx * dt, self.y0 + self.pmy * dt, sigma


def _fit(x: np.ndarray, y: np.ndarray, t: np.ndarray, sig: np.ndarray) -> _Fit:
    w = 1.0 / sig**2
    wsum = float(w.sum())
    tbar = float((w * t).sum() / wsum)
    dt = t - tbar
    sxx = float((w * dt**2).sum())
    if not sxx > 0:
        raise ValueError("motion fit needs positions at two or more dates")
    out = []
    chi2 = 0.0
    for v in (x, y):
        vbar = float((w * v).sum() / wsum)
        slope = float((w * dt * (v - vbar)).sum() / sxx)
        chi2 += float((w * (v - vbar - slope * dt) ** 2).sum())
        out.append((vbar, slope))
    (x0, pmx), (y0, pmy) = out
    return _Fit(tbar, x0, y0, pmx, pmy, wsum, sxx, chi2, len(t))


def _consistent(fit: _Fit, params: FusionParams) -> bool:
    """True unless the positions scatter about the fitted line far beyond their errors."""
    return fit.dof <= 0 or float(chi2_dist.sf(fit.chi2, fit.dof)) >= params.min_fit_probability


# --- one track ----------------------------------------------------------------


def fuse_track(
    epochs: Sequence[PositionLike],
    archive: Mapping[Observatory, Sequence[ArchivalSource]],
    params: FusionParams = FusionParams(),
    *,
    frame_offset_arcsec: tuple[float, float] = (0.0, 0.0),
) -> FusedMotion:
    """Fit one track's motion using SPHEREx plus whatever `archive` holds.

    `epochs` are the track's SPHEREx positions (two or more dates). `archive`
    maps each consulted observatory to its catalog sources around the track; an
    empty mapping means SPHEREx only. `frame_offset_arcsec` (east, north) is
    subtracted from the SPHEREx positions first.
    """
    if len(epochs) < 2:
        raise ValueError("a track needs at least two SPHEREx epochs")
    order = np.argsort([e.mjd for e in epochs])
    ra0, dec0 = float(epochs[order[0]].ra), float(epochs[order[0]].dec)
    sx, sy = _project([epochs[i].ra for i in order], [epochs[i].dec for i in order], ra0, dec0)
    sx = sx - frame_offset_arcsec[0]
    sy = sy - frame_offset_arcsec[1]
    st = np.array([epochs[i].mjd for i in order], dtype=float) / DAYS_PER_YEAR
    ssig = np.hypot([epochs[i].sigma_arcsec for i in order], params.spherex_floor_arcsec)
    if not (np.isfinite(sx).all() and np.isfinite(sy).all() and np.isfinite(st).all() and (ssig > 0).all()):
        raise ValueError("track has non-finite positions, dates or errors")

    spx = _fit(sx, sy, st, ssig)
    # Like pm_err below: positions scattering beyond their errors deflate it.
    spx_signif = math.hypot(spx.pmx, spx.pmy) * math.sqrt(spx.sxx)
    if spx.dof > 0:
        spx_signif /= math.sqrt(max(1.0, spx.chi2 / spx.dof))
    t_ref = float(st.mean())
    notes: list[str] = []
    matched: list[ArchivalSource] = []
    # Per matched source: would chance alone rarely put a source like it there?
    unlikely: list[bool] = []
    x, y, t, sig = sx, sy, st, ssig

    sources = [s for group in archive.values() for s in group]
    ax = ay = np.empty(0)
    if sources:
        ax, ay = _project([s.ra for s in sources], [s.dec for s in sources], ra0, dec0)
        at = np.array([s.mjd for s in sources], dtype=float) / DAYS_PER_YEAR
        asig = np.hypot([s.sigma_arcsec for s in sources], params.archive_floor_arcsec)
        rx, ry, rsig = spx.predict(t_ref)
        rx, ry, rsig = float(rx), float(ry), float(rsig)

        def other_object(i: int) -> bool:
            """True when source i belongs to a different, (nearly) stationary object.

            If an independent survey shows a source at almost the same place
            many years apart, those two detections define a slow track. Unless
            that track leads to our SPHEREx position, source i is not ours.
            AllWISE and CatWISE do not anchor each other: they share images,
            and CatWISE lists WISE 0855 (8"/yr) not only at 2015.4 but also
            with entries 2-3" from its 2010 AllWISE position.
            """
            near = np.flatnonzero(np.hypot(ax - ax[i], ay - ay[i]) <= params.anchor_arcsec)
            partners = [
                j for j in near
                if family(sources[i].observatory) != family(sources[j].observatory)
                and abs(at[j] - at[i]) >= 1.0
            ]
            for j in partners:
                k = (t_ref - at[i]) / (at[j] - at[i])
                ex = ax[i] + k * (ax[j] - ax[i])
                ey = ay[i] + k * (ay[j] - ay[i])
                err = math.sqrt((1 - k) ** 2 * asig[i] ** 2 + k**2 * asig[j] ** 2)
                tol = max(params.min_gate_arcsec, params.gate_sigma * math.hypot(err, rsig))
                if math.hypot(ex - rx, ey - ry) <= tol:
                    return False
            return bool(partners)

        rungs: dict[tuple[Observatory, int], list[int]] = {}
        for i, s in enumerate(sources):
            rungs.setdefault((s.observatory, s.visit), []).append(i)

        def chance(i: int, miss: float) -> float:
            """Expected number of unrelated sources as bright as i, as close as `miss`.

            Counts same-catalog sources at least as bright (a bright star is
            rarely there by accident, a faint one often is) over the area the
            catalog covers here.
            """
            same = np.array([s.observatory == sources[i].observatory for s in sources])
            sx_, sy_ = ax[same], ay[same]
            extent = float(np.hypot(sx_ - sx_.mean(), sy_ - sy_.mean()).max())
            area = math.pi * max(extent, params.max_gate_arcsec) ** 2
            mags = np.array([s.mag for s in sources])[same]
            mag = sources[i].mag
            n = int((~(mags > mag)).sum()) if math.isfinite(mag) else int(same.sum())
            return n / area * math.pi * max(miss, 0.5) ** 2

        def pick(obs: Observatory, members: list[int]) -> tuple[int | None, str, float]:
            """Source matching the current fit at this epoch (index, "", miss) or why none."""
            idx = np.array(members)
            px, py, psig = _fit(x, y, t, sig).predict(at[idx])
            dist = np.hypot(ax[idx] - px, ay[idx] - py)
            total = np.hypot(psig, asig[idx])
            wanted = params.gate_sigma * total
            gate = np.clip(wanted, params.min_gate_arcsec, params.max_gate_arcsec)
            in_gate = np.flatnonzero(dist <= gate)
            eligible = [k for k in in_gate if not other_object(int(idx[k]))]
            if not eligible:
                if (wanted > params.max_gate_arcsec).any() and not len(in_gate):
                    return None, (
                        f"{obs.value}: predicted position too uncertain "
                        f"(search limited to {params.max_gate_arcsec:g}\")"
                    ), 0.0
                return None, "", 0.0
            eligible.sort(key=lambda k: dist[k] / total[k])
            best = eligible[0]
            # Entries closer together than any of these surveys can resolve are
            # one object listed twice (CatWISE splits fast movers into pieces).
            rivals = [
                k for k in eligible[1:]
                if math.hypot(ax[idx[k]] - ax[idx[best]], ay[idx[k]] - ay[idx[best]])
                > params.duplicate_arcsec
            ]
            if rivals:
                second = rivals[0]
                if dist[second] / total[second] - dist[best] / total[best] < 1.0:
                    # Equally close, but one may be so bright that chance would
                    # rarely put a source like it anywhere in the search area
                    # (Barnard's Star among faint 2MASS field stars, 40" from a
                    # loose SPHEREx-only prediction): take that one. Only when
                    # SPHEREx itself sees the motion in three or more epochs:
                    # otherwise a static star, or two unrelated sources linked
                    # into a "mover" (two points always make a line), would
                    # adopt a bright neighbour such as another track's star.
                    if spx_signif >= params.spherex_support_sigma and len(epochs) >= 3:
                        close = [best] + [
                            k for k in rivals if dist[k] / total[k] - dist[best] / total[best] < 1.0
                        ]
                        rare = [
                            k for k in close
                            if chance(int(idx[k]), float(gate[k])) < params.max_chance
                        ]
                        # ...unless the line through it misses the other epochs
                        # (a bright star beside a fainter mover WISE did see).
                        if len(rare) == 1:
                            i = int(idx[rare[0]])
                            refit = _fit(
                                np.append(x, ax[i]), np.append(y, ay[i]),
                                np.append(t, at[i]), np.append(sig, asig[i]),
                            )
                            if _consistent(refit, params):
                                return i, "", float(dist[rare[0]])
                    return None, (
                        f"{obs.value}: {1 + len(rivals)} possible counterparts, "
                        "none preferred; epoch skipped"
                    ), 0.0
            return int(idx[best]), "", float(dist[best])

        # Nearest in time first: the prediction is tightest there, and each
        # accepted epoch tightens it for the others. Epochs that could not be
        # matched are tried again whenever another epoch has improved the fit.
        pending = sorted(rungs.items(), key=lambda kv: abs(float(np.median(at[kv[1]])) - t_ref))
        secure = False

        # Most sources never moved. Unless SPHEREx itself sees motion, look for
        # the object at its SPHEREx position first: found there at two or more
        # epochs, those are its counterparts, and a noisy SPHEREx-only motion
        # cannot lead the search to a neighbour instead.
        if spx_signif < params.spherex_support_sigma:
            home: list[tuple[tuple[Observatory, int], int, float]] = []
            for key, members in pending:
                idx = np.array(members)
                d = np.hypot(ax[idx] - rx, ay[idx] - ry)
                reach = np.maximum(
                    params.min_gate_arcsec, params.gate_sigma * np.hypot(rsig, asig[idx])
                )
                near = sorted(np.flatnonzero(d <= reach), key=lambda k: d[k])
                if near and all(
                    math.hypot(ax[idx[k]] - ax[idx[near[0]]], ay[idx[k]] - ay[idx[near[0]]])
                    <= params.duplicate_arcsec
                    for k in near[1:]
                ):
                    home.append((key, int(idx[near[0]]), float(d[near[0]])))
            if len(home) >= 2:
                secure = True
                for _, i, miss in home:
                    matched.append(sources[i])
                    unlikely.append(chance(i, miss) < params.max_chance)
                    x, y = np.append(x, ax[i]), np.append(y, ay[i])
                    t, sig = np.append(t, at[i]), np.append(sig, asig[i])
                found = {key for key, _, _ in home}
                pending = [(key, members) for key, members in pending if key not in found]
        while pending:
            failed, reasons = [], []
            for (obs, visit), members in pending:
                i, reason, miss = pick(obs, members)
                if i is None:
                    failed.append(((obs, visit), members))
                    if reason:
                        reasons.append(reason)
                    continue
                unlikely.append(chance(i, miss) < params.max_chance)
                secure = secure or unlikely[-1]
                matched.append(sources[i])
                x, y = np.append(x, ax[i]), np.append(y, ay[i])
                t, sig = np.append(t, at[i]), np.append(sig, asig[i])
            if len(failed) == len(pending):
                notes += list(dict.fromkeys(reasons))
                break
            pending = failed
        # A lone source inside a wide search area may be a bystander. The chain
        # is trusted once any link is one that chance would rarely produce: a
        # bright source, or one that landed right on a tight prediction.
        if matched and not secure:
            notes.append(
                f"{len(matched)} possible archival counterpart(s) too likely to be "
                "chance alignments; not used"
            )
            matched, unlikely = [], []
            x, y, t, sig = sx, sy, st, ssig

    final = _fit(x, y, t, sig)
    chi2_dof = final.chi2 / final.dof if final.dof > 0 else float("nan")
    pm_err = 1.0 / math.sqrt(final.sxx)
    if final.dof > 0 and chi2_dof > 1.0:
        pm_err *= math.sqrt(chi2_dof)  # errors underestimated or motion not linear
    fx, fy, _ = final.predict(t_ref)
    ra, dec = _deproject(float(fx), float(fy), ra0, dec0)
    pm_total = math.hypot(final.pmx, final.pmy)

    offset = None
    archive_candidate = False
    if not archive:
        verdict = Verdict.SPHEREX_ONLY
    elif matched:
        significant = pm_total >= params.mover_sigma * pm_err and pm_total >= params.min_pm_arcsec_yr
        shift = pm_total * float(t.max() - t.min())
        if not significant or shift < params.min_shift_arcsec:
            # Below about one SPHEREx pixel, two surveys' centroids of the same
            # blended source differ by as much as this "motion".
            verdict = Verdict.STATIONARY
        elif spx_signif >= params.spherex_support_sigma:
            verdict = Verdict.CONFIRMED
        else:
            # SPHEREx itself does not see the motion, so the archive alone has
            # to establish it: with epochs from two independent surveys (WISE
            # catalogs share exposures, so a WISE artifact or blend is in all
            # of them), each one a source that chance would rarely put there.
            # Otherwise a chain that started on a chance neighbour - in a
            # crowded field or a bright star's halo there are dozens within
            # the first, loose search area - can be extended by one more
            # neighbour and "confirm" itself.
            independent = {family(s.observatory) for s, ok in zip(matched, unlikely) if ok}
            if len(independent) >= 2:
                verdict = Verdict.CONFIRMED
            else:
                verdict = Verdict.UNCONFIRMED
                if len(matched) == 1:
                    notes.append(
                        "motion rests on a single archival position; needs a second epoch to confirm"
                    )
                else:
                    archive_candidate = _consistent(final, params) and _archive_alone_moves(
                        matched, unlikely, ra0, dec0, params
                    )
                    notes.append(
                        _weak_note(spx_signif, archive, matched, unlikely, archive_candidate, params)
                    )
        latest = max(matched, key=lambda s: s.mjd)
        lx, ly = _project(latest.ra, latest.dec, ra0, dec0)
        offset = (float(spx.x0 - lx[0]), float(spx.y0 - ly[0]))
    else:
        verdict = Verdict.UNCONFIRMED
        # Did SPHEREx link different stationary objects? Then every position of
        # the track has its own archival source sitting at that very spot, years
        # before the "mover" was supposedly there.
        radius = np.maximum(params.min_gate_arcsec, params.gate_sigma * ssig)
        steps = np.hypot(np.diff(sx), np.diff(sy))
        if sources and (steps > 2 * float(radius.max())).all():
            nearest = []
            for k in range(len(sx)):
                d = np.hypot(ax - sx[k], ay - sy[k])
                j = int(np.argmin(d))
                if d[j] <= radius[k]:
                    nearest.append(j)
            apart = all(
                math.hypot(ax[a] - ax[b], ay[a] - ay[b]) > params.duplicate_arcsec
                for n, a in enumerate(nearest) for b in nearest[n + 1:]
            )
            if len(nearest) == len(sx) and apart:
                verdict = Verdict.REFUTED
                notes.append(
                    "every SPHEREx position coincides with its own stationary archival source"
                )

    return FusedMotion(
        verdict=verdict,
        ra=ra,
        dec=dec,
        ref_mjd=t_ref * DAYS_PER_YEAR,
        pm_ra_cosdec=final.pmx,
        pm_dec=final.pmy,
        pm_err=pm_err,
        chi2_dof=chi2_dof,
        baseline_yr=float(t.max() - t.min()),
        n_epochs=final.n,
        matched=matched,
        notes=notes,
        spherex_minus_archive_arcsec=offset,
        archive_candidate=archive_candidate,
    )


def _archive_alone_moves(
    matched: Sequence[ArchivalSource],
    unlikely: Sequence[bool],
    ra0: float,
    dec0: float,
    params: FusionParams,
) -> bool:
    """Do the secure archival positions by themselves show significant motion (> 1 pixel)?"""
    secure = [s for s, ok in zip(matched, unlikely) if ok]
    if len(secure) < 2:
        return False
    t = np.array([s.mjd for s in secure], dtype=float) / DAYS_PER_YEAR
    if float(t.max() - t.min()) < 1.0:
        return False
    x, y = _project([s.ra for s in secure], [s.dec for s in secure], ra0, dec0)
    fit = _fit(x, y, t, np.hypot([s.sigma_arcsec for s in secure], params.archive_floor_arcsec))
    pm = math.hypot(fit.pmx, fit.pmy)
    return (
        pm * math.sqrt(fit.sxx) >= params.mover_sigma
        and pm >= params.min_pm_arcsec_yr
        and pm * float(t.max() - t.min()) >= params.min_shift_arcsec
    )


def _weak_note(
    spx_signif: float,
    archive: Mapping[Observatory, Sequence[ArchivalSource]],
    matched: Sequence[ArchivalSource],
    unlikely: Sequence[bool],
    candidate: bool,
    params: FusionParams,
) -> str:
    """Why a track SPHEREx alone barely sees was not confirmed, naming the evidence."""
    consulted = sorted({family(o) for o in archive})
    named = [f"{s.observatory.value} {s.name or '(unnamed)'}" for s in matched]
    chancy = [n for n, ok in zip(named, unlikely) if not ok]
    secure = sorted({family(s.observatory) for s, ok in zip(matched, unlikely) if ok})
    text = (
        f"SPHEREx alone sees the motion at {spx_signif:.1f} sigma "
        f"(under {params.spherex_support_sigma:g}); survey families consulted: {', '.join(consulted)}; "
        f"matched: {', '.join(named)}"
    )
    if chancy:
        text += f"; likely chance alignments: {', '.join(chancy)}"
    if candidate:
        return text + (
            f"; the secure {secure[0]} positions alone show the motion, but one survey "
            "family's catalogs share exposures: a candidate, not confirmed"
        )
    families = ", ".join(secure) if secure else "none"
    return text + f"; secure matches from: {families}; two independent surveys are needed to confirm"


# --- a whole patch ------------------------------------------------------------

Fetch = Callable[[Observatory, float, float, float], list[ArchivalSource]]


def refine_objects(
    objects: Sequence[TrackLike],
    *,
    observatories: str | Iterable[str] | None = None,
    fetch: Fetch = catalogs.fetch_catalog,
    params: FusionParams = FusionParams(),
    reach_arcsec: float = 300.0,
    neowise_radius_arcsec: float = 30.0,
    neowise_max_tracks: int = 10,
    min_frame_stars: int = 5,
) -> FusionResult:
    """Refine every track of one sky patch with the selected observatories.

    `observatories` is the toggle: "spherex" uses SPHEREx only (no network);
    "multi", "all" or a list adds archival catalogs (see `parse_observatories`;
    None reads the PLANETX_OBSERVATORIES setting). `objects` are the engine's
    tracked objects, or anything with `.sources` of (ra, dec, mjd, sigma_arcsec).

    Catalogs are fetched once for the patch, out to `reach_arcsec` beyond the
    outermost track (300" covers 11"/yr back to 2MASS). A catalog that cannot
    be fetched is skipped and reported in `notes`; the rest still apply.
    NEOWISE, when selected, is queried per track and only for mover candidates.
    """
    obs = parse_observatories(observatories)
    result = FusionResult(observatories=obs, fused=[])
    if not objects:
        return result
    if obs == SPHEREX_ONLY:
        result.fused = [fuse_track(o.sources, {}, params) for o in objects]
        return result

    first = objects[0].sources[0]
    ra0, dec0 = float(first.ra), float(first.dec)
    px, py = _project(
        [s.ra for o in objects for s in o.sources],
        [s.dec for o in objects for s in o.sources],
        ra0,
        dec0,
    )
    cx, cy = float(px.mean()), float(py.mean())
    c_ra, c_dec = _deproject(cx, cy, ra0, dec0)
    radius = float(np.hypot(px - cx, py - cy).max()) + reach_arcsec
    if radius > catalogs.MAX_RADIUS_ARCSEC:
        result.notes.append(
            f"archive search clipped from {radius:.0f}\" to {catalogs.MAX_RADIUS_ARCSEC:.0f}\"; "
            "fast movers near the patch edge may lack counterparts"
        )
        radius = catalogs.MAX_RADIUS_ARCSEC

    archive: dict[Observatory, list[ArchivalSource]] = {}
    for o in sorted(obs - {Observatory.SPHEREX, Observatory.NEOWISE}):
        try:
            archive[o] = fetch(o, c_ra, c_dec, radius)
        except catalogs.ArchiveError as exc:
            log.warning("skipping %s: %s", o.value, exc)
            result.notes.append(f"{o.value} unavailable: {exc}")
            continue
        result.archive_counts[o] = len(archive[o])

    extra: dict[int, list[ArchivalSource]] = {}
    if Observatory.NEOWISE in obs:
        wanted = [
            i for i, o in enumerate(objects)
            if getattr(o, "is_mover", False) or getattr(o, "link", "") == "mover"
        ]
        if len(wanted) > neowise_max_tracks:
            result.notes.append(
                f"neowise queried for the first {neowise_max_tracks} of {len(wanted)} mover candidates"
            )
        for i in wanted[:neowise_max_tracks]:
            last = max(objects[i].sources, key=lambda s: s.mjd)
            try:
                extra[i] = fetch(Observatory.NEOWISE, float(last.ra), float(last.dec), neowise_radius_arcsec)
            except catalogs.ArchiveError as exc:
                log.warning("skipping neowise for track %d: %s", i, exc)
                result.notes.append(f"neowise unavailable for track {i}: {exc}")
                continue
            result.archive_counts[Observatory.NEOWISE] = (
                result.archive_counts.get(Observatory.NEOWISE, 0) + len(extra[i])
            )

    if not archive and not extra:
        result.notes.append("no archival catalog could be fetched; SPHEREx-only fits returned")

    def run(offset: tuple[float, float]) -> list[FusedMotion]:
        out = []
        for i, o in enumerate(objects):
            per_track = dict(archive)
            if i in extra:
                per_track[Observatory.NEOWISE] = extra[i]
            out.append(fuse_track(o.sources, per_track, params, frame_offset_arcsec=offset))
        return out

    fused = run((0.0, 0.0))
    # SPHEREx frames are aligned to each other, not to the sky, so the whole
    # patch can sit a fraction of a pixel off the archival frame; over a long
    # baseline that fakes a small common motion. Most stars barely move, so the
    # median SPHEREx-minus-archive offset of the stationary ones measures that
    # shift; remove it and refit.
    offsets = np.array([
        f.spherex_minus_archive_arcsec for f in fused
        if f.verdict is Verdict.STATIONARY and f.spherex_minus_archive_arcsec
    ])
    if len(offsets) >= min_frame_stars:
        shift = (float(np.median(offsets[:, 0])), float(np.median(offsets[:, 1])))
        result.frame_offset_arcsec = shift
        fused = run(shift)
    elif archive or extra:
        result.notes.append(
            f"only {len(offsets)} stationary matched tracks: SPHEREx not tied to the archival frame"
        )
    result.fused = fused
    return result
