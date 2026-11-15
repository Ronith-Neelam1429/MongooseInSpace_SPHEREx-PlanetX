"""Offline tests for planetx.spherex.cutouts: grid orientation, masking, cache, encodings."""
import io

import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.wcs import WCS
from PIL import Image

from planetx.spherex import archive, cutouts
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

    monkeypatch.setattr(archive, "fetch_cutout", fake_fetch)
    p1 = cutouts.cached_cutout(URL, RA, DEC, 0.075)
    p2 = cutouts.cached_cutout(URL, RA, DEC, 0.075)
    assert p1 == p2 and p1.read_bytes() == body
    assert calls == [(URL, RA, DEC, 0.075)]
    assert p1.is_relative_to(cache_dir)
    # A different centre or size is a different entry.
    assert cutouts.cached_cutout(URL, RA, DEC + 0.01, 0.075) != p1
    assert len(calls) == 2
    assert not list(cache_dir.rglob("*.tmp"))


def test_cache_evicts_least_recently_used_beyond_cap(tmp_path, monkeypatch):
    """Cap 1 MB, 400 kB entries: the third download evicts the least recently used."""
    import os

    from planetx.config import Settings

    monkeypatch.setattr(cutouts, "settings", Settings(cache_dir=tmp_path, cache_max_mb=1))
    monkeypatch.setattr(archive, "fetch_cutout", lambda *a: b"x" * 400_000)
    a = cutouts.cached_cutout(URL, RA, DEC, 0.075)
    b = cutouts.cached_cutout(URL, RA, DEC + 0.01, 0.075)
    os.utime(a, (1_000, 1_000))
    os.utime(b, (2_000, 2_000))
    assert cutouts.cached_cutout(URL, RA, DEC, 0.075) == a  # hit: a is now most recent
    c = cutouts.cached_cutout(URL, RA, DEC + 0.02, 0.075)  # 1.2 MB > cap: evict LRU (b)
    assert a.exists() and c.exists() and not b.exists()


def test_cache_keeps_new_entry_even_if_larger_than_cap(tmp_path, monkeypatch):
    from planetx.config import Settings

    monkeypatch.setattr(cutouts, "settings", Settings(cache_dir=tmp_path, cache_max_mb=1))
    monkeypatch.setattr(archive, "fetch_cutout", lambda *a: b"x" * 2_000_000)
    old = cutouts.cached_cutout(URL, RA, DEC, 0.075)
    new = cutouts.cached_cutout(URL, RA, DEC + 0.01, 0.075)
    assert new.exists() and not old.exists()


def test_aligned_cutout_refetches_if_evicted_before_open(cache_dir, monkeypatch):
    body = make_mef(RA, DEC)
    calls = []
    monkeypatch.setattr(archive, "fetch_cutout", lambda *a: calls.append(a) or body)
    real_align = cutouts.align

    def align_after_eviction(path, *args):
        if len(calls) == 1:
            path.unlink()  # another request evicted it in between
        return real_align(path, *args)

    monkeypatch.setattr(cutouts, "align", align_after_eviction)
    out = cutouts.aligned_cutout(URL, RA, DEC, SIZE)
    assert len(calls) == 2 and np.isfinite(out.data).any()


@pytest.mark.parametrize("raw", ["0", "-5", "lots"])
def test_cache_cap_setting_rejects_invalid_values(monkeypatch, raw):
    from planetx.config import Settings

    monkeypatch.setenv("PLANETX_CACHE_MAX_MB", raw)
    with pytest.raises(ValueError, match="PLANETX_CACHE_MAX_MB"):
        Settings()


def test_failed_download_leaves_no_cache_entry(cache_dir, monkeypatch):
    def failing(*args):
        raise archive.UpstreamError("HTTP 503: busy", status=503, transient=True)

    monkeypatch.setattr(archive, "fetch_cutout", failing)
    with pytest.raises(archive.UpstreamError):
        cutouts.cached_cutout(URL, RA, DEC, 0.075)
    assert not [p for p in cache_dir.rglob("*") if p.is_file()]


def test_crash_during_write_leaves_no_partial_file(cache_dir, monkeypatch):
    monkeypatch.setattr(archive, "fetch_cutout", lambda *a: make_mef(RA, DEC))

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

    monkeypatch.setattr(archive, "fetch_cutout", slow_fetch)
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
    monkeypatch.setattr(archive, "fetch_cutout", lambda *a: pytest.fail("must not fetch"))
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


# --- pinning ---------------------------------------------------------------------------


def test_pinned_entries_survive_eviction_until_unpinned(tmp_path, monkeypatch):
    import os

    from planetx.config import Settings

    monkeypatch.setattr(cutouts, "settings", Settings(cache_dir=tmp_path, cache_max_mb=1))
    monkeypatch.setattr(archive, "fetch_cutout", lambda *a: b"x" * 400_000)
    old = cutouts.cutout_path(URL, RA, DEC, 0.075)
    with cutouts.pinned([old]), cutouts.pinned([old]):  # pins nest
        assert cutouts.cached_cutout(URL, RA, DEC, 0.075) == old
        os.utime(old, (1_000, 1_000))  # least recently used
        for i in range(1, 4):  # 1.6 MB > cap: everything unpinned but the newest goes
            cutouts.cached_cutout(URL, RA, DEC + 0.01 * i, 0.075)
        assert old.exists()
    assert not cutouts._pinned
    cutouts.cached_cutout(URL, RA, DEC + 0.05, 0.075)
    assert not old.exists()


# --- cropping --------------------------------------------------------------------------


def _big_cutout(sources=(), flags=None):
    """A synthetic raw cutout with the live layout: alternate-key A and W cards, ZODI and EPSF."""
    from astropy.io import fits

    hdul = fits.open(io.BytesIO(make_mef(RA, DEC, sources=sources, flags=flags)))
    for name in ("IMAGE", "FLAGS", "VARIANCE"):
        h = hdul[name].header
        if name != "IMAGE":  # live FLAGS/VARIANCE carry the same WCS cards
            h.update({k: v for k, v in hdul["IMAGE"].header.items() if k.startswith(("CRPIX", "CRVAL", "CTYPE"))})
        h["CRPIX1A"], h["CRPIX2A"] = h["CRPIX1W"], h["CRPIX2W"]
    hdul.append(fits.ImageHDU(np.arange(SHAPE[0] * SHAPE[1], dtype=np.float32).reshape(SHAPE),
                              header=hdul["IMAGE"].header, name="ZODI"))
    hdul.append(fits.BinTableHDU.from_columns([fits.Column(name="PSF", format="E", array=np.ones(3))], name="EPSF"))
    return hdul


def test_crop_is_the_sub_array_with_shifted_reference_pixels():
    big = _big_cutout()
    ra, dec = _shifted(40, -25)  # 40" north, 25" west of the big cutout's centre
    x0, x1, y0, y1 = cutouts.crop_box(cutouts.cutout_wcs(big["IMAGE"].header), SHAPE, ra, dec, 0.02)
    assert (x1 - x0, y1 - y0) == (12, 12)  # 0.02 deg at 6.15"/px = 11.7 -> 12 px
    crop = cutouts.crop_cutout(big, ra, dec, 0.02)
    assert [h.name for h in crop] == ["PRIMARY", "IMAGE", "FLAGS", "VARIANCE", "ZODI", "WCS-WAVE"]  # no EPSF
    for name in ("IMAGE", "FLAGS", "VARIANCE", "ZODI"):
        np.testing.assert_array_equal(crop[name].data, big[name].data[y0:y1, x0:x1])
        for key in ("CRPIX1", "CRPIX1A", "CRPIX1W"):
            assert crop[name].header[key] == big[name].header[key] - x0
        for key in ("CRPIX2", "CRPIX2A", "CRPIX2W"):
            assert crop[name].header[key] == big[name].header[key] - y0
    assert crop["IMAGE"].header["A_0_0"] == big["IMAGE"].header["A_0_0"]  # SIP is relative to CRPIX
    np.testing.assert_array_equal(crop["WCS-WAVE"].data["VALUES"], big["WCS-WAVE"].data["VALUES"])


def test_cropped_frame_sees_the_same_sky_wavelength_and_flags():
    from planetx.engine import frame_from_hdul

    star = _shifted(30, 20)
    wcs = WCS(synthetic_header(RA, DEC))
    sx, sy = (int(round(float(c))) for c in wcs.world_to_pixel_values(*star))
    flags = np.zeros(SHAPE, np.int32)
    flags[sy, sx] = 1 << 1  # OVERFLOW (saturated) on the star's pixel
    big = _big_cutout(sources=[(*star, 100.0)], flags=flags)
    big["FLAGS"].header.update({"MSKN0001": "OVERFLOW", "MSKM0001": 2})
    centre = _shifted(25, 15)
    crop = cutouts.crop_cutout(big, *centre, 0.03)
    x0, _, y0, _ = cutouts.crop_box(cutouts.cutout_wcs(big["IMAGE"].header), SHAPE, *centre, 0.03)
    whole, part = frame_from_hdul(big), frame_from_hdul(crop)
    # The same sky position maps to the same detector pixel, SIP distortion included.
    bx, by = (float(c) for c in whole.wcs.world_to_pixel_values(*star))
    cx, cy = (float(c) for c in part.wcs.world_to_pixel_values(*star))
    assert (cx + x0, cy + y0) == pytest.approx((bx, by), abs=1e-9)
    assert part.saturated[sy - y0, sx - x0] and part.saturated.sum() == 1
    ny, nx = part.data.shape
    assert part.wavelength_um == pytest.approx(wavelength_at_raw_pixel(x0 + (nx - 1) / 2), abs=1e-6)
    assert part.mjd == whole.mjd and part.detector == whole.detector


def test_crop_at_the_edge_is_clipped_and_off_the_cutout_is_none():
    big = _big_cutout()
    wcs = WCS(synthetic_header(RA, DEC))
    edge = wcs.pixel_to_world_values(1.0, 30.0)  # 1 px from the left edge
    crop = cutouts.crop_cutout(big, *edge, 0.03)
    assert crop["IMAGE"].data.shape[0] == 18 and crop["IMAGE"].data.shape[1] < 18
    assert cutouts.crop_cutout(big, *wcs.pixel_to_world_values(-3.0, 30.0), 0.03) is None
    assert cutouts.crop_cutout(big, RA + 10, DEC, 0.03) is None


@pytest.mark.parametrize(("x", "y", "x0", "y0"), [
    # Live pair (Nov 2026): a 0.30683 deg field request whose centre fell on pixel
    # (547.754, 893.441) of a 2.1 deg cutout came back as the 180 px box from (458, 804).
    (547.754, 893.441, 458, 804),
    # Ends of the range seen over 106 live field cutouts: centre - origin in [89.0, 90.0).
    (300.001, 300.999, 211, 211),
    (299.999, 301.0, 210, 212),
])
def test_crop_box_follows_irsas_rule(x, y, x0, y0):
    wcs = cutouts.cutout_wcs(synthetic_header(RA, DEC))
    ra, dec = (float(c) for c in wcs.pixel_to_world_values(x, y))
    assert cutouts.crop_box(wcs, (2000, 2000), ra, dec, 0.30683) == (x0, x0 + 180, y0, y0 + 180)


def test_unpinned_entries_keep_a_floor_when_pins_fill_the_cap(tmp_path, monkeypatch):
    """Pins alone at the cap: the newest unpinned entries still keep UNPINNED_FLOOR of it."""
    import os

    from planetx.config import Settings

    monkeypatch.setattr(cutouts, "settings", Settings(cache_dir=tmp_path, cache_max_mb=1))
    monkeypatch.setattr(archive, "fetch_cutout", lambda *a: b"x" * 200_000)
    pins = [cutouts.cutout_path(URL, RA, DEC + 0.01 * i, 0.075) for i in range(6)]  # 1.2 MB pinned
    with cutouts.pinned(pins):
        for i, p in enumerate(pins):
            cutouts.cached_cutout(URL, RA, DEC + 0.01 * i, 0.075)
            os.utime(p, (1_000 + i, 1_000 + i))
        oldest = cutouts.cached_cutout(URL, RA + 0.01, DEC, 0.075)
        os.utime(oldest, (2_000, 2_000))
        older = cutouts.cached_cutout(URL, RA + 0.02, DEC, 0.075)
        os.utime(older, (3_000, 3_000))
        newest = cutouts.cached_cutout(URL, RA + 0.03, DEC, 0.075)
        # Besides the newest (just written), 0.4 MB unpinned > the 0.25 MB floor: the least
        # recently used goes, the next one stays although the cache is above its cap.
        assert all(p.exists() for p in pins)
        assert newest.exists() and older.exists() and not oldest.exists()
