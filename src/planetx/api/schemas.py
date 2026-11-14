"""API contract: response models shared by the backend and the frontend.

The frontend codes against these shapes (also served as OpenAPI at /docs), so
change them deliberately: add fields freely, but rename or remove only together
with the frontend.

Conventions:
  * Angles in degrees (ICRS) unless the name says otherwise (`_arcsec`, `_um`).
  * Times as MJD (float) plus a human `date` (UTC, YYYY-MM-DD).
  * Wavelengths in microns.
"""
from pydantic import BaseModel, Field


class Health(BaseModel):
    status: str = "ok"


class Visit(BaseModel):
    """One SPHEREx exposure (one detector) that covers the requested position."""

    obs_id: str
    collection: str = Field(description="IRSA collection, e.g. spherex_dr1 or spherex_qr3")
    detector: int | None = Field(None, description="SPHEREx detector 1-6, if known")
    mjd: float = Field(description="Exposure mid-time (MJD, UTC)")
    date: str = Field(description="Exposure date, UTC, YYYY-MM-DD")
    wavelength_min_um: float | None = None
    wavelength_max_um: float | None = None
    access_url: str = Field(description="Full-file URL at IRSA; cutouts are made from it")


class Pass(BaseModel):
    """A survey pass: visits grouped in time (a new pass starts after a >30 day gap)."""

    start: str = Field(description="First date in the pass, YYYY-MM-DD")
    end: str = Field(description="Last date in the pass, YYYY-MM-DD")
    n_visits: int


class VisitsResponse(BaseModel):
    ra: float
    dec: float
    radius_arcsec: float
    n_visits: int
    passes: list[Pass]
    visits: list[Visit] = Field(description="Sorted by mjd ascending")


class Frame(BaseModel):
    """A visit plus ready-to-load cutout URLs for the time-lapse player.

    All frames in one FramesResponse are resampled onto the same north-up grid
    (centre ra/dec, side size_deg), so the player can blink/difference them
    pixel for pixel.
    """

    obs_id: str
    collection: str
    detector: int | None = None
    mjd: float
    date: str
    wavelength_um: float | None = Field(None, description="Band centre at the target position")
    image_url: str = Field(description="Relative URL of an 8-bit PNG preview of this frame")
    data_url: str = Field(description="Relative URL of the float pixel data (CutoutData JSON)")


class FramesResponse(BaseModel):
    ra: float
    dec: float
    size_deg: float
    frames: list[Frame] = Field(description="Sorted by mjd ascending")


class CutoutData(BaseModel):
    """Float pixel data of one aligned cutout, for client-side blink/stack/difference.

    `data_b64` is base64 of little-endian float32, row-major, `height` rows of
    `width` values. Row 0 is the TOP (north) row and column 0 is the LEFT (east)
    edge, matching canvas/image orientation. NaN marks no data or masked pixels.
    """

    width: int
    height: int
    ra: float = Field(description="Centre RA of the grid")
    dec: float = Field(description="Centre Dec of the grid")
    pixel_scale_arcsec: float
    mjd: float
    wavelength_um: float | None = None
    bunit: str = Field(description="Pixel unit, e.g. MJy/sr")
    data_b64: str
