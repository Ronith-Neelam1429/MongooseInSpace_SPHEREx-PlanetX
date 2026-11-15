"""Offline tests for GET /api/object/profile: validation, assembly with fake catalogues,
failures turned into notes, the time budget, the cache, and the catalogue parsers."""
import math
import threading
import time

import pytest
from astropy.io import ascii as astropy_ascii
from fastapi.testclient import TestClient
from planetx.api.app import app
from planetx.classify.catalogs import CatalogError
from planetx.observatories import catalogs as gator
from planetx.observatories.catalogs import ArchiveError
from planetx.profile import fetchers, service

client = TestClient(app)

BARNARD_NOW = (269.4473, 4.7685)
BARNARD = fetchers.GaiaStar(
    source_id="4472832130942575872", ra=269.44850252544, dec=4.73942005111, ref_epoch=2016.0,
    parallax=546.9759, parallax_error=0.0401, pmra=-801.551, pmra_error=0.032, pmdec=10362.394,
    pmdec_error=0.036, radial_velocity=-110.47, radial_velocity_error=0.13, g=8.193974, bp=9.791788,
    rp=6.958091, bp_rp=2.833697, teff_gspphot=3099.6, ruwe=1.085, non_single_star=0, distance_gspphot=1.8275,
)
BARNARD_SIMBAD = fetchers.SimbadObject(
    main_id="NAME Barnard's star", name="Barnard's Star", otype="BY*", description="BY Dra Variable",
    sp_type="M4V", plx=546.9759, plx_err=0.0401, rv=-110.11, rv_err=None, pmra=-801.551, pmdec=10362.394,
    ra=269.4520769586187, dec=4.693364966576667,
)
BARNARD_PARAMS = fetchers.AstroParams(teff_gspphot=3099.6335, radius_flame=0.2022, radius_flame_lo=0.1981,
                                      radius_flame_hi=0.2062, lum_flame=0.003398)
WISE0855_SIMBAD = fetchers.SimbadObject(
    main_id="WISEA J085510.74-071442.5", name="WISEA J085510.74-071442.5", otype="BD*", description="Brown Dwarf",
    sp_type="Y2", plx=439.0, plx_err=2.4, rv=None, rv_err=None, pmra=-8123.7, pmdec=673.2,
    ra=133.81890825, dec=-7.247048,
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """Empty profile cache, and no real Gator call even if a fake is missing."""
    service.clear_cache()

    def no_gator(*a, **k):
        raise AssertionError("offline test tried to reach IRSA Gator")

    monkeypatch.setattr(gator, "_get", no_gator)
    yield
    service.clear_cache()


def _fake(monkeypatch, *, gaia=None, simbad=None, params=None, companions=(), calls=None):
    """Replace every fetcher; a value that is an Exception is raised instead of returned."""
    calls = calls if calls is not None else []

    def make(name, value):
        def fn(*args, **kwargs):
            calls.append(name)
            if isinstance(value, BaseException):
                raise value
            return value
        return fn

    monkeypatch.setattr(fetchers, "find_gaia", make("find_gaia", gaia))
    monkeypatch.setattr(fetchers, "fetch_gaia_by_id", make("fetch_gaia_by_id", gaia))
    monkeypatch.setattr(fetchers, "find_simbad", make(
        "find_simbad", simbad if isinstance(simbad, BaseException) else (simbad, None)))
    monkeypatch.setattr(fetchers, "fetch_simbad_by_ident", make("fetch_simbad_by_ident", simbad))
    monkeypatch.setattr(fetchers, "fetch_astro_params", make("fetch_astro_params", params))
    if not isinstance(companions, (BaseException, fetchers.CompanionSearch)):
        companions = fetchers.CompanionSearch(list(companions))
    monkeypatch.setattr(fetchers, "fetch_companion_candidates", make("fetch_companion_candidates", companions))
    return calls


def _get(**params):
    r = client.get("/api/object/profile", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# --- validation --------------------------------------------------------------------------


@pytest.mark.parametrize("params", [
    {"ra": 400, "dec": 0},
    {"ra": 10, "dec": 91},
    {"ra": "nan", "dec": 0},
    {"ra": 10, "dec": 0, "gaia_id": "12a"},
    {"ra": 10, "dec": 0, "gaia_id": "1" * 20},
    {"ra": 10, "dec": 0, "gaia_id": "1 OR 1=1"},
    {"ra": 10, "dec": 0, "name": "x" * 101},
    {"ra": 10, "dec": 0, "name": "bad\nname"},
    {"ra": 10, "dec": 0, "name": ""},
    {"ra": 10, "dec": 0, "pmra": "nan", "pmdec": 0},
    {"ra": 10, "dec": 0, "pmra": "inf", "pmdec": 0},
    {"ra": 10, "dec": 0, "pmra": 30000, "pmdec": 0},
    {"ra": 10, "dec": 0, "pm_err": -1},
    {"ra": 10, "dec": 0, "mag": 99},
])
def test_bad_parameters_are_rejected(params, monkeypatch):
    calls = _fake(monkeypatch)
    assert client.get("/api/object/profile", params=params).status_code == 422
    assert calls == []


def test_missing_position_is_rejected():
    assert client.get("/api/object/profile", params={"name": "Vega"}).status_code == 422


# --- a full profile from fake catalogues ----------------------------------------------------


def test_barnard_profile_from_all_catalogues(monkeypatch):
    companion = fetchers.GaiaStar(
        source_id="111", ra=BARNARD.ra + 0.01, dec=BARNARD.dec, ref_epoch=2016.0, parallax=546.8,
        parallax_error=0.1, pmra=-805.0, pmra_error=0.1, pmdec=10360.0, pmdec_error=0.1, g=15.0, bp_rp=3.5,
    )
    field = fetchers.GaiaStar(  # same distance, unrelated motion
        source_id="222", ra=BARNARD.ra, dec=BARNARD.dec + 0.02, ref_epoch=2016.0, parallax=546.0,
        parallax_error=0.1, pmra=100.0, pmdec=-300.0, g=14.0, bp_rp=3.0,
    )
    calls = _fake(monkeypatch, gaia=BARNARD, simbad=BARNARD_SIMBAD, params=BARNARD_PARAMS,
                  companions=[BARNARD, companion, field])
    p = _get(ra=BARNARD_NOW[0], dec=BARNARD_NOW[1])

    assert sorted(calls) == ["fetch_astro_params", "fetch_companion_candidates", "find_gaia", "find_simbad"]
    ident = p["identity"]
    assert ident["name"] == "Barnard's Star" and ident["simbad_type"] == "BY*"
    assert ident["kind"] == "Red dwarf star" and ident["spectral_type"] == "M4V"
    assert ident["gaia_id"] == BARNARD.source_id and ident["constellation"] == "Ophiuchus"
    assert p["distance"]["source"] == "gaia_parallax" and p["distance"]["pc"] == pytest.approx(1.828, abs=1e-3)

    phys = p["physical"]
    assert phys["class"] == "red dwarf"  # serialized under the contract's name, not class_
    assert "class_" not in phys
    assert phys["radius_source"] == "Gaia FLAME" and phys["radius_rsun"] == pytest.approx(0.202)
    assert phys["radius_rsun_err"] == pytest.approx(0.0041, abs=2e-4)
    assert phys["luminosity_lsun"] == pytest.approx(0.0034, abs=1e-4)
    assert phys["teff_source"] == "Gaia GSP-Phot" and phys["color_hex"].startswith("#ff")
    assert phys["mass_source"].startswith("estimate") and 0.1 < phys["mass_msun"] < 0.25
    assert [c["thing"] for c in phys["size_comparison"]] == ["Sun", "Jupiter", "Earth"]

    m = p["motion"]
    assert m["source"] == "gaia" and m["population"] == "thick disk"
    assert m["tangential_km_s"] == pytest.approx(90, abs=1) and m["space_km_s"] == pytest.approx(142, abs=1)
    assert m["radial_km_s"] == -110.47  # Gaia's RV wins over SIMBAD's
    assert m["parallax_wobble_arcsec"] == pytest.approx(0.547, abs=1e-3)
    assert m["estimated_distance_pc"] is None

    path = p["path"]
    assert len(path["points"]) == 41 and path["closest_approach"]["dist_ly"] == pytest.approx(3.77, abs=0.05)

    g = p["gravity"]
    assert [c["gaia_id"] for c in g["companions"]] == ["111"]  # itself and the field star left out
    c = g["companions"][0]
    assert c["bound"] == "likely" and c["sep_arcsec"] == pytest.approx(35.9, abs=0.2)
    assert c["sep_au"] == pytest.approx(66, abs=1) and c["period_yr"] > 0
    assert "1 star moving with it" in g["summary"]
    assert g["wobble_hint"] is False and g["ruwe"] == pytest.approx(1.08, abs=0.01)
    assert "companion" in [w["factor"] for w in p["why_moving"]]

    assert p["solar_system"] is None
    assert set(p["sources"]) == {"Gaia DR3 (IRSA)", "SIMBAD", "Gaia DR3 astrophysical parameters (VizieR)",
                                 "Gaia DR3 companion search (IRSA)"}
    assert p["notes"] == []
    labels = [link["label"] for link in p["links"]]
    assert labels[0] == "SIMBAD" and "Gaia DR3 (VizieR)" in labels
    assert "discover" not in str(p).lower()


def test_lookup_order_with_a_known_gaia_id_and_name(monkeypatch):
    calls = _fake(monkeypatch, gaia=BARNARD, simbad=BARNARD_SIMBAD, params=BARNARD_PARAMS)
    _get(ra=BARNARD_NOW[0], dec=BARNARD_NOW[1], gaia_id=BARNARD.source_id, name="Barnard's Star")
    assert "fetch_gaia_by_id" in calls and "fetch_simbad_by_ident" in calls
    assert "find_gaia" not in calls and "find_simbad" not in calls


def test_simbad_falls_back_to_the_gaia_id(monkeypatch):
    seen = []

    def by_ident(ident, **kwargs):
        seen.append(ident)
        return None if ident == "nonsense" else BARNARD_SIMBAD

    _fake(monkeypatch, gaia=BARNARD, params=BARNARD_PARAMS)
    monkeypatch.setattr(fetchers, "fetch_simbad_by_ident", by_ident)
    p = _get(ra=BARNARD_NOW[0], dec=BARNARD_NOW[1], name="nonsense")
    assert seen == ["nonsense", f"Gaia DR3 {BARNARD.source_id}"]
    assert p["identity"]["name"] == "Barnard's Star"
    assert any("nonsense" in n for n in p["notes"])


def test_brown_dwarf_known_only_to_simbad(monkeypatch):
    _fake(monkeypatch, gaia=None, simbad=WISE0855_SIMBAD)
    p = _get(ra=133.75977, dec=-7.24218)
    assert p["identity"]["gaia_id"] is None and p["identity"]["kind"] == "Brown dwarf"
    assert p["distance"]["source"] == "simbad_parallax" and p["distance"]["ly"] == pytest.approx(7.43, abs=0.01)
    phys = p["physical"]
    assert phys["class"] == "brown dwarf" and phys["class_basis"] == "spectral type"
    assert phys["teff_k"] < 500 and phys["teff_source"] == "estimate from spectral type"
    assert phys["luminosity_lsun"] is None and "rough" in phys["radius_source"]
    m = p["motion"]
    assert m["source"] == "simbad" and m["radial_km_s"] is None and m["uvw_km_s"] is None
    assert m["tangential_km_s"] == pytest.approx(88, abs=1)
    assert p["path"]["closest_approach"] is None and all(pt["dist_ly"] is None for pt in p["path"]["points"])
    assert "No radial velocity" in p["path"]["note"]
    assert "No Gaia DR3 star matches this position." in p["notes"]
    assert p["gravity"]["ruwe"] is None


def test_motion_only_object_gets_an_estimated_distance(monkeypatch):
    _fake(monkeypatch)
    p = _get(ra=200.0, dec=-30.0, pmra=1200.0, pmdec=-500.0, pm_err=80.0)
    m = p["motion"]
    assert m["source"] == "measured" and p["distance"] is None
    lo, hi = m["estimated_distance_pc"]
    assert lo < hi and lo == pytest.approx(20 / (4.74047 * 1.3), rel=1e-2)
    assert p["path"]["span_years"] == 2000
    factors = [w["factor"] for w in p["why_moving"]]
    assert factors == ["unknown", "unknown"]
    assert "candidate" in " ".join(n for n in p["notes"])


def test_distant_object_without_motion_is_static(monkeypatch):
    far = fetchers.GaiaStar(source_id="333", ra=10.0, dec=20.0, ref_epoch=2016.0, parallax=0.4,
                            parallax_error=0.05, pmra=0.2, pmra_error=0.3, pmdec=-0.1, pmdec_error=0.3,
                            g=17.0, bp_rp=1.0, ruwe=1.0)
    _fake(monkeypatch, gaia=far)
    p = _get(ra=10.0, dec=20.0)
    assert p["why_moving"][0]["factor"] == "static"
    assert p["gravity"]["companions"] == []
    assert "too slowly" in p["gravity"]["summary"] or "uncertain" in p["gravity"]["summary"]


def test_poor_gaia_fit_hints_at_an_unseen_companion(monkeypatch):
    wobbly = fetchers.GaiaStar(source_id="444", ra=50.0, dec=10.0, ref_epoch=2016.0, parallax=50.0,
                               parallax_error=0.1, pmra=300.0, pmra_error=0.1, pmdec=-200.0, pmdec_error=0.1,
                               g=10.0, bp_rp=1.0, ruwe=3.2, non_single_star=1)
    _fake(monkeypatch, gaia=wobbly)
    g = _get(ra=50.0, dec=10.0)["gravity"]
    assert g["wobble_hint"] is True and g["non_single_star"] == 1
    assert "unseen companion" in g["summary"] and "non-single" in g["summary"]
    assert g["summary"].startswith("No star moving with it was found in Gaia within 15'.")
    assert "overall pull" not in g["summary"]  # not while a wobble points at a companion


# --- failures ---------------------------------------------------------------------------------


def test_failing_services_become_notes_not_errors(monkeypatch):
    _fake(monkeypatch, gaia=ArchiveError("IRSA returned HTTP 503 after 2 attempts"),
          simbad=CatalogError("SIMBAD (simbad.harvard.edu) request failed: ConnectTimeout"))
    p = _get(ra=10.0, dec=20.0)
    assert any(n.startswith("Gaia DR3 (IRSA) was unavailable") for n in p["notes"])
    assert any(n.startswith("SIMBAD was unavailable") for n in p["notes"])
    assert p["sources"] == [] and p["why_moving"] and p["identity"]["constellation"]
    assert "No catalogue data" in p["gravity"]["summary"]


def test_an_unexpected_bug_in_a_fetcher_is_a_note(monkeypatch):
    _fake(monkeypatch, gaia=BARNARD, simbad=BARNARD_SIMBAD, params=KeyError("boom"))
    p = _get(ra=BARNARD_NOW[0], dec=BARNARD_NOW[1])
    assert any("failed unexpectedly" in n for n in p["notes"])
    assert p["physical"]["radius_source"] == "estimate from luminosity and temperature"


def test_slow_service_is_cut_off_by_the_budget(monkeypatch):
    monkeypatch.setattr(service, "ROUND1_BUDGET_S", 0.2)
    _fake(monkeypatch, simbad=BARNARD_SIMBAD)

    def slow(*a, **k):
        time.sleep(1.0)
        return BARNARD

    monkeypatch.setattr(fetchers, "find_gaia", slow)
    t0 = time.monotonic()
    p = _get(ra=BARNARD_NOW[0], dec=BARNARD_NOW[1])
    assert time.monotonic() - t0 < 0.9
    assert "Gaia DR3 (IRSA) did not answer in time." in p["notes"]
    assert p["identity"]["name"] == "Barnard's Star"  # SIMBAD answered in time
    assert p["distance"]["source"] == "simbad_parallax"


# --- cache -----------------------------------------------------------------------------------


def test_profiles_are_cached(monkeypatch):
    calls = _fake(monkeypatch, gaia=BARNARD, simbad=BARNARD_SIMBAD, params=BARNARD_PARAMS)
    first = _get(ra=BARNARD_NOW[0], dec=BARNARD_NOW[1])
    n = len(calls)
    assert _get(ra=BARNARD_NOW[0] + 1e-6, dec=BARNARD_NOW[1]) == first
    assert len(calls) == n


def test_partial_profiles_expire_sooner(monkeypatch):
    _fake(monkeypatch, gaia=ArchiveError("down"), simbad=None)
    _get(ra=10.0, dec=20.0)
    (expiry, _), = service._cache.values()
    assert expiry - time.monotonic() <= service.CACHE_TTL_PARTIAL_S


# --- parsers ----------------------------------------------------------------------------------

GATOR_TEXT = """\\fixlen = T
|            source_id|                       ra|                      dec| ref_epoch|                 parallax|           parallax_error|                           pmra|                     pmra_error|                          pmdec|                    pmdec_error|                radial_velocity|          radial_velocity_error|    phot_g_mean_mag|   phot_bp_mean_mag|   phot_rp_mean_mag|              bp_rp|       teff_gspphot|               ruwe| non_single_star|   distance_gspphot|
|                 long|                   double|                   double|    double|                   double|                   double|                         double|                         double|                         double|                         double|                         double|                         double|             double|             double|             double|             double|             double|             double|             int|             double|
|                     |                      deg|                      deg|        yr|                      mas|                      mas|                         mas/yr|                         mas/yr|                         mas/yr|                         mas/yr|                           km/s|                           km/s|                mag|                mag|                mag|                mag|                  K|                   |                |                 pc|
|                 null|                     null|                     null|      null|                     null|                     null|                           null|                           null|                           null|                           null|                           null|                           null|               null|               null|               null|               null|               null|               null|            null|               null|
   4472832130942575872           269.44850252544             4.73942005111   2016.000                  546.9759                    0.0401                        -801.551                           0.032                       10362.394                           0.036                         -110.47                            0.13            8.193974            9.791788            6.958091            2.833697              3099.6               1.085                0              1.8275
   4472832886845552640           269.44857919607             4.74137329143   2016.000                      null                      null                            null                            null                            null                            null                            null                            null           19.345739           19.585512           17.838881            1.746632                null                null                0                null
"""


def test_parse_gaia_table_reads_gator_ipac():
    stars = fetchers.parse_gaia_table(astropy_ascii.read(GATOR_TEXT, format="ipac"))
    assert [s.source_id for s in stars] == ["4472832130942575872", "4472832886845552640"]
    b, faint = stars
    assert b.parallax == 546.9759 and b.pmdec == 10362.394 and b.radial_velocity == -110.47
    assert b.non_single_star == 0 and b.teff_gspphot == 3099.6 and b.distance_gspphot == 1.8275
    assert faint.parallax is None and faint.pmra is None and faint.g == pytest.approx(19.345739)


def test_pick_gaia_match_takes_the_fast_star_where_it_is_now():
    stars = fetchers.parse_gaia_table(astropy_ascii.read(GATOR_TEXT, format="ipac"))
    # Today Barnard is ~108" north of its 2016 position; the faint static star stays behind.
    # (The website's Barnard view centre is 3.7" from the star: inside the 5" match.)
    assert fetchers.pick_gaia_match(stars, *BARNARD_NOW, 3.0, 2026.9) is None
    assert fetchers.pick_gaia_match(stars, *BARNARD_NOW, service.MATCH_ARCSEC, 2026.9).source_id == BARNARD.source_id
    assert fetchers.pick_gaia_match(stars, 269.44858, 4.74137, 3.0, 2026.9).source_id == "4472832886845552640"
    assert fetchers.pick_gaia_match(stars, 10.0, 10.0, 3.0, 2026.9) is None


VIZIER_TEXT = """#
#RESOURCE=yCat_1355
#Table	I_355_paramp:
Source	Teff	Rad	Rad-Flame	b_Rad-Flame	B_Rad-Flame	Lum-Flame	Mass-Flame	Age-Flame	Teff-UCD	Evol
 	K	solRad	Rsun	Rsun	Rsun	Lsun	Msun	Gyr	K
-------------------	----------	---------	--------	--------	--------	----------	------	------	-------	----
4472832440168859904	 5313.4326	   0.6584	  0.6513	      	      	 3.006e-01
4472832130942575872	 3099.6335	   0.2070	  0.2022	  0.1981	  0.2062	 3.398e-03
4472832508888389376	 4517.2646	   0.5400	  0.7853	      	      	 2.269e-01	 0.720	 7.261	       	 319
"""


def test_parse_paramp_picks_the_requested_source():
    p = fetchers.parse_paramp(VIZIER_TEXT, "4472832130942575872")
    assert p.teff_gspphot == pytest.approx(3099.6335) and p.radius_flame == pytest.approx(0.2022)
    assert p.radius_flame_lo == pytest.approx(0.1981) and p.lum_flame == pytest.approx(0.003398)
    assert p.mass_flame is None and p.age_flame is None
    other = fetchers.parse_paramp(VIZIER_TEXT, "4472832508888389376")
    assert other.mass_flame == 0.72 and other.age_flame == pytest.approx(7.261) and other.evolstage_flame == 319
    assert fetchers.parse_paramp(VIZIER_TEXT, "1") is None


def test_parse_vizier_tsv_empty_and_broken():
    assert fetchers.parse_vizier_tsv("#\n#RESOURCE=yCat_1355\n#Table\tI_355_paramp:\n") == []
    with pytest.raises(CatalogError):
        fetchers.parse_vizier_tsv("<html>Service unavailable</html>")


SIMBAD_TEXT = (
    "main_id,otype,sp_type,plx_value,plx_err,rvz_radvel,rvz_err,pmra,pmdec,ra,dec,description\n"
    "\"NAME Barnard's star\",\"BY*\",\"M4V\",546.9759,0.0401,-110.11,0.0,-801.551,10362.394,"
    "269.4520769586187,4.693364966576667,\"BY Dra Variable\"\n"
    "\"WISEA J085510.74-071442.5\",\"BD*\",\"Y2\",439.0,2.4,,,-8123.7,673.2,133.81890825,-7.247048,\"Brown Dwarf\"\n"
)


def test_parse_simbad_and_match_by_track():
    objs = fetchers.parse_simbad(SIMBAD_TEXT)
    assert [o.name for o in objs] == ["Barnard's Star", "WISEA J085510.74-071442.5"]
    assert objs[1].rv is None and objs[1].sp_type == "Y2"
    # J2000 positions moved by their own motion to today.
    assert fetchers.pick_simbad_match(objs, *BARNARD_NOW, 5.0, 2026.9).name == "Barnard's Star"
    assert fetchers.pick_simbad_match(objs, 133.75977, -7.24218, 5.0, 2026.0).sp_type == "Y2"
    assert fetchers.pick_simbad_match(objs, 133.8189, -7.2470, 5.0, 2026.0) is None  # its J2000 spot is empty now


def test_adql_string_escapes_quotes_and_refuses_control_characters():
    assert fetchers.adql_string("Barnard's Star") == "'Barnard''s Star'"
    assert fetchers.adql_string("x' OR '1'='1") == "'x'' OR ''1''=''1'"
    with pytest.raises(ValueError):
        fetchers.adql_string("a\x00b")


def test_gaia_by_id_refuses_non_digits():
    with pytest.raises(ValueError):
        fetchers.fetch_gaia_by_id("1 or 1=1", deadline=time.monotonic() + 10)


# --- a measured motion must match the catalogue object (review item 1) -----------------------

BARNARD_MOTION = {"pmra": -820.0, "pmdec": 10300.0, "pm_err": 50.0}
OTHER_MOTION = {"pmra": 1200.0, "pmdec": -500.0, "pm_err": 80.0}  # 1.3"/yr the other way


def test_catalogue_star_moving_differently_is_not_shown(monkeypatch):
    calls = _fake(monkeypatch, gaia=BARNARD, simbad=BARNARD_SIMBAD, params=BARNARD_PARAMS)
    p = _get(ra=BARNARD_NOW[0], dec=BARNARD_NOW[1], **OTHER_MOTION)
    assert p["motion"]["source"] == "measured" and p["motion"]["pmra"] == 1200.0
    ident = p["identity"]
    assert ident["name"] is None and ident["gaia_id"] is None and ident["spectral_type"] is None
    assert p["distance"] is None and p["physical"] is None
    assert p["notes"].count(service.MISMATCH_NOTE) == 1
    assert "No Gaia DR3 star matches this position." not in p["notes"]
    assert "Gaia DR3 (VizieR)" not in [link["label"] for link in p["links"]]
    assert "fetch_astro_params" not in calls and "fetch_companion_candidates" not in calls


def test_a_given_gaia_id_moving_differently_is_not_shown(monkeypatch):
    _fake(monkeypatch, gaia=BARNARD, simbad=BARNARD_SIMBAD, params=BARNARD_PARAMS)
    p = _get(ra=BARNARD_NOW[0], dec=BARNARD_NOW[1], gaia_id=BARNARD.source_id, **OTHER_MOTION)
    assert p["identity"]["gaia_id"] is None and p["motion"]["source"] == "measured"
    assert p["distance"] is None and service.MISMATCH_NOTE in p["notes"]


def test_a_mover_is_not_named_after_a_bystander_with_no_known_motion(monkeypatch):
    # A faint Gaia star with a 2-parameter solution (no parallax, no motion) next to a mover.
    bystander = fetchers.GaiaStar(source_id="555", ra=BARNARD_NOW[0], dec=BARNARD_NOW[1], ref_epoch=2016.0,
                                  parallax=None, pmra=None, pmdec=None, g=20.9)
    _fake(monkeypatch, gaia=bystander, simbad=None, params=None)
    p = _get(ra=BARNARD_NOW[0], dec=BARNARD_NOW[1], **OTHER_MOTION)
    assert p["identity"]["gaia_id"] is None and p["physical"] is None
    assert p["motion"]["source"] == "measured" and service.MISMATCH_NOTE in p["notes"]
    # Without a significant measured motion the same star is still shown.
    service.clear_cache()
    p = _get(ra=BARNARD_NOW[0], dec=BARNARD_NOW[1])
    assert p["identity"]["gaia_id"] == "555"


def test_agreeing_or_insignificant_measured_motion_keeps_the_catalogue(monkeypatch):
    _fake(monkeypatch, gaia=BARNARD, simbad=BARNARD_SIMBAD, params=BARNARD_PARAMS)
    for motion in (BARNARD_MOTION, {"pmra": 1200.0, "pmdec": -500.0, "pm_err": 1000.0}):
        service.clear_cache()
        p = _get(ra=BARNARD_NOW[0], dec=BARNARD_NOW[1], **motion)
        assert p["identity"]["gaia_id"] == BARNARD.source_id and p["motion"]["source"] == "gaia"
        assert service.MISMATCH_NOTE not in p["notes"]


def test_position_match_prefers_the_star_moving_like_the_measurement():
    bright_static = fetchers.GaiaStar(source_id="1", ra=10.0, dec=20.0, ref_epoch=2016.0, parallax=1.0,
                                      pmra=0.0, pmra_error=0.1, pmdec=0.0, pmdec_error=0.1, g=8.0)
    faint_mover = fetchers.GaiaStar(source_id="2", ra=10.0, dec=20.0, ref_epoch=2016.0, parallax=30.0,
                                    pmra=300.0, pmra_error=0.1, pmdec=0.0, pmdec_error=0.1, g=15.0)
    stars = [bright_static, faint_mover]
    assert fetchers.pick_gaia_match(stars, 10.0, 20.0, 5.0, 2026.0).source_id == "1"  # brightest
    assert fetchers.pick_gaia_match(stars, 10.0, 20.0, 5.0, 2026.0, (300.0, 0.0, 20.0)).source_id == "2"


def test_simbad_match_prefers_the_object_moving_like_the_measurement():
    def obj(main_id, otype, pmra):
        return fetchers.SimbadObject(main_id=main_id, name=main_id, otype=otype, description=None, sp_type=None,
                                     plx=None, plx_err=None, rv=None, rv_err=None, pmra=pmra,
                                     pmdec=None if pmra is None else 0.0, ra=10.0, dec=20.0)
    quasar, mover = obj("QSO X", "QSO", None), obj("Star Y", "PM*", 100.0)
    assert fetchers.pick_simbad_match([quasar, mover], 10.0, 20.0, 5.0, 2026.0).main_id == "QSO X"  # closest
    # A quasar without a catalogued motion counts as not moving, so it disagrees with 100 mas/yr.
    assert fetchers.pick_simbad_match([quasar, mover], 10.0, 20.0, 5.0, 2026.0, (100.0, 0.0, 10.0),
                                      service._OTYPE_KINDS).main_id == "Star Y"


# --- non-stellar objects get no distance (item 2) ----------------------------------------------


def _simbad_obj(otype, plx=None, plx_err=None):
    return fetchers.SimbadObject(main_id=f"Some {otype}", name=f"Some {otype}", otype=otype, description=None,
                                 sp_type=None, plx=plx, plx_err=plx_err, rv=15000.0, rv_err=None, pmra=None,
                                 pmdec=None, ra=10.0, dec=20.0)


def _gaia_at(**kw):
    base = dict(source_id="555", ra=10.0, dec=20.0, ref_epoch=2016.0, pmra=0.5, pmra_error=0.3, pmdec=-0.2,
                pmdec_error=0.3, g=18.0, bp_rp=0.6, ruwe=1.0)
    return fetchers.GaiaStar(**{**base, **kw})


@pytest.mark.parametrize("plx_err", [0.2, 0.05])  # 4 sigma and 16 sigma: both noise for a quasar
def test_quasar_gets_no_distance(monkeypatch, plx_err):
    calls = _fake(monkeypatch, gaia=_gaia_at(parallax=0.8, parallax_error=plx_err, distance_gspphot=2000.0),
                  simbad=_simbad_obj("QSO"))
    p = _get(ra=10.0, dec=20.0)
    assert p["identity"]["kind"] == "Quasar" and p["distance"] is None and p["physical"] is None
    assert p["motion"]["parallax_wobble_arcsec"] is None and p["motion"]["estimated_distance_pc"] is None
    assert p["motion"]["tangential_km_s"] is None and p["motion"]["radial_km_s"] is None
    text = " ".join(w["text"] for w in p["why_moving"])
    assert "light-years" not in text and {w["factor"] for w in p["why_moving"]} <= {"static", "unknown"}
    assert "fetch_companion_candidates" not in calls and "beyond our Galaxy" in p["gravity"]["summary"]


@pytest.mark.parametrize("plx, plx_err, gsp, has_distance", [
    (1.0, 0.2, None, False),  # 5 sigma: ignored for a nebula
    (1.0, 0.1, None, True),  # 10 sigma
    (None, None, 900.0, False),  # GSP-Phot never
])
def test_nebula_needs_a_parallax_above_5_sigma(monkeypatch, plx, plx_err, gsp, has_distance):
    star = _gaia_at(parallax=plx, parallax_error=plx_err, distance_gspphot=gsp, pmra=20.0, pmra_error=1.0,
                    pmdec=0.0, pmdec_error=1.0)
    _fake(monkeypatch, gaia=star, simbad=_simbad_obj("PN"))
    p = _get(ra=10.0, dec=20.0)
    assert (p["distance"] is not None) is has_distance
    if has_distance:
        assert p["distance"]["source"] == "gaia_parallax"
    else:
        assert p["motion"]["estimated_distance_pc"] is None  # no "typical star" guess for a nebula
        assert "light-years" not in " ".join(w["text"] for w in p["why_moving"])


# --- deadlines and thread use (item 3) -----------------------------------------------------------


def test_a_stuck_lookup_does_not_starve_the_next_request(monkeypatch):
    """Nine requests whose Gaia lookup hangs (ignoring its deadline) used to fill the shared
    8-thread pool, so the next request's lookups queued and 'did not answer in time'."""
    monkeypatch.setattr(service, "ROUND1_BUDGET_S", 0.15)
    monkeypatch.setattr(service, "TOTAL_BUDGET_S", 0.3)
    _fake(monkeypatch, simbad=BARNARD_SIMBAD, params=BARNARD_PARAMS)
    release = threading.Event()

    def gaia(ra, dec, *args, **kwargs):
        if ra < 100:
            release.wait(10)
            return None
        return BARNARD

    monkeypatch.setattr(fetchers, "find_gaia", gaia)
    try:
        for i in range(9):
            _, complete = service.build_profile(service.ProfileRequest(ra=10.0 + i, dec=20.0))
            assert not complete
        # Its lookups start at once (none queued behind the stuck ones), so all answer within 0.15 s.
        p, complete = service.build_profile(service.ProfileRequest(ra=BARNARD_NOW[0], dec=BARNARD_NOW[1]))
        assert complete and p.identity.gaia_id == BARNARD.source_id
        assert not any("in time" in n for n in p.notes)
    finally:
        release.set()


def test_every_lookup_gets_its_rounds_deadline(monkeypatch):
    seen = {}

    def record(name, value):
        def fn(*args, deadline, **kwargs):
            seen[name] = deadline
            return value
        return fn

    _fake(monkeypatch)
    monkeypatch.setattr(fetchers, "find_gaia", record("gaia", BARNARD))
    monkeypatch.setattr(fetchers, "find_simbad", record("simbad", (BARNARD_SIMBAD, None)))
    monkeypatch.setattr(fetchers, "fetch_astro_params", record("params", BARNARD_PARAMS))
    monkeypatch.setattr(fetchers, "fetch_companion_candidates", record("companions", fetchers.CompanionSearch([])))
    t0 = time.monotonic()
    service.build_profile(service.ProfileRequest(ra=BARNARD_NOW[0], dec=BARNARD_NOW[1]))
    t1 = time.monotonic()
    for name in ("gaia", "simbad"):
        assert t0 + service.ROUND1_BUDGET_S <= seen[name] <= t1 + service.ROUND1_BUDGET_S
    for name in ("params", "companions"):
        assert t0 + service.TOTAL_BUDGET_S <= seen[name] <= t1 + service.TOTAL_BUDGET_S


def test_out_of_time_inside_a_fetcher_is_a_late_note(monkeypatch):
    _fake(monkeypatch, gaia=fetchers.OutOfTime("no time left for IRSA"), simbad=BARNARD_SIMBAD)
    p = _get(ra=BARNARD_NOW[0], dec=BARNARD_NOW[1])
    assert "Gaia DR3 (IRSA) did not answer in time." in p["notes"]


def test_the_two_gaia_cones_run_at_the_same_time(monkeypatch):
    def cone(*args, **kwargs):
        time.sleep(0.3)
        return []

    monkeypatch.setattr(fetchers, "fetch_gaia_cone", cone)
    t0 = time.monotonic()
    assert fetchers.find_gaia(10.0, 20.0, 5.0, 2026.0, deadline=time.monotonic() + 10) is None
    assert time.monotonic() - t0 < 0.5


class _FakeResponse:
    def __init__(self, status, chunks=(b"ok",), delay=0.0):
        self.status_code, self._chunks, self._delay = status, chunks, delay

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size):
        for c in self._chunks:
            time.sleep(self._delay)
            yield c


def _fake_open(monkeypatch, responses, seen):
    def fake(method, url, params, timeout):
        seen.append(timeout)
        r = responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r
    monkeypatch.setattr(fetchers, "_open", fake)


def test_http_caps_timeouts_to_the_time_left(monkeypatch):
    seen = []
    _fake_open(monkeypatch, [_FakeResponse(200, (b"a", b"b"))], seen)
    assert fetchers.http("GET", "https://x", {}, deadline=time.monotonic() + 3.0, what="X",
                         error=CatalogError) == (200, "ab")
    (connect, read), = seen
    assert 2.5 < read <= 3.0 and connect <= read


def test_http_retries_a_5xx_only_when_it_fits(monkeypatch):
    seen = []
    _fake_open(monkeypatch, [_FakeResponse(503), _FakeResponse(200)], seen)
    assert fetchers.http("GET", "https://x", {}, deadline=time.monotonic() + 30, what="X",
                         error=CatalogError) == (200, "ok")
    assert len(seen) == 2
    seen.clear()
    monkeypatch.setattr(fetchers, "RETRY_WAIT_S", 2.0)
    _fake_open(monkeypatch, [_FakeResponse(503), _FakeResponse(200)], seen)
    t0 = time.monotonic()
    assert fetchers.http("GET", "https://x", {}, deadline=time.monotonic() + 2.5, what="X",
                         error=CatalogError) == (503, "")
    assert len(seen) == 1 and time.monotonic() - t0 < 0.5  # no 2-4 s sleep past the deadline


def test_http_stops_at_the_deadline(monkeypatch):
    seen = []
    _fake_open(monkeypatch, [], seen)
    with pytest.raises(fetchers.OutOfTime):
        fetchers.http("GET", "https://x", {}, deadline=time.monotonic() + 0.5, what="X", error=CatalogError)
    assert seen == []  # less than MIN_CALL_S left: no call made
    monkeypatch.setattr(fetchers, "MIN_CALL_S", 0.01)
    _fake_open(monkeypatch, [_FakeResponse(200, (b"a",) * 5, delay=0.1)], seen)
    t0 = time.monotonic()
    with pytest.raises(fetchers.OutOfTime):
        fetchers.http("GET", "https://x", {}, deadline=time.monotonic() + 0.25, what="X", error=CatalogError)
    assert time.monotonic() - t0 < 0.4  # a trickling body is cut off between chunks


def test_http_network_errors_become_the_callers_error(monkeypatch):
    import requests
    _fake_open(monkeypatch, [requests.ConnectionError("refused")], [])
    with pytest.raises(ArchiveError, match="ConnectionError"):
        fetchers.http("GET", "https://x", {}, deadline=time.monotonic() + 5, what="IRSA", error=ArchiveError)


# --- wording, path and gravity (items 4-6, 8) ------------------------------------------------------


def test_wobble_is_said_once_in_the_gravity_summary_only(monkeypatch):
    wobbly = fetchers.GaiaStar(source_id="444", ra=50.0, dec=10.0, ref_epoch=2016.0, parallax=50.0,
                               parallax_error=0.1, pmra=300.0, pmra_error=0.1, pmdec=-200.0, pmdec_error=0.1,
                               g=10.0, bp_rp=1.0, ruwe=3.2, non_single_star=1)
    _fake(monkeypatch, gaia=wobbly)
    p = _get(ra=50.0, dec=10.0)
    assert p["gravity"]["summary"] == ("No star moving with it was found in Gaia within 15'. Gaia lists it as "
                                       "a non-single star and its position fit is poor (RUWE 3.2): signs of an "
                                       "unseen companion.")
    why = " ".join(w["text"] for w in p["why_moving"])
    assert "RUWE" not in why and "non-single" not in why and "unseen" not in why


def test_closest_approach_is_flagged_when_outside_the_path():
    # 100 pc, 20 mas/yr, approaching at 1 km/s: closest approach about a million years from now.
    slow = fetchers.GaiaStar(source_id="666", ra=10.0, dec=20.0, ref_epoch=2016.0, parallax=10.0,
                             parallax_error=0.01, pmra=20.0, pmra_error=0.01, pmdec=0.0, pmdec_error=0.01,
                             radial_velocity=-1.0, radial_velocity_error=0.1, g=10.0, bp_rp=0.8)
    kw = dict(companion_radius_arcsec=None, companion_note=None, companion_failed=False, notes=[], sources=[])
    req = service.ProfileRequest(ra=10.0, dec=20.0)
    path = service.assemble(req, 2026.9, slow, None, None, None, **kw).path
    years = [pt.year for pt in path.points]
    assert path.closest_approach is not None and path.closest_approach.year > max(years)
    assert path.closest_in_span is False
    barnard = service.assemble(service.ProfileRequest(ra=BARNARD_NOW[0], dec=BARNARD_NOW[1]), 2026.9, BARNARD,
                               BARNARD_SIMBAD, BARNARD_PARAMS, None, **kw).path
    assert barnard.closest_in_span is True
    nothing = service.assemble(service.ProfileRequest(ra=133.76, dec=-7.24), 2026.0, None, WISE0855_SIMBAD,
                               None, None, **kw).path
    assert nothing.closest_approach is None and nothing.closest_in_span is False


def test_weak_parallax_distance_is_worded_as_an_estimate(monkeypatch):
    weak = fetchers.GaiaStar(source_id="777", ra=10.0, dec=20.0, ref_epoch=2016.0, parallax=40.0,
                             parallax_error=10.0, pmra=1500.0, pmra_error=1.0, pmdec=0.0, pmdec_error=1.0,
                             g=15.0, bp_rp=2.5, ruwe=1.0)
    _fake(monkeypatch, gaia=weak)
    p = _get(ra=10.0, dec=20.0)
    assert p["distance"]["note"].startswith("parallax only 4.0 sigma")
    why = dict((w["factor"], w["text"]) for w in p["why_moving"])
    assert "roughly 82 light-years" in why["closeness"] and "roughly" in why["speed"]


@pytest.mark.parametrize("ucd, teff, source", [
    (1500.0, 1500, "Gaia ESP-UCD"),
    (None, 1650, "estimate from spectral type"),
])
def test_ultracool_dwarf_temperature_skips_gsp_phot(monkeypatch, ucd, teff, source):
    dwarf = fetchers.GaiaStar(source_id="888", ra=10.0, dec=20.0, ref_epoch=2016.0, parallax=50.0,
                              parallax_error=0.5, pmra=500.0, pmra_error=0.5, pmdec=0.0, pmdec_error=0.5,
                              g=20.0, bp_rp=4.5, teff_gspphot=3100.0, ruwe=1.0)
    l5 = fetchers.SimbadObject(main_id="2MASS J0000+2000", name="2MASS J0000+2000", otype="BD*",
                               description="Brown Dwarf", sp_type="L5", plx=None, plx_err=None, rv=None,
                               rv_err=None, pmra=500.0, pmdec=0.0, ra=10.0, dec=20.0)
    _fake(monkeypatch, gaia=dwarf, simbad=l5, params=fetchers.AstroParams(teff_gspphot=3100.0, teff_ucd=ucd))
    phys = _get(ra=10.0, dec=20.0)["physical"]
    assert phys["teff_k"] == teff and phys["teff_source"] == source and phys["class"] == "brown dwarf"


# --- crowded fields (items 7 and 10) -----------------------------------------------------------------


def test_crowded_companion_search_is_a_summary_not_a_failure(monkeypatch):
    _fake(monkeypatch, gaia=BARNARD, simbad=BARNARD_SIMBAD, params=BARNARD_PARAMS,
          companions=fetchers.CompanionSearch([], crowded=True))
    p = _get(ra=BARNARD_NOW[0], dec=BARNARD_NOW[1])
    assert p["gravity"]["summary"] == "The field is too crowded to search Gaia for companions."
    assert p["notes"] == [] and "Gaia DR3 companion search (IRSA)" in p["sources"]
    (expiry, _), = service._cache.values()
    assert expiry - time.monotonic() > service.CACHE_TTL_PARTIAL_S  # a complete profile


def test_companion_query_has_a_motion_window_and_reports_crowding(monkeypatch):
    sent = []

    def fake_http(method, url, params, **kwargs):
        sent.append(params)
        return 200, GATOR_TEXT

    monkeypatch.setattr(fetchers, "http", fake_http)
    args = (269.4485, 4.7394, 900.0, 500.0, 600.0, -801.5, 10362.4, 1036.2)
    found = fetchers.fetch_companion_candidates(*args, deadline=time.monotonic() + 10)
    assert not found.crowded and len(found.stars) == 2
    c = sent[0]["constraints"]
    assert "pmra>=-1837.700 and pmra<=234.700" in c and "pmdec>=9326.200 and pmdec<=11398.600" in c
    assert "parallax>=500.0000 and parallax<=600.0000" in c
    monkeypatch.setattr(fetchers, "COMPANION_MAX_ROWS", 2)
    crowded = fetchers.fetch_companion_candidates(*args, deadline=time.monotonic() + 10)
    assert crowded.crowded and crowded.stars == []


def test_companion_window_covers_the_per_star_cut():
    # A star at the edge of the companions() motion tolerance must pass the query's box.
    pm = (-801.5, 10362.4)
    window = max(service.COMPANION_PM_FRACTION * math.hypot(*pm), service.COMPANION_PM_FLOOR_MAS)
    assert window == pytest.approx(0.10 * math.hypot(*pm))


def test_cut_off_simbad_list_is_narrowed_and_noted(monkeypatch):
    queries = []
    answers = [([BARNARD_SIMBAD], fetchers.SIMBAD_TOP), ([BARNARD_SIMBAD], 3)]

    def fake_simbad(query, deadline):
        queries.append(query)
        return answers.pop(0)

    monkeypatch.setattr(fetchers, "_simbad", fake_simbad)
    obj, note = fetchers.find_simbad(*BARNARD_NOW, 5.0, 2026.9, deadline=time.monotonic() + 10)
    assert obj is BARNARD_SIMBAD and "narrower area" in note
    assert f"{45.0 / 3600:.6f}" in queries[0] and f"{15.0 / 3600:.6f}" in queries[1]

    answers[:] = [([], fetchers.SIMBAD_TOP), ([], fetchers.SIMBAD_TOP)]
    obj, note = fetchers.find_simbad(*BARNARD_NOW, 5.0, 2026.9, deadline=time.monotonic() + 10)
    assert obj is None and "cut off" in note

    answers[:] = [([BARNARD_SIMBAD], 12)]
    assert fetchers.find_simbad(*BARNARD_NOW, 5.0, 2026.9, deadline=time.monotonic() + 10) == (BARNARD_SIMBAD, None)


def test_simbad_search_note_reaches_the_profile(monkeypatch):
    _fake(monkeypatch, gaia=BARNARD, params=BARNARD_PARAMS)
    monkeypatch.setattr(fetchers, "find_simbad", lambda *a, **k: (BARNARD_SIMBAD, "SIMBAD lists many objects here."))
    p = _get(ra=BARNARD_NOW[0], dec=BARNARD_NOW[1])
    assert "SIMBAD lists many objects here." in p["notes"] and p["identity"]["name"] == "Barnard's Star"
