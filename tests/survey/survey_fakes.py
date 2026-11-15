"""Fake pipeline objects for the survey processor and candidates tests (no network, no engine run)."""
from planetx.classify import Known
from planetx.engine import PassSource, TrackedObject
from planetx.observatories.fusion import FusedMotion, Verdict
from planetx.pipeline import FieldObject, FieldRun
from planetx.survey.candidates import Candidate, TrackPoint


def field_object(
    ra: float,
    dec: float,
    *,
    kind: str = "mover",
    classification: str | None = "unknown_candidate",
    name: str | None = None,
    pm: tuple[float, float] = (0.0, 10.0),
    pm_err: float = 1.0,
    is_variable: bool = False,
    variability_sigma: float = 6.0,
) -> FieldObject:
    sources = [
        PassSource(pass_index=i, ra=ra, dec=dec + i * 1e-4, sigma_arcsec=0.5, mjd=60900.0 + 180 * i,
                   n_det=6, n_cover=6, snr=40.0, flux_by_detector={})
        for i in range(3)
    ]
    tracked = TrackedObject(
        sources=sources, link="mover" if kind == "mover" else "static", ra=ra, dec=dec, ref_mjd=61080.0,
        pm_ra_cosdec=pm[0], pm_dec=pm[1], pm_err=pm_err, chi2_dof=1.0, is_mover=kind == "mover",
        is_variable=is_variable,
        variability={"detector": "D2", "ratio": 0.5, "significance": variability_sigma, "change": "fainter"}
        if is_variable else None,
    )
    fused = FusedMotion(verdict=Verdict.UNCONFIRMED, ra=ra, dec=dec, ref_mjd=61080.0, pm_ra_cosdec=pm[0],
                        pm_dec=pm[1], pm_err=pm_err, chi2_dof=1.0, baseline_yr=1.0, n_epochs=3)
    return FieldObject(tracked=tracked, fused=fused, known=Known(kind=kind, classification=classification, name=name))


def field_run(objects: list[FieldObject]) -> FieldRun:
    return FieldRun(result=None, objects=objects, notes=[])


def candidate(ra: float = 10.0, dec: float = 20.0, *, score: float = 0.5, classification: str | None = "known_star",
              name: str | None = "Star") -> Candidate:
    return Candidate(
        ra=ra, dec=dec, ref_mjd=61080.0, kind="mover", classification=classification, name=name,
        is_variable=False, pm_ra_cosdec=-0.8, pm_dec=10.36, pm_total=10.39, pm_err=0.5, significance=20.8,
        position_angle_deg=355.6, archive_verdict="confirmed",
        track=(TrackPoint(ra=ra, dec=dec, mjd=60900.0, sigma_arcsec=0.4, n_frames=6),
               TrackPoint(ra=ra, dec=dec + 1e-3, mjd=61080.0, sigma_arcsec=0.4, n_frames=5)),
        score=score, field_ra=ra, field_dec=dec,
    )
