"""Live tests of the sky endpoints against IRSA (Barnard's Star).

Run with: uv run pytest tests/api -m network
"""
import io
from urllib.parse import parse_qs, urlsplit

import astropy.units as u
import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.time import Time
from PIL import Image

from targets import (
    BARNARD_DEC_DEG, BARNARD_DISTANCE_PC, BARNARD_PMDEC, BARNARD_PMRA_COSDEC, BARNARD_RA_DEG,
)
from planetx.spherex import archive, cutouts

pytestmark = pytest.mark.network

SIZE_DEG = 0.1  # -> 59 x 59 px grid; Barnard moved ~100" (16 px) north since J2016
POS = {"ra": BARNARD_RA_DEG, "dec": BARNARD_DEC_DEG}


@pytest.fixture(scope="module")
def visits(client):
    resp = client.get("/api/visits", params=POS)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture(scope="module")
def first_frame(client):
    resp = client.get("/api/frames", params={**POS, "size_deg": SIZE_DEG})
    assert resp.status_code == 200, resp.text
    frames = resp.json()["frames"]
    assert frames, "no frames at Barnard's Star"
    return frames[0]


@pytest.fixture(scope="module")
def first_cutout(client, first_frame):
    resp = client.get(first_frame["data_url"])
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_visits_at_barnard_span_at_least_three_passes(visits):
    passes = visits["passes"]
    print(f"\n[visits] n={visits['n_visits']} passes={[(p['start'], p['end'], p['n_visits']) for p in passes]}")
    assert len(passes) >= 3
    assert sum(p["n_visits"] for p in passes) == visits["n_visits"] == len(visits["visits"])
    rows = visits["visits"]
    assert [v["mjd"] for v in rows] == sorted(v["mjd"] for v in rows)
    assert {v["collection"] for v in rows} == {"spherex_dr1", "spherex_qr3"}
    assert {v["detector"] for v in rows} <= set(range(1, 7))
    for v in rows:
        assert 0.7 <= v["wavelength_min_um"] < v["wavelength_max_um"] <= 5.1
    assert len({v["access_url"] for v in rows}) == len(rows)
    for v in rows:  # every file (dr1 and qr3) must pass the cutout URL allow-list
        archive.validate_spherex_url(v["access_url"])


def _frame_url(frame) -> str:
    return parse_qs(urlsplit(frame["data_url"]).query)["url"][0]


def test_cutout_json_decodes_to_finite_aligned_grid(visits, first_frame, first_cutout):
    body = first_cutout
    data = cutouts.decode_float32(body["data_b64"], body["width"], body["height"])
    print(f"\n[cutout] {first_frame['obs_id']} D{first_frame['detector']} {first_frame['date']} "
          f"shape={data.shape} finite={np.isfinite(data).mean():.3f} wavelength={body['wavelength_um']}")
    assert data.shape == (59, 59)
    assert np.isfinite(data).mean() > 0.8
    assert body["bunit"] == "MJy/sr"
    assert abs(body["mjd"] - first_frame["mjd"]) < 0.01
    # The band at the target lies inside that detector's band from SIA.
    (visit,) = [v for v in visits["visits"] if v["access_url"] == _frame_url(first_frame)]
    assert visit["wavelength_min_um"] <= body["wavelength_um"] <= visit["wavelength_max_um"]


def test_barnard_lies_at_proper_motion_position_on_the_grid(first_cutout):
    body = first_cutout
    data = cutouts.decode_float32(body["data_b64"], body["width"], body["height"]).astype(float)
    star = SkyCoord(
        BARNARD_RA_DEG * u.deg, BARNARD_DEC_DEG * u.deg,
        pm_ra_cosdec=BARNARD_PMRA_COSDEC * u.mas / u.yr, pm_dec=BARNARD_PMDEC * u.mas / u.yr,
        distance=BARNARD_DISTANCE_PC * u.pc, obstime=Time(2016.0, format="jyear"),
    ).apply_space_motion(new_obstime=Time(body["mjd"], format="mjd"))
    wcs, _ = cutouts.grid_wcs(body["ra"], body["dec"], SIZE_DEG)
    px, py = (float(v) for v in wcs.world_to_pixel_values(star.ra.deg, star.dec.deg))
    # Moving north means moving UP the grid (smaller row index).
    assert py < (data.shape[0] - 1) / 2 - 10

    yy, xx = np.indices(data.shape)
    window = (np.hypot(xx - px, yy - py) <= 8) & np.isfinite(data)
    peak_y, peak_x = np.unravel_index(np.argmax(np.where(window, data, -np.inf)), data.shape)
    box = (slice(peak_y - 2, peak_y + 3), slice(peak_x - 2, peak_x + 3))
    w = np.clip(np.nan_to_num(data[box]), 0, None)
    cx, cy = (w * xx[box]).sum() / w.sum(), (w * yy[box]).sum() / w.sum()
    bkg = np.nanmedian(data)
    sigma = 1.4826 * np.nanmedian(np.abs(data - bkg))
    print(f"\n[star] predicted ({px:.2f},{py:.2f}) centroid ({cx:.2f},{cy:.2f}) "
          f"offset {np.hypot(cx - px, cy - py):.2f} px, peak {data[peak_y, peak_x]:.0f} bkg {bkg:.2f}")
    assert data[peak_y, peak_x] > bkg + 20 * sigma
    assert np.hypot(cx - px, cy - py) <= 1.5


def test_cutout_png_is_valid_image_of_the_same_grid(client, first_frame):
    resp = client.get(first_frame["image_url"])
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(resp.content))
    assert img.mode == "LA" and img.size == (59, 59)


def test_live_flag_planes_match_flag_bits(client, first_frame):
    """The masking constants match the MSKNnnnn cards of a real FLAGS header."""
    path = cutouts.cached_cutout(
        _frame_url(first_frame), *cutouts.cutout_request(BARNARD_RA_DEG, BARNARD_DEC_DEG, SIZE_DEG)
    )
    with fits.open(path) as hdul:
        header = hdul["FLAGS"].header
    planes = {header[k].strip(): int(k[4:]) for k in header if k.startswith("MSKN")}
    print(f"\n[flags] {planes}")
    for name in cutouts.UNUSABLE_FLAGS + cutouts.PHOTOMETRY_BAD_FLAGS:
        if name in planes:
            assert planes[name] == cutouts.FLAG_BITS[name], name
    assert set(cutouts.UNUSABLE_FLAGS) <= set(planes)
    assert all(planes[n] == cutouts.FLAG_BITS[n] for n in planes)


def test_cutout_outside_file_footprint_is_404(client, first_frame):
    resp = client.get("/api/cutout.json", params={"url": _frame_url(first_frame), "ra": 100.0, "dec": -40.0})
    print(f"\n[outside] {resp.status_code} {resp.json()['detail']}")
    if resp.status_code == 502 and "HTTP 5" in resp.json()["detail"]:
        pytest.skip("IRSA stayed busy (5xx) for every attempt")
    assert resp.status_code == 404, resp.text
