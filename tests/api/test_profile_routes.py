"""Offline tests for GET /api/object/profile: validation, assembly with fake catalogues,
failures turned into notes, the time budget, the cache, and the catalogue parsers."""
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
    monkeypatch.setattr(fetchers, "find_simbad", make("find_simbad", simbad))
    monkeypatch.setattr(fetchers, "fetch_simbad_by_ident", make("fetch_simbad_by_ident", simbad))
    monkeypatch.setattr(fetchers, "fetch_astro_params", make("fetch_astro_params", params))
    monkeypatch.setattr(fetchers, "fetch_companion_candidates", make("fetch_companion_candidates", list(companions)))
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

    def by_ident(ident):
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
        fetchers.fetch_gaia_by_id("1 or 1=1")
