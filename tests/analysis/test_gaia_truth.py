"""Gaia ground truth for engine tracks: matching, truth labels and the summary (synthetic, offline)."""
import io
import json
import math

import numpy as np
import pytest
from astropy.table import Table
from planetx.analysis import gaia_truth as gt
from planetx.engine import PassSource, TrackedObject

RA0, DEC0 = 150.0, 40.0
COS = math.cos(math.radians(DEC0))
GAIA_EPOCH = gt.MJD_J2000 + 16 * gt.DAYS_PER_YEAR
PASS_MJDS = (60900.0, 61080.0, 61260.0)  # three passes, half a year apart (2025-2026)
WIDE = 3600.0  # cone radius that makes chance matches negligible for a handful of stars


def star(sid, dx=0.0, dy=0.0, g=15.0, pm=(0.001, -0.002), pm_err=(0.0002, 0.0002), plx=0.001):
    """A Gaia star `dx`, `dy` arcsec east/north of the field centre at 2016.0."""
    return gt.GaiaStar(
        source_id=str(sid), ra=RA0 + dx / 3600 / COS, dec=DEC0 + dy / 3600, epoch_mjd=GAIA_EPOCH, g_mag=g,
        pm_ra=None if pm is None else pm[0], pm_dec=None if pm is None else pm[1],
        pm_ra_err=None if pm is None else pm_err[0], pm_dec_err=None if pm is None else pm_err[1], parallax=plx,
    )


def where(s: gt.GaiaStar, mjd: float, ex=0.0, ey=0.0):
    """(ra, dec) of star `s` on `mjd`, plus an offset of `ex`, `ey` arcsec."""
    years = (mjd - s.epoch_mjd) / gt.DAYS_PER_YEAR
    return (s.ra + ((s.pm_ra or 0) * years + ex) / 3600 / COS, s.dec + ((s.pm_dec or 0) * years + ey) / 3600)


def track(positions, pm=(0.0, 0.0), pm_err=0.05, mover=False, snr=50.0, sigma=0.5, mjds=PASS_MJDS, first_pass=0):
    sources = [
        PassSource(pass_index=first_pass + i, ra=ra, dec=dec, sigma_arcsec=sigma, mjd=mjd, n_det=6, n_cover=6,
                   snr=snr, flux_by_detector={})
        for i, ((ra, dec), mjd) in enumerate(zip(positions, mjds))
    ]
    return TrackedObject(
        sources=sources, link="mover" if mover else "static", ra=positions[0][0], dec=positions[0][1],
        ref_mjd=float(np.mean(mjds[: len(positions)])), pm_ra_cosdec=pm[0], pm_dec=pm[1], pm_err=pm_err,
        chi2_dof=1.0, is_mover=mover,
    )


def on(s, mjds=PASS_MJDS, jitter=0.3):
    """Pass positions on star `s`, each off by a little."""
    return [where(s, m, jitter * (-1) ** i, jitter) for i, m in enumerate(mjds)]


# --- parsing and propagation ----------------------------------------------------------

CSV = (
    "source_id,ra,dec,ref_epoch,pmra,pmdec,pmra_error,pmdec_error,parallax,phot_g_mean_mag\n"
    "1,269.4485,4.7394,2016.0,-801.551,10362.394,0.03,0.03,546.98,8.19\n"
    "2,10.0,20.0,2016.0,,,,,,19.1\n"
    ",10.0,20.0,2016.0,1,1,1,1,1,12\n"
)


def test_parse_gaia_csv_converts_mas_to_arcsec_and_keeps_missing_motion_as_none():
    stars = gt.parse_gaia_csv(CSV)
    assert [s.source_id for s in stars] == ["1", "2"]  # the row without an id is dropped
    barnard, faint = stars
    assert barnard.pm_ra == pytest.approx(-0.801551) and barnard.pm_dec == pytest.approx(10.362394)
    assert barnard.pm_err == pytest.approx(0.00003) and barnard.parallax == pytest.approx(0.54698)
    assert barnard.epoch_mjd == pytest.approx(57388.5)
    assert not faint.has_pm and faint.pm_total is None and faint.pm_err is None


def test_parse_gaia_csv_rejects_an_error_body():
    with pytest.raises(ValueError, match="no column"):
        gt.parse_gaia_csv('<?xml version="1.0"?><VOTABLE>error</VOTABLE>')


def test_positions_follow_proper_motion_from_2016():
    fast = star(1, pm=(0.0, 10.0))
    field = gt.GaiaField([fast], WIDE)
    ra, dec = field.positions(GAIA_EPOCH + 10 * gt.DAYS_PER_YEAR)
    assert (dec[0] - DEC0) * 3600 == pytest.approx(100.0)
    assert ra[0] == pytest.approx(RA0)


def test_invisible_stars_are_left_out():
    field = gt.GaiaField([star(1, g=15), star(2, g=20.5), star(3, g=None)], WIDE)
    assert [s.source_id for s in field.stars] == ["1"]


# --- matching and truth ---------------------------------------------------------------

def test_static_star_matches_in_every_pass_and_is_static_and_clean():
    s = star(1)
    field = gt.GaiaField([s], WIDE)
    matched, unmatched = gt.evaluate_field([track(on(s))], field, "f")
    assert unmatched == []
    (row,) = matched
    assert row["match"] == "full" and row["n_passes_matched"] == 3
    assert row["truth"] == "static" and row["clean"]
    assert row["gaia_source_id"] == "1" and row["g_mag"] == 15.0


def test_fast_mover_is_matched_at_each_epoch_not_at_its_2016_position():
    fast = star(1, pm=(-3.0, 4.0), g=14.0)
    # A static star sits where the fast one was in 2016: a match at the wrong epoch would find it.
    decoy = star(2, g=13.0)
    field = gt.GaiaField([fast, decoy], WIDE)
    (row,), _ = gt.evaluate_field([track(on(fast), pm=(-3.05, 3.9), pm_err=0.2, mover=True)], field, "f")
    assert row["gaia_source_id"] == "1"
    assert row["truth"] == "real_mover" and row["match"] == "full"
    summary = gt.summarize([row], 1)
    assert summary["confusion"] == {"tp": 1, "fp": 0, "fn": 0, "tn": 0}
    assert summary["recovered_fraction"] == 1.0


def test_brightest_star_wins_over_a_nearer_faint_one():
    bright, faint = star(1, dx=3.0, g=12.0), star(2, dx=0.5, g=18.5)
    field = gt.GaiaField([bright, faint], WIDE)
    positions = [(RA0, DEC0)] * 3
    (row,), _ = gt.evaluate_field([track(positions)], field, "f")
    assert row["gaia_source_id"] == "1"


def test_static_blend_reported_as_mover_is_a_false_mover_and_not_clean():
    a, b = star(1, g=14.0), star(2, dx=3.5, g=14.8)
    field = gt.GaiaField([a, b], WIDE)
    # The centroid walks between the two stars: the engine sees motion.
    positions = [where(a, PASS_MJDS[0]), where(a, PASS_MJDS[1], 1.5), where(a, PASS_MJDS[2], 3.0)]
    obj = track(positions, pm=(3.0, 0.0), pm_err=0.3, mover=True)
    (row,), _ = gt.evaluate_field([obj], field, "f")
    assert row["truth"] == "static" and row["n_blend"] == 1 and not row["clean"]
    assert "blend" in row["truth_reason"]
    assert gt.false_movers([row]) == [row]
    summary = gt.summarize([row], 1)
    assert summary["confusion"]["fp"] == 1 and summary["false_mover_rate"] == 1.0
    assert summary["n_residual"] == 0 and summary["residual_median"] is None


def test_significant_static_tracks_the_engine_held_back_are_near_false_movers():
    s = star(1)
    field = gt.GaiaField([s], WIDE)
    held = track(on(s), pm=(1.2, 0.0), pm_err=0.2)  # 6 sigma, not flagged (e.g. under 1 px of shift)
    quiet = track(on(s), pm=(0.1, 0.0), pm_err=0.2)
    flagged = track(on(s), pm=(1.2, 0.0), pm_err=0.2, mover=True)
    matched, _ = gt.evaluate_field([quiet, held, flagged], field, "f")
    assert gt.near_false_movers(matched) == [matched[1]]
    assert gt.false_movers(matched) == [matched[2]]


def test_much_fainter_neighbour_is_not_a_blend():
    a, b = star(1, g=12.0), star(2, dx=3.0, g=16.0)
    field = gt.GaiaField([a, b], WIDE)
    (row,), _ = gt.evaluate_field([track(on(a))], field, "f")
    assert row["n_blend"] == 0 and row["clean"]


def test_track_hopping_between_static_stars_is_static():
    stars = [star(i, dx=15.0 * i, g=14.0 + 0.1 * i) for i in range(3)]
    field = gt.GaiaField(stars, WIDE)
    positions = [where(s, m) for s, m in zip(stars, PASS_MJDS)]
    (row,), _ = gt.evaluate_field([track(positions, pm=(30.0, 0.0), pm_err=1.0, mover=True)], field, "f")
    assert row["match"] == "hop" and row["truth"] == "static"
    assert "different static" in row["truth_reason"]


def test_pass_without_any_gaia_star_makes_the_match_partial_and_ambiguous():
    s = star(1)
    field = gt.GaiaField([s], WIDE)
    positions = on(s)[:2] + [where(s, PASS_MJDS[2], 60.0)]
    (row,), _ = gt.evaluate_field([track(positions)], field, "f")
    assert row["match"] == "partial" and row["truth"] == "ambiguous"


def test_object_with_no_gaia_star_is_unmatched():
    field = gt.GaiaField([star(1, dx=300.0)], WIDE)
    matched, unmatched = gt.evaluate_field([track([(RA0, DEC0)] * 3, mover=True)], field, "f")
    assert matched == [] and len(unmatched) == 1
    assert unmatched[0]["truth"] is None and unmatched[0]["engine_is_mover"]


def test_chance_match_in_a_crowded_field_is_ambiguous():
    rng = np.random.default_rng(1)
    # 2,000 G=16 stars, density taken over a 300" cone: one per 140 arcsec^2, so a 4.5" circle holds ~0.45.
    crowd = [star(i, dx=float(x), dy=float(y), g=16.0) for i, (x, y) in enumerate(rng.uniform(-200, 200, (2000, 2)))]
    target = star("t", dx=1.0, g=16.0)
    field = gt.GaiaField([*crowd, target], 300.0)
    (row,), _ = gt.evaluate_field([track(on(target, jitter=0.1))], field, "f")
    assert row["chance_p"] > gt.CHANCE_MAX
    assert row["truth"] == "ambiguous" and "chance" in row["truth_reason"]
    assert not row["clean"]


def test_fast_mover_followed_through_a_crowd_is_not_a_chance_match():
    rng = np.random.default_rng(2)
    crowd = [star(i, dx=float(x), dy=float(y), g=16.0) for i, (x, y) in enumerate(rng.uniform(-200, 200, (2000, 2)))]
    fast = star("f", pm=(0.0, 10.0), g=16.0)  # 10"/yr: 5" between passes, out of its own match circle
    field = gt.GaiaField([*crowd, fast], 300.0)
    assert field.chance_probability(16.0, gt.MATCH_RADIUS_ARCSEC) > gt.CHANCE_MAX
    (row,), _ = gt.evaluate_field([track(on(fast, jitter=0.1), pm=(0.0, 10.0), pm_err=0.3, mover=True)], field, "f")
    assert row["gaia_source_id"] == "f" and row["truth"] == "real_mover"
    # p per pass (~0.36 here), once per pass the star was found in: 0.36^3
    assert row["chance_p"] == pytest.approx(field.chance_probability(16.0, gt.MATCH_RADIUS_ARCSEC) ** 3, rel=1e-3)


def test_star_found_in_more_passes_beats_a_brighter_one_found_once():
    steady, bright = star(1, g=17.0), star(2, dx=20.0, g=11.0)
    field = gt.GaiaField([steady, bright], WIDE)
    positions = on(steady)[:2] + [where(bright, PASS_MJDS[2])]
    (row,), _ = gt.evaluate_field([track(positions)], field, "f")
    assert row["gaia_source_id"] == "1" and row["n_passes_matched"] == 2


def test_red_mover_faint_in_g_next_to_a_bright_static_star_is_not_called_static():
    # A 1"/yr M dwarf 2 mag fainter in G than its static neighbour (brighter at SPHEREx's 1-5 um):
    # 3" from the static star at the SPHEREx epochs, so both are in every pass's circle.
    bright = star(1, g=15.0)
    dwarf = star(2, dx=3.0 - 1.0 * 10.0, g=17.0, pm=(1.0, 0.0))
    field = gt.GaiaField([bright, dwarf], WIDE)
    (row,), _ = gt.evaluate_field([track(on(dwarf), pm=(1.0, 0.0), pm_err=0.2, mover=True)], field, "f")
    assert row["gaia_source_id"] == "1"  # brighter in G wins the primary
    assert row["n_blend"] == 1 and row["truth"] == "ambiguous" and not row["clean"]
    assert gt.false_movers([row]) == []
    # The dwarf was seen by this track: it is not a missed mover.
    assert gt.untracked_movers(field, [row], PASS_MJDS[1], lambda ra, dec: True) == []


def test_blend_seen_only_in_one_pass_still_counts():
    # A 10"/yr star crosses the primary's pixel in the first pass only.
    primary = star(1, g=14.0)
    passer = star(2, dx=0.0, dy=2.0 - 10.0 * (PASS_MJDS[0] - GAIA_EPOCH) / gt.DAYS_PER_YEAR, g=14.5, pm=(0.0, 10.0))
    field = gt.GaiaField([primary, passer], WIDE)
    (row,), _ = gt.evaluate_field([track(on(primary, jitter=0.0))], field, "f")
    assert row["n_blend"] == 1 and row["truth"] == "ambiguous"


def test_hop_onto_a_moving_star_is_ambiguous():
    a, b = star(1, g=14.0), star(2, dx=30.0, g=14.0, pm=(0.0, 0.8))
    c = star(3, dx=60.0, g=14.0)
    field = gt.GaiaField([a, b, c], WIDE)
    positions = [where(s, m) for s, m in zip((a, b, c), PASS_MJDS)]
    (row,), _ = gt.evaluate_field([track(positions, pm=(40.0, 0.0), pm_err=1.0, mover=True)], field, "f")
    assert row["match"] == "hop" and row["truth"] == "ambiguous"


def test_two_sources_sharing_passes_on_one_star_are_not_a_split():
    s = star(1, g=12.0)
    field = gt.GaiaField([s], WIDE)
    near = track([where(s, m, 1.0, 0.0) for m in PASS_MJDS])
    other = track([where(s, m, -2.0, 0.0) for m in PASS_MJDS])
    matched, _ = gt.evaluate_field([near, other], field, "f")
    assert [r["n_tracks_same_star"] for r in matched] == [2, 2]
    assert not any(r["split_track"] for r in matched)
    assert gt.summarize(matched, 2)["n_stars_split_into_tracks"] == 0


def _ipac(table: Table) -> str:
    out = io.StringIO()
    table.write(out, format="ascii.ipac")
    return out.getvalue()


def test_gator_ipac_answer_becomes_the_same_stars():
    table = Table(
        {"source_id": [1633310130630331648, 2257806931042907008], "ra": [269.99594, 270.0288],
         "dec": [66.57051, 66.55512], "ref_epoch": [2016.0, 2016.0], "pmra": [-2.723, 0.0],
         "pmdec": [-9.607, 0.0], "pmra_error": [0.063, 0.0], "pmdec_error": [0.042, 0.0],
         "parallax": [0.6109, 0.0], "phot_g_mean_mag": [15.870794, 20.863712], "dist": [35.8, 45.9]},
        masked=True,
    )
    for col in ("pmra", "pmdec", "pmra_error", "pmdec_error", "parallax"):
        table[col].mask = [False, True]  # a 2-parameter solution: Gator writes "null"
    stars = gt.parse_gaia_csv(gt.gaia_ipac_to_csv(_ipac(table)))
    assert [s.source_id for s in stars] == ["1633310130630331648", "2257806931042907008"]
    assert stars[0].pm_dec == pytest.approx(-0.009607) and stars[0].pm_ra_err == pytest.approx(0.000063)
    assert not stars[1].has_pm and stars[1].parallax is None


def test_gator_answer_without_motion_columns_is_rejected():
    with pytest.raises(ValueError, match="no column"):
        gt.gaia_ipac_to_csv(_ipac(Table({"source_id": [1], "ra": [2.0], "dec": [3.0]})))


@pytest.mark.parametrize(
    ("pm", "pm_err", "spherex_err", "truth"),
    [
        ((0.03, 0.03), (0.001, 0.001), 0.1, "static"),
        ((0.03, 0.03), (0.02, 0.02), 0.1, "ambiguous"),  # Gaia too imprecise to call it static
        ((0.2, 0.0), (0.001, 0.001), 0.1, "ambiguous"),  # between 0.05 and 0.5"/yr
        ((0.8, 0.0), (0.001, 0.001), 0.1, "real_mover"),
        ((0.8, 0.0), (0.001, 0.001), 0.4, "ambiguous"),  # under 3x the SPHEREx error
        (None, None, 0.1, "ambiguous"),  # Gaia 2-parameter solution
    ],
)
def test_truth_thresholds(pm, pm_err, spherex_err, truth):
    s = star(1, pm=pm, pm_err=pm_err)
    field = gt.GaiaField([s], WIDE)
    (row,), _ = gt.evaluate_field([track(on(s), pm_err=spherex_err)], field, "f")
    assert row["truth"] == truth


def test_static_star_blended_with_a_mover_is_ambiguous():
    # b moves 1"/yr east: 8" west of a in 2016, about 2" east of it by the SPHEREx passes.
    a, b = star(1, g=14.0), star(2, dx=-8.0, g=14.5, pm=(1.0, 0.0))
    field = gt.GaiaField([a, b], WIDE)
    (row,), _ = gt.evaluate_field([track(on(a, jitter=0.0))], field, "f")
    assert row["truth"] == "ambiguous"


# --- summary --------------------------------------------------------------------------

def _row(truth, mover, dx=0.0, dy=0.0, err=0.1, snr=50.0, n_passes=3, clean=True):
    return {
        "truth": truth, "engine_is_mover": mover, "clean": clean, "snr_band": gt.snr_band(snr),
        "n_passes": n_passes, "spherex_pm_ra": dx, "spherex_pm_dec": dy, "spherex_pm_err": err,
        "gaia_pm_ra": 0.0, "gaia_pm_dec": 0.0, "gaia_pm_err": 0.0, "gaia_source_id": "x", "pm_significance": 1.0,
    }


def test_summary_counts_confusion_and_rates():
    rows = [_row("static", False)] * 8 + [_row("static", True)] * 2 + [_row("real_mover", True)] * 3 \
        + [_row("real_mover", False)] + [_row("ambiguous", True)]
    s = gt.summarize(rows, 20)
    assert s["n_objects"] == 20 and s["n_matched"] == 15
    assert s["confusion"] == {"tp": 3, "fp": 2, "fn": 1, "tn": 8}
    assert s["false_mover_rate"] == pytest.approx(0.2)
    assert s["false_mover_rate_upper95"] > 0.2
    assert s["recovered_fraction"] == pytest.approx(0.75)
    assert s["n_by_truth"] == {"real_mover": 4, "static": 10, "ambiguous": 1}


def test_zero_false_movers_in_n_rules_out_about_three_over_n():
    s = gt.summarize([_row("static", False)] * 1000, 1000)
    assert s["false_mover_rate"] == 0.0
    assert s["false_mover_rate_upper95"] == pytest.approx(3.0 / 1000, rel=0.01)


def test_pulls_are_near_one_when_errors_are_honest_and_large_when_too_small():
    rng = np.random.default_rng(7)
    honest = [_row("static", False, *rng.normal(0, 0.1, 2), err=0.1) for _ in range(2000)]
    s = gt.summarize(honest, len(honest))
    assert s["pull_std"] == pytest.approx(1.0, abs=0.05)
    assert s["pull_robust_std"] == pytest.approx(1.0, abs=0.07)
    assert s["residual_mad"] == pytest.approx(0.1, abs=0.007)
    # median |r| of a 2-D normal is sigma * sqrt(2 ln 2)
    assert s["residual_median"] == pytest.approx(0.1 * math.sqrt(2 * math.log(2)), abs=0.006)
    assert s["frame_pm_offset"]["n"] == 2000 and abs(s["frame_pm_offset"]["pm_ra"]) < 0.01
    overconfident = [_row("static", False, *rng.normal(0, 0.1, 2), err=0.05) for _ in range(2000)]
    assert gt.summarize(overconfident, 2000)["pull_std"] == pytest.approx(2.0, abs=0.1)


def test_residuals_use_only_clean_rows():
    rows = [_row("static", False, 0.1, 0.0), _row("static", True, 9.0, 9.0, clean=False)]
    s = gt.summarize(rows, 2)
    assert s["n_residual"] == 1 and s["residual_median"] == pytest.approx(0.1)


def test_summary_splits_by_snr_band_and_passes():
    rows = [_row("static", False, snr=10.0, n_passes=2), _row("static", True, snr=20.0, n_passes=3),
            _row("real_mover", True, snr=80.0, n_passes=5)]
    s = gt.summarize(rows, 3)
    assert set(s["by_snr"]) == {"<15", "15-40", ">=40"}
    assert s["by_snr"]["15-40"]["confusion"]["fp"] == 1
    assert set(s["by_passes"]) == {"2", "3", "4+"}
    assert s["by_passes"]["4+"]["recovered_fraction"] == 1.0
    json.dumps(s, allow_nan=False)  # every number is JSON-safe


def test_snr_band_edges_follow_the_engine():
    assert [gt.snr_band(x) for x in (14.9, 15.0, 39.9, 40.0)] == ["<15", "15-40", "15-40", ">=40"]


def test_untracked_movers_lists_fast_stars_no_track_matched_inside_the_field():
    tracked, missed, outside, faint = (
        star(1, pm=(1.0, 0.0)), star(2, dx=40.0, pm=(0.0, 2.0)), star(3, dx=900.0, pm=(0.0, 2.0)),
        star(4, dx=-40.0, pm=(0.0, 2.0), g=19.0),
    )
    field = gt.GaiaField([tracked, missed, outside, faint], WIDE)
    matched, _ = gt.evaluate_field([track(on(tracked), pm=(1.0, 0.0), mover=True)], field, "f")

    def inside(ra, dec):
        return abs((ra - RA0) * COS * 3600) < 300 and abs((dec - DEC0) * 3600) < 300

    out = gt.untracked_movers(field, matched, PASS_MJDS[1], inside)
    assert [m["gaia_source_id"] for m in out] == ["2"]


def test_fast_star_split_into_two_tracks_is_counted_once_as_split():
    # What the engine did with Epsilon Indi B (4.7"/yr): passes 1-2 and 3-4 linked as two static pairs.
    fast = star(1, pm=(4.0, -2.5), g=18.0)
    field = gt.GaiaField([fast, star(2, dx=200.0)], WIDE)
    mjds = (60800.0, 60950.0, 61150.0, 61300.0)
    first = track([where(fast, m) for m in mjds[:2]], pm=(2.8, -3.4), pm_err=0.4, mjds=mjds[:2])
    second = track([where(fast, m) for m in mjds[2:]], pm=(2.6, -3.2), pm_err=0.4, mjds=mjds[2:], first_pass=2)
    other = track(on(field.stars[1]))
    matched, _ = gt.evaluate_field([first, second, other], field, "f")
    assert [r["n_tracks_same_star"] for r in matched] == [2, 2, 1]
    assert [r["split_track"] for r in matched] == [True, True, False]
    s = gt.summarize(matched, 3)
    assert s["n_stars_split_into_tracks"] == 1 and s["n_real_movers_split_into_tracks"] == 1
    assert s["confusion"]["fn"] == 2  # each piece is a real mover the engine did not flag


def test_extra_fields_reach_the_rows_and_the_verdict_table():
    static, fast = star(1), star(2, dx=100.0, pm=(2.0, 0.0))
    field = gt.GaiaField([static, fast], WIDE)
    tracks = [track(on(static)), track(on(fast), pm=(2.0, 0.0), pm_err=0.2, mover=True),
              track([(RA0 + 0.5, DEC0)] * 3)]
    extra = [{"motion_verdict": "not_moving"}, {"motion_verdict": "moving"}, {"motion_verdict": "unsure"}]
    matched, unmatched = gt.evaluate_field(tracks, field, "f", extra)
    assert [r["motion_verdict"] for r in matched] == ["not_moving", "moving"]
    assert unmatched[0]["motion_verdict"] == "unsure"
    table = gt.summarize(matched, 3)["verdict_vs_truth"]
    assert table == {"moving": {"real_mover": 1, "static": 0, "ambiguous": 0},
                     "not_moving": {"real_mover": 0, "static": 1, "ambiguous": 0}}
    with pytest.raises(ValueError, match="entries"):
        gt.evaluate_field(tracks, field, "f", extra[:2])


def test_rows_are_json_safe_with_nan_chi2():
    s = star(1)
    obj = track(on(s))
    obj.chi2_dof = float("nan")
    (row,), _ = gt.evaluate_field([obj], gt.GaiaField([s], WIDE), "f")
    assert row["chi2_dof"] is None
    json.dumps(row, allow_nan=False)
