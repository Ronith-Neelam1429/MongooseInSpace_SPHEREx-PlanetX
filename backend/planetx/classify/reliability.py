"""Is an object's motion real? One honest verdict per object. Pure functions: no network.

The engine's "mover" flag rests on one number (fitted speed over its error), and the
checks after it (older surveys, Gaia, the image tests) each overrule it in their own
way. This module reads all of them and says, for every tracked object:

  moving       the motion is real: `certainty` "confirmed" (a catalogue or older
               surveys agree) or "candidate" (SPHEREx alone, or one survey family)
  not_moving   no motion beyond what noise does
  fake_motion  SPHEREx shows motion, and the evidence says it is not real; `cause`
               (CAUSES, as in docs/RESULTS.md) says what most likely made it
  unsure       motion is claimed but the evidence cannot settle it; `cause`, when
               set, is the suspected problem

There is no probability: nothing measured yet says how often each rule is right, so
a number would be invented. `decided_by` says which evidence settled it instead.

The first rule that applies wins, in this order: an apparent motion is needed at all;
evidence against it from outside SPHEREx (older surveys, Gaia); evidence for it from
outside SPHEREx; then what SPHEREx alone can say, with each known way the engine
fakes a motion (saturation, blends, faint passes, few passes). `refine` adds the
evidence view's image tests (planetx.api.evidence_checks), which only run on request.
"""
from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from planetx.engine import TrackedObject
from planetx.observatories import FusedMotion, Verdict

from .catalogs import DAYS_PER_YEAR
from .check import CANDIDATE_MIN_PASSES, CANDIDATE_SIGMA, Known

VERDICTS = ("moving", "not_moving", "fake_motion", "unsure")
CERTAINTIES = ("confirmed", "candidate")
# What makes a fake mover (docs/RESULTS.md, false_movers.json).
CAUSES = (
    "blend", "saturated_halo", "edge", "flagged_pixels", "low_snr", "wavelength_centroid",
    "parallax_misfit", "chance_link", "other",
)
DECIDERS = ("spherex", "archive", "catalogue", "evidence_checks")

SPHEREX_PIXEL_ARCSEC = 6.15  # planetx.spherex.cutouts.PIXEL_SCALE_ARCSEC
# The engine's own bar for a mover (find_motion's mover_sigma).
MOVER_SIGMA = 5.0
# Engine-style "weak pass" (engine.motion._weak): found in under half the exposures that
# covered it, and faint in all of them.
WEAK_DETECT_FRAC = 0.5
WEAK_SNR = 10.0
# A pass whose detections are mostly on pixels SPHEREx flags as ghost, halo, crosstalk,
# persistence or streak light (Detection.contaminated).
CONTAMINATED_FRAC = 0.5
# Very bright (heavily saturated) stars spread light this far (engine.motion: the same
# radius and size it uses to warn about variability near such stars).
BRIGHT_SAT_AREA_PX = 50
BRIGHT_HALO_ARCSEC = 150.0
# Archive verdict "absent" (CatWISE lists nothing moving like the track where it puts the object)
# settles a motion only when SPHEREx saw the object at WISE's wavelengths (W1 3.4 um, W2 4.6 um)
# and bright there. CatWISE finds sources on 2010-2018 stacks, where a fast mover's light is
# spread along its path, so for one it is about as deep as single WISE exposures (5 sigma near
# W1 15.3 / W2 14.5 Vega, AB ~18; approximate) rather than ~3 mag deeper than SPHEREx. At AB 16.5
# or brighter every single exposure sees it at ~20 sigma. WISE 0855 (W2 ~14 Vega, AB ~17.3,
# 8"/yr) is in CatWISE with the right motion, so the bar is cautious. SPHEREx magnitudes use the
# 3x3 box flux without an aperture correction: slightly too faint, the cautious direction.
WISE_BAND_MIN_UM = 2.8
WISE_BAND_MIN_DETECTIONS = 2
WISE_SURE_AB_MAG = 16.5


@dataclass(frozen=True)
class MotionVerdict:
    verdict: str  # VERDICTS
    reason: str  # one or two plain sentences, with the numbers that decided it
    decided_by: str  # DECIDERS: the evidence that settled it
    certainty: str | None = None  # CERTAINTIES, moving only
    cause: str | None = None  # CAUSES: fake_motion always, unsure when one is suspected

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(f"unknown verdict {self.verdict!r}")
        if self.decided_by not in DECIDERS:
            raise ValueError(f"unknown decider {self.decided_by!r}")
        if (self.certainty is not None) != (self.verdict == "moving") or (
            self.certainty is not None and self.certainty not in CERTAINTIES
        ):
            raise ValueError(f"certainty {self.certainty!r} does not fit verdict {self.verdict!r}")
        if self.cause is not None and self.cause not in CAUSES:
            raise ValueError(f"unknown cause {self.cause!r}")
        if self.verdict == "fake_motion" and self.cause is None:
            raise ValueError("fake_motion needs a cause")
        # Reasons are sentences, also when they open with a name such as "older surveys".
        object.__setattr__(self, "reason", self.reason[:1].upper() + self.reason[1:])


class FoundLike(Protocol):
    """What the pipeline knows about one object (`planetx.pipeline.FieldObject`, a frozen dataclass)."""

    @property
    def tracked(self) -> TrackedObject: ...
    @property
    def fused(self) -> FusedMotion: ...
    @property
    def known(self) -> Known: ...


class CheckLike(Protocol):
    """One evidence test (`planetx.api.schemas.EvidenceCheck`)."""

    @property
    def key(self) -> str: ...
    @property
    def status(self) -> str: ...  # pass | weak | fail | unknown


def _sigma(x: float) -> str:
    return f"{x:.1f} times its measurement error"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _span_yr(obj: TrackedObject) -> float:
    return (obj.sources[-1].mjd - obj.sources[0].mjd) / DAYS_PER_YEAR if obj.sources else 0.0


def apparent_motion(found: FoundLike, pixel_arcsec: float = SPHEREX_PIXEL_ARCSEC) -> bool:
    """Whether anything claims this object moves: the engine, the archival refit, or SPHEREx
    numbers that clear the engine's bar but were stopped by one of its guards.

    A motion of MOVER_SIGMA or more counts even when its total shift is under a pixel: a
    real mover split into short tracks looks exactly like that (Eps Indi B, 4.7"/yr, came
    out as two 2-pass tracks of ~4.4"/yr at 10 sigma). Gaia-static stars do so rarely
    (10 of 2,686 in gaia-truth's interim fields), and those are then "unsure", not movers.
    """
    obj, fm, known = found.tracked, found.fused, found.known
    if obj.is_mover or known.kind != "static" or known.demoted is not None:
        return True
    if fm.verdict is Verdict.CONFIRMED or fm.archive_candidate:
        return True
    if obj.pm_significance >= MOVER_SIGMA:
        return True
    shift = obj.pm_total * _span_yr(obj)
    return (
        shift >= pixel_arcsec and len(obj.sources) >= CANDIDATE_MIN_PASSES and obj.pm_significance >= CANDIDATE_SIGMA
    )


# A short track's split partner: another track that moves the same way (within this many
# sigma of the two motions' combined error) and whose position, moved by that motion to the
# partner's date, lands within SPLIT_RADIUS_PX SPHEREx pixels of it.
SPLIT_AGREE_SIGMA = 3.0
SPLIT_RADIUS_PX = 2.0


def split_partners(
    objects: Sequence[TrackedObject], pixel_arcsec: float = SPHEREX_PIXEL_ARCSEC
) -> list[TrackedObject | None]:
    """For each significant track shorter than a pixel of total shift, another track that is
    most likely the same moving object split in two; None otherwise."""
    def short_and_significant(o: TrackedObject) -> bool:
        return o.pm_significance >= MOVER_SIGMA and o.pm_total * _span_yr(o) < pixel_arcsec

    out: list[TrackedObject | None] = []
    for a in objects:
        partner = None
        if short_and_significant(a):
            for b in objects:
                if b is a or b.pm_significance < CANDIDATE_SIGMA:
                    continue
                err = math.hypot(a.pm_err, b.pm_err)
                if math.hypot(a.pm_ra_cosdec - b.pm_ra_cosdec, a.pm_dec - b.pm_dec) > SPLIT_AGREE_SIGMA * err:
                    continue
                years = (b.ref_mjd - a.ref_mjd) / DAYS_PER_YEAR
                ra = a.ra + a.pm_ra_cosdec * years / 3600.0 / max(math.cos(math.radians(a.dec)), 1e-9)
                dec = a.dec + a.pm_dec * years / 3600.0
                if _separation_arcsec(ra, dec, b.ra, b.dec) <= SPLIT_RADIUS_PX * pixel_arcsec:
                    partner = b
                    break
        out.append(partner)
    return out


def bright_star_distances(objects: Sequence[TrackedObject]) -> list[float | None]:
    """For each object, the distance (arcsec) to the nearest other object that is a very bright,
    heavily saturated star, or None when there is none in the field."""
    def heavy(o: TrackedObject) -> bool:
        return max((d.sat_area for s in o.sources for d in s.detections), default=0) >= BRIGHT_SAT_AREA_PX

    bright = [o for o in objects if heavy(o)]
    out: list[float | None] = []
    for o in objects:
        seps = [_separation_arcsec(o.ra, o.dec, b.ra, b.dec) for b in bright if b is not o]
        out.append(min(seps) if seps else None)
    return out


def _separation_arcsec(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    a1, d1, a2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    h = math.sin((d1 - d2) / 2) ** 2 + math.cos(d1) * math.cos(d2) * math.sin((a1 - a2) / 2) ** 2
    return math.degrees(2 * math.asin(math.sqrt(min(1.0, max(0.0, h))))) * 3600.0


def _saturated(obj: TrackedObject) -> bool:
    return any(d.saturated for s in obj.sources for d in s.detections)


def _weak_passes(obj: TrackedObject) -> int:
    return sum(
        1 for s in obj.sources
        if s.n_cover > 1 and s.n_det < WEAK_DETECT_FRAC * s.n_cover and s.snr < WEAK_SNR
    )


def _wise_band_mag(obj: TrackedObject, pixel_arcsec: float) -> float | None:
    """Median AB magnitude of the clean detections at WISE's wavelengths; None if too few."""
    fluxes = [
        d.flux for s in obj.sources for d in s.detections
        if d.wavelength_um >= WISE_BAND_MIN_UM and not d.saturated and not d.contaminated and d.flux > 0
    ]
    if len(fluxes) < WISE_BAND_MIN_DETECTIONS:
        return None
    # Detection.flux is a 3x3 sum of MJy/sr pixels: times the pixel solid angle it is MJy.
    pixel_sr = (pixel_arcsec / 206264.806) ** 2
    jy = statistics.median(fluxes) * 1e6 * pixel_sr
    return -2.5 * math.log10(jy / 3631.0)


def _contaminated_passes(obj: TrackedObject) -> int:
    n = 0
    for s in obj.sources:
        if s.detections and sum(d.contaminated for d in s.detections) >= CONTAMINATED_FRAC * len(s.detections):
            n += 1
    return n


_SURVEY_NAMES = {"2mass": "2MASS", "allwise": "AllWISE", "catwise": "CatWISE", "neowise": "NEOWISE"}


def survey_names(observatories: Iterable[str | None]) -> str:
    """The older surveys among `observatories` (Observatory values) for a sentence, e.g. "2MASS, AllWISE";
    "older surveys" when there are none. SPHEREx itself and missing (None or empty) entries are left out."""
    seen = [_SURVEY_NAMES.get(o, o) for o in observatories if o and o != "spherex"]
    return ", ".join(seen) if seen else "older surveys"


def _archive_names(fm: FusedMotion) -> str:
    return survey_names(str(o) for o in fm.observatories)


def _gaia_star(known: Known):
    return next((m for m in known.matches if m.catalog == "gaia"), None)


def _star_text(known: Known) -> str:
    """'Gaia DR3 123 (0.004″/yr)' for the Gaia star at the object's position, if known."""
    star = _gaia_star(known)
    if star is None:
        return "a slow Gaia star"
    if star.pm_ra_cosdec is not None and star.pm_dec is not None:
        return f"{star.name}, which Gaia measures moving only {math.hypot(star.pm_ra_cosdec, star.pm_dec):.3f}″ a year"
    return star.name


def _engine_cause(obj: TrackedObject, bright_arcsec: float | None) -> str | None:
    """The known way SPHEREx alone fakes a motion that this track shows, most telling first."""
    if _saturated(obj):
        return "saturated_halo"
    if bright_arcsec is not None and bright_arcsec <= BRIGHT_HALO_ARCSEC:
        return "saturated_halo"
    if obj.blended_passes:
        return "blend"
    if _contaminated_passes(obj):
        return "flagged_pixels"
    if _weak_passes(obj):
        return "low_snr"
    if obj.parallax is not None and len(obj.sources) <= 3:
        return "parallax_misfit"
    return None


_CAUSE_TEXT = {
    "saturated_halo": "it is too bright for the detector or sits in a very bright star's glare, where measured "
                      "positions wander",
    "blend": "it overlaps another source in some passes, which pulls its measured position",
    "flagged_pixels": "most of its detections in a pass sit on pixels SPHEREx flags as ghost, halo or crosstalk light",
    "low_snr": "it is faint: in a pass it was found in fewer than half the exposures that covered it",
    "parallax_misfit": "its positions only fit with a parallax, which so few passes cannot pin down",
    "chance_link": "its positions belong to different objects joined into one track",
    "wavelength_centroid": "SPHEREx's detectors see it at different spots, as a colour-dependent position would",
    "edge": "it lies at the edge of the images",
    "other": "no single cause stands out",
}


def object_verdict(
    found: FoundLike,
    *,
    bright_star_arcsec: float | None = None,
    split_partner: TrackedObject | None = None,
    pixel_arcsec: float = SPHEREX_PIXEL_ARCSEC,
) -> MotionVerdict:
    """The verdict for one pipeline object (see the module docstring for the rule order).

    `bright_star_arcsec`: distance to the nearest very bright star in the field
    (`bright_star_distances`), and `split_partner`: another track that is most likely the
    same object (`split_partners`), when the caller knows the field.
    """
    obj, fm, known = found.tracked, found.fused, found.known
    sigma = obj.pm_significance
    speed = f"{obj.pm_total:.2f}″ a year"
    checked = known.classification is not None  # Gaia (and SIMBAD, SkyBoT) were consulted
    archive = "unconfirmed" if known.archive_void else fm.verdict.value
    years = f" over {fm.baseline_yr:.0f} years" if fm.baseline_yr and fm.baseline_yr > 1.5 else ""

    if not apparent_motion(found, pixel_arcsec):
        reason = f"SPHEREx sees no motion beyond the noise ({sigma:.1f} times its measurement error)."
        decided = "spherex"
        if archive == "stationary":
            reason = f"{_archive_names(fm)} also saw it in the same place{years}. " + reason
            decided = "archive"
        if obj.is_variable:
            reason += " It does change brightness."
        return MotionVerdict("not_moving", reason, decided)

    # A catalogued star whose own motion follows every pass position settles it first:
    # the engine's guards and the older surveys' matching can both miss a real mover.
    if known.classification == "known_star":
        star = known.name or "A catalogued star"
        return MotionVerdict(
            "moving", f"{star} is catalogued moving the same way, and SPHEREx follows it.", "catalogue",
            certainty="confirmed",
        )

    # --- evidence against the motion from outside SPHEREx ---
    if archive == "refuted":
        cause = "blend" if obj.blended_passes else "chance_link"
        return MotionVerdict(
            "fake_motion", f"{_archive_names(fm)} show a separate, fixed source at each SPHEREx position: the engine "
            "joined neighbouring stars into one track, and nothing moved.", "archive", cause=cause,
        )
    if known.demoted == "slow_stars":
        return MotionVerdict(
            "fake_motion", "Each SPHEREx position falls on a different Gaia star, none moving anywhere near "
            f"{speed}: the track hops between fixed stars.", "catalogue", cause="chance_link",
        )
    if obj.missing_passes:
        n = len(obj.missing_passes)
        if _saturated(obj):
            # A saturated star's core is masked and its patch is often cut at an image edge, so an
            # empty pass is weak evidence here (Eps Indi A, a real 4.7"/yr mover, was called fake).
            return MotionVerdict(
                "unsure", f"{_plural(n, 'pass').capitalize()} of SPHEREx covered its path and found nothing, but it is "
                "a saturated star, whose masked core or a cut image edge can hide it: not enough to call the motion "
                "real or fake.", "spherex", cause="saturated_halo",
            )
        return MotionVerdict(
            "fake_motion", f"{_plural(n, 'pass').capitalize()} of SPHEREx covered the path it would have taken and saw "
            "nothing there, so its positions belong to different objects.", "spherex", cause="chance_link",
        )
    cause = _engine_cause(obj, bright_star_arcsec)
    if archive == "absent" and known.classification != "known_asteroid":
        names = survey_names(str(o) for o in fm.absent_in)
        nothing = f"{names} lists no source moving like this where its track puts it in 2015"
        mag = _wise_band_mag(obj, pixel_arcsec)
        if cause == "saturated_halo":
            return MotionVerdict(
                "unsure", f"{nothing}, but {_CAUSE_TEXT[cause]}, and WISE is confused there too: not enough to "
                "call the motion real or fake.", "archive", cause=cause,
            )
        if mag is None:
            return MotionVerdict(
                "unsure", f"{nothing}, but SPHEREx saw it only at wavelengths shorter than WISE's, where a very hot "
                "object could be too faint for WISE.", "archive", cause=cause,
            )
        if mag > WISE_SURE_AB_MAG:
            return MotionVerdict(
                "unsure", f"{nothing}, which points to a fake motion, but at 3-5 µm it is faint (AB {mag:.1f}): "
                "a real fast mover that faint can be smeared out of CatWISE's stacked images.", "archive",
                cause=cause,
            )
        return MotionVerdict(
            "fake_motion", f"It is bright at 3-5 µm (AB {mag:.1f}), where every single WISE exposure would see it, "
            f"yet {nothing}: the {speed} is not real; most likely {_CAUSE_TEXT[cause or 'chance_link']}.",
            "archive", cause=cause or "chance_link",
        )
    if archive == "stationary":
        return MotionVerdict(
            "fake_motion", f"{_archive_names(fm)} saw it in the same place{years}, so the {speed} SPHEREx measured is "
            f"not real; most likely {_CAUSE_TEXT[cause or 'other']}.", "archive", cause=cause or "other",
        )
    if known.demoted in ("slow_star", "lead_star"):
        return MotionVerdict(
            "fake_motion", f"It sits on {_star_text(known)}; the {speed} SPHEREx measured is not real, most likely "
            f"because {_CAUSE_TEXT[cause or 'other']}.", "catalogue", cause=cause or "other",
        )
    if known.archive_void and not obj.is_mover and known.kind == "static":
        return MotionVerdict(
            "fake_motion", "Older surveys seemed to show it moving, but those positions belong to other Gaia stars, "
            f"and SPHEREx alone sees no real motion ({sigma:.1f} times its error).", "catalogue", cause="chance_link",
        )

    # --- evidence for the motion from outside SPHEREx ---
    if known.classification == "known_asteroid":
        return MotionVerdict(
            "moving", f"A known solar-system body ({known.name or 'from SkyBoT'}) was at its position.", "catalogue",
            certainty="confirmed",
        )
    if archive == "confirmed":
        if not checked and sigma < MOVER_SIGMA:
            return MotionVerdict(
                "moving", f"{_archive_names(fm)} positions lie on its track{years}, but Gaia was not checked, and Gaia "
                "is what catches archival positions that belong to other stars: a candidate.", "archive",
                certainty="candidate",
            )
        if cause == "saturated_halo" and checked:
            return MotionVerdict(
                "unsure", f"{_archive_names(fm)} positions lie on its track, but it is a saturated star or sits in a "
                "very bright star's halo, and Gaia lists no star here moving like this.", "archive",
                cause="saturated_halo",
            )
        tail = (" No catalogue lists this motion: an uncatalogued candidate whose motion older surveys confirm."
                if checked else " The catalogues were not checked.")
        return MotionVerdict(
            "moving", f"{_archive_names(fm)} positions{years} lie on the same track.{tail}", "archive",
            certainty="confirmed",
        )

    # --- SPHEREx alone (the archive cannot say, or was not asked) ---
    if known.demoted == "near_star":
        return MotionVerdict(
            "unsure", f"It lies next to {_star_text(known)}, and its motion is too close to that star's for SPHEREx "
            "to tell the two apart.", "catalogue", cause="blend",
        )
    void = (" Older surveys seemed to agree, but those positions belong to other Gaia stars."
            if known.archive_void else "")
    if cause == "saturated_halo":
        if checked:
            return MotionVerdict(
                "fake_motion", f"Only SPHEREx shows the {speed}, and {_CAUSE_TEXT[cause]}; Gaia lists no star here "
                f"moving like this.{void}", "catalogue", cause=cause,
            )
        return MotionVerdict(
            "unsure", f"Only SPHEREx shows the {speed}, and {_CAUSE_TEXT[cause]}. The catalogues were not checked."
            f"{void}", "spherex", cause=cause,
        )
    if cause is not None:
        return MotionVerdict(
            "unsure", f"SPHEREx alone shows {speed} ({_sigma(sigma)}), but {_CAUSE_TEXT[cause]}.{void}",
            "spherex", cause=cause,
        )
    if fm.archive_candidate:
        return MotionVerdict(
            "moving", f"Positions from one older survey family ({_archive_names(fm)}) show it moving too; a second, "
            "independent survey is needed to confirm it.", "archive", certainty="candidate",
        )
    if known.kind == "mover" and obj.is_mover:
        unchecked = "" if checked else " The catalogues were not checked."
        return MotionVerdict(
            "moving", f"SPHEREx alone measures {speed}, {_sigma(sigma)}, across "
            f"{_plural(len(obj.sources), 'pass')}; no other telescope has confirmed it yet.{unchecked}{void}",
            "spherex", certainty="candidate",
        )
    shift = obj.pm_total * _span_yr(obj)
    if sigma >= MOVER_SIGMA and shift < pixel_arcsec:
        about = f"about {obj.pm_total:.1f}″ a year ({_sigma(sigma)})"
        if split_partner is not None:
            return MotionVerdict(
                "moving", f"It moves {about}, and another track nearby moves the same way "
                f"({split_partner.pm_total:.1f}″ a year) along the same path: most likely one object whose track "
                f"was split in two. Each piece alone spans too little time to be sure.{void}", "spherex",
                certainty="candidate",
            )
        return MotionVerdict(
            "unsure", f"It moves {about}, but there is too little time between its visits to be sure: it shifted "
            f"only {shift:.1f}″, less than one SPHEREx pixel.{void}", "spherex",
        )
    if len(obj.sources) < CANDIDATE_MIN_PASSES:
        return MotionVerdict(
            "unsure", f"Seen in only {_plural(len(obj.sources), 'pass')}: two positions always fit a line, so "
            f"{speed} at {_sigma(sigma)} is not enough to tell.{void}", "spherex",
        )
    if sigma >= MOVER_SIGMA:
        return MotionVerdict(
            "unsure", f"SPHEREx measures {speed}, {_sigma(sigma)}, but the engine did not accept it as a mover: "
            f"its passes do not all hold up.{void}", "spherex",
        )
    return MotionVerdict(
        "unsure", f"SPHEREx measures {speed}, {_sigma(sigma)}: above the noise, but short of the "
        f"{MOVER_SIGMA:.0f} the engine asks for.{void}", "spherex",
    )


def field_verdicts(found: Sequence[FoundLike], pixel_arcsec: float = SPHEREX_PIXEL_ARCSEC) -> list[MotionVerdict]:
    """`object_verdict` for every object of one field, with the field's bright stars and split tracks
    taken into account."""
    tracks = [f.tracked for f in found]
    bright = bright_star_distances(tracks)
    partners = split_partners(tracks, pixel_arcsec)
    return [
        object_verdict(f, bright_star_arcsec=b, split_partner=p, pixel_arcsec=pixel_arcsec)
        for f, b, p in zip(found, bright, partners, strict=True)
    ]


# --- the evidence view's image tests -----------------------------------------------

# A failed test and the cause it points to. "Decisive" tests catch a fake on their own:
# a second object at its old or new spot, a different object in another pass, or a
# move no larger than the fixed stars' apparent shifts.
_DECISIVE = {"no_stand_in": "chance_link", "same_look": "chance_link", "beyond_noise": "other"}
_HINTS = {
    "detectors_agree": "wavelength_centroid", "track_line": "chance_link", "images_show_motion": "other",
    "many_exposures": "other",
}
_FAILED_TEXT = {
    "no_stand_in": "another source sat at one of its positions in a different pass",
    "same_look": "its brightness or colour differs between passes, as two different objects would",
    "beyond_noise": "its shift is no larger than the field's fixed stars appear to shift",
    "detectors_agree": "SPHEREx's detectors do not agree on the move",
    "track_line": "its positions do not lie on one straight line",
    "images_show_motion": "the difference image does not show the move",
    "many_exposures": "it was found in too few single exposures",
}


def refine(verdict: MotionVerdict, checks: Iterable[CheckLike]) -> MotionVerdict:
    """The verdict after the evidence view's image tests (`EvidenceCheck`s).

    They only matter for a claimed motion that a catalogue did not settle: a
    catalogued star's or asteroid's motion stands, and a fake stays fake. A decisive
    failure, or two other failures, make a SPHEREx-only motion fake; one other failure
    makes it unsure. Older surveys' confirmation drops to unsure on a decisive failure.
    """
    if verdict.verdict in ("not_moving", "fake_motion") or verdict.decided_by == "catalogue":
        return verdict
    checks = list(checks)
    failed = [c.key for c in checks if c.status == "fail"]
    decisive = [k for k in failed if k in _DECISIVE]
    if not failed:
        passed = sum(c.status == "pass" for c in checks)
        if passed == 0:
            return verdict
        return replace(
            verdict, reason=f"{verdict.reason} It passes {passed} of {len(checks)} image tests, failing none."
        )
    what = "; ".join(_FAILED_TEXT.get(k, k) for k in failed)
    cause = _DECISIVE.get(decisive[0]) if decisive else _HINTS.get(failed[0], "other")
    if verdict.verdict == "moving" and verdict.certainty == "confirmed":
        if not decisive:
            return replace(verdict, reason=f"{verdict.reason} One caution from the SPHEREx images: {what}.")
        return MotionVerdict(
            "unsure", f"{verdict.reason} But in the SPHEREx images {what}.", "evidence_checks", cause=cause,
        )
    if decisive or len(failed) >= 2:
        return MotionVerdict(
            "fake_motion", f"The SPHEREx images argue against the motion: {what}.", "evidence_checks", cause=cause,
        )
    return MotionVerdict(
        "unsure", f"{verdict.reason} But in the SPHEREx images {what}.", "evidence_checks",
        cause=verdict.cause or cause,
    )


# --- survey rows stored before verdicts were -------------------------------------


def stored_verdict(
    kind: str,
    classification: str | None,
    archive_verdict: str | None,
    significance: float,
    n_passes: int,
    is_variable: bool,
) -> MotionVerdict:
    """A verdict from what a survey candidate row keeps (no saturation, blend or catalogue detail).

    For rows stored before verdicts were; weaker than `object_verdict`, so it never
    calls a motion fake that the stored columns do not show to be.
    """
    sigma = f"{significance:.1f} times its measurement error" if math.isfinite(significance) else "unknown"
    if archive_verdict == "refuted":
        return MotionVerdict(
            "fake_motion", "Older surveys show a separate, fixed source at each SPHEREx position: neighbouring stars "
            "joined into one track.", "archive", cause="chance_link",
        )
    if kind == "static":
        if archive_verdict == "stationary" and significance >= MOVER_SIGMA:
            return MotionVerdict(
                "fake_motion", f"SPHEREx measured motion at {sigma}, but older surveys saw it in the same place.",
                "archive", cause="other",
            )
        if classification == "artifact" and significance >= CANDIDATE_SIGMA:
            return MotionVerdict(
                "fake_motion", "Its apparent motion was set aside: its positions fall on different fixed stars.",
                "catalogue", cause="chance_link",
            )
        if significance >= MOVER_SIGMA:
            return MotionVerdict(
                "unsure", f"SPHEREx measured motion at {sigma}, but the engine did not accept it as a mover (too "
                "little shift between visits, or passes that do not hold up).", "spherex",
            )
        tail = " It does change brightness." if is_variable else ""
        return MotionVerdict("not_moving", f"No motion beyond the noise ({sigma}).{tail}", "spherex")
    if classification in ("known_star", "known_asteroid"):
        what = "star" if classification == "known_star" else "solar-system body"
        return MotionVerdict(
            "moving", f"A catalogued {what} moving the same way.", "catalogue", certainty="confirmed",
        )
    if archive_verdict == "absent":
        # The row keeps no detections, so saturation, wavelength and brightness cannot be checked.
        return MotionVerdict(
            "unsure", "CatWISE lists no source moving like this where its track puts it in 2015; whether WISE "
            "should have seen it cannot be checked from the stored row.", "archive",
        )
    if archive_verdict == "confirmed":
        if classification is None and significance < MOVER_SIGMA:
            return MotionVerdict(
                "moving", "Older-survey positions lie on its track, but Gaia was not checked, and Gaia is what "
                "catches archival positions that belong to other stars: a candidate.", "archive",
                certainty="candidate",
            )
        return MotionVerdict(
            "moving", "Older-survey positions lie on the same track.", "archive", certainty="confirmed",
        )
    if kind == "mover":
        return MotionVerdict(
            "moving", f"SPHEREx alone, at {sigma} over {_plural(n_passes, 'pass')}; no other telescope has confirmed "
            "it yet.", "spherex", certainty="candidate",
        )
    return MotionVerdict(
        "unsure", f"SPHEREx measures motion at {sigma}: above the noise, but short of what the engine asks for.",
        "spherex",
    )


def stored_verdict_sql(
    kind: str, classification: str | None, archive_verdict: str | None, significance: float, track: str,
    is_variable: int,
) -> str:
    """`stored_verdict(...).verdict` from raw candidates-table columns, for an SQLite function."""
    try:
        n_passes = len(json.loads(track))
    except (TypeError, ValueError):
        n_passes = 0
    return stored_verdict(kind, classification, archive_verdict, significance, n_passes, bool(is_variable)).verdict
