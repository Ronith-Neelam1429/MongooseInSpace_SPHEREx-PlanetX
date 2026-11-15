"""Images of a patch of sky from older surveys (2MASS, AllWISE), to set beside SPHEREx.

SPHEREx's own passes span about a year, so a star moving a few arcsec per year shifts
by a pixel or two. 2MASS (1997-2001) and WISE (2010) images of the same patch show the
same star decades earlier, where its motion is obvious (Barnard's Star: about 4.5').

Both come from IRSA, the archive SPHEREx uses: the images are found with SIA2
(collections `twomass_allsky` and `wise_allwise`), cut out server-side by IRSA's IBE
(`?center=&size=` on the file URL) and, where one image does not cover the patch,
pieced together from neighbouring images of the same night. Verified live 2026-11-14: SIA answers in
~0.6 s, a 300" cutout in ~0.3 s (2MASS ~0.3 MB, WISE ~0.2 MB gzipped). The CDS
hips2fits service would cover more surveys, but CDS primary hosts were failing.

The images are an extra: every failure (service down, no coverage, a bad file)
becomes a short reason in `Unavailable`, never an exception for the caller, and is
remembered briefly so a dead service is not asked again on every view. Fetches have
their own timeouts, size limits and concurrency cap, separate from the SPHEREx
downloads (`planetx.spherex.archive`), so they cannot hold those up.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import threading
import time
import warnings
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit

import numpy as np
import requests
from astropy.io import fits
from astropy.stats import sigma_clipped_stats
from astropy.time import Time
from astropy.wcs import WCS, FITSFixedWarning
from reproject import reproject_interp

from planetx.config import settings
from planetx.spherex import archive, cutouts

log = logging.getLogger(__name__)

SIA_URL = "https://irsa.ipac.caltech.edu/SIA"
_HOST = "irsa.ipac.caltech.edu"
CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 10
DEADLINE_S = 20  # one request, body included
RETRY_WAITS_S = (0.5, 1.5)  # after a 5xx: 3 attempts in all
MAX_SIA_BYTES = 8 * 2**20  # MAX_SIA_ROWS rows of VOTable are ~1 MB
MAX_CUTOUT_BYTES = 8 * 2**20  # a 900" 2MASS cutout is ~3.3 MB
MAX_FITS_BYTES = 16 * 2**20  # after gunzip (bounds a gzip bomb)
MAX_IMAGE_PX = 601  # side of the rendered image; coarser pixels beyond that
MAX_CONCURRENT = 2  # upstream requests in flight, across all callers
FAILURE_TTL_S = 120.0  # how long a failure is answered from memory
CACHE_MAX_MB = 200
# Most images listed for one patch (all those touching it, plus their auxiliary files).
MAX_SIA_ROWS = 400
MAX_TILES = 3  # images pieced together for one patch
SAME_NIGHT_DAYS = 0.5  # pieces must be from the night of the main image, so the patch has one date

_session = requests.Session()
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
_failures: dict[tuple, tuple[float, str]] = {}
_failures_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="survey-images")


@dataclass(frozen=True)
class Survey:
    key: str
    label: str
    collection: str
    band: str  # SIA energy_bandpassname
    pixel_arcsec: float  # native pixel size
    path: re.Pattern  # allowed IBE file paths of this survey


SURVEYS = (
    Survey(
        "2mass", "2MASS Ks", "twomass_allsky", "K", 1.0,
        re.compile(r"/ibe/data/twomass/allsky/allsky/[0-9a-z]+/[0-9a-z]+/image/[0-9a-z]+\.fits\.gz"),
    ),
    Survey(
        "wise", "AllWISE W1", "wise_allwise", "W1", 1.375,
        re.compile(r"/ibe/data/wise/allwise/p3am_cdd/[0-9]+/[0-9]+/[0-9a-z_]+/[0-9a-z_]+-w1-int-3\.fits"),
    ),
)
SURVEY_KEYS = tuple(s.key for s in SURVEYS)


class Unusable(Exception):
    """This survey cannot give an image here; the message says why, for people."""


@dataclass(frozen=True)
class PatchImage:
    survey: Survey
    data: np.ndarray  # north-up, east-left, row 0 at the north edge; NaN where there is no data
    pixel_arcsec: float
    mjd: float


def validate_url(url: str, survey: Survey) -> str:
    """`url` if it is an image file of `survey` at IRSA, else raise Unusable (fetched server-side: SSRF)."""
    parts = urlsplit(url)
    ok = (
        parts.scheme == "https" and parts.netloc == _HOST and not parts.query and not parts.fragment
        and survey.path.fullmatch(parts.path) is not None and url == f"https://{_HOST}{parts.path}"
    )
    if not ok:
        raise Unusable(f"{survey.label}: the archive listed an unexpected file")
    return url


class _ServerError(Unusable):
    """IRSA answered 5xx: its proxy often does, briefly, so worth one more try."""


def _get(url: str, params: dict, limit: int) -> bytes:
    """GET with timeouts, a size limit and no redirects, retried on 5xx; Unusable on any failure."""
    for wait in RETRY_WAITS_S:
        try:
            return _get_once(url, params, limit)
        except _ServerError:
            time.sleep(wait)
    return _get_once(url, params, limit)


def _get_once(url: str, params: dict, limit: int) -> bytes:
    deadline = time.monotonic() + DEADLINE_S
    if not _slots.acquire(timeout=DEADLINE_S):
        raise Unusable("busy with other image requests")
    try:
        with _session.get(
            url, params=params, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S), stream=True, allow_redirects=False
        ) as resp:
            if resp.status_code >= 500:
                raise _ServerError(f"IRSA answered HTTP {resp.status_code}")
            if resp.status_code != 200:
                raise Unusable(f"IRSA answered HTTP {resp.status_code}")
            chunks, size = [], 0
            for chunk in resp.iter_content(16 * 1024):
                size += len(chunk)
                if size > limit:
                    raise Unusable("the archive sent more than expected")
                if time.monotonic() > deadline:
                    raise Unusable("IRSA did not answer in time")
                chunks.append(chunk)
            return b"".join(chunks)
    except requests.Timeout as exc:
        raise Unusable("IRSA did not answer in time") from exc
    except requests.RequestException as exc:
        raise Unusable("IRSA could not be reached") from exc
    finally:
        _slots.release()


def _offsets_deg(ra0: float, dec0: float, ra: np.ndarray, dec: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(east, north) of positions from (ra0, dec0) in degrees, flat-sky (fine across a survey image)."""
    return ((ra - ra0 + 180) % 360 - 180) * np.cos(np.radians(dec0)), dec - dec0


def coverage(s_region: str, ra: float, dec: float, side_arcsec: float) -> float:
    """Fraction of the square patch (side_arcsec, centred on ra, dec) inside an SIA footprint polygon.

    Sampled on an 11 x 11 grid; 0 for a footprint that cannot be read.
    """
    words = s_region.split()
    try:
        values = [float(w) for w in words[1:] if w.upper() not in ("ICRS", "FK5", "J2000", "UNKNOWNFRAME")]
    except ValueError:
        return 0.0
    if not words or words[0].upper() != "POLYGON" or len(values) < 6 or len(values) % 2:
        return 0.0
    px, py = _offsets_deg(ra, dec, np.array(values[0::2]), np.array(values[1::2]))
    half = side_arcsec / 7200
    gx, gy = np.meshgrid(np.linspace(-half, half, 11), np.linspace(-half, half, 11))
    gx, gy = gx.ravel(), gy.ravel()
    inside = np.zeros(gx.shape, dtype=bool)
    for i in range(len(px)):  # even-odd ray casting
        x1, y1, x2, y2 = px[i - 1], py[i - 1], px[i], py[i]
        crosses = (y1 > gy) != (y2 > gy)
        with np.errstate(divide="ignore", invalid="ignore"):
            at = x1 + (gy - y1) * (x2 - x1) / (y2 - y1)
        inside ^= crosses & (gx < at)
    return float(inside.mean())


def find_images(survey: Survey, ra: float, dec: float, side_arcsec: float) -> list[str]:
    """URLs of the survey images touching the square patch, those covering most of it first.

    2MASS images abut rather than overlap (8.5' x 17' each), so a patch around a fast star's
    old and new positions often needs two of them.
    """
    radius = side_arcsec / 2 * 2**0.5 / 3600  # through the patch corners
    body = _get(SIA_URL, {
        "COLLECTION": survey.collection, "POS": f"CIRCLE {ra:.7f} {dec:.7f} {radius:.5f}", "MAXREC": MAX_SIA_ROWS,
    }, MAX_SIA_BYTES)
    try:
        table = archive.parse_sia_votable(body)
    except archive.UpstreamError as exc:
        raise Unusable("the image search failed") from exc
    ranked = []
    for row in table:
        if str(row["dataproduct_subtype"]) != "science" or str(row["energy_bandpassname"]) != survey.band:
            continue
        east, north = _offsets_deg(ra, dec, np.array(float(row["s_ra"])), np.array(float(row["s_dec"])))
        share = coverage(str(row["s_region"]), ra, dec, side_arcsec)
        ranked.append((-share, float(np.hypot(east, north)), str(row["access_url"])))
    ranked.sort()
    useful = [url for share, _, url in ranked if share < 0] or [url for _, _, url in ranked[:1]]
    if not useful:
        raise Unusable(f"{survey.label} has no image here")
    return [validate_url(url, survey) for url in useful[:MAX_TILES]]


def _gunzip(body: bytes) -> bytes:
    """`body` unzipped if it is gzip (IBE sends WISE cutouts gzipped), within MAX_FITS_BYTES."""
    if body[:2] != b"\x1f\x8b":
        return body
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = inflater.decompress(body, MAX_FITS_BYTES + 1)
    except zlib.error as exc:
        raise Unusable("the archive sent a damaged file") from exc
    if len(out) > MAX_FITS_BYTES or inflater.unconsumed_tail:
        raise Unusable("the archive sent more than expected")
    return out


def observed_mjd(header: fits.Header, survey: Survey) -> float:
    """Date of the image: 2MASS gives the night (ORDATE yymmdd) and UT; AllWISE the mean (MIDOBS)."""
    try:
        if survey.key == "2mass":
            night = str(header["ORDATE"]).strip()
            yy = int(night[:2])  # 2MASS observed 1997-2001
            year = 1900 + yy if yy >= 90 else 2000 + yy
            ut = str(header.get("UT", "00:00:00")).strip()
            return float(Time(f"{year}-{night[2:4]}-{night[4:6]}T{ut}", scale="utc").mjd)
        return float(Time(str(header["MIDOBS"]).strip(), scale="utc").mjd)
    except (KeyError, ValueError) as exc:
        raise Unusable(f"{survey.label}: the image has no date") from exc


def _image_hdu(body: bytes, survey: Survey) -> tuple[np.ndarray, fits.Header]:
    try:
        with fits.open(io.BytesIO(body), memmap=False) as hdul:
            hdu = hdul[0]
            if hdu.data is None or hdu.data.ndim != 2:
                raise Unusable(f"{survey.label}: the archive sent no image")
            return np.asarray(hdu.data, dtype=np.float64), hdu.header.copy()
    except (OSError, ValueError, TypeError) as exc:
        raise Unusable(f"{survey.label}: the archive sent a damaged file") from exc


def patch_grid(ra: float, dec: float, side_arcsec: float, pixel_arcsec: float) -> tuple[WCS, int]:
    """North-up, east-left TAN grid of odd side centred on (ra, dec), as the SPHEREx evidence patch."""
    n = int(np.ceil(side_arcsec / pixel_arcsec)) | 1
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    wcs.wcs.crval = [ra, dec]
    wcs.wcs.crpix = [(n + 1) / 2, (n + 1) / 2]
    wcs.wcs.cdelt = [-pixel_arcsec / 3600, -pixel_arcsec / 3600]
    return wcs, n


def _resample(survey: Survey, data: np.ndarray, header: fits.Header, grid: WCS, n: int) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        try:
            # Nearest neighbour, as the SPHEREx images: measured values only, nothing smoothed in.
            return reproject_interp(
                (data, WCS(header)), grid, shape_out=(n, n), order="nearest-neighbor", return_footprint=False
            )
        except ValueError as exc:
            raise Unusable(f"{survey.label}: the image has no usable sky coordinates") from exc


def _cache_path(survey: Survey, ra: float, dec: float, side_arcsec: float):
    digest = hashlib.sha256(f"{survey.key}|{ra:.6f}|{dec:.6f}|{side_arcsec:.1f}".encode()).hexdigest()
    return settings.cache_dir / "surveys" / digest[:2] / f"{digest}.fits"


def _from_cache(survey: Survey, path) -> PatchImage | None:
    try:
        with fits.open(path, memmap=False) as hdul:
            header = hdul[0].header
            return PatchImage(
                survey=survey, data=np.asarray(hdul[0].data, dtype=np.float64),
                pixel_arcsec=float(header["PIXSCALE"]), mjd=float(header["MJD-OBS"]),
            )
    except FileNotFoundError:
        return None
    except (OSError, KeyError, ValueError, TypeError):
        log.warning("unreadable survey cache entry %s; fetching again", path)
        return None


def patch_image(survey: Survey, ra: float, dec: float, side_arcsec: float) -> PatchImage:
    """The survey's image of the square patch centred on (ra, dec), north-up; raises Unusable.

    Pieced together from the images covering most of the patch, taken on the same night as
    the first (so the image has one date); pixels none of them covers are NaN. Cached on disk.
    """
    path = _cache_path(survey, ra, dec, side_arcsec)
    cached = _from_cache(survey, path)
    if cached is not None:
        cutouts._touch(path)
        return cached
    pixel = max(survey.pixel_arcsec, side_arcsec / MAX_IMAGE_PX)
    grid, n = patch_grid(ra, dec, side_arcsec, pixel)
    out, mjd = np.full((n, n), np.nan), None
    for url in find_images(survey, ra, dec, side_arcsec):
        try:
            body = _gunzip(_get(url, {
                "center": f"{ra:.7f},{dec:.7f}", "size": f"{side_arcsec:.1f}arcsec", "gzip": "false",
            }, MAX_CUTOUT_BYTES))
            data, header = _image_hdu(body, survey)
            taken = observed_mjd(header, survey)
            if mjd is not None and abs(taken - mjd) > SAME_NIGHT_DAYS:
                continue
            piece = _resample(survey, data, header, grid, n)
        except Unusable:
            if mjd is None:
                raise  # the main image is needed; a piece of the edge is not
            continue
        finite = np.isfinite(piece)
        if not finite.any():
            continue
        # Each image has its own sky level (2MASS images are in raw counts): pieces are put on a
        # common zero so their seam does not look like a feature.
        piece = piece - sigma_clipped_stats(piece[finite], sigma=3.0, maxiters=5)[1]
        mjd = taken if mjd is None else mjd
        fill = ~np.isfinite(out)
        out[fill] = piece[fill]
        if np.isfinite(out).all():
            break
    if mjd is None:
        raise Unusable(f"{survey.label} has no image here")
    header = fits.Header({"MJD-OBS": mjd, "PIXSCALE": pixel})
    buf = io.BytesIO()
    fits.PrimaryHDU(out.astype(np.float32), header).writeto(buf)
    # Shares the cutout cache's crash-safe write and least-recently-used eviction.
    cutouts._atomic_write(path, buf.getvalue())
    cutouts._evict(path.parent.parent, CACHE_MAX_MB * 2**20, keep=path)
    return PatchImage(survey=survey, data=out, pixel_arcsec=pixel, mjd=mjd)


def _remembered(key: tuple) -> str | None:
    with _failures_lock:
        hit = _failures.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        _failures.pop(key, None)
        return None


def _try(survey: Survey, ra: float, dec: float, side_arcsec: float) -> PatchImage | str:
    key = (survey.key, round(ra, 6), round(dec, 6), round(side_arcsec, 1))
    reason = _remembered(key)
    if reason is not None:
        return reason
    try:
        return patch_image(survey, ra, dec, side_arcsec)
    except Unusable as exc:
        reason = str(exc)
    except Exception:  # an extra must never break the evidence: log it, report it as missing
        log.exception("%s image at ra=%s dec=%s failed", survey.key, ra, dec)
        reason = f"{survey.label}: the image could not be prepared"
    with _failures_lock:
        _failures[key] = (time.monotonic() + FAILURE_TTL_S, reason)
    return reason


def patch_images(ra: float, dec: float, side_arcsec: float) -> list[PatchImage | str]:
    """Every survey's image of the patch (in SURVEYS order), or why there is none: never raises."""
    futures = [_pool.submit(_try, s, ra, dec, side_arcsec) for s in SURVEYS]
    out = []
    for survey, future in zip(SURVEYS, futures, strict=True):
        try:
            # Each request has its own deadline; this bounds waiting for a free slot as well.
            out.append(future.result(timeout=3 * DEADLINE_S))
        except TimeoutError:
            out.append(f"{survey.label}: IRSA did not answer in time")
    return out
