"""The frames of every field of a survey patch, from ONE raw cutout per SPHEREx visit.

Loading each field separately (`pipeline.analyse_field`) costs one ~4.3 MB
cutout per visit per field, and neighbouring fields sample different visits:
an nside-64 patch near the NEP (64 fields, 54 frames each) meant ~3,500
downloads, ~15 GB. Here a patch costs about one download per visit used:

  1. Listing. SIA cones larger than a few arcsec time out at IRSA (0.1 deg did,
     Nov 2026), so 10" cones at a few probe fields (the most central and the
     outermost) list the visits around the patch. Each row's footprint
     (`s_region`) then says which field centres it covers, as a cone at every
     field would. A visit that covers some field but no probe (a sliver at the
     patch's edge) is missed.
  2. Sampling. Per (pass, detector), visits are chosen greedily until every
     field has min(FRAMES_PER_PASS_DETECTOR, visits covering it): first the
     visit covering most fields still short of that, among those the one
     farthest in time from the visits already chosen. Each field then takes
     `pipeline.pick_visits` of the chosen visits covering it, so the engine
     sees the sample size it was validated on.
  3. Downloads. One cutout per chosen visit, centred on the fields that use it
     and just large enough to contain their field cutouts (at most
     MAX_REQUEST_DEG, ~25 MB; larger sets are split), DOWNLOAD_WORKERS at a
     time, pinned in the cache until the patch is done.
  4. Cropping. Each field's frame is cut locally (`cutouts.crop_cutout`) and is
     identical to the cutout IRSA would send for that field.
"""
from __future__ import annotations

import logging
import threading
import time
import warnings
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning, Sip

from planetx import pipeline
from planetx.engine import Frame, group_into_passes
from planetx.engine import frames as engine_frames
from planetx.spherex import archive, cutouts

if TYPE_CHECKING:
    from planetx.api.schemas import Visit

log = logging.getLogger(__name__)

PROBES = 5  # listing cones per patch: the most central field and the 4 outermost
MAX_REQUEST_DEG = 2.2  # largest cutout asked for: ~1350 px a side, ~25 MB
# Room for IRSA's pixel count per degree (a 2.1 deg request gave 1233 px, 6.13"/px)
# and local pixel-scale changes across the detector, on top of the crop's 4 px.
REQUEST_SCALE_MARGIN = 0.02
DOWNLOAD_WORKERS = pipeline.DOWNLOAD_WORKERS
# A cutout serves a whole patch, so it is worth waiting out IRSA's 503 bursts.
DOWNLOAD_RETRY_WAITS_S = (5.0, 15.0, 30.0)
# More failed downloads than this share fail the patch (retried later) rather than
# storing results built on a fraction of its data.
MAX_FAILED_SHARE = 0.2

FieldRequest = tuple[float, float, float]
"""A field's raw cutout as `cutouts.cutout_request` gives it: (ra, dec, size_deg)."""


def _unit(ra: np.ndarray, dec: np.ndarray) -> np.ndarray:
    ra, dec = np.radians(ra), np.radians(dec)
    return np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=-1)


def _radec(u: np.ndarray) -> tuple[float, float]:
    u = u / np.linalg.norm(u)
    return float(np.degrees(np.arctan2(u[1], u[0])) % 360), float(np.degrees(np.arcsin(np.clip(u[2], -1, 1))))


def covers(vertices: np.ndarray, ra: np.ndarray, dec: np.ndarray) -> np.ndarray:
    """Whether each (ra, dec) lies inside (or on) the convex spherical polygon `vertices` (n, 2)."""
    v = _unit(vertices[:, 0], vertices[:, 1])
    normals = np.cross(v, np.roll(v, -1, axis=0))  # one great circle per edge
    centre = v.sum(axis=0)
    inner = np.sign(normals @ centre)  # which side of each edge is inside, whatever the winding
    p = _unit(np.asarray(ra, float), np.asarray(dec, float)).reshape(-1, 3)
    return ((p @ normals.T) * inner >= 0).all(axis=1) & (p @ centre > 0)


def probe_fields(ra: np.ndarray, dec: np.ndarray, n: int = PROBES) -> list[int]:
    """Indices of the fields to list visits at: the most central one, then each next
    the one farthest from those already chosen (the patch's outermost fields)."""
    u = _unit(np.asarray(ra, float), np.asarray(dec, float))
    centre = u.sum(axis=0)
    chosen = [int(np.argmax(u @ centre))]
    nearest = u @ u[chosen[0]]  # cosine to the nearest chosen probe
    while len(chosen) < min(n, len(u)):
        nxt = int(np.argmin(nearest))
        chosen.append(nxt)
        nearest = np.maximum(nearest, u @ u[nxt])
    return chosen


def list_footprints(
    probes: Sequence[tuple[float, float]], should_stop: Callable[[], bool]
) -> list[tuple[Visit, np.ndarray]]:
    """The visits (with footprints) at the probe positions, one entry per access_url."""
    def query(pos: tuple[float, float]) -> list[tuple[Visit, np.ndarray]]:
        ra, dec = pos
        return pipeline.retry_upstream(
            lambda: archive.query_footprints(ra, dec, pipeline.VISIT_RADIUS_ARCSEC),
            f"patch probe ra={ra} dec={dec}: visit listing", should_stop,
        )

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS, thread_name_prefix="patch-sia") as pool:
        listed = list(pool.map(query, probes))
    found: dict[str, tuple[Visit, np.ndarray]] = {}
    for rows in listed:
        for visit, footprint in rows:
            found.setdefault(visit.access_url, (visit, footprint))
    return sorted(found.values(), key=lambda vf: (vf[0].mjd, vf[0].detector or 0, vf[0].access_url))


def share_visits(cover: np.ndarray, mjd: np.ndarray, per_field: int) -> list[int]:
    """Rows of `cover` (visits x fields, bool; one pass and detector) to download so that
    every field is covered by min(per_field, its visits) of them (see the module docstring)."""
    need = np.minimum(per_field, cover.sum(axis=0))
    have = np.zeros_like(need)
    free = np.ones(len(cover), dtype=bool)
    chosen: list[int] = []
    while (have < need).any():
        gain = np.where(free, cover[:, have < need].sum(axis=1), -1)
        candidates = np.flatnonzero(gain == gain.max())  # gain.max() >= 1: a short field has a free visit
        if chosen:
            gap = np.abs(mjd[candidates, None] - mjd[chosen][None, :]).min(axis=1)
            pick = int(candidates[np.argmax(gap)])  # ties: the earliest
        else:
            pick = int(candidates[0])
        chosen.append(pick)
        free[pick] = False
        have += cover[pick]
    return chosen


@dataclass(frozen=True)
class Request:
    """One raw cutout to download, and the fields cropped from it."""

    visit: Visit
    ra: float
    dec: float
    size_deg: float
    fields: tuple[int, ...]


def _sep_deg(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    return np.degrees(np.arccos(np.clip(u @ v, -1, 1)))


def _requests_for(visit: Visit, users: list[int], fields: Sequence[FieldRequest]) -> list[Request]:
    """Cutouts of `visit` holding the field cutouts of `users`: one, or several if too large."""
    ra = np.array([fields[i][0] for i in users])
    dec = np.array([fields[i][1] for i in users])
    u = _unit(ra, dec)
    cra, cdec = cutouts.snap_center(*_radec(u.sum(axis=0)))
    # A field cutout is a square of its size along the detector's pixel axes around its
    # centre; along either axis that centre is at most its distance from our centre.
    half = float(_sep_deg(u, _unit(np.array(cra), np.array(cdec))).max()) + max(fields[i][2] for i in users) / 2
    size = round(2 * half * (1 + REQUEST_SCALE_MARGIN) + cutouts.CUTOUT_MARGIN_DEG, 5)
    if size <= MAX_REQUEST_DEG or len(users) == 1:
        return [Request(visit, cra, cdec, size, tuple(users))]
    # Field lists are in NESTED order, so contiguous quarters are compact sub-patches.
    return [r for part in np.array_split(np.array(users), 4) if len(part)
            for r in _requests_for(visit, [int(i) for i in part], fields)]


@dataclass(frozen=True)
class PatchPlan:
    fields: tuple[FieldRequest, ...]
    requests: tuple[Request, ...]
    by_field: tuple[tuple[int, ...], ...]  # per field: indices into `requests`, one per picked visit


def plan_patch(fields: Sequence[FieldRequest], footprints: Sequence[tuple[Visit, np.ndarray]]) -> PatchPlan:
    """Which visits to download for the patch, how, and which of them each field uses."""
    rows = sorted(
        ((v, fp) for v, fp in footprints if v.detector in pipeline.DETECTORS),
        key=lambda vf: (vf[0].mjd, vf[0].detector, vf[0].access_url),
    )
    visits = [v for v, _ in rows]
    ra = np.array([f[0] for f in fields])
    dec = np.array([f[1] for f in fields])
    cover = np.array([covers(fp, ra, dec) for _, fp in rows], dtype=bool).reshape(len(rows), len(fields))
    mjd = np.array([v.mjd for v in visits])
    chosen: set[int] = set()
    for ids in group_into_passes(list(mjd)):
        for detector in pipeline.DETECTORS:
            mine = np.array([i for i in ids if visits[i].detector == detector], dtype=int)
            if len(mine):
                chosen.update(int(mine[j]) for j in share_visits(cover[mine], mjd[mine], pipeline.FRAMES_PER_PASS_DETECTOR))

    index = {v.access_url: i for i, v in enumerate(visits)}
    picked = [
        [index[v.access_url] for v in pipeline.pick_visits([visits[i] for i in sorted(chosen) if cover[i, f]])]
        for f in range(len(fields))
    ]
    users: dict[int, list[int]] = {}
    for f, ids in enumerate(picked):
        for i in ids:
            users.setdefault(i, []).append(f)
    requests: list[Request] = []
    for i in sorted(users):
        requests += _requests_for(visits[i], users[i], fields)
    serving = {(r.visit.access_url, f): k for k, r in enumerate(requests) for f in r.fields}
    by_field = tuple(tuple(serving[(visits[i].access_url, f)] for i in ids) for f, ids in enumerate(picked))
    return PatchPlan(fields=tuple(fields), requests=tuple(requests), by_field=by_field)


class OpenCutout:
    """A downloaded patch cutout, opened once (memory-mapped), that crops field frames.

    `frame(ra, dec, size)` equals `frame_from_hdul(cutouts.crop_cutout(hdul, ra, dec,
    size))` exactly (test_patch_frames checks it): the same pixel operations on a
    slice of the planes, and the WCS a cropped header would give, i.e. the cutout's
    own with CRPIX moved by the crop offset (integers, so exact). Going through
    FITS headers instead cost ~250 ms per frame, a third of a field's analysis;
    this is about 2 ms.
    """

    def __init__(self, path: Path):
        self._hdul = fits.open(path, memmap=True)
        try:
            self._load()
        except BaseException:
            self._hdul.close()
            raise

    def _load(self) -> None:
        hdul = self._hdul
        image = hdul["IMAGE"]
        self.header = image.header
        self.image = image.data
        self.wcs = cutouts.cutout_wcs(self.header)
        # Raised by frame() only when a crop falls on the cutout, as crop_cutout and
        # frame_from_hdul would raise it (the frame fails; without a crop there is no data).
        self._error: Exception | None = None
        try:
            for name in cutouts.CROP_PLANES:
                if name in hdul and (hdul[name].data is None or hdul[name].data.shape != self.image.shape):
                    raise ValueError(f"cutout {name} plane does not match IMAGE")
            self.variance = hdul["VARIANCE"].data
            self.flags = hdul["FLAGS"].data
        except (KeyError, ValueError) as exc:
            self._error = exc
            return
        bits = engine_frames.flag_bits(hdul["FLAGS"].header)
        self._bad, self._saturated, self._contaminated = (
            _bits(bits, names) for names in
            (engine_frames.BAD_FLAG_NAMES, engine_frames.SATURATION_FLAG_NAMES, engine_frames.CONTAMINATION_FLAG_NAMES)
        )
        self._wave: WCS | Exception | None = None
        if "WCS-WAVE" in hdul:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", FITSFixedWarning)
                    self._wave = cutouts._wavelength_wcs(self.header, hdul)
            except Exception as exc:  # noqa: BLE001 - as frame_from_hdul: reported per frame, the frame is kept
                self._wave = exc

    def close(self) -> None:
        self._hdul.close()

    def frame(self, ra: float, dec: float, size_deg: float, obs_id: str = "") -> Frame | None:
        """The frame of request (ra, dec, size_deg), or None when (ra, dec) is off this cutout."""
        box = cutouts.crop_box(self.wcs, self.image.shape, ra, dec, size_deg)
        if box is None:
            return None
        if self._error is not None:
            raise self._error
        x0, x1, y0, y1 = box
        data = np.array(self.image[y0:y1, x0:x1], dtype=np.float64)
        variance = np.array(self.variance[y0:y1, x0:x1], dtype=np.float64)
        flags = self.flags[y0:y1, x0:x1].astype(np.int64)
        mask = (flags & self._bad) != 0
        data[mask] = np.nan
        variance[mask] = np.nan
        variance[~(variance > 0)] = np.nan

        wcs = self.wcs.deepcopy()
        wcs.wcs.crpix = self.wcs.wcs.crpix - np.array([x0, y0], dtype=float)
        if wcs.sip is not None:  # SIP is relative to CRPIX, which astropy hands it when reading a header
            sip = self.wcs.sip
            wcs.sip = Sip(sip.a, sip.b, sip.ap, sip.bp, wcs.wcs.crpix)
        wcs.pixel_shape = (x1 - x0, y1 - y0)

        wavelength = float("nan")
        if self._wave is not None:
            try:
                if isinstance(self._wave, Exception):
                    raise self._wave
                ny, nx = data.shape
                # The crop's centre pixel, in this cutout's pixels (the W table is indexed by them).
                wavelength = float(self._wave.all_pix2world(x0 + (nx - 1) / 2, y0 + (ny - 1) / 2, 0)[0])
            except Exception as exc:  # noqa: BLE001 - wavelength is metadata, as in frame_from_hdul
                warnings.warn(f"could not evaluate WCS-WAVE: {exc}")

        header = self.header
        if "MJD-AVG" not in header:
            raise ValueError("IMAGE header has no MJD-AVG; cannot time-stamp frame")
        return Frame(
            data=data,
            variance=variance,
            wcs=wcs,
            mjd=float(header["MJD-AVG"]),
            detector=int(header.get("DETECTOR", 0)),
            obs_id=obs_id,
            wavelength_um=wavelength,
            psf_fwhm_arcsec=float(header.get("PSF_FWHM", float("nan"))),
            saturated=(flags & self._saturated) != 0,
            contaminated=(flags & self._contaminated) != 0,
        )


def _bits(bits: dict[str, int], names: frozenset[str]) -> int:
    want = 0
    for name in names:
        want |= bits.get(name, 0)
    return want


class PatchFrames:
    """A plan's cutouts, downloaded; `field_frames(i)` crops field i's frames from them."""

    def __init__(self, plan: PatchPlan):
        self.plan = plan
        self.paths: list[Path | None] = [None] * len(plan.requests)
        self.errors: list[str | None] = [None] * len(plan.requests)  # None: downloaded or no data here
        self.no_data = [False] * len(plan.requests)
        self.gaia_down: str | None = None  # set when Gaia is known to be down: fields skip the catalogue check
        self._open: dict[int, OpenCutout] = {}  # per request, opened on first use: every field crops it

    def download(self, should_stop: Callable[[], bool]) -> None:
        """Fetch every request, DOWNLOAD_WORKERS at a time.

        Raises `pipeline.Stopped` when `should_stop()` turns true, and a transient
        `archive.UpstreamError` when more than MAX_FAILED_SHARE of them failed.
        """
        lock = threading.Lock()
        done = [0]

        def cached(r: Request) -> Path:
            try:
                return cutouts.cached_cutout(r.visit.access_url, r.ra, r.dec, r.size_deg)
            except archive.IrsaBusy as exc:  # our own request cap: as worth waiting for as IRSA's 503
                raise archive.UpstreamError(str(exc), transient=True) from exc

        def fetch(k: int) -> None:
            r = self.plan.requests[k]
            if should_stop():
                return
            try:
                self.paths[k] = pipeline.retry_upstream(
                    lambda: cached(r), f"patch cutout {r.visit.access_url}", should_stop, DOWNLOAD_RETRY_WAITS_S
                )
            except pipeline.Stopped:
                return
            except archive.UpstreamError as exc:
                if exc.status in pipeline.NO_CUTOUT_STATUS:
                    self.no_data[k] = True
                else:
                    self.errors[k] = str(exc)
            except ValueError as exc:  # an access_url outside the IRSA allow-list
                self.errors[k] = f"ValueError: {exc}"
            with lock:
                done[0] += 1
                if done[0] % 10 == 0 or done[0] == len(self.plan.requests):
                    log.info("patch cutouts: %d/%d downloaded", done[0], len(self.plan.requests))

        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS, thread_name_prefix="patch-dl") as pool:
            list(pool.map(fetch, range(len(self.plan.requests))))
        if should_stop():
            raise pipeline.Stopped()
        failed = [e for e in self.errors if e is not None]
        if len(failed) > MAX_FAILED_SHARE * len(self.plan.requests):
            raise archive.UpstreamError(
                f"{len(failed)} of {len(self.plan.requests)} patch cutouts failed, e.g. {failed[0]}", transient=True
            )

    def field_frames(self, i: int) -> tuple[list[Frame], int, list[str]]:
        """Field i's frames: (frames, exposures without data there, failure messages)."""
        frames: list[Frame] = []
        no_data, failures = 0, []
        ra, dec, size = self.plan.fields[i]
        for k in self.plan.by_field[i]:
            if self.no_data[k]:
                no_data += 1
                continue
            if self.paths[k] is None:
                failures.append(self.errors[k] or "cutout not downloaded")
                continue
            try:
                frame = self._crop(k, ra, dec, size)
            except archive.UpstreamError as exc:
                failures.append(str(exc))
            except (KeyError, ValueError, OSError) as exc:  # a cutout the engine cannot read
                failures.append(f"{type(exc).__name__}: {exc}")
            else:
                if frame is None:
                    no_data += 1
                else:
                    frames.append(frame)
        return frames, no_data, failures

    def _crop(self, k: int, ra: float, dec: float, size: float) -> Frame | None:
        r = self.plan.requests[k]
        if k not in self._open:
            try:
                self._open[k] = OpenCutout(self.paths[k])
            except FileNotFoundError:
                # Pins hold only within this process: another one sharing the cache evicted it.
                self.paths[k] = cutouts.cached_cutout(r.visit.access_url, r.ra, r.dec, r.size_deg)
                self._open[k] = OpenCutout(self.paths[k])
        return self._open[k].frame(ra, dec, size, obs_id=r.visit.obs_id)

    def paths_to_pin(self) -> list[Path]:
        """Where the plan's cutouts live in the cache (as `load` pins them)."""
        return [cutouts.cutout_path(r.visit.access_url, r.ra, r.dec, r.size_deg) for r in self.plan.requests]

    def __getstate__(self) -> dict:
        # Sent to the field processes (survey.motion), which open the cutouts themselves.
        return {**self.__dict__, "_open": {}}

    def close(self) -> None:
        """Close the cutouts opened by `field_frames`."""
        for cutout in self._open.values():
            cutout.close()
        self._open.clear()

    def field_run(self, i: int, should_stop: Callable[[], bool]) -> pipeline.FieldRun:
        """The pipeline on field i (`pipeline.analyse_frames`; raises as it does)."""
        frames, no_data, failures = self.field_frames(i)
        ra, dec, _ = self.plan.fields[i]
        return pipeline.analyse_frames(
            frames, n_visits=len(self.plan.by_field[i]), no_data=no_data, failures=failures,
            where=f"field ra={ra} dec={dec}", should_stop=should_stop, gaia_down=self.gaia_down,
        )


@contextmanager
def load(fields: Sequence[FieldRequest], should_stop: Callable[[], bool]) -> Iterator[PatchFrames]:
    """List, plan and download a patch's cutouts; they stay pinned in the cache inside the block.

    `fields` are the fields' raw cutout requests (`cutouts.cutout_request`). Raises
    `pipeline.Stopped` and `archive.UpstreamError` (see `PatchFrames.download`).
    """
    start = time.monotonic()
    ra = np.array([f[0] for f in fields])
    dec = np.array([f[1] for f in fields])
    footprints = list_footprints([(fields[i][0], fields[i][1]) for i in probe_fields(ra, dec)], should_stop)
    plan = plan_patch(fields, footprints)
    listed = time.monotonic()
    log.info("patch: %d visits listed, %d cutouts to download for %d fields (%.0fs)",
             len(footprints), len(plan.requests), len(fields), listed - start)
    patch = PatchFrames(plan)
    with cutouts.pinned(patch.paths_to_pin()), closing(patch):
        patch.download(should_stop)
        size = sum(p.stat().st_size for p in patch.paths if p is not None and p.exists())
        log.info("patch: %d cutouts (%.0f MB) ready, %d without data, %d failed (%.0fs)",
                 sum(p is not None for p in patch.paths), size / 2**20, sum(patch.no_data),
                 sum(e is not None for e in patch.errors), time.monotonic() - listed)
        if size > cutouts.settings.cache_max_mb * 2**20:
            log.warning("patch cutouts (%.0f MB) exceed the cache cap (PLANETX_CACHE_MAX_MB=%d): "
                        "the cache stays above it until the patch is done", size / 2**20,
                        cutouts.settings.cache_max_mb)
        yield patch
