"""Plant fake moving point sources into real SPHEREx frames.

Pure functions, no network. They take the engine's `Frame`s and return copies with
point sources added at positions that follow a proper motion (and optionally a
parallax) across the frames' dates. Running the motion engine on those copies and
counting what comes back measures what the engine can and cannot find.

Choices that matter for reading the results:

* **PSF**: each frame's own effective PSF (EPSF), the calibration table SPHEREx ships
  in every file (identical for every exposure of a detector: 11 x 41 zones, 33 x 33
  samples at 5x oversampling). IRSA cutouts carry it, but the dev cutout cache
  strips it, so `EpsfLibrary` collects one table per detector from any full cutout and
  picks the zone nearest the source. A Gaussian of the header's PSF_FWHM is the
  fallback (`Psf.gaussian`) for a detector with no table.
* **Flux**: AB magnitude, the same in every frame (flat in f_nu), so "mag 16" means
  16 at whichever wavelength each frame happens to observe. Image pixels are surface
  brightness (MJy/sr); a source of flux F puts F * share / pixel_solid_angle in a
  pixel taking `share` of its light.
* **Noise**: none is added. The frames are real, so their pixels already carry the
  real noise (and the VARIANCE plane already describes it). Adding a draw from the
  VARIANCE plane on top would double it. The only noise missing is the injected
  source's own photon noise, which cannot be set without the detector gain (small
  next to the background except for very bright sources).
* **Saturation** is not modelled: a bright injection stays linear and unflagged.
* **Where**: sites are chosen where the whole track stays clear of real detections
  (any frame, `exclusion_arcsec`), away from frame edges, and with a clean 3 x 3
  pixel stamp (finite, not saturated) in at least 90% of the frames.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from pathlib import Path

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning
from scipy.ndimage import map_coordinates
from scipy.spatial import cKDTree

from planetx.engine import Frame, TrackedObject
from planetx.engine.detect import detect_sources
from planetx.engine.motion import DAYS_PER_YEAR, parallax_factors
from planetx.spherex.cutouts import without_sip

AB_ZERO_POINT_JY = 3631.0
ARCSEC_PER_RAD = 180.0 * 3600.0 / math.pi
# Pixels around the source (3 x 3) that must be usable in most frames: nearly all the light
# is there. A masked pixel then leaves the injection partly masked, as a real star would be.
STAMP_HALF_PX = 1
MAX_BAD_STAMP_FRACTION = 0.1


# --- photometry ------------------------------------------------------------------------


def ab_mag_to_flux_mjy(mag: float) -> float:
    """Flux density (mJy) of an AB magnitude."""
    return AB_ZERO_POINT_JY * 10 ** (-0.4 * mag) * 1e3


def flux_mjy_to_ab_mag(flux_mjy: float) -> float:
    return -2.5 * math.log10(flux_mjy * 1e-3 / AB_ZERO_POINT_JY)


def pixel_solid_angle_sr(frame: Frame) -> float:
    return (frame.pixel_scale_arcsec / ARCSEC_PER_RAD) ** 2


# --- PSF -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Psf:
    """An effective PSF sampled `oversample` times finer than the pixels.

    The value at a pixel's offset from the source is proportional to the share of the
    source's light that pixel collects (the pixel response is part of the EPSF).
    """

    kernel: np.ndarray  # (n, n), odd n, peak at the centre sample
    oversample: int

    @classmethod
    def gaussian(cls, fwhm_arcsec: float, pixel_arcsec: float, oversample: int = 5, half_px: float = 3.2) -> Psf:
        sigma = fwhm_arcsec / pixel_arcsec / (2 * math.sqrt(2 * math.log(2)))
        n = 2 * round(half_px * oversample) + 1
        r = (np.arange(n) - (n - 1) / 2) / oversample
        g = np.exp(-(r[:, None] ** 2 + r[None, :] ** 2) / (2 * sigma**2))
        return cls(g / g.sum(), oversample)

    def shares(self, x: float, y: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Pixel rows, columns and light shares of a source at pixel (x, y) (0-based, centres at integers).

        Shares are normalised over the EPSF box (~6.6 px), the way `planetx.spherex.spectrum`
        reads the same table, so the light beyond the box (1-2%) is dropped.
        """
        half = (self.kernel.shape[0] - 1) / 2 / self.oversample
        reach = math.ceil(half) + 1
        cy, cx = round(y), round(x)
        rows, cols = np.meshgrid(np.arange(cy - reach, cy + reach + 1), np.arange(cx - reach, cx + reach + 1), indexing="ij")
        c = (self.kernel.shape[0] - 1) / 2
        vals = map_coordinates(
            self.kernel, [c + (rows - y) * self.oversample, c + (cols - x) * self.oversample], order=1, cval=0.0
        )
        vals = np.clip(vals, 0.0, None)
        return rows, cols, vals / vals.sum()


class EpsfLibrary:
    """The per-detector EPSF tables: kernels by zone centre in detector pixels."""

    def __init__(self, tables: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray, int]]):
        # detector -> (x_centres, y_centres, kernels[n_zone, h, w], oversample)
        self.tables = tables

    @property
    def detectors(self) -> list[int]:
        return sorted(self.tables)

    def add_hdul(self, hdul: fits.HDUList) -> bool:
        """Take the table of a full cutout (False if it has none, or the detector is already known)."""
        if "EPSF" not in hdul:
            return False
        det = int(hdul["IMAGE"].header["DETECTOR"])
        table = hdul["EPSF"]
        if det in self.tables or len(table.data) == 0:
            return False
        ov = int(table.header["OVSMPX"])
        kernels = np.asarray(table.data["EPSF"], dtype=np.float32)
        self.tables[det] = (
            np.asarray(table.data["XCENTER"], np.float32),
            np.asarray(table.data["YCENTER"], np.float32),
            kernels,
            ov,
        )
        return True

    def psf_at(self, detector: int, x_det: float, y_det: float) -> Psf:
        """The EPSF of the zone nearest to detector pixel (x_det, y_det)."""
        xc, yc, kernels, ov = self.tables[detector]
        k = int(np.argmin(np.hypot(xc - x_det, yc - y_det)))
        kern = np.asarray(kernels[k], dtype=np.float64)
        return Psf(kern / kern.sum(), ov)

    def save(self, path: Path) -> None:
        arrays = {}
        for det, (xc, yc, kernels, ov) in self.tables.items():
            arrays[f"x{det}"], arrays[f"y{det}"], arrays[f"k{det}"], arrays[f"o{det}"] = xc, yc, kernels, np.array(ov)
        np.savez_compressed(path, **arrays)

    @classmethod
    def load(cls, path: Path) -> EpsfLibrary:
        with np.load(path) as z:
            dets = sorted(int(k[1:]) for k in z.files if k.startswith("o"))
            return cls({d: (z[f"x{d}"], z[f"y{d}"], z[f"k{d}"], int(z[f"o{d}"])) for d in dets})


def detector_xy(header: fits.Header, x: float, y: float) -> tuple[float, float]:
    """Detector pixel of image pixel (x, y), from the header's alternate linear WCS (key A)."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        ax, ay = WCS(without_sip(header), key="A").all_pix2world(x, y, 0)
    return float(ax), float(ay)


# --- injections ------------------------------------------------------------------------


@dataclass(frozen=True)
class Injection:
    """A fake point source: position and motion at `ref_mjd`, and brightness."""

    ra: float  # degrees at ref_mjd
    dec: float
    ref_mjd: float
    pm_ra_cosdec: float  # arcsec/yr, east positive
    pm_dec: float  # arcsec/yr, north positive
    mag_ab: float
    parallax_arcsec: float = 0.0

    @property
    def pm_total(self) -> float:
        return math.hypot(self.pm_ra_cosdec, self.pm_dec)

    def position(self, mjds) -> tuple[np.ndarray, np.ndarray]:
        """(ra, dec) in degrees at each MJD: linear motion plus, if set, parallax."""
        mjds = np.atleast_1d(np.asarray(mjds, float))
        t = (mjds - self.ref_mjd) / DAYS_PER_YEAR
        east, north = self.pm_ra_cosdec * t, self.pm_dec * t
        if self.parallax_arcsec:
            # Parallax is measured relative to the ref epoch's position, like the engine's fit.
            fe, fn = parallax_factors(self.ra, self.dec, np.append(mjds, self.ref_mjd))
            east = east + self.parallax_arcsec * (fe[:-1] - fe[-1])
            north = north + self.parallax_arcsec * (fn[:-1] - fn[-1])
        c = SkyCoord(self.ra * u.deg, self.dec * u.deg).spherical_offsets_by(east * u.arcsec, north * u.arcsec)
        return np.atleast_1d(c.ra.deg), np.atleast_1d(c.dec.deg)


def inject(frames: list[Frame], injections: list[Injection], psf_for=None) -> list[Frame]:
    """Copies of `frames` with the injections added (the inputs are not modified).

    `psf_for(frame, x, y) -> Psf` picks the PSF (default: a Gaussian of the frame's
    PSF_FWHM). Pixels that are NaN (masked) stay NaN. A source outside a frame is
    simply absent from it. Each copy's `meta["injected"]` lists, per injection, its
    pixel position there and the flux added.
    """
    out = []
    for f in frames:
        data = f.data.copy()
        omega = pixel_solid_angle_sr(f)
        log = []
        for inj in injections:
            ra, dec = inj.position([f.mjd])
            x, y = (float(v) for v in f.wcs.all_world2pix(ra[0], dec[0], 0))
            psf = psf_for(f, x, y) if psf_for else Psf.gaussian(f.psf_fwhm_arcsec, f.pixel_scale_arcsec)
            rows, cols, share = psf.shares(x, y)
            ny, nx = data.shape
            ok = (rows >= 0) & (rows < ny) & (cols >= 0) & (cols < nx)
            flux_mjy_sr = ab_mag_to_flux_mjy(inj.mag_ab) * 1e-9 / omega  # mJy -> MJy, per sr
            data[rows[ok], cols[ok]] += flux_mjy_sr * share[ok]  # NaN + x stays NaN
            log.append({"x": x, "y": y, "flux_mjy": ab_mag_to_flux_mjy(inj.mag_ab)})
        out.append(dataclasses.replace(f, data=data, meta={**f.meta, "injected": log}))
    return out


# --- choosing where to plant them ------------------------------------------------------


def _unit(ra, dec) -> np.ndarray:
    ra, dec = np.radians(ra), np.radians(dec)
    return np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=-1)


class SiteFinder:
    """Finds places where a fake source's whole track avoids real sources and bad pixels."""

    def __init__(self, frames: list[Frame], exclusion_arcsec: float = 20.0, margin_px: float = 5.0, nsigma: float = 3.0, min_hits: int | None = None):
        self.frames = frames
        min_hits = min_hits if min_hits is not None else max(2, len(frames) // 10)
        self.margin_px = margin_px
        self.exclusion_arcsec = exclusion_arcsec
        sky, snr = [], []
        for i, f in enumerate(frames):
            try:
                dets = detect_sources(f, i, nsigma)
            except ValueError:  # too few usable pixels: the engine skips such frames too
                continue
            sky += [(d.ra, d.dec) for d in dets]
            snr += [d.snr for d in dets]
            if f.saturated is not None and f.saturated.any():  # saturated stars: their patches are not detections
                ys, xs = np.nonzero(f.saturated)
                r, d = f.wcs.all_pix2world(xs, ys, 0)
                sky += list(zip(r, d))
                snr += [np.inf] * len(xs)
        # A 3-sigma peak that shows up once is noise; real stars recur from frame to frame (or are bright).
        if sky:
            pts = _unit(*np.array(sky).T)
            tree = cKDTree(pts)
            hits = tree.query_ball_point(pts, 8.0 / ARCSEC_PER_RAD, return_length=True)
            keep = (hits >= min_hits) | (np.array(snr) >= 8.0)
            self.tree = cKDTree(pts[keep]) if keep.any() else None
        else:
            self.tree = None
        self.mjds = np.array([f.mjd for f in frames])
        self.ref_mjd = float(np.mean(self.mjds))
        # Clean stamps: finite data, not saturated. Ghost/halo/crosstalk pixels (`contaminated`) are
        # allowed: they are flagged as biasing brightness, not unusable, and they cover enough of a
        # field, over many frames, to leave no room (the engine treats them the same way).
        self.bad = []
        for f in frames:
            bad = ~np.isfinite(f.data)
            if f.saturated is not None:
                bad |= f.saturated
            self.bad.append(bad)

    def clear(self, inj: Injection, taken: list[Injection] = (), separation_arcsec: float = 50.0) -> bool:
        ra, dec = inj.position(self.mjds)
        if self.tree is not None:
            chord = 2 * math.sin(self.exclusion_arcsec / ARCSEC_PER_RAD / 2)
            if self.tree.query_ball_point(_unit(ra, dec), chord, return_length=True).any():
                return False
        for other in taken:
            ora, odec = other.position(self.mjds)
            sep = np.hypot((ra - ora) * math.cos(math.radians(dec[0])), dec - odec) * 3600
            if sep.min() < separation_arcsec:
                return False
        n_bad = 0
        for f, bad, r, d in zip(self.frames, self.bad, ra, dec):
            x, y = (float(v) for v in f.wcs.all_world2pix(r, d, 0))
            ny, nx = f.data.shape
            m = self.margin_px
            if not (m <= x <= nx - 1 - m and m <= y <= ny - 1 - m):
                return False
            cx, cy = round(x), round(y)
            n_bad += bool(bad[cy - STAMP_HALF_PX : cy + STAMP_HALF_PX + 1, cx - STAMP_HALF_PX : cx + STAMP_HALF_PX + 1].any())
        return n_bad <= MAX_BAD_STAMP_FRACTION * len(self.frames)

    def sample(
        self, rng: np.random.Generator, pm_east: float, pm_north: float, mag_ab: float,
        taken: list[Injection] = (), parallax_arcsec: float = 0.0, tries: int = 1000,
    ) -> Injection | None:
        """A clear injection with this motion and brightness, or None if no site was found."""
        f0 = self.frames[len(self.frames) // 2]
        ny, nx = f0.data.shape
        for _ in range(tries):
            x = rng.uniform(self.margin_px, nx - 1 - self.margin_px)
            y = rng.uniform(self.margin_px, ny - 1 - self.margin_px)
            ra, dec = (float(v) for v in f0.wcs.all_pix2world(x, y, 0))
            # `ra, dec` is where the source is at f0's date; shift to the common ref epoch.
            t = (f0.mjd - self.ref_mjd) / DAYS_PER_YEAR
            c = SkyCoord(ra * u.deg, dec * u.deg).spherical_offsets_by(-pm_east * t * u.arcsec, -pm_north * t * u.arcsec)
            inj = Injection(float(c.ra.deg), float(c.dec.deg), self.ref_mjd, pm_east, pm_north, mag_ab, parallax_arcsec)
            if self.clear(inj, taken):
                return inj
        return None


# --- scoring ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Match:
    obj: TrackedObject | None
    separation_arcsec: float = float("nan")

    @property
    def recovered(self) -> bool:
        return self.obj is not None

    @property
    def flagged_mover(self) -> bool:
        return self.obj is not None and self.obj.is_mover

    def pull(self, inj: Injection) -> tuple[float, float] | None:
        """(east, north) proper-motion errors in units of the reported error, if recovered."""
        if self.obj is None or not self.obj.pm_err > 0:
            return None
        return (
            (self.obj.pm_ra_cosdec - inj.pm_ra_cosdec) / self.obj.pm_err,
            (self.obj.pm_dec - inj.pm_dec) / self.obj.pm_err,
        )


def match_injections(objects: list[TrackedObject], injections: list[Injection], radius_arcsec: float = 12.0) -> list[Match]:
    """For each injection, the engine object nearest to where it was at that object's reference date.

    Each object is used by at most one injection (the closest pair first). An object
    within `radius_arcsec` (about two pixels) counts.
    """
    pairs = []
    for i, inj in enumerate(injections):
        for j, obj in enumerate(objects):
            ra, dec = inj.position([obj.ref_mjd])
            sep = float(np.hypot((obj.ra - ra[0]) * math.cos(math.radians(obj.dec)), obj.dec - dec[0]) * 3600)
            if sep <= radius_arcsec:
                pairs.append((sep, i, j))
    result = [Match(None) for _ in injections]
    used_i, used_j = set(), set()
    for sep, i, j in sorted(pairs):
        if i in used_i or j in used_j:
            continue
        used_i.add(i)
        used_j.add(j)
        result[i] = Match(objects[j], sep)
    return result
