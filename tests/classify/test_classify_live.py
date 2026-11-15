"""Live checks that the catalogue fetchers and the Horizons lookup still get what the rules need.

Each test hits the real service and checks content against a known answer.
"""
import math

import pytest

from planetx.classify import catalogs, observer
from planetx.classify.catalogs import CatalogError, fetch_gaia, fetch_simbad, fetch_skybot
from planetx.classify.rules import FieldCatalog

pytestmark = pytest.mark.network

# Where the engine saw Barnard's Star in the cached field (2026.2): 106" north of Gaia's 2016.0 position.
BARNARD_SPHEREX = (269.44615, 4.76875, 61102.1)


def test_gaia_cone_holds_barnards_star_with_its_proper_motion():
    stars = fetch_gaia(269.4485, 4.7394, 60.0)
    barnard = next(s for s in stars if s.name == "Gaia DR3 4472832130942575872")
    assert stars[0] is barnard  # brightest first
    assert math.hypot(barnard.pm_ra_cosdec, barnard.pm_dec) == pytest.approx(10.39, abs=0.02)
    assert barnard.epoch_mjd == 57388.5 and barnard.magnitude == pytest.approx(8.19, abs=0.05)
    assert any(s.pm_ra_cosdec is None for s in stars)  # faint stars without a proper motion come back too
    (index, sep), *_ = FieldCatalog(stars).near(*BARNARD_SPHEREX, 4.5)
    assert stars[index] is barnard and sep < 2.0


def test_simbad_cone_names_barnards_star_at_its_j2000_position():
    objects = fetch_simbad(269.45208, 4.69337, 120.0)
    barnard = next(o for o in objects if o.name == "Barnard's Star")
    assert barnard.variable and barnard.kind == "BY Dra Variable"
    assert barnard.epoch_mjd == 51544.5 and barnard.pm_dec == pytest.approx(10.36, abs=0.02)
    assert not any("Barnard's Star " in o.name for o in objects)  # its planets are left out
    (index, sep), *_ = FieldCatalog(objects).near(*BARNARD_SPHEREX, 4.5)
    assert objects[index] is barnard and sep < 2.0


def test_skybot_lists_asteroids_on_the_ecliptic_and_nothing_near_the_pole():
    # MJD 61000 near the ecliptic: five known main-belt asteroids within 4' when recorded.
    bodies = fetch_skybot(269.4485, -23.0, 240.0, 61000.0)
    assert bodies and all(b.mjd == 61000.0 and b.geocentric_au > 0.5 for b in bodies)
    assert all(abs(b.dec + 23.0) < 0.1 and abs(b.ra - 269.4485) < 0.1 for b in bodies)
    assert any(b.kind == "Main-belt asteroid" and b.name.startswith("(") for b in bodies)
    assert fetch_skybot(269.4485, 89.0, 36.0, 61000.0) == []


# --- SPHEREx as the observer (JPL Horizons) ---------------------------------------


def _horizons_radec(target: str, center: str, jd: float) -> tuple[float, float, float]:
    """(ra, dec, distance au) of a body in a Horizons observer table: astrometric ICRF, UT."""
    resp = catalogs._send("GET", observer.HORIZONS_URL, timeout=60.0, what="Horizons", params={
        "format": "json", "COMMAND": f"'{target}'", "OBJ_DATA": "NO", "EPHEM_TYPE": "OBSERVER",
        "CENTER": f"'{center}'", "QUANTITIES": "'1,20'", "ANG_FORMAT": "DEG", "EXTRA_PREC": "YES",
        "CSV_FORMAT": "YES", "TIME_TYPE": "UT", "TLIST_TYPE": "JD", "TLIST": f"'{jd}'",
    })
    text = resp.json()["result"]
    cells = [c.strip() for c in text[text.index("$$SOE") + 5:text.index("$$EOE")].strip().split(",")]
    return float(cells[3]), float(cells[4]), float(cells[5])


def test_horizons_knows_spherex_in_low_earth_orbit(tmp_path):
    first, later = observer.spherex_geocentric_km([61000.0, 61000.01], cache_dir=tmp_path / "c")
    for xyz in (first, later):
        assert 6900.0 < math.dist(xyz, (0, 0, 0)) < 7100.0
    # As recorded on 2026-11-14 (TLE-based, so a later TLE set may move it a little).
    assert math.dist(first, (4297.603, -3372.597, -4430.586)) < 25.0
    assert math.dist(first, later) > 5000.0  # 14 minutes is a seventh of an orbit


def test_moving_a_geocentric_position_to_spherex_matches_horizons_spacecraft_ephemeris(tmp_path):
    jd = 2461000.5
    geo_ra, geo_dec, au = _horizons_radec("433;", "500@399", jd)
    seen_ra, seen_dec, _ = _horizons_radec("433;", "@-163182", jd)
    (spherex_km,) = observer.spherex_geocentric_km([jd - observer.MJD_TO_JD], cache_dir=tmp_path / "c")
    ra, dec = observer.from_observer(geo_ra, geo_dec, au, spherex_km)
    miss = math.hypot((ra - seen_ra) * math.cos(math.radians(seen_dec)), dec - seen_dec) * 3600.0
    shift = math.hypot((geo_ra - seen_ra) * math.cos(math.radians(seen_dec)), geo_dec - seen_dec) * 3600.0
    assert shift > 20.0 and miss < 0.1


def test_horizons_refuses_times_before_launch(tmp_path):
    with pytest.raises(CatalogError, match="No ephemeris"):
        observer.spherex_geocentric_km([60000.0], cache_dir=tmp_path / "c")


def test_skybot_has_no_spherex_observer_so_positions_must_be_moved_here():
    def bodies(code):
        resp = catalogs._send("GET", catalogs.SKYBOT_URL, timeout=60.0, what="SkyBoT", params={
            "-ra": "180", "-dec": "0", "-rd": "0.5", "-ep": "2461000.5", "-observer": code,
            "-mime": "json", "-output": "obs", "-objFilter": "100",
        })
        return resp.json()

    geocentre, spherex_code, hubble = bodies("500"), bodies("288"), bodies("250")
    assert geocentre[0]["RA (hms)"] == spherex_code[0]["RA (hms)"]  # 288 is SPHEREx, answered as 500
    assert geocentre[0]["RA (hms)"] != hubble[0]["RA (hms)"]  # the parameter itself works
    assert bodies("@-163182")["flag"] == -1  # spacecraft ids are refused
