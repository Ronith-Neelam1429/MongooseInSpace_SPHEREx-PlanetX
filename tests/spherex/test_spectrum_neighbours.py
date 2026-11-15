"""Offline tests for the neighbour check in planetx.spherex.spectrum (synthetic SPHEREx cutouts)."""
import io
import math

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_area
from planetx.spherex import spectrum as sp
from scipy.special import erf
from synthetic import SCALE_DEG, SHAPE, make_mef

RA, DEC = 120.0, 40.0
FWHM_PX = sp.PSF_FWHM_PX[2]
SIGMA_PX = FWHM_PX / 2.3548
TOTAL = 400.0  # MJy/sr summed over pixels
PIX_ARCSEC = SCALE_DEG * 3600


def _share(offset):
    s = SIGMA_PX * math.sqrt(2)
    return 0.5 * (erf((offset + 0.5) / s) - erf((offset - 0.5) / s))


def _frame(sources, rotation_deg=30.0):
    """Synthetic detector-2 cutout with pixel-integrated Gaussian stars (ra, dec, total)."""
    hdul = fits.open(io.BytesIO(make_mef(RA, DEC, rotation_deg=rotation_deg)))
    wcs = WCS(hdul["IMAGE"].header)
    yy, xx = np.indices(SHAPE)
    image = np.full(SHAPE, 2.0)
    for ra, dec, total in sources:
        x0, y0 = wcs.world_to_pixel_values(ra, dec)
        image += total * _share(xx - x0) * _share(yy - y0)
    hdul["IMAGE"].data = image.astype(np.float32)
    hdul["IMAGE"].header["DETECTOR"] = 2
    hdul["IMAGE"].header["PSF_FWHM"] = FWHM_PX * PIX_ARCSEC
    return hdul


def _to_mjy(hdul):
    return proj_plane_pixel_area(WCS(hdul["IMAGE"].header).celestial) * (math.pi / 180) ** 2 * 1e9


def _offset(east_arcsec, north_arcsec):
    return RA + east_arcsec / 3600 / math.cos(math.radians(DEC)), DEC + north_arcsec / 3600


def test_share_is_one_on_top_and_falls_to_nothing_beyond_three_pixels():
    shares = [sp.neighbour_share(s, FWHM_PX) for s in (0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 3.5)]
    assert shares[0] == pytest.approx(1.0)
    assert all(a > b for a, b in zip(shares, shares[1:]))
    assert shares[-1] < 1e-3  # NEIGHBOUR_SEARCH_PX leaves out nothing that matters
    with pytest.raises(ValueError):
        sp.neighbour_share(-1.0, FWHM_PX)


@pytest.mark.parametrize("sep_arcsec", [4.0, 7.5, 10.0])
def test_predicted_share_matches_forced_photometry_of_a_neighbour(sep_arcsec):
    """A star `sep` away and nothing at the target: on average over field rotations and
    sub-pixel phases (they differ in every exposure), what the aperture measures there is what
    the check predicts. Single exposures scatter around it by up to ~30%."""
    ratios = []
    for k, rot in enumerate(np.linspace(0, 360, 40, endpoint=False)):
        target = _offset(0.37 * k % 3.1, 0.53 * k % 2.9)  # a different sub-pixel phase each time
        a = math.radians(17 + 222.5 * k)  # direction on the pixel grid spread over all angles
        star = (target[0] + sep_arcsec * math.sin(a) / 3600 / math.cos(math.radians(DEC)),
                target[1] + sep_arcsec * math.cos(a) / 3600, TOTAL)
        hdul = _frame([star], rotation_deg=rot)
        m = sp.measure(hdul, *target)
        assert m.status == "ok"
        ratios.append(m.flux_mjy / (TOTAL * _to_mjy(hdul)))
    predicted = sp.neighbour_share(sep_arcsec / PIX_ARCSEC, FWHM_PX)
    assert np.mean(ratios) == pytest.approx(predicted, rel=0.1, abs=0.01)


def test_wise0855_like_object_is_dominated_below_2p5um_only():
    """Invisible below 2.5 um, bright at 4 um, a star 4" away: the short bands are the star's."""
    star = sp.Neighbour("Gaia DR3 1", 4.0 / PIX_ARCSEC, sp.sed_from_magnitudes(G=16.3, J=15.4, Ks=15.0))
    bands = []
    for b in sp.BANDS:
        lam = b.center_um
        own = 1.0 if lam > 3.8 else 0.0  # mJy
        nb = star.flux_mjy(lam) * sp.neighbour_share(star.separation_px, sp.PSF_FWHM_PX[b.detector])
        bands.append(sp.BandFlux(b, own + nb, 0.002 + 0.02 * (own + nb), 3, 0))
    contam = {c.band.index: c for c in sp.contamination(bands, [star])}
    short = [c for c in contam.values() if c.band.center_um < 2.5]
    long = [c for c in contam.values() if 3.95 <= c.band.center_um <= 4.15]
    assert all(c.dominated and c.fraction == pytest.approx(1.0, rel=0.05) for c in short)
    assert not any(c.dominated for c in long)
    cleaned = sp.subtract_neighbours(bands, list(contam.values()))
    for b in cleaned:
        if b.band.center_um < 2.5:
            assert abs(b.flux_mjy) < 1e-9 and b.flux_err_mjy > bands[b.band.index].flux_err_mjy


def test_faint_or_distant_neighbours_do_not_dominate():
    bands = [sp.BandFlux(b, 1.0, 0.02, 3, 0) for b in sp.BANDS]  # a 1 mJy object
    faint = sp.Neighbour("faint", 0.5, sp.sed_from_magnitudes(J=21.0, Ks=20.0))
    far = sp.Neighbour("far", 4.0, sp.sed_from_magnitudes(J=10.0, Ks=9.5))
    assert not any(c.dominated for c in sp.contamination(bands, [faint, far]))
    assert all(c.neighbour_mjy == 0 for c in sp.contamination(bands, [far]))  # beyond NEIGHBOUR_SEARCH_PX
    assert all(c.fraction == 0 for c in sp.contamination(bands, []))


def test_a_neighbour_below_the_noise_never_dominates():
    """The reference is max(flux, 2 x error): a noise-level object is not 'dominated' by a
    neighbour that adds less than its noise."""
    bands = [sp.BandFlux(b, 0.0, 1.0, 1, 0) for b in sp.BANDS[:5]]
    nb = sp.Neighbour("x", 0.0, ((1.0, 0.5),))  # adds 0.5 mJy, noise is 1 mJy
    assert not any(c.dominated for c in sp.contamination(bands, [nb]))
    nb = sp.Neighbour("x", 0.0, ((1.0, 1.5),))
    assert all(c.dominated for c in sp.contamination(bands, [nb]))


def test_sed_from_magnitudes_uses_the_zero_points_and_sorts():
    sed = sp.sed_from_magnitudes(Ks=0.0, J=0.0, G=None)
    assert sed == ((1.235, pytest.approx(1594e3)), (2.159, pytest.approx(666.7e3)))
    assert sp.sed_from_magnitudes(J=5.0)[0][1] == pytest.approx(1594e3 / 100)
    with pytest.raises(ValueError):
        sp.sed_from_magnitudes(W1=10.0)


def test_neighbour_sed_interpolates_and_has_a_rayleigh_jeans_tail():
    n = sp.Neighbour("x", 1.0, ((1.0, 10.0), (2.0, 5.0)))
    assert n.flux_mjy(0.8) == 10.0  # flat bluewards
    assert n.flux_mjy(1.5) == pytest.approx(10.0 * 1.5**-1.0)  # power law through the points
    assert n.flux_mjy(4.0) == pytest.approx(5.0 * 2.0**-2.0)  # lambda^-2 beyond the last
    assert sp.Neighbour("empty", 1.0, ()).flux_mjy(2.0) == 0.0
