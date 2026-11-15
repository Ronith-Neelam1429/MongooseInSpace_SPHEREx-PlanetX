"""The honest motion verdict (planetx.classify.reliability): one test per rule, plus the real Barnard field."""
from dataclasses import dataclass, replace

import pytest
from test_classify_check import (
    BARNARD_FIELD_GAIA,
    BARNARD_FIELD_SIMBAD,
    PIXEL,
    Catalogues,
    barnard_field,
    fused,
    tracked,
)

from planetx.classify.check import Known
from planetx.classify.reliability import (
    CAUSES,
    MotionVerdict,
    apparent_motion,
    bright_star_distances,
    field_verdicts,
    object_verdict,
    refine,
    split_partners,
    stored_verdict,
    stored_verdict_sql,
    survey_names,
)
from planetx.classify.rules import Match
from planetx.engine import TrackedObject
from planetx.observatories import FusedMotion, Observatory, Verdict

YEAR = 365.25


@dataclass(frozen=True)
class Found:
    tracked: TrackedObject
    fused: FusedMotion
    known: Known


def mover(*, n_passes=3, pm=(3.0, 4.0), pm_err=0.5, is_mover=True, step_yr=0.5):
    """A clean track moving `pm` arcsec/yr, `n_passes` passes `step_yr` apart."""
    passes = []
    for k in range(n_passes):
        t = k * step_yr
        passes.append((150.0 + pm[0] * t / 3600.0, 20.0 + pm[1] * t / 3600.0, 60900.0 + t * YEAR))
    return tracked(passes, pm=pm, pm_err=pm_err, is_mover=is_mover)


def found(obj, verdict=Verdict.UNCONFIRMED, *, kind=None, classification: str | None = "unknown_candidate",
          **known):
    kind = kind or ("mover" if obj.is_mover else "static")
    return Found(obj, fused(obj, verdict), Known(kind=kind, classification=classification, **known))


def gaia_match(pm=(0.002, -0.004)):
    return Match(catalog="gaia", name="Gaia DR3 42", kind="Star", sep_arcsec=0.4, pm_ra_cosdec=pm[0], pm_dec=pm[1])


def saturate(obj):
    return replace(obj, sources=[
        replace(s, detections=[replace(d, saturated=True, sat_area=80) for d in s.detections]) for s in obj.sources
    ])


# --- no claimed motion ------------------------------------------------------------

def test_a_track_within_the_noise_is_not_moving():
    obj = mover(pm=(0.1, 0.1), pm_err=0.5, is_mover=False)
    v = object_verdict(found(obj, classification="static"))
    assert (v.verdict, v.cause, v.certainty, v.decided_by) == ("not_moving", None, None, "spherex")


def test_older_surveys_that_agree_it_stays_put_say_so():
    obj = mover(pm=(0.1, 0.1), pm_err=0.5, is_mover=False)
    v = object_verdict(found(obj, Verdict.STATIONARY, classification="static"))
    assert (v.verdict, v.decided_by) == ("not_moving", "archive") and "same place" in v.reason


def test_a_variable_that_does_not_move_is_not_moving_and_says_it_varies():
    obj = replace(mover(pm=(0.0, 0.1), is_mover=False), is_variable=True)
    v = object_verdict(found(obj, classification="variable_star"))
    assert v.verdict == "not_moving" and "brightness" in v.reason


def test_under_one_pixel_only_a_significant_slope_is_a_claim():
    # 4" in a year: under a 6.15" pixel. At 40 sigma it is a claim (a split mover looks like this),
    # at 4 sigma it is not (the 3-5 sigma candidate path needs a pixel of shift).
    assert apparent_motion(found(mover(pm=(0.0, 4.0), pm_err=0.1, is_mover=False), classification="static"))
    assert not apparent_motion(found(mover(pm=(0.0, 4.0), pm_err=1.0, is_mover=False), classification="static"))


# --- evidence against the motion ---------------------------------------------------

def test_archive_refuted_is_a_chance_link():
    obj = mover()
    v = object_verdict(found(obj, Verdict.REFUTED, kind="static", classification="artifact"))
    assert (v.verdict, v.cause, v.decided_by) == ("fake_motion", "chance_link", "archive")


def test_archive_refuted_with_a_blended_pass_is_a_blend():
    obj = replace(mover(), blended_passes=[1])
    v = object_verdict(found(obj, Verdict.REFUTED, kind="static", classification="artifact"))
    assert (v.verdict, v.cause) == ("fake_motion", "blend")


def test_positions_on_different_slow_gaia_stars_are_a_chance_link():
    v = object_verdict(found(mover(), kind="static", classification="artifact", demoted="slow_stars"))
    assert (v.verdict, v.cause, v.decided_by) == ("fake_motion", "chance_link", "catalogue")


def test_a_pass_that_saw_nothing_on_the_path_is_a_chance_link():
    obj = replace(mover(pm_err=0.2, is_mover=False, step_yr=1.0), missing_passes=[1])  # 10" in 2 years
    v = object_verdict(found(obj, classification="static"))
    assert (v.verdict, v.cause, v.decided_by) == ("fake_motion", "chance_link", "spherex")


def test_a_saturated_star_with_an_empty_pass_is_unsure_not_fake():
    # Eps Indi A (a real 4.7"/yr mover) was called a chance link because one pass missed its
    # saturated, masked core.
    obj = replace(saturate(mover(pm_err=0.2, step_yr=1.0)), missing_passes=[1])
    v = object_verdict(found(obj, classification="static"))
    assert (v.verdict, v.cause, v.decided_by) == ("unsure", "saturated_halo", "spherex")


def at_wavelength(obj, um, flux=5.0):
    """Every detection at `um`; flux 5.0 (3x3 MJy/sr sum) is AB ~14.8, 0.5 is AB ~17.3."""
    return replace(obj, sources=[
        replace(s, detections=[replace(d, wavelength_um=um, flux=flux) for d in s.detections]) for s in obj.sources
    ])


def absent(obj, **kw):
    f = found(obj, Verdict.ABSENT, **kw)
    return replace(f, fused=replace(f.fused, absent_in=(Observatory.CATWISE,)))


def test_catwise_absence_of_a_mover_bright_at_wise_wavelengths_is_fake():
    v = object_verdict(absent(at_wavelength(mover(), 4.2)))
    assert (v.verdict, v.cause, v.decided_by) == ("fake_motion", "chance_link", "archive")
    assert "CatWISE lists no source moving like this" in v.reason and "AB 14.8" in v.reason


def test_catwise_absence_of_a_faint_mover_is_only_unsure():
    # CatWISE stacks 2010-2018 images: a faint fast mover's light is smeared along its path.
    v = object_verdict(absent(at_wavelength(mover(), 4.2, flux=0.5)))
    assert (v.verdict, v.decided_by) == ("unsure", "archive") and "faint (AB 17.3)" in v.reason


def test_catwise_absence_needs_two_clean_detections_at_wise_wavelengths():
    obj = at_wavelength(mover(), 4.2)
    one = replace(obj, sources=[
        replace(s, detections=[replace(d, contaminated=(i, j) != (0, 0)) for j, d in enumerate(s.detections)])
        for i, s in enumerate(obj.sources)
    ])
    assert object_verdict(absent(one)).verdict == "unsure"


def test_catwise_absence_names_the_engine_cause():
    obj = replace(at_wavelength(mover(), 4.2), blended_passes=[1])
    assert object_verdict(absent(obj)).cause == "blend"


def test_catwise_absence_is_unsure_for_a_saturated_star_or_one_seen_only_at_short_wavelengths():
    v = object_verdict(absent(saturate(at_wavelength(mover(), 4.2))))
    assert (v.verdict, v.cause) == ("unsure", "saturated_halo")
    v = object_verdict(absent(at_wavelength(mover(), 1.3)))  # a hot object can be faint for WISE
    assert (v.verdict, v.decided_by) == ("unsure", "archive") and "shorter than WISE" in v.reason


def test_a_catalogued_moving_star_or_known_asteroid_beats_a_catwise_absence():
    v = object_verdict(absent(at_wavelength(mover(), 4.2), classification="known_star", name="Gaia DR3 1"))
    assert (v.verdict, v.certainty) == ("moving", "confirmed")
    v = object_verdict(absent(at_wavelength(mover(), 4.2), classification="known_asteroid", name="(433) Eros"))
    assert (v.verdict, v.certainty) == ("moving", "confirmed")


def test_a_stored_absent_row_is_unsure_unless_catalogued_or_static():
    v = stored_verdict("mover", None, "absent", 12.0, 3, False)
    assert (v.verdict, v.decided_by) == ("unsure", "archive")
    assert stored_verdict("mover", "known_star", "absent", 12.0, 3, False).verdict == "moving"
    assert stored_verdict("static", "static", "absent", 2.0, 3, False).verdict == "not_moving"


def test_archive_stationary_makes_the_motion_fake_and_names_the_engine_cause():
    v = object_verdict(found(saturate(mover()), Verdict.STATIONARY, kind="static", classification="static"))
    assert (v.verdict, v.cause, v.decided_by) == ("fake_motion", "saturated_halo", "archive")
    plain = object_verdict(found(mover(), Verdict.STATIONARY, kind="static", classification="static"))
    assert (plain.verdict, plain.cause) == ("fake_motion", "other")


@pytest.mark.parametrize("demoted", ["slow_star", "lead_star"])
def test_a_track_on_one_slow_gaia_star_is_fake_and_quotes_its_motion(demoted):
    v = object_verdict(found(mover(), kind="static", classification="static", demoted=demoted,
                             matches=[gaia_match()]))
    assert (v.verdict, v.decided_by) == ("fake_motion", "catalogue")
    assert "Gaia DR3 42" in v.reason and "0.004″ a year" in v.reason


def test_a_faint_pass_is_named_as_the_cause():
    obj = mover()
    weak = replace(obj.sources[1], n_det=1, n_cover=4, snr=6.0)
    obj = replace(obj, sources=[obj.sources[0], weak, obj.sources[2]])
    v = object_verdict(found(obj, kind="static", classification="static", demoted="slow_star"))
    assert v.cause == "low_snr"


def test_an_archive_confirmation_on_other_stars_with_no_spherex_motion_is_fake():
    obj = mover(pm=(0.6, -1.5), pm_err=1.1, is_mover=False)
    f = Found(obj, fused(obj, Verdict.CONFIRMED), Known(kind="static", classification="static", archive_void=True))
    v = object_verdict(f)
    assert (v.verdict, v.cause) == ("fake_motion", "chance_link")


# --- evidence for the motion -------------------------------------------------------

def test_a_catalogued_star_moving_the_same_way_is_confirmed_even_with_a_missing_pass():
    obj = replace(mover(), missing_passes=[1])
    v = object_verdict(found(obj, kind="mover", classification="known_star", name="Barnard's Star"))
    assert (v.verdict, v.certainty, v.decided_by) == ("moving", "confirmed", "catalogue")
    assert "Barnard's Star" in v.reason


def test_a_known_asteroid_is_confirmed():
    v = object_verdict(found(mover(), classification="known_asteroid", name="(433) Eros"))
    assert (v.verdict, v.certainty) == ("moving", "confirmed")


def test_archive_confirmed_is_confirmed_motion_of_a_candidate_object():
    v = object_verdict(found(mover(), Verdict.CONFIRMED))
    assert (v.verdict, v.certainty, v.decided_by) == ("moving", "confirmed", "archive")
    assert "candidate" in v.reason and "discover" not in v.reason


def test_archive_confirmed_without_gaia_and_weak_in_spherex_is_only_a_candidate():
    obj = mover(pm=(0.3, 0.3), pm_err=0.2, is_mover=False)
    v = object_verdict(found(obj, Verdict.CONFIRMED, kind="mover", classification=None))
    assert (v.verdict, v.certainty) == ("moving", "candidate") and "Gaia was not checked" in v.reason


def test_archive_confirmed_saturated_star_that_gaia_does_not_explain_is_unsure():
    v = object_verdict(found(saturate(mover()), Verdict.CONFIRMED))
    assert (v.verdict, v.cause) == ("unsure", "saturated_halo")


def test_one_survey_family_alone_is_a_moving_candidate():
    obj = mover(pm=(1.0, 1.0), pm_err=0.5, is_mover=False)
    known = Known(kind="candidate", classification="unknown_candidate")
    f = Found(obj, replace(fused(obj), archive_candidate=True), known)
    v = object_verdict(f)
    assert (v.verdict, v.certainty, v.decided_by) == ("moving", "candidate", "archive")


# --- SPHEREx alone -----------------------------------------------------------------

def test_a_clean_spherex_only_mover_is_a_moving_candidate():
    v = object_verdict(found(mover()))
    assert (v.verdict, v.certainty, v.decided_by, v.cause) == ("moving", "candidate", "spherex", None)


def test_a_saturated_spherex_only_mover_is_fake_once_gaia_was_asked_and_unsure_otherwise():
    obj = saturate(mover())
    assert (object_verdict(found(obj)).verdict, object_verdict(found(obj)).cause) == ("fake_motion", "saturated_halo")
    unchecked = object_verdict(found(obj, classification=None))
    assert (unchecked.verdict, unchecked.cause) == ("unsure", "saturated_halo")


def test_a_mover_in_a_bright_stars_halo_is_suspect():
    obj = mover()
    bright = replace(saturate(mover(pm=(0.0, 0.0), is_mover=False)), ra=obj.ra + 60 / 3600, dec=obj.dec)
    distances = bright_star_distances([obj, bright])
    assert distances[0] == pytest.approx(60 * 0.9397, rel=1e-3)  # 60 s of RA at dec 20
    assert distances[1] is None  # the bright star itself has no other bright star near
    v = field_verdicts([found(obj), found(bright, classification="static")])[0]
    assert v.cause == "saturated_halo"
    far = replace(bright, ra=obj.ra + 0.1)
    assert field_verdicts([found(obj), found(far, classification="static")])[0].verdict == "moving"


def test_a_mover_that_needs_its_blended_pass_is_unsure_with_cause_blend():
    obj = replace(mover(n_passes=2, pm_err=0.2), blended_passes=[2])
    v = object_verdict(found(obj))
    assert (v.verdict, v.cause) == ("unsure", "blend")


def test_flagged_pixels_and_parallax_are_named():
    obj = mover()
    flagged = replace(obj, sources=[
        replace(obj.sources[0], detections=[replace(d, contaminated=True) for d in obj.sources[0].detections]),
        *obj.sources[1:],
    ])
    assert object_verdict(found(flagged)).cause == "flagged_pixels"
    assert object_verdict(found(replace(obj, parallax=0.5, parallax_err=0.2))).cause == "parallax_misfit"


def test_a_three_to_five_sigma_track_is_unsure():
    obj = mover(pm=(3.0, 4.0), pm_err=1.25, is_mover=False)  # 4 sigma, 5" a year over a year
    v = object_verdict(found(obj, kind="candidate"))
    assert (v.verdict, v.cause) == ("unsure", None) and "short of the 5" in v.reason


def test_a_two_pass_track_is_unsure():
    obj = mover(n_passes=2, pm=(6.0, 8.0), pm_err=0.8, is_mover=False, step_yr=1.0)
    v = object_verdict(found(obj, classification="static"))
    assert v.verdict == "unsure" and "two positions always fit a line" in v.reason


def eps_indi_b_halves():
    """Eps Indi B as gaia-truth found it: one 4.4"/yr mover split into two 2-pass tracks at 10 sigma, each
    spanning under half a year, so each moved less than a pixel."""
    def half(start_yr):
        passes = []
        for t in (start_yr, start_yr + 0.45):
            passes.append((330.8 + 3.7 * t / 3600.0 / 0.4, -56.8 - 2.4 * t / 3600.0, 60800.0 + t * YEAR))
        return tracked(passes, pm=(3.7, -2.4), pm_err=0.44, is_mover=False)
    return half(0.0), half(1.0)


def test_a_significant_motion_under_one_pixel_is_unsure_not_still():
    first, _ = eps_indi_b_halves()
    assert first.pm_significance > 9 and first.pm_total * 0.45 < PIXEL
    v = object_verdict(found(first, classification="static"), pixel_arcsec=PIXEL)
    assert (v.verdict, v.cause) == ("unsure", None)
    assert "about 4.4″ a year" in v.reason and "too little time between its visits" in v.reason


def test_two_halves_of_a_split_mover_make_a_moving_candidate():
    first, second = eps_indi_b_halves()
    verdicts = field_verdicts([found(first, classification="static"), found(second, classification="static")],
                              pixel_arcsec=PIXEL)
    assert [(v.verdict, v.certainty) for v in verdicts] == [("moving", "candidate")] * 2
    assert "split in two" in verdicts[0].reason
    # A track moving the same way somewhere else on the sky is no partner.
    far = replace(second, ra=second.ra + 0.05, sources=second.sources)
    lone = field_verdicts([found(first, classification="static"), found(far, classification="static")],
                          pixel_arcsec=PIXEL)
    assert [v.verdict for v in lone] == ["unsure", "unsure"]
    # Nor is one at the right place moving another way.
    other = replace(second, pm_ra_cosdec=-3.7, pm_dec=2.4)
    assert split_partners([first, other], PIXEL) == [None, None]


def test_next_to_a_slow_star_with_a_similar_motion_is_unsure_blend():
    v = object_verdict(found(mover(), kind="candidate", demoted="near_star", matches=[gaia_match()]))
    assert (v.verdict, v.cause, v.decided_by) == ("unsure", "blend", "catalogue")


# --- the real Barnard field (cached cutouts: one real mover, two false ones) --------

def test_barnard_field_verdicts():
    objects, fusion = barnard_field()
    known = Catalogues(gaia=BARNARD_FIELD_GAIA, simbad=BARNARD_FIELD_SIMBAD).check(objects, fusion).known
    barnard, still, hop = field_verdicts([Found(o, f, k) for o, f, k in zip(objects, fusion, known, strict=True)],
                                         pixel_arcsec=PIXEL)
    assert (barnard.verdict, barnard.certainty) == ("moving", "confirmed")
    assert (still.verdict, still.cause) == ("fake_motion", "chance_link")  # the voided archival "confirmation"
    assert (hop.verdict, hop.cause) == ("fake_motion", "chance_link")  # hops between two slow Gaia stars


# --- the image tests ----------------------------------------------------------------

@dataclass(frozen=True)
class Check:
    key: str
    status: str


def checks(**status):
    keys = ["beyond_noise", "track_line", "images_show_motion", "many_exposures", "detectors_agree", "same_look",
            "no_stand_in"]
    return [Check(k, status.get(k, "pass")) for k in keys]


CANDIDATE = MotionVerdict("moving", "SPHEREx alone.", "spherex", certainty="candidate")
ARCHIVE = MotionVerdict("moving", "Older surveys agree.", "archive", certainty="confirmed")
CATALOGUE = MotionVerdict("moving", "Barnard's Star.", "catalogue", certainty="confirmed")


def test_all_tests_passing_keeps_the_verdict_and_says_so():
    v = refine(CANDIDATE, checks())
    assert (v.verdict, v.certainty) == ("moving", "candidate") and "passes 7 of 7" in v.reason


@pytest.mark.parametrize(("key", "cause"), [("no_stand_in", "chance_link"), ("same_look", "chance_link"),
                                            ("beyond_noise", "other")])
def test_a_decisive_failure_makes_a_spherex_only_motion_fake(key, cause):
    v = refine(CANDIDATE, checks(**{key: "fail"}))
    assert (v.verdict, v.cause, v.decided_by) == ("fake_motion", cause, "evidence_checks")


def test_one_other_failure_is_unsure_and_two_are_fake():
    one = refine(CANDIDATE, checks(detectors_agree="fail"))
    assert (one.verdict, one.cause) == ("unsure", "wavelength_centroid")
    two = refine(CANDIDATE, checks(detectors_agree="fail", track_line="fail"))
    assert two.verdict == "fake_motion"


def test_archive_confirmation_survives_a_minor_failure_but_not_a_decisive_one():
    assert refine(ARCHIVE, checks(images_show_motion="fail")).verdict == "moving"
    assert refine(ARCHIVE, checks(no_stand_in="fail")).verdict == "unsure"


def test_catalogue_verdicts_and_settled_ones_ignore_the_image_tests():
    assert refine(CATALOGUE, checks(no_stand_in="fail", same_look="fail")) == CATALOGUE
    fake = MotionVerdict("fake_motion", "x", "archive", cause="blend")
    assert refine(fake, checks()) == fake


def test_unknown_and_weak_tests_count_for_nothing():
    v = refine(CANDIDATE, [Check("beyond_noise", "unknown"), Check("track_line", "weak")])
    assert v == CANDIDATE


# --- the verdict's own invariants, and stored survey rows ---------------------------

def test_invalid_verdicts_are_refused():
    with pytest.raises(ValueError):
        MotionVerdict("moving", "x", "spherex")  # moving needs a certainty
    with pytest.raises(ValueError):
        MotionVerdict("fake_motion", "x", "spherex")  # fake needs a cause
    with pytest.raises(ValueError):
        MotionVerdict("unsure", "x", "spherex", cause="gremlins")
    assert set(CAUSES) >= {"blend", "saturated_halo", "chance_link", "other"}


@pytest.mark.parametrize(("row", "expected"), [
    (("mover", None, "refuted", 6.0, 3, False), ("fake_motion", None, "chance_link")),
    (("mover", "known_star", "confirmed", 6.0, 3, False), ("moving", "confirmed", None)),
    (("mover", None, "confirmed", 2.2, 9, False), ("moving", "candidate", None)),  # the NEP rows, Gaia not run
    (("mover", "unknown_candidate", "confirmed", 6.0, 3, False), ("moving", "confirmed", None)),
    (("mover", "unknown_candidate", "unconfirmed", 6.0, 3, False), ("moving", "candidate", None)),
    (("candidate", "unknown_candidate", "unconfirmed", 3.5, 3, False), ("unsure", None, None)),
    (("static", "variable_star", "unconfirmed", 0.5, 3, True), ("not_moving", None, None)),
    (("static", "artifact", "unconfirmed", 4.0, 3, True), ("fake_motion", None, "chance_link")),
    (("static", "static", "stationary", 6.0, 3, True), ("fake_motion", None, "other")),
])
def test_stored_rows_get_a_verdict_from_their_columns(row, expected):
    v = stored_verdict(*row)
    assert (v.verdict, v.certainty, v.cause) == expected


def test_survey_names_leave_out_spherex_and_missing_entries():
    assert survey_names(["spherex", "2mass", None, "", "catwise"]) == "2MASS, CatWISE"
    assert survey_names([None, "spherex"]) == "older surveys"
    assert survey_names(["someday_survey"]) == "someday_survey"


def test_the_sqlite_function_reads_raw_columns():
    assert stored_verdict_sql("mover", None, "refuted", 6.0, "[{}, {}, {}]", 0) == "fake_motion"
    assert stored_verdict_sql("candidate", None, None, 3.5, "not json", 0) == "unsure"
