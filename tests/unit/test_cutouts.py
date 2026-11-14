"""Offline tests for planetx.cutouts: grid orientation, masking, cache, encodings."""
import io

import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.wcs import WCS
from PIL import Image

from planetx import cutouts, irsa
from synthetic import SHAPE, make_mef, synthetic_header, wavelength_at_raw_pixel

RA, DEC = 120.0, 40.0
URL = "https://irsa.ipac.caltech.edu/ibe/data/spherex/qr3/level2/x/3/level2_test_D3.fits"
SIZE = 0.05  # -> 29 x 29 px output grid, centre pixel (14, 14)
STEP = 6.15 / 3600  # one output pixel, deg


def _write(tmp_path, body, name="c.fits"):
    path = tmp_path / name
    path.write_bytes(body)
    return path


def _centroid(data, row, col, half=3):
    box = (slice(row - half, row + half + 1), slice(col - half, col + half + 1))
    yy, xx = np.indices(data.shape)
    w = np.clip(np.nan_to_num(data[box]), 0, None)
    return (w * yy[box]).sum() / w.sum(), (w * xx[box]).sum() / w.sum()


def _shifted(d_north, d_east):
    from astropy import units as u

    c = SkyCoord(RA * u.deg, DEC * u.deg).spherical_offsets_by(d_east * u.arcsec, d_north * u.arcsec)
    return c.ra.deg, c.dec.deg


def test_grid_is_odd_sized_and_centred_on_target():
    wcs, shape = cutouts.grid_wcs(RA, DEC, SIZE)
    assert shape == (29, 29)
    ra, dec = wcs.pixel_to_world_values(14, 14)
    assert (ra, dec) == pytest.approx((RA, DEC))
    assert cutouts.grid_wcs(RA, DEC, 0.1)[1] == (59, 59)
    assert cutouts.grid_wcs(RA, DEC, 0.01)[1] == (7, 7)


@pytest.mark.parametrize("rotation", [0.0, 30.0, 137.0])
def test_align_puts_sources_north_up_east_left(tmp_path, rotation):
    """A source 60" north lands ~9.8 rows ABOVE centre; 60" east ~9.8 columns LEFT,
    whatever the input rotation/parity."""
    north = _shifted(60, 0)
    east = _shifted(0, 60)
    body = make_mef(RA, DEC, sources=[(*north, 100.0), (*east, 50.0)], rotation_deg=rotation)
    out = cutouts.align(_write(tmp_path, body), RA, DEC, SIZE)
    d = 60 / 6.15
    assert out.data.shape == (29, 29)

    row, col = _centroid(out.data, 14 - 10, 14)
    assert (row, col) == pytest.approx((14 - d, 14), abs=0.1)
    row, col = _centroid(out.data, 14, 14 - 10)
    assert (row, col) == pytest.approx((14, 14 - d), abs=0.1)
    # The returned WCS describes the array as stored.
    x, y = out.wcs.world_to_pixel_values(*north)
    assert (y, x) == pytest.approx((14 - d, 14), abs=0.01)


def test_two_rotations_align_pixel_for_pixel(tmp_path):
    src = [(*_shifted(-20, 12), 80.0)]
    a = cutouts.align(_write(tmp_path, make_mef(RA, DEC, src, rotation_deg=10.0), "a.fits"), RA, DEC, SIZE)
    b = cutouts.align(_write(tmp_path, make_mef(RA, DEC, src, rotation_deg=200.0), "b.fits"), RA, DEC, SIZE)
    peak = np.unravel_index(np.nanargmax(a.data), a.data.shape)
    assert np.unravel_index(np.nanargmax(b.data), b.data.shape) == peak
    assert np.allclose(_centroid(a.data, *peak), _centroid(b.data, *peak), atol=0.1)


def test_align_reads_metadata_and_wavelength_without_sip(tmp_path):
    out = cutouts.align(_write(tmp_path, make_mef(RA, DEC)), RA, DEC, SIZE)
    x0, _ = WCS(synthetic_header(RA, DEC)).world_to_pixel_values(RA, DEC)
    assert out.wavelength_um == pytest.approx(wavelength_at_raw_pixel(x0), abs=1e-5)
    assert out.mjd == 60902.5 and out.bunit == "MJy/sr" and out.detector == 3


def test_align_masks_unusable_flags_but_keeps_source_flag(tmp_path):
    flags = np.zeros(SHAPE, np.int32)
    flags[:, : SHAPE[1] // 2] = 1 << cutouts.FLAG_BITS["HOT"]  # half the input is bad
    flags[:, SHAPE[1] // 2:] = 1 << cutouts.FLAG_BITS["SOURCE"]  # informational only
    out = cutouts.align(_write(tmp_path, make_mef(RA, DEC, flags=flags, rotation_deg=0.0)), RA, DEC, SIZE)
    nan = np.isnan(out.data)
    assert 0.3 < nan.mean() < 0.7, "HOT half should be NaN, SOURCE half kept"

    strict = cutouts.align(_write(tmp_path, make_mef(RA, DEC, flags=flags, rotation_deg=0.0)), RA, DEC, SIZE,
                           mask_flags=("SOURCE", "HOT"))
    assert np.isnan(strict.data).all()


def test_area_outside_the_input_is_nan(tmp_path):
    out = cutouts.align(_write(tmp_path, make_mef(RA, DEC)), RA, DEC, 0.5)  # grid > input
    assert np.isnan(out.data[0, 0]) and np.isfinite(out.data[out.data.shape[0] // 2, out.data.shape[1] // 2])


def test_flag_bits_match_documented_values():
    # Values from the MSKNnnnn cards of a live FLAGS header and IRSA's docs.
    assert cutouts.flag_mask(cutouts.UNUSABLE_FLAGS) == (1 << 6) | (1 << 9) | (1 << 10) | (1 << 11)
    assert cutouts.FLAG_BITS["BLOOM"] == 26 and cutouts.FLAG_BITS["SATELLITE_HALO"] == 29


# --- cache ---------------------------------------------------------------------------


def test_cache_downloads_once_and_reuses(cache_dir, monkeypatch):
    calls = []
    body = make_mef(RA, DEC)

    def fake_fetch(url, ra, dec, size):
        calls.append((url, ra, dec, size))
        return body

    monkeypatch.setattr(irsa, "fetch_cutout", fake_fetch)
    p1 = cutouts.cached_cutout(URL, RA, DEC, 0.075)
    p2 = cutouts.cached_cutout(URL, RA, DEC, 0.075)
    assert p1 == p2 and p1.read_bytes() == body
    assert calls == [(URL, RA, DEC, 0.075)]
    assert p1.is_relative_to(cache_dir)
    # A different centre or size is a different entry.
    assert cutouts.cached_cutout(URL, RA, DEC + 0.01, 0.075) != p1
    assert len(calls) == 2
    assert not list(cache_dir.rglob("*.tmp"))


def test_failed_download_leaves_no_cache_entry(cache_dir, monkeypatch):
    def failing(*args):
        raise irsa.UpstreamError("HTTP 503: busy", status=503, transient=True)

    monkeypatch.setattr(irsa, "fetch_cutout", failing)
    with pytest.raises(irsa.UpstreamError):
        cutouts.cached_cutout(URL, RA, DEC, 0.075)
    assert not [p for p in cache_dir.rglob("*") if p.is_file()]


def test_crash_during_write_leaves_no_partial_file(cache_dir, monkeypatch):
    monkeypatch.setattr(irsa, "fetch_cutout", lambda *a: make_mef(RA, DEC))

    def crash(fd):
        raise OSError("disk full")

    monkeypatch.setattr(cutouts.os, "fsync", crash)
    with pytest.raises(OSError, match="disk full"):
        cutouts.cached_cutout(URL, RA, DEC, 0.075)
    assert not [p for p in cache_dir.rglob("*") if p.is_file()]


def test_concurrent_requests_for_one_cutout_download_it_once(cache_dir, monkeypatch):
    import threading
    import time

    calls = []

    def slow_fetch(*args):
        calls.append(args)
        time.sleep(0.2)
        return make_mef(RA, DEC)

    monkeypatch.setattr(irsa, "fetch_cutout", slow_fetch)
    paths = []
    threads = [threading.Thread(target=lambda: paths.append(cutouts.cached_cutout(URL, RA, DEC, 0.075)))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and len(set(paths)) == 1 and len(paths) == 4


def test_cutout_request_snaps_centre_and_pads_size():
    ra, dec, size = cutouts.cutout_request(10.00049, -20.0006, 0.1)
    assert (ra, dec) == (10.0, -20.001)
    assert size == pytest.approx(0.15 + 4 * 6.15 / 3600, abs=1e-5)
    assert cutouts.cutout_request(359.9997, 89.9999, 0.1)[:2] == (0.0, 90.0)
    # Nearby positions share one cached cutout.
    assert cutouts.cutout_request(10.0001, 5.0001, 0.1) == cutouts.cutout_request(10.0002, 5.0002, 0.1)


def test_align_masks_infinite_pixels(tmp_path):
    from astropy.io import fits

    body = make_mef(RA, DEC, rotation_deg=0.0)
    with fits.open(io.BytesIO(body)) as hdul:
        hdul["IMAGE"].data[30, 30] = np.inf
        buf = io.BytesIO()
        hdul.writeto(buf)
    out = cutouts.align(_write(tmp_path, buf.getvalue()), RA, DEC, SIZE)
    assert not np.isinf(out.data).any() and np.isnan(out.data).any()


def test_cached_cutout_rejects_disallowed_url(cache_dir, monkeypatch):
    monkeypatch.setattr(irsa, "fetch_cutout", lambda *a: pytest.fail("must not fetch"))
    with pytest.raises(ValueError):
        cutouts.cached_cutout("https://evil.example/ibe/data/spherex/x.fits", RA, DEC, 0.1)


# --- encodings -----------------------------------------------------------------------


def test_float32_base64_round_trip_preserves_nan_and_layout():
    data = np.arange(12, dtype=np.float64).reshape(3, 4) * 1.5
    data[0, 1] = np.nan
    data[2, 3] = -1e-30
    b64 = cutouts.encode_float32(data)
    back = cutouts.decode_float32(b64, width=4, height=3)
    assert back.dtype == np.dtype("<f4") and back.shape == (3, 4)
    np.testing.assert_array_equal(back, data.astype(np.float32))
    # Little-endian, row-major: first value is row 0 col 0, then row 0 col 1 (NaN).
    raw = np.frombuffer(__import__("base64").b64decode(b64), dtype="<f4")
    assert raw[0] == 0.0 and np.isnan(raw[1]) and raw[4] == 6.0


def test_png_is_gray_alpha_with_transparent_nan_and_rows_preserved():
    data = np.zeros((5, 4))
    data[0, :] = 10.0  # bright top row
    data[4, 0] = np.nan
    img = Image.open(io.BytesIO(cutouts.render_png(data, vmin=0, vmax=10)))
    assert img.mode == "LA" and img.size == (4, 5)
    px = np.asarray(img)
    assert (px[0, :, 0] == 255).all() and (px[1:4, :, 0] == 0).all()
    assert px[4, 0, 1] == 0 and (px[:4, :, 1] == 255).all()


def test_png_handles_all_nan_and_flat_images():
    for data in (np.full((3, 3), np.nan), np.ones((3, 3))):
        img = Image.open(io.BytesIO(cutouts.render_png(data)))
        assert img.size == (3, 3)
