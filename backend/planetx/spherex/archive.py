"""IRSA access for SPHEREx: SIA2 visit queries and MEF cutout downloads.

Facts verified against the live service (Nov 2026):
  * SIA2 at https://irsa.ipac.caltech.edu/SIA returns a VOTable. Errors come back
    as HTTP 200 with ``<INFO name="QUERY_STATUS" value="ERROR">``; truncation at
    MAXREC is NOT signalled, so a full page is treated as possibly truncated.
  * Columns used: obs_id, obs_collection, energy_bandpassname ("SPHEREx-D6"),
    t_min/t_max (MJD), em_min/em_max (metres; the detector's whole band, not
    the band at the target), access_url. obs_id is shared by the detectors of
    one exposure, so (obs_id, detector) and access_url are the unique keys.
  * spherex_dr1 and spherex_qr3 share no frames, so their rows can be merged.
  * Cutouts: ``<access_url>?center=RA,Dec&size=DEG`` (size in degrees). A
    position outside the file answers 422; IRSA answers 502/503 at random and
    sometimes HTTP 200 with a non-FITS "busy" body.

The SIA query is made with plain requests rather than pyvo/astroquery because
those have no timeout and can hang a request forever. Every call here has a
connect/read timeout, a total deadline, a body size limit, bounded retries
(transient failures only) and a cap on concurrent requests to IRSA.
"""
import io
import logging
import re
import threading
import time
import warnings
from collections.abc import Callable
from urllib.parse import urlsplit

import numpy as np
import requests
from astropy.io import fits
from astropy.io.votable import parse as parse_votable
from astropy.io.votable.exceptions import VOWarning
from astropy.table import Table
from astropy.time import Time
from astropy.wcs import WCS

from planetx.api.schemas import Pass, Visit

log = logging.getLogger(__name__)

SIA_URL = "https://irsa.ipac.caltech.edu/SIA"
COLLECTIONS = ("spherex_dr1", "spherex_qr3")
SIA_MAXREC = 10000

CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 30  # max silence between bytes
REQUEST_DEADLINE_S = 60  # max total time of one attempt, body included
TOTAL_DEADLINE_S = 120  # max total time of one call: waiting for a slot, all attempts, back-off
MAX_BODY_BYTES = 64 * 2**20  # cutouts are ~4-6 MB; full files (hundreds of MB) are refused
RETRY_BACKOFF_S = (1.0, 3.0)  # sleeps between attempts: 3 attempts in all
TRANSIENT_STATUS = frozenset({408, 429})  # 4xx worth retrying (timeout, throttling); plus all 5xx
MAX_CONCURRENT_REQUESTS = 4
PASS_GAP_DAYS = 30
SIA_CACHE_TTL_S = 600
SIA_CACHE_MAX_ENTRIES = 256
# Words that may follow POLYGON in an STC-S s_region before the vertices (IRSA sends ICRS).
S_REGION_FRAMES = frozenset({"ICRS", "FK5", "J2000", "UNKNOWNFRAME", "GEOCENTER", "BARYCENTER", "TOPOCENTER",
                             "UNKNOWNREFPOS", "SPHERICAL2"})

_ALLOWED_NETLOC = "irsa.ipac.caltech.edu"
# SPHEREx file paths contain only these characters; anything else (%, \, ?, ;) is refused.
_ALLOWED_PATH = re.compile(r"/ibe/data/spherex/[A-Za-z0-9._/-]+\.fits")
_DETECTOR = re.compile(r"-D([1-6])$")

_session = requests.Session()
_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
_sia_cache: dict[tuple, tuple[float, list[Visit]]] = {}
_sia_cache_lock = threading.Lock()


class UpstreamError(Exception):
    """IRSA failed or answered with something unusable.

    ``status`` is the HTTP status if there was a response; ``transient`` says
    whether retrying could help (5xx, timeouts, connection errors, bad bodies).
    """

    def __init__(self, message: str, status: int | None = None, transient: bool = False):
        super().__init__(message)
        self.status = status
        self.transient = transient


class IrsaBusy(UpstreamError):
    """Our own limit on concurrent IRSA requests was reached; the client should retry later."""


def validate_spherex_url(url: str) -> str:
    """Return ``url`` if it is a SPHEREx file at IRSA, else raise ValueError.

    The URL comes from users and is fetched server-side, so it is checked
    exactly (SSRF): https only, host exactly irsa.ipac.caltech.edu (no port,
    userinfo or trailing dot), path under /ibe/data/spherex/ ending in .fits,
    no query/fragment, no '.'/'..' or empty path segments.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != _ALLOWED_NETLOC:
        raise ValueError("url must be an https://irsa.ipac.caltech.edu SPHEREx file URL")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise ValueError("url must not have a query string or fragment")
    if not _ALLOWED_PATH.fullmatch(parts.path):
        raise ValueError("url path must be a .fits file under /ibe/data/spherex/")
    if any(seg in ("", ".", "..") for seg in parts.path.split("/")[1:]):
        raise ValueError("url path must not contain empty, '.' or '..' segments")
    # urlsplit silently drops tabs/newlines; require the URL to be exactly what was checked.
    if url != f"https://{_ALLOWED_NETLOC}{parts.path}":
        raise ValueError("url contains characters that are not allowed")
    return url


def _read_body(resp: requests.Response, deadline: float) -> bytes:
    chunks, size = [], 0
    # Small chunks so the deadline is checked often even on a slow trickle.
    for chunk in resp.iter_content(8 * 1024):
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise UpstreamError(f"response larger than {MAX_BODY_BYTES // 2**20} MB")
        if time.monotonic() > deadline:
            raise UpstreamError("response not complete before the deadline", transient=True)
        chunks.append(chunk)
    return b"".join(chunks)


def _get_once(url: str, params: dict, call_deadline: float) -> bytes:
    if not _slots.acquire(timeout=max(0.0, call_deadline - time.monotonic())):
        raise IrsaBusy("too many requests to IRSA in flight; try again shortly")
    try:
        deadline = min(time.monotonic() + REQUEST_DEADLINE_S, call_deadline)
        # Redirects are not followed: a redirect could point anywhere (SSRF).
        with _session.get(
            url, params=params, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
            stream=True, allow_redirects=False,
        ) as resp:
            body = _read_body(resp, deadline)
            code = resp.status_code
    except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
        raise UpstreamError(f"{type(exc).__name__}: {exc}", transient=True) from exc
    except requests.RequestException as exc:
        raise UpstreamError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        _slots.release()
    if code != 200:
        snippet = body[:200].decode("utf-8", "replace").strip()
        transient = code >= 500 or code in TRANSIENT_STATUS
        raise UpstreamError(f"HTTP {code}: {snippet}", status=code, transient=transient)
    return body


def _get[T](url: str, params: dict, what: str, parse: Callable[[bytes], T]) -> T:
    """GET and parse, retrying transient failures with bounded back-off.

    ``parse`` raises UpstreamError(transient=True) for a malformed 200 body.
    4xx and other non-transient errors are raised at once. The whole call,
    slot waits and back-off included, ends by TOTAL_DEADLINE_S.
    """
    call_deadline = time.monotonic() + TOTAL_DEADLINE_S
    for attempt in range(1, len(RETRY_BACKOFF_S) + 2):
        try:
            return parse(_get_once(url, params, call_deadline))
        except IrsaBusy:
            raise
        except UpstreamError as exc:
            last = attempt > len(RETRY_BACKOFF_S)
            if not exc.transient or last or time.monotonic() + RETRY_BACKOFF_S[attempt - 1] >= call_deadline:
                raise UpstreamError(
                    f"{what} failed after {attempt} attempt(s): {exc}", exc.status, exc.transient
                ) from exc
            log.warning("%s attempt %d failed (%s); retrying", what, attempt, exc)
            time.sleep(RETRY_BACKOFF_S[attempt - 1])
    raise AssertionError("unreachable")


# --- SIA -----------------------------------------------------------------------------


def parse_sia_votable(body: bytes) -> Table:
    """Parse an SIA2 VOTable response; raise UpstreamError on a service error."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", VOWarning)
            vot = parse_votable(io.BytesIO(body))
    except Exception as exc:  # astropy raises several types for non-XML bodies
        raise UpstreamError(f"response is not a VOTable (starts {body[:60]!r})", transient=True) from exc
    for resource in vot.resources:
        for info in resource.infos:
            if info.name == "QUERY_STATUS" and info.value == "ERROR":
                raise UpstreamError(f"SIA error: {(info.content or '').strip()}")
    tables = list(vot.iter_tables())
    if not tables:
        raise UpstreamError("SIA response has no result table")
    return tables[0].to_table(use_names_over_ids=True)


def _float(value) -> float | None:
    if np.ma.is_masked(value):
        return None
    value = float(value)
    return value if np.isfinite(value) else None


def visits_from_table(table: Table) -> list[Visit]:
    """Convert SIA rows to Visits (wavelengths m -> um, mjd = exposure midpoint)."""
    visits = []
    for row in table:
        t_min, t_max = _float(row["t_min"]), _float(row["t_max"])
        if t_min is None:
            raise UpstreamError(f"SIA row {row['obs_id']} has no t_min")
        mjd = (t_min + t_max) / 2 if t_max is not None else t_min
        em_min, em_max = _float(row["em_min"]), _float(row["em_max"])
        match = _DETECTOR.search(str(row["energy_bandpassname"]))
        visits.append(Visit(
            obs_id=str(row["obs_id"]),
            collection=str(row["obs_collection"]),
            detector=int(match.group(1)) if match else None,
            mjd=mjd,
            date=mjd_to_date(mjd),
            wavelength_min_um=round(em_min * 1e6, 6) if em_min is not None else None,
            wavelength_max_um=round(em_max * 1e6, 6) if em_max is not None else None,
            access_url=str(row["access_url"]),
        ))
    return visits


def mjd_to_date(mjd: float) -> str:
    """UTC calendar date (YYYY-MM-DD) of an MJD."""
    return Time(mjd, format="mjd", scale="utc").iso[:10]


def parse_s_region(text: str) -> np.ndarray:
    """Vertices (n, 2: ra, dec in degrees) of an SIA ``s_region`` such as
    ``POLYGON ICRS ra1 dec1 ra2 dec2 ...``; a closing vertex repeating the first is dropped.

    SPHEREx footprints are convex quadrilaterals (verified live). Raises
    UpstreamError for anything else IRSA might send.
    """
    words = str(text).split()
    if not words or words[0].upper() != "POLYGON":
        raise UpstreamError(f"SIA s_region is not a POLYGON: {str(text)[:80]!r}")
    numbers = words[1:]
    while numbers and numbers[0].upper() in S_REGION_FRAMES:  # STC-S frame, reference position, ...
        numbers = numbers[1:]
    try:
        values = np.array([float(w) for w in numbers])
    except ValueError:
        raise UpstreamError(f"SIA s_region has a non-numeric vertex: {str(text)[:80]!r}") from None
    if len(values) % 2 or not np.isfinite(values).all() or np.abs(values[1::2]).max(initial=0) > 90:
        raise UpstreamError(f"SIA s_region has malformed vertices: {str(text)[:80]!r}")
    vertices = values.reshape(-1, 2)
    if len(vertices) > 3 and np.allclose(vertices[0], vertices[-1]):
        vertices = vertices[:-1]
    if len(vertices) < 3:
        raise UpstreamError(f"SIA s_region has fewer than 3 vertices: {str(text)[:80]!r}")
    return vertices


def _query_table(collection: str, ra: float, dec: float, radius_deg: float) -> Table:
    pos = f"CIRCLE {ra:.7f} {dec:.7f} {radius_deg:.7f}"
    params = {"COLLECTION": collection, "POS": pos, "MAXREC": SIA_MAXREC}
    table = _get(SIA_URL, params, f"SIA query {collection}", parse_sia_votable)
    if len(table) >= SIA_MAXREC:
        raise UpstreamError(
            f"SIA query {collection} hit the {SIA_MAXREC}-row limit; results would be "
            "truncated, use a smaller radius"
        )
    return table


def _query_collection(collection: str, ra: float, dec: float, radius_deg: float) -> list[Visit]:
    return visits_from_table(_query_table(collection, ra, dec, radius_deg))


def query_footprints(ra: float, dec: float, radius_arcsec: float) -> list[tuple[Visit, np.ndarray]]:
    """Like `query_visits` (same rows, order and errors) with each image's footprint
    (`parse_s_region` vertices), and not cached.

    A row whose footprint is missing or unreadable is left out (logged), so one bad
    row cannot fail a survey patch on every retry; a collection where every row's
    footprint is unreadable raises (the format must have changed).
    """
    found: list[tuple[Visit, np.ndarray]] = []
    seen: set[str] = set()
    for collection in COLLECTIONS:
        table = _query_table(collection, ra, dec, radius_arcsec / 3600)
        if len(table) and "s_region" not in table.colnames:
            raise UpstreamError(f"SIA query {collection} returned no s_region column")
        bad: list[str] = []
        for visit, region in zip(visits_from_table(table), table["s_region"] if len(table) else [], strict=True):
            try:
                if np.ma.is_masked(region):
                    raise UpstreamError("SIA s_region is empty")
                footprint = parse_s_region(region)
            except UpstreamError as exc:
                bad.append(f"{visit.access_url}: {exc}")
                continue
            if visit.access_url not in seen:
                seen.add(visit.access_url)
                found.append((visit, footprint))
        if bad and len(bad) == len(table):
            raise UpstreamError(f"SIA query {collection}: no row has a readable s_region, e.g. {bad[0]}")
        if bad:
            log.warning("SIA query %s at ra=%s dec=%s: left out %d of %d rows with unreadable footprints, e.g. %s",
                        collection, ra, dec, len(bad), len(table), bad[0])
    found.sort(key=lambda vf: (vf[0].mjd, vf[0].detector or 0, vf[0].access_url))
    return found


def query_visits(ra: float, dec: float, radius_arcsec: float) -> list[Visit]:
    """All SPHEREx visits (dr1 + qr3) whose images cover the cone, sorted by mjd.

    Raises UpstreamError if either collection fails (no partial results).
    Results are cached in memory for SIA_CACHE_TTL_S.
    """
    key = (round(ra, 6), round(dec, 6), round(radius_arcsec, 3))
    now = time.monotonic()
    with _sia_cache_lock:
        hit = _sia_cache.get(key)
        if hit and now - hit[0] < SIA_CACHE_TTL_S:
            return list(hit[1])
    visits, seen = [], set()
    for collection in COLLECTIONS:
        for visit in _query_collection(collection, ra, dec, radius_arcsec / 3600):
            if visit.access_url not in seen:
                seen.add(visit.access_url)
                visits.append(visit)
    visits.sort(key=lambda v: (v.mjd, v.detector or 0, v.access_url))
    with _sia_cache_lock:
        if len(_sia_cache) >= SIA_CACHE_MAX_ENTRIES:
            del _sia_cache[min(_sia_cache, key=lambda k: _sia_cache[k][0])]
        _sia_cache[key] = (now, visits)
    return list(visits)


def group_passes(visits: list[Visit], gap_days: float = PASS_GAP_DAYS) -> list[Pass]:
    """Group visits (sorted by mjd) into survey passes split at gaps > gap_days."""
    groups: list[list[Visit]] = []
    for visit in visits:
        if not groups or visit.mjd - groups[-1][-1].mjd > gap_days:
            groups.append([])
        groups[-1].append(visit)
    return [Pass(start=g[0].date, end=g[-1].date, n_visits=len(g)) for g in groups]


# --- Cutouts -------------------------------------------------------------------------


def check_cutout_fits(body: bytes) -> bytes:
    """Return ``body`` if it is a complete SPHEREx MEF with the HDUs the app uses."""
    if not body.startswith(b"SIMPLE"):
        raise UpstreamError(f"HTTP 200 but not FITS (starts {body[:40]!r})", transient=True)
    if len(body) % 2880:  # every FITS HDU is padded to 2880-byte blocks
        raise UpstreamError(f"truncated FITS ({len(body)} bytes)", transient=True)
    try:
        with warnings.catch_warnings():
            # astropy only warns about a short file; that is checked explicitly below.
            warnings.simplefilter("ignore")
            hdul = fits.open(io.BytesIO(body))
            hdul.readall()
            last = hdul.fileinfo(len(hdul) - 1)
    except Exception as exc:  # corrupt FITS raises several types
        raise UpstreamError(f"unreadable FITS ({type(exc).__name__}: {exc})", transient=True) from exc
    with hdul:
        if last["datLoc"] + last["datSpan"] > len(body):
            raise UpstreamError(f"truncated FITS ({len(body)} bytes)", transient=True)
        _check_cutout_layout({hdu.name: hdu for hdu in hdul})
    return body


def _check_cutout_layout(hdus: dict) -> None:
    missing = {"IMAGE", "FLAGS", "WCS-WAVE"} - hdus.keys()
    if missing:
        raise UpstreamError(f"cutout lacks HDUs {sorted(missing)}; has {sorted(hdus)}")
    image, flags = hdus["IMAGE"], hdus["FLAGS"]
    if image.data is None or flags.data is None or image.data.ndim != 2 or image.data.shape != flags.data.shape:
        raise UpstreamError("cutout IMAGE/FLAGS are missing or not matching 2-D arrays")
    if "MJD-AVG" not in image.header:
        raise UpstreamError("cutout IMAGE header has no MJD-AVG")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            celestial = WCS(image.header).has_celestial
    except Exception as exc:  # astropy raises several types for a broken WCS
        raise UpstreamError(f"cutout IMAGE has an invalid WCS ({exc})") from exc
    if not celestial:
        raise UpstreamError("cutout IMAGE has no celestial WCS")


def fetch_cutout(url: str, ra: float, dec: float, size_deg: float) -> bytes:
    """Download and validate a SPHEREx MEF cutout of side ``size_deg`` at (ra, dec)."""
    validate_spherex_url(url)
    params = {"center": f"{ra:.7f},{dec:.7f}", "size": f"{size_deg:.5f}"}
    return _get(url, params, "cutout download", check_cutout_fits)
