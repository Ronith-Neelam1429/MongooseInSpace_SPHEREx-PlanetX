"""Offline tests for the /api sky endpoints (IRSA replaced by fakes)."""
import io
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from planetx.spherex import archive, cutouts
from planetx.api.app import app
from planetx.api.schemas import Visit
from synthetic import make_mef

client = TestClient(app)

URL = "https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/x/6/level2_x_0001_1D6_spx.fits"
RA, DEC = 269.4485, 4.7394


def _visit(obs_id, mjd, detector, collection="spherex_dr1"):
    return Visit(
        obs_id=obs_id, collection=collection, detector=detector, mjd=mjd,
        date=archive.mjd_to_date(mjd), wavelength_min_um=1.6, wavelength_max_um=2.4,
        access_url=URL.replace("0001_1D6", f"{obs_id}D{detector}"),
    )


VISITS = [
    _visit("a", 60902.0, 3), _visit("a", 60902.0, 6), _visit("b", 60905.0, 3),
    _visit("c", 61085.0, 3), _visit("d", 61270.0, 6, "spherex_qr3"),
]


@pytest.fixture
def fake_visits(monkeypatch):
    calls = []

    def fake(ra, dec, radius_arcsec):
        calls.append((ra, dec, radius_arcsec))
        return list(VISITS)

    monkeypatch.setattr(archive, "query_visits", fake)
    return calls


def test_visits_groups_passes(fake_visits):
    resp = client.get("/api/visits", params={"ra": RA, "dec": DEC})
    assert resp.status_code == 200
    body = resp.json()
    assert fake_visits == [(RA, DEC, 10.0)]
    assert body["n_visits"] == 5 and body["radius_arcsec"] == 10.0
    assert [p["n_visits"] for p in body["passes"]] == [3, 1, 1]
    assert body["visits"][0]["access_url"] == VISITS[0].access_url


@pytest.mark.parametrize("params", [
    {"ra": RA, "dec": DEC, "radius_arcsec": 301},
    {"ra": RA, "dec": DEC, "radius_arcsec": 0},
    {"ra": 360, "dec": DEC},
    {"ra": RA, "dec": 91},
    {"ra": "nan", "dec": DEC},
    {"dec": DEC},
])
def test_visits_rejects_bad_input(fake_visits, params):
    assert client.get("/api/visits", params=params).status_code == 422
    assert fake_visits == []


def test_visits_upstream_failure_is_502(monkeypatch):
    def down(*args):
        raise archive.UpstreamError("SIA query spherex_dr1 failed after 3 attempt(s): HTTP 503", 503, True)

    monkeypatch.setattr(archive, "query_visits", down)
    resp = client.get("/api/visits", params={"ra": RA, "dec": DEC})
    assert resp.status_code == 502
    assert "HTTP 503" in resp.json()["detail"]


def test_frames_builds_encoded_cutout_urls_and_filters_detector(fake_visits):
    resp = client.get("/api/frames", params={"ra": RA, "dec": DEC, "size_deg": 0.2, "detector": 3})
    assert resp.status_code == 200
    body = resp.json()
    assert body["size_deg"] == 0.2
    assert [f["obs_id"] for f in body["frames"]] == ["a", "b", "c"]
    frame = body["frames"][0]
    assert frame["wavelength_um"] is None
    for key, path in (("image_url", "/api/cutout.png"), ("data_url", "/api/cutout.json")):
        parts = urlsplit(frame[key])
        assert parts.path == path and not parts.netloc
        q = parse_qs(parts.query)
        assert q == {"url": [VISITS[0].access_url], "ra": [str(RA)], "dec": [str(DEC)], "size_deg": ["0.2"]}


@pytest.mark.parametrize("params", [
    {"ra": RA, "dec": DEC, "size_deg": 0.6},
    {"ra": RA, "dec": DEC, "size_deg": 0.001},
    {"ra": RA, "dec": DEC, "detector": 7},
])
def test_frames_rejects_bad_input(fake_visits, params):
    assert client.get("/api/frames", params=params).status_code == 422


# --- cutouts: full path with a synthetic FITS served by a fake fetch ------------------


@pytest.fixture
def fake_fetch(cache_dir, monkeypatch):
    calls = []

    def fetch(url, ra, dec, size_deg):
        calls.append((url, ra, dec, size_deg))
        # Cutout centred where requested; the source sits 60" north of the TARGET.
        return make_mef(ra, dec, sources=[(RA, DEC + 60 / 3600, 100.0)])

    monkeypatch.setattr(archive, "fetch_cutout", fetch)
    return calls


def test_cutout_json_decodes_to_aligned_grid(fake_fetch):
    resp = client.get("/api/cutout.json", params={"url": URL, "ra": RA, "dec": DEC, "size_deg": 0.05})
    assert resp.status_code == 200
    body = resp.json()
    assert (body["width"], body["height"]) == (29, 29)
    assert body["bunit"] == "MJy/sr" and body["mjd"] == 60902.5
    assert body["pixel_scale_arcsec"] == 6.15 and body["wavelength_um"] > 1
    data = cutouts.decode_float32(body["data_b64"], body["width"], body["height"])
    # Source 60" north: ~9.8 rows above the centre row, centre column.
    assert np.unravel_index(np.nanargmax(data), data.shape) == (4, 14)
    # The cutout requested from IRSA is larger than the grid (covers any rotation)
    # and centred on the 0.001 deg snap grid.
    (url, ra, dec, size), = fake_fetch
    assert (url, ra, dec) == (URL, 269.448, 4.739)  # 269.4485 rounds half to even
    assert size == pytest.approx(0.05 * 1.5 + 4 * 6.15 / 3600, abs=1e-5)


def test_cutout_png_and_json_share_one_download(fake_fetch):
    params = {"url": URL, "ra": RA, "dec": DEC, "size_deg": 0.05}
    png = client.get("/api/cutout.png", params={**params, "vmin": 0, "vmax": 200})  # no clipping
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(png.content))
    assert img.mode == "LA" and img.size == (29, 29)
    assert np.unravel_index(np.argmax(np.asarray(img)[..., 0]), (29, 29)) == (4, 14)
    assert client.get("/api/cutout.json", params=params).status_code == 200
    assert len(fake_fetch) == 1


@pytest.mark.parametrize("url", [
    "https://evil.example/ibe/data/spherex/x.fits",
    "http://irsa.ipac.caltech.edu/ibe/data/spherex/x.fits",
    "https://irsa.ipac.caltech.edu/ibe/data/spherex/../../x.fits",
])
@pytest.mark.parametrize("path", ["/api/cutout.png", "/api/cutout.json"])
def test_cutout_rejects_disallowed_urls_with_400(fake_fetch, url, path):
    resp = client.get(path, params={"url": url, "ra": RA, "dec": DEC})
    assert resp.status_code == 400
    assert fake_fetch == []


def test_cutout_png_rejects_inverted_levels(fake_fetch):
    params = {"url": URL, "ra": RA, "dec": DEC, "vmin": 5, "vmax": 1}
    assert client.get("/api/cutout.png", params=params).status_code == 400
    assert client.get("/api/cutout.png", params={**params, "vmin": "nan"}).status_code == 422
    assert fake_fetch == []


@pytest.mark.parametrize(("status", "expected"), [
    (422, 404), (404, 404), (400, 404), (429, 502), (403, 502), (503, 502), (None, 502),
])
def test_cutout_upstream_errors(cache_dir, monkeypatch, status, expected):
    def fail(*args):
        raise archive.UpstreamError(f"cutout download failed: HTTP {status}", status, status == 503)

    monkeypatch.setattr(archive, "fetch_cutout", fail)
    resp = client.get("/api/cutout.json", params={"url": URL, "ra": RA, "dec": DEC})
    assert resp.status_code == expected
    assert f"HTTP {status}" in resp.json()["detail"]


def test_busy_irsa_slots_give_503_with_retry_after(cache_dir, monkeypatch):
    def busy(*args):
        raise archive.IrsaBusy("too many requests to IRSA in flight; try again shortly")

    monkeypatch.setattr(archive, "fetch_cutout", busy)
    monkeypatch.setattr(archive, "query_visits", busy)
    resp = client.get("/api/cutout.png", params={"url": URL, "ra": RA, "dec": DEC})
    assert resp.status_code == 503 and resp.headers["retry-after"] == "5"
    assert client.get("/api/visits", params={"ra": RA, "dec": DEC}).status_code == 503
