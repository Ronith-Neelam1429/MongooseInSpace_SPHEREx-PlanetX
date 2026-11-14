"""Live tests: IRSA archival catalogs used to extend the SPHEREx time baseline.

Run with `uv run pytest tests/connections -v` (needs network).
"""
from dataclasses import dataclass

import pytest

from planetx.observatories import Observatory, Verdict, fetch_catalog, refine_objects
from planetx.observatories.fusion import _deproject

pytestmark = pytest.mark.network

# Barnard's Star, Gaia DR3 (epoch 2016.0 = MJD 57388.5); motion in arcsec/yr.
BARNARD = (269.44850252, 4.73942005, 57388.5, (-0.80171, 10.36234))
# WISE 0855-0714: AllWISE position (mean MJD 55412.3).
W0855 = (133.7947597, -7.2451456)


@dataclass
class Epoch:
    ra: float
    dec: float
    mjd: float
    sigma_arcsec: float = 0.4


@dataclass
class Track:
    sources: list


def test_2mass_has_barnards_star_in_2000():
    sources = fetch_catalog(Observatory.TWOMASS, 269.4521, 4.6934, 40.0)
    star = min(sources, key=lambda s: s.mag)
    assert star.name == "17574849+0441405"
    assert star.mag == pytest.approx(4.52, abs=0.05) and star.band == "Ks"
    assert 51600 < star.mjd < 51800  # observed in mid-2000


def test_allwise_and_catwise_see_wise0855():
    allwise = fetch_catalog(Observatory.ALLWISE, *W0855, 10.0)
    assert any(s.name == "J085510.74-071442.5" and 55300 < s.mjd < 55500 for s in allwise)
    # By the CatWISE epoch (2015.4) it had moved ~39" west.
    catwise = fetch_catalog(Observatory.CATWISE, *W0855, 60.0)
    assert any(s.name == "J085508.34-071439.2" for s in catwise)


def test_neowise_single_exposures_are_binned():
    sources = fetch_catalog(Observatory.NEOWISE, *W0855, 30.0)
    assert sources and all(s.observatory is Observatory.NEOWISE for s in sources)
    assert max(s.mjd for s in sources) > 60000  # final (2024) release is in
    assert len({s.visit for s in sources}) >= 10


def test_barnard_motion_improves_with_archival_data():
    """Simulated SPHEREx epochs (0.3" errors) + the real 2MASS catalog."""
    ra, dec, mjd0, pm = BARNARD
    jitter = [(0.3, -0.2), (-0.25, 0.3), (0.2, 0.1)]
    epochs = []
    for mjd, (je, jn) in zip((60920.0, 61120.0, 61290.0), jitter):
        dt = (mjd - mjd0) / 365.25
        epochs.append(Epoch(*_deproject(pm[0] * dt + je, pm[1] * dt + jn, ra, dec), mjd))
    track = Track(epochs)

    alone = refine_objects([track], observatories="spherex").fused[0]
    both = refine_objects([track], observatories="multi").fused[0]
    assert alone.verdict is Verdict.SPHEREX_ONLY
    assert both.verdict is Verdict.CONFIRMED
    assert Observatory.TWOMASS in both.observatories
    assert both.pm_err < alone.pm_err / 20
    assert (both.pm_ra_cosdec, both.pm_dec) == pytest.approx(pm, abs=0.03)


def test_wise0855_is_followed_through_allwise_and_catwise():
    """Simulated SPHEREx epochs of an 8"/yr brown dwarf + the real WISE catalogs."""
    pm = (-8.118, 0.680)  # Luhman 2014
    jitter = [(0.3, -0.2), (-0.25, 0.3), (0.2, 0.1)]
    epochs = []
    for mjd, (je, jn) in zip((60920.0, 61120.0, 61290.0), jitter):
        dt = (mjd - 55412.3) / 365.25
        epochs.append(Epoch(*_deproject(pm[0] * dt + je, pm[1] * dt + jn, *W0855), mjd))
    f = refine_objects([Track(epochs)], observatories="multi").fused[0]
    assert f.verdict is Verdict.CONFIRMED
    assert {Observatory.ALLWISE, Observatory.CATWISE} <= set(f.observatories)
    assert (f.pm_ra_cosdec, f.pm_dec) == pytest.approx(pm, abs=0.05)
