"""Fetch the catalogues that say what an engine object is: Gaia DR3, SIMBAD, SkyBoT.

Thin wrappers: one HTTP request each, with explicit timeouts, and a parser that
is a pure function of the response text. Plain `requests` rather than astroquery,
because astroquery caches responses (errors included) for a week and its TAP
clients take no per-call timeout.

Verified live on 2026-11-14:
  * Gaia: ESA TAP `sync` answers a 10' cone (about 7,000 stars) as CSV in ~4 s.
    Errors are HTTP 400 with a VOTable body.
  * SIMBAD: the CDS host answers 404; the Harvard mirror answers in under a second.
    Coordinates are at epoch J2000 (Barnard's Star sits 166" south of its Gaia
    position), so positions must be moved with SIMBAD's own proper motions.
  * SkyBoT: 2-10 s per cone. No object is HTTP 204 with an empty body; a failure
    can be HTTP 200 with a JSON object `{"flag": -1, "message": ...}` instead of
    the JSON list of bodies. Planets are left out of the search: their satellite
    ephemerides made a query fail, and planets saturate SPHEREx anyway.
"""
from __future__ import annotations

import csv
import io
import json
import math
import random
import re
import time
from dataclasses import dataclass

import requests

GAIA_TAP_URL = "https://gea.esac.esa.int/tap-server/tap/sync"
# Tried in this order; the first that answers wins.
SIMBAD_HOSTS = ("simbad.harvard.edu", "simbad.cds.unistra.fr")
SKYBOT_URL = "https://ssp.imcce.fr/webservices/skybot/api/conesearch.php"

CONNECT_TIMEOUT_S = 10.0
GAIA_TIMEOUT_S = 60.0
SIMBAD_TIMEOUT_S = 30.0
SKYBOT_TIMEOUT_S = 30.0
RETRY_WAIT_S = 2.0  # one more try after a 5xx answer
MAX_RADIUS_ARCSEC = 3600.0

# Brightest first, so a cut-off list loses only the faintest stars.
GAIA_MAX_ROWS = 20_000
SIMBAD_MAX_ROWS = 5_000

GAIA_DR3_EPOCH_YR = 2016.0
SIMBAD_EPOCH_MJD = 51544.5  # J2000.0
MJD_J2000 = 51544.5
DAYS_PER_YEAR = 365.25
# Gaia's own cut for the DR3 variability tables.
GAIA_VARIABLE_FLAG = "VARIABLE"
# A star is called "high proper motion" in words from this speed (arcsec/yr).
HIGH_PM_ARCSEC_YR = 0.1

# SIMBAD object types (codes from its `otypedef` table) defined by a changing
# brightness. A trailing "?" in SIMBAD marks a candidate of the same type.
SIMBAD_VARIABLE_OTYPES = frozenset({
    "var", "V*", "Er*", "Ir*", "Pu*", "Ro*", "BY*", "CV*", "No*", "EB*", "El*", "RS*", "Sy*",
    "XB*", "HXB", "LXB", "AB*", "Mi*", "Ce*", "cC*", "RR*", "LP*", "OH*", "RV*", "WV*", "SX*",
    "dS*", "gD*", "bC*", "RC*", "a2*", "SN*", "Y*O", "Ae*", "Or*", "TT*",
})
# Exoplanets are listed at their star's position; they are never what SPHEREx sees.
SIMBAD_SKIP_OTYPES = ("Pl", "Pl?")

# SkyBoT dynamical classes ("MB>Inner", "NEA>Apollo", ...) in plain words, by first part.
_BODY_KINDS = {
    "MB": "Main-belt asteroid",
    "NEA": "Near-Earth asteroid",
    "KBO": "Kuiper-belt object",
    "Trojan": "Trojan asteroid",
    "Mars-Crosser": "Mars-crossing asteroid",
    "Hungaria": "Hungaria asteroid",
    "Centaur": "Centaur",
    "Comet": "Comet",
}


class CatalogError(RuntimeError):
    """The catalogue could not answer (network failure, HTTP error, unreadable body)."""


@dataclass(frozen=True)
class CatalogObject:
    """One catalogued star or other fixed object, from Gaia or SIMBAD."""

    catalog: str  # "gaia" | "simbad"
    name: str  # "Gaia DR3 4472832130942575872", "Barnard's Star"
    ra: float  # degrees (ICRS) at `epoch_mjd`
    dec: float
    epoch_mjd: float
    pm_ra_cosdec: float | None = None  # arcsec/yr, east positive; None = not measured
    pm_dec: float | None = None
    magnitude: float | None = None  # Gaia G; for SIMBAD G, else V, else K
    kind: str | None = None  # object type in plain words
    variable: bool = False  # the catalogue lists it as a variable star

    @property
    def pm_total(self) -> float:
        """Total proper motion in arcsec/yr; 0 when the catalogue has none."""
        if self.pm_ra_cosdec is None or self.pm_dec is None:
            return 0.0
        return math.hypot(self.pm_ra_cosdec, self.pm_dec)


@dataclass(frozen=True)
class SolarSystemBody:
    """A known asteroid or comet at the moment a SkyBoT cone was searched."""

    name: str  # "(433) Eros", "2015 RQ390"
    kind: str  # plain words, e.g. "Main-belt asteroid"
    ra: float  # degrees, geocentric
    dec: float
    mjd: float
    v_mag: float | None = None
    geocentric_au: float | None = None
    position_err_arcsec: float | None = None


def _check_cone(ra: float, dec: float, radius_arcsec: float) -> None:
    if not (math.isfinite(ra) and math.isfinite(dec) and 0 <= ra < 360 and -90 <= dec <= 90):
        raise ValueError(f"invalid position ra={ra}, dec={dec}")
    if not (math.isfinite(radius_arcsec) and 0 < radius_arcsec <= MAX_RADIUS_ARCSEC):
        raise ValueError(f"radius must be in (0, {MAX_RADIUS_ARCSEC:g}] arcsec, got {radius_arcsec}")


def _send(method: str, url: str, *, params: dict[str, str], timeout: float, what: str) -> requests.Response:
    """One catalogue request. A 5xx answer is tried once more; everything else is final.

    All three services are queried with read-only requests, so repeating one is safe.
    """
    for attempt in (1, 2):
        try:
            if method == "POST":
                resp = requests.post(url, data=params, timeout=(CONNECT_TIMEOUT_S, timeout))
            else:
                resp = requests.get(url, params=params, timeout=(CONNECT_TIMEOUT_S, timeout))
        except requests.RequestException as exc:
            raise CatalogError(f"{what} request failed: {type(exc).__name__}") from exc
        if resp.status_code < 500 or attempt == 2:
            return resp
        time.sleep(RETRY_WAIT_S * (1 + random.random()))
    raise AssertionError("unreachable")


def _float(text: str | None) -> float | None:
    if text is None or text.strip() == "":
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _rows(text: str, required: tuple[str, ...], what: str) -> list[dict[str, str]]:
    """CSV rows as dicts, after checking that the expected columns are there."""
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    missing = [c for c in required if c not in header]
    if missing:
        raise CatalogError(f"unexpected {what} response (no column {missing[0]!r}): {text.strip()[:120]!r}")
    return list(reader)


# --- Gaia DR3 -------------------------------------------------------------------

_GAIA_COLUMNS = (
    "source_id", "ra", "dec", "ref_epoch", "pmra", "pmdec",
    "phot_g_mean_mag", "phot_rp_mean_mag", "phot_variable_flag",
)


def parse_gaia_csv(text: str) -> list[CatalogObject]:
    """Stars from a Gaia TAP CSV answer. Proper motions are converted from mas/yr."""
    out = []
    for row in _rows(text, _GAIA_COLUMNS, "Gaia"):
        ra, dec = _float(row["ra"]), _float(row["dec"])
        if ra is None or dec is None or not row["source_id"].strip():
            continue
        epoch_yr = _float(row["ref_epoch"]) or GAIA_DR3_EPOCH_YR
        pmra, pmdec = _float(row["pmra"]), _float(row["pmdec"])
        has_pm = pmra is not None and pmdec is not None
        fast = has_pm and math.hypot(pmra, pmdec) / 1000.0 >= HIGH_PM_ARCSEC_YR
        out.append(CatalogObject(
            catalog="gaia",
            name=f"Gaia DR3 {row['source_id'].strip()}",
            ra=ra, dec=dec,
            epoch_mjd=MJD_J2000 + (epoch_yr - 2000.0) * DAYS_PER_YEAR,
            pm_ra_cosdec=pmra / 1000.0 if has_pm else None,
            pm_dec=pmdec / 1000.0 if has_pm else None,
            magnitude=_float(row["phot_g_mean_mag"]),
            kind="High proper-motion star" if fast else "Star",
            variable=row["phot_variable_flag"].strip() == GAIA_VARIABLE_FLAG,
        ))
    return out


def fetch_gaia(ra: float, dec: float, radius_arcsec: float) -> list[CatalogObject]:
    """Gaia DR3 stars in a cone, brightest first, at most GAIA_MAX_ROWS of them.

    Positions are at Gaia's epoch (2016.0): the cone must be wide enough to hold
    fast stars that have since moved into the field. Raises CatalogError.
    """
    _check_cone(ra, dec, radius_arcsec)
    # The values are numbers formatted here, never text from a request: no ADQL injection.
    query = (
        f"SELECT TOP {GAIA_MAX_ROWS} {', '.join(_GAIA_COLUMNS)} FROM gaiadr3.gaia_source "
        f"WHERE 1 = CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra:.7f}, {dec:.7f}, {radius_arcsec / 3600.0:.6f})) "
        "ORDER BY phot_g_mean_mag"
    )
    resp = _send(
        "POST", GAIA_TAP_URL, timeout=GAIA_TIMEOUT_S, what="Gaia",
        params={"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": query},
    )
    if resp.status_code != 200:
        raise CatalogError(f"Gaia returned HTTP {resp.status_code}")
    return parse_gaia_csv(resp.text)


# --- SIMBAD ---------------------------------------------------------------------

_SIMBAD_COLUMNS = ("main_id", "ra", "dec", "otype", "description", "pmra", "pmdec", "G", "V", "K")
_NAME_PREFIX = re.compile(r"^(NAME|V\*|\*\*|\*)\s+")
_WORD_START = re.compile(r"(^|\s)([a-z])")


def display_name(main_id: str) -> str:
    """A SIMBAD identifier as people write it: "NAME Barnard's star" -> "Barnard's Star".

    SIMBAD marks the kind of identifier with a prefix ("NAME" for proper names,
    "V*" for variable-star designations, "*" for Bayer/Flamsteed names). Proper
    names get each word capitalised; catalogue designations are left as they are.
    """
    text = " ".join(main_id.split())
    match = _NAME_PREFIX.match(text)
    if not match:
        return text
    rest = text[match.end():]
    if match.group(1) == "NAME":
        rest = _WORD_START.sub(lambda m: m.group(1) + m.group(2).upper(), rest)
    return rest


def is_variable_otype(otype: str) -> bool:
    """True for SIMBAD types (and their "?" candidates) defined by a changing brightness."""
    if otype in SIMBAD_VARIABLE_OTYPES:
        return True
    if otype.endswith("?"):
        stem = otype[:-1]
        return stem in SIMBAD_VARIABLE_OTYPES or stem + "*" in SIMBAD_VARIABLE_OTYPES
    return False


def parse_simbad_csv(text: str) -> list[CatalogObject]:
    """Objects from a SIMBAD TAP CSV answer (one per identifier; the type join can repeat rows)."""
    out: dict[str, CatalogObject] = {}
    for row in _rows(text, _SIMBAD_COLUMNS, "SIMBAD"):
        main_id = row["main_id"].strip()
        ra, dec = _float(row["ra"]), _float(row["dec"])
        if not main_id or ra is None or dec is None or main_id in out:
            continue
        pmra, pmdec = _float(row["pmra"]), _float(row["pmdec"])
        has_pm = pmra is not None and pmdec is not None
        magnitude = next((m for m in (_float(row[band]) for band in ("G", "V", "K")) if m is not None), None)
        otype = row["otype"].strip()
        out[main_id] = CatalogObject(
            catalog="simbad",
            name=display_name(main_id),
            ra=ra, dec=dec, epoch_mjd=SIMBAD_EPOCH_MJD,
            pm_ra_cosdec=pmra / 1000.0 if has_pm else None,
            pm_dec=pmdec / 1000.0 if has_pm else None,
            magnitude=magnitude,
            kind=row["description"].strip() or otype or None,
            variable=is_variable_otype(otype),
        )
    return list(out.values())


def fetch_simbad(ra: float, dec: float, radius_arcsec: float) -> list[CatalogObject]:
    """SIMBAD objects in a cone, from the first mirror that answers. Raises CatalogError.

    Positions are at epoch J2000, so the cone must reach further back than Gaia's.
    """
    _check_cone(ra, dec, radius_arcsec)
    skip = ", ".join(f"'{o}'" for o in SIMBAD_SKIP_OTYPES)
    query = (
        f"SELECT TOP {SIMBAD_MAX_ROWS} b.main_id, b.ra, b.dec, b.otype, o.description, b.pmra, b.pmdec, "
        "f.G, f.V, f.K FROM basic AS b "
        "LEFT JOIN otypedef AS o ON o.otype = b.otype "
        "LEFT JOIN allfluxes AS f ON f.oidref = b.oid "
        f"WHERE b.otype NOT IN ({skip}) AND CONTAINS(POINT('ICRS', b.ra, b.dec), "
        f"CIRCLE('ICRS', {ra:.7f}, {dec:.7f}, {radius_arcsec / 3600.0:.6f})) = 1"
    )
    params = {"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": query}
    failures = []
    for host in SIMBAD_HOSTS:
        try:
            resp = _send("POST", f"https://{host}/simbad/sim-tap/sync", params=params,
                         timeout=SIMBAD_TIMEOUT_S, what=f"SIMBAD ({host})")
            if resp.status_code != 200:
                raise CatalogError(f"SIMBAD ({host}) returned HTTP {resp.status_code}")
            return parse_simbad_csv(resp.text)
        except CatalogError as exc:
            failures.append(str(exc))
    raise CatalogError("; ".join(failures))


# --- SkyBoT ---------------------------------------------------------------------


def _sexagesimal(text: str, hours: bool) -> float:
    """"17 57 59.0642" (hours) or "-22 59 33.230" (degrees) as degrees."""
    parts = text.split()
    if len(parts) != 3:
        raise ValueError(f"not a sexagesimal angle: {text!r}")
    sign = -1.0 if parts[0].startswith("-") else 1.0
    value = abs(float(parts[0])) + float(parts[1]) / 60.0 + float(parts[2]) / 3600.0
    return sign * value * (15.0 if hours else 1.0)


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def parse_skybot_json(text: str, mjd: float) -> list[SolarSystemBody]:
    """Bodies from a SkyBoT cone-search JSON answer (`-output=all`).

    An empty body means no known object. A JSON object instead of a list is
    SkyBoT reporting a failure, whatever the HTTP status was.
    """
    if not text.strip():
        return []
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CatalogError(f"unreadable SkyBoT response: {text.strip()[:120]!r}") from exc
    if not isinstance(data, list):
        message = data.get("message") if isinstance(data, dict) else None
        raise CatalogError(f"SkyBoT error: {str(message or data).strip()[:200]}")
    out = []
    for row in data:
        try:
            name = str(row["Name"]).strip()
            ra = _sexagesimal(row["RA (hms)"], hours=True)
            dec = _sexagesimal(row["DEC (dms)"], hours=False)
            dyn_class = str(row["Class"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CatalogError(f"unexpected SkyBoT row: {str(row)[:120]}") from exc
        number = row.get("Num")
        if isinstance(number, int) and not isinstance(number, bool):
            name = f"({number}) {name}"
        out.append(SolarSystemBody(
            name=name,
            kind=_BODY_KINDS.get(dyn_class.split(">")[0], "Solar-system body"),
            ra=ra, dec=dec, mjd=mjd,
            v_mag=_number(row.get("VMag (mag)")),
            geocentric_au=_number(row.get("dg (ua)")),
            position_err_arcsec=_number(row.get("Err (arcsec)")),
        ))
    return out


def fetch_skybot(ra: float, dec: float, radius_arcsec: float, mjd: float) -> list[SolarSystemBody]:
    """Known asteroids and comets in a cone at one moment (UTC), seen from the Earth's centre.

    SPHEREx is in low Earth orbit, so a body's true position differs from the
    geocentric one by up to about 10" divided by its distance in au: see
    planetx.classify.observer for the correction. Raises CatalogError.
    """
    _check_cone(ra, dec, radius_arcsec)
    if not math.isfinite(mjd):
        raise ValueError(f"invalid epoch mjd={mjd}")
    resp = _send("GET", SKYBOT_URL, timeout=SKYBOT_TIMEOUT_S, what="SkyBoT", params={
        "-ra": f"{ra:.6f}",
        "-dec": f"{dec:.6f}",
        "-rd": f"{radius_arcsec / 3600.0:.6f}",
        "-ep": f"{mjd + 2400000.5:.6f}",  # Julian date
        # The geocentre. SkyBoT takes only observatory codes here: SPHEREx's own code
        # (288) is answered as 500, and "-loc" (astroquery 0.4.11's name) is ignored.
        "-observer": "500",
        "-filter": "120",  # keep bodies whose own position is uncertain by up to 120"
        "-objFilter": "101",  # asteroids and comets, no planets
        "-refsys": "EQJ2000",
        "-output": "all",
        "-mime": "json",
    })
    if resp.status_code == 204:
        return []
    if resp.status_code != 200:
        raise CatalogError(f"SkyBoT returned HTTP {resp.status_code}")
    return parse_skybot_json(resp.text, mjd)
