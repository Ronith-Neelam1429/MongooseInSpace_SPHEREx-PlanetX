"""Offline tests for SPHEREx's observer position: parallax maths, the Horizons parser and its disk cache.

The Eros numbers are real Horizons answers of 2026-11-14 for 2025-11-21 0h UT:
geocentric (CENTER 500@399), from SPHEREx (CENTER @-163182), and SPHEREx's
geocentric ICRF position.
"""
import json
import math

import numpy as np
import pytest

from planetx.classify import catalogs, observer
from planetx.classify.catalogs import CatalogError
from planetx.classify.observer import (
    from_observer,
    mean_position,
    parallax_arcsec,
    parse_horizons_vectors,
    spherex_geocentric_km,
)

EROS_GEOCENTRIC = (8.812562109, 43.282462942, 0.40016966952235)  # ra, dec (deg), distance (au)
EROS_FROM_SPHEREX = (8.817809812, 43.287997422)
SPHEREX_KM = (4297.603103518885, -3372.596855402749, -4430.585798428440)
MJD = 61000.0  # JD 2461000.5


def sky_offset_arcsec(ra1, dec1, ra2, dec2):
    """(east, north) of point 1 from point 2, small angles."""
    return (ra1 - ra2) * 3600.0 * math.cos(math.radians(dec2)), (dec1 - dec2) * 3600.0


def test_parallax_is_the_angle_the_baseline_subtends_at_the_body():
    assert parallax_arcsec(7000.0, 1.0) == pytest.approx(9.65, abs=0.01)  # the old 9.7"/au rule
    assert parallax_arcsec(7100.0, 0.4) == pytest.approx(24.47, abs=0.01)
    assert parallax_arcsec(7100.0, 0.02) == pytest.approx(489.5, abs=0.1)  # a near-Earth object: 8'
    assert parallax_arcsec(0.0, 0.01) == 0.0
    assert parallax_arcsec(1e9, 1e-6) == pytest.approx(324_000.0)  # clamps at 90 degrees
    for bad in ((-1.0, 1.0), (7000.0, 0.0), (7000.0, -0.5), (7000.0, math.nan)):
        with pytest.raises(ValueError):
            parallax_arcsec(*bad)


def test_moving_eros_to_spherex_reproduces_horizons_own_spacecraft_ephemeris():
    ra, dec = from_observer(*EROS_GEOCENTRIC, SPHEREX_KM)
    east, north = sky_offset_arcsec(ra, dec, *EROS_FROM_SPHEREX)
    assert math.hypot(east, north) < 0.01
    # ...where the geocentric position is 24" off: the shift the old fixed tolerance had to swallow.
    east, north = sky_offset_arcsec(*EROS_GEOCENTRIC[:2], *EROS_FROM_SPHEREX)
    assert math.hypot(east, north) == pytest.approx(24.2, abs=0.1)


def test_an_observer_at_the_geocentre_sees_the_geocentric_position_and_a_shift_points_away():
    assert from_observer(120.0, -30.0, 0.1, (0.0, 0.0, 0.0)) == pytest.approx((120.0, -30.0))
    # Body on the +x axis at 0.01 au, observer 7,000 km along +y: the body appears shifted towards -y (lower RA).
    ra, dec = from_observer(0.0, 0.0, 0.01, (0.0, 7000.0, 0.0))
    assert dec == pytest.approx(0.0, abs=1e-12)
    assert (ra - 360.0) * 3600.0 == pytest.approx(-parallax_arcsec(7000.0, 0.01), rel=1e-4)
    # Across RA 0 the answer stays in [0, 360).
    assert 0.0 <= ra < 360.0
    with pytest.raises(ValueError):
        from_observer(0.0, 0.0, 0.0, SPHEREX_KM)


def test_mean_position_and_its_spread():
    mean, spread = mean_position([(7000.0, 0.0, 0.0), (-7000.0, 0.0, 0.0)])
    assert np.allclose(mean, 0.0) and spread == pytest.approx(7000.0)
    mean, spread = mean_position([SPHEREX_KM])
    assert np.allclose(mean, SPHEREX_KM) and spread == 0.0
    with pytest.raises(ValueError):
        mean_position([])


def horizons_json(jds_and_xyz, error=None):
    rows = "\n".join(
        f"{jd:.9f}, A.D. 2025-Nov-21 00:00:00.0000,  {x:.15E}, {y:.15E}, {z:.15E},"
        for jd, (x, y, z) in jds_and_xyz
    )
    data = {"signature": {"source": "NASA/JPL Horizons API", "version": "1.2"},
            "result": f"header\n$$SOE\n{rows}\n$$EOE\nfooter"}
    if error:
        data = {**data, "result": error, "error": error}
    return data


def test_parse_horizons_vectors_reads_rows_and_rejects_anything_odd():
    data = horizons_json([(2461000.5, SPHEREX_KM), (2461000.51, (-762.9, -685.6, -6966.2))])
    rows = parse_horizons_vectors(data, 2)
    assert rows[0] == pytest.approx((2461000.5, *SPHEREX_KM)) and rows[1][0] == pytest.approx(2461000.51)
    with pytest.raises(CatalogError, match="2 positions for 3"):
        parse_horizons_vectors(data, 3)
    with pytest.raises(CatalogError, match="No ephemeris"):
        parse_horizons_vectors(horizons_json([], error='No ephemeris for target "SPHEREx (spacecraft)"'), 1)
    with pytest.raises(CatalogError, match="not its orbit"):  # the Moon's distance: the wrong target
        parse_horizons_vectors(horizons_json([(2461000.5, (384_400.0, 0.0, 0.0))]), 1)
    with pytest.raises(CatalogError, match="unexpected Horizons response"):
        parse_horizons_vectors({"result": "no table"}, 1)


class Response:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.text = json.dumps(data) if not isinstance(data, str) else data

    def json(self):
        if isinstance(self._data, str):
            raise ValueError("not JSON")
        return self._data


@pytest.fixture
def horizons(monkeypatch):
    """A fake Horizons: answers every asked time with a position derived from it, records the asked JDs."""
    asked: list[list[float]] = []
    state = {"fail": None}

    def send(method, url, *, params, timeout, what):
        assert method == "GET" and url == observer.HORIZONS_URL and what == "Horizons"
        assert params["COMMAND"] == "'-163182'" and params["CENTER"] == "'500@399'"
        assert params["REF_PLANE"] == "FRAME" and params["TIME_TYPE"] == "UT" and params["TLIST_TYPE"] == "JD"
        jds = [float(t.strip("'")) for t in params["TLIST"].split()]
        asked.append(jds)
        if state["fail"] is not None:
            return state["fail"]
        return Response(200, horizons_json([(jd, (7000.0, jd - 2461000.0, 0.0)) for jd in jds]))

    monkeypatch.setattr(catalogs, "_send", send)
    return asked, state


def test_positions_are_fetched_once_and_then_come_from_the_disk_cache(tmp_path, horizons):
    asked, _ = horizons
    cache = tmp_path / "horizons" / "spherex.json"
    first = spherex_geocentric_km([MJD, MJD + 0.001, MJD], cache_path=cache)
    assert len(asked) == 1 and asked[0] == pytest.approx([2461000.5, 2461000.501])  # each time asked once
    assert first[0] == pytest.approx((7000.0, 0.5, 0.0)) and first[1] == pytest.approx((7000.0, 0.501, 0.0))
    assert np.array_equal(first[0], first[2])
    again = spherex_geocentric_km([MJD + 0.001, MJD], cache_path=cache)
    assert len(asked) == 1 and np.array_equal(again[0], first[1])  # no new request
    spherex_geocentric_km([MJD, MJD + 0.002], cache_path=cache)
    assert asked[1] == pytest.approx([2461000.502])  # only the new time
    assert spherex_geocentric_km([], cache_path=cache) == [] and len(asked) == 2


def test_many_times_are_split_into_several_requests(tmp_path, horizons, monkeypatch):
    asked, _ = horizons
    monkeypatch.setattr(observer, "MAX_EPOCHS_PER_CALL", 2)
    out = spherex_geocentric_km([MJD + 0.01 * k for k in range(5)], cache_path=tmp_path / "c.json")
    assert [len(a) for a in asked] == [2, 2, 1] and len(out) == 5


def test_a_horizons_failure_raises_and_caches_nothing(tmp_path, horizons):
    asked, state = horizons
    cache = tmp_path / "c.json"
    state["fail"] = Response(400, {"result": "No ephemeris", "error": "No ephemeris for target prior to 2025-MAR-12"})
    with pytest.raises(CatalogError, match="prior to 2025-MAR-12"):
        spherex_geocentric_km([60000.0], cache_path=cache)
    state["fail"] = Response(503, "<html>Service Unavailable</html>")
    with pytest.raises(CatalogError, match="HTTP 503"):
        spherex_geocentric_km([MJD], cache_path=cache)
    assert not cache.exists()
    state["fail"] = None
    spherex_geocentric_km([MJD], cache_path=cache)
    assert cache.exists() and len(asked) == 3


def test_a_corrupt_cache_is_ignored_and_rewritten(tmp_path, horizons, caplog):
    asked, _ = horizons
    cache = tmp_path / "c.json"
    cache.write_text("{not json")
    assert spherex_geocentric_km([MJD], cache_path=cache)[0] == pytest.approx((7000.0, 0.5, 0.0))
    assert "unreadable Horizons cache" in caplog.text and len(asked) == 1
    cache.write_text(json.dumps({"61000.00000000": ["x", 1, 2], "61000.00100000": [1.0, 2.0, 3.0]}))
    out = spherex_geocentric_km([MJD, MJD + 0.001], cache_path=cache)
    assert asked[1] == pytest.approx([2461000.5]) and out[1] == pytest.approx((1.0, 2.0, 3.0))


def test_invalid_times_are_refused_before_any_request(tmp_path, horizons):
    asked, _ = horizons
    with pytest.raises(ValueError):
        spherex_geocentric_km([math.nan], cache_path=tmp_path / "c.json")
    assert asked == []
