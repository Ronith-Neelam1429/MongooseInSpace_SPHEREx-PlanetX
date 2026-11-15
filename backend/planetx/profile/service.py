"""Build the object profile (GET /api/object/profile): fetch catalogues, then derive.

Two rounds of concurrent lookups inside one 30 s budget:
  1. Gaia DR3 star (by id, or by position) and SIMBAD (by name, Gaia id or position);
  2. what needs the star: Gaia astrophysical parameters, Gaia companions, and SIMBAD by
     Gaia id when round 1 found nothing in SIMBAD.
A service that fails or runs late becomes a sentence in `notes`, never an error.
Whole profiles are kept in memory for an hour (two minutes when a service failed).
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from astropy.time import Time

from planetx.api import schemas as s
from planetx.classify.catalogs import CatalogError
from planetx.observatories.catalogs import ArchiveError
from planetx.profile import fetchers
from planetx.profile import physics as ph

log = logging.getLogger(__name__)

TOTAL_BUDGET_S = 30.0
ROUND1_BUDGET_S = 16.0
CACHE_TTL_S = 3600.0
CACHE_TTL_PARTIAL_S = 120.0
CACHE_MAX = 256
# Wider than the 3" first planned: engine positions (and the site's Barnard view centre,
# 3.7" off) are only good to about half a 6.2" SPHEREx pixel.
MATCH_ARCSEC = 5.0
MATCH_FAST_ARCSEC = 10.0  # engine positions of fast movers are less certain
SIMBAD_MATCH_ARCSEC = 5.0
# Companion search: projected separation up to 0.5 pc, cone capped at 15'.
COMPANION_MAX_SEP_PC = 0.5
COMPANION_MAX_CONE_ARCSEC = 900.0
COMPANION_MIN_PM_MAS = 20.0  # slower: field stars share the motion by chance
COMPANION_LIMIT = 10
SIMBAD_URL = "https://simbad.harvard.edu/simbad"
VIZIER_URL = "https://vizier.cfa.harvard.edu/viz-bin/VizieR-4"

_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="profile")

_LABELS = {
    "gaia": "Gaia DR3 (IRSA)",
    "simbad": "SIMBAD",
    "simbad2": "SIMBAD",
    "params": "Gaia DR3 astrophysical parameters (VizieR)",
    "companions": "Gaia DR3 companion search (IRSA)",
}

# SIMBAD object types that are not single stars, in plain words.
_OTYPE_KINDS = {
    "G": "Galaxy", "GiG": "Galaxy", "GiC": "Galaxy", "GiP": "Galaxy", "BiC": "Galaxy", "IG": "Galaxy",
    "PaG": "Galaxy", "SBG": "Starburst galaxy", "LSB": "Galaxy", "bCG": "Galaxy", "EmG": "Galaxy",
    "H2G": "Galaxy", "rG": "Radio galaxy", "AGN": "Active galaxy", "SyG": "Active galaxy",
    "Sy1": "Active galaxy", "Sy2": "Active galaxy", "LIN": "Active galaxy", "QSO": "Quasar",
    "Bla": "Quasar (blazar)", "BLL": "Quasar (blazar)", "ClG": "Galaxy cluster", "GrG": "Galaxy group",
    "PN": "Planetary nebula", "HII": "Glowing gas cloud", "SNR": "Supernova remnant",
    "ISM": "Gas or dust cloud", "MoC": "Molecular cloud", "DNe": "Dark nebula", "RNe": "Reflection nebula",
    "Cld": "Gas or dust cloud", "Cl*": "Star cluster", "OpC": "Open star cluster",
    "GlC": "Globular star cluster", "As*": "Group of stars",
}
_STAR_KINDS = {"**": "Double star", "EB*": "Eclipsing binary star", "SB*": "Spectroscopic binary star",
               "Mi*": "Mira variable star", "C*": "Carbon star", "Y*O": "Young star", "TT*": "Young star",
               "PM*": "High proper-motion star"}
_CLASS_KINDS = {
    "white dwarf": "White dwarf", "red dwarf": "Red dwarf star", "sun-like star": "Sun-like star",
    "hot star": "Hot star", "giant": "Giant star", "supergiant": "Supergiant star",
    "brown dwarf": "Brown dwarf",
}


@dataclass(frozen=True)
class ProfileRequest:
    ra: float
    dec: float
    gaia_id: str | None = None
    name: str | None = None
    pmra: float | None = None
    pmdec: float | None = None
    pm_err: float | None = None
    mag: float | None = None

    @property
    def measured_pm(self) -> float | None:
        if self.pmra is None or self.pmdec is None:
            return None
        return math.hypot(self.pmra, self.pmdec)

    def key(self) -> tuple:
        def r(v, nd):
            return None if v is None else round(v, nd)
        return (r(self.ra, 4), r(self.dec, 4), self.gaia_id, (self.name or "").strip().lower() or None,
                r(self.pmra, 0), r(self.pmdec, 0), r(self.pm_err, 0), r(self.mag, 1))


# --- cache --------------------------------------------------------------------------

_cache: OrderedDict[tuple, tuple[float, s.ObjectProfile]] = OrderedDict()
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def get_profile(req: ProfileRequest) -> s.ObjectProfile:
    """Cached `build_profile`."""
    key = req.key()
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            _cache.move_to_end(key)
            return hit[1]
    profile, complete = build_profile(req)
    with _cache_lock:
        _cache[key] = (time.monotonic() + (CACHE_TTL_S if complete else CACHE_TTL_PARTIAL_S), profile)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)
    return profile


# --- fetching -------------------------------------------------------------------------


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return text[:160]


def _run(tasks: dict[str, Callable[[], Any]], budget_s: float, notes: list[str], failed: set[str],
         answered: set[str]) -> dict[str, Any]:
    """Run lookups concurrently; collect results, and notes for the ones that failed or ran late."""
    futures = {k: _pool.submit(fn) for k, fn in tasks.items()}
    done, _ = wait(futures.values(), timeout=max(0.1, budget_s))
    out: dict[str, Any] = {}
    for key, fut in futures.items():
        label = _LABELS[key]
        if fut not in done:
            fut.cancel()
            failed.add(key)
            notes.append(f"{label} did not answer in time.")
            continue
        try:
            out[key] = fut.result()
            answered.add(label)
        except (ArchiveError, CatalogError) as exc:
            failed.add(key)
            notes.append(f"{label} was unavailable ({_short(exc)}).")
        except ValueError as exc:
            failed.add(key)
            notes.append(f"{label} lookup was refused ({_short(exc)}).")
        except Exception:  # an unexpected bug must not turn the whole card into a 500
            log.exception("profile lookup %s failed", key)
            failed.add(key)
            notes.append(f"{label} lookup failed unexpectedly.")
    return out


# --- building ---------------------------------------------------------------------------


def build_profile(req: ProfileRequest, now_year: float | None = None) -> tuple[s.ObjectProfile, bool]:
    """The profile and whether every lookup answered (complete answers are cached longer)."""
    now = Time.now().jyear if now_year is None else now_year
    start = time.monotonic()
    notes: list[str] = []
    failed: set[str] = set()
    answered: set[str] = set()
    fast = (req.measured_pm or 0.0) >= fetchers.FAST_PM_MAS
    tol = MATCH_FAST_ARCSEC if fast else MATCH_ARCSEC

    round1: dict[str, Callable[[], Any]] = {}
    if req.gaia_id:
        round1["gaia"] = lambda: fetchers.fetch_gaia_by_id(req.gaia_id)
    else:
        round1["gaia"] = lambda: fetchers.find_gaia(req.ra, req.dec, tol, now)
    simbad_by_gaia_id = False
    if req.name:
        round1["simbad"] = lambda: fetchers.fetch_simbad_by_ident(req.name)
    elif req.gaia_id:
        simbad_by_gaia_id = True
        round1["simbad"] = lambda: fetchers.fetch_simbad_by_ident(f"Gaia DR3 {req.gaia_id}")
    else:
        round1["simbad"] = lambda: fetchers.find_simbad(req.ra, req.dec, max(SIMBAD_MATCH_ARCSEC, tol), now)
    r1 = _run(round1, ROUND1_BUDGET_S, notes, failed, answered)
    star: fetchers.GaiaStar | None = r1.get("gaia")
    simbad: fetchers.SimbadObject | None = r1.get("simbad")
    if "gaia" not in failed and star is None:
        notes.append(f"Gaia DR3 source {req.gaia_id} was not found." if req.gaia_id
                     else "No Gaia DR3 star matches this position.")
    if "simbad" not in failed and simbad is None and req.name:
        notes.append(f"SIMBAD does not know the name {req.name!r}.")

    # Distance and motion as far as round 1 knows them, for the companion search.
    pre_plx, pre_plx_err, pre_pm, pre_pos = _astrometry(star, simbad)

    round2: dict[str, Callable[[], Any]] = {}
    if star is not None:
        round2["params"] = lambda: fetchers.fetch_astro_params(star)
    companion_note = None
    radius = None
    if pre_plx is not None and pre_pm is not None:
        companion_note = _companion_precheck(pre_plx, pre_plx_err, pre_pm)
        if companion_note is None:
            radius, lo, hi = _companion_cone(pre_plx, pre_plx_err)
            round2["companions"] = lambda: fetchers.fetch_companion_candidates(pre_pos[0], pre_pos[1], radius, lo, hi)
    if simbad is None and star is not None and "simbad" not in failed and not simbad_by_gaia_id:
        round2["simbad2"] = lambda: fetchers.fetch_simbad_by_ident(f"Gaia DR3 {star.source_id}")
    r2 = _run(round2, TOTAL_BUDGET_S - (time.monotonic() - start), notes, failed, answered) if round2 else {}
    params: fetchers.AstroParams | None = r2.get("params")
    simbad = simbad or r2.get("simbad2")
    if star is not None and "params" not in failed and params is None:
        notes.append("Gaia has no astrophysical parameters for this star.")

    profile = assemble(req, now, star, simbad, params, r2.get("companions"),
                       companion_radius_arcsec=radius if "companions" in r2 else None,
                       companion_note=companion_note,
                       companion_failed="companions" in failed, notes=notes,
                       sources=[label for label in dict.fromkeys(_LABELS.values()) if label in answered])
    return profile, not failed


def _astrometry(star, simbad):
    """(parallax, error, (pmra, pmdec), (ra, dec) at 2016) from Gaia, else SIMBAD."""
    if star is not None and star.parallax is not None and star.pmra is not None and star.pmdec is not None:
        return star.parallax, star.parallax_error, (star.pmra, star.pmdec), (star.ra, star.dec)
    if simbad is not None and simbad.plx is not None and simbad.pmra is not None and simbad.pmdec is not None:
        pos = ph.position_at(simbad.ra, simbad.dec, simbad.pmra, simbad.pmdec, fetchers.SIMBAD_EPOCH,
                             fetchers.GAIA_EPOCH)
        return simbad.plx, simbad.plx_err, (simbad.pmra, simbad.pmdec), pos
    return None, None, None, None


def _companion_precheck(plx, plx_err, pm) -> str | None:
    if plx <= 0 or (plx_err and plx / plx_err < ph.GOOD_PARALLAX_SNR):
        return "Its distance is too uncertain to look for companions."
    if math.hypot(*pm) < COMPANION_MIN_PM_MAS:
        return "It moves too slowly to tell companions from unrelated stars."
    return None


def _companion_cone(plx: float, plx_err: float | None) -> tuple[float, float, float]:
    """Cone radius (arcsec) and loose parallax bounds (mas) for the Gaia companion query."""
    d_pc = 1000.0 / plx
    radius = min(COMPANION_MAX_CONE_ARCSEC, max(10.0, COMPANION_MAX_SEP_PC * ph.AU_PER_PC / d_pc))
    # Depth as large as the widest projected separation, plus 3 sigma with a generous error
    # for faint companions; the exact cut is made per star in `companions`.
    pad = 3.0 * math.hypot(plx_err or 0.0, 2.0) + plx * radius / ph.AU_PER_PC
    return radius, max(plx - pad, 0.0), plx + pad


def companions(candidates: list[fetchers.GaiaStar], self_id: str | None, ra: float, dec: float,
               plx: float, plx_err: float | None, pmra: float, pmdec: float,
               self_mass: float | None) -> list[s.Companion]:
    """Gaia stars at the same distance sharing the object's motion, closest first.

    Parallaxes agree within 3 sigma plus a depth as large as the projected separation; motions
    agree within 10 % (at least 3 mas/yr). "likely" when within 5 % (1.5 mas/yr) and 2 sigma.
    """
    pm = math.hypot(pmra, pmdec)
    d_pc = 1000.0 / plx
    out = []
    for c in candidates:
        if c.source_id == self_id or c.parallax is None or c.pmra is None or c.pmdec is None:
            continue
        sep = ph.separation_arcsec(ra, dec, c.ra, c.dec)
        if sep < 1.0:
            continue
        sigma = math.hypot(plx_err or 0.0, c.parallax_error or 0.0)
        depth = plx * sep / ph.AU_PER_PC
        dplx = abs(c.parallax - plx)
        dpm = math.hypot(c.pmra - pmra, c.pmdec - pmdec)
        if dplx > 3.0 * sigma + depth or dpm > max(0.10 * pm, 3.0):
            continue
        likely = dplx <= 2.0 * sigma + depth and dpm <= max(0.05 * pm, 1.5)
        sep_au = sep * d_pc
        period = None
        m2 = _companion_mass(c)
        if self_mass is not None and m2 is not None:
            period = ph.kepler_period_yr(sep_au, self_mass + m2)
        out.append(s.Companion(
            gaia_id=c.source_id, sep_arcsec=round(sep, 2), sep_au=round(sep_au, 0),
            g_mag=None if c.g is None else round(c.g, 2),
            period_yr=None if period is None else float(f"{period:.2g}"),
            bound="likely" if likely else "possible",
        ))
    out.sort(key=lambda x: x.sep_arcsec)
    return out[:COMPANION_LIMIT]


def _companion_mass(c: fetchers.GaiaStar) -> float | None:
    if c.g is None or c.parallax is None or c.parallax <= 0:
        return None
    abs_g = ph.abs_mag(c.g, 1000.0 / c.parallax)
    teff = c.teff_gspphot or ph.teff_from_bp_rp(c.bp_rp)
    cls, _ = ph.classify(abs_g, c.bp_rp, teff, None, None)
    lum = ph.luminosity_from_abs_g(abs_g, teff) if teff else None
    return ph.orbit_mass(cls, lum) if cls or lum else 0.5


# --- assembling ---------------------------------------------------------------------------


def _round(v: float | None, sig: int = 3) -> float | None:
    if v is None or not math.isfinite(v):
        return None
    return float(f"{v:.{sig}g}")


def _kind(simbad, cls) -> str | None:
    otype = simbad.otype if simbad else None
    if otype in _OTYPE_KINDS:
        return _OTYPE_KINDS[otype]
    if cls in _CLASS_KINDS:
        return _CLASS_KINDS[cls]
    if otype in _STAR_KINDS:
        return _STAR_KINDS[otype]
    if simbad and simbad.description:
        return simbad.description[0].upper() + simbad.description[1:]
    return "Star" if cls or otype else None


def _distance(star, simbad) -> ph.Distance | None:
    if star is not None:
        d = ph.distance_from_parallax(star.parallax, star.parallax_error, "gaia_parallax")
        if d:
            return d
    if simbad is not None:
        d = ph.distance_from_parallax(simbad.plx, simbad.plx_err, "simbad_parallax")
        if d:
            return d
    if star is not None and star.distance_gspphot:
        return ph.Distance(star.distance_gspphot, None, "photometric_estimate",
                           "estimate from Gaia's colours (GSP-Phot), not a parallax")
    return None


def assemble(req: ProfileRequest, now: float, star, simbad, params, candidates, *,
             companion_radius_arcsec: float | None, companion_note: str | None, companion_failed: bool,
             notes: list[str], sources: list[str]) -> s.ObjectProfile:
    """Turn the lookups into the profile (pure: no network)."""
    non_stellar = simbad is not None and simbad.otype in _OTYPE_KINDS
    ctx = ph.sky_context(req.ra, req.dec)

    # Distance.
    dist = _distance(star, simbad)
    distance = None
    if dist is not None:
        distance = s.ProfileDistance(
            pc=_round(dist.pc, 4), pc_err=_round(dist.pc_err, 2), ly=_round(dist.ly, 4),
            light_left_year=ph.light_left_year(dist.ly, now), source=dist.source, note=dist.note,
        )

    # Motion: Gaia, else SIMBAD, else what the page measured.
    pmra = pmdec = pm_err = None
    motion_source = ref_epoch = ref_pos = None
    if star is not None and star.pmra is not None and star.pmdec is not None:
        pmra, pmdec, motion_source = star.pmra, star.pmdec, "gaia"
        pm_err = ph.pm_total_err(pmra, pmdec, star.pmra_error, star.pmdec_error)
        ref_epoch, ref_pos = star.ref_epoch, (star.ra, star.dec)
    elif simbad is not None and simbad.pmra is not None and simbad.pmdec is not None:
        pmra, pmdec, motion_source = simbad.pmra, simbad.pmdec, "simbad"
        ref_epoch, ref_pos = fetchers.SIMBAD_EPOCH, (simbad.ra, simbad.dec)
    elif req.pmra is not None and req.pmdec is not None:
        pmra, pmdec, motion_source, pm_err = req.pmra, req.pmdec, "measured", req.pm_err
        ref_epoch, ref_pos = now, (req.ra, req.dec)

    rv = rv_err = None
    if star is not None and star.radial_velocity is not None:
        rv, rv_err = star.radial_velocity, star.radial_velocity_error
    elif simbad is not None and simbad.rv is not None and not non_stellar:
        rv, rv_err = simbad.rv, simbad.rv_err

    motion = None
    pm = v_tan = space = pop = None
    est = None
    uvw = None
    plx_mas = 1000.0 / dist.pc if dist is not None and dist.source != "photometric_estimate" else None
    if pmra is not None:
        pm = ph.pm_total(pmra, pmdec)
        v_tan_err = pop_basis = None
        if dist is not None:
            v_tan, v_tan_err = ph.tangential_velocity(pm, dist.pc, pm_err, dist.pc_err)
            if rv is not None:
                space = ph.space_velocity(v_tan, rv)
                uvw = ph.uvw(ref_pos[0], ref_pos[1], dist.pc, pmra, pmdec, rv)
                pop, v_lsr = ph.population(uvw)
                pop_basis = f"Toomre velocity {v_lsr:.0f} km/s relative to the local standard of rest"
        elif pm_err is None or pm >= 3.0 * pm_err:
            est = ph.estimated_distance_range(pm)
        motion = s.ProfileMotion(
            pmra=round(pmra, 3), pmdec=round(pmdec, 3), pm=round(pm, 3), pm_err=_round(pm_err, 3),
            pa_deg=round(ph.position_angle_deg(pmra, pmdec), 2), source=motion_source,
            tangential_km_s=_round(v_tan, 4), tangential_err=_round(v_tan_err, 2),
            radial_km_s=_round(rv, 5), radial_err=_round(rv_err, 2), space_km_s=_round(space, 4),
            uvw_km_s=None if uvw is None else [round(x, 1) for x in uvw],
            population=pop, population_basis=pop_basis,
            parallax_wobble_arcsec=None if plx_mas is None else _round(plx_mas / 1000.0, 4),
            estimated_distance_pc=None if est is None else [_round(est[0], 3), _round(est[1], 3)],
        )
        if motion_source == "measured":
            notes.append("The motion is the one measured on this page (a candidate), not a catalogue value.")

    # Physical properties.
    physical = None if non_stellar else _physical(star, simbad, params, dist, req.mag, notes)
    cls = physical.class_ if physical else None

    # Path across the sky.
    path = None
    if pmra is not None and pm > 0:
        pr = ph.sky_path(ref_pos[0], ref_pos[1], ref_epoch, pmra, pmdec,
                         dist.pc if dist is not None else None, rv, now)
        note = "Straight-line motion; real paths curve slowly around the Galaxy."
        if dist is not None and rv is None:
            note += " No radial velocity is known, so the path assumes none and gives no distances."
        if dist is None:
            note += " Without a distance this is the sky track only, over 2,000 years."
        path = s.ProfilePath(
            points=[s.PathPoint(year=y, ra=round(r, 5), dec=round(d, 5), dist_ly=_round(dl, 4))
                    for y, r, d, dl in pr.points],
            span_years=pr.span_years,
            closest_approach=None if pr.closest is None else s.ClosestApproach(
                year=pr.closest[0], dist_ly=_round(pr.closest[1], 3)),
            note=note,
        )

    # Gravity: companions and wobble.
    self_mass = None
    if physical is not None:
        self_mass = physical.mass_msun or ph.orbit_mass(cls, physical.luminosity_lsun)
    comps: list[s.Companion] = []
    if candidates and plx_mas is not None and pmra is not None:
        pos = ref_pos if motion_source == "gaia" else ph.position_at(
            ref_pos[0], ref_pos[1], pmra, pmdec, ref_epoch, fetchers.GAIA_EPOCH)
        comps = companions(candidates, star.source_id if star else None, pos[0], pos[1],
                           plx_mas, dist.pc_err * plx_mas**2 / 1000.0 if dist.pc_err else None,
                           pmra, pmdec, self_mass)
    ruwe = star.ruwe if star is not None else None
    wobble = ruwe is not None and ruwe > ph.RUWE_WOBBLE
    nss = star.non_single_star if star is not None else None
    gravity = s.ProfileGravity(
        summary=_gravity_summary(star, simbad, comps, companion_radius_arcsec, companion_note, companion_failed,
                                 ruwe, wobble, nss),
        companions=comps, ruwe=_round(ruwe, 3), wobble_hint=wobble, non_single_star=nss,
    )

    companion_text = None
    if comps:
        c = comps[0]
        orbit = f", orbiting every ~{c.period_yr:,.0f} years (estimate)" if c.period_yr else ""
        companion_text = (f"It moves together with Gaia DR3 {c.gaia_id}, {c.sep_au:,.0f} AU away: "
                          f"{'likely' if c.bound == 'likely' else 'possibly'} a companion{orbit}.")
    why = ph.why_moving(ph.WhyInput(
        pm_mas=pm, pm_err=pm_err, motion_source=motion_source,
        d_ly=dist.ly if dist is not None else None, distance_source=dist.source if dist else None,
        space_kms=space, tangential_kms=v_tan, population=pop, parallax_mas=plx_mas,
        estimated_pc=est, n_companions=len(comps), companion_text=companion_text, ruwe=ruwe,
    ))

    gaia_id = star.source_id if star is not None else req.gaia_id
    identity = s.ProfileIdentity(
        name=simbad.name if simbad else (req.name.strip() if req.name else None),
        simbad_type=simbad.otype if simbad else None,
        kind=_kind(simbad, cls),
        spectral_type=simbad.sp_type if simbad else None,
        gaia_id=gaia_id,
        constellation=ctx["constellation"],
        galactic_l=round(ctx["galactic_l"], 3), galactic_b=round(ctx["galactic_b"], 3),
        ecliptic_lat=round(ctx["ecliptic_lat"], 3),
    )
    return s.ObjectProfile(
        ra=req.ra, dec=req.dec, identity=identity, distance=distance, physical=physical, motion=motion,
        path=path, why_moving=[s.WhyMoving(factor=f, text=t) for f, t in why], gravity=gravity,
        links=_links(req, star, simbad), sources=sources, notes=notes,
    )


def _physical(star, simbad, params, dist, page_mag, notes) -> s.ProfilePhysical | None:
    sp = simbad.sp_type if simbad else None
    otype = simbad.otype if simbad else None
    bp_rp = star.bp_rp if star else None
    abs_g = None
    if dist is not None and star is not None and star.g is not None:
        abs_g = ph.abs_mag(star.g, dist.pc)
    elif dist is not None and page_mag is not None:
        abs_g = ph.abs_mag(page_mag, dist.pc)
        notes.append("Absolute magnitude uses the brightness the page supplied (its band may not be Gaia G).")

    teff = teff_source = None
    gsp = (params.teff_gspphot if params else None) or (star.teff_gspphot if star else None)
    sp_teff = ph.teff_from_spectral_type(sp)
    cool_type = bool(sp) and (sp.lstrip().startswith(("L", "T", "Y", "D")) or otype in ("BD*", "WD*"))
    if gsp:
        teff, teff_source = gsp, "Gaia GSP-Phot"
    elif params and params.teff_ucd:
        teff, teff_source = params.teff_ucd, "Gaia ESP-UCD"
    elif sp_teff and cool_type:
        teff, teff_source = sp_teff, "estimate from spectral type"
    elif ph.teff_from_bp_rp(bp_rp):
        teff, teff_source = ph.teff_from_bp_rp(bp_rp), "estimate from Gaia BP-RP colour"
    elif sp_teff:
        teff, teff_source = sp_teff, "estimate from spectral type"

    cls, basis = ph.classify(abs_g, bp_rp, teff, sp, otype)

    lum = lum_source = None
    if params and params.lum_flame:
        lum, lum_source = params.lum_flame, "Gaia FLAME"
    elif abs_g is not None and teff and teff >= 2300 and cls != "brown dwarf" and star is not None:
        lum = ph.luminosity_from_abs_g(abs_g, teff)
        lum_source = "estimate from absolute G magnitude and a bolometric correction"

    radius = radius_err = radius_source = None
    if params and params.radius_flame:
        radius, radius_source = params.radius_flame, "Gaia FLAME"
        if params.radius_flame_lo is not None and params.radius_flame_hi is not None:
            radius_err = (params.radius_flame_hi - params.radius_flame_lo) / 2.0
    elif params and params.radius_gspphot:
        radius, radius_source = params.radius_gspphot, "Gaia GSP-Phot"
    elif lum and teff:
        radius, radius_source = ph.radius_from_l_teff(lum, teff), "estimate from luminosity and temperature"
    elif cls == "brown dwarf":
        radius, radius_source = 0.1, "typical brown-dwarf size, about Jupiter's (rough)"

    mass = mass_source = None
    if params and params.mass_flame:
        mass, mass_source = params.mass_flame, "Gaia FLAME"
    elif cls in ("red dwarf", "sun-like star", "hot star") and lum:
        mass, mass_source = ph.mass_from_luminosity(lum), "estimate from luminosity (main-sequence relation)"
    elif cls == "white dwarf":
        mass, mass_source = ph.TYPICAL_MASS["white dwarf"], "typical white-dwarf mass (rough)"

    if all(v is None for v in (teff, cls, abs_g, radius, lum)):
        return None
    return s.ProfilePhysical(
        class_=cls, class_basis=basis, teff_k=None if teff is None else round(teff),
        teff_source=teff_source, color_hex=ph.blackbody_hex(teff),
        radius_rsun=_round(radius, 3), radius_rsun_err=_round(radius_err, 2), radius_source=radius_source,
        radius_km=None if radius is None else float(f"{radius * ph.R_SUN_KM:.3g}"),
        mass_msun=_round(mass, 3), mass_source=mass_source,
        luminosity_lsun=_round(lum, 3), luminosity_source=lum_source,
        age_gyr=_round(params.age_flame, 3) if params else None,
        abs_mag_g=None if abs_g is None else round(abs_g, 2),
        size_comparison=[] if radius is None else [
            s.SizeComparison(thing=t, ratio=_round(r, 3)) for t, r in ph.size_comparison(radius)],
    )


def _gravity_summary(star, simbad, comps, searched_arcsec, precheck_note, search_failed, ruwe, wobble, nss) -> str:
    parts = []
    if comps:
        c = comps[0]
        n = len(comps)
        bound = "is likely bound to it" if c.bound == "likely" else "may be bound to it"
        orbit = f" (orbit roughly {c.period_yr:,.0f} years)" if c.period_yr else ""
        parts.append(f"Gaia shows {n} star{'s' if n > 1 else ''} moving with it at the same distance; "
                     f"the closest, {c.sep_au:,.0f} AU away, {bound}{orbit}.")
    elif searched_arcsec is not None:
        within = f"{searched_arcsec / 60:.0f}'" if searched_arcsec >= 60 else f'{searched_arcsec:.0f}"'
        if wobble:
            parts.append(f"No star moving with it was found in Gaia within {within}.")
        else:
            parts.append(f"No companion found in Gaia within {within}; its path is set by the Galaxy's "
                         "overall pull.")
    elif search_failed:
        parts.append("The Gaia companion search did not answer, so companions are unknown.")
    elif precheck_note:
        parts.append(precheck_note)
    elif star is None and simbad is None:
        parts.append("No catalogue data, so companions and wobble could not be checked.")
    else:
        parts.append("Without a good distance and motion, companions could not be checked.")
    if wobble:
        parts.append(f"Gaia's position fit is poor (RUWE {ruwe:.1f}), which suggests an unseen companion.")
    if nss:
        parts.append("Gaia lists it as a non-single star (it fitted a binary orbit).")
    return " ".join(parts)


def _links(req, star, simbad) -> list[s.ProfileLink]:
    links = []
    if simbad is not None:
        links.append(s.ProfileLink(label="SIMBAD", url=f"{SIMBAD_URL}/sim-id?{urlencode({'Ident': simbad.main_id})}"))
    else:
        links.append(s.ProfileLink(label="SIMBAD (around this position)", url=f"{SIMBAD_URL}/sim-coo?" + urlencode(
            {"Coord": f"{req.ra:.6f} {req.dec:+.6f}", "Radius": "10", "Radius.unit": "arcsec"})))
    if star is not None:
        links.append(s.ProfileLink(label="Gaia DR3 (VizieR)", url=f"{VIZIER_URL}?" + urlencode(
            {"-source": "I/355/gaiadr3", "-c": f"{star.ra:.6f} {star.dec:+.6f}", "-c.rs": "2"})))
    if simbad is not None:
        q = quote(f'object:"{simbad.main_id}"', safe="")
        links.append(s.ProfileLink(label="Papers (NASA ADS)", url=f"https://ui.adsabs.harvard.edu/search/q={q}"))
    links.append(s.ProfileLink(label="ESASky", url="https://sky.esa.int/esasky/?" + urlencode(
        {"target": f"{req.ra:.6f} {req.dec:+.6f}", "fov": "0.1"})))
    return links


def shutdown() -> None:
    _pool.shutdown(wait=False, cancel_futures=True)
