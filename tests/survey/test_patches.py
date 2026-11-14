"""Offline tests for HEALPix patch ordering and geometry."""
import astropy.units as u
import numpy as np
import pytest
from astropy.coordinates import SkyCoord

from planetx.survey import patches
from planetx.survey.patches import (
    NEP,
    crawl_order,
    moc_order,
    n_patches,
    nep_distance,
    patch_center,
    patch_containing,
    patch_corners,
)


def test_nep_is_the_j2000_ecliptic_pole():
    assert NEP.ra.deg == pytest.approx(270.0, abs=1e-4)
    assert NEP.dec.deg == pytest.approx(66.5607, abs=1e-4)


@pytest.mark.parametrize("nside", [1, 2, 8, 64])
def test_crawl_order_covers_every_patch_once(nside):
    order = crawl_order(nside)
    assert len(order) == n_patches(nside) == 12 * nside**2
    assert np.array_equal(np.sort(order), np.arange(12 * nside**2))


@pytest.mark.parametrize("nside", [1, 2, 8, 64])
def test_crawl_starts_at_the_patch_containing_the_nep(nside):
    order = crawl_order(nside)
    assert order[0] == patch_containing(NEP.ra.deg, NEP.dec.deg, nside)


@pytest.mark.parametrize("nside", [1, 2, 8, 64])
def test_crawl_moves_outward_from_the_nep(nside):
    dist = nep_distance(crawl_order(nside), nside)
    # Rank 0 is forced to the patch containing the NEP; the rest is sorted.
    assert np.all(np.diff(dist[1:]) >= -1e-9)
    assert dist[-1] > 180 - 60 / nside  # ends near the south ecliptic pole (~59/nside deg patches)


def test_default_crawl_is_deterministic():
    assert np.array_equal(crawl_order(), crawl_order())
    assert patches.DEFAULT_NSIDE == 64


def test_nested_indices_are_valid_moc_cells():
    # NESTED: the 4 children of a cell at order k are 4*ipix + 0..3 at order k+1.
    ra, dec = 101.3, -16.7
    parent = patch_containing(ra, dec, 8)
    child = patch_containing(ra, dec, 16)
    assert child // 4 == parent


def test_patch_center_and_corners():
    nside = 64
    ipix = crawl_order(nside)[0]
    ra, dec = patch_center(ipix, nside)
    assert patch_containing(ra, dec, nside) == ipix
    corners = patch_corners(ipix, nside)
    assert len(corners) == 4
    centre = SkyCoord(ra * u.deg, dec * u.deg)
    for c_ra, c_dec in corners:
        sep = centre.separation(SkyCoord(c_ra * u.deg, c_dec * u.deg)).deg
        assert 0.3 < sep < 1.0  # patches are ~0.92 deg across
    # The NEP lies inside the first patch: inside the corners' bounding box here.
    ras, decs = zip(*corners)
    assert min(ras) <= NEP.ra.deg <= max(ras)
    assert min(decs) <= NEP.dec.deg <= max(decs)


def test_moc_order():
    assert moc_order(1) == 0
    assert moc_order(64) == 6


@pytest.mark.parametrize("bad", [0, 3, 48, 2048, -4, True, 64.0, "64"])
def test_invalid_nside_rejected(bad):
    with pytest.raises(ValueError):
        crawl_order(bad)


@pytest.mark.parametrize("bad", [-1, 12 * 64**2, 1.5, True])
def test_invalid_ipix_rejected(bad):
    with pytest.raises(ValueError):
        patch_center(bad, 64)


@pytest.mark.parametrize("ra, dec", [(float("nan"), 0.0), (10.0, 91.0), (10.0, float("inf"))])
def test_invalid_position_rejected(ra, dec):
    with pytest.raises(ValueError):
        patch_containing(ra, dec)
