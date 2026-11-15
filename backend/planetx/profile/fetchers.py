"""Catalogue lookups for the object profile: Gaia DR3 (IRSA Gator), Gaia DR3 astrophysical
parameters (VizieR mirror) and SIMBAD (Harvard mirror TAP).

Each fetcher takes one absolute `deadline` (time.monotonic()) for everything it does:
every HTTP call's timeouts are capped to the time left, a 5xx is retried only when the
wait and a new call still fit, and the body is read in chunks with the deadline checked
between them. Fetchers raise on failure (`ArchiveError` / `CatalogError`, `OutOfTime`);
the profile service turns failures into notes. Parsers are pure functions of the response.

Verified live on 2026-11-15:
  * Gator `gaia_dr3_source` has parallax(_error), pmra/pmdec(_error), pm, radial_velocity(_error),
    phot_g/bp/rp_mean_mag, bp_rp, teff_gspphot, ruwe, non_single_star, distance_gspphot.
    `spatial=NONE` with `constraints=source_id=...` answers in ~1 s.
  * VizieR `I/355/paramp` columns Source, Teff, Rad, Rad-Flame (b_/B_ = 16/84 %), Lum-Flame,
    Mass-Flame, Age-Flame, Teff-UCD, Evol. A `Source=` filter alone returns nothing; a 2" cone
    at the Gaia (2016) position does. Barnard's Star has Rad-Flame and Lum-Flame but no mass or age.
  * SIMBAD `ident.id = '...'` matches names loosely ("barnard's star", "Gaia DR3 <id>").
"""
from __future__ import annotations

import math
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import requests

from planetx.classify import catalogs as cat
from planetx.observatories import catalogs as gator
from planetx.profile import physics

GAIA_EPOCH = 2016.0
SIMBAD_EPOCH = 2000.0
# SPHEREx started observing in 2025; a request position is "now" or engine-measured since then.
SPHEREX_START_YEAR = 2025.3

GAIA_COLUMNS = (
    "source_id,ra,dec,ref_epoch,parallax,parallax_error,pmra,pmra_error,pmdec,pmdec_error,"
    "radial_velocity,radial_velocity_error,phot_g_mean_mag,phot_bp_mean_mag,phot_rp_mean_mag,"
    "bp_rp,teff_gspphot,ruwe,non_single_star,distance_gspphot"
)
CONNECT_TIMEOUT_S = 10.0
RETRY_WAIT_S = 2.0  # one more try after a 5xx answer, only if it still fits the deadline
MIN_CALL_S = 1.0  # less time left than this: do not start a call
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
GAIA_MAX_ROWS = 5000
# Near cone holds every star slower than FAST_PM_MAS (it moves < 11" from 2016 to now);
# faster stars come from a wider cone restricted to fast movers (Barnard's is 10.4"/yr).
NEAR_CONE_ARCSEC = 25.0
FAST_PM_MAS = 1000.0
FAST_CONE_ARCSEC = 160.0
COMPANION_MAX_ROWS = 2000

VIZIER_HOSTS = ("vizier.cfa.harvard.edu", "vizier.cds.unistra.fr")
PARAMP_COLUMNS = ("Source", "Teff", "Rad", "Rad-Flame", "b_Rad-Flame", "B_Rad-Flame",
                  "Lum-Flame", "Mass-Flame", "Age-Flame", "Teff-UCD", "Evol")

SIMBAD_NEAR_ARCSEC = 40.0
SIMBAD_FAST_ARCSEC = 300.0
SIMBAD_TOP = 2000
# Crowded field (SIMBAD's list cut off): search again with the J2000 cone shrunk to the match
# tolerance plus this pad (covers stars slower than ~0.35"/yr since 2000; fast movers stay).
SIMBAD_NARROW_PAD_ARCSEC = 10.0
_SIMBAD_COLUMNS = ("main_id", "otype", "sp_type", "plx_value", "plx_err", "rvz_radvel", "rvz_err",
                   "pmra", "pmdec", "ra", "dec", "description")
_SIMBAD_SELECT = (
    "SELECT TOP {top} b.main_id, b.otype, b.sp_type, b.plx_value, b.plx_err, b.rvz_radvel, b.rvz_err, "
    "b.pmra, b.pmdec, b.ra, b.dec, d.description FROM basic AS b "
    "LEFT JOIN otypedef AS d ON d.otype = b.otype "
)
_GAIA_ID = re.compile(r"^[0-9]{1,19}$")

# A measured motion (pmra, pmdec, error), mas/yr, that a catalogue match must agree with.
Measured = tuple[float, float, float]


class OutOfTime(Exception):
    """The request's time budget ran out before this lookup could make or finish a call."""


# --- HTTP with one deadline -----------------------------------------------------------------


def _open(method: str, url: str, params: dict[str, str], timeout: tuple[float, float]) -> requests.Response:
    """The one place this module touches the network (offline tests replace it)."""
    kw = {"data": params} if method == "POST" else {"params": params}
    return requests.request(method, url, timeout=timeout, stream=True, **kw)


def _time_left(deadline: float, what: str) -> float:
    left = deadline - time.monotonic()
    if left < MIN_CALL_S:
        raise OutOfTime(f"no time left for {what}")
    return left


def _read_body(resp: requests.Response, deadline: float, what: str, error: type[Exception]) -> str:
    chunks, size = [], 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise error(f"{what} answer is larger than {MAX_RESPONSE_BYTES // 2**20} MB")
        if time.monotonic() > deadline:
            raise OutOfTime(f"{what} was still sending when time ran out")
    return b"".join(chunks).decode("utf-8", errors="replace")


def http(method: str, url: str, params: dict[str, str], *, deadline: float, what: str,
         error: type[Exception]) -> tuple[int, str]:
    """(status, body) of one read-only request whose connect, read and retry all end by `deadline`.

    Each socket wait is at most the time left when the call starts, and the deadline is checked
    between body chunks. A 5xx is tried once more only when the wait and a call still fit;
    otherwise the 5xx status is returned with an empty body.
    """
    status = 0
    for attempt in (1, 2):
        left = _time_left(deadline, what)
        try:
            with _open(method, url, params, (min(CONNECT_TIMEOUT_S, left), left)) as resp:
                status = resp.status_code
                if status < 500:
                    return status, _read_body(resp, deadline, what, error)
        except requests.RequestException as exc:
            raise error(f"{what} request failed: {type(exc).__name__}") from exc
        wait = RETRY_WAIT_S * (1 + random.random())
        if attempt == 2 or deadline - time.monotonic() - wait < MIN_CALL_S:
            break
        time.sleep(wait)
    return status, ""


# --- Gaia DR3 -------------------------------------------------------------------------


@dataclass(frozen=True)
class GaiaStar:
    source_id: str
    ra: float  # deg at ref_epoch
    dec: float
    ref_epoch: float
    parallax: float | None = None  # mas
    parallax_error: float | None = None
    pmra: float | None = None  # mas/yr
    pmra_error: float | None = None
    pmdec: float | None = None
    pmdec_error: float | None = None
    radial_velocity: float | None = None  # km/s
    radial_velocity_error: float | None = None
    g: float | None = None
    bp: float | None = None
    rp: float | None = None
    bp_rp: float | None = None
    teff_gspphot: float | None = None
    ruwe: float | None = None
    non_single_star: int | None = None
    distance_gspphot: float | None = None  # pc


def _value(v) -> float | None:
    if np.ma.is_masked(v):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_gaia_table(table) -> list[GaiaStar]:
    """Gator rows -> GaiaStar (rows without a position are dropped)."""
    out = []
    names = set(table.colnames)
    missing = [c for c in ("source_id", "ra", "dec", "parallax", "pmra", "pmdec") if c not in names]
    if missing:
        raise gator.ArchiveError(f"Gaia answer has no column {missing[0]!r}")

    def get(row, col):
        return _value(row[col]) if col in names else None

    for row in table:
        ra, dec = get(row, "ra"), get(row, "dec")
        if ra is None or dec is None or np.ma.is_masked(row["source_id"]):
            continue
        nss = get(row, "non_single_star")
        out.append(GaiaStar(
            source_id=str(int(row["source_id"])), ra=ra, dec=dec,
            ref_epoch=get(row, "ref_epoch") or GAIA_EPOCH,
            parallax=get(row, "parallax"), parallax_error=get(row, "parallax_error"),
            pmra=get(row, "pmra"), pmra_error=get(row, "pmra_error"),
            pmdec=get(row, "pmdec"), pmdec_error=get(row, "pmdec_error"),
            radial_velocity=get(row, "radial_velocity"),
            radial_velocity_error=get(row, "radial_velocity_error"),
            g=get(row, "phot_g_mean_mag"), bp=get(row, "phot_bp_mean_mag"), rp=get(row, "phot_rp_mean_mag"),
            bp_rp=get(row, "bp_rp"), teff_gspphot=get(row, "teff_gspphot"), ruwe=get(row, "ruwe"),
            non_single_star=None if nss is None else int(nss),
            distance_gspphot=get(row, "distance_gspphot"),
        ))
    return out


def _gaia_query(params: dict[str, str], deadline: float, max_rows: int | None) -> list[GaiaStar]:
    """Gator `gaia_dr3_source`; a list reaching `max_rows` raises (cut off), None skips that check."""
    status, text = http("GET", gator.GATOR_URL, params, deadline=deadline, what="IRSA",
                        error=gator.ArchiveError)
    if status != 200:
        raise gator.ArchiveError(f"IRSA returned HTTP {status}")
    table = gator.parse_table(text, what="gaia_dr3_source", max_rows=max_rows)
    return parse_gaia_table(table) if len(table) else []


def fetch_gaia_by_id(source_id: str, *, deadline: float) -> GaiaStar | None:
    """One Gaia DR3 source by id (digits only; checked here as well as at the route)."""
    if not _GAIA_ID.match(source_id):
        raise ValueError(f"not a Gaia source_id: {source_id!r}")
    params = {"catalog": "gaia_dr3_source", "spatial": "NONE", "outfmt": "1", "selcols": GAIA_COLUMNS,
              "constraints": f"source_id={int(source_id)}", "outrows": "2"}
    stars = _gaia_query(params, deadline, max_rows=3)
    return stars[0] if stars else None


def fetch_gaia_cone(ra: float, dec: float, radius_arcsec: float, constraints: str = "", *, deadline: float,
                    max_rows: int = GAIA_MAX_ROWS, check_cut_off: bool = True) -> list[GaiaStar]:
    params = gator.cone_params("gaia_dr3_source", GAIA_COLUMNS, ra, dec, radius_arcsec, max_rows, constraints)
    return _gaia_query(params, deadline, max_rows if check_cut_off else None)


def gaia_motion_agrees(s: GaiaStar, measured: Measured | None) -> bool | None:
    """Whether a Gaia star's motion agrees with a measured one (None: nothing to compare)."""
    if measured is None or s.pmra is None or s.pmdec is None:
        return None
    err = physics.pm_total_err(s.pmra, s.pmdec, s.pmra_error, s.pmdec_error)
    return physics.motion_agrees(s.pmra, s.pmdec, err, *measured)


def _agreement_rank(agrees: bool | None) -> int:
    return 0 if agrees is True else 1 if agrees is None else 2


def pick_gaia_match(stars: list[GaiaStar], ra: float, dec: float, tolerance_arcsec: float,
                    now_year: float, measured: Measured | None = None) -> GaiaStar | None:
    """The star whose track since SPHEREx started passes within the tolerance of (ra, dec).

    With a measured motion, a star moving the same way wins over a brighter one that does
    not (the caller still refuses a disagreeing pick); otherwise the brightest wins.
    """
    best, best_key = None, None
    for s in stars:
        sep = physics.track_distance_arcsec(ra, dec, s.ra, s.dec, s.pmra, s.pmdec, s.ref_epoch,
                                            SPHEREX_START_YEAR, now_year)
        if sep > tolerance_arcsec:
            continue
        key = (_agreement_rank(gaia_motion_agrees(s, measured)), s.g if s.g is not None else 99.0)
        if best_key is None or key < best_key:
            best, best_key = s, key
    return best


def find_gaia(ra: float, dec: float, tolerance_arcsec: float, now_year: float,
              measured: Measured | None = None, *, deadline: float) -> GaiaStar | None:
    """The Gaia DR3 star at (ra, dec) now: a near cone plus, at the same time, a wide cone of fast movers."""
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="profile-gaia-fast") as ex:
        fast = ex.submit(fetch_gaia_cone, ra, dec, FAST_CONE_ARCSEC, f"pm>={FAST_PM_MAS:.0f}", deadline=deadline)
        stars = fetch_gaia_cone(ra, dec, NEAR_CONE_ARCSEC + tolerance_arcsec, deadline=deadline)
        stars += fast.result()
    return pick_gaia_match(stars, ra, dec, tolerance_arcsec, now_year, measured)


@dataclass(frozen=True)
class CompanionSearch:
    stars: list[GaiaStar]
    crowded: bool = False  # the list hit COMPANION_MAX_ROWS: too many stars to search


def fetch_companion_candidates(ra: float, dec: float, radius_arcsec: float, plx_lo: float, plx_hi: float,
                               pmra: float, pmdec: float, pm_window: float, *,
                               deadline: float) -> CompanionSearch:
    """Gaia stars in a cone with a good parallax (>= 5 sigma) inside [plx_lo, plx_hi] mas and
    a motion within `pm_window` mas/yr of (pmra, pmdec) in each component.

    The motion window keeps ordinary fast stars far below the row cap; a field that still
    reaches it is reported as crowded, not as a failure.
    """
    constraints = (
        f"parallax_over_error>=5 and parallax>={plx_lo:.4f} and parallax<={plx_hi:.4f} "
        f"and pmra>={pmra - pm_window:.3f} and pmra<={pmra + pm_window:.3f} "
        f"and pmdec>={pmdec - pm_window:.3f} and pmdec<={pmdec + pm_window:.3f}"
    )
    stars = fetch_gaia_cone(ra, dec, radius_arcsec, constraints=constraints, deadline=deadline,
                            max_rows=COMPANION_MAX_ROWS, check_cut_off=False)
    if len(stars) >= COMPANION_MAX_ROWS:
        return CompanionSearch([], crowded=True)
    return CompanionSearch(stars)


# --- Gaia DR3 astrophysical parameters (VizieR I/355/paramp) -----------------------------------


@dataclass(frozen=True)
class AstroParams:
    teff_gspphot: float | None = None
    radius_gspphot: float | None = None
    radius_flame: float | None = None
    radius_flame_lo: float | None = None
    radius_flame_hi: float | None = None
    lum_flame: float | None = None
    mass_flame: float | None = None
    age_flame: float | None = None
    teff_ucd: float | None = None
    evolstage_flame: int | None = None


def parse_vizier_tsv(text: str) -> list[dict[str, str]]:
    """VizieR asu-tsv rows (header, units, dashes, data). An empty answer still has '#RESOURCE'."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    dash = next((i for i, ln in enumerate(lines) if re.fullmatch(r"-+(\t-+)*\s*", ln)), None)
    if dash is None or dash < 1:
        if "#RESOURCE=" in text and not lines:
            return []
        raise cat.CatalogError(f"unexpected VizieR response: {text.strip()[:120]!r}")
    cols = [c.strip() for c in lines[0].split("\t")]
    rows = []
    for ln in lines[dash + 1:]:
        vals = ln.split("\t")
        rows.append({c: (vals[i].strip() if i < len(vals) else "") for i, c in enumerate(cols)})
    return rows


def parse_paramp(text: str, source_id: str) -> AstroParams | None:
    for row in parse_vizier_tsv(text):
        if row.get("Source", "").strip() != source_id:
            continue
        f = cat._float
        evol = f(row.get("Evol"))
        return AstroParams(
            teff_gspphot=f(row.get("Teff")), radius_gspphot=f(row.get("Rad")),
            radius_flame=f(row.get("Rad-Flame")), radius_flame_lo=f(row.get("b_Rad-Flame")),
            radius_flame_hi=f(row.get("B_Rad-Flame")), lum_flame=f(row.get("Lum-Flame")),
            mass_flame=f(row.get("Mass-Flame")), age_flame=f(row.get("Age-Flame")),
            teff_ucd=f(row.get("Teff-UCD")), evolstage_flame=None if evol is None else int(evol),
        )
    return None


def fetch_astro_params(star: GaiaStar, *, deadline: float) -> AstroParams | None:
    """FLAME / GSP-Phot parameters of one Gaia source, from the first VizieR mirror that answers."""
    params = {
        "-source": "I/355/paramp",
        "-c": f"{star.ra:.7f} {star.dec:+.7f}",
        "-c.rs": "2",
        "-out": ",".join(PARAMP_COLUMNS),
        "-out.max": "20",
    }
    failures = []
    for host in VIZIER_HOSTS:
        try:
            status, text = http("GET", f"https://{host}/viz-bin/asu-tsv", params, deadline=deadline,
                                what=f"VizieR ({host})", error=cat.CatalogError)
            if status != 200:
                raise cat.CatalogError(f"VizieR ({host}) returned HTTP {status}")
            return parse_paramp(text, star.source_id)
        except cat.CatalogError as exc:
            failures.append(str(exc))
    raise cat.CatalogError("; ".join(failures))


# --- SIMBAD -------------------------------------------------------------------------------


@dataclass(frozen=True)
class SimbadObject:
    main_id: str
    name: str  # as people write it
    otype: str | None
    description: str | None
    sp_type: str | None
    plx: float | None  # mas
    plx_err: float | None
    rv: float | None  # km/s
    rv_err: float | None
    pmra: float | None  # mas/yr
    pmdec: float | None
    ra: float  # deg, J2000 epoch
    dec: float


def parse_simbad(text: str) -> list[SimbadObject]:
    out: dict[str, SimbadObject] = {}
    f = cat._float
    for row in cat._rows(text, _SIMBAD_COLUMNS, "SIMBAD"):
        main_id = row["main_id"].strip()
        ra, dec = f(row["ra"]), f(row["dec"])
        if not main_id or ra is None or dec is None or main_id in out:
            continue
        out[main_id] = SimbadObject(
            main_id=main_id, name=cat.display_name(main_id),
            otype=row["otype"].strip() or None, description=row["description"].strip() or None,
            sp_type=row["sp_type"].strip() or None,
            plx=f(row["plx_value"]), plx_err=f(row["plx_err"]),
            rv=f(row["rvz_radvel"]), rv_err=f(row["rvz_err"]),
            pmra=f(row["pmra"]), pmdec=f(row["pmdec"]), ra=ra, dec=dec,
        )
    return list(out.values())


def _simbad(query: str, deadline: float) -> tuple[list[SimbadObject], int]:
    """(objects, number of rows SIMBAD sent) from the first mirror that answers."""
    params = {"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": query}
    failures = []
    for host in cat.SIMBAD_HOSTS:
        try:
            status, text = http("POST", f"https://{host}/simbad/sim-tap/sync", params, deadline=deadline,
                                what=f"SIMBAD ({host})", error=cat.CatalogError)
            if status != 200:
                raise cat.CatalogError(f"SIMBAD ({host}) returned HTTP {status}")
            return parse_simbad(text), len(cat._rows(text, _SIMBAD_COLUMNS, "SIMBAD"))
        except cat.CatalogError as exc:
            failures.append(str(exc))
    raise cat.CatalogError("; ".join(failures))


def adql_string(text: str) -> str:
    """An ADQL string literal: quotes doubled, control characters refused."""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError("identifier contains control characters")
    return "'" + text.replace("'", "''") + "'"


def fetch_simbad_by_ident(ident: str, *, deadline: float) -> SimbadObject | None:
    """The SIMBAD object with this identifier (SIMBAD normalises case and spacing)."""
    query = (_SIMBAD_SELECT.format(top=5) + "JOIN ident AS i ON i.oidref = b.oid "
             f"WHERE i.id = {adql_string(ident.strip())}")
    found, _ = _simbad(query, deadline)
    return found[0] if found else None


def simbad_motion_agrees(o: SimbadObject, measured: Measured | None, non_stellar_otypes=()) -> bool | None:
    """Whether a SIMBAD object's motion agrees with a measured one (None: nothing to compare).

    Objects of `non_stellar_otypes` without a catalogued motion count as not moving.
    SIMBAD's motion errors are not fetched, so only the measured error counts.
    """
    if measured is None:
        return None
    pmra, pmdec = o.pmra, o.pmdec
    if (pmra is None or pmdec is None) and o.otype in non_stellar_otypes:
        pmra, pmdec = 0.0, 0.0
    if pmra is None or pmdec is None:
        return None
    return physics.motion_agrees(pmra, pmdec, None, *measured)


def pick_simbad_match(objs: list[SimbadObject], ra: float, dec: float, tolerance_arcsec: float,
                      now_year: float, measured: Measured | None = None,
                      non_stellar_otypes=()) -> SimbadObject | None:
    """Closest object whose track (moved from J2000 by its own motion) passes near (ra, dec);
    with a measured motion, one moving the same way wins over a closer one that does not."""
    best, best_key = None, None
    for o in objs:
        sep = physics.track_distance_arcsec(ra, dec, o.ra, o.dec, o.pmra, o.pmdec, SIMBAD_EPOCH,
                                            SPHEREX_START_YEAR, now_year)
        if sep > tolerance_arcsec:
            continue
        key = (_agreement_rank(simbad_motion_agrees(o, measured, non_stellar_otypes)), sep)
        if best_key is None or key < best_key:
            best, best_key = o, key
    return best


def _simbad_cone_query(ra: float, dec: float, near_arcsec: float) -> str:
    return (
        _SIMBAD_SELECT.format(top=SIMBAD_TOP)
        + f"WHERE b.otype NOT IN ({', '.join(adql_string(o) for o in cat.SIMBAD_SKIP_OTYPES)}) "
        f"AND CONTAINS(POINT('ICRS', b.ra, b.dec), CIRCLE('ICRS', {ra:.7f}, {dec:.7f}, {SIMBAD_FAST_ARCSEC / 3600.0:.6f})) = 1 "
        f"AND (CONTAINS(POINT('ICRS', b.ra, b.dec), CIRCLE('ICRS', {ra:.7f}, {dec:.7f}, {near_arcsec / 3600.0:.6f})) = 1 "
        f"OR b.pmra * b.pmra + b.pmdec * b.pmdec >= {FAST_PM_MAS ** 2:.0f})"
    )


def find_simbad(ra: float, dec: float, tolerance_arcsec: float, now_year: float,
                measured: Measured | None = None, non_stellar_otypes=(), *,
                deadline: float) -> tuple[SimbadObject | None, str | None]:
    """(SIMBAD object at (ra, dec) now, note). Positions are J2000, so fast movers come from a wider cone.

    A list SIMBAD cut off at SIMBAD_TOP rows may miss the object, so the J2000 cone is narrowed
    once; the note says what was done (or that the list was still cut off).
    """
    objs, n = _simbad(_simbad_cone_query(ra, dec, SIMBAD_NEAR_ARCSEC + tolerance_arcsec), deadline)
    note = None
    if n >= SIMBAD_TOP:
        objs, n = _simbad(_simbad_cone_query(ra, dec, tolerance_arcsec + SIMBAD_NARROW_PAD_ARCSEC), deadline)
        note = ("SIMBAD lists so many objects here that even a narrowed search was cut off; "
                "its match may be missing." if n >= SIMBAD_TOP else
                "SIMBAD lists many objects here, so only a narrower area was searched "
                "(slow-moving stars further out are not included).")
    return pick_simbad_match(objs, ra, dec, tolerance_arcsec, now_year, measured, non_stellar_otypes), note
