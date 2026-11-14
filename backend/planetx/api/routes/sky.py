"""SPHEREx data endpoints: visits, aligned frames and cutouts for a sky position.

Handlers are plain ``def`` (run in the threadpool) because they block on IRSA
and disk I/O. The data logic lives in planetx.spherex.archive and planetx.spherex.cutouts.
"""
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Response

from planetx.spherex import archive, cutouts
from planetx.api.schemas import CutoutData, Frame, FramesResponse, Visit, VisitsResponse

router = APIRouter(tags=["sky"])

DEFAULT_RADIUS_ARCSEC = 10.0
MAX_RADIUS_ARCSEC = 300.0  # larger cones are slow at IRSA
DEFAULT_SIZE_DEG = 0.1
MIN_SIZE_DEG = 0.01
MAX_SIZE_DEG = 0.5

Ra = Annotated[float, Query(ge=0, lt=360, description="Right ascension, deg (ICRS)")]
Dec = Annotated[float, Query(ge=-90, le=90, description="Declination, deg (ICRS)")]
SizeDeg = Annotated[float, Query(
    ge=MIN_SIZE_DEG, le=MAX_SIZE_DEG, description="Side of the square output grid, deg"
)]
Level = Annotated[float | None, Query(
    allow_inf_nan=False, description="Black (vmin) / white (vmax) level, data units"
)]
FileUrl = Annotated[str, Query(
    max_length=512, description="A SPHEREx file access_url from /api/visits"
)]


# IRSA statuses that mean "this file has no cutout at this position" (422: position
# outside the file), i.e. a problem with the request rather than an outage.
_NO_CUTOUT_STATUS = {400, 404, 422}


def _upstream_failure(exc: archive.UpstreamError) -> HTTPException:
    if isinstance(exc, archive.IrsaBusy):
        return HTTPException(503, str(exc), headers={"Retry-After": "5"})
    return HTTPException(502, f"IRSA request failed: {exc}")


def _visits(ra: float, dec: float, radius_arcsec: float) -> list[Visit]:
    try:
        return archive.query_visits(ra, dec, radius_arcsec)
    except archive.UpstreamError as exc:
        raise _upstream_failure(exc) from exc


def _aligned(url: str, ra: float, dec: float, size_deg: float) -> cutouts.AlignedCutout:
    try:
        archive.validate_spherex_url(url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        return cutouts.aligned_cutout(url, ra, dec, size_deg)
    except archive.UpstreamError as exc:
        if exc.status in _NO_CUTOUT_STATUS and not isinstance(exc, archive.IrsaBusy):
            raise HTTPException(404, f"No cutout for this file and position: {exc}") from exc
        raise _upstream_failure(exc) from exc


@router.get("/visits")
def visits(
    ra: Ra,
    dec: Dec,
    radius_arcsec: Annotated[float, Query(ge=0.1, le=MAX_RADIUS_ARCSEC)] = DEFAULT_RADIUS_ARCSEC,
) -> VisitsResponse:
    """SPHEREx exposures (spherex_dr1 + spherex_qr3) covering a cone, grouped into passes."""
    found = _visits(ra, dec, radius_arcsec)
    return VisitsResponse(
        ra=ra, dec=dec, radius_arcsec=radius_arcsec, n_visits=len(found),
        passes=archive.group_passes(found), visits=found,
    )


@router.get("/frames")
def frames(
    ra: Ra,
    dec: Dec,
    size_deg: SizeDeg = DEFAULT_SIZE_DEG,
    detector: Annotated[int | None, Query(ge=1, le=6, description="Only this detector")] = None,
) -> FramesResponse:
    """Visits covering (ra, dec) with URLs of their cutouts on one common aligned grid.

    ``wavelength_um`` is null here: SIA only gives each detector's whole band;
    the band at the target comes with the cutout (CutoutData.wavelength_um).
    """
    out = []
    for v in _visits(ra, dec, DEFAULT_RADIUS_ARCSEC):
        if detector is not None and v.detector != detector:
            continue
        query = urlencode({"url": v.access_url, "ra": ra, "dec": dec, "size_deg": size_deg})
        out.append(Frame(
            obs_id=v.obs_id, collection=v.collection, detector=v.detector, mjd=v.mjd,
            date=v.date, image_url=f"/api/cutout.png?{query}", data_url=f"/api/cutout.json?{query}",
        ))
    return FramesResponse(ra=ra, dec=dec, size_deg=size_deg, frames=out)


@router.get("/cutout.png", response_class=Response, responses={200: {"content": {"image/png": {}}}})
def cutout_png(
    url: FileUrl,
    ra: Ra,
    dec: Dec,
    size_deg: SizeDeg = DEFAULT_SIZE_DEG,
    vmin: Level = None,
    vmax: Level = None,
) -> Response:
    """8-bit grayscale PNG of one aligned frame (asinh stretch; no-data pixels transparent)."""
    if vmin is not None and vmax is not None and vmax <= vmin:
        raise HTTPException(400, "vmax must be greater than vmin")
    frame = _aligned(url, ra, dec, size_deg)
    return Response(
        cutouts.render_png(frame.data, vmin, vmax), media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/cutout.json")
def cutout_json(url: FileUrl, ra: Ra, dec: Dec, size_deg: SizeDeg = DEFAULT_SIZE_DEG) -> CutoutData:
    """Float32 pixels of one aligned frame for client-side blink/stack/difference."""
    frame = _aligned(url, ra, dec, size_deg)
    height, width = frame.data.shape
    return CutoutData(
        width=width, height=height, ra=ra, dec=dec,
        pixel_scale_arcsec=cutouts.PIXEL_SCALE_ARCSEC, mjd=frame.mjd,
        wavelength_um=frame.wavelength_um, bunit=frame.bunit,
        data_b64=cutouts.encode_float32(frame.data),
    )
