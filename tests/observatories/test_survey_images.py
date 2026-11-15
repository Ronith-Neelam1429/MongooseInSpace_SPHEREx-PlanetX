"""Offline tests for older-survey images (planetx.observatories.images) and their route."""
import base64
import gzip
import io

import astropy.units as u
import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.io.votable import from_table
from astropy.table import Table
from astropy.time import Time
from astropy.wcs import WCS
from fastapi.testclient import TestClient
from PIL import Image
from planetx.api.app import app
from planetx.config import Settings
from planetx.observatories import images

client = TestClient(app)

RA, DEC = 269.4459, 4.7673
TWOMASS, WISE = images.SURVEYS
TWOMASS_URL = "https://irsa.ipac.caltech.edu/ibe/data/twomass/allsky/allsky/000528n/s101/image/ki1010220.fits.gz"
WISE_URL = "https://irsa.ipac.caltech.edu/ibe/data/wise/allwise/p3am_cdd/26/2688/2688p045_ac51/2688p045_ac51-w1-int-3.fits"
# A star planted this far (east, north) from the patch centre, arcsec.
STAR = (40.0, -25.0)


def _cutout(survey, header_cards: dict, rotation_deg: float = 0.0, side_px: int = 241) -> bytes:
    """A survey-like cutout: SIN projection (as IRSA's), optionally rotated, with one star at STAR."""
    scale = survey.pixel_arcsec / 3600
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---SIN", "DEC--SIN"]
    wcs.wcs.crval = [RA, DEC]
    wcs.wcs.crpix = [(side_px + 1) / 2, (side_px + 1) / 2]
    t = np.radians(rotation_deg)
    wcs.wcs.cd = np.array([[-scale * np.cos(t), scale * np.sin(t)], [scale * np.sin(t), scale * np.cos(t)]])
    star = SkyCoord(RA * u.deg, DEC * u.deg).spherical_offsets_by(STAR[0] * u.arcsec, STAR[1] * u.arcsec)
    x, y = wcs.world_to_pixel(star)
    rows, cols = np.mgrid[0:side_px, 0:side_px]
    data = 10.0 + 1000.0 * np.exp(-((cols - x) ** 2 + (rows - y) ** 2) / (2 * 1.5 ** 2))
    header = wcs.to_header()
    for k, v in header_cards.items():
        header[k] = v
    buf = io.BytesIO()
    fits.PrimaryHDU(data.astype(np.float32), header).writeto(buf)
    return buf.getvalue()


TWOMASS_CARDS = {"ORDATE": "000528", "UT": "10:17:06.44"}
WISE_CARDS = {"MIDOBS": "2010-09-14T14:05:49.712"}


@pytest.fixture
def irsa(monkeypatch, tmp_path):
    """IRSA replaced: find_image answers from `state['urls']`, downloads from `state['bodies']`."""
    state = {"urls": {"2mass": TWOMASS_URL, "wise": WISE_URL}, "bodies": {}, "calls": []}

    def get_once(url, params, limit):
        state["calls"].append((url, params))
        body = state["bodies"].get(url)
        if isinstance(body, Exception):
            raise body
        if body is None:
            raise images.Unusable("IRSA answered HTTP 404")
        return body

    def find_images(survey, ra, dec, side_arcsec):
        state["calls"].append(("SIA", survey.key))
        urls = state["urls"][survey.key]
        if isinstance(urls, Exception):
            raise urls
        return [images.validate_url(url, survey) for url in ([urls] if isinstance(urls, str) else urls)]

    state["bodies"][TWOMASS_URL] = _cutout(TWOMASS, TWOMASS_CARDS, rotation_deg=30)
    state["bodies"][WISE_URL] = gzip.compress(_cutout(WISE, WISE_CARDS))
    monkeypatch.setattr(images, "_get_once", get_once)
    monkeypatch.setattr(images, "find_images", find_images)
    monkeypatch.setattr(images, "settings", Settings(cache_dir=tmp_path))
    monkeypatch.setattr(images, "RETRY_WAITS_S", (0.0, 0.0))
    images._failures.clear()
    yield state
    images._failures.clear()


def _png(b64: str) -> np.ndarray:
    image = Image.open(io.BytesIO(base64.b64decode(b64)))
    return np.asarray(image)[..., 0].astype(float)


def test_images_are_north_up_east_left_with_the_star_where_the_offsets_say(irsa):
    resp = client.get("/api/motion/evidence/surveys", params={"ra": RA, "dec": DEC, "side_arcsec": 200})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unavailable"] == []
    assert [im["survey"] for im in body["images"]] == ["2mass", "wise"]  # oldest first
    assert resp.headers["cache-control"] == "public, max-age=86400"
    for im in body["images"]:
        grey = _png(im["image_png_b64"])
        n = im["patch_px"]
        assert grey.shape == (n, n) and n % 2 == 1
        centre = (n - 1) / 2
        # The documented convention: column = centre - east / pixel, row = centre - north / pixel.
        row, col = centre - STAR[1] / im["pixel_arcsec"], centre - STAR[0] / im["pixel_arcsec"]
        # The star's core is white (the stretch clips it): its middle is the star's position.
        white = np.argwhere(grey == grey.max())
        peak = white.mean(axis=0)
        assert abs(peak[0] - row) <= 1.0 and abs(peak[1] - col) <= 1.0, (im["survey"], peak, row, col)


def test_dates_come_from_the_headers(irsa):
    body = client.get("/api/motion/evidence/surveys", params={"ra": RA, "dec": DEC, "side_arcsec": 200}).json()
    by = {im["survey"]: im for im in body["images"]}
    assert by["2mass"]["date"] == "2000-05-28"
    assert by["2mass"]["mjd"] == pytest.approx(Time("2000-05-28T10:17:06.44").mjd, abs=1e-6)
    assert by["wise"]["date"] == "2010-09-14"
    hdr = fits.Header({"ORDATE": "971103", "UT": "01:00:00"})
    assert Time(images.observed_mjd(hdr, TWOMASS), format="mjd").isot.startswith("1997-11-03T01:00")
    with pytest.raises(images.Unusable, match="no date"):
        images.observed_mjd(fits.Header(), WISE)


def test_cutouts_are_cached_on_disk(irsa, tmp_path):
    params = {"ra": RA, "dec": DEC, "side_arcsec": 200}
    first = client.get("/api/motion/evidence/surveys", params=params).json()
    n = len(irsa["calls"])
    assert list(tmp_path.rglob("*.fits"))
    again = client.get("/api/motion/evidence/surveys", params=params).json()
    assert again == first
    assert len(irsa["calls"]) == n  # neither the image search nor the download again


def test_a_failing_survey_is_reported_not_raised_and_not_asked_again_at_once(irsa):
    irsa["urls"]["wise"] = images.Unusable("IRSA answered HTTP 502")
    params = {"ra": RA, "dec": DEC, "side_arcsec": 200}
    resp = client.get("/api/motion/evidence/surveys", params=params)
    assert resp.status_code == 200
    body = resp.json()
    assert [im["survey"] for im in body["images"]] == ["2mass"]
    assert body["unavailable"] == [{"survey": "wise", "label": "AllWISE W1", "reason": "IRSA answered HTTP 502"}]
    assert "cache-control" not in resp.headers  # a browser must ask again later
    searches = [c for c in irsa["calls"] if c == ("SIA", "wise")]
    client.get("/api/motion/evidence/surveys", params=params)
    assert [c for c in irsa["calls"] if c == ("SIA", "wise")] == searches  # remembered for a while


def test_an_unexpected_error_is_reported_as_missing(irsa, monkeypatch):
    monkeypatch.setattr(images, "observed_mjd", lambda header, survey: 1 / 0)
    body = client.get("/api/motion/evidence/surveys", params={"ra": RA, "dec": DEC, "side_arcsec": 200}).json()
    assert body["images"] == []
    assert {g["reason"] for g in body["unavailable"]} == {
        "2MASS Ks: the image could not be prepared", "AllWISE W1: the image could not be prepared",
    }


def test_a_damaged_file_is_not_cached(irsa, tmp_path):
    irsa["bodies"][TWOMASS_URL] = b"SIMPLE  = not really a FITS file"
    body = client.get("/api/motion/evidence/surveys", params={"ra": RA, "dec": DEC, "side_arcsec": 200}).json()
    assert body["unavailable"][0]["reason"] == "2MASS Ks: the archive sent a damaged file"
    assert len(list(tmp_path.rglob("*.fits"))) == 1  # the WISE cutout only


def _half(survey, cards, keep_north: bool) -> bytes:
    """A cutout whose north or south half has no data (beyond the edge of a survey image)."""
    with fits.open(io.BytesIO(_cutout(survey, cards))) as hdul:
        data = hdul[0].data
        rows = data.shape[0] // 2
        # Row 0 is south in a FITS image with north up.
        if keep_north:
            data = data[rows:]
            hdul[0].header["CRPIX2"] -= rows
        else:
            data = data[:rows]
        buf = io.BytesIO()
        fits.PrimaryHDU(data, hdul[0].header).writeto(buf)
    return buf.getvalue()


def test_a_patch_beyond_one_image_is_pieced_together_from_the_same_night(irsa):
    north, south = TWOMASS_URL, TWOMASS_URL.replace("ki1010220", "ki1010209")
    other_night = TWOMASS_URL.replace("000528n", "991103n")
    irsa["urls"]["2mass"] = [north, other_night, south]
    irsa["bodies"][north] = _half(TWOMASS, TWOMASS_CARDS, keep_north=True)
    irsa["bodies"][other_night] = _cutout(TWOMASS, {"ORDATE": "991103", "UT": "02:00:00"})
    irsa["bodies"][south] = _half(TWOMASS, {"ORDATE": "000528", "UT": "10:18:00"}, keep_north=False)
    image = images.patch_image(TWOMASS, RA, DEC, 200)
    assert np.isfinite(image.data).all()  # both halves, and nothing from the other night...
    assert image.mjd == pytest.approx(Time("2000-05-28T10:17:06.44").mjd)
    # ...which alone would have filled the gap, had it been taken that night.
    irsa["bodies"][south] = None  # a missing piece of the edge is no failure
    images._failures.clear()
    partial = images.patch_image(TWOMASS, RA + 0.001, DEC, 200)
    assert 0.3 < np.isfinite(partial.data).mean() < 0.7


def test_server_errors_are_retried(monkeypatch):
    answers = [images._ServerError("IRSA answered HTTP 502"), images._ServerError("IRSA answered HTTP 502"), b"ok"]
    monkeypatch.setattr(images, "RETRY_WAITS_S", (0.0, 0.0))

    def get_once(url, params, limit):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(images, "_get_once", get_once)
    assert images._get("https://irsa.ipac.caltech.edu/SIA", {}, 10) == b"ok"
    answers[:] = [images.Unusable("IRSA answered HTTP 404"), b"never"]
    with pytest.raises(images.Unusable, match="404"):  # a 4xx is not retried
        images._get("https://irsa.ipac.caltech.edu/SIA", {}, 10)


@pytest.mark.parametrize("url,survey", [
    (TWOMASS_URL, TWOMASS),
    (WISE_URL, WISE),
])
def test_allowed_urls(url, survey):
    assert images.validate_url(url, survey) == url


@pytest.mark.parametrize("url,survey", [
    (TWOMASS_URL, WISE),  # another survey's file
    (TWOMASS_URL.replace("irsa.ipac.caltech.edu", "evil.example"), TWOMASS),
    (TWOMASS_URL.replace("https", "http"), TWOMASS),
    (TWOMASS_URL + "?center=0,0", TWOMASS),
    (TWOMASS_URL.replace("/image/", "/../image/"), TWOMASS),
    (WISE_URL.replace("-w1-int-3", "-w1-unc-3"), WISE),  # not the science image
    ("https://irsa.ipac.caltech.edu:8443/ibe/data/wise/allwise/p3am_cdd/26/2688/x/x-w1-int-3.fits", WISE),
])
def test_refused_urls(url, survey):
    with pytest.raises(images.Unusable):
        images.validate_url(url, survey)


def test_gzip_is_bounded(monkeypatch):
    assert images._gunzip(b"SIMPLE") == b"SIMPLE"
    assert images._gunzip(gzip.compress(b"x" * 100)) == b"x" * 100
    monkeypatch.setattr(images, "MAX_FITS_BYTES", 1000)
    with pytest.raises(images.Unusable, match="more than expected"):
        images._gunzip(gzip.compress(b"\0" * 10_000))
    with pytest.raises(images.Unusable, match="damaged"):
        images._gunzip(b"\x1f\x8b" + b"junk")


def _box(ra: float, dec: float, w_deg: float, h_deg: float) -> str:
    """An SIA footprint: a w x h degree rectangle centred on (ra, dec)."""
    dra = w_deg / 2 / np.cos(np.radians(dec))
    corners = [(ra - dra, dec - h_deg / 2), (ra + dra, dec - h_deg / 2), (ra + dra, dec + h_deg / 2), (ra - dra, dec + h_deg / 2)]
    return "POLYGON ICRS " + " ".join(f"{a:.6f} {d:.6f}" for a, d in corners)


def test_coverage_of_the_patch_by_a_footprint():
    assert images.coverage(_box(RA, DEC, 0.2, 0.2), RA, DEC, 300) == 1.0
    # Footprint edge through the middle of the patch: about half (11 samples per side, the middle row on the edge).
    half = images.coverage(_box(RA, DEC + 0.1, 0.2, 0.2), RA, DEC, 300)
    assert 0.4 < half < 0.6
    assert images.coverage(_box(RA, DEC + 1, 0.2, 0.2), RA, DEC, 300) == 0.0
    assert images.coverage("CIRCLE 1 2 3", RA, DEC, 300) == 0.0
    assert images.coverage("POLYGON ICRS 1 x 3 4 5 6", RA, DEC, 300) == 0.0
    # Across RA 0.
    assert images.coverage(_box(0.0, 10.0, 0.2, 0.2), 359.99, 10.0, 60) == 1.0


def test_image_search_ranks_the_science_images_by_how_much_of_the_patch_they_cover(monkeypatch):
    # 2MASS images are 512" x 1024" (0.142 x 0.284 deg). The nearest one is 0.06 deg east, so a
    # 600" patch hangs over its west edge (~57% covered); the other, centred farther south, covers ~80%.
    w, h = 512 / 3600, 1024 / 3600
    near = TWOMASS_URL.replace("ki1010220", "ki1010221")
    east = 0.06 / np.cos(np.radians(DEC))
    rows = [
        ("science", "K", RA + east, DEC + 0.03, _box(RA + east, DEC + 0.03, w, h), near),
        ("science", "K", RA, DEC - 0.09, _box(RA, DEC - 0.09, w, h), TWOMASS_URL),
        ("science", "J", RA, DEC, _box(RA, DEC, w, h), TWOMASS_URL.replace("ki1010220", "ji1010220")),
        ("auxiliary", "K", RA, DEC, _box(RA, DEC, w, h), "https://irsa.ipac.caltech.edu/ibe/data/twomass/allsky/allsky/000528n/s101/pers.tbl"),
    ]
    table = Table(rows=rows, names=("dataproduct_subtype", "energy_bandpassname", "s_ra", "s_dec", "s_region", "access_url"))
    buf = io.BytesIO()
    from_table(table).to_xml(buf)
    monkeypatch.setattr(images, "_get", lambda url, params, limit: buf.getvalue())
    assert images.find_images(TWOMASS, RA, DEC, 600) == [TWOMASS_URL, near]
    # A small patch fits in both: the better centred one wins.
    assert images.find_images(TWOMASS, RA, DEC, 60) == [near, TWOMASS_URL]
    monkeypatch.setattr(images, "_get", lambda url, params, limit: buf.getvalue().replace(b">K<", b">H<"))
    with pytest.raises(images.Unusable, match="no image here"):
        images.find_images(TWOMASS, RA, DEC, 600)


@pytest.mark.parametrize("params", [
    {"ra": RA, "dec": DEC},  # side missing
    {"ra": RA, "dec": DEC, "side_arcsec": 10},
    {"ra": RA, "dec": DEC, "side_arcsec": 901},
    {"ra": 361, "dec": DEC, "side_arcsec": 100},
])
def test_rejects_bad_input(params, monkeypatch):
    monkeypatch.setattr(images, "patch_images", lambda *a: pytest.fail("must not fetch"))
    assert client.get("/api/motion/evidence/surveys", params=params).status_code == 422
