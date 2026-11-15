"""Where SPHEREx was: its geocentric position from JPL Horizons, and the parallax maths that uses it.

SkyBoT, the asteroid search the classifier uses, predicts positions only for
observatory codes (MPC/IAU). SPHEREx is in a ~650 km polar orbit, about 7,000 km
from the Earth's centre, so a body d au away appears up to ~9.7"/d from its
geocentric (code 500) position: 24" for (433) Eros at 0.40 au, minutes of arc
for a near-Earth object at 0.02 au. A known NEO can then look "unknown".

The fix used here: take SkyBoT's geocentric position and distance, and move the
body to where SPHEREx saw it, using SPHEREx's own geocentric position from
Horizons (`spherex_geocentric_km`). When Horizons cannot answer, the caller
falls back to the geocentric position with the match radius widened by the
largest possible parallax (`parallax_arcsec`).

Verified live on 2026-11-14:
  * Horizons has SPHEREx as spacecraft -163182 ("SPHEREx Spacecraft", trajectory
    from concatenated Space-Track TLEs, 2025-Mar-12 to 30 days ahead). A VECTORS
    query with CENTER='500@399' and REF_PLANE=FRAME gives ICRF geocentric
    positions of 6,960-7,080 km. Times before launch give an `error` in the JSON.
  * Horizons also takes '@-163182' as the observer of an ephemeris: Eros on
    2025-11-21 0h UT is 13.8" east and 19.9" north of its geocentric position.
  * SkyBoT's observer parameter (`-observer`) takes only codes. "@-163182" is
    "unknown code for the observer location"; MPC code 288 (SPHEREx) is accepted
    but answers exactly as 500 (geocentric), since it has no fixed site.
  * SPHEREx frame times (MJD-AVG) are UTC (TIMESYS 'UTC'), as Horizons' TIME_TYPE=UT.
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from planetx.config import settings

from . import catalogs
from .catalogs import CatalogError

log = logging.getLogger(__name__)

HORIZONS_URL = "https://ssd.jpl.nasa.gov/api/horizons.api"
SPHEREX_HORIZONS_ID = "-163182"
HORIZONS_TIMEOUT_S = 60.0
# Horizons allows 10,000 times per TLIST, but every time lengthens the URL.
MAX_EPOCHS_PER_CALL = 100
AU_KM = 149_597_870.7
# SPHEREx's distance from the Earth's centre is 6,960-7,080 km (Horizons). This
# bound gives the widest parallax when its actual position is not known.
SPHEREX_MAX_GEOCENTRIC_KM = 7_100.0
# Anything outside this range of geocentric distance is not SPHEREx in low Earth orbit.
_SANE_GEOCENTRIC_KM = (6_378.0, 8_000.0)
ARCSEC_PER_RAD = 180.0 / math.pi * 3600.0
MJD_TO_JD = 2400000.5
# Cache key precision: 1e-8 day is ~1 ms, in which SPHEREx moves ~8 m.
_KEY_FORMAT = "{:.8f}"
CACHE_FILE_MODE = 0o644

_cache_lock = threading.Lock()


def parallax_arcsec(baseline_km: float, distance_au: float) -> float:
    """Largest angle (arcsec) between two views of a body `distance_au` away from points `baseline_km` apart."""
    if not (baseline_km >= 0 and distance_au > 0):
        raise ValueError(f"need baseline >= 0 and distance > 0, got {baseline_km} km, {distance_au} au")
    return math.asin(min(baseline_km / (distance_au * AU_KM), 1.0)) * ARCSEC_PER_RAD


def unit_vector(ra: float, dec: float) -> np.ndarray:
    a, d = math.radians(ra), math.radians(dec)
    return np.array([math.cos(d) * math.cos(a), math.cos(d) * math.sin(a), math.sin(d)])


def from_observer(ra: float, dec: float, distance_au: float, observer_km: Sequence[float]) -> tuple[float, float]:
    """(ra, dec) in degrees of a body at geocentric (ra, dec, distance) as seen from `observer_km`.

    `observer_km` is the observer's geocentric ICRF position. Exact vector
    subtraction, so it holds for near-Earth objects too. The light-time
    difference between the two places (< 0.03 s) is left out.
    """
    if not distance_au > 0:
        raise ValueError(f"distance must be positive, got {distance_au}")
    seen = unit_vector(ra, dec) * distance_au * AU_KM - np.asarray(observer_km, dtype=float)
    x, y, z = seen / np.linalg.norm(seen)
    return float(math.degrees(math.atan2(y, x)) % 360.0), float(math.degrees(math.asin(max(-1.0, min(1.0, z)))))


def mean_position(
    positions: Sequence[Sequence[float]], weights: Sequence[float] | None = None
) -> tuple[np.ndarray, float]:
    """(weighted mean position, largest distance of any position from it) in km.

    A pass position is the weighted mean of its detections (weights 1/sigma^2 in
    the engine, and the pass time uses the same weights), and a body's shift is
    linear in the observer's position to first order: the observer positions
    averaged with those weights give the shift of the pass position. The spread
    bounds how far any one frame's shift can differ from it.
    """
    pts = np.asarray(positions, dtype=float).reshape(-1, 3)
    if len(pts) == 0:
        raise ValueError("no positions")
    w = np.ones(len(pts)) if weights is None else np.asarray(weights, dtype=float)
    if w.shape != (len(pts),) or not (np.all(np.isfinite(w)) and np.all(w >= 0) and w.sum() > 0):
        raise ValueError(f"need one finite, non-negative weight per position with a positive sum, got {weights}")
    mean = (w[:, None] * pts).sum(axis=0) / w.sum()
    return mean, float(np.linalg.norm(pts - mean, axis=1).max())


# --- Horizons -------------------------------------------------------------------


def parse_horizons_vectors(data: dict, n_expected: int) -> list[tuple[float, float, float, float]]:
    """(jd_ut, x, y, z) rows from a Horizons VECTORS answer (format=json, CSV_FORMAT=YES), in km."""
    if data.get("error"):
        raise CatalogError(f"Horizons error: {str(data['error']).strip()[:200]}")
    text = data.get("result")
    if not isinstance(text, str) or "$$SOE" not in text or "$$EOE" not in text:
        raise CatalogError(f"unexpected Horizons response: {str(data)[:120]!r}")
    body = text[text.index("$$SOE") + 5:text.index("$$EOE")]
    rows = []
    for line in body.strip().splitlines():
        cells = [c.strip() for c in line.split(",")]
        try:
            jd, x, y, z = float(cells[0]), float(cells[2]), float(cells[3]), float(cells[4])
        except (IndexError, ValueError) as exc:
            raise CatalogError(f"unexpected Horizons row: {line.strip()[:120]!r}") from exc
        r = math.sqrt(x * x + y * y + z * z)
        if not _SANE_GEOCENTRIC_KM[0] <= r <= _SANE_GEOCENTRIC_KM[1]:
            raise CatalogError(f"Horizons gave SPHEREx {r:.0f} km from the Earth's centre: not its orbit")
        rows.append((jd, x, y, z))
    if len(rows) != n_expected:
        raise CatalogError(f"Horizons returned {len(rows)} positions for {n_expected} times")
    return rows


def _fetch_horizons(mjds: Sequence[float]) -> dict[str, list[float]]:
    """One Horizons request for SPHEREx's geocentric position at each UTC time. Raises CatalogError."""
    tlist = " ".join(f"'{m + MJD_TO_JD:.8f}'" for m in mjds)
    resp = catalogs._send("GET", HORIZONS_URL, timeout=HORIZONS_TIMEOUT_S, what="Horizons", params={
        "format": "json",
        "COMMAND": f"'{SPHEREX_HORIZONS_ID}'",
        "OBJ_DATA": "NO",
        "MAKE_EPHEM": "YES",
        "EPHEM_TYPE": "VECTORS",
        "CENTER": "'500@399'",  # the Earth's centre
        "REF_PLANE": "FRAME",  # ICRF equator, as SkyBoT's EQJ2000 positions
        "REF_SYSTEM": "ICRF",
        "OUT_UNITS": "KM-S",
        "VEC_TABLE": "1",  # position only
        "VEC_CORR": "NONE",  # geometric
        "CSV_FORMAT": "YES",
        "TIME_TYPE": "UT",
        "TLIST_TYPE": "JD",
        "TLIST": tlist,
    })
    # Horizons reports a bad request (e.g. a time before launch) as HTTP 400 with the JSON error.
    try:
        data = resp.json()
    except ValueError:
        raise CatalogError(f"Horizons returned HTTP {resp.status_code}: {resp.text.strip()[:120]!r}") from None
    if resp.status_code != 200 and not (isinstance(data, dict) and data.get("error")):
        raise CatalogError(f"Horizons returned HTTP {resp.status_code}")
    if not isinstance(data, dict):
        raise CatalogError(f"unexpected Horizons response: {str(data)[:120]!r}")
    rows = parse_horizons_vectors(data, len(mjds))
    out = {}
    for m, (jd, x, y, z) in zip(mjds, rows, strict=True):
        if abs(jd - (m + MJD_TO_JD)) > 1e-6:
            raise CatalogError(f"Horizons answered for JD {jd} when asked for {m + MJD_TO_JD:.8f}")
        out[_KEY_FORMAT.format(m)] = [x, y, z]
    return out


def default_cache_dir() -> Path:
    return settings.cache_dir / "horizons" / "spherex"


def _day_file(cache_dir: Path, key: str) -> Path:
    """One small JSON file per UTC day (MJD integer part), so a survey's cache never
    has to be rewritten in full: a day holds at most the few hundred frames SPHEREx took."""
    return cache_dir / f"{math.floor(float(key))}.json"


def _read_cache(path: Path) -> dict[str, list[float]]:
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable Horizons cache %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("ignoring Horizons cache %s: not a JSON object", path)
        return {}

    def valid(v: object) -> bool:
        return (
            isinstance(v, list) and len(v) == 3
            and all(isinstance(c, (int, float)) and math.isfinite(c) for c in v)
        )

    good = {k: v for k, v in data.items() if valid(v)}
    if len(good) != len(data):
        log.warning("ignoring %d bad entries in Horizons cache %s", len(data) - len(good), path)
    return good


def _write_cache(path: Path, data: dict[str, list[float]]) -> None:
    """Atomic replace, so a reader never sees half a file. Another process writing the
    same day at the same time can drop this one's new entries: they are fetched again."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, CACHE_FILE_MODE)  # mkstemp makes it 0600; other users of the cache read it too
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def spherex_geocentric_km(mjds: Sequence[float], cache_dir: Path | None = None) -> list[np.ndarray]:
    """SPHEREx's geocentric ICRF position (km) at each UTC time, from Horizons, cached on disk.

    Only times not in the cache are asked for, in one request per
    MAX_EPOCHS_PER_CALL times. Raises CatalogError when Horizons cannot answer;
    nothing is cached then. A failed cache write is logged and does not fail the call.
    """
    if not mjds:
        return []
    for m in mjds:
        if not math.isfinite(m):
            raise ValueError(f"invalid epoch mjd={m}")
    root = cache_dir or default_cache_dir()
    keys = [_KEY_FORMAT.format(m) for m in mjds]
    days: dict[Path, list[str]] = {}
    for k in keys:
        days.setdefault(_day_file(root, k), []).append(k)
    found: dict[str, list[float]] = {}
    with _cache_lock:
        for path in days:
            found.update(_read_cache(path))
    missing = sorted({k for k in keys if k not in found})
    if missing:
        fetched: dict[str, list[float]] = {}
        for start in range(0, len(missing), MAX_EPOCHS_PER_CALL):
            fetched.update(_fetch_horizons([float(k) for k in missing[start:start + MAX_EPOCHS_PER_CALL]]))
        found.update(fetched)
        new_by_day: dict[Path, dict[str, list[float]]] = {}
        for k, v in fetched.items():
            new_by_day.setdefault(_day_file(root, k), {})[k] = v
        with _cache_lock:
            for path, new in new_by_day.items():
                try:
                    _write_cache(path, {**_read_cache(path), **new})
                except OSError as exc:
                    log.warning("could not write Horizons cache %s: %s", path, exc)
    return [np.array(found[k], dtype=float) for k in keys]
