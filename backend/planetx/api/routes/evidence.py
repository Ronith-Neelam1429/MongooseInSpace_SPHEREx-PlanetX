"""Evidence for one engine object: the images and measurements behind its verdict.

Engine data of a finished run comes from `planetx.api.motion.finished_run`; the
images are built in `planetx.api.evidence`. See docs/API.md.
"""
import base64
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response

from planetx.api import evidence as proof
from planetx.api import motion as runs
from planetx.api.routes.motion import DEFAULT_SIZE_DEG, MAX_SIZE_DEG, MIN_SIZE_DEG
from planetx.api.routes.sky import Dec, Ra
from planetx.api.schemas import MotionEvidence, SurveyGap, SurveyImage, SurveyImages
from planetx.observatories import images as older
from planetx.spherex import archive, cutouts

router = APIRouter(prefix="/motion", tags=["motion"])

# Far above what a field holds (a few hundred objects); only keeps the parameter bounded.
MAX_OBJECT_ID = 100_000
# The evidence of a finished run does not change while the run is kept (an hour); a
# re-run can renumber its objects, so browsers may reuse an answer only briefly.
CACHE_CONTROL = "public, max-age=300"


@router.get("/evidence")
def evidence(
    ra: Ra,
    dec: Dec,
    object_id: Annotated[int, Query(
        alias="object", ge=0, le=MAX_OBJECT_ID, description="MotionObject.id in the run's result"
    )],
    response: Response,
    size_deg: Annotated[float, Query(
        ge=MIN_SIZE_DEG, le=MAX_SIZE_DEG, description="Side of the analysed field, deg"
    )] = DEFAULT_SIZE_DEG,
) -> MotionEvidence:
    """Images of one object's patch of sky in every pass, and its measured positions.

    Takes the same ``ra``, ``dec`` and ``size_deg`` as the finished ``/api/motion`` run
    the object came from. 404 when that run's data is no longer kept: request
    ``/api/motion`` again until it is done, then retry.
    """
    run = runs.finished_run(ra, dec, size_deg)
    if run is None:
        # Start the run again when its engine data was dropped, so the client's next
        # /api/motion poll follows a fresh run instead of the kept summary.
        try:
            runs.request_job(ra, dec, size_deg, need_engine=True)
        except runs.MotionBusy:
            pass  # the client's /api/motion request reports the 503 itself
        raise HTTPException(
            404, "no finished motion run is kept for this field; request /api/motion for it again, then retry"
        )
    if object_id >= len(run.tracked):
        raise HTTPException(404, f"the run of this field has no object {object_id} (it tracked {len(run.tracked)})")
    response.headers["Cache-Control"] = CACHE_CONTROL
    return proof.evidence_for(run, object_id)


MIN_SURVEY_ARCSEC = 30.0
MAX_SURVEY_ARCSEC = 900.0  # 15': Barnard's Star's 2MASS position is ~4.5' from its SPHEREx one
# Archive images do not change; a failure is not cached by browsers (the server retries after a while).
SURVEY_CACHE_CONTROL = "public, max-age=86400"


@router.get("/evidence/surveys")
def survey_images(
    ra: Ra,
    dec: Dec,
    response: Response,
    side_arcsec: Annotated[float, Query(
        ge=MIN_SURVEY_ARCSEC, le=MAX_SURVEY_ARCSEC, description="Side of the square patch, arcsec"
    )],
) -> SurveyImages:
    """2MASS and AllWISE images of a square patch, centred and oriented as the evidence images.

    Independent of any motion run. Always 200: a survey that cannot give an image
    (service down, no coverage) is listed in ``unavailable`` with the reason.
    """
    images, gaps = [], []
    for got, survey in zip(older.patch_images(ra, dec, side_arcsec), older.SURVEYS, strict=True):
        if isinstance(got, str):
            gaps.append(SurveyGap(survey=survey.key, label=survey.label, reason=got))
            continue
        n = got.data.shape[0]
        images.append(SurveyImage(
            survey=survey.key, label=survey.label, mjd=got.mjd, date=archive.mjd_to_date(got.mjd),
            patch_px=n, pixel_arcsec=got.pixel_arcsec,
            image_png_b64=base64.b64encode(cutouts.render_png(got.data)).decode("ascii"),
        ))
    images.sort(key=lambda image: image.mjd)
    if not gaps:
        response.headers["Cache-Control"] = SURVEY_CACHE_CONTROL
    return SurveyImages(ra=ra, dec=dec, patch_arcsec=side_arcsec, images=images, unavailable=gaps)
