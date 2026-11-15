"""Live checks that the three catalogue fetchers still get what the rules need.

Each test hits the real service and checks content against a known answer.
"""
import math

import pytest

from planetx.classify.catalogs import fetch_gaia, fetch_simbad, fetch_skybot
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
