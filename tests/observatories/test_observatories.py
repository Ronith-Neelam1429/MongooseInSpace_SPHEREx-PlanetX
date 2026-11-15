"""Offline tests for the multi-observatory motion fusion (no network)."""
import math
from dataclasses import dataclass, field

import numpy as np
import pytest

from planetx.observatories import (
    DEFAULT_MULTI,
    SPHEREX_ONLY,
    ArchivalSource,
    ArchiveError,
    FusionParams,
    Observatory,
    Verdict,
    fuse_track,
    parse_observatories,
    refine_objects,
)
from planetx.observatories import catalogs
from planetx.observatories.fusion import _deproject, _project

RA0, DEC0 = 269.45, 4.74  # near Barnard's Star
MJD_2MASS = 51692.4
MJD_ALLWISE = 55400.0
MJD_CATWISE = 57170.0
SPHEREX_MJDS = (60920.0, 61120.0, 61290.0)  # three passes, about one year


@dataclass
class Epoch:
    ra: float
    dec: float
    mjd: float
    sigma_arcsec: float = 0.4


@dataclass
class Track:
    sources: list[Epoch]
    is_mover: bool = False
    link: str = "static"
    extra: dict = field(default_factory=dict)


def sky(east: float, north: float) -> tuple[float, float]:
    """Sky position `east`, `north` arcsec from the test field centre."""
    return _deproject(east, north, RA0, DEC0)


def position(start, pm, mjd, ref_mjd=SPHEREX_MJDS[0]):
    """Position of an object at `mjd` given its offset at ref_mjd and motion (arcsec/yr)."""
    dt = (mjd - ref_mjd) / 365.25
    return sky(start[0] + pm[0] * dt, start[1] + pm[1] * dt)


def spherex_track(start, pm, jitter=(), **kw) -> Track:
    jitter = list(jitter) + [(0.0, 0.0)] * len(SPHEREX_MJDS)
    epochs = []
    for mjd, (je, jn) in zip(SPHEREX_MJDS, jitter):
        dt = (mjd - SPHEREX_MJDS[0]) / 365.25
        ra, dec = sky(start[0] + pm[0] * dt + je, start[1] + pm[1] * dt + jn)
        epochs.append(Epoch(ra, dec, mjd))
    return Track(epochs, **kw)


def archival(obs, mjd, start, pm, sigma=0.08) -> ArchivalSource:
    ra, dec = position(start, pm, mjd)
    return ArchivalSource(obs, ra, dec, mjd, sigma)


def all_epochs(start, pm) -> dict:
    return {
        Observatory.TWOMASS: [archival(Observatory.TWOMASS, MJD_2MASS, start, pm)],
        Observatory.ALLWISE: [archival(Observatory.ALLWISE, MJD_ALLWISE, start, pm)],
        Observatory.CATWISE: [archival(Observatory.CATWISE, MJD_CATWISE, start, pm)],
    }


# --- the toggle ---------------------------------------------------------------


def test_toggle_presets_and_lists():
    assert parse_observatories("spherex") == SPHEREX_ONLY
    assert parse_observatories("multi") == DEFAULT_MULTI
    assert Observatory.NEOWISE in parse_observatories("all")
    assert parse_observatories(" 2MASS , catwise ") == {
        Observatory.SPHEREX, Observatory.TWOMASS, Observatory.CATWISE
    }
    assert parse_observatories([Observatory.ALLWISE]) == {Observatory.SPHEREX, Observatory.ALLWISE}


def test_toggle_rejects_unknown_and_empty():
    with pytest.raises(ValueError, match="hubble"):
        parse_observatories("spherex,hubble")
    with pytest.raises(ValueError):
        parse_observatories("")


def test_toggle_default_comes_from_settings():
    from planetx.config import settings

    assert parse_observatories(None) == parse_observatories(settings.observatories)


def test_spherex_only_never_fetches():
    def fetch(*args):
        raise AssertionError("SPHEREx-only mode must not touch the network")

    track = spherex_track((0, 0), (1.0, 0.0))
    result = refine_objects([track], observatories="spherex", fetch=fetch)
    assert result.observatories == SPHEREX_ONLY
    f = result.fused[0]
    assert f.verdict is Verdict.SPHEREX_ONLY
    assert f.is_mover is None  # the engine's own flag stands
    assert f.observatories == (Observatory.SPHEREX,)
    assert f.pm_ra_cosdec == pytest.approx(1.0, abs=1e-6)


# --- geometry -----------------------------------------------------------------


def test_projection_round_trip_at_high_declination():
    ra, dec = _deproject(250.0, -180.0, 270.0, 66.56)
    x, y = _project(ra, dec, 270.0, 66.56)
    assert (x[0], y[0]) == pytest.approx((250.0, -180.0), abs=1e-6)
    # 250" east at dec 66.56 is far more than 250" of RA.
    assert (ra - 270.0) * 3600 == pytest.approx(250.0 / math.cos(math.radians(66.56)), rel=5e-3)


# --- one track ----------------------------------------------------------------


def test_fast_mover_is_confirmed_and_error_shrinks():
    pm = (-0.80, 10.36)  # Barnard's Star, arcsec/yr
    # SPHEREx centroids off by a few tenths of an arcsec, as in real data.
    track = spherex_track((0, 0), pm, jitter=[(0.3, -0.2), (-0.25, 0.3), (0.2, 0.1)])
    alone = fuse_track(track.sources, {})
    both = fuse_track(track.sources, all_epochs((0, 0), pm))

    assert both.verdict is Verdict.CONFIRMED and both.is_mover is True
    assert set(both.observatories) == set(DEFAULT_MULTI)
    assert both.baseline_yr > 25
    assert both.pm_err < alone.pm_err / 10
    assert both.pm_ra_cosdec == pytest.approx(pm[0], abs=0.03)
    assert both.pm_dec == pytest.approx(pm[1], abs=0.03)
    # The joint fit is closer to the truth than SPHEREx alone.
    err = lambda f: math.hypot(f.pm_ra_cosdec - pm[0], f.pm_dec - pm[1])
    assert err(both) < err(alone)


def test_ladder_reaches_2mass_through_intermediate_epochs():
    """A poor SPHEREx fit misses the 2MASS position by ~40"; the WISE epochs lead to it."""
    pm = (-0.80, 10.36)
    params = FusionParams(max_gate_arcsec=30.0)
    # 0.8" centroid errors bias the SPHEREx-only motion by about 1.6"/yr.
    track = spherex_track((0, 0), pm, jitter=[(0.8, 0.0), (0.0, 0.0), (-0.8, 0.0)])
    full = fuse_track(track.sources, all_epochs((0, 0), pm), params)
    assert set(full.observatories) == set(DEFAULT_MULTI)
    assert (full.pm_ra_cosdec, full.pm_dec) == pytest.approx(pm, abs=0.05)

    only_2mass = {Observatory.TWOMASS: all_epochs((0, 0), pm)[Observatory.TWOMASS]}
    direct = fuse_track(track.sources, only_2mass, params)
    # Without the epochs in between, the 2MASS position is outside the search
    # limit, so nothing is matched and the reason is reported.
    assert direct.verdict is Verdict.UNCONFIRMED
    assert any("too uncertain" in n for n in direct.notes)


def test_false_motion_of_a_static_star_is_rejected():
    """Centroid jitter fakes ~1.5"/yr in SPHEREx; the archive shows the star never moved."""
    track = spherex_track((0, 0), (0, 0), jitter=[(-0.7, 0.0), (0.0, 0.0), (0.8, 0.0)], is_mover=True)
    alone = fuse_track(track.sources, {})
    assert alone.pm_total > 1.0

    both = fuse_track(track.sources, all_epochs((0, 0), (0, 0)))
    assert both.verdict is Verdict.STATIONARY and both.is_mover is False
    assert both.pm_total < 0.05


def test_slow_mover_invisible_to_spherex_is_found():
    pm = (0.25, -0.15)  # 0.29"/yr: under 0.05 px per SPHEREx pass
    track = spherex_track((0, 0), pm)
    alone = fuse_track(track.sources, {})
    assert alone.pm_significance < 5

    both = fuse_track(track.sources, all_epochs((0, 0), pm))
    assert both.verdict is Verdict.CONFIRMED
    assert both.pm_significance > 20
    assert (both.pm_ra_cosdec, both.pm_dec) == pytest.approx(pm, abs=0.02)


def test_two_static_sources_linked_as_a_mover_are_refuted():
    a, b = (0.0, 0.0), (18.0, 6.0)
    ra_a, dec_a = sky(*a)
    ra_b, dec_b = sky(*b)
    track = Track(
        [Epoch(ra_a, dec_a, SPHEREX_MJDS[0]), Epoch(ra_b, dec_b, SPHEREX_MJDS[1])],
        is_mover=True,
        link="mover",
    )
    archive = {
        Observatory.TWOMASS: [
            archival(Observatory.TWOMASS, MJD_2MASS, a, (0, 0)),
            archival(Observatory.TWOMASS, MJD_2MASS, b, (0, 0)),
        ],
        Observatory.CATWISE: [
            archival(Observatory.CATWISE, MJD_CATWISE, a, (0, 0)),
            archival(Observatory.CATWISE, MJD_CATWISE, b, (0, 0)),
        ],
    }
    f = fuse_track(track.sources, archive)
    assert f.verdict is Verdict.REFUTED and f.is_mover is False
    assert f.matched == []


def test_no_counterpart_leaves_spherex_fit_standing():
    track = spherex_track((0, 0), (3.0, 0.0), is_mover=True)
    far = {Observatory.TWOMASS: [archival(Observatory.TWOMASS, MJD_2MASS, (500, 500), (0, 0))]}
    f = fuse_track(track.sources, far)
    assert f.verdict is Verdict.UNCONFIRMED and f.is_mover is None
    assert f.pm_ra_cosdec == pytest.approx(3.0, abs=1e-6)


def test_background_star_is_not_mistaken_for_the_mover():
    """A stationary star sits right where the mover was in 2015; the mover itself was not catalogued."""
    pm = (4.0, 0.0)
    track = spherex_track((0, 0), pm, jitter=[(0.2, 0.2), (-0.2, 0.1), (0.1, -0.2)])
    spot = (pm[0] * (MJD_CATWISE - SPHEREX_MJDS[0]) / 365.25 + 0.5, 0.3)
    archive = {
        Observatory.CATWISE: [archival(Observatory.CATWISE, MJD_CATWISE, spot, (0, 0))],
        Observatory.TWOMASS: [archival(Observatory.TWOMASS, MJD_2MASS, spot, (0, 0))],
    }
    f = fuse_track(track.sources, archive)
    assert f.matched == []
    assert f.verdict is Verdict.UNCONFIRMED


def test_two_equally_good_counterparts_skip_the_epoch():
    track = spherex_track((0, 0), (0, 0))
    archive = {
        Observatory.CATWISE: [
            archival(Observatory.CATWISE, MJD_CATWISE, (1.5, 0.0), (0, 0)),
            archival(Observatory.CATWISE, MJD_CATWISE, (-1.5, 0.0), (0, 0)),
        ]
    }
    f = fuse_track(track.sources, archive)
    assert f.matched == []
    assert any("none preferred" in n for n in f.notes)


def test_single_epoch_track_is_an_error():
    with pytest.raises(ValueError, match="two SPHEREx epochs"):
        fuse_track([Epoch(RA0, DEC0, SPHEREX_MJDS[0])], {})
    same_date = [Epoch(RA0, DEC0, 60920.0), Epoch(RA0, DEC0, 60920.0)]
    with pytest.raises(ValueError, match="two or more dates"):
        fuse_track(same_date, {})


# --- a whole patch ------------------------------------------------------------


def field_of_static_stars(n, shift=(0.0, 0.0), seed=1):
    """n static stars: SPHEREx tracks (shifted by `shift`) and their archive entries."""
    rng = np.random.default_rng(seed)
    tracks, archive = [], {o: [] for o in (Observatory.TWOMASS, Observatory.ALLWISE, Observatory.CATWISE)}
    for east, north in rng.uniform(-150, 150, size=(n, 2)):
        tracks.append(spherex_track((east + shift[0], north + shift[1]), (0, 0)))
        for obs, sources in all_epochs((east, north), (0, 0)).items():
            archive[obs] += sources
    return tracks, archive


def test_patch_frame_offset_is_measured_and_removed():
    """A 0.9" pointing offset of the SPHEREx patch must not turn stars into movers."""
    tracks, archive = field_of_static_stars(12, shift=(0.9, -0.5))
    calls = []

    def fetch(obs, ra, dec, radius):
        calls.append((obs, radius))
        return archive[obs]

    result = refine_objects(tracks, observatories="multi", fetch=fetch)
    assert result.frame_offset_arcsec == pytest.approx((0.9, -0.5), abs=0.05)
    assert all(f.verdict is Verdict.STATIONARY for f in result.fused)
    assert max(f.pm_total for f in result.fused) < 0.01
    assert {c[0] for c in calls} == {Observatory.TWOMASS, Observatory.ALLWISE, Observatory.CATWISE}
    assert all(300 < radius <= catalogs.MAX_RADIUS_ARCSEC for _, radius in calls)
    assert result.archive_counts[Observatory.CATWISE] == 12


def test_patch_survives_an_unavailable_catalog():
    tracks, archive = field_of_static_stars(6)

    def fetch(obs, ra, dec, radius):
        if obs is Observatory.CATWISE:
            raise ArchiveError("IRSA returned HTTP 503 after 3 attempts")
        return archive[obs]

    result = refine_objects(tracks, observatories="multi", fetch=fetch)
    assert any("catwise unavailable" in n for n in result.notes)
    assert all(Observatory.CATWISE not in f.observatories for f in result.fused)
    assert all(f.verdict is Verdict.STATIONARY for f in result.fused)


def test_patch_with_every_catalog_down_falls_back_to_spherex():
    def fetch(*args):
        raise ArchiveError("network down")

    result = refine_objects([spherex_track((0, 0), (2.0, 0))], observatories="multi", fetch=fetch)
    assert result.fused[0].verdict is Verdict.SPHEREX_ONLY
    assert any("SPHEREx-only fits returned" in n for n in result.notes)


def test_neowise_is_queried_only_for_mover_candidates():
    pm = (5.0, 0.0)
    mover = spherex_track((40, 0), pm, is_mover=True, link="mover")
    static = spherex_track((-40, 0), (0, 0))
    neowise_mjd = 60400.0
    neowise = [ArchivalSource(Observatory.NEOWISE, *position((40, 0), pm, neowise_mjd), neowise_mjd, 0.1)]
    asked = []

    def fetch(obs, ra, dec, radius):
        if obs is Observatory.NEOWISE:
            asked.append(radius)
            return neowise
        return []

    result = refine_objects([mover, static], observatories="all", fetch=fetch)
    assert asked == [30.0]
    assert result.fused[0].verdict is Verdict.CONFIRMED
    assert result.fused[0].observatories == (Observatory.SPHEREX, Observatory.NEOWISE)
    assert result.fused[1].verdict is Verdict.UNCONFIRMED


def test_empty_patch():
    assert refine_objects([], observatories="multi").fused == []


# --- catalog parsing ----------------------------------------------------------

GATOR_2MASS = r"""\fixlen = T
\RowsRetrieved =                 3
|       designation|         ra|        dec| err_maj| err_min|    j_m|    k_m| ph_qual| cc_flg|        jdate|
|              char|     double|     double|  double|  double| double| double|    char|   char|       double|
|                  |        deg|        deg|  arcsec|  arcsec|    mag|    mag|        |       |            d|
|              null|       null|       null|    null|    null|   null|   null|    null|   null|         null|
   17574849+0441405  269.452044    4.694597     0.06     0.06   5.244   4.524      AAA     000  2451692.9284
   17574743+0441461  269.447647    4.696159     0.21     0.21  13.185  13.857      UUD     00c  2451692.9284
   17574999+0441000  269.458000    4.683000     0.10     0.10  12.000    null      Add     0pp  2451692.9284
   17570000+0440000  269.250000    4.666000     0.30     0.30  15.000  14.000      UUU     000  2451692.9284
   17570001+0440001  269.250100    4.666100     0.30     0.30  15.000  14.000      AAA     ppp  2451692.9284
"""


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code, self.text = status_code, text


def test_fetch_parses_2mass_and_filters_bad_rows(monkeypatch):
    seen = {}

    def get(url, params, timeout):
        seen.update(params)
        return FakeResponse(text=GATOR_2MASS)

    monkeypatch.setattr(catalogs.requests, "get", get)
    sources = catalogs.fetch_catalog(Observatory.TWOMASS, 269.4521, 4.6934, 40.0)

    assert seen["catalog"] == "fp_psc" and seen["spatial"] == "cone"
    assert seen["objstr"] == "269.4521000 +4.6934000" and seen["radius"] == "40.00"
    # Dropped: all upper limits, and detections that are all artifacts.
    assert [s.name for s in sources] == ["17574849+0441405", "17574743+0441461", "17574999+0441000"]
    star = sources[0]
    assert star.observatory is Observatory.TWOMASS
    assert star.mjd == pytest.approx(51692.4284)
    assert star.sigma_arcsec == pytest.approx(0.06)
    assert (star.mag, star.band) == (4.524, "Ks")
    assert sources[2].band == "J"  # null Ks falls back to J


def test_fetch_reports_gator_errors_and_bad_input(monkeypatch):
    monkeypatch.setattr(
        catalogs.requests, "get",
        lambda url, params, timeout: FakeResponse(text='[struct stat="ERROR", msg="The catalog is not on the list."]'),
    )
    with pytest.raises(ArchiveError, match="not on the list"):
        catalogs.fetch_catalog(Observatory.TWOMASS, 10.0, 10.0, 30.0)
    with pytest.raises(ValueError, match="radius"):
        catalogs.fetch_catalog(Observatory.NEOWISE, 10.0, 10.0, 300.0)
    with pytest.raises(ValueError, match="invalid position"):
        catalogs.fetch_catalog(Observatory.TWOMASS, 10.0, 95.0, 30.0)
    with pytest.raises(ValueError, match="no archival catalog"):
        catalogs.fetch_catalog(Observatory.SPHEREX, 10.0, 10.0, 30.0)


def test_fetch_retries_only_on_5xx(monkeypatch):
    monkeypatch.setattr(catalogs.time, "sleep", lambda s: None)
    responses = [FakeResponse(502), FakeResponse(503), FakeResponse(text=GATOR_2MASS)]
    calls = []

    def get(url, params, timeout):
        calls.append(1)
        return responses[len(calls) - 1]

    monkeypatch.setattr(catalogs.requests, "get", get)
    assert len(catalogs.fetch_catalog(Observatory.TWOMASS, 269.45, 4.69, 40.0)) == 3
    assert len(calls) == 3

    calls.clear()
    responses[:] = [FakeResponse(404)]
    with pytest.raises(ArchiveError, match="404"):
        catalogs.fetch_catalog(Observatory.TWOMASS, 269.45, 4.69, 40.0)
    assert len(calls) == 1


def test_neowise_exposures_are_binned_per_visit_and_object():
    rng = np.random.default_rng(3)
    ra, dec, mjd = [], [], []
    # Object A: 12 exposures in each of two visits half a year apart, moving 1" north.
    for visit_mjd, north in ((58000.0, 0.0), (58182.0, 1.0)):
        for k in range(12):
            ra.append(RA0 + rng.normal(0, 0.3) / 3600)
            dec.append(DEC0 + (north + rng.normal(0, 0.3)) / 3600)
            mjd.append(visit_mjd + k * 0.066)
    # Object B, 10" away, first visit only; plus two stray detections (too few to keep).
    for k in range(8):
        ra.append(RA0 + 10 / 3600)
        dec.append(DEC0)
        mjd.append(58000.0 + k * 0.066)
    ra += [RA0 + 30 / 3600, RA0 + 30 / 3600]
    dec += [DEC0, DEC0]
    mjd += [58000.1, 58000.2]
    n = len(ra)
    out = catalogs.bin_exposures(
        np.array(ra), np.array(dec), np.array(mjd), np.full(n, 0.3), np.full(n, 14.0)
    )
    assert len(out) == 3
    assert sorted(s.visit for s in out) == [0, 0, 1]
    late = next(s for s in out if s.visit == 1)
    assert (late.dec - DEC0) * 3600 == pytest.approx(1.0, abs=0.3)
    assert late.sigma_arcsec < 0.15  # 12 exposures of 0.3" each
    assert late.mjd == pytest.approx(58182.36, abs=0.1)


def test_ambiguous_epoch_is_retried_after_the_fit_tightens():
    """CatWISE has a decoy near the loose first prediction; AllWISE settles which is right."""
    pm = (-8.1, 0.7)  # WISE 0855-like
    track = spherex_track((0, 0), pm, jitter=[(0.3, -0.2), (-0.25, 0.3), (0.2, 0.1)])
    true_catwise = archival(Observatory.CATWISE, MJD_CATWISE, (0, 0), pm)
    decoy = archival(Observatory.CATWISE, MJD_CATWISE, (0, 5.0), pm)
    archive = {
        Observatory.CATWISE: [decoy, true_catwise],
        Observatory.ALLWISE: [archival(Observatory.ALLWISE, MJD_ALLWISE, (0, 0), pm)],
    }
    alone = fuse_track(track.sources, {Observatory.CATWISE: archive[Observatory.CATWISE]})
    assert alone.matched == [] and any("none preferred" in n for n in alone.notes)

    f = fuse_track(track.sources, archive)
    assert true_catwise in f.matched and decoy not in f.matched
    assert set(f.observatories) == {Observatory.SPHEREX, Observatory.ALLWISE, Observatory.CATWISE}
    assert f.notes == []


def test_catalog_fragments_of_one_object_are_not_ambiguous():
    """CatWISE lists a fast mover as several entries within ~1"; that is one counterpart."""
    pm = (-8.1, 0.7)
    track = spherex_track((0, 0), pm)
    pieces = [
        archival(Observatory.CATWISE, MJD_CATWISE, (0.5, 0.2), pm, sigma=0.11),
        archival(Observatory.CATWISE, MJD_CATWISE, (-0.3, -0.4), pm, sigma=0.12),
        archival(Observatory.CATWISE, MJD_CATWISE, (-0.9, 0.6), pm, sigma=0.38),
    ]
    f = fuse_track(track.sources, {Observatory.CATWISE: pieces})
    assert len(f.matched) == 1 and f.matched[0] in pieces
    assert f.verdict is Verdict.CONFIRMED and f.notes == []


def crowd(obs, mjd, n=400, half=300.0, mag=17.0, seed=5):
    """n faint unrelated sources scattered over a (2*half)" square."""
    rng = np.random.default_rng(seed)
    out = []
    for east, north in rng.uniform(-half, half, size=(n, 2)):
        ra, dec = sky(east, north)
        out.append(ArchivalSource(obs, ra, dec, mjd, 0.2, mag, "W1"))
    return out


def test_faint_bystander_in_a_crowded_field_does_not_make_a_mover():
    """Found on real data: a static star whose own counterpart is missing must not
    adopt a faint neighbour 10" away and become a 1"/yr 'mover'."""
    track = spherex_track((0, 0), (0, 0), jitter=[(0.3, 0.0), (0.0, 0.2), (-0.3, 0.0)])
    bystander = ArchivalSource(Observatory.CATWISE, *sky(9.0, 5.0), MJD_CATWISE, 0.2, 17.2, "W1")
    field = [s for s in crowd(Observatory.CATWISE, MJD_CATWISE)
             if math.hypot(*[v[0] for v in _project(s.ra, s.dec, RA0, DEC0)]) > 40]
    f = fuse_track(track.sources, {Observatory.CATWISE: field + [bystander]})
    assert f.matched == []
    assert f.verdict is Verdict.UNCONFIRMED and f.is_mover is None
    assert any("chance" in n for n in f.notes)


def test_bright_counterpart_in_a_crowded_field_is_trusted():
    """Barnard's Star in 2MASS: one very bright source, far from a loose prediction."""
    pm = (-0.80, 10.36)
    track = spherex_track((0, 0), pm, jitter=[(0.5, 0.0), (0.0, 0.0), (-0.5, 0.0)])
    star = ArchivalSource(
        Observatory.TWOMASS, *position((0, 0), pm, MJD_2MASS), MJD_2MASS, 0.06, 4.5, "Ks"
    )
    field = crowd(Observatory.TWOMASS, MJD_2MASS, mag=14.0)
    field = [s for s in field if math.hypot(*[v[0] for v in _project(s.ra, s.dec, star.ra, star.dec)]) > 70]
    f = fuse_track(track.sources, {Observatory.TWOMASS: field + [star]})
    assert f.matched == [star]
    assert f.verdict is Verdict.CONFIRMED
    assert (f.pm_ra_cosdec, f.pm_dec) == pytest.approx(pm, abs=0.03)


def test_blend_offset_below_one_pixel_is_not_motion():
    """Found on real data: 2MASS/WISE resolve a neighbour 3.5" from the SPHEREx
    blend centroid. That is a significant 0.13"/yr 'motion', but under one pixel."""
    track = spherex_track((0, 0), (0, 0))
    neighbour = (-2.3, 2.7)
    archive = {
        Observatory.TWOMASS: [archival(Observatory.TWOMASS, MJD_2MASS, neighbour, (0, 0), sigma=0.25)],
        Observatory.ALLWISE: [archival(Observatory.ALLWISE, MJD_ALLWISE, (-1.6, 1.9), (0, 0), sigma=0.2)],
    }
    f = fuse_track(track.sources, archive)
    assert f.verdict is Verdict.STATIONARY and f.is_mover is False


def test_single_archival_epoch_needs_spherex_to_see_the_motion():
    """One displaced catalog position cannot confirm a mover SPHEREx itself does not see."""
    static = spherex_track((0, 0), (0, 0))
    one = {Observatory.ALLWISE: [archival(Observatory.ALLWISE, MJD_ALLWISE, (6.0, 4.5), (0, 0))]}
    f = fuse_track(static.sources, one, FusionParams(min_gate_arcsec=8.0))
    assert f.verdict is Verdict.UNCONFIRMED and f.is_mover is None
    assert any("single archival position" in n for n in f.notes)

    pm = (4.0, 0.0)  # SPHEREx alone sees this at >5 sigma
    mover = spherex_track((0, 0), pm)
    f = fuse_track(mover.sources, {Observatory.ALLWISE: [archival(Observatory.ALLWISE, MJD_ALLWISE, (0, 0), pm)]})
    assert f.verdict is Verdict.CONFIRMED


def test_real_mover_passing_one_background_star_is_not_refuted():
    """An uncatalogued 5"/yr mover; a single 2MASS star lies near two of its three positions."""
    track = spherex_track((0, 0), (5.0, 0.0), is_mover=True, link="mover")
    star = {Observatory.TWOMASS: [archival(Observatory.TWOMASS, MJD_2MASS, (1.37, 0.0), (0, 0))]}
    f = fuse_track(track.sources, star)
    assert f.verdict is Verdict.UNCONFIRMED and f.is_mover is None


def test_static_star_keeps_its_own_counterparts_despite_noisy_spherex_motion():
    """SPHEREx jitter fakes ~1"/yr towards a neighbour; the star's own entries must win."""
    track = spherex_track((0, 0), (0, 0), jitter=[(-0.55, 0.0), (0.0, 0.0), (0.55, 0.0)])
    archive = all_epochs((0.2, -0.1), (0, 0))
    lure_east = -1.1 * (SPHEREX_MJDS[1] - MJD_CATWISE) / 365.25  # where the fake motion points
    archive[Observatory.CATWISE].append(
        ArchivalSource(Observatory.CATWISE, *sky(lure_east, 0.5), MJD_CATWISE, 0.2, 17.0, "W1")
    )
    f = fuse_track(track.sources, archive)
    assert f.verdict is Verdict.STATIONARY
    assert set(f.observatories) == set(DEFAULT_MULTI)
    assert f.pm_total < 0.03


def test_patch_frame_offset_ignores_movers():
    """A fast mover's large SPHEREx-minus-archive offset must not shift the patch."""
    tracks, archive = field_of_static_stars(6, shift=(0.5, 0.0))
    pm = (0.0, 9.0)
    for k in range(5):
        start = (-200 + 100 * k, 220)
        tracks.append(spherex_track(start, pm, is_mover=True))
        for obs, src in all_epochs(start, pm).items():
            archive[obs] += src
    result = refine_objects(tracks, observatories="multi", fetch=lambda o, ra, dec, r: archive[o])
    assert result.frame_offset_arcsec == pytest.approx((0.5, 0.0), abs=0.05)
    assert sum(f.verdict is Verdict.CONFIRMED for f in result.fused) == 5


def test_binning_across_ra_zero():
    ra = np.array([359.99995, 0.00005] * 6)
    out = catalogs.bin_exposures(
        ra, np.full(12, 66.56), 58000.0 + np.arange(12) * 0.066, np.full(12, 0.3), np.full(12, 14.0)
    )
    assert len(out) == 1
    assert min(out[0].ra, 360.0 - out[0].ra) < 1e-4


def test_truncated_catalog_response_is_an_error(monkeypatch):
    monkeypatch.setattr(catalogs.requests, "get", lambda url, params, timeout: FakeResponse(text=GATOR_2MASS))
    monkeypatch.setitem(
        catalogs._CATALOGS, Observatory.TWOMASS,
        catalogs._Catalog("fp_psc", "ra", catalogs._parse_2mass, max_rows=5),
    )
    with pytest.raises(ArchiveError, match="smaller radius"):
        catalogs.fetch_catalog(Observatory.TWOMASS, 269.45, 4.69, 40.0)


# --- weak SPHEREx tracks: the archive alone must establish the motion ---------
# Found live on Barnard's Star (54 frames, detectors 1-3): a track SPHEREx puts
# at 1.4 sigma was "confirmed" at 2.0"/yr by one AllWISE and one CatWISE entry,
# both unrelated stars in Barnard's crowded, halo-ridden field.

HALO_PM = (-1.3, -1.5)  # the false track's fused motion, arcsec/yr


def weak_track(jitter=()):
    """SPHEREx track with 1" centroid errors: its motion is only ~1.4 sigma."""
    track = spherex_track((0, 0), HALO_PM, jitter=jitter)
    for e in track.sources:
        e.sigma_arcsec = 1.0
    return track


def test_wise_alone_cannot_confirm_a_track_spherex_does_not_see_moving():
    track = weak_track()
    assert fuse_track(track.sources, {}).pm_significance < 3
    wise = {
        Observatory.ALLWISE: [archival(Observatory.ALLWISE, MJD_ALLWISE, (0, 0), HALO_PM)],
        Observatory.CATWISE: [archival(Observatory.CATWISE, MJD_CATWISE, (0, 0), HALO_PM)],
    }
    f = fuse_track(track.sources, wise)
    # AllWISE and CatWISE share exposures: one survey, not two independent epochs.
    assert len(f.matched) == 2
    assert f.verdict is Verdict.UNCONFIRMED and f.is_mover is None
    assert any("one survey family" in n for n in f.notes)

    # 2MASS, an independent survey, on the same line: now the archive alone shows it.
    wise[Observatory.TWOMASS] = [archival(Observatory.TWOMASS, MJD_2MASS, (0, 0), HALO_PM)]
    assert fuse_track(track.sources, wise).verdict is Verdict.CONFIRMED


def test_chance_link_plus_one_secure_epoch_does_not_confirm():
    """The first link is a faint CatWISE star that chance often puts that close;
    a 2MASS star on the line it implies is one secure epoch, not two."""
    # Centroid errors bias the SPHEREx-only motion, so the CatWISE source lies
    # ~8" from the SPHEREx-only prediction: a typical chance distance here.
    track = weak_track(jitter=[(0.4, 0.0), (0.0, 0.0), (-0.4, 0.0)])
    decoy = ArchivalSource(
        Observatory.CATWISE, *position((0, 0), HALO_PM, MJD_CATWISE), MJD_CATWISE, 0.2, 17.2, "W1"
    )
    field = [s for s in crowd(Observatory.CATWISE, MJD_CATWISE)
             if math.hypot(*[v[0] for v in _project(s.ra, s.dec, decoy.ra, decoy.dec)]) > 60]
    archive = {
        Observatory.CATWISE: field + [decoy],
        Observatory.TWOMASS: [archival(Observatory.TWOMASS, MJD_2MASS, (0, 0), HALO_PM)],
    }
    f = fuse_track(track.sources, archive)
    assert decoy in f.matched and len(f.matched) == 2
    assert f.verdict is Verdict.UNCONFIRMED and f.is_mover is None

    # A secure AllWISE epoch as well: two independent surveys agree.
    archive[Observatory.ALLWISE] = [archival(Observatory.ALLWISE, MJD_ALLWISE, (0, 0), HALO_PM)]
    assert fuse_track(track.sources, archive).verdict is Verdict.CONFIRMED


def barnard_with_faint_rival(n_epochs=3):
    """Barnard's Star in 2MASS and a faint star equally far from the loose
    SPHEREx-only prediction (as on the real field), plus faint field stars."""
    pm = (-0.80, 10.36)
    track = spherex_track((0, 0), pm, jitter=[(0.5, 0.0), (0.0, 0.0), (-0.5, 0.0)])
    track.sources = track.sources[:n_epochs]
    alone = fuse_track(track.sources, {})
    dt = (MJD_2MASS - alone.ref_mjd) / 365.25
    px, py = _project(alone.ra, alone.dec, RA0, DEC0)
    guess = (px[0] + alone.pm_ra_cosdec * dt, py[0] + alone.pm_dec * dt)
    tx, ty = _project(*position((0, 0), pm, MJD_2MASS), RA0, DEC0)
    star = ArchivalSource(Observatory.TWOMASS, *sky(tx[0], ty[0]), MJD_2MASS, 0.06, 4.5, "Ks")
    mirror = (2 * guess[0] - tx[0], 2 * guess[1] - ty[0])
    rival = ArchivalSource(Observatory.TWOMASS, *sky(*mirror), MJD_2MASS, 0.1, 14.5, "Ks")
    field = [s for s in crowd(Observatory.TWOMASS, MJD_2MASS, mag=14.0)
             if math.hypot(*[v[0] for v in _project(s.ra, s.dec, *sky(*guess))]) > 120]
    return track, pm, star, {Observatory.TWOMASS: field + [star, rival]}


def test_bright_star_wins_an_equal_distance_tie():
    """Barnard's Star had no archival match on the real field: 2MASS showed it
    and a faint star equally far from the prediction, so the epoch was skipped."""
    track, pm, star, archive = barnard_with_faint_rival()
    f = fuse_track(track.sources, archive)
    assert f.matched == [star]
    assert f.verdict is Verdict.CONFIRMED
    assert (f.pm_ra_cosdec, f.pm_dec) == pytest.approx(pm, abs=0.03)


def test_two_epoch_track_does_not_break_ties_by_brightness():
    """Two SPHEREx epochs always fit a line (the Eps Indi B field has such a
    link next to the real mover): it must not adopt a bright neighbour."""
    track, _, star, archive = barnard_with_faint_rival(n_epochs=2)
    f = fuse_track(track.sources, archive)
    assert star not in f.matched
    assert any("none preferred" in n for n in f.notes)
