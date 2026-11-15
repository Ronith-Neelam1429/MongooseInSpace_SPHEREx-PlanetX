"""Aligned SPHEREx cutouts: disk cache, resampling onto a common sky grid, rendering.

Every frame of a position is resampled onto ONE grid built by ``grid_wcs``: a
north-up TAN projection centred on (ra, dec) at the SPHEREx pixel scale, so
frames from different visits line up pixel for pixel. Array orientation:
row 0 is the NORTH (top) edge and column 0 the EAST (left) edge, as the sky is
shown in Aladin; the returned WCS describes the array exactly as returned.

Masking: the FLAGS bit names below were read from the MSKNnnnn cards of a live
cutout's FLAGS header and match IRSA's documentation. Display products mask
only pixels with no usable measurement (UNUSABLE_FLAGS). IRSA's spectrophotometry
tool ignores a wider set (PHOTOMETRY_BAD_FLAGS) that includes BLOOM and
SUR_ERROR; those cover the cores of bright stars such as Barnard's Star, so they
are left for the photometry/detection pipeline to apply.

Disk cache: settings.cache_dir/cutouts, ~4 MB per field cutout (~25 MB for a survey
patch's), capped at settings.cache_max_mb. A hit refreshes the entry's mtime; after each new download
the least recently used entries are deleted until the cache fits the cap, except
entries this process has `pinned` (a survey patch's cutouts while it is processed).

Cropping: an IRSA cutout is an exact sub-array of the file, and only the CRPIX
cards (CRPIXn, CRPIXnA, CRPIXnW) and NAXISn differ between two cutouts of one
file (verified live, Nov 2026). `crop_cutout` therefore cuts the frame of a small
request out of a larger cutout of the same file locally.
"""
import base64
import hashlib
import io
import logging
import math
import os
import re
import tempfile
import threading
import time
import warnings
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning
from PIL import Image
from reproject import reproject_interp

from planetx.config import settings
from planetx.engine.frames import celestial_header
from planetx.spherex import archive

log = logging.getLogger(__name__)

PIXEL_SCALE_ARCSEC = 6.15  # SPHEREx (SIA s_pixel_scale)
# Requested cutout side: covers the output grid at any rotation (sqrt 2), plus a
# margin for the interpolation kernel, odd-pixel rounding and centre snapping.
CUTOUT_OVERSIZE = 1.5
CUTOUT_MARGIN_DEG = 4 * PIXEL_SCALE_ARCSEC / 3600
# Cutout centres are snapped to this grid (3.6", about half a pixel) so nearby
# views share one cached download and arbitrary float positions cannot each
# trigger a new download. Alignment still uses the exact requested centre.
CUTOUT_CENTER_STEP_DEG = 0.001
ASINH_SOFTENING = 0.1
PERCENTILES = (0.5, 99.5)

FLAG_BITS = {
    "TRANSIENT": 0, "OVERFLOW": 1, "SUR_ERROR": 2, "NONFUNC": 6, "DICHROIC": 7,
    "MISSING_DATA": 9, "HOT": 10, "COLD": 11, "FULLSAMPLE": 12, "PHANMISS": 14,
    "NONLINEAR": 15, "PERSIST": 17, "OUTLIER": 19, "CROSSTALK": 20, "SOURCE": 21,
    "GHOST": 22, "GHOST_FPA": 23, "GHOST_EXT": 24, "STREAK": 25, "BLOOM": 26,
    "SNOWBALL": 27, "HALO": 28, "SATELLITE_HALO": 29,
}
UNUSABLE_FLAGS = ("NONFUNC", "MISSING_DATA", "HOT", "COLD")
# https://irsa.ipac.caltech.edu/onlinehelp/spherex/spherex/sp.html (pixels ignored by Tractor)
PHOTOMETRY_BAD_FLAGS = (
    "SUR_ERROR", "NONFUNC", "MISSING_DATA", "HOT", "COLD", "NONLINEAR", "PERSIST",
    "BLOOM", "SNOWBALL", "HALO", "SATELLITE_HALO",
)

_SIP_CARD = re.compile(r"(A|B|AP|BP)_(ORDER|\d+_\d+)")
_CRPIX_CARD = re.compile(r"CRPIX([12])[A-Z]?")
# The planes `crop_cutout` cuts (ZODI is optional); WCS-WAVE is copied whole. The
# EPSF table (~3.8 MB of every cutout) is left out: nothing downstream reads it.
CROP_PLANES = ("IMAGE", "FLAGS", "VARIANCE", "ZODI")
# Striped locks: concurrent requests for the same cutout download it once.
_download_locks = [threading.Lock() for _ in range(64)]
_evict_lock = threading.Lock()
_pinned: Counter[Path] = Counter()
# Share of the cap the unpinned entries (e.g. the website's cutouts) keep however much is pinned.
UNPINNED_FLOOR = 0.25
_last_over_cap_log = float("-inf")
_pinned_lock = threading.Lock()


def flag_mask(names: tuple[str, ...]) -> int:
    """Bitmask of the named FLAGS planes."""
    return sum(1 << FLAG_BITS[name] for name in names)


@dataclass(frozen=True)
class AlignedCutout:
    data: np.ndarray  # float64 (height, width); row 0 north, column 0 east; NaN = no data/masked
    wcs: WCS  # describes `data` exactly as stored
    mjd: float  # exposure midpoint (MJD-AVG)
    wavelength_um: float | None  # band centre at the grid centre
    bunit: str
    detector: int | None


def grid_wcs(ra: float, dec: float, size_deg: float) -> tuple[WCS, tuple[int, int]]:
    """The common output grid for a position: WCS and (height, width).

    The side is an odd number of pixels so (ra, dec) is the centre of a pixel.
    """
    n = max(3, round(size_deg * 3600 / PIXEL_SCALE_ARCSEC)) | 1
    scale = PIXEL_SCALE_ARCSEC / 3600
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    wcs.wcs.crval = [ra, dec]
    wcs.wcs.crpix = [(n + 1) / 2, (n + 1) / 2]
    # x grows westward (east on the left), y grows southward (row 0 is north).
    wcs.wcs.cdelt = [-scale, -scale]
    return wcs, (n, n)


def snap_center(ra: float, dec: float) -> tuple[float, float]:
    """(ra, dec) snapped to the CUTOUT_CENTER_STEP_DEG grid of cutout centres."""
    step = CUTOUT_CENTER_STEP_DEG
    return round(round(ra / step) * step % 360, 7), round(min(90.0, max(-90.0, round(dec / step) * step)), 7)


def cutout_request(ra: float, dec: float, size_deg: float) -> tuple[float, float, float]:
    """(ra, dec, size) of the raw cutout to request from IRSA for an output grid."""
    return *snap_center(ra, dec), round(size_deg * CUTOUT_OVERSIZE + CUTOUT_MARGIN_DEG, 5)


def _atomic_write(path: Path, body: bytes) -> None:
    """Write via temp file + rename so a crash never leaves a partial cache entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def cutout_path(url: str, ra: float, dec: float, size_deg: float) -> Path:
    """Where `cached_cutout` keeps the cutout of this request (whether or not it is there yet)."""
    archive.validate_spherex_url(url)
    ra, dec, size_deg = round(ra, 7), round(dec, 7), round(size_deg, 5)
    digest = hashlib.sha256(f"{url}|{ra:.7f}|{dec:.7f}|{size_deg:.5f}".encode()).hexdigest()
    return settings.cache_dir / "cutouts" / digest[:2] / f"{digest}.fits"


@contextmanager
def pinned(paths: Iterable[Path]) -> Iterator[None]:
    """Within the block, eviction by this process skips `paths` (present or still to be downloaded).

    Pins nest and count. The cache can then exceed its cap by the pinned files;
    other processes sharing the cache directory do not see the pins.
    """
    paths = list(paths)
    with _pinned_lock:
        _pinned.update(paths)
    try:
        yield
    finally:
        with _pinned_lock:
            _pinned.subtract(paths)
            for path in paths:
                if _pinned[path] <= 0:
                    del _pinned[path]


def cached_cutout(url: str, ra: float, dec: float, size_deg: float) -> Path:
    """Path of the raw MEF cutout (downloading and caching it if needed).

    Keyed by (url, centre, size) as sent to IRSA. Only validated FITS is cached.
    """
    ra, dec, size_deg = round(ra, 7), round(dec, 7), round(size_deg, 5)
    path = cutout_path(url, ra, dec, size_deg)
    lock = _download_locks[int(path.stem[:8], 16) % len(_download_locks)]
    if not lock.acquire(timeout=archive.TOTAL_DEADLINE_S):
        raise archive.IrsaBusy("cutout downloads are queued up; try again shortly")
    try:
        if path.exists():
            _touch(path)
            return path
        _atomic_write(path, archive.fetch_cutout(url, ra, dec, size_deg))
    finally:
        lock.release()
    _evict(settings.cache_dir / "cutouts", settings.cache_max_mb * 1024 * 1024, keep=path)
    return path


def cache_path(url: str, ra: float, dec: float, size_deg: float) -> Path:
    """Where `cached_cutout` keeps this cutout (it may not exist yet); same as `cutout_path`."""
    return cutout_path(url, ra, dec, size_deg)


def _touch(path: Path) -> None:
    """Mark a cache entry as recently used (mtime drives eviction order)."""
    try:
        os.utime(path)
    except FileNotFoundError:
        pass  # evicted concurrently; the caller's open retries via aligned_cutout


def _evict(root: Path, max_bytes: int, keep: Path) -> None:
    """Delete least recently used cutouts until the cache is at most max_bytes.

    `keep` (the entry just written) and pinned entries are never deleted, so a
    single cutout larger than the cap still works. When they alone fill most of
    the cap, the other entries still keep UNPINNED_FLOOR of it rather than all
    going (the cache then stays above its cap, logged). Entries vanishing
    mid-scan (another thread or process evicting) are skipped.
    """
    global _last_over_cap_log
    with _evict_lock:
        with _pinned_lock:
            protected = {keep, *_pinned}
        entries = []
        for p in root.rglob("*.fits"):
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            entries.append((st.st_mtime, st.st_size, p))
        fixed = sum(size for _, size, p in entries if p in protected)
        budget = max(max_bytes - fixed, int(max_bytes * UNPINNED_FLOOR))
        rest = sum(size for _, size, p in entries if p not in protected)
        for _, size, p in sorted(entries, key=lambda e: e[0]):
            if rest <= budget:
                break
            if p in protected:
                continue
            p.unlink(missing_ok=True)
            rest -= size
        if fixed + rest > max_bytes and time.monotonic() - _last_over_cap_log > 60:
            _last_over_cap_log = time.monotonic()
            log.warning("cutout cache at %d MB, above its %d MB cap: %d MB are pinned or just written",
                        (fixed + rest) // 2**20, max_bytes // 2**20, fixed // 2**20)


def _wavelength_wcs(header: fits.Header, hdul: fits.HDUList) -> WCS:
    """The per-pixel (wavelength, bandwidth) lookup WCS (alternate key W).

    Its table is indexed by raw pixel coordinates, so the celestial SIP
    distortion cards are dropped first (astropy would otherwise apply them).
    """
    return WCS(without_sip(header), fobj=hdul, key="W")


def without_sip(header: fits.Header) -> fits.Header:
    """A copy of `header` without the celestial SIP distortion cards.

    For the alternate pixel-indexed WCSs of SPHEREx files (W: wavelength, A: detector
    pixels), which astropy would otherwise distort with the celestial SIP terms.
    """
    header = header.copy()
    for key in [k for k in header if _SIP_CARD.fullmatch(k)]:
        del header[key]
    return header


def cutout_wcs(header: fits.Header) -> WCS:
    """The celestial WCS (SIP included) of a cutout's IMAGE `header`, as `crop_box` takes it."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        return WCS(celestial_header(header)).celestial


def crop_box(wcs: WCS, shape: tuple[int, int], ra: float, dec: float, size_deg: float
             ) -> tuple[int, int, int, int] | None:
    """Pixel box (x0, x1, y0, y1), end-exclusive and clipped to `shape`, that an IRSA
    request (ra, dec, size_deg) covers in the cutout of `cutout_wcs` `wcs`; None when
    (ra, dec) is off the cutout (IRSA answers such a request with 422).

    IRSA's rule, measured on 107 live field cutouts (Nov 2026; it also gave the
    boxes of two ~1 deg cutouts): n pixels a side, the first at floor(c + 1 - n/2)
    where c is the 0-based pixel of (ra, dec) in the full (SIP) WCS. n is
    `size_deg` at the nominal scale, rounded (180 for a 0.2 deg field's request,
    as IRSA returned in every case).
    """
    x, y = wcs.world_to_pixel_values(ra, dec)
    ny, nx = shape
    if not (np.isfinite(x) and np.isfinite(y) and -0.5 <= x < nx - 0.5 and -0.5 <= y < ny - 0.5):
        return None
    side = size_deg * 3600 / PIXEL_SCALE_ARCSEC
    n = max(1, math.floor(side + 0.5))
    x0, y0 = math.floor(x + 1 - n / 2), math.floor(y + 1 - n / 2)
    return max(x0, 0), min(x0 + n, nx), max(y0, 0), min(y0 + n, ny)


def _shifted(header: fits.Header, dx: int, dy: int) -> fits.Header:
    header = header.copy()
    for key in list(header):
        match = _CRPIX_CARD.fullmatch(key)
        if match:
            header[key] = header[key] - (dx if match.group(1) == "1" else dy)
    for key in ("BZERO", "BSCALE"):  # the data are copied already scaled
        header.remove(key, ignore_missing=True)
    return header


def crop_cutout(
    hdul: fits.HDUList, ra: float, dec: float, size_deg: float, wcs: WCS | None = None
) -> fits.HDUList | None:
    """The cutout IRSA would return for request (ra, dec, size_deg), cut from `hdul`,
    a larger cutout of the same file; None when (ra, dec) is off it.

    The planes in CROP_PLANES are sliced and their CRPIX cards shifted, so every
    WCS (celestial with SIP, and the W wavelength lookup) keeps describing the
    same sky and detector pixels; the PRIMARY header and WCS-WAVE are copied. The
    request must lie inside `hdul`'s own request (or the file's edge), otherwise
    the crop comes out smaller than IRSA's cutout would be. `wcs` is
    `cutout_wcs` of `hdul` (worth passing when cropping one cutout many times).
    """
    image = hdul["IMAGE"]
    box = crop_box(cutout_wcs(image.header) if wcs is None else wcs, image.data.shape, ra, dec, size_deg)
    if box is None:
        return None
    x0, x1, y0, y1 = box
    out = fits.HDUList([fits.PrimaryHDU(header=hdul[0].header.copy())])
    for name in CROP_PLANES:
        if name not in hdul:
            continue
        hdu = hdul[name]
        if hdu.data is None or hdu.data.shape != image.data.shape:
            raise ValueError(f"cutout {name} plane does not match IMAGE")
        out.append(fits.ImageHDU(np.array(hdu.data[y0:y1, x0:x1]), header=_shifted(hdu.header, x0, y0), name=name))
    if "WCS-WAVE" in hdul:
        wave = hdul["WCS-WAVE"]
        out.append(fits.BinTableHDU(data=wave.data.copy(), header=wave.header.copy(), name="WCS-WAVE"))
    return out


def align(
    path: Path, ra: float, dec: float, size_deg: float,
    mask_flags: tuple[str, ...] = UNUSABLE_FLAGS,
) -> AlignedCutout:
    """Resample a cutout's IMAGE onto ``grid_wcs(ra, dec, size_deg)``.

    Pixels that are non-finite or carry any of ``mask_flags`` become NaN before
    bilinear resampling, so output pixels touching them are NaN too.
    """
    with fits.open(path) as hdul, warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        header = hdul["IMAGE"].header
        data = np.array(hdul["IMAGE"].data, dtype=np.float64)
        data[~np.isfinite(data) | ((hdul["FLAGS"].data & flag_mask(mask_flags)) != 0)] = np.nan
        wcs_in = WCS(header)
        x, y = wcs_in.world_to_pixel_values(ra, dec)
        wavelength, _ = _wavelength_wcs(header, hdul).all_pix2world(x, y, 0)
    out_wcs, shape = grid_wcs(ra, dec, size_deg)
    aligned = reproject_interp(
        (data, wcs_in), out_wcs, shape_out=shape, order="bilinear", return_footprint=False
    )
    wavelength = float(wavelength)
    detector = header.get("DETECTOR")
    return AlignedCutout(
        data=aligned,
        wcs=out_wcs,
        mjd=float(header["MJD-AVG"]),
        wavelength_um=wavelength if np.isfinite(wavelength) else None,
        bunit=str(header.get("BUNIT", "")).replace(" ", ""),
        detector=int(detector) if detector is not None else None,
    )


def aligned_cutout(
    url: str, ra: float, dec: float, size_deg: float,
    mask_flags: tuple[str, ...] = UNUSABLE_FLAGS,
) -> AlignedCutout:
    """Fetch (or reuse) the cutout of one SPHEREx file and align it to the common grid."""
    request = cutout_request(ra, dec, size_deg)
    try:
        return align(cached_cutout(url, *request), ra, dec, size_deg, mask_flags)
    except FileNotFoundError:
        # Evicted by another request between caching and opening: fetch it again once.
        return align(cached_cutout(url, *request), ra, dec, size_deg, mask_flags)


def encode_float32(data: np.ndarray) -> str:
    """Base64 of little-endian float32, row-major (the CutoutData.data_b64 format)."""
    return base64.b64encode(np.ascontiguousarray(data, dtype="<f4").tobytes()).decode("ascii")


def decode_float32(b64: str, width: int, height: int) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b64), dtype="<f4").reshape(height, width)


def render_png(data: np.ndarray, vmin: float | None = None, vmax: float | None = None) -> bytes:
    """8-bit grayscale + alpha PNG with an asinh stretch; NaN pixels are transparent.

    vmin/vmax are in data units and default to the 0.5/99.5 percentiles of the
    finite pixels. Pass the same values for every frame to blink them fairly.
    """
    finite = np.isfinite(data)
    if finite.any():
        lo, hi = np.percentile(data[finite], PERCENTILES)
    else:
        lo, hi = 0.0, 1.0
    lo = lo if vmin is None else vmin
    hi = hi if vmax is None else vmax
    with np.errstate(invalid="ignore"):
        scaled = np.clip((data - lo) / (hi - lo), 0, 1) if hi > lo else np.zeros_like(data)
    stretched = np.arcsinh(scaled / ASINH_SOFTENING) / np.arcsinh(1 / ASINH_SOFTENING)
    gray = np.where(finite, np.round(stretched * 255), 0).astype(np.uint8)
    alpha = np.where(finite, 255, 0).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(np.dstack([gray, alpha])).save(buf, format="PNG")
    return buf.getvalue()
