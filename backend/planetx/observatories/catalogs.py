"""Fetch archival catalog positions from IRSA (the NASA infrared archive).

Uses IRSA's Gator cone search rather than TAP: on 2026-11-14 TAP timed out
(>90 s) on catwise_2020 and neowiser_p1bs_psd while Gator answered in ~1.5 s.
Gator reports errors as HTTP 200 with a `[struct stat="ERROR" ...]` body.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import requests
from astropy.io import ascii as astropy_ascii
from astropy.table import Table

from .sources import ArchivalSource, Observatory

GATOR_URL = "https://irsa.ipac.caltech.edu/cgi-bin/Gator/nph-query"
MAX_RADIUS_ARCSEC = 1200.0
# CatWISE2020 quotes its motion-fit position (ra_pm, dec_pm) at this date.
CATWISE_EPOCH_MJD = 57170.0
# NEOWISE exposures of one object are averaged in windows this long; a
# linearly moving source's mean position at the mean time stays exact.
NEOWISE_BIN_DAYS = 30.0
NEOWISE_CLUSTER_ARCSEC = 2.0
NEOWISE_MIN_EXPOSURES = 3


class ArchiveError(RuntimeError):
    """The archive could not answer (network failure, 5xx, or a Gator error)."""


def _col(table: Table, name: str) -> np.ndarray:
    """Float column with masked/null entries as NaN."""
    col = table[name]
    data = np.ma.filled(np.ma.asarray(col, dtype=float), np.nan)
    return np.asarray(data, dtype=float)


def _text(table: Table, name: str) -> list[str]:
    col = np.ma.asarray(table[name])
    return ["" if m else str(v) for v, m in zip(col.data, np.ma.getmaskarray(col))]


def _iso_sigma(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Per-axis sigma of the circular error with the same area as (a, b)."""
    return np.sqrt(np.abs(a * b))


def _parse_2mass(t: Table) -> list[ArchivalSource]:
    ra, dec = _col(t, "ra"), _col(t, "dec")
    sigma = _iso_sigma(_col(t, "err_maj"), _col(t, "err_min"))
    mjd = _col(t, "jdate") - 2400000.5
    k, j = _col(t, "k_m"), _col(t, "j_m")
    out = []
    for i, (qual, cc, name) in enumerate(
        zip(_text(t, "ph_qual"), _text(t, "cc_flg"), _text(t, "designation"))
    ):
        # Keep a source only if some band is a real detection (A-D) that is not
        # a persistence/diffraction/stripe/bleed artifact ("c" is confusion: fine).
        if not any(q in "ABCD" and c in "0c" for q, c in zip(qual, cc)):
            continue
        if not np.isfinite([ra[i], dec[i], sigma[i], mjd[i]]).all():
            continue
        mag, band = (k[i], "Ks") if np.isfinite(k[i]) else (j[i], "J")
        out.append(
            ArchivalSource(Observatory.TWOMASS, ra[i], dec[i], mjd[i], sigma[i], mag, band, name)
        )
    return out


def _wise_real(cc_flags: str) -> bool:
    """False when both W1 and W2 are flagged as spurious (upper-case D/H/O/P)."""
    w1w2 = cc_flags[:2]
    return not (len(w1w2) == 2 and all(c.isupper() for c in w1w2))


def _parse_allwise(t: Table) -> list[ArchivalSource]:
    ra, dec = _col(t, "ra"), _col(t, "dec")
    sigma = _iso_sigma(_col(t, "sigra"), _col(t, "sigdec"))
    m1, m2 = _col(t, "w1mjdmean"), _col(t, "w2mjdmean")
    mjd = np.where(np.isfinite(m1), m1, m2)
    w1, w2 = _col(t, "w1mpro"), _col(t, "w2mpro")
    out = []
    for i, (cc, name) in enumerate(zip(_text(t, "cc_flags"), _text(t, "designation"))):
        if not _wise_real(cc) or not np.isfinite([ra[i], dec[i], sigma[i], mjd[i]]).all():
            continue
        mag, band = (w1[i], "W1") if np.isfinite(w1[i]) else (w2[i], "W2")
        out.append(
            ArchivalSource(Observatory.ALLWISE, ra[i], dec[i], mjd[i], sigma[i], mag, band, name)
        )
    return out


def _parse_catwise(t: Table) -> list[ArchivalSource]:
    ra, dec = _col(t, "ra_pm"), _col(t, "dec_pm")
    sigma = _iso_sigma(_col(t, "sigra_pm"), _col(t, "sigdec_pm"))
    w1, w2 = _col(t, "w1mpro_pm"), _col(t, "w2mpro_pm")
    out = []
    for i, name in enumerate(_text(t, "source_name")):
        if not np.isfinite([ra[i], dec[i], sigma[i]]).all():
            continue
        mag, band = (w1[i], "W1") if np.isfinite(w1[i]) else (w2[i], "W2")
        out.append(
            ArchivalSource(
                Observatory.CATWISE, ra[i], dec[i], CATWISE_EPOCH_MJD, sigma[i], mag, band, name
            )
        )
    return out


def bin_exposures(
    ra: np.ndarray, dec: np.ndarray, mjd: np.ndarray, sigma: np.ndarray, mag: np.ndarray
) -> list[ArchivalSource]:
    """Average NEOWISE single-exposure detections into one position per object per visit."""
    out: list[ArchivalSource] = []
    order = np.argsort(mjd)
    ra, dec, mjd, sigma, mag = (a[order] for a in (ra, dec, mjd, sigma, mag))
    visit = 0
    start = 0
    n = len(mjd)
    while start < n:
        stop = int(np.searchsorted(mjd, mjd[start] + NEOWISE_BIN_DAYS, side="right"))
        idx = np.arange(start, stop)
        cosd = math.cos(math.radians(float(np.mean(dec[idx]))))
        x = ((ra[idx] - ra[idx][0] + 180.0) % 360.0 - 180.0) * cosd * 3600.0
        y = (dec[idx] - dec[idx][0]) * 3600.0
        unassigned = np.ones(len(idx), dtype=bool)
        made = False
        # Seed clusters with the best-measured detections first.
        for seed in np.argsort(sigma[idx]):
            if not unassigned[seed]:
                continue
            near = unassigned & (np.hypot(x - x[seed], y - y[seed]) <= NEOWISE_CLUSTER_ARCSEC)
            unassigned &= ~near
            if near.sum() < NEOWISE_MIN_EXPOSURES:
                continue
            members = idx[near]
            w = 1.0 / sigma[members] ** 2
            mx = float((w * x[near]).sum() / w.sum())
            my = float((w * y[near]).sum() / w.sum())
            formal = float(1.0 / math.sqrt(w.sum()))
            scatter = float(
                np.hypot(x[near].std(), y[near].std()) / math.sqrt(2 * near.sum())
            )
            out.append(
                ArchivalSource(
                    Observatory.NEOWISE,
                    float((ra[idx][0] + mx / 3600.0 / cosd) % 360.0),
                    float(dec[idx][0] + my / 3600.0),
                    float((w * mjd[members]).sum() / w.sum()),
                    max(formal, scatter),
                    float(np.nanmedian(mag[members])) if np.isfinite(mag[members]).any() else float("nan"),
                    "W1",
                    visit=visit,
                )
            )
            made = True
        if made:
            visit += 1
        start = stop
    return out


def _parse_neowise(t: Table) -> list[ArchivalSource]:
    ra, dec, mjd = _col(t, "ra"), _col(t, "dec"), _col(t, "mjd")
    sigma = _iso_sigma(_col(t, "sigra"), _col(t, "sigdec"))
    mag = _col(t, "w1mpro")
    real = np.array([_wise_real(c) for c in _text(t, "cc_flags")], dtype=bool)
    ok = real & np.isfinite(ra) & np.isfinite(dec) & np.isfinite(mjd) & (sigma > 0)
    return bin_exposures(ra[ok], dec[ok], mjd[ok], sigma[ok], mag[ok])


@dataclass(frozen=True)
class _Catalog:
    table: str
    columns: str
    parse: Callable[[Table], list[ArchivalSource]]
    constraints: str = ""
    max_radius_arcsec: float = MAX_RADIUS_ARCSEC
    max_rows: int = 50_000


_CATALOGS: dict[Observatory, _Catalog] = {
    Observatory.TWOMASS: _Catalog(
        "fp_psc",
        "designation,ra,dec,err_maj,err_min,j_m,k_m,ph_qual,cc_flg,jdate",
        _parse_2mass,
        constraints="mp_flg=0",  # drop detections of known minor planets
    ),
    Observatory.ALLWISE: _Catalog(
        "allwise_p3as_psd",
        "designation,ra,dec,sigra,sigdec,w1mpro,w2mpro,w1mjdmean,w2mjdmean,cc_flags",
        _parse_allwise,
        constraints="w1snr>=5 or w2snr>=5",
    ),
    Observatory.CATWISE: _Catalog(
        "catwise_2020",
        "source_name,ra_pm,dec_pm,sigra_pm,sigdec_pm,w1mpro_pm,w2mpro_pm",
        _parse_catwise,
        # The catalog keeps very low S/N entries; they are mostly noise peaks.
        constraints="w1snr_pm>=5 or w2snr_pm>=5",
    ),
    Observatory.NEOWISE: _Catalog(
        "neowiser_p1bs_psd",
        "ra,dec,sigra,sigdec,mjd,w1mpro,cc_flags",
        _parse_neowise,
        # Frame-quality cuts recommended by the NEOWISE explanatory supplement.
        constraints="qual_frame>0 and qi_fact>0 and saa_sep>0 and (w1snr>=5 or w2snr>=5)",
        max_radius_arcsec=60.0,
        max_rows=60_000,
    ),
}


def _get(params: dict[str, str], timeout: float, retries: int) -> str:
    """GET from Gator; retry only on 5xx (IRSA returns 502/503 in bursts)."""
    last = ""
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(2.0 * attempt)
        try:
            resp = requests.get(GATOR_URL, params=params, timeout=timeout)
        except requests.RequestException as exc:
            raise ArchiveError(f"IRSA request failed: {exc}") from exc
        if resp.status_code < 500:
            if resp.status_code != 200:
                raise ArchiveError(f"IRSA returned HTTP {resp.status_code}")
            return resp.text
        last = f"IRSA returned HTTP {resp.status_code}"
    raise ArchiveError(f"{last} after {retries + 1} attempts")


def fetch_catalog(
    observatory: Observatory,
    ra: float,
    dec: float,
    radius_arcsec: float,
    *,
    timeout: float = 90.0,
    retries: int = 2,
) -> list[ArchivalSource]:
    """All usable catalog positions of `observatory` within a cone.

    Raises ArchiveError when the archive cannot answer and ValueError for a bad
    request. A radius above the catalog's limit is rejected, not clipped, so the
    caller always knows what area was searched.
    """
    if observatory not in _CATALOGS:
        raise ValueError(f"{observatory.value} has no archival catalog")
    cat = _CATALOGS[observatory]
    if not (math.isfinite(ra) and math.isfinite(dec) and 0 <= ra < 360 and -90 <= dec <= 90):
        raise ValueError(f"invalid position ra={ra}, dec={dec}")
    if not (0 < radius_arcsec <= cat.max_radius_arcsec):
        raise ValueError(
            f"{observatory.value} radius must be in (0, {cat.max_radius_arcsec:g}] arcsec, "
            f"got {radius_arcsec}"
        )
    params = {
        "catalog": cat.table,
        "spatial": "cone",
        "objstr": f"{ra:.7f} {dec:+.7f}",
        "radius": f"{radius_arcsec:.2f}",
        "radunits": "arcsec",
        "outfmt": "1",  # IPAC ASCII table
        "selcols": cat.columns,
        "outrows": str(cat.max_rows),
    }
    if cat.constraints:
        params["constraints"] = cat.constraints
    text = _get(params, timeout, retries)
    if text.lstrip().startswith("[struct"):
        raise ArchiveError(f"Gator error for {cat.table}: {text.strip()[:300]}")
    try:
        table = astropy_ascii.read(text, format="ipac")
    except Exception as exc:
        raise ArchiveError(f"unreadable Gator response for {cat.table}: {exc}") from exc
    if not isinstance(table, Table):
        raise ArchiveError(f"unreadable Gator response for {cat.table}")
    if len(table) >= cat.max_rows:
        # A cut-off list could hide the true counterpart; better none than partial.
        raise ArchiveError(
            f"{cat.table} cone of {radius_arcsec:.0f}\" returned more than {cat.max_rows} rows; "
            "use a smaller radius"
        )
    if len(table) == 0:
        return []
    return cat.parse(table)
