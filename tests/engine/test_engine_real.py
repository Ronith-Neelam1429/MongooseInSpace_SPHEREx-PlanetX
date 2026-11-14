"""Engine regression tests on the challenge's real SPHEREx targets.

Network on first run (cutouts are cached under .cache/dev afterwards).
Expected motions are Gaia DR3 / literature values; tolerances reflect what
SPHEREx's 6.15" pixels and ~1 yr baseline allow.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from astropy import log
from astropy.coordinates import SkyCoord
from astropy.io import fits

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "dev"))
from fetch import fetch_all, find_visits, pick_visits, resolve  # noqa: E402

from planetx.engine import find_motion, frame_from_hdul  # noqa: E402

pytestmark = [pytest.mark.network, pytest.mark.timeout(900)]
log.setLevel("WARNING")


def run(target: str, detectors=None, per_pass: int = 6, stack: bool = False):
    coord, default = resolve(target)
    visits = pick_visits(find_visits(coord, detectors or default), per_pass)
    frames = []
    for v, path in fetch_all(visits, coord, 0.1):
        with fits.open(path) as h:
            frames.append(frame_from_hdul(h, v.obs_id))
    return coord, find_motion(frames, stack=stack)


def sep(coord, obj) -> float:
    return coord.separation(SkyCoord(obj.ra, obj.dec, unit="deg")).arcsec


def angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


@pytest.mark.parametrize(
    "target,kw,pm,pa,pm_tol",
    [
        # Gaia DR3: 10.39"/yr at PA 355.6
        ("barnard", {"detectors": (1, 2, 3, 4, 5, 6)}, 10.39, 355.6, 0.05),
        # Eps Ind Ba/Bb system: ~4.7"/yr at PA ~122
        ("eps-indi-b", {}, 4.68, 121.8, 0.10),
        ("eps-indi-b", {"stack": True}, 4.68, 121.8, 0.10),
    ],
)
def test_known_movers_are_the_only_movers(target, kw, pm, pa, pm_tol):
    coord, r = run(target, **kw)
    assert len(r.movers) == 1, [(m.pm_total, sep(coord, m)) for m in r.movers]
    m = r.movers[0]
    assert sep(coord, m) < 10
    assert m.pm_total == pytest.approx(pm, rel=pm_tol)
    assert angle_diff(m.position_angle_deg, pa) < 12
    assert m.pm_significance >= 5


def test_r_draconis_is_variable_not_moving():
    coord, r = run("r-dra", per_pass=30)
    assert r.movers == []
    (rdra,) = [o for o in r.variables if sep(coord, o) < 5]
    assert rdra.variability["ratio"] > 1.25


def test_wise0855_motion_measured_in_stacks():
    # Too faint for single frames; found in per-pass stacks. Direction and
    # speed come out right, but its stacked positions scatter more than their
    # errors allow (and more than a physical parallax explains), so its
    # significance is ~3 sigma and the engine rightly withholds "mover".
    # Literature: ~8.1"/yr at PA ~275.
    coord, r = run("wise0855", stack=True)
    assert r.movers == []
    (w,) = [o for o in r.objects if sep(coord, o) < 10]
    assert w.pm_total == pytest.approx(8.1, rel=0.15)
    assert angle_diff(w.position_angle_deg, 275.0) < 12
    assert w.pm_significance > 2.5
