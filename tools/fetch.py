"""Dev-only SPHEREx cutout fetcher for exercising the engine.

Not the app's data layer (that's planetx.spherex.cutouts).
Saves trimmed cutouts (IMAGE/FLAGS/VARIANCE/WCS-WAVE, ~40 KB instead of ~4 MB)
under .cache/dev so reruns are offline.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import astropy.units as u  # noqa: E402
import numpy as np  # noqa: E402
import requests  # noqa: E402
from astropy.coordinates import SkyCoord  # noqa: E402
from astropy.io import fits  # noqa: E402

ALLOWED_PREFIX = "https://irsa.ipac.caltech.edu/ibe/data/spherex/"
CACHE = Path(__file__).resolve().parent.parent / ".cache" / "dev"
KEEP_HDUS = ("IMAGE", "FLAGS", "VARIANCE", "WCS-WAVE")
COLLECTIONS = ("spherex_dr1", "spherex_qr3")

# name -> (SIMBAD identifier or None, fallback "ra dec" string, default detectors)
TARGETS = {
    "barnard": ("Barnard's Star", None, (1, 2, 3)),
    # The challenge gives 08h55m10.831s -07d14m42.53s, an older-epoch position
    # (~8"/yr since); SIMBAD + proper motion lands closer to the SPHEREx epochs.
    "wise0855": ("WISE J085510.83-071442.5", "08h55m10.831s -07d14m42.53s", (5, 6)),
    "eps-indi-b": ("eps Ind B", None, (3, 4, 5)),
    "rw-aur": ("RW Aur", None, (1, 2, 3)),
    "r-dra": ("R Dra", None, (1, 2, 3)),
}


@dataclass
class Visit:
    url: str
    obs_id: str
    mjd: float
    detector: int
    collection: str


def resolve(target: str) -> tuple[SkyCoord, tuple[int, ...]]:
    """Target key, SIMBAD name, or 'ra dec' (deg or sexagesimal) -> coord."""
    if target in TARGETS:
        name, coords, dets = TARGETS[target]
        if name:
            return _simbad(name), dets
        return SkyCoord(coords, unit=(u.hourangle, u.deg)), dets
    try:
        parts = target.replace(",", " ").split()
        return SkyCoord(float(parts[0]) * u.deg, float(parts[1]) * u.deg), (1, 2, 3)
    except (ValueError, IndexError):
        return _simbad(target), (1, 2, 3)


def _simbad(name: str, epoch_jyear: float = 2026.0) -> SkyCoord:
    """Resolve via SIMBAD and move the J2000 position to `epoch_jyear`.

    SIMBAD positions are epoch 2000; fast movers (Barnard's Star: 10"/yr) have
    moved minutes of arc since, i.e. off a small cutout centred on J2000.
    """
    from astroquery.simbad import Simbad

    simbad = Simbad()
    # CDS's own host was returning 404 on 2026-11-14; the Harvard mirror works.
    simbad.server = "simbad.harvard.edu"
    simbad.add_votable_fields("pmra", "pmdec")
    table = simbad.query_object(name)
    if table is None or len(table) == 0:
        raise LookupError(f"SIMBAD could not resolve {name!r}")
    c = SkyCoord(float(table["ra"][0]) * u.deg, float(table["dec"][0]) * u.deg)
    pmra, pmdec = table["pmra"][0], table["pmdec"][0]
    if np.ma.is_masked(pmra) or np.ma.is_masked(pmdec):
        return c
    dt = epoch_jyear - 2000.0
    return c.spherical_offsets_by(float(pmra) * dt * u.mas, float(pmdec) * dt * u.mas)


def _detector_from_url(url: str) -> int:
    m = re.search(r"_\d+D(\d)_spx_", url)
    if not m:
        raise ValueError(f"cannot find detector number in {url}")
    return int(m.group(1))


def find_visits(coord: SkyCoord, detectors: tuple[int, ...], refresh: bool = False) -> list[Visit]:
    """SPHEREx visits covering `coord`, cached on disk so reruns work when
    IRSA is busy. Pass refresh=True to pick up a new data release."""
    key = hashlib.sha1(f"{coord.ra.deg:.5f}|{coord.dec.deg:.5f}|{sorted(detectors)}|{COLLECTIONS}".encode()).hexdigest()[:16]
    cache = CACHE / f"visits_{key}.json"
    if cache.exists() and not refresh:
        return [Visit(**v) for v in json.loads(cache.read_text())]
    visits = _query_visits(coord, detectors)
    CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps([asdict(v) for v in visits]))
    return visits


def _query_visits(coord: SkyCoord, detectors: tuple[int, ...]) -> list[Visit]:
    from astroquery.ipac.irsa import Irsa

    visits: list[Visit] = []
    for coll in COLLECTIONS:
        # Keep the cone tiny: big cones hang IRSA's SIA service.
        table = _retry(lambda: Irsa.query_sia(pos=(coord, 10 * u.arcsec), collection=coll))
        for row in table:
            url = str(row["access_url"])
            det = _detector_from_url(url)
            if det in detectors:
                visits.append(Visit(url, str(row["obs_id"]), float(row["t_min"]), det, coll))
    visits.sort(key=lambda v: v.mjd)
    return visits


def pick_visits(visits: list[Visit], per_pass: int, gap_days: float = 60.0) -> list[Visit]:
    """At most `per_pass` visits per (pass, detector), spread evenly in time."""
    picked: list[Visit] = []
    passes: list[list[Visit]] = []
    for v in visits:
        if not passes or v.mjd - passes[-1][-1].mjd > gap_days:
            passes.append([])
        passes[-1].append(v)
    for p in passes:
        for det in sorted({v.detector for v in p}):
            vs = [v for v in p if v.detector == det]
            idx = np.unique(np.linspace(0, len(vs) - 1, min(per_pass, len(vs))).round().astype(int))
            picked += [vs[i] for i in idx]
    return picked


class _Transient(Exception):
    pass


def _retry(fn, attempts: int = 5):
    for k in range(attempts):
        try:
            return fn()
        except _Transient:
            pass
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code < 500:
                raise
        except (requests.ConnectionError, requests.Timeout):
            pass
        except Exception as exc:  # pyvo wraps HTTP errors in DALServiceError
            code = getattr(exc, "code", None)
            if type(exc).__name__ != "DALServiceError" or (code is not None and code < 500):
                raise
        if k < attempts - 1:
            time.sleep(2 * (k + 1))
    raise RuntimeError(f"gave up after {attempts} attempts (IRSA busy?)")


def fetch_cutout(visit: Visit, coord: SkyCoord, size_deg: float) -> Path:
    if not visit.url.startswith(ALLOWED_PREFIX):
        raise ValueError(f"refusing non-IRSA-SPHEREx URL: {visit.url}")
    key = hashlib.sha1(f"{visit.url}|{coord.ra.deg:.6f}|{coord.dec.deg:.6f}|{size_deg}".encode()).hexdigest()[:16]
    # obs_id comes from IRSA; keep only safe characters so it can't steer the path.
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", visit.obs_id).lstrip(".")
    path = CACHE / f"{safe_id}_D{visit.detector}_{key}.fits"
    if path.exists():
        return path

    def get() -> bytes:
        r = requests.get(
            visit.url,
            params={"center": f"{coord.ra.deg},{coord.dec.deg}", "size": size_deg},
            timeout=120,
            allow_redirects=False,  # the allow-list only vets the first URL
        )
        if r.is_redirect:
            raise ValueError(f"IRSA redirected the cutout request to {r.headers.get('location')!r}")
        r.raise_for_status()
        if not r.content.startswith(b"SIMPLE"):
            raise _Transient()  # IRSA sometimes answers 200 with a "busy" page
        return r.content

    raw = _retry(get)
    with fits.open(io.BytesIO(raw)) as hdul:
        keep = fits.HDUList([fits.PrimaryHDU(header=hdul[0].header)] + [hdul[n].copy() for n in KEEP_HDUS if n in hdul])
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        keep.writeto(tmp, overwrite=True)
        tmp.replace(path)  # atomic: no half-written cache files
    return path


def fetch_all(visits: list[Visit], coord: SkyCoord, size_deg: float, workers: int = 4) -> list[tuple[Visit, Path]]:
    # A few parallel requests at most: IRSA throttles and returns 503 when busy.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        paths = list(pool.map(lambda v: fetch_cutout(v, coord, size_deg), visits))
    return list(zip(visits, paths))
