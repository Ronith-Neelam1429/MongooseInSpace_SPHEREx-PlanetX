"""Offline tests for the object-profile physics (planetx.profile.physics): no network."""
import math

import pytest
from planetx.profile import physics as ph

# Barnard's Star, Gaia DR3 (epoch 2016.0), as given in the task brief.
B_RA, B_DEC = 269.44850252, 4.73942005
B_PLX, B_PMRA, B_PMDEC, B_RV = 546.98, -802.8, 10362.5, -110.6


def test_barnard_distance_and_speeds():
    d = ph.distance_from_parallax(B_PLX, 0.04, "gaia_parallax")
    assert d.pc == pytest.approx(1.828, abs=0.001)
    assert d.ly == pytest.approx(5.96, abs=0.01)
    assert d.note is None
    pm = ph.pm_total(B_PMRA, B_PMDEC)
    v_tan, _ = ph.tangential_velocity(pm, d.pc)
    assert v_tan == pytest.approx(90.1, abs=0.3)
    assert ph.space_velocity(v_tan, B_RV) == pytest.approx(142.6, abs=0.5)
    assert ph.position_angle_deg(B_PMRA, B_PMDEC) == pytest.approx(355.6, abs=0.1)


def test_barnard_closest_approach_is_3_8_ly_around_year_11800():
    d_pc = 1000.0 / B_PLX
    year, dist_ly = ph.closest_approach(d_pc, B_PMRA, B_PMDEC, B_RV, 2016.0)
    assert 11_500 <= year <= 12_000
    assert dist_ly == pytest.approx(3.77, abs=0.05)


def test_barnard_path_matches_the_analytic_closest_approach():
    path = ph.sky_path(B_RA, B_DEC, 2016.0, B_PMRA, B_PMDEC, 1000.0 / B_PLX, B_RV, now_year=2026.9)
    assert len(path.points) == ph.PATH_POINTS and path.span_years == 20_000
    years = [p[0] for p in path.points]
    assert years == sorted(years) and years[0] < 2027 < years[-1]
    nearest = min(p[3] for p in path.points)
    assert nearest == pytest.approx(path.closest[1], abs=0.05)  # sampled every 500 yr near the minimum
    # Moving north: Dec grows along the path.
    assert path.points[-1][2] > path.points[0][2] + 30


def test_barnard_is_a_thick_disk_star():
    uvw = ph.uvw(B_RA, B_DEC, 1000.0 / B_PLX, B_PMRA, B_PMDEC, B_RV)
    assert uvw[0] == pytest.approx(-141.4, abs=1.5)  # U toward the Galactic centre: Barnard moves away from it
    assert uvw[2] == pytest.approx(18.4, abs=1.5)
    pop, v_lsr = ph.population(uvw)
    assert pop == "thick disk" and 85 < v_lsr < 180


def test_population_cuts():
    sun_lsr = tuple(-v for v in ph.SUN_UVW_LSR)  # at rest relative to the LSR
    assert ph.population(sun_lsr) == ("thin disk", pytest.approx(0.0, abs=1e-9))
    assert ph.population((sun_lsr[0], sun_lsr[1] - 250.0, sun_lsr[2]))[0] == "halo"


def test_distance_from_parallax_quality():
    assert ph.distance_from_parallax(None, None, "gaia_parallax") is None
    assert ph.distance_from_parallax(-1.0, 0.5, "gaia_parallax") is None
    assert ph.distance_from_parallax(1.0, 0.5, "gaia_parallax") is None  # 2 sigma
    weak = ph.distance_from_parallax(2.0, 0.5, "gaia_parallax")  # 4 sigma
    assert weak.pc == 500.0 and "uncertain" in weak.note
    assert ph.distance_from_parallax(10.0, 0.1, "simbad_parallax").pc_err == pytest.approx(1.0)


def test_light_left_year_can_be_bce():
    assert ph.light_left_year(5.96, 2026.9) == 2021
    assert ph.light_left_year(10_000.0, 2026.0) == -7974


def test_estimated_distance_range_for_motion_only():
    lo, hi = ph.estimated_distance_range(1000.0)  # 1"/yr
    assert lo == pytest.approx(20 / 4.74047, rel=1e-4) and hi == pytest.approx(100 / 4.74047, rel=1e-4)
    assert ph.estimated_distance_range(0.0) is None


def test_stellar_relations_on_the_sun():
    assert ph.radius_from_l_teff(1.0, ph.T_SUN_K) == pytest.approx(1.0)
    assert ph.mass_from_luminosity(1.0) == pytest.approx(1.0)
    assert ph.kepler_period_yr(1.0, 1.0) == pytest.approx(1.0)
    # Sun: M_G ~ 4.67 with BC_G ~ +0.06 -> L ~ 1.
    assert ph.luminosity_from_abs_g(4.67, ph.T_SUN_K) == pytest.approx(1.0, rel=0.1)
    ratios = dict(ph.size_comparison(1.0))
    assert ratios["Sun"] == 1.0 and ratios["Jupiter"] == pytest.approx(9.95, abs=0.05)
    assert ratios["Earth"] == pytest.approx(109.2, abs=0.2)


def test_mass_luminosity_relation_is_continuous():
    for l_edge, m_edge in ((0.23 * 0.43**2.3, 0.43), (16.0, 2.0)):
        below, above = ph.mass_from_luminosity(l_edge * 0.999), ph.mass_from_luminosity(l_edge * 1.001)
        assert below == pytest.approx(m_edge, rel=0.01) and above == pytest.approx(m_edge, rel=0.01)


def test_temperatures_from_colour_and_spectral_type():
    assert ph.teff_from_bp_rp(0.82) == pytest.approx(5770)
    assert ph.teff_from_bp_rp(2.83) == pytest.approx(3210, abs=5)
    assert ph.teff_from_bp_rp(7.0) is None
    assert ph.teff_from_spectral_type("M4V") == pytest.approx(3215, abs=30)
    assert ph.teff_from_spectral_type("G2V") == pytest.approx(5820, abs=60)
    assert ph.teff_from_spectral_type("Y2") < 500
    assert ph.teff_from_spectral_type("T1V+T6V") == pytest.approx(1210, abs=20)
    assert ph.teff_from_spectral_type("DA2.5") == pytest.approx(20160)
    assert ph.teff_from_spectral_type("pec") is None


def test_blackbody_colours():
    assert ph.blackbody_hex(3100) == "#ffb475"  # orange-red dwarf
    r, g, b = (int(ph.blackbody_hex(30000)[i:i + 2], 16) for i in (1, 3, 5))
    assert b == 255 and r < 200  # hot stars are blue-white
    r, g, b = (int(ph.blackbody_hex(300)[i:i + 2], 16) for i in (1, 3, 5))
    assert r == 255 and b == 0  # clamped: deep orange-red
    assert ph.blackbody_hex(None) is None


@pytest.mark.parametrize("args, cls", [
    ((11.88, 2.83, 3100, "M4V", "BY*"), "red dwarf"),
    ((13.0, 0.0, None, None, None), "white dwarf"),
    ((0.5, 1.2, 4700, None, None), "giant"),
    ((-6.0, 0.3, 9000, None, None), "supergiant"),
    ((4.7, 0.82, 5772, None, None), "sun-like star"),
    ((1.0, 0.0, 9700, None, None), "hot star"),
    ((None, None, None, "Y2", "BD*"), "brown dwarf"),
    ((None, None, None, "K0III", None), "giant"),
    ((None, None, None, "B1Ia", None), "supergiant"),
    ((None, None, None, "DA2.5", None), "white dwarf"),
    ((None, None, 3000, None, None), "red dwarf"),
])
def test_classify(args, cls):
    assert ph.classify(*args)[0] == cls


def test_classify_without_any_data():
    assert ph.classify(None, None, None, None, None) == (None, None)


def test_constellation_spelling_is_fixed():
    assert ph.sky_context(B_RA, B_DEC)["constellation"] == "Ophiuchus"


def test_path_without_distance_is_a_2000_year_sky_track():
    path = ph.sky_path(133.76, -7.24, 2026.0, -8000.0, 600.0, None, None, now_year=2026.0)
    assert path.span_years == 2_000 and path.closest is None
    assert all(p[3] is None for p in path.points)
    first, last = path.points[0], path.points[-1]
    assert first[1] > 133.76 > last[1]  # moving west (pmra < 0)
    sep = ph.separation_arcsec(first[1], first[2], last[1], last[2])
    assert sep == pytest.approx(2000 * math.hypot(8.0, 0.6), rel=1e-3)


def test_track_distance_matches_a_fast_star_seen_since_spherex_started():
    now = ph.position_at(B_RA, B_DEC, B_PMRA, B_PMDEC, 2016.0, 2026.4)
    assert ph.track_distance_arcsec(*now, B_RA, B_DEC, B_PMRA, B_PMDEC, 2016.0, 2025.3, 2026.9) < 0.01
    # The 2016 position itself is ~108" south of today's track.
    assert ph.track_distance_arcsec(B_RA, B_DEC, B_RA, B_DEC, B_PMRA, B_PMDEC, 2016.0, 2025.3, 2026.9) > 90


def _why(**kw):
    return ph.why_moving(ph.WhyInput(**kw))


def test_why_moving_for_barnard():
    why = _why(pm_mas=10393.0, pm_err=0.04, motion_source="gaia", d_ly=5.96, space_kms=142.6,
               tangential_kms=90.1, population="thick disk", parallax_mas=546.98)
    factors = [f for f, _ in why]
    assert factors[:3] == ["closeness", "speed", "population"] and "parallax" in factors
    assert "6.0 light-years" in why[0][1] and "7 times faster" in why[1][1]
    assert '0.55"' in dict(why)["parallax"]


def test_why_moving_static_for_a_distant_object_without_motion():
    why = _why(pm_mas=0.3, pm_err=0.5, motion_source="gaia", d_ly=6500.0, parallax_mas=0.5)
    assert why[0][0] == "static" and "no measurable motion" in why[0][1]
    assert all(f in ("static",) for f, _ in why)


def test_why_moving_slow_drift_is_static_with_pixel_crossing_time():
    why = _why(pm_mas=20.0, pm_err=0.1, motion_source="gaia", d_ly=3000.0)
    assert why[0][0] == "static" and "310 years" in why[0][1]


def test_why_moving_motion_only_gives_estimated_distance_and_candidate_wording():
    est = ph.estimated_distance_range(1500.0)
    why = _why(pm_mas=1500.0, pm_err=100.0, motion_source="measured", estimated_pc=est)
    text = " ".join(t for _, t in why)
    assert [f for f, _ in why] == ["unknown", "unknown"]
    assert "candidate" in text and "estimate" in text and "discover" not in text


def test_why_moving_without_any_motion_is_unknown():
    assert _why() == [("unknown", "No motion across the sky has been measured for it yet.")]


def test_why_moving_mentions_a_poor_gaia_fit():
    why = _why(pm_mas=300.0, pm_err=0.1, motion_source="gaia", d_ly=50.0, ruwe=2.5)
    assert why[-1][0] == "companion" and "RUWE 2.5" in why[-1][1]
