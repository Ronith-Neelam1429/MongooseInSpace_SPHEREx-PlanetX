"""Live tests for GET /api/object/profile (IRSA Gator, VizieR mirror, SIMBAD mirror).

Run: uv run pytest tests/api -m network -k profile
"""
import pytest
from fastapi.testclient import TestClient
from planetx.api.app import app
from planetx.profile import service
from targets import ENGINE_TARGETS_DEG

pytestmark = pytest.mark.network

client = TestClient(app)


def _profile(**params) -> dict:
    service.clear_cache()
    r = client.get("/api/object/profile", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_barnard_from_its_position_only():
    # The website's Barnard view centre (CLAUDE.md), 3.7" from the star today.
    p = _profile(ra=269.4473, dec=4.7685)
    print(p["notes"])
    assert p["identity"]["gaia_id"] == "4472832130942575872"
    assert p["identity"]["name"] == "Barnard's Star" and p["identity"]["constellation"] == "Ophiuchus"
    assert p["distance"]["source"] == "gaia_parallax" and p["distance"]["pc"] == pytest.approx(1.83, abs=0.01)
    m = p["motion"]
    assert m["source"] == "gaia" and m["tangential_km_s"] == pytest.approx(90, abs=1)
    assert m["space_km_s"] == pytest.approx(142, abs=2) and m["population"] == "thick disk"
    ca = p["path"]["closest_approach"]
    assert 11_500 < ca["year"] < 12_000 and ca["dist_ly"] == pytest.approx(3.8, abs=0.1)
    assert p["physical"]["class"] == "red dwarf"
    assert p["physical"]["radius_source"] == "Gaia FLAME" and p["physical"]["radius_rsun"] == pytest.approx(0.2, abs=0.02)
    assert p["gravity"]["ruwe"] is not None and p["gravity"]["wobble_hint"] is False
    assert {"Gaia DR3 (IRSA)", "SIMBAD"} <= set(p["sources"])


def test_wise0855_from_its_position_only():
    ra, dec = ENGINE_TARGETS_DEG["wise0855"]  # 2026.0 position
    p = _profile(ra=ra, dec=dec)
    print(p["notes"])
    assert p["identity"]["gaia_id"] is None  # too faint for Gaia
    assert p["identity"]["spectral_type"].startswith("Y") and p["identity"]["kind"] == "Brown dwarf"
    assert p["distance"]["source"] == "simbad_parallax" and p["distance"]["ly"] == pytest.approx(7.4, abs=0.2)
    assert p["physical"]["class"] == "brown dwarf" and p["physical"]["teff_k"] < 600
    assert p["motion"]["source"] == "simbad" and p["motion"]["pm"] == pytest.approx(8150, abs=100)
    assert p["path"]["closest_approach"] is None  # no radial velocity
    assert "discover" not in str(p).lower()


def test_wise0855_by_its_designation():
    p = _profile(ra=133.79513, dec=-7.24515, name="WISE J085510.83-071442.5")
    assert p["identity"]["name"].startswith("WISEA J085510")
    assert p["distance"]["source"] == "simbad_parallax"
