"""Offline tests for planetx.survey.patch_frames: footprints, shared visit sample, requests, downloads, crops."""
import numpy as np
import pytest
from synthetic import make_mef

from planetx import pipeline
from planetx.api.schemas import Visit
from planetx.spherex import archive, cutouts
from planetx.survey import motion, patch_frames
from planetx.survey.patches import NEP, patch_center, patch_containing

NSIDE = 64
NEP_IPIX = patch_containing(NEP.ra.deg, NEP.dec.deg, NSIDE)
URL = "https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/x/{d}/level2_{n}D{d}_spx.fits"


def _visit(n: int, mjd: float, detector: int = 1) -> Visit:
    return Visit(obs_id=f"obs{n}", collection="spherex_dr1", detector=detector, mjd=mjd, date="2025-08-15",
                 access_url=URL.format(n=n, d=detector))


def _square(ra: float, dec: float, side: float, winding: int = 1) -> np.ndarray:
    """A footprint of about `side` deg around (ra, dec), as SIA lists them."""
    h = side / 2
    k = h / np.cos(np.radians(dec))
    corners = [(ra - k, dec - h), (ra + k, dec - h), (ra + k, dec + h), (ra - k, dec + h)]
    return np.array(corners[::winding])


def _requests(fields):
    return [cutouts.cutout_request(f.ra, f.dec, f.size_deg) for f in fields]


def _sep(ra1, dec1, ra2, dec2):
    u = patch_frames._unit(np.array([ra1, ra2]), np.array([dec1, dec2]))
    return float(np.degrees(np.arccos(np.clip(u[0] @ u[1], -1, 1))))


# -- footprints and probes -----------------------------------------------------

@pytest.mark.parametrize("winding", [1, -1])
def test_covers_is_a_point_in_footprint_test_whatever_the_winding(winding):
    square = _square(10.0, 20.0, 2.0, winding)
    inside = patch_frames.covers(square, np.array([10.0, 10.9, 9.2, 10.0, 10.0, 190.0]),
                                 np.array([20.0, 20.9, 19.1, 21.2, 18.7, -20.0]))
    assert inside.tolist() == [True, True, True, False, False, False]  # last one: the antipode


def test_covers_a_footprint_around_the_pole():
    cap = np.array([(0.0, 88.0), (90.0, 88.0), (180.0, 88.0), (270.0, 88.0)])
    assert patch_frames.covers(cap, np.array([0.0, 45.0, 0.0]), np.array([90.0, 89.0, 85.0])).tolist() == [
        True, True, False]


def test_probes_are_the_central_field_and_the_outermost_ones():
    fields = motion.patch_fields(NEP_IPIX, NSIDE)
    ra, dec = np.array([f.ra for f in fields]), np.array([f.dec for f in fields])
    probes = patch_frames.probe_fields(ra, dec)
    ra0, dec0 = patch_center(NEP_IPIX, NSIDE)
    dist = np.array([_sep(ra0, dec0, r, d) for r, d in zip(ra, dec, strict=True)])
    assert len(set(probes)) == 5
    assert dist[probes[0]] == pytest.approx(dist.min(), abs=0.05)
    # The others spread to the patch's extremities: both ends of its long axis, then its sides.
    assert set(np.argsort(dist)[-2:]) <= set(probes)
    assert min(_sep(ra[a], dec[a], ra[b], dec[b]) for a in probes for b in probes if a < b) > 0.45
    assert patch_frames.probe_fields(ra[:1], dec[:1]) == [0]


# -- the shared sample ---------------------------------------------------------

def test_share_takes_full_coverage_visits_spread_in_time():
    cover = np.ones((10, 4), dtype=bool)
    chosen = sorted(patch_frames.share_visits(cover, np.arange(10.0), per_field=6))
    assert len(chosen) == 6 and chosen[0] == 0 and chosen[-1] == 9
    assert max(np.diff(chosen)) <= 3


def test_share_fills_each_field_with_the_fewest_visits():
    # Visits 0-1 cover both fields, 2-5 only field 0, 6-9 only field 1.
    cover = np.zeros((10, 3), dtype=bool)
    cover[:2] = [True, True, False]
    cover[2:6, 0] = True
    cover[6:, 1] = True
    chosen = patch_frames.share_visits(cover, np.arange(10.0), per_field=3)
    assert set(chosen[:2]) == {0, 1} and len(chosen) == 4  # field 2 has none: nothing needed for it
    assert cover[chosen].sum(axis=0).tolist() == [3, 3, 0]


def _patch_visits(n_full: int, n_half: int):
    """Footprints around the NEP patch: per pass (3) and detector (1-3, plus 5 which is never used),
    `n_full` covering the whole patch and `n_half` covering only its east or west half."""
    ra0, dec0 = patch_center(NEP_IPIX, NSIDE)
    rows, n = [], 0
    for p, start in enumerate((60800.0, 61000.0, 61200.0)):
        for detector in (1, 2, 3, 5):
            for j in range(n_full + n_half):
                n += 1
                if j < n_full:
                    fp = _square(ra0, dec0, 3.5)
                else:  # its edge runs through the patch centre: covers one half
                    east = j % 2
                    fp = _square(ra0 + (1 if east else -1) * 1.75 / np.cos(np.radians(dec0)), dec0, 3.5)
                rows.append((_visit(n, start + j * 0.5, detector), fp))
    return rows


def test_plan_downloads_each_visit_once_for_every_field():
    fields = motion.patch_fields(NEP_IPIX, NSIDE)
    plan = patch_frames.plan_patch(_requests(fields), _patch_visits(n_full=8, n_half=0))
    # 6 per pass and detector suffice for all 64 fields: 3 passes x 3 detectors x 6 cutouts.
    assert len(plan.requests) == 54
    assert len({r.visit.access_url for r in plan.requests}) == 54
    assert {r.visit.detector for r in plan.requests} == {1, 2, 3}
    assert all(len(ids) == 54 for ids in plan.by_field)
    for f, ids in enumerate(plan.by_field):
        ra, dec, size = plan.fields[f]
        for k in ids:
            r = plan.requests[k]
            assert f in r.fields
            # The field's own cutout lies inside the patch cutout along any pixel axes.
            assert _sep(r.ra, r.dec, ra, dec) + size / 2 < r.size_deg / 2
            assert r.size_deg <= patch_frames.MAX_REQUEST_DEG


def test_plan_adds_partial_visits_only_where_fields_need_them():
    fields = motion.patch_fields(NEP_IPIX, NSIDE)
    plan = patch_frames.plan_patch(_requests(fields), _patch_visits(n_full=4, n_half=6))
    # Every field: 4 full + 3 of its half cover it, 6 are needed: 4 full + 2 per half = 8 per pass and detector.
    assert len(plan.requests) == 3 * 3 * 8
    assert all(len(ids) == 54 for ids in plan.by_field)
    full = [r for r in plan.requests if len(r.fields) == 64]
    half = [r for r in plan.requests if len(r.fields) < 64]
    assert len(full) == 36 and len(half) == 36
    assert max(r.size_deg for r in half) < min(r.size_deg for r in full)


def test_plan_splits_cutouts_that_would_be_too_large():
    fields = motion.patch_fields(10, 16)  # 1024 fields: far more than one 2.2 deg cutout holds
    ra0, dec0 = patch_center(10, 16)
    plan = patch_frames.plan_patch(_requests(fields), [(_visit(1, 60800.0), _square(ra0, dec0, 10.0))])
    assert len(plan.requests) > 1
    assert all(r.size_deg <= patch_frames.MAX_REQUEST_DEG for r in plan.requests)
    assert sorted(f for r in plan.requests for f in r.fields) == list(range(len(fields)))
    assert all(len(ids) == 1 and f in plan.requests[ids[0]].fields for f, ids in enumerate(plan.by_field))


def test_plan_without_visits_leaves_every_field_empty():
    fields = motion.patch_fields(NEP_IPIX, NSIDE)
    plan = patch_frames.plan_patch(_requests(fields), [])
    assert plan.requests == () and all(ids == () for ids in plan.by_field)


# -- downloads and crops ---------------------------------------------------------

RA, DEC = 120.0, 40.0


@pytest.fixture
def irsa(cache_dir, monkeypatch):
    """Fake cutout service: a synthetic cutout centred on each request, or the scripted error per URL."""
    monkeypatch.setattr(patch_frames, "DOWNLOAD_RETRY_WAITS_S", (0.0,))
    calls, errors = [], {}

    def fetch(url, ra, dec, size):
        calls.append((url, ra, dec, size))
        if errors.get(url):
            raise errors[url].pop(0)
        return make_mef(ra, dec, sources=[(RA, DEC, 50.0)])

    monkeypatch.setattr(archive, "fetch_cutout", fetch)
    return calls, errors


def _small_patch(n_visits: int = 3):
    """Three tiny fields around (RA, DEC) that fit one synthetic 61-px cutout, and their footprints."""
    k = 1 / np.cos(np.radians(DEC))
    fields = [cutouts.cutout_request(RA + dx * k, DEC + dy, 0.01) for dx, dy in ((0, 0), (0.01, 0), (0, 0.01))]
    rows = [(_visit(n, 60800.0 + n, detector=1 + n % 3), _square(RA, DEC, 3.5)) for n in range(n_visits)]
    return fields, rows


def test_load_downloads_one_cutout_per_visit_and_crops_every_field(irsa, monkeypatch):
    calls, errors = irsa
    fields, rows = _small_patch()
    monkeypatch.setattr(patch_frames, "list_footprints", lambda probes, should_stop: rows)
    errors[rows[2][0].access_url] = [archive.UpstreamError("HTTP 422", status=422)]  # no data there
    with patch_frames.load(fields, lambda: False) as patch:
        assert len(calls) == 3  # one per visit, not one per visit and field
        assert set(cutouts._pinned) == {p for p in patch.paths if p is not None} | {
            cutouts.cutout_path(r.visit.access_url, r.ra, r.dec, r.size_deg) for r in patch.plan.requests}
        for i in range(3):
            frames, no_data, failures = patch.field_frames(i)
            assert sorted(f.obs_id for f in frames) == ["obs0", "obs1"] and no_data == 1 and failures == []
            assert all(f.data.shape == (13, 13) for f in frames)  # 0.0218 deg at 6.15"/px
    assert not cutouts._pinned


def test_field_frames_match_a_direct_cutout_of_the_field(irsa, monkeypatch):
    """Cropped from the patch cutout or cut by "IRSA" from the same synthetic file: the same frame."""
    from astropy.io import fits
    from planetx.engine import frame_from_hdul

    fields, rows = _small_patch(1)
    monkeypatch.setattr(patch_frames, "list_footprints", lambda probes, should_stop: rows)
    with patch_frames.load(fields, lambda: False) as patch:
        (crop,), _, _ = patch.field_frames(1)
        r = patch.plan.requests[0]
        with fits.open(patch.paths[0]) as hdul:
            whole = frame_from_hdul(hdul)
            header = hdul["IMAGE"].header.copy()
    x, y = (float(c) for c in whole.wcs.world_to_pixel_values(RA, DEC))
    cx, cy = (float(c) for c in crop.wcs.world_to_pixel_values(RA, DEC))
    x0, _, y0, _ = cutouts.crop_box(cutouts.cutout_wcs(header), whole.data.shape, *fields[1])
    assert (cx + x0, cy + y0) == pytest.approx((x, y), abs=1e-6)
    np.testing.assert_array_equal(crop.data, whole.data[y0:y0 + crop.data.shape[0], x0:x0 + crop.data.shape[1]])
    assert crop.obs_id == r.visit.obs_id


def test_busy_and_503_are_waited_out(irsa, monkeypatch):
    calls, errors = irsa
    fields, rows = _small_patch(1)
    monkeypatch.setattr(patch_frames, "list_footprints", lambda probes, should_stop: rows)
    errors[rows[0][0].access_url] = [archive.IrsaBusy("busy")]
    with patch_frames.load(fields, lambda: False) as patch:
        assert patch.paths[0] is not None and len(calls) == 2


def test_too_many_failed_downloads_fail_the_patch(irsa, monkeypatch):
    _, errors = irsa
    fields, rows = _small_patch()
    monkeypatch.setattr(patch_frames, "list_footprints", lambda probes, should_stop: rows)
    for visit, _ in rows:
        errors[visit.access_url] = [archive.UpstreamError("HTTP 503", status=503, transient=True)] * 2
    with pytest.raises(archive.UpstreamError, match="3 of 3 patch cutouts failed") as err, \
            patch_frames.load(fields, lambda: False):
        pass
    assert err.value.transient
    assert not cutouts._pinned


def test_one_failed_download_is_left_out_of_the_fields(irsa, monkeypatch):
    _, errors = irsa
    fields, rows = _small_patch(6)
    monkeypatch.setattr(patch_frames, "list_footprints", lambda probes, should_stop: rows)
    errors[rows[0][0].access_url] = [archive.UpstreamError("HTTP 500", status=500, transient=True)] * 2
    with patch_frames.load(fields, lambda: False) as patch:
        frames, no_data, failures = patch.field_frames(0)
    assert len(frames) == 5 and no_data == 0 and len(failures) == 1 and "HTTP 500" in failures[0]


def test_stop_during_downloads_raises_stopped(irsa, monkeypatch):
    fields, rows = _small_patch()
    monkeypatch.setattr(patch_frames, "list_footprints", lambda probes, should_stop: rows)
    with pytest.raises(pipeline.Stopped), patch_frames.load(fields, lambda: True):
        pass


def test_a_cutout_evicted_by_another_process_is_fetched_again(irsa, monkeypatch):
    calls, _ = irsa
    fields, rows = _small_patch(1)
    monkeypatch.setattr(patch_frames, "list_footprints", lambda probes, should_stop: rows)
    with patch_frames.load(fields, lambda: False) as patch:
        patch.paths[0].unlink()
        frames, _, failures = patch.field_frames(0)
    assert len(frames) == 1 and failures == [] and len(calls) == 2


def test_field_run_feeds_the_field_pipeline(irsa, monkeypatch):
    fields, rows = _small_patch()
    monkeypatch.setattr(patch_frames, "list_footprints", lambda probes, should_stop: rows)
    seen = {}

    def analyse(frames, **kw):
        seen.update(kw, frames=frames)
        return "run"

    monkeypatch.setattr(pipeline, "analyse_frames", analyse)
    with patch_frames.load(fields, lambda: False) as patch:
        assert patch.field_run(2, lambda: False) == "run"
    assert len(seen["frames"]) == 3 and seen["n_visits"] == 3 and seen["no_data"] == 0


# -- the fast crop -------------------------------------------------------------

def _cutout_file(tmp_path, variance=True):
    """A synthetic patch cutout with stars, flag planes (bad, saturated, contaminated),
    noisy variance, SIP and the W wavelength table, written to disk."""
    import io

    from astropy.io import fits
    from astropy.wcs import WCS
    from synthetic import SHAPE, synthetic_header

    rng = np.random.default_rng(3)
    wcs = WCS(synthetic_header(RA, DEC))
    stars = [(*wcs.pixel_to_world_values(float(x), float(y)), 50.0) for x, y in rng.uniform(5, 55, (8, 2))]
    flags = np.zeros(SHAPE, np.int32)
    flags[rng.integers(0, 61, 40), rng.integers(0, 61, 40)] = 1 << 3  # HOT: bad
    flags[30, 30] |= 1 << 1  # OVERFLOW: saturated
    flags[10:14, 40:44] |= 1 << 5  # GHOST: contaminated
    hdul = fits.open(io.BytesIO(make_mef(RA, DEC, sources=stars, flags=flags)))
    hdul["FLAGS"].header.update({"MSKN0001": "OVERFLOW", "MSKM0001": 2, "MSKN0003": "HOT", "MSKM0003": 8,
                                 "MSKN0005": "GHOST", "MSKM0005": 32})
    # Higher SIP terms, so a CRPIX the distortion is not moved with shows.
    hdul["IMAGE"].header.update({"A_1_1": 2e-5, "B_2_0": -3e-5, "A_0_2": 1e-5})
    hdul["IMAGE"].data = hdul["IMAGE"].data + rng.normal(0, 0.1, SHAPE).astype(np.float32)
    hdul["VARIANCE"].data = rng.uniform(-0.01, 0.05, SHAPE).astype(np.float32)  # some <= 0: unknown
    if not variance:
        del hdul["VARIANCE"]
    path = tmp_path / "cutout.fits"
    hdul.writeto(path)
    return path, wcs


def _assert_same_frame(a, b):
    for name in ("data", "variance", "saturated", "contaminated"):
        x, y = getattr(a, name), getattr(b, name)
        assert x.dtype == y.dtype and x.shape == y.shape
        np.testing.assert_array_equal(x, y)
    assert (a.mjd, a.detector, a.obs_id, a.meta) == (b.mjd, b.detector, b.obs_id, b.meta)
    assert np.array_equal([a.psf_fwhm_arcsec, a.wavelength_um], [b.psf_fwhm_arcsec, b.wavelength_um], equal_nan=True)
    assert np.isfinite(a.wavelength_um)
    assert a.wcs.to_header_string(relax=True) == b.wcs.to_header_string(relax=True)
    assert a.wcs.pixel_shape == b.wcs.pixel_shape
    pix = np.array([[0.0, 0.0], [3.3, 7.9], [11.0, 2.5]])
    assert a.wcs.all_pix2world(pix, 0).tobytes() == b.wcs.all_pix2world(pix, 0).tobytes()
    assert a.pixel_scale_arcsec == b.pixel_scale_arcsec


def test_open_cutout_frames_equal_frame_from_hdul_of_the_crop(tmp_path):
    """The survey's fast crop is exactly what the engine builds from crop_cutout's HDUList."""
    from astropy.io import fits
    from planetx.engine import frame_from_hdul

    path, wcs = _cutout_file(tmp_path)
    cutout = patch_frames.OpenCutout(path)
    compared = 0
    try:
        with fits.open(path) as hdul:
            for x, y in [(30, 30), (10.4, 47.7), (1.0, 30.0), (59.6, 0.2), (45.0, 12.0)]:  # inside, then edges
                ra, dec = (float(c) for c in wcs.pixel_to_world_values(x, y))
                for size in (0.01, 0.03):
                    crop = cutouts.crop_cutout(hdul, ra, dec, size)
                    _assert_same_frame(cutout.frame(ra, dec, size, obs_id="o1"), frame_from_hdul(crop, obs_id="o1"))
                    compared += 1
            off = tuple(float(c) for c in wcs.pixel_to_world_values(-3.0, 30.0))
            assert cutout.frame(*off, 0.03) is None and cutouts.crop_cutout(hdul, *off, 0.03) is None
    finally:
        cutout.close()
    assert compared == 10


def test_open_cutout_without_variance_fails_only_where_it_has_data(tmp_path):
    path, wcs = _cutout_file(tmp_path, variance=False)
    cutout = patch_frames.OpenCutout(path)
    try:
        assert cutout.frame(*(float(c) for c in wcs.pixel_to_world_values(-3.0, 30.0)), 0.03) is None
        with pytest.raises(KeyError):
            cutout.frame(*(float(c) for c in wcs.pixel_to_world_values(30.0, 30.0)), 0.03)
    finally:
        cutout.close()


# -- fields in processes -------------------------------------------------------

def test_fields_in_processes_give_what_one_process_gives(irsa, monkeypatch):
    """The real pipeline (SPHEREx only: no network) on synthetic frames, in 2 spawned processes."""
    monkeypatch.setenv("PLANETX_OBSERVATORIES", "spherex")  # read afresh by the field processes
    monkeypatch.setattr(pipeline, "parse_observatories", lambda: pipeline.SPHEREX_ONLY)  # this process
    rng = np.random.default_rng(5)
    k = 1 / np.cos(np.radians(DEC))
    stars = [(RA + dx * k, DEC + dy, 80.0) for dx, dy in rng.uniform(-0.03, 0.03, (15, 2))]
    mjd = {}

    def fetch(url, ra, dec, size):  # a synthetic cutout taken at its visit's time
        import io

        from astropy.io import fits

        with fits.open(io.BytesIO(make_mef(ra, dec, sources=stars))) as hdul:
            hdul["IMAGE"].header["MJD-AVG"] = mjd[url]
            out = io.BytesIO()
            hdul.writeto(out)
        return out.getvalue()

    monkeypatch.setattr(archive, "fetch_cutout", fetch)
    requests = [cutouts.cutout_request(RA + dx * k, DEC + dy, 0.03) for dx, dy in ((0, 0), (0.02, 0), (0, 0.02))]
    rows = [(_visit(10 * p + n, start + n, detector=1 + n % 3), _square(RA, DEC, 3.5))
            for p, start in enumerate((60800.0, 61000.0, 61200.0)) for n in range(3)]
    mjd.update({v.access_url: v.mjd for v, _ in rows})
    monkeypatch.setattr(patch_frames, "list_footprints", lambda probes, should_stop: rows)
    fields = [motion.Field(ra, dec) for ra, dec, _ in requests]
    patch = motion.Patch(NSIDE, patch_containing(RA, DEC, NSIDE), 0, 0)
    with patch_frames.load(requests, lambda: False) as frames:
        one = motion._analyse_fields(patch, fields, frames, lambda: False, processes=1)
        two = motion._analyse_fields(patch, fields, frames, lambda: False, processes=2)
    assert one == two
    assert one[1] >= 5  # the synthetic stars, each owned by the field nearest to it


def test_a_stop_while_fields_run_in_processes_abandons_the_patch(irsa, monkeypatch):
    monkeypatch.setenv("PLANETX_OBSERVATORIES", "spherex")
    requests, rows = _small_patch(3)
    monkeypatch.setattr(patch_frames, "list_footprints", lambda probes, should_stop: rows)
    monkeypatch.setattr(motion, "STOP_POLL_S", 0.05)
    fields = [motion.Field(ra, dec) for ra, dec, _ in requests]
    patch = motion.Patch(NSIDE, patch_containing(RA, DEC, NSIDE), 0, 0)
    with patch_frames.load(requests, lambda: False) as frames, pytest.raises(motion.PatchAbandoned):
        motion._analyse_fields(patch, fields, frames, lambda: True, processes=2)
