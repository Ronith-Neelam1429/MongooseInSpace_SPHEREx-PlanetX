"""Brown-dwarf candidates from SPHEREx spectra, and R Draconis's light curve.

    uv run python tools/brown_dwarf_search.py [--budget-mb 1300] [--fields eps-indi-b,wise0855,...]
    uv run python tools/brown_dwarf_search.py --light-curve [--budget-mb 250]

Writes results/brown_dwarfs.json or results/light_curve_r_dra.json (docs/RESULTS.md).

Brown dwarfs (`planetx.analysis.spectral_index`): methane depth vs water depth for
  (a) known T/Y dwarfs (positions and motions from SIMBAD, verified 2026-11-14),
  (b) ordinary reference stars (2MASS, Ks 9.5-13, isolated) from the same cutouts,
  (c) every mover the motion engine finds in those fields and the test-target fields,
      and every mover in the survey database (data/survey.sqlite, opened read-only).

Frames are chosen by where the object falls on each detector: SPHEREx's wavelength changes
across a detector, so the SIA footprint (s_region corners = detector corners, checked on 6
cached cutouts: within ~30 px) plus one detector's wavelength map predicts the wavelength of
every exposure before it is downloaded. Each field gets PER_WINDOW exposures in each
wavelength window the indices read, instead of random ones. Downloads are trimmed (EPSF
rows near the cutout only, ~0.3 MB instead of ~4 MB) into .cache/spectral; exposures already
in the shared cutout cache, the survey cache or the dev cache are used as they are.

R Draconis saturates SPHEREx: its brightness comes from the PSF wings (an annulus outside the
saturated core), compared only within one band (102-band grid), and a period is fitted with
one level per band and one amplitude per detector (`spectral_index.fit_period`).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import sqlite3
import sys
import threading
import time
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import numpy as np  # noqa: E402
from astropy.io import fits  # noqa: E402
from astropy.wcs import WCS, FITSFixedWarning  # noqa: E402
from scipy.optimize import least_squares  # noqa: E402

from planetx.analysis import spectral_index as si  # noqa: E402
from planetx.config import settings  # noqa: E402
from planetx.spherex import archive  # noqa: E402
from planetx.spherex import spectrum as sp  # noqa: E402
from planetx.spherex.cutouts import PIXEL_SCALE_ARCSEC, without_sip  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
SHARED_CACHE = Path(settings.cache_dir)
TRIM_CACHE = Path(os.environ.get("PLANETX_SPECTRAL_CACHE", SHARED_CACHE.parent / "spectral"))
SURVEY_CACHE = SHARED_CACHE.parent / "survey" / "cutouts"
DEV_CACHE = SHARED_CACHE.parent / "dev"
SURVEY_DB = Path(settings.db_path)  # PLANETX_DB
DETECTOR_PX = 2040
DOWNLOAD_WORKERS = 3  # the team shares IRSA: at most 4 requests at once
PER_WINDOW = 4
MID_MJD = 61050.0  # middle of DR1 + QR3 (2025-05 .. 2026-10)
warnings.simplefilter("ignore", FITSFixedWarning)

# Known brown dwarfs: SIMBAD (simbad.harvard.edu) 2026-11-14: J2000 position, pm mas/yr, type, J.
KNOWN = {
    "eps-indi-b": ("eps Ind B", 331.04415, -56.78284, 3981.977, -2466.832, "T1V+T6V", 11.91),
    "wise0855": ("WISE J085510.83-071442.5", 133.81891, -7.24705, -8123.7, 673.2, "Y2", 25.0),
    "2mass0559": ("2MASS J05591914-1404488", 89.82995, -14.08034, 571.34, -338.167, "T4.5", 13.80),
    "2mass0415": ("2MASS J04151954-0935066", 63.83143, -9.58519, 2193.0, 527.0, "T8.0", 15.70),
    "ugps0722": ("UGPS J072227.51-054031.2", 110.61628, -5.67599, -903.738, 351.808, "T9", 16.52),
    "luhman16": ("Luhman 16AB (WISE J104915.57-531906.1)", 162.32881, -53.31947, -2767.502, 356.856,
                 "L7.5+T0.5", 10.73),
}
# Test-target fields measured from the dev cache (tools/fetch.py; trimmed, so no EPSF:
# aperture corrections there come from the header's PSF_FWHM).
TEST_FIELDS = {
    "barnard": ("Barnard's Star", 269.4520769586187, 4.693364966576667, -801.551, 10362.394),
    "r-dra": ("R Dra", 248.16761083295, 66.75497941722, -20.643, 6.149),
}
FIELD_DEG = 0.3  # cutout side; costs ~0.5 MB more than 0.1 deg (the EPSF table dominates), holds more reference stars
FIELD_DEG_OVERRIDE = {"wise0855": 0.2}  # downloaded first, at 0.2 deg
NEP_FIELD_DEG = 0.3
REF_KS = (9.5, 13.0)
REF_PER_FIELD = 25
REF_EDGE_PX = 10
LIGHT_CURVE_DETECTORS = (1, 2, 3)


def log(*args) -> None:
    print(time.strftime("%H:%M:%S"), *args, flush=True)


# --- frames on disk -------------------------------------------------------------------------


@dataclass
class FrameFile:
    path: Path
    obs_id: str
    detector: int
    mjd: float
    source: str  # trim | shared | survey | dev
    epsf: bool
    shape: tuple[int, int]
    ra_c: float  # centre, for a quick first test of coverage
    dec_c: float
    _wcs: WCS | None = field(default=None, repr=False)

    @property
    def wcs(self) -> WCS:
        if self._wcs is None:
            with fits.open(self.path, memmap=True) as h:
                self._wcs = WCS(h["IMAGE"].header).celestial
        return self._wcs

    def covers(self, ra: float, dec: float, margin_px: float = 9.0) -> bool:
        half_diag = math.hypot(*self.shape) / 2 * PIXEL_SCALE_ARCSEC * 1.05
        if si.separation_arcsec(ra, dec, self.ra_c, self.dec_c) > half_diag:
            return False
        x, y = self.wcs.world_to_pixel_values(ra, dec)
        ny, nx = self.shape
        return bool(np.isfinite(x) and margin_px <= x <= nx - 1 - margin_px and margin_px <= y <= ny - 1 - margin_px)


def _frame_file(path: Path, source: str) -> FrameFile | None:
    try:
        with fits.open(path, memmap=True) as h:
            hd = h["IMAGE"].header
            shape = (int(hd["NAXIS2"]), int(hd["NAXIS1"]))
            wcs = WCS(hd).celestial
            ra_c, dec_c = (float(v) for v in wcs.all_pix2world((shape[1] - 1) / 2, (shape[0] - 1) / 2, 0))
            return FrameFile(path, str(hd.get("OBSID", "")), int(hd["DETECTOR"]), float(hd["MJD-AVG"]), source,
                             "EPSF" in h, shape, ra_c, dec_c, wcs)
    except (OSError, KeyError, ValueError) as exc:
        log(f"skipping unreadable {path.name}: {exc}")
        return None


def scan(root: Path, source: str) -> list[FrameFile]:
    """Every readable cutout under `root` (read-only; another tool may own it).

    Header facts are kept in an index under TRIM_CACHE (keyed by path, size and mtime), since
    opening ~1,500 cutouts takes minutes; the WCS itself is parsed only when needed."""
    if not root.exists():
        return []
    index_path = TRIM_CACHE / f"index_{source}.json"
    try:
        index = json.loads(index_path.read_text())
    except (OSError, ValueError):
        index = {}
    out, fresh = [], {}
    for p in sorted(root.rglob("*.fits")):
        try:
            st = p.stat()
        except OSError:
            continue
        key = str(p)
        rec = index.get(key)
        if rec and rec["size"] == st.st_size and rec["mtime"] == st.st_mtime:
            f = FrameFile(p, rec["obs_id"], rec["detector"], rec["mjd"], source, rec["epsf"], tuple(rec["shape"]),
                          rec["ra_c"], rec["dec_c"])
        else:
            f = _frame_file(p, source)
            if f is None:
                continue
        fresh[key] = {"size": st.st_size, "mtime": st.st_mtime, "obs_id": f.obs_id, "detector": f.detector,
                      "mjd": f.mjd, "epsf": f.epsf, "shape": list(f.shape), "ra_c": f.ra_c, "dec_c": f.dec_c}
        out.append(f)
    TRIM_CACHE.mkdir(parents=True, exist_ok=True)
    tmp = index_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(fresh))
    tmp.replace(index_path)
    log(f"{source} cache: {len(out)} cutouts")
    return out


# --- wavelength prediction ----------------------------------------------------------------


class WavelengthMaps:
    """Wavelength at any detector pixel, per detector, from one cached cutout's WCS-WAVE table
    (the table spans the whole detector; the cutout's 'A' WCS gives its offset)."""

    def __init__(self, frames: list[FrameFile]):
        self.maps: dict[int, tuple[Path, float, float]] = {}
        for f in frames:
            if f.detector in self.maps:
                continue
            with fits.open(f.path) as h:
                hd = h["IMAGE"].header
                if "WCS-WAVE" not in h or "CRPIX1A" not in hd:
                    continue
                ax, ay = WCS(without_sip(hd), key="A").all_pix2world(0, 0, 0)
            self.maps[f.detector] = (f.path, float(ax), float(ay))
        self._wcs: dict[int, tuple[WCS, fits.HDUList]] = {}

    def wavelength(self, detector: int, det_x: float, det_y: float) -> float | None:
        if detector not in self.maps:
            return None
        if detector not in self._wcs:
            path, ax, ay = self.maps[detector]
            hdul = fits.open(path)
            self._wcs[detector] = (WCS(without_sip(hdul["IMAGE"].header), fobj=hdul, key="W"), hdul)
        path, ax, ay = self.maps[detector]
        w = float(self._wcs[detector][0].all_pix2world(det_x - ax, det_y - ay, 0)[0])
        return w if math.isfinite(w) and w > 0 else None


def detector_position(polygon: np.ndarray, ra: float, dec: float) -> tuple[float, float] | None:
    """(x, y) detector pixel of (ra, dec) from an SIA footprint: bilinear in the corners, which
    are the detector corners in order (0,0), (N,0), (N,N), (0,N) (verified on 6 cutouts)."""
    if len(polygon) != 4:
        return None
    c = math.cos(math.radians(dec))
    p = np.column_stack([((polygon[:, 0] - ra + 180) % 360 - 180) * c, polygon[:, 1] - dec])

    def resid(uv):
        u, v = uv
        return (1 - u) * (1 - v) * p[0] + u * (1 - v) * p[1] + u * v * p[2] + (1 - u) * v * p[3]

    fit = least_squares(resid, [0.5, 0.5])
    u, v = fit.x
    if not (-0.02 <= u <= 1.02 and -0.02 <= v <= 1.02):
        return None
    return float(u * DETECTOR_PX), float(v * DETECTOR_PX)


# --- downloads -------------------------------------------------------------------------------


class Downloader:
    """Trimmed cutouts into TRIM_CACHE, with a byte budget for the session."""

    def __init__(self, budget_mb: float):
        self.budget = budget_mb * 2**20
        self.spent = 0
        self.failed = 0
        self.lock = threading.Lock()
        TRIM_CACHE.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def path(url: str, ra: float, dec: float, size: float) -> Path:
        key = hashlib.sha256(f"{url}|{ra:.5f}|{dec:.5f}|{size:.4f}".encode()).hexdigest()
        return TRIM_CACHE / key[:2] / f"{key}.fits"

    def over_budget(self) -> bool:
        with self.lock:
            return self.spent >= self.budget

    def fetch(self, url: str, ra: float, dec: float, size: float) -> Path | None:
        path = self.path(url, ra, dec, size)
        if path.exists():
            return path
        if self.over_budget():
            return None
        try:
            body = archive.fetch_cutout(url, ra, dec, size)
        except archive.UpstreamError as exc:
            with self.lock:
                self.failed += 1
            log(f"cutout failed ({exc.status}): {url.rsplit('/', 1)[-1]}")
            return None
        with self.lock:
            self.spent += len(body)
        trimmed = trim(body)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(trimmed)
        tmp.replace(path)
        return path


def trim(body: bytes) -> bytes:
    """Keep IMAGE, FLAGS, VARIANCE, WCS-WAVE and the EPSF zones that can be nearest to some
    pixel of the cutout (centres within its half-diagonal plus the zone spacing)."""
    with fits.open(io.BytesIO(body)) as h:
        hd = h["IMAGE"].header
        keep = [fits.PrimaryHDU(header=h[0].header)] + [h[n].copy() for n in ("IMAGE", "FLAGS", "VARIANCE", "WCS-WAVE") if n in h]
        if "EPSF" in h and "CRPIX1A" in hd:
            t = h["EPSF"]
            ny, nx = h["IMAGE"].data.shape
            cx, cy = WCS(without_sip(hd), key="A").all_pix2world((nx - 1) / 2, (ny - 1) / 2, 0)
            xc, yc = np.asarray(t.data["XCENTER"], float), np.asarray(t.data["YCENTER"], float)
            d = np.hypot(xc - cx, yc - cy)
            others = np.hypot(xc[:, None] - xc[None], yc[:, None] - yc[None])
            spacing = float(np.min(others[others > 0])) if (others > 0).any() else 0.0
            rows = d <= math.hypot(nx, ny) / 2 + 1.5 * spacing
            keep.append(fits.BinTableHDU(data=t.data[rows], header=t.header, name="EPSF"))
        buf = io.BytesIO()
        fits.HDUList(keep).writeto(buf)
        return buf.getvalue()


# --- objects ----------------------------------------------------------------------------------


@dataclass
class Target:
    name: str
    kind: str  # known_brown_dwarf | reference_star | mover
    ra: float  # at epoch_mjd
    dec: float
    epoch_mjd: float
    pm_ra: float = 0.0  # arcsec/yr
    pm_dec: float = 0.0
    pm_err: float = 0.0
    extra: dict = field(default_factory=dict)

    def at(self, mjd: float) -> tuple[float, float]:
        return sp.position_at(self.ra, self.dec, self.pm_ra * 1e3, self.pm_dec * 1e3,
                              si._jyear(self.epoch_mjd), mjd)

    @property
    def pm_total(self) -> float:
        return math.hypot(self.pm_ra, self.pm_dec)


@dataclass
class Field:
    key: str
    ra: float
    dec: float
    size_deg: float
    targets: list[Target]
    detectors: tuple[int, ...] = (2, 3, 4, 5)
    windows: tuple[tuple[float, float], ...] = si.INDEX_WINDOWS_UM
    frames: list[FrameFile] = field(default_factory=list)
    engine_frames: list[FrameFile] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def known_target(key: str) -> Target:
    name, ra, dec, pmra, pmdec, sptype, jmag = KNOWN[key]
    return Target(name, "known_brown_dwarf", ra, dec, si.MJD_J2000, pmra / 1e3, pmdec / 1e3, 0.0,
                  {"spectral_type": sptype, "j_mag": jmag, "field": key})


def survey_movers() -> list[Target]:
    """Every mover in the survey database, opened read-only (the worker writes it)."""
    if not SURVEY_DB.exists():
        return []
    con = sqlite3.connect(f"file:{SURVEY_DB}?mode=ro", uri=True, timeout=30)
    try:
        rows = con.execute(
            "SELECT id, ra, dec, ref_mjd, pm_ra_cosdec, pm_dec, pm_err, significance, classification, "
            "archive_verdict, nside, ipix FROM candidates WHERE kind = 'mover'"
        ).fetchall()
    finally:
        con.close()
    return [Target(f"survey mover #{r[0]}", "mover", r[1], r[2], r[3], r[4], r[5], r[6],
                   {"source": "survey", "candidate_id": r[0], "significance": r[7], "classification": r[8],
                    "archive_verdict": r[9], "patch": f"{r[10]}/{r[11]}"}) for r in rows]


def cluster(targets: list[Target], radius_deg: float) -> list[list[Target]]:
    groups: list[list[Target]] = []
    for t in targets:
        ra, dec = t.at(MID_MJD)
        for g in groups:
            gra, gdec = g[0].at(MID_MJD)
            if si.separation_arcsec(ra, dec, gra, gdec) / 3600 <= radius_deg:
                g.append(t)
                break
        else:
            groups.append([t])
    return groups


# --- choosing exposures ----------------------------------------------------------------------


def choose_visits(fld: Field, maps: WavelengthMaps, per_window: int) -> list[tuple[archive.Visit, float]]:
    """Visits whose predicted wavelength at the field centre falls in each window, PER_WINDOW
    per window, spread over the survey (evenly in time among the candidates)."""
    found = archive.query_footprints(fld.ra, fld.dec, 10.0)
    by_window: dict[tuple[float, float], list[tuple[archive.Visit, float]]] = defaultdict(list)
    n_pred = 0
    for visit, poly in found:
        if visit.detector not in fld.detectors:
            continue
        pos = detector_position(poly, fld.ra, fld.dec)
        if pos is None:
            continue
        lam = maps.wavelength(visit.detector, *pos)
        if lam is None:
            continue
        n_pred += 1
        for w in fld.windows:
            lo, hi = sp.DETECTOR_EDGES_UM[visit.detector]
            if w[0] <= lam <= w[1] and lo <= w[0] and w[1] <= hi * 1.03:
                by_window[w].append((visit, lam))
    picked: dict[str, tuple[archive.Visit, float]] = {}
    for w in fld.windows:
        cands = sorted(by_window.get(w, []), key=lambda vl: vl[0].mjd)
        if not cands:
            fld.notes.append(f"no exposure puts the field centre in {w[0]}-{w[1]} um")
            continue
        idx = np.unique(np.linspace(0, len(cands) - 1, min(per_window, len(cands))).round().astype(int))
        for i in idx:
            picked[cands[i][0].access_url] = cands[i]
    log(f"{fld.key}: {len(found)} visits, {n_pred} with a predicted wavelength, {len(picked)} chosen")
    return list(picked.values())


def download_field(fld: Field, chosen, dl: Downloader) -> list[FrameFile]:
    def one(item):
        visit, _ = item
        path = dl.fetch(visit.access_url, fld.ra, fld.dec, fld.size_deg)
        return _frame_file(path, "trim") if path else None

    with ThreadPoolExecutor(DOWNLOAD_WORKERS) as pool:
        got = [f for f in pool.map(one, chosen) if f is not None]
    log(f"{fld.key}: {len(got)} of {len(chosen)} frames on disk; downloaded so far {dl.spent / 2**20:.0f} MB")
    return got


# --- measuring -------------------------------------------------------------------------------


def measure_targets(targets: list[Target], frames: list[FrameFile]) -> list[tuple[list[sp.Measurement], list[FrameFile]]]:
    """Forced photometry of every target in every frame that covers it, opening each frame once.
    The same exposure found in two caches is measured once (trimmed download first, then larger)."""
    out: list[tuple[list[sp.Measurement], list[FrameFile]]] = [([], []) for _ in targets]
    seen: set[tuple[str, int]] = set()
    for f in sorted(frames, key=lambda f: (f.source != "trim", -f.shape[0] * f.shape[1], f.mjd)):
        key = (f.obs_id, f.detector)
        if key in seen:
            continue
        seen.add(key)
        todo = [(i, t.at(f.mjd)) for i, t in enumerate(targets)]
        todo = [(i, pos) for i, pos in todo if f.covers(*pos)]
        if not todo:
            continue
        try:
            with fits.open(f.path) as h:
                fm = sp.FrameMeasurer(h)
                for i, (ra, dec) in todo:
                    out[i][0].append(fm.measure(ra, dec))
                    out[i][1].append(f)
        except (OSError, KeyError, ValueError) as exc:
            log(f"unreadable {f.path.name}: {exc}")
    return out


def _r(x, nd=4):
    if x is None:
        return None
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return round(float(x), nd)


def analyse(t: Target, ms: list[sp.Measurement], used: list[FrameFile], stars: list[si.CatalogStar],
            seed: int) -> dict | None:
    status = defaultdict(int)
    for m in ms:
        status[m.status] += 1
    ok = [m for m in ms if m.status == "ok"]
    mid = float(np.median([m.mjd for m in ms])) if ms else MID_MJD
    ra, dec = t.at(mid)
    base = {
        "ra": _r(ra, 6), "dec": _r(dec, 6), "name": t.name, "pm_total": _r(t.pm_total, 3), "kind": t.kind,
        "mid_mjd": _r(mid, 2), "n_exposures": len(ms), "status_counts": dict(status), **t.extra,
    }
    if status["saturated"] > max(len(ok), 0) or not ok:
        return {**base, "skip": "saturated" if status["saturated"] else "no usable exposure"}
    bands, _ = sp.combine(ms)
    mjds = [m.mjd for m in ok]
    nbs = si.neighbours_of(t.ra, t.dec, t.epoch_mjd, stars, mjds, t.pm_ra, t.pm_dec, t.pm_err)
    ix = si.measure_index(bands, nbs, seed=seed)
    row = {
        **base,
        "methane_depth": _r(ix.methane_depth), "methane_err": _r(ix.methane_err),
        "water_depth": _r(ix.water_depth), "water_err": _r(ix.water_err),
        "color_1_4um": _r(ix.color_1_4um, 3), "color_is_lower_limit": ix.color_is_lower_limit,
        "snr": _r(ix.snr, 1), "n_bands": ix.n_bands,
        "features": {k: {"depth": _r(d), "err": _r(e)} for k, (d, e) in ix.features.items()},
        "contamination_max": _r(ix.contamination_max if math.isfinite(ix.contamination_max) else 99.0, 3),
        "contamination_at_um": _r(ix.contamination_at_um, 3),
        "neighbours": [{"name": n.name, "separation_arcsec": _r(n.separation_px * PIXEL_SCALE_ARCSEC, 2)}
                       for n in ix.neighbours[:3]],
        "website_suggestion": ix.website_suggestion,
        "aperture_correction": "epsf" if all(f.epsf for f in used) else ("gaussian" if not any(f.epsf for f in used) else "mixed"),
        "detectors": sorted({m.detector for m in ok}),
        "_index": ix,
    }
    if ix.contamination_max >= 0.1:
        contam = sp.contamination(bands, nbs)
        cix = si.measure_index(sp.subtract_neighbours(bands, contam), seed=seed)
        row["neighbour_subtracted"] = {
            "methane_depth": _r(cix.methane_depth), "methane_err": _r(cix.methane_err),
            "water_depth": _r(cix.water_depth), "water_err": _r(cix.water_err),
            "color_1_4um": _r(cix.color_1_4um, 3), "_index": cix,
        }
    return row


def engine_movers(fld: Field) -> list[Target]:
    """Movers the motion engine finds in the field's own (uniform) frames."""
    from planetx.engine import find_motion, frame_from_hdul

    frames = []
    for f in fld.engine_frames:
        with fits.open(f.path) as h:
            try:
                frames.append(frame_from_hdul(h, obs_id=f.obs_id))
            except (KeyError, ValueError) as exc:
                log(f"engine cannot read {f.path.name}: {exc}")
    if len(frames) < 6:
        fld.notes.append(f"engine not run: only {len(frames)} frames")
        return []
    result = find_motion(frames)
    log(f"{fld.key}: engine on {len(frames)} frames: {len(result.objects)} objects, {len(result.movers)} movers")
    return [Target(f"{fld.key} engine mover {i + 1}", "mover", o.ra, o.dec, o.ref_mjd, o.pm_ra_cosdec, o.pm_dec,
                   o.pm_err, {"source": "engine", "field": fld.key, "significance": round(o.pm_significance, 2)})
            for i, o in enumerate(result.movers)]


def reference_stars(fld: Field, stars: list[si.CatalogStar], frames: list[FrameFile], n: int) -> list[Target]:
    """Isolated 2MASS stars (J, H, Ks all measured; Ks in REF_KS) inside the cutouts, spread in J-Ks."""
    if not frames:
        return []
    probe = max(frames, key=lambda f: f.shape[0] * f.shape[1])
    cands = []
    for s in stars:
        if s.j is None or s.h is None or s.ks is None or not REF_KS[0] <= s.ks <= REF_KS[1]:
            continue
        ra, dec = s.position_at(MID_MJD)
        if not probe.covers(ra, dec, REF_EDGE_PX):
            continue
        # Isolated: no catalogued neighbour adding more than 2% of the star's own Ks flux to its aperture.
        crowded = False
        for o in stars:
            if o is s:
                continue
            sep = si.separation_arcsec(ra, dec, *o.position_at(MID_MJD)) / PIXEL_SCALE_ARCSEC
            if sep < sp.NEIGHBOUR_SEARCH_PX:
                oks = o.ks if o.ks is not None else (si.ks_from_gaia(o.g, o.bp_rp) if o.g is not None else None)
                if oks is None or 10 ** (-0.4 * (oks - s.ks)) * sp.neighbour_share(sep, 1.3) > 0.02:
                    crowded = True
                    break
        if not crowded:
            cands.append(s)
    cands.sort(key=lambda s: s.j - s.ks)
    idx = np.unique(np.linspace(0, len(cands) - 1, min(n, len(cands))).round().astype(int)) if cands else []
    out = []
    for i in idx:
        s = cands[i]
        out.append(Target(s.name, "reference_star", s.ra, s.dec, s.epoch_mjd, s.pm_ra or 0.0, s.pm_dec or 0.0, 0.0,
                          {"field": fld.key, "j_ks": round(s.j - s.ks, 3), "ks_mag": s.ks,
                           "g_mag": s.g, "bp_rp": s.bp_rp}))
    return out


# --- the search --------------------------------------------------------------------------------


def robust_std(x) -> float | None:
    x = np.asarray([v for v in x if v is not None and math.isfinite(v)], float)
    if len(x) < 5:
        return None
    return float(1.4826 * np.median(np.abs(x - np.median(x))))


def brown_dwarf_search(args) -> None:
    dl = Downloader(args.budget_mb)
    shared = scan(SHARED_CACHE / "cutouts", "shared")
    survey = scan(SURVEY_CACHE, "survey")
    dev = scan(DEV_CACHE, "dev")
    maps = WavelengthMaps([f for f in shared if f.epsf] + survey)
    log(f"wavelength maps for detectors {sorted(maps.maps)}")

    # Default order = download priority (the budget may run out): Luhman 16 is an extra.
    keys = ([k for k in args.fields.split(",") if k] if args.fields
            else [*(k for k in KNOWN if k != "luhman16"), "survey", "luhman16", "tests"])
    fields: list[Field] = []
    for key in keys:
        if key not in KNOWN and key not in ("survey", "tests"):
            raise SystemExit(f"unknown field {key!r}; choose from {', '.join(KNOWN)}, survey, tests")
        if key in KNOWN:
            t = known_target(key)
            ra, dec = t.at(MID_MJD)
            fields.append(Field(key, ra, dec, FIELD_DEG_OVERRIDE.get(key, FIELD_DEG), [t]))
    movers = survey_movers() if "survey" in keys else []
    log(f"survey database: {len(movers)} movers")
    for i, group in enumerate(cluster(movers, NEP_FIELD_DEG / 2 - 0.03)):
        pos = np.array([t.at(MID_MJD) for t in group])
        fields.append(Field(f"survey-{i + 1}", float(np.mean(pos[:, 0])), float(np.mean(pos[:, 1])), NEP_FIELD_DEG,
                            group, detectors=(3, 4, 5),
                            windows=tuple(w for w in si.INDEX_WINDOWS_UM if w[0] >= 1.63)))
    for key, (name, ra, dec, pmra, pmdec) in (TEST_FIELDS.items() if "tests" in keys else ()):
        t = Target(name, "test_target", ra, dec, si.MJD_J2000, pmra / 1e3, pmdec / 1e3, 0.0, {"field": key})
        fra, fdec = t.at(MID_MJD)
        fields.append(Field(key, fra, fdec, 0.1, [t], detectors=()))

    group_of = {f.key: ("survey" if f.key.startswith("survey-") else "tests" if f.key in TEST_FIELDS else f.key)
                for f in fields}
    fields.sort(key=lambda f: keys.index(group_of[f.key]))
    rows: list[dict] = []
    for n_field, fld in enumerate(fields):
        log(f"--- field {fld.key} at {fld.ra:.4f} {fld.dec:+.4f}")
        mine: list[FrameFile] = []
        if fld.detectors:
            try:
                chosen = choose_visits(fld, maps, PER_WINDOW if not fld.key.startswith("survey") else 3)
            except archive.UpstreamError as exc:
                fld.notes.append(f"SIA failed: {exc}")
                log(f"{fld.key}: SIA failed: {exc}")
                chosen = []
            mine = download_field(fld, chosen, dl)
        local = [f for f in shared + survey + dev + mine if f.covers(fld.ra, fld.dec, 0)]
        fld.frames = local
        fld.engine_frames = mine if mine else [f for f in dev if f.covers(fld.ra, fld.dec, 0)]
        log(f"{fld.key}: {len(local)} frames cover the field ({len(mine)} downloaded here)")
        radius = fld.size_deg / 2 * math.sqrt(2) * 3600 + 30
        try:
            stars = si.fetch_field_stars(fld.ra, fld.dec, min(radius, si.MAX_CONE_ARCSEC))
        except Exception as exc:  # catalogue down: no neighbour check here, say so
            log(f"{fld.key}: catalogues failed: {exc}")
            fld.notes.append(f"Gaia/2MASS query failed ({type(exc).__name__}): no neighbour check, no reference stars")
            stars = []
        targets = list(fld.targets)
        if fld.engine_frames:
            for m in engine_movers(fld):
                if all(si.separation_arcsec(*m.at(MID_MJD), *t.at(MID_MJD)) > 2 * PIXEL_SCALE_ARCSEC for t in targets):
                    targets.append(m)
                else:
                    near = min(targets, key=lambda t: si.separation_arcsec(*m.at(MID_MJD), *t.at(MID_MJD)))
                    near.extra["engine_mover"] = True
                    near.extra["engine_pm_total"] = round(m.pm_total, 3)
        if mine:
            targets += reference_stars(fld, stars, mine, REF_PER_FIELD)
        log(f"{fld.key}: measuring {len(targets)} objects")
        measured = measure_targets(targets, local)
        for k, (t, (ms, used)) in enumerate(zip(targets, measured)):
            row = analyse(t, ms, used, stars, seed=1000 * n_field + k)
            if row is not None:
                row.setdefault("field", fld.key)
                rows.append(row)
        for note in fld.notes:
            log(f"{fld.key}: {note}")

    write_brown_dwarfs(rows, fields, dl, args)


def write_brown_dwarfs(rows: list[dict], fields: list[Field], dl: Downloader, args) -> None:
    kinds = ("mover", "reference_star", "known_brown_dwarf")
    measured = [r for r in rows if "skip" not in r and r["kind"] in kinds]
    test_targets = [r for r in rows if "skip" not in r and r["kind"] not in kinds]
    skipped = [{k: r[k] for k in ("name", "kind", "field", "ra", "dec", "skip", "status_counts") if k in r}
               for r in rows if "skip" in r]
    # Honest errors: how much do reference stars scatter beyond their formal errors?
    refs = [r for r in measured if r["kind"] == "reference_star" and r["contamination_max"] < 0.1 and (r["snr"] or 0) >= 20]
    scales = {}
    for mol in ("methane", "water"):
        pulls = [r[f"{mol}_depth"] / r[f"{mol}_err"] for r in refs if r[f"{mol}_depth"] is not None and r[f"{mol}_err"]]
        s = robust_std(pulls)
        scales[mol] = max(1.0, s) if s is not None else 1.0
    scale = max(scales.values())
    for r in measured + test_targets:
        ix = r.pop("_index")
        r["flag"], r["reason"] = si.flag(ix, error_scale=scale)
        if "neighbour_subtracted" in r:
            cix = r["neighbour_subtracted"].pop("_index")
            r["neighbour_subtracted"]["flag"], r["neighbour_subtracted"]["reason"] = si.flag(cix, error_scale=scale)
        for mol in ("methane", "water"):
            if r[f"{mol}_err"] is not None:
                r[f"{mol}_err_formal"] = r[f"{mol}_err"]
                r[f"{mol}_err"] = _r(r[f"{mol}_err"] * scale)

    def stats(kind, key, flagset=None):
        v = [r[key] for r in measured if r["kind"] == kind and r[key] is not None and (flagset is None or r["flag"] in flagset)]
        if not v:
            return None
        return {"n": len(v), "median": _r(float(np.median(v)), 3), "p05": _r(float(np.percentile(v, 5)), 3),
                "p95": _r(float(np.percentile(v, 95)), 3), "min": _r(min(v), 3), "max": _r(max(v), 3)}

    clean = {"suggests_brown_dwarf", "star_like", "too_faint"}
    ref_good = [r for r in measured if r["kind"] == "reference_star" and r["flag"] == "star_like"]
    bds = [r for r in measured if r["kind"] == "known_brown_dwarf"]
    movers = [r for r in measured if r["kind"] == "mover"]
    ref_ch4_p99 = float(np.percentile([r["methane_depth"] for r in ref_good], 99)) if len(ref_good) >= 5 else None
    separation = []
    for r in bds:
        separation.append({
            "name": r["name"], "flag": r["flag"], "methane_depth": r["methane_depth"], "methane_err": r["methane_err"],
            "water_depth": r["water_depth"], "water_err": r["water_err"],
            "above_all_reference_stars": (r["methane_depth"] is not None and ref_good != []
                                          and r["methane_depth"] > max(x["methane_depth"] for x in ref_good)),
        })
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in measured:
        counts[r["kind"]][r["flag"]] += 1
    out = {
        "name": "brown_dwarfs", "version": 1, "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": "uv run python tools/brown_dwarf_search.py " + " ".join(sys.argv[1:]),
        "notes": [
            "Depths are 1 - (flux in the band / power-law continuum), averaged over the 1.6, 2.2, 3.3 um methane "
            "and 1.4, 1.9, 2.7 um water features that were measured; 0 = no absorption.",
            f"Errors are Monte Carlo errors of the band fluxes, multiplied by {scale:.2f}: the scatter of "
            "isolated reference stars beyond their formal errors (methane_err_formal keeps the unscaled value).",
            "suggests_brown_dwarf needs methane AND water each >= 0.15 deep and >= 3 sigma; star_like needs a methane "
            f"depth below {si.METHANE_BROWN_DWARF} at 2 sigma. A suggestion from a low-resolution spectrum, never a "
            "classification.",
            "contaminated: a Gaia DR3 / 2MASS neighbour could supply >= 50% of the measured flux in a band the "
            "indices use (PSF overlap with 1.5 px aperture, stellar SED extrapolated with a Rayleigh-Jeans tail). "
            "neighbour_subtracted repeats the indices after removing that predicted flux: a cross-check only.",
            "color_1_4um = m_AB(1.22-1.31 um) - m_AB(3.95-4.15 um); a Rayleigh-Jeans star is -2.5.",
            "Exposures of all passes are combined: a variable object's spectrum mixes its states.",
            "Dev-cache fields (barnard, r-dra) have no EPSF in their files: aperture corrections there come from the "
            "header PSF_FWHM (aperture_correction says which).",
        ] + [f"{f.key}: {n}" for f in fields for n in f.notes],
        "objects": measured,
        "skipped": skipped,
        "test_targets": test_targets,
        "summary": {
            "n_objects": len(measured),
            "n_suggests_brown_dwarf": sum(r["flag"] == "suggests_brown_dwarf" for r in measured),
            "by_kind": {k: dict(v) for k, v in counts.items()},
            "n_movers": len(movers),
            "n_movers_suggest_brown_dwarf": sum(r["flag"] == "suggests_brown_dwarf" for r in movers),
            "error_scale": {k: _r(v, 3) for k, v in scales.items()},
            "reference_methane": stats("reference_star", "methane_depth", clean),
            "reference_water": stats("reference_star", "water_depth", clean),
            "known_brown_dwarf_methane": stats("known_brown_dwarf", "methane_depth"),
            "reference_methane_p99_star_like": _r(ref_ch4_p99, 3),
            "known_brown_dwarfs": separation,
            "downloaded_mb": round(dl.spent / 2**20, 1),
            "failed_downloads": dl.failed,
        },
    }
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / "brown_dwarfs.json"
    path.write_text(json.dumps(out, indent=1, allow_nan=False) + "\n")
    log(f"wrote {path} ({len(measured)} objects, {len(skipped)} skipped)")
    print(json.dumps(out["summary"], indent=1))


# --- R Draconis light curve ----------------------------------------------------------------------


LIGHT_CURVE_BIN_TRIALS = (0.002, 0.005, 0.01, 0.02)


def _lc_fit(wings: list[si.WingFlux], bin_frac: float):
    """Group wing fluxes in wavelength bins `bin_frac` wide (per detector) and fit a period.

    A Mira's spectrum changes by tens of percent within one of the 102 bands (water and TiO
    bands), so only nearly equal wavelengths are compared. Bins with two or more points more
    than 30 days apart carry the information on changes; the rest are left out."""
    pts = [(w, w.detector * 10000 + math.floor(math.log(w.wavelength_um) / math.log1p(bin_frac))) for w in wings]
    by_bin = defaultdict(list)
    for w, b in pts:
        by_bin[b].append(w)
    usable = {b for b, ws in by_bin.items() if len(ws) >= 2 and max(x.mjd for x in ws) - min(x.mjd for x in ws) > 30}
    sel = [(w, b) for w, b in pts if b in usable]
    if len(sel) < 10:
        raise ValueError(f"only {len(sel)} usable measurements with {bin_frac:.1%} bins")
    t = np.array([w.mjd for w, _ in sel])
    y = np.log([w.flux for w, _ in sel])
    # 2% calibration floor per exposure on top of the noise (as the spectra use).
    e = np.array([math.hypot(w.flux_err / w.flux, sp.SYSTEMATIC_FRAC) for w, _ in sel])
    g = np.array([b for _, b in sel])
    a = np.array([w.detector for w, _ in sel])
    fit, keep = si.fit_period_robust(t, y, e, g, a, periods=np.geomspace(60.0, 1500.0, 3000))
    return sel, t, y, e, g, fit, keep, usable


def light_curve(args) -> None:
    name, ra0, dec0, pmra, pmdec = TEST_FIELDS["r-dra"]
    star = Target(name, "test_target", ra0, dec0, si.MJD_J2000, pmra / 1e3, pmdec / 1e3)
    ra, dec = star.at(MID_MJD)
    dl = Downloader(args.budget_mb)
    have = [f for f in scan(DEV_CACHE, "dev") + scan(SHARED_CACHE / "cutouts", "shared") + scan(TRIM_CACHE, "trim")
            if f.covers(*star.at(f.mjd), 15)]
    have_keys = {(f.obs_id, f.detector) for f in have}
    log(f"{len(have)} cached exposures of R Dra")
    try:
        visits = archive.query_visits(ra, dec, 10.0)
    except archive.UpstreamError as exc:
        log(f"SIA failed ({exc}); using cached exposures only")
        visits = []
    missing = [v for v in visits if v.detector in LIGHT_CURVE_DETECTORS and (v.obs_id, v.detector) not in have_keys]
    log(f"{len(visits)} visits listed; {len(missing)} at detectors {LIGHT_CURVE_DETECTORS} not cached")
    def one(v: archive.Visit) -> FrameFile | None:
        path = dl.fetch(v.access_url, ra, dec, 0.1)
        return _frame_file(path, "trim") if path else None

    with ThreadPoolExecutor(DOWNLOAD_WORKERS) as pool:
        got = [f for f in pool.map(one, missing) if f]
    have += got
    log(f"downloaded {len(got)} ({dl.spent / 2**20:.0f} MB); {len(have)} exposures in all")

    wings = []
    seen = set()
    for f in sorted(have, key=lambda f: f.mjd):
        if (f.obs_id, f.detector) in seen or f.detector not in LIGHT_CURVE_DETECTORS:
            continue
        seen.add((f.obs_id, f.detector))
        with fits.open(f.path) as h:
            w = si.wing_flux(h, *star.at(f.mjd))
        if w is not None and w.flux > 0 and sp.band_index(w.detector, w.wavelength_um) is not None:
            wings.append(w)
    lit = 245.0
    sel, t, y, e, g, fit, keep, usable = _lc_fit(wings, args.bin_frac)
    log(f"{len(wings)} wing measurements, {len(sel)} in {len(usable)} wavelength bins seen at two or more epochs; "
        f"fit kept {keep.sum()}")
    # How much does the period depend on the choice of wavelength bins? (an honest error)
    by_binning = []
    for frac in LIGHT_CURVE_BIN_TRIALS:
        try:
            other = _lc_fit(wings, frac)
        except ValueError:
            continue
        f2 = other[5]
        by_binning.append({"bin_frac": frac, "n_points": int(other[6].sum()), "period_days": _r(f2.period_days, 1),
                           "period_err_days": _r(f2.period_err_days, 1),
                           "reduced_chi2": _r(f2.chi2_best / max(f2.dof, 1), 1)})
    spread = [b["period_days"] for b in by_binning]
    log(f"period by binning: {by_binning}")
    i_lit = int(np.argmin(np.abs(fit.periods - lit)))
    s2 = max(1.0, fit.chi2_best / fit.dof)
    # Other local minima within reach of the best one: aliases the sampling allows.
    mins = [i for i in range(1, len(fit.chi2) - 1) if fit.chi2[i] < fit.chi2[i - 1] and fit.chi2[i] <= fit.chi2[i + 1]]
    alts = sorted((fit.chi2[i] - fit.chi2_best, fit.periods[i]) for i in mins)
    alts = [{"period_days": _r(p, 1), "delta_chi2_scaled": _r(d / s2, 2)} for d, p in alts[:6]]
    points = []
    for (w, b), yi, ei, kept in zip(sel, y, e, keep):
        if b not in fit.offsets:  # every point of its bin was an outlier
            continue
        rel = math.exp(yi - fit.offsets[b])
        points.append({"mjd": _r(w.mjd, 4), "flux": _r(rel, 4), "flux_err": _r(rel * ei, 4),
                       "wavelength": _r(w.wavelength_um, 4), "detector": w.detector,
                       "band": sp.band_index(w.detector, w.wavelength_um), "wavelength_bin": b,
                       "wing_flux_mjy": _r(w.flux, 4), "outlier": not bool(kept)})
    baseline = float(t[keep].max() - t[keep].min())
    dchi_lit = (fit.chi2[i_lit] - fit.chi2_best) / s2
    honest = []
    if baseline < 2 * lit:
        honest.append(f"The SPHEREx data span {baseline:.0f} days, {baseline / lit:.1f} cycles of the literature "
                      "period: enough to see the star vary, too short to pin a period down.")
    if spread:
        honest.append(
            f"The best period moves between {min(spread):.0f} and {max(spread):.0f} days when the wavelength bins "
            f"change ({', '.join(f'{b['bin_frac']:.1%}' for b in by_binning)}); that spread, not period_err_days, is "
            "the honest uncertainty. The literature period (~245 d) lies inside it."
            if min(spread) <= lit <= max(spread) else
            f"The best period moves between {min(spread):.0f} and {max(spread):.0f} days when the wavelength bins "
            "change; that spread, not period_err_days, is the honest uncertainty."
        )
    honest += fit.notes
    out = {
        "name": "light_curve_r_dra", "version": 1,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": "uv run python tools/brown_dwarf_search.py " + " ".join(sys.argv[1:]),
        "notes": [
            "R Draconis saturates SPHEREx at every wavelength measured, so its brightness is the flux in an "
            f"annulus {si.WING_ANNULUS_PX[0]:g}-{si.WING_ANNULUS_PX[1]:g} px around it (PSF wings), which is a fixed "
            "fraction of the total only at one wavelength and detector position.",
            f"flux is relative: each point divided by the fitted mean level of its own wavelength bin ({args.bin_frac:.1%} "
            "wide, per detector), so points from different wavelengths can share one curve. flux_err includes a 2% "
            "calibration floor.",
            f"Detectors {', '.join(map(str, LIGHT_CURVE_DETECTORS))} (0.75-2.42 um); only wavelength bins seen at two "
            "or more epochs over 30 days apart are used.",
            "Model: ln F = bin level + per-detector sinusoid with a common period and phase. Points more than "
            "4x the robust residual scatter from the model are marked outlier and left out of the fit; "
            "period_err_days is scaled by the reduced chi^2.",
            f"Reduced chi^2 of the best fit: {fit.chi2_best / max(fit.dof, 1):.1f}: the wing fluxes scatter "
            "much more than their photon noise (exposures minutes apart in the same band can differ by tens of "
            "percent), so the curve shows the trend, not small wiggles.",
        ] + honest,
        "n_outliers": int((~keep).sum()),
        "points": points,
        "period_days_fit": _r(fit.period_days, 1),
        "period_err_days": _r(fit.period_err_days, 1),
        "period_constrained": fit.constrained,
        "period_days_literature": lit,
        "period_days_by_binning": by_binning,
        "delta_chi2_at_literature_period": _r(dchi_lit, 2),
        "amplitude_mag_by_detector": {str(k): _r(v, 3) for k, v in fit.amplitude_mag.items()},
        "chi2_constant": _r(fit.chi2_constant, 1), "chi2_best": _r(fit.chi2_best, 1), "dof": fit.dof,
        "other_minima": alts,
        "baseline_days": _r(baseline, 1),
        "n_points": len(points), "n_wavelength_bins": len(usable),
        "mjd_range": [_r(float(t.min()), 2), _r(float(t.max()), 2)],
        "periodogram": [{"period_days": _r(p, 2), "delta_chi2_scaled": _r((c - fit.chi2_best) / s2, 2)}
                        for p, c in zip(fit.periods[::10], fit.chi2[::10])],
        "downloaded_mb": round(dl.spent / 2**20, 1),
    }
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / "light_curve_r_dra.json"
    path.write_text(json.dumps(out, indent=1, allow_nan=False) + "\n")
    log(f"wrote {path}")
    print(json.dumps({k: v for k, v in out.items() if k not in ("points", "periodogram")}, indent=1))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--light-curve", action="store_true", help="R Draconis light curve instead of the brown-dwarf search")
    p.add_argument("--budget-mb", type=float, default=1300.0, help="cap on new IRSA downloads this run (MB)")
    p.add_argument("--bin-frac", type=float, default=0.005, help="light curve: wavelength bin width (fraction)")
    p.add_argument("--fields", default="", help=f"comma list of known-dwarf fields (default all: {','.join(KNOWN)})")
    args = p.parse_args()
    if args.light_curve:
        light_curve(args)
    else:
        brown_dwarf_search(args)


if __name__ == "__main__":
    main()
