"""Physics for the object profile: pure functions, no network.

Everything here turns catalogue numbers into the quantities the profile shows
(distance, speeds, Galactic population, temperature colour, size, orbit periods,
sky path) and into plain sentences. Anything derived from rough relations is an
estimate, and the callers label it so.

Conventions: proper motions in mas/yr (RA component includes cos dec), parallax in
mas, distances in pc, velocities in km/s, years as Julian years.
"""
from __future__ import annotations

import math
import re
import warnings
from dataclasses import dataclass

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.time import Time

# km/s per (arcsec/yr x pc).
K_KMS = 4.740470446
LY_PER_PC = 3.261563777
AU_PER_PC = 206264.806
R_SUN_KM = 695_700.0
R_JUPITER_KM = 69_911.0
R_EARTH_KM = 6_371.0
T_SUN_K = 5772.0
M_BOL_SUN = 4.74
# Solar motion relative to the local standard of rest (Schoenrich, Binney & Dehnen 2010), km/s.
SUN_UVW_LSR = (11.1, 12.24, 7.25)
# Toomre-diagram cuts on the total velocity relative to the LSR (km/s).
THIN_DISK_MAX_KMS = 85.0
THICK_DISK_MAX_KMS = 180.0
# Typical speed of a nearby star relative to the Sun, for "N times faster" sentences.
TYPICAL_STAR_KMS = 20.0
# Assumed speed range for objects with a motion but no distance.
ASSUMED_SPEED_KMS = (20.0, 100.0)
# Distance quality: parallax / error at or above this is a good distance.
GOOD_PARALLAX_SNR = 5.0
# A motion below this (mas/yr) is "drifting", far too slow for SPHEREx to see.
SLOW_PM_MAS = 100.0
SPHEREX_PIXEL_ARCSEC = 6.2
RUWE_WOBBLE = 1.4

PATH_POINTS = 41
PATH_SPAN_WITH_DISTANCE_YR = 20_000
PATH_SPAN_NO_DISTANCE_YR = 2_000
_KMS_TO_PC_PER_YR = (1.0 * u.km / u.s).to(u.pc / u.yr).value

# astropy's constellation list has a few misspellings.
_CONSTELLATION_FIXES = {
    "Ophiucus": "Ophiuchus",
    "Chamaleon": "Chamaeleon",
    "Pisces Austrinus": "Piscis Austrinus",
}


# --- position ------------------------------------------------------------------


def sky_context(ra: float, dec: float) -> dict[str, float | str]:
    """Constellation, Galactic l/b and ecliptic latitude of a position (deg)."""
    c = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame="icrs")
    name = str(c.get_constellation()).strip()
    g = c.galactic
    return {
        "constellation": _CONSTELLATION_FIXES.get(name, name),
        "galactic_l": float(g.l.deg),
        "galactic_b": float(g.b.deg),
        "ecliptic_lat": float(c.barycentrictrueecliptic.lat.deg),
    }


def position_at(ra: float, dec: float, pmra: float, pmdec: float, epoch: float, year: float) -> tuple[float, float]:
    """Linear (small-angle) proper-motion propagation; fine over a few decades except at the poles."""
    dt = year - epoch
    cosd = max(math.cos(math.radians(dec)), 1e-6)
    new_dec = dec + pmdec * dt / 3.6e6
    return (ra + pmra * dt / 3.6e6 / cosd) % 360.0, max(-90.0, min(90.0, new_dec))


def track_distance_arcsec(
    ra: float, dec: float, star_ra: float, star_dec: float, pmra: float | None, pmdec: float | None,
    epoch: float, t0: float, t1: float,
) -> float:
    """Closest distance (arcsec) between (ra, dec) and a star's track between years t0 and t1."""
    pmra, pmdec = pmra or 0.0, pmdec or 0.0
    a = position_at(star_ra, star_dec, pmra, pmdec, epoch, t0)
    b = position_at(star_ra, star_dec, pmra, pmdec, epoch, t1)
    cosd = math.cos(math.radians(dec))

    def xy(p):
        return (((p[0] - ra + 180.0) % 360.0 - 180.0) * cosd * 3600.0, (p[1] - dec) * 3600.0)

    (ax, ay), (bx, by) = xy(a), xy(b)
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    s = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
    return math.hypot(ax + s * dx, ay + s * dy)


def separation_arcsec(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    h = math.sin((d2 - d1) / 2) ** 2 + math.cos(d1) * math.cos(d2) * math.sin((r2 - r1) / 2) ** 2
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(h)))) * 3600.0


# --- distance and motion ---------------------------------------------------------


@dataclass(frozen=True)
class Distance:
    pc: float
    pc_err: float | None
    source: str  # gaia_parallax | simbad_parallax | photometric_estimate
    note: str | None = None

    @property
    def ly(self) -> float:
        return self.pc * LY_PER_PC


def distance_from_parallax(plx_mas: float | None, err_mas: float | None, source: str) -> Distance | None:
    """1/parallax with first-order error. None for a missing, negative or < 3 sigma parallax.

    3-5 sigma parallaxes are kept with a note: 1/parallax is then biased and its error lopsided.
    """
    if plx_mas is None or not math.isfinite(plx_mas) or plx_mas <= 0:
        return None
    snr = plx_mas / err_mas if err_mas and err_mas > 0 else None
    if snr is not None and snr < 3.0:
        return None
    note = None
    if snr is not None and snr < GOOD_PARALLAX_SNR:
        note = f"parallax only {snr:.1f} sigma: distance uncertain"
    elif snr is None:
        note = "parallax has no quoted error"
    pc = 1000.0 / plx_mas
    pc_err = 1000.0 * err_mas / plx_mas**2 if err_mas and err_mas > 0 else None
    return Distance(pc, pc_err, source, note)


def light_left_year(ly: float, now_year: float) -> int:
    """Year the light we receive now left the object (negative = BCE)."""
    return int(round(now_year - ly))


def pm_total(pmra: float, pmdec: float) -> float:
    return math.hypot(pmra, pmdec)


def pm_total_err(pmra: float, pmdec: float, e_ra: float | None, e_dec: float | None) -> float | None:
    if e_ra is None or e_dec is None:
        return None
    pm = pm_total(pmra, pmdec)
    if pm == 0:
        return math.hypot(e_ra, e_dec) / math.sqrt(2)
    return math.hypot(pmra * e_ra, pmdec * e_dec) / pm


def position_angle_deg(pmra: float, pmdec: float) -> float:
    """Direction of motion, degrees east of north."""
    return math.degrees(math.atan2(pmra, pmdec)) % 360.0


def tangential_velocity(pm_mas: float, d_pc: float, pm_err: float | None = None,
                        d_err: float | None = None) -> tuple[float, float | None]:
    """Speed across the line of sight, 4.74 x mu x d (km/s), with first-order error."""
    v = K_KMS * pm_mas / 1000.0 * d_pc
    if pm_err is None and d_err is None:
        return v, None
    e = math.hypot(K_KMS * d_pc * (pm_err or 0.0) / 1000.0, K_KMS * pm_mas / 1000.0 * (d_err or 0.0))
    return v, e


def space_velocity(v_tan: float, v_rad: float) -> float:
    return math.hypot(v_tan, v_rad)


def _coord(ra, dec, d_pc, pmra, pmdec, rv, epoch) -> SkyCoord:
    return SkyCoord(
        ra=ra * u.deg, dec=dec * u.deg, distance=d_pc * u.pc,
        pm_ra_cosdec=pmra * u.mas / u.yr, pm_dec=pmdec * u.mas / u.yr,
        radial_velocity=rv * u.km / u.s, obstime=Time(epoch, format="jyear", scale="tdb"), frame="icrs",
    )


def uvw(ra: float, dec: float, d_pc: float, pmra: float, pmdec: float, rv: float) -> tuple[float, float, float]:
    """Heliocentric Galactic velocity (U toward the Galactic centre, V along rotation, W north), km/s."""
    v = _coord(ra, dec, d_pc, pmra, pmdec, rv, 2016.0).galactic.velocity
    return float(v.d_x.to_value(u.km / u.s)), float(v.d_y.to_value(u.km / u.s)), float(v.d_z.to_value(u.km / u.s))


def population(uvw_kms: tuple[float, float, float]) -> tuple[str, float]:
    """Galactic population from the total velocity relative to the LSR (Toomre-diagram cuts)."""
    v_lsr = math.sqrt(sum((a + b) ** 2 for a, b in zip(uvw_kms, SUN_UVW_LSR)))
    if v_lsr < THIN_DISK_MAX_KMS:
        return "thin disk", v_lsr
    if v_lsr < THICK_DISK_MAX_KMS:
        return "thick disk", v_lsr
    return "halo", v_lsr


def estimated_distance_range(pm_mas: float) -> tuple[float, float] | None:
    """Distance (pc) at which this proper motion means 20 and 100 km/s across the sky."""
    if pm_mas <= 0:
        return None
    mu = pm_mas / 1000.0
    lo, hi = (v / (K_KMS * mu) for v in ASSUMED_SPEED_KMS)
    return lo, hi


# --- stellar physics ---------------------------------------------------------------

# Dwarf sequence, BP-RP -> Teff (after Pecaut & Mamajek's table; rough, for estimates only).
_BP_RP_TEFF = (
    (-0.33, 31400), (-0.25, 20600), (-0.16, 15700), (-0.08, 12500), (0.00, 9700), (0.20, 8180),
    (0.38, 7220), (0.59, 6510), (0.76, 5920), (0.82, 5770), (0.98, 5270), (1.10, 5040),
    (1.43, 4440), (1.65, 4050), (1.84, 3850), (2.18, 3560), (2.43, 3430), (2.83, 3210),
    (3.27, 3060), (4.10, 2810), (4.40, 2680), (4.60, 2570), (4.80, 2380), (5.00, 2250),
)


def teff_from_bp_rp(bp_rp: float | None) -> float | None:
    """Dwarf-sequence temperature from Gaia BP-RP (no reddening correction); None off the table."""
    if bp_rp is None or not math.isfinite(bp_rp):
        return None
    xs = [p[0] for p in _BP_RP_TEFF]
    if not xs[0] - 0.05 <= bp_rp <= xs[-1] + 0.05:
        return None
    return float(np.interp(bp_rp, xs, [p[1] for p in _BP_RP_TEFF]))


# Spectral class -> Teff at subtype 0 and 5 (dwarfs; L/T/Y after Kirkpatrick et al.).
_SPECTRAL_TEFF = {
    "O": (45000, 41000), "B": (31400, 15700), "A": (9700, 8180), "F": (7220, 6510),
    "G": (5920, 5660), "K": (5270, 4440), "M": (3850, 3060), "L": (2250, 1650),
    "T": (1250, 1050), "Y": (450, 320),
}
_CLASS_ORDER = "OBAFGKMLTY"
_SPTYPE = re.compile(r"^\s*(?:sd|esd|usd|d)?([OBAFGKMLTY])(\d(?:\.\d+)?)?")
_WD_TYPE = re.compile(r"^\s*D[ABOQZCX]+(\d+(?:\.\d+)?)?")


def teff_from_spectral_type(sp: str | None) -> float | None:
    """Rough temperature from a spectral type ("M4V", "T6", "Y2", "DA2.5")."""
    if not sp:
        return None
    wd = _WD_TYPE.match(sp)
    if wd:
        # White-dwarf temperature index: number = 50,400 K / Teff.
        return 50400.0 / float(wd.group(1)) if wd.group(1) and float(wd.group(1)) > 0 else None
    m = _SPTYPE.match(sp)
    if not m:
        return None
    letter, sub = m.group(1), float(m.group(2)) if m.group(2) else 5.0
    t0, t5 = _SPECTRAL_TEFF[letter]
    nxt = _CLASS_ORDER.index(letter) + 1
    t10 = _SPECTRAL_TEFF[_CLASS_ORDER[nxt]][0] if nxt < len(_CLASS_ORDER) else t5 - (t0 - t5)
    teff = t0 + (t5 - t0) * sub / 5.0 if sub <= 5 else t5 + (t10 - t5) * (sub - 5.0) / 5.0
    return max(float(teff), 150.0)


def blackbody_hex(teff: float | None) -> str | None:
    """Approximate sRGB colour of a blackbody (Helland's fit; clamped to 1,000-40,000 K)."""
    if teff is None or not math.isfinite(teff) or teff <= 0:
        return None
    t = min(max(teff, 1000.0), 40000.0) / 100.0
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * (t - 60) ** -0.1332047592
        g = 288.1221695283 * (t - 60) ** -0.0755148492
        b = 255.0
    rgb = [int(round(min(255.0, max(0.0, c)))) for c in (r, g, b)]
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def abs_mag(app_mag: float, d_pc: float) -> float:
    return app_mag - 5.0 * math.log10(d_pc / 10.0)


def bolometric_correction_g(teff: float) -> float:
    """Gaia G bolometric correction (Andrae et al. 2018 polynomial), Teff clamped to 3300-8000 K."""
    t = min(max(teff, 3300.0), 8000.0)
    a = ((6.000e-02, 6.731e-05, -6.647e-08, 2.859e-11, -7.197e-15) if t >= 4000.0
         else (1.749e00, 1.977e-03, 3.737e-07, -8.966e-11, -4.183e-14))
    x = t - T_SUN_K
    return sum(c * x**i for i, c in enumerate(a))


def luminosity_from_abs_g(abs_g: float, teff: float) -> float:
    """L / L_sun from absolute G and a bolometric correction (no extinction: a lower limit, roughly)."""
    return 10.0 ** (-0.4 * (abs_g + bolometric_correction_g(teff) - M_BOL_SUN))


def radius_from_l_teff(lum_lsun: float, teff: float) -> float:
    """Stefan-Boltzmann: R / R_sun = sqrt(L) x (T_sun / T)^2."""
    return math.sqrt(lum_lsun) * (T_SUN_K / teff) ** 2


def mass_from_luminosity(lum_lsun: float) -> float:
    """Main-sequence mass-luminosity relation (L ~ 0.23 M^2.3, M^4, 1.4 M^3.5), M_sun."""
    if lum_lsun < 0.23 * 0.43**2.3:
        return (lum_lsun / 0.23) ** (1 / 2.3)
    if lum_lsun < 16.0:
        return lum_lsun**0.25
    return (lum_lsun / 1.4) ** (1 / 3.5)


# Typical masses (M_sun) for orbit-period estimates when nothing better is known.
TYPICAL_MASS = {"white dwarf": 0.6, "brown dwarf": 0.05, "giant": 1.5, "supergiant": 15.0}


def orbit_mass(cls: str | None, lum_lsun: float | None) -> float | None:
    """A mass good enough for a Kepler period: main-sequence relation, else a typical value."""
    if cls in TYPICAL_MASS:
        return TYPICAL_MASS[cls]
    if lum_lsun is not None and lum_lsun > 0:
        return mass_from_luminosity(lum_lsun)
    return None


def kepler_period_yr(sep_au: float, total_mass_msun: float) -> float:
    """Kepler III, taking the projected separation as the semi-major axis (an estimate)."""
    return math.sqrt(sep_au**3 / total_mass_msun)


_LUM_CLASS = re.compile(r"(?<![IV])(Ia|Iab|Ib|I|II|III|IV|V)(?![IV])")


def classify(abs_g: float | None, bp_rp: float | None, teff: float | None,
             sp_type: str | None, otype: str | None) -> tuple[str | None, str | None]:
    """Plain class of a star and what decided it; (None, None) when nothing does."""
    sp = (sp_type or "").strip()
    if otype in ("BD*", "BD?") or re.match(r"^\s*(?:sd)?[LTY]\d", sp):
        return "brown dwarf", "spectral type" if sp else "SIMBAD object type"
    if otype in ("WD*", "WD?") or _WD_TYPE.match(sp):
        return "white dwarf", "spectral type" if _WD_TYPE.match(sp) else "SIMBAD object type"
    if re.match(r"^\s*[OBAFGKM]", sp):
        lum = _LUM_CLASS.search(sp[1:])
        if lum and lum.group(1) in ("Ia", "Iab", "Ib", "I"):
            return "supergiant", "spectral type"
        if lum and lum.group(1) in ("II", "III"):
            return "giant", "spectral type"
    if abs_g is not None and bp_rp is not None:
        if bp_rp < 1.7 and abs_g > 10.0 + 2.6 * bp_rp:
            return "white dwarf", "absolute magnitude and colour"
        if abs_g < -3.5:
            return "supergiant", "absolute magnitude and colour"
        if bp_rp > 0.8 and abs_g < 1.72 + 3.6 * bp_rp - 3.0:
            return "giant", "absolute magnitude and colour"
        t = teff if teff is not None else teff_from_bp_rp(bp_rp)
        if t is not None:
            return _class_from_teff(t), "absolute magnitude and colour"
    if teff is not None:
        return _class_from_teff(teff), "temperature"
    return None, None


def _class_from_teff(teff: float) -> str:
    if teff < 2300:
        return "brown dwarf"
    if teff < 4000:
        return "red dwarf"
    if teff < 7500:
        return "sun-like star"
    return "hot star"


def size_comparison(radius_rsun: float) -> list[tuple[str, float]]:
    """Object diameter over the Sun's, Jupiter's and Earth's."""
    km = radius_rsun * R_SUN_KM
    return [("Sun", km / R_SUN_KM), ("Jupiter", km / R_JUPITER_KM), ("Earth", km / R_EARTH_KM)]


# --- sky path ------------------------------------------------------------------------


@dataclass(frozen=True)
class PathResult:
    points: list[tuple[int, float, float, float | None]]  # (year, ra, dec, dist_ly)
    span_years: int
    closest: tuple[int, float] | None  # (year, dist_ly)


def path_years(now_year: float, span: int, n: int = PATH_POINTS) -> list[int]:
    start = int(round(now_year)) - span // 2
    return [start + round(i * span / (n - 1)) for i in range(n)]


def sky_path(ra: float, dec: float, epoch: float, pmra: float, pmdec: float,
             d_pc: float | None, rv: float | None, now_year: float) -> PathResult:
    """Straight-line space motion across the sky, centred on now.

    With a distance: astropy `apply_space_motion` over 20,000 years (radial velocity 0 when
    unknown, and then no distances along the path). Without one: a great circle at the
    proper motion over 2,000 years.
    """
    if d_pc is not None:
        span = PATH_SPAN_WITH_DISTANCE_YR
        years = path_years(now_year, span)
        c = _coord(ra, dec, d_pc, pmra, pmdec, rv if rv is not None else 0.0, epoch)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # ERFA "dubious year" for dates far from now
            moved = c.apply_space_motion(dt=(np.asarray(years, float) - epoch) * u.yr)
        ras, decs = moved.ra.deg, moved.dec.deg
        dists = moved.distance.to_value(u.lyr) if rv is not None else [None] * len(years)
        points = [(y, float(r), float(d), None if dl is None else float(dl))
                  for y, r, d, dl in zip(years, ras, decs, dists)]
        closest = closest_approach(d_pc, pmra, pmdec, rv, epoch) if rv is not None else None
        return PathResult(points, span, closest)
    span = PATH_SPAN_NO_DISTANCE_YR
    years = path_years(now_year, span)
    start = SkyCoord(ra=ra * u.deg, dec=dec * u.deg)
    pm = pm_total(pmra, pmdec)
    pa = position_angle_deg(pmra, pmdec)
    dts = np.asarray(years, float) - epoch
    moved = start.directional_offset_by(
        np.where(dts >= 0, pa, (pa + 180.0) % 360.0) * u.deg, np.abs(dts) * pm * u.mas,
    )
    points = [(y, float(r), float(d), None) for y, r, d in zip(years, moved.ra.deg, moved.dec.deg)]
    return PathResult(points, span, None)


def closest_approach(d_pc: float, pmra: float, pmdec: float, rv: float, epoch: float) -> tuple[int, float]:
    """Year and distance (ly) of closest approach to the Sun on a straight line."""
    v_tan, _ = tangential_velocity(pm_total(pmra, pmdec), d_pc)
    v2 = v_tan**2 + rv**2
    if v2 == 0:
        return int(round(epoch)), d_pc * LY_PER_PC
    # r(t) = r0 + v t, with r0 = d along the line of sight; t_min = -d v_r / v^2.
    t_min_yr = -d_pc * rv / v2 / _KMS_TO_PC_PER_YR
    d_min = d_pc * v_tan / math.sqrt(v2)
    return int(round(epoch + t_min_yr)), d_min * LY_PER_PC


# --- plain sentences ---------------------------------------------------------------------


def _ly_words(ly: float) -> str:
    return f"{ly:.1f} light-years" if ly < 10 else f"{ly:,.0f} light-years"


def _arcsec_words(arcsec: float) -> str:
    return f'{arcsec:.2f}"' if arcsec >= 0.1 else f'{arcsec * 1000:.1f} mas'


@dataclass(frozen=True)
class WhyInput:
    pm_mas: float | None = None
    pm_err: float | None = None
    motion_source: str | None = None  # gaia | simbad | measured
    d_ly: float | None = None
    distance_source: str | None = None
    space_kms: float | None = None
    tangential_kms: float | None = None
    population: str | None = None
    parallax_mas: float | None = None
    estimated_pc: tuple[float, float] | None = None
    n_companions: int = 0
    companion_text: str | None = None
    ruwe: float | None = None


def why_moving(w: WhyInput) -> list[tuple[str, str]]:
    """Ordered (factor, sentence) pairs explaining the object's motion; always at least one."""
    out: list[tuple[str, str]] = []
    significant = w.pm_mas is not None and (w.pm_err is None or w.pm_mas >= 3.0 * w.pm_err)
    if w.pm_mas is None:
        out.append(("unknown", "No motion across the sky has been measured for it yet."))
    elif not significant:
        far = f" At {_ly_words(w.d_ly)}, even a fast object would barely shift." if w.d_ly else ""
        out.append(("static", "It shows no measurable motion across our sky." + far))
    elif w.pm_mas < SLOW_PM_MAS:
        years = SPHEREX_PIXEL_ARCSEC / (w.pm_mas / 1000.0)
        far = f" It is {_ly_words(w.d_ly)} away, so its real speed hardly shows." if w.d_ly and w.d_ly > 300 else ""
        out.append(("static", f"It drifts only {w.pm_mas:.1f} mas a year, so it would take about "
                              f"{years:,.0f} years to cross one SPHEREx pixel.{far}"))
    else:
        if w.motion_source == "measured":
            out.append(("unknown", f"SPHEREx measured it moving {w.pm_mas / 1000:.2f}\" a year; until other "
                                   "telescopes agree this stays a candidate motion."))
        if w.d_ly is not None:
            if w.d_ly < 33:
                out.append(("closeness", f"It is only {_ly_words(w.d_ly)} away, so even ordinary speed "
                                         "shifts it fast across our sky."))
            elif w.d_ly < 330:
                out.append(("closeness", f"It is fairly close, {_ly_words(w.d_ly)} away, which helps its "
                                         "motion show."))
            else:
                out.append(("closeness", f"It is {_ly_words(w.d_ly)} away, so to shift this fast it must "
                                         "be moving very quickly."))
        elif w.estimated_pc is not None:
            lo, hi = (p * LY_PER_PC for p in w.estimated_pc)
            out.append(("unknown", f"Its distance is unknown. If it moves like a typical star "
                                   f"({ASSUMED_SPEED_KMS[0]:.0f}-{ASSUMED_SPEED_KMS[1]:.0f} km/s), it is "
                                   f"roughly {lo:,.0f}-{hi:,.0f} light-years away: an estimate."))
        speed = w.space_kms if w.space_kms is not None else w.tangential_kms
        if speed is not None:
            what = "travels" if w.space_kms is not None else "moves across our line of sight at"
            ratio = speed / TYPICAL_STAR_KMS
            if ratio >= 2:
                out.append(("speed", f"It also {what} {speed:.0f} km/s relative to the Sun, about "
                                     f"{ratio:.0f} times faster than typical nearby stars."))
            else:
                out.append(("speed", f"Its speed, {speed:.0f} km/s relative to the Sun, is ordinary "
                                     "for a star near us."))
        if w.population == "thin disk":
            out.append(("population", "Its speed fits the Galaxy's thin disk, where the Sun lives too."))
        elif w.population == "thick disk":
            out.append(("population", "Speed like that suggests an old star from the Galaxy's thick disk."))
        elif w.population == "halo":
            out.append(("population", "Speed like that suggests a very old star from the Galaxy's halo, "
                                      "crossing the disk on a steep orbit."))
        if w.population is not None:
            out.append(("orbit", "Like the Sun, it circles the centre of the Milky Way; the motion we see "
                                 "is the difference between its orbit and ours."))
    if w.parallax_mas is not None and w.parallax_mas >= 10.0:
        lead = "On top of that it" if out and out[0][0] not in ("static", "unknown") else "It"
        out.append(("parallax", f"{lead} wobbles {_arcsec_words(w.parallax_mas / 1000.0)} back and forth "
                                "each year because Earth orbits the Sun."))
    if w.companion_text:
        out.append(("companion", w.companion_text))
    if w.ruwe is not None and w.ruwe > RUWE_WOBBLE:
        out.append(("companion", f"Gaia's fit of its position is poor (RUWE {w.ruwe:.1f}), which suggests "
                                 "an unseen companion tugging it."))
    return out
