"""Live connection tests for SPHEREx data served by IRSA (SIA2 + MEF cutouts).

Findings verified against the live service (see assertions for the contract):
  * SIA2 endpoint: https://irsa.ipac.caltech.edu/SIA, queried through
    ``astroquery.ipac.irsa.Irsa.query_sia``.
  * Cutout ``size`` is in DEGREES: size=0.05 gives a 29x29 px image at
    ~6.15"/px (= 0.05 deg); size=0.1 gives ~59x58 px.
  * Cutout MEF extensions: PRIMARY, IMAGE, FLAGS, VARIANCE, ZODI, EPSF, WCS-WAVE.
  * Per-pixel wavelength lives in the WCS-WAVE binary table: a 9x9 grid of
    (wavelength [um], bandwidth [um]) samples at detector pixels X, Y.

IRSA intermittently answers 502/503 under load, so network calls are retried a
few times (bounded back-off, not a synchronisation sleep). Cone radii are kept
small: a 10 arcmin Barnard query against the Wide survey hung, and deep-field
queries near the NEP return tens of thousands of rows unless ``maxrec`` is set.
"""
import time

import astropy.units as u
import numpy as np
import pytest
import requests
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.time import Time
from astropy.wcs import WCS
from astroquery.ipac.irsa import Irsa
from pyvo.dal import DALServiceError

pytestmark = pytest.mark.network

CONE_RADIUS = 1 * u.arcmin
NEP = SkyCoord(270 * u.deg, 66.56 * u.deg, frame="icrs")
# Barnard's Star Gaia DR3 proper motion (mas/yr); total ~10393 matches conftest.
BARNARD_PMRA_COSDEC = -801.551  # Gaia DR3 4472832130942575872
BARNARD_PMDEC = 10362.394  # Gaia DR3
BARNARD_DISTANCE_PC = 1.83
ARCSEC_PER_PIX = 6.15  # SPHEREx pixel scale
CUTOUT_SIZE_DEG = 0.1
RETRIES = 5
RETRY_DELAY_S = 4


class _NotFits(Exception):
    """HTTP 200 whose body is not FITS (IRSA serves 'busy' HTML pages this way)."""


def _is_transient(exc):
    if isinstance(exc, (requests.ConnectionError, requests.Timeout, _NotFits)):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code >= 500
    if isinstance(exc, DALServiceError):
        return not exc.code or exc.code >= 500  # 0/None: no HTTP response at all
    return False


def _retry(fn, what):
    """Call fn(); retry only transient failures (5xx, connection, timeout, non-FITS 200).

    4xx and programming errors propagate immediately. Returns (result, seconds of
    the successful attempt).
    """
    for attempt in range(1, RETRIES + 1):
        start = time.monotonic()
        try:
            result = fn()
        except Exception as exc:
            if not _is_transient(exc):
                raise
            if attempt == RETRIES:
                pytest.fail(f"{what} failed after {RETRIES} attempts: {exc!r}")
            time.sleep(RETRY_DELAY_S * attempt)
            continue
        if attempt > 1:
            print(f"\n[retry] {what} needed {attempt} attempts")
        return result, time.monotonic() - start


def _sia(coord, collection, radius=CONE_RADIUS, **kw):
    return _retry(
        lambda: Irsa.query_sia(pos=(coord, radius), collection=collection, **kw),
        f"SIA query {collection}",
    )


def _distinct_dates(table):
    mjd = np.asarray(table["t_min"], dtype=float)
    assert np.all(np.isfinite(mjd)), "t_min contains non-finite values"
    return sorted({d[:10] for d in Time(mjd, format="mjd").iso})


def _clusters(dates, gap_days=30):
    """Group ISO dates into passes: a new cluster starts after a gap > gap_days."""
    mjd = Time(dates).mjd
    out = [[dates[0]]]
    for prev, cur, d in zip(mjd, mjd[1:], dates[1:]):
        if cur - prev > gap_days:
            out.append([])
        out[-1].append(d)
    return out


def _span_days(dates):
    return (Time(dates[-1]) - Time(dates[0])).to_value(u.day)


@pytest.fixture(scope="module")
def dr1_barnard(barnard):
    table, dt = _sia(barnard, "spherex_dr1")
    print(f"\n[spherex_dr1 @ Barnard] rows={len(table)} latency={dt:.1f}s")
    return table


@pytest.fixture(scope="module")
def qr3_barnard(barnard):
    table, dt = _sia(barnard, "spherex_qr3")
    print(f"\n[spherex_qr3 @ Barnard] rows={len(table)} latency={dt:.1f}s")
    return table


@pytest.fixture(scope="module")
def cutout(dr1_barnard, barnard, tmp_path_factory):
    """Download one small MEF cutout (earliest dr1 epoch) and return (path, row)."""
    ordered = dr1_barnard.copy()
    ordered.sort("t_min")
    row = ordered[0]
    url = str(row["access_url"])

    def fetch():
        resp = requests.get(
            url,
            params={"center": f"{barnard.ra.deg},{barnard.dec.deg}", "size": CUTOUT_SIZE_DEG},
            timeout=120,
        )
        resp.raise_for_status()  # 5xx retried, 4xx raised by _retry
        if resp.content[:6] != b"SIMPLE":
            raise _NotFits(
                f"200 but not FITS (starts {resp.content[:40]!r}, "
                f"content-type={resp.headers.get('content-type')})"
            )
        return resp

    resp, dt = _retry(fetch, "cutout download")
    path = tmp_path_factory.mktemp("spherex") / "cutout.fits"
    path.write_bytes(resp.content)
    print(f"\n[cutout] {len(resp.content) / 1e6:.1f} MB in {dt:.1f}s from {url}")
    return path, row


def _assert_sia_rows(table, collection, require_epochs=0):
    assert len(table) >= 1, f"{collection}: SIA query returned no rows"
    assert "access_url" in table.colnames, f"{collection}: no access_url column"
    urls = [str(x) for x in table["access_url"]]
    assert all(x.startswith("https://") and x.endswith(".fits") for x in urls), (
        f"{collection}: unexpected access_url shape, e.g. {urls[0]}"
    )
    if require_epochs:
        dates = _distinct_dates(table)
        print(f"[{collection}] rows={len(table)} distinct dates={len(dates)} "
              f"span={dates[0]}..{dates[-1]}")
        assert len(dates) >= require_epochs, (
            f"{collection}: need >={require_epochs} observation dates for a time-lapse, "
            f"got {len(dates)}: {dates}"
        )


def test_dr1_wide_barnard_has_rows_and_long_time_baseline(dr1_barnard):
    _assert_sia_rows(dr1_barnard, "spherex_dr1", require_epochs=2)
    assert set(dr1_barnard["obs_collection"]) == {"spherex_dr1"}
    assert set(dr1_barnard["dataproduct_type"]) == {"image"}
    dates = _distinct_dates(dr1_barnard)
    clusters = _clusters(dates)
    print(f"[spherex_dr1 clusters] {[(c[0], c[-1], len(c)) for c in clusters]}")
    # One survey pass spans several days; motion needs separate passes months apart.
    assert _span_days(dates) >= 150 and len(clusters) >= 2, (
        f"dr1 baseline too short for motion detection: span {_span_days(dates):.0f} d, "
        f"clusters {[(c[0], c[-1]) for c in clusters]}"
    )


def test_qr3_barnard_has_rows_and_epochs(qr3_barnard):
    _assert_sia_rows(qr3_barnard, "spherex_qr3", require_epochs=2)
    assert set(qr3_barnard["obs_collection"]) == {"spherex_qr3"}


def test_dr1_plus_qr3_extend_baseline_without_double_counting(dr1_barnard, qr3_barnard):
    """QR3 adds a later pass; the two collections share no frames at Barnard."""
    assert qr3_barnard["t_min"].max() > dr1_barnard["t_min"].max(), (
        "spherex_qr3 has no observations later than spherex_dr1 at Barnard's Star"
    )
    for col in ("obs_id", "obs_publisher_did"):
        shared = set(map(str, dr1_barnard[col])) & set(map(str, qr3_barnard[col]))
        print(f"[dr1 vs qr3] shared {col}: {len(shared)} "
              f"(dr1 {len(set(map(str, dr1_barnard[col])))}, "
              f"qr3 {len(set(map(str, qr3_barnard[col])))})")
        assert not shared, f"collections share {col} values, merging would double-count: {sorted(shared)[:3]}"
    # access_urls also differ, so frames are distinct files.
    assert not set(map(str, dr1_barnard["access_url"])) & set(map(str, qr3_barnard["access_url"]))
    all_dates = sorted(set(_distinct_dates(dr1_barnard)) | set(_distinct_dates(qr3_barnard)))
    clusters = _clusters(all_dates)
    print(f"[dr1+qr3 clusters] {[(c[0], c[-1], len(c)) for c in clusters]}")
    assert len(clusters) >= 3, f"expected >=3 passes (2 in DR1 + 1 in QR3), got {len(clusters)}"


@pytest.mark.parametrize("collection", ["spherex_dr1_deep", "spherex_qr3_deep"])
def test_deep_collections_near_north_ecliptic_pole(collection):
    table, dt = _sia(NEP, collection, maxrec=50)
    print(f"\n[{collection} @ NEP] rows={len(table)} (maxrec=50) latency={dt:.1f}s")
    _assert_sia_rows(table, collection)
    assert set(table["obs_collection"]) == {collection}


def test_dr1_cube_collection_returns_hips_cube_tile(barnard):
    """Cubes are HiPS-tiled (e.g. .../hips5_cube/.../Npix7945_cube.fits), not per-exposure."""
    table, dt = _sia(barnard, "spherex_dr1_cube")
    print(f"\n[spherex_dr1_cube @ Barnard] rows={len(table)} latency={dt:.1f}s")
    _assert_sia_rows(table, "spherex_dr1_cube")
    assert all("cube" in str(u_) for u_ in table["access_url"]), (
        f"cube collection returned non-cube URLs: {list(table['access_url'])[:3]}"
    )


def test_cutout_mef_structure_and_image_content(cutout):
    path, _ = cutout
    with fits.open(path) as hdul:
        names = [h.name for h in hdul]
        print(f"\n[cutout HDUs] {names}")
        for required in ("IMAGE", "FLAGS", "VARIANCE"):
            assert required in names, f"missing {required} HDU; found {names}"
        image, flags, variance = hdul["IMAGE"], hdul["FLAGS"], hdul["VARIANCE"]
        assert image.data.ndim == 2, f"IMAGE not 2D: shape {image.data.shape}"
        assert image.data.shape == flags.data.shape == variance.data.shape
        # Size is in degrees: 0.1 deg / 6.15"/px ~ 58.5 px per side.
        expected = CUTOUT_SIZE_DEG * 3600 / ARCSEC_PER_PIX
        for n in image.data.shape:
            assert abs(n - expected) <= 3, (
                f"cutout side {n}px, expected ~{expected:.0f}px if size is in degrees"
            )
        assert np.isfinite(image.data).any(), "IMAGE is all NaN"
        assert image.header["BUNIT"].replace(" ", "") == "MJy/sr"
        assert np.isfinite(variance.data).any() and np.nanmin(variance.data) >= 0
        assert np.issubdtype(flags.data.dtype, np.integer)


def test_cutout_wcs_is_celestial_and_covers_barnard(cutout, barnard):
    path, row = cutout
    with fits.open(path) as hdul:
        header, shape = hdul["IMAGE"].header, hdul["IMAGE"].data.shape
    wcs = WCS(header)
    assert wcs.has_celestial, "IMAGE WCS has no celestial axes"
    assert wcs.celestial.wcs.ctype[0].startswith("RA---")

    # Scale check: ~6.15 arcsec/pixel.
    scale = np.sqrt(abs(np.linalg.det(wcs.celestial.pixel_scale_matrix))) * 3600
    assert scale == pytest.approx(ARCSEC_PER_PIX, rel=0.05)

    def inside(coord):
        x, y = wcs.world_to_pixel(coord)
        return -0.5 <= x <= shape[1] - 0.5 and -0.5 <= y <= shape[0] - 0.5, (x, y)

    # Cutout is centred on the J2016 position: it must be near the centre.
    ok, (x, y) = inside(barnard)
    assert ok and abs(x - shape[1] / 2) < 2 and abs(y - shape[0] / 2) < 2, (
        f"J2016 position at pixel ({x:.1f},{y:.1f}) is not at the centre of {shape}"
    )

    # Barnard has moved ~10.4"/yr since 2016; at the exposure date it should have
    # shifted ~tens of pixels north yet still lie within the 0.1 deg footprint.
    obs_time = Time(header["MJD-AVG"], format="mjd")
    assert abs(obs_time.mjd - float(row["t_min"])) < 1.0, "header MJD-AVG disagrees with SIA t_min"
    star = SkyCoord(
        barnard.ra, barnard.dec,
        pm_ra_cosdec=BARNARD_PMRA_COSDEC * u.mas / u.yr,
        pm_dec=BARNARD_PMDEC * u.mas / u.yr,
        distance=BARNARD_DISTANCE_PC * u.pc,
        obstime=Time(2016.0, format="jyear"),
    ).apply_space_motion(new_obstime=obs_time)
    years = (obs_time - Time(2016.0, format="jyear")).to_value(u.yr)
    shift = barnard.separation(star).arcsec
    assert shift == pytest.approx(10.39 * years, rel=0.02), "proper-motion propagation sanity"
    ok, (x, y) = inside(star)
    print(f"\n[footprint] obs {obs_time.iso[:10]}: PM shift {shift:.0f}\" -> pixel ({x:.1f},{y:.1f}) of {shape}")
    assert ok, (
        f"Barnard's Star at obs date ({shift:.0f}\" from J2016 position) falls outside "
        f"the cutout at pixel ({x:.1f},{y:.1f}), shape {shape}"
    )


def _predicted_star(header, barnard):
    obs_time = Time(header["MJD-AVG"], format="mjd")
    return SkyCoord(
        barnard.ra, barnard.dec,
        pm_ra_cosdec=BARNARD_PMRA_COSDEC * u.mas / u.yr,
        pm_dec=BARNARD_PMDEC * u.mas / u.yr,
        distance=BARNARD_DISTANCE_PC * u.pc,
        obstime=Time(2016.0, format="jyear"),
    ).apply_space_motion(new_obstime=obs_time)


def test_barnard_star_is_visible_at_proper_motion_position(cutout, barnard):
    """Pixel content: the brightest source near the PM-predicted spot is Barnard's Star."""
    path, _ = cutout
    with fits.open(path) as hdul:
        data = np.asarray(hdul["IMAGE"].data, dtype=float)
        header = hdul["IMAGE"].header
    wcs = WCS(header)
    px, py = (float(v) for v in wcs.world_to_pixel(_predicted_star(header, barnard)))
    j2016 = tuple(float(v) for v in wcs.world_to_pixel(barnard))

    yy, xx = np.indices(data.shape)
    window = (np.hypot(xx - px, yy - py) <= 8) & np.isfinite(data)
    peak_y, peak_x = np.unravel_index(np.argmax(np.where(window, data, -np.inf)), data.shape)
    # Flux-weighted centroid over the 5x5 box around the peak (the core may be
    # saturated/flagged, so the peak pixel alone is only a seed).
    box = (slice(max(peak_y - 2, 0), peak_y + 3), slice(max(peak_x - 2, 0), peak_x + 3))
    w = np.clip(data[box], 0, None)
    cx, cy = (w * xx[box]).sum() / w.sum(), (w * yy[box]).sum() / w.sum()

    offset = np.hypot(cx - px, cy - py)
    from_j2016 = np.hypot(cx - j2016[0], cy - j2016[1])
    bkg = np.nanmedian(data)
    sigma = 1.4826 * np.nanmedian(np.abs(data - bkg))
    print(f"\n[star] predicted ({px:.2f},{py:.2f}) peak ({peak_x},{peak_y}) value {data[peak_y, peak_x]:.0f} "
          f"centroid ({cx:.2f},{cy:.2f}) offset {offset:.2f} px; {from_j2016:.1f} px from J2016 "
          f"position; background {bkg:.2f} MAD-sigma {sigma:.2f}")
    assert data[peak_y, peak_x] > bkg + 20 * sigma, "no significant source near the predicted position"
    assert offset <= 1.5, f"brightest source is {offset:.2f} px from the proper-motion prediction"
    assert from_j2016 > 10, (
        f"source is only {from_j2016:.1f} px from the J2016 position: proper motion not seen"
    )


def test_wavelength_at_barnard_pixel_via_wave_tab_wcs(cutout, barnard):
    """Evaluate (wavelength, bandwidth) at the star's pixel as the app will."""
    path, _ = cutout
    with fits.open(path) as hdul:
        header = hdul["IMAGE"].header
        wcs_w = WCS(header, fobj=hdul, key="W")
        table = hdul["WCS-WAVE"].data
        band = table["VALUES"][0][..., 0]
    assert list(wcs_w.wcs.ctype) == ["WAVE-TAB", "WAVE-TAB"], wcs_w.wcs.ctype
    px, py = (float(v) for v in WCS(header).world_to_pixel(_predicted_star(header, barnard)))
    wavelength, bandwidth = (float(v) for v in wcs_w.all_pix2world(px, py, 0))
    print(f"\n[wavelength @ star pixel ({px:.1f},{py:.1f})] {wavelength:.4f} um, "
          f"bandwidth {bandwidth:.4f} um; detector grid {band.min():.3f}-{band.max():.3f} um")
    assert np.isfinite([wavelength, bandwidth]).all()
    assert band.min() - 0.05 <= wavelength <= band.max() + 0.05, (
        f"{wavelength} um outside detector band {band.min()}-{band.max()}"
    )
    assert 0 < bandwidth < 0.2


def test_cutout_has_per_pixel_wavelength_table(cutout):
    path, _ = cutout
    with fits.open(path) as hdul:
        assert "WCS-WAVE" in hdul, f"no WCS-WAVE HDU; found {[h.name for h in hdul]}"
        table = hdul["WCS-WAVE"].data
        assert {"X", "Y", "VALUES"} <= set(table.names), f"WCS-WAVE columns: {table.names}"
        x, y, values = table["X"][0], table["Y"][0], table["VALUES"][0]
        # 9x9 grid over the 2040-px detector; each node is (wavelength_um, bandwidth_um).
        assert values.shape == (len(y), len(x), 2), f"unexpected VALUES shape {values.shape}"
        wavelength, bandwidth = values[..., 0], values[..., 1]
        assert np.all(np.isfinite(values))
        assert wavelength.min() >= 0.7 and wavelength.max() <= 5.3, (
            f"wavelengths outside SPHEREx 0.75-5.0 um band: "
            f"{wavelength.min():.2f}-{wavelength.max():.2f}"
        )
        assert np.all(bandwidth > 0)
        # Linear variable filter: wavelength varies smoothly across the detector.
        assert wavelength.max() - wavelength.min() > 0.05
        print(f"\n[WCS-WAVE] {values.shape} grid, wavelength {wavelength.min():.3f}-"
              f"{wavelength.max():.3f} um, bandwidth ~{bandwidth.mean():.3f} um, "
              f"detector={hdul['IMAGE'].header['DETECTOR']}")


def test_cutout_rejects_unknown_file_without_returning_fits(barnard):
    """A bogus path must not silently yield a FITS-looking payload."""
    bad = ("https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/"
           "does_not_exist/nope.fits")
    def fetch():
        resp = requests.get(
            bad, params={"center": f"{barnard.ra.deg},{barnard.dec.deg}", "size": 0.05}, timeout=120
        )
        if resp.status_code >= 500:  # IRSA answers 503 "Server busy" at random: retry
            resp.raise_for_status()
        return resp

    resp, _ = _retry(fetch, "bogus cutout request")
    assert 400 <= resp.status_code < 500, f"expected a 4xx client error, got {resp.status_code}"
    assert not resp.content.startswith(b"SIMPLE")
