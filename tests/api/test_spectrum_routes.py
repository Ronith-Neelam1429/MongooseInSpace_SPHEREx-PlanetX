"""Offline tests for /api/spectrum: the background job on synthetic exposures (IRSA replaced by fakes)."""
import io
import math

import numpy as np
import pytest
from astropy.io import fits
from astropy.time import Time
from astropy.wcs import WCS
from fastapi.testclient import TestClient
from planetx.api import spectrum
from planetx.api.app import app
from planetx.api.schemas import Visit
from planetx.spherex import archive, cutouts
from scipy.special import erf
from synthetic import SHAPE, make_mef

client = TestClient(app)

RA, DEC = 150.0, 20.0
EPOCH = 2026.0
PM = (0.0, 12000.0)  # mas/yr: 12"/yr north, two SPHEREx pixels a year
TOTAL = 400.0  # MJy/sr summed over pixels
SIGMA_PX = 0.6


def _url(tag: str) -> str:
    return f"https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/x/{tag}_spx.fits"


def _visits(n_per_detector=20, detectors=(1, 2)) -> list[Visit]:
    out = []
    for det in detectors:
        for k in range(n_per_detector):
            mjd = Time(2025.6, format="jyear").mjd + 25.0 * k + 0.01 * det  # 2025.6 .. 2026.9
            out.append(Visit(
                obs_id=f"k{k}", collection="spherex_dr1", detector=det, mjd=mjd,
                date=archive.mjd_to_date(mjd), access_url=_url(f"k{k}D{det}"),
            ))
    return sorted(out, key=lambda v: v.mjd)


def _share(offset):
    s = SIGMA_PX * math.sqrt(2)
    return 0.5 * (erf((offset + 0.5) / s) - erf((offset - 0.5) / s))


def _cutout(visit: Visit, saturate: bool) -> bytes:
    """An exposure with the object where its proper motion puts it at the visit's date."""
    ra, dec = spectrum.sp.position_at(RA, DEC, *PM, EPOCH, visit.mjd)
    with fits.open(io.BytesIO(make_mef(RA, DEC))) as hdul:
        h = hdul["IMAGE"].header
        h["MJD-AVG"], h["DETECTOR"], h["PSF_FWHM"] = visit.mjd, visit.detector, SIGMA_PX * 2.3548 * 6.15
        x0, y0 = WCS(h).world_to_pixel_values(ra, dec)
        yy, xx = np.indices(SHAPE)
        hdul["IMAGE"].data = (3.0 + TOTAL * _share(xx - x0) * _share(yy - y0)).astype(np.float32)
        if saturate:
            flags = np.zeros(SHAPE, np.int32)
            flags[round(float(y0)), round(float(x0))] = 1 << cutouts.FLAG_BITS["BLOOM"]
            hdul["FLAGS"].data = flags
        buf = io.BytesIO()
        hdul.writeto(buf)
    return buf.getvalue()


class _Inline:
    def submit(self, fn, *args):
        fn(*args)


@pytest.fixture
def fakes(monkeypatch, cache_dir, tmp_path):
    state = {"visits": _visits(), "downloads": [], "saturate": False, "missing": set()}

    def query_visits(ra, dec, radius_arcsec):
        return list(state["visits"])

    def cached_cutout(url, ra, dec, size_deg):
        visit = next(v for v in state["visits"] if v.access_url == url)
        if url in state["missing"]:
            raise archive.UpstreamError("cutout download failed after 1 attempt(s): HTTP 422", 422)
        path = cutouts.cache_path(url, ra, dec, size_deg)
        if not path.exists():
            state["downloads"].append((url, size_deg))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_cutout(visit, state["saturate"]))
        return path

    monkeypatch.setattr(archive, "query_visits", query_visits)
    monkeypatch.setattr(cutouts, "cached_cutout", cached_cutout)
    monkeypatch.setattr(spectrum, "_runner", _Inline())
    spectrum._jobs.clear()
    yield state
    spectrum._jobs.clear()


def _get(**params):
    return client.get("/api/spectrum", params={"ra": RA, "dec": DEC, **params})


def _omega_mjy() -> float:
    with fits.open(io.BytesIO(make_mef(RA, DEC))) as hdul:
        from astropy.wcs.utils import proj_plane_pixel_area

        return proj_plane_pixel_area(WCS(hdul["IMAGE"].header).celestial) * (math.pi / 180) ** 2 * 1e9


def test_spectrum_of_a_moving_object(fakes):
    body = _get(pmra=PM[0], pmdec=PM[1], epoch=EPOCH).json()
    assert body["status"] == "done" and body["error"] is None
    n_new = 2 * spectrum.NEW_PER_DETECTOR
    assert (body["frames_done"], body["frames_total"]) == (n_new, n_new)
    assert len(fakes["downloads"]) == n_new
    assert {size for _, size in fakes["downloads"]} == {spectrum.CUTOUT_DEG}
    r = body["result"]
    assert r["counts"]["ok"] == n_new and r["saturated"] is False
    truth = TOTAL * _omega_mjy()
    fluxes = [p["flux_mjy"] for p in r["points"]]
    assert np.allclose(fluxes, truth, rtol=0.02)  # the aperture follows the object
    assert [p["wavelength_um"] for p in r["points"]] == sorted(p["wavelength_um"] for p in r["points"])
    assert r["bands"] and all(b["detector"] == 1 for b in r["bands"])  # synthetic wavelengths are ~1.03 um
    assert len(r["features"]) == 6 and r["suggestion"] == "unknown"  # one band: nothing to judge
    assert r["aperture_arcsec"] == pytest.approx(spectrum.sp.APERTURE_PX * 6.15)


def test_without_its_proper_motion_the_aperture_misses(fakes):
    r = _get().json()["result"]
    fluxes = np.array([p["flux_mjy"] for p in r["points"]])
    truth = TOTAL * _omega_mjy()
    assert (fluxes < 0.7 * truth).sum() >= 4  # 12"/yr leaves the aperture behind it, far from 2026.0


def test_saturated_object_is_reported_not_measured(fakes):
    fakes["saturate"] = True
    r = _get(pmra=PM[0], pmdec=PM[1], epoch=EPOCH).json()["result"]
    assert r["saturated"] is True and r["suggestion"] == "saturated"
    assert r["points"] == [] and r["counts"]["saturated"] == 2 * spectrum.NEW_PER_DETECTOR
    assert any("saturated" in note for note in r["notes"])


def test_cached_exposures_are_used_without_download(fakes):
    # Pretend /api/frames already fetched three exposures at the shared size.
    shared = cutouts.cutout_request(RA, DEC, spectrum.SHARED_SIZE_DEG)
    for v in fakes["visits"][:3]:
        path = cutouts.cache_path(v.access_url, *shared)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_cutout(v, saturate=False))
    body = _get(pmra=PM[0], pmdec=PM[1], epoch=EPOCH).json()
    # Cached ones count towards each detector's quota: only the rest is downloaded.
    assert body["frames_total"] == 2 * spectrum.NEW_PER_DETECTOR
    assert len(fakes["downloads"]) == 2 * spectrum.NEW_PER_DETECTOR - 3
    assert {url for url, _ in fakes["downloads"]}.isdisjoint(v.access_url for v in fakes["visits"][:3])
    assert body["result"]["counts"]["ok"] == body["frames_total"]


def test_asking_again_later_downloads_nothing_new(fakes):
    _get()
    n = len(fakes["downloads"])
    spectrum._jobs.clear()  # the finished run expired
    body = _get().json()
    assert len(fakes["downloads"]) == n and body["frames_total"] == n


def test_exposures_without_a_cutout_are_counted(fakes):
    fakes["missing"] = {v.access_url for v in fakes["visits"]}
    r = _get().json()["result"]
    assert r["counts"]["no_data"] == 2 * spectrum.NEW_PER_DETECTOR and r["points"] == []
    assert r["suggestion"] == "unknown"


def test_upstream_failure_fails_the_run(fakes, monkeypatch):
    def down(*args):
        raise archive.UpstreamError("SIA query failed: HTTP 503", 503, transient=True)

    monkeypatch.setattr(archive, "query_visits", down)
    body = _get().json()
    assert body["status"] == "failed" and "temporarily unavailable" in body["error"]


def test_no_visits_is_an_empty_spectrum(fakes):
    fakes["visits"] = []
    r = _get().json()["result"]
    assert r["points"] == [] and r["bands"] == [] and "no exposures" in r["notes"][0]


def test_same_object_shares_one_run(fakes):
    _get(pmra=PM[0], pmdec=PM[1], epoch=EPOCH)
    n = len(fakes["downloads"])
    assert _get(pmra=PM[0], pmdec=PM[1], epoch=EPOCH).json()["status"] == "done"
    assert len(fakes["downloads"]) == n and len(spectrum._jobs) == 1


@pytest.mark.parametrize("params", [
    {"ra": 360.0, "dec": 0.0}, {"ra": 10.0, "dec": 91.0},
    {"ra": 10.0, "dec": 0.0, "pmra": 1e6}, {"ra": 10.0, "dec": 0.0, "epoch": 1500},
    {"ra": 10.0, "dec": 0.0, "pmdec": "nan"},
])
def test_bad_parameters_are_422(fakes, params):
    assert client.get("/api/spectrum", params=params).status_code == 422


def test_busy_is_503(fakes, monkeypatch):
    class _Never:
        def submit(self, fn, *args):
            pass

    monkeypatch.setattr(spectrum, "_runner", _Never())
    for i in range(spectrum.MAX_WAITING_JOBS + 1):
        assert _get(ra=RA + i * 0.01).status_code == 200
    resp = _get(ra=RA + 0.5)
    assert resp.status_code == 503 and resp.headers["Retry-After"] == "30"


def test_select_spreads_new_exposures_over_time(cache_dir):
    visits = _visits(n_per_detector=40, detectors=(3,))
    picked = spectrum.select_visits(visits, [(RA, DEC, spectrum.CUTOUT_DEG)], per_detector=5)
    mjds = [v.mjd for v, _ in picked]
    assert len(picked) == 5 and mjds[0] == visits[0].mjd and mjds[-1] == visits[-1].mjd


def test_epoch_is_ignored_without_motion(fakes):
    _get(epoch=2016.0)
    _get(epoch=2020.0)
    assert len(spectrum._jobs) == 1
