"""Catalogue lookups for the object profile: Gaia DR3 (IRSA Gator), Gaia DR3 astrophysical
parameters (VizieR mirror) and SIMBAD (Harvard mirror TAP).

Each fetcher makes one or two requests with its own timeout and raises on failure
(`ArchiveError` / `CatalogError`); the profile service turns failures into notes.
Parsers are pure functions of the response.

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
import re
from dataclasses import dataclass

import numpy as np

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
GATOR_TIMEOUT_S = 12.0
GATOR_RETRIES = 1
GAIA_MAX_ROWS = 5000
# Near cone holds every star slower than FAST_PM_MAS (it moves < 11" from 2016 to now);
# faster stars come from a wider cone restricted to fast movers (Barnard's is 10.4"/yr).
NEAR_CONE_ARCSEC = 25.0
FAST_PM_MAS = 1000.0
FAST_CONE_ARCSEC = 160.0
COMPANION_MAX_ROWS = 2000

VIZIER_HOSTS = ("vizier.cfa.harvard.edu", "vizier.cds.unistra.fr")
VIZIER_TIMEOUT_S = 12.0
PARAMP_COLUMNS = ("Source", "Teff", "Rad", "Rad-Flame", "b_Rad-Flame", "B_Rad-Flame",
                  "Lum-Flame", "Mass-Flame", "Age-Flame", "Teff-UCD", "Evol")

SIMBAD_TIMEOUT_S = 12.0
SIMBAD_NEAR_ARCSEC = 40.0
SIMBAD_FAST_ARCSEC = 300.0
_SIMBAD_COLUMNS = ("main_id", "otype", "sp_type", "plx_value", "plx_err", "rvz_radvel", "rvz_err",
                   "pmra", "pmdec", "ra", "dec", "description")
_SIMBAD_SELECT = (
    "SELECT TOP {top} b.main_id, b.otype, b.sp_type, b.plx_value, b.plx_err, b.rvz_radvel, b.rvz_err, "
    "b.pmra, b.pmdec, b.ra, b.dec, d.description FROM basic AS b "
    "LEFT JOIN otypedef AS d ON d.otype = b.otype "
)
_GAIA_ID = re.compile(r"^[0-9]{1,19}$")


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


def _gaia_query(params: dict[str, str], max_rows: int) -> list[GaiaStar]:
    table = gator.query_table(params, what="gaia_dr3_source", timeout=GATOR_TIMEOUT_S,
                              retries=GATOR_RETRIES, max_rows=max_rows)
    return parse_gaia_table(table) if len(table) else []


def fetch_gaia_by_id(source_id: str) -> GaiaStar | None:
    """One Gaia DR3 source by id (digits only; checked here as well as at the route)."""
    if not _GAIA_ID.match(source_id):
        raise ValueError(f"not a Gaia source_id: {source_id!r}")
    params = {"catalog": "gaia_dr3_source", "spatial": "NONE", "outfmt": "1", "selcols": GAIA_COLUMNS,
              "constraints": f"source_id={int(source_id)}", "outrows": "2"}
    stars = _gaia_query(params, max_rows=3)
    return stars[0] if stars else None


def fetch_gaia_cone(ra: float, dec: float, radius_arcsec: float, constraints: str = "",
                    max_rows: int = GAIA_MAX_ROWS) -> list[GaiaStar]:
    params = gator.cone_params("gaia_dr3_source", GAIA_COLUMNS, ra, dec, radius_arcsec, max_rows, constraints)
    return _gaia_query(params, max_rows)


def pick_gaia_match(stars: list[GaiaStar], ra: float, dec: float, tolerance_arcsec: float,
                    now_year: float) -> GaiaStar | None:
    """Brightest star whose track since SPHEREx started passes within the tolerance of (ra, dec)."""
    best = None
    for s in stars:
        sep = physics.track_distance_arcsec(ra, dec, s.ra, s.dec, s.pmra, s.pmdec, s.ref_epoch,
                                            SPHEREX_START_YEAR, now_year)
        if sep > tolerance_arcsec:
            continue
        if best is None or (s.g if s.g is not None else 99.0) < (best.g if best.g is not None else 99.0):
            best = s
    return best


def find_gaia(ra: float, dec: float, tolerance_arcsec: float, now_year: float) -> GaiaStar | None:
    """The Gaia DR3 star at (ra, dec) now: a near cone plus a wide cone of fast movers."""
    stars = fetch_gaia_cone(ra, dec, NEAR_CONE_ARCSEC + tolerance_arcsec)
    stars += fetch_gaia_cone(ra, dec, FAST_CONE_ARCSEC, constraints=f"pm>={FAST_PM_MAS:.0f}")
    return pick_gaia_match(stars, ra, dec, tolerance_arcsec, now_year)


def fetch_companion_candidates(ra: float, dec: float, radius_arcsec: float, plx_lo: float,
                               plx_hi: float) -> list[GaiaStar]:
    """Gaia stars in a cone with a good parallax (>= 5 sigma) inside [plx_lo, plx_hi] mas."""
    constraints = f"parallax_over_error>=5 and parallax>={plx_lo:.4f} and parallax<={plx_hi:.4f}"
    return fetch_gaia_cone(ra, dec, radius_arcsec, constraints=constraints, max_rows=COMPANION_MAX_ROWS)


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


def fetch_astro_params(star: GaiaStar) -> AstroParams | None:
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
            resp = cat._send("GET", f"https://{host}/viz-bin/asu-tsv", params=params,
                             timeout=VIZIER_TIMEOUT_S, what=f"VizieR ({host})")
            if resp.status_code != 200:
                raise cat.CatalogError(f"VizieR ({host}) returned HTTP {resp.status_code}")
            return parse_paramp(resp.text, star.source_id)
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


def _simbad(query: str) -> list[SimbadObject]:
    params = {"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": query}
    failures = []
    for host in cat.SIMBAD_HOSTS:
        try:
            resp = cat._send("POST", f"https://{host}/simbad/sim-tap/sync", params=params,
                             timeout=SIMBAD_TIMEOUT_S, what=f"SIMBAD ({host})")
            if resp.status_code != 200:
                raise cat.CatalogError(f"SIMBAD ({host}) returned HTTP {resp.status_code}")
            return parse_simbad(resp.text)
        except cat.CatalogError as exc:
            failures.append(str(exc))
    raise cat.CatalogError("; ".join(failures))


def adql_string(text: str) -> str:
    """An ADQL string literal: quotes doubled, control characters refused."""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError("identifier contains control characters")
    return "'" + text.replace("'", "''") + "'"


def fetch_simbad_by_ident(ident: str) -> SimbadObject | None:
    """The SIMBAD object with this identifier (SIMBAD normalises case and spacing)."""
    query = (_SIMBAD_SELECT.format(top=5) + "JOIN ident AS i ON i.oidref = b.oid "
             f"WHERE i.id = {adql_string(ident.strip())}")
    found = _simbad(query)
    return found[0] if found else None


def pick_simbad_match(objs: list[SimbadObject], ra: float, dec: float, tolerance_arcsec: float,
                      now_year: float) -> SimbadObject | None:
    """Closest object whose track (moved from J2000 by its own motion) passes near (ra, dec)."""
    best, best_sep = None, math.inf
    for o in objs:
        sep = physics.track_distance_arcsec(ra, dec, o.ra, o.dec, o.pmra, o.pmdec, SIMBAD_EPOCH,
                                            SPHEREX_START_YEAR, now_year)
        if sep <= tolerance_arcsec and sep < best_sep:
            best, best_sep = o, sep
    return best


def find_simbad(ra: float, dec: float, tolerance_arcsec: float, now_year: float) -> SimbadObject | None:
    """SIMBAD object at (ra, dec) now. Positions are J2000, so fast movers come from a wider cone."""
    near = (SIMBAD_NEAR_ARCSEC + tolerance_arcsec) / 3600.0
    query = (
        _SIMBAD_SELECT.format(top=2000)
        + f"WHERE b.otype NOT IN ({', '.join(adql_string(o) for o in cat.SIMBAD_SKIP_OTYPES)}) "
        f"AND CONTAINS(POINT('ICRS', b.ra, b.dec), CIRCLE('ICRS', {ra:.7f}, {dec:.7f}, {SIMBAD_FAST_ARCSEC / 3600.0:.6f})) = 1 "
        f"AND (CONTAINS(POINT('ICRS', b.ra, b.dec), CIRCLE('ICRS', {ra:.7f}, {dec:.7f}, {near:.6f})) = 1 "
        f"OR b.pmra * b.pmra + b.pmdec * b.pmdec >= {FAST_PM_MAS ** 2:.0f})"
    )
    return pick_simbad_match(_simbad(query), ra, dec, tolerance_arcsec, now_year)
