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

Disk cache: settings.cache_dir/cutouts, ~4 MB per entry, capped at
settings.cache_max_mb. A hit refreshes the entry's mtime; after each new download
the least recently used entries are deleted until the cache fits the cap.
"""
import base64
import hashlib
import io
import os
import re
import tempfile
import threading
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning
from PIL import Image
from reproject import reproject_interp

from planetx.spherex import archive
from planetx.config import settings

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
# Striped locks: concurrent requests for the same cutout download it once.
_download_locks = [threading.Lock() for _ in range(64)]
_evict_lock = threading.Lock()


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


def cutout_request(ra: float, dec: float, size_deg: float) -> tuple[float, float, float]:
    """(ra, dec, size) of the raw cutout to request from IRSA for an output grid."""
    step = CUTOUT_CENTER_STEP_DEG
    snapped_ra = round(round(ra / step) * step % 360, 7)
    snapped_dec = round(min(90.0, max(-90.0, round(dec / step) * step)), 7)
    return snapped_ra, snapped_dec, round(size_deg * CUTOUT_OVERSIZE + CUTOUT_MARGIN_DEG, 5)


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


def cached_cutout(url: str, ra: float, dec: float, size_deg: float) -> Path:
    """Path of the raw MEF cutout (downloading and caching it if needed).

    Keyed by (url, centre, size) as sent to IRSA. Only validated FITS is cached.
    """
    archive.validate_spherex_url(url)
    ra, dec, size_deg = round(ra, 7), round(dec, 7), round(size_deg, 5)
    digest = hashlib.sha256(f"{url}|{ra:.7f}|{dec:.7f}|{size_deg:.5f}".encode()).hexdigest()
    path = settings.cache_dir / "cutouts" / digest[:2] / f"{digest}.fits"
    lock = _download_locks[int(digest[:8], 16) % len(_download_locks)]
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


def _touch(path: Path) -> None:
    """Mark a cache entry as recently used (mtime drives eviction order)."""
    try:
        os.utime(path)
    except FileNotFoundError:
        pass  # evicted concurrently; the caller's open retries via aligned_cutout


def _evict(root: Path, max_bytes: int, keep: Path) -> None:
    """Delete least recently used cutouts until the cache is at most max_bytes.

    `keep` (the entry just written) is never deleted, so a single cutout larger
    than the cap still works. Entries vanishing mid-scan (another thread or
    process evicting) are skipped.
    """
    with _evict_lock:
        entries = []
        for p in root.rglob("*.fits"):
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            entries.append((st.st_mtime, st.st_size, p))
        total = sum(size for _, size, _ in entries)
        for _, size, p in sorted(entries, key=lambda e: e[0]):
            if total <= max_bytes:
                break
            if p == keep:
                continue
            p.unlink(missing_ok=True)
            total -= size


def _wavelength_wcs(header: fits.Header, hdul: fits.HDUList) -> WCS:
    """The per-pixel (wavelength, bandwidth) lookup WCS (alternate key W).

    Its table is indexed by raw pixel coordinates, so the celestial SIP
    distortion cards are dropped first (astropy would otherwise apply them).
    """
    header = header.copy()
    for key in [k for k in header if _SIP_CARD.fullmatch(k)]:
        del header[key]
    return WCS(header, fobj=hdul, key="W")


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
