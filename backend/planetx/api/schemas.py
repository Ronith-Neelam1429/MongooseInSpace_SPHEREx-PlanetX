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


class TrackPoint(BaseModel):
    """Where one object was seen in one survey pass (its frames averaged)."""

    ra: float
    dec: float
    mjd: float
    date: str
    sigma_arcsec: float = Field(description="Position error of this pass, per axis")
    n_frames: int = Field(description="Frames of the pass that detected the object")


class Variability(BaseModel):
    """Brightness change between two passes, from frames at matching wavelengths."""

    band: str = Field(description='Detector and measurement kind, e.g. "D3" or "D3-wing"')
    ratio: float = Field(description="Brighter over fainter pass, >= 1")
    significance: float
    change: str = Field(description='Later pass relative to earlier, e.g. "P3/P1 = 0.56"')
    caution: str | None = Field(None, description="Set when a nearby very bright star may cause it")


class ArchiveCheck(BaseModel):
    """The SPHEREx track refitted together with archival positions (2MASS, AllWISE, CatWISE)."""

    verdict: str = Field(description="confirmed | stationary | refuted | unconfirmed | spherex_only")
    pm_ra_cosdec: float | None = None
    pm_dec: float | None = None
    pm_err: float | None = None
    baseline_yr: float | None = None
    observatories: list[str] = Field(description="Observatories whose positions entered the fit")
    candidate: bool = Field(
        default=False,
        description="Archive positions from one survey family alone show a significant motion: a candidate, not confirmed",
    )
    notes: list[str] = Field(
        default_factory=list,
        description="Why the refit reached its verdict (e.g. which archival matches were likely chance alignments)",
    )


class KnownMatch(BaseModel):
    """A catalogued object that an engine object was matched to."""

    catalog: str = Field(description="gaia | simbad | skybot")
    name: str = Field(description='Designation, e.g. "Gaia DR3 4472832130942575872", "Barnard\'s Star", "(433) Eros"')
    kind: str | None = Field(None, description='Object type in plain words, e.g. "High proper-motion star", "Asteroid"')
    sep_arcsec: float = Field(description="Distance from the engine position, at the SPHEREx epoch")
    pm_ra_cosdec: float | None = Field(None, description="Catalogued proper motion, arcsec/yr (to compare with ours)")
    pm_dec: float | None = None
    magnitude: float | None = Field(None, description="Catalogue magnitude, if any")


class MotionObject(BaseModel):
    """One object the engine tracked across passes.

    `kind` is the motion class: "mover" (significant motion, or motion confirmed
    by archival positions), "candidate" (3-5 sigma in SPHEREx alone and the
    archive cannot settle it) or "static". Motions are SPHEREx-only measurements
    in arcsec/yr, east and north positive; `archive` carries the multi-epoch refit.
    """

    id: int = Field(0, description="Index in MotionResult.objects")
    kind: str
    is_variable: bool
    ra: float = Field(description="Position at ref_mjd")
    dec: float
    ref_mjd: float
    pm_ra_cosdec: float
    pm_dec: float
    pm_total: float
    pm_err: float = Field(description="Includes the field's systematic floor")
    significance: float = Field(description="pm_total / pm_err")
    position_angle_deg: float = Field(description="Direction of motion, east of north")
    parallax_arcsec: float | None = Field(None, description="Only when it explains a poor straight-line fit")
    parallax_err_arcsec: float | None = None
    saturated: bool = Field(description="Positions come from a saturated star's masked core")
    track: list[TrackPoint] = Field(description="One point per pass, time-ordered")
    variability: Variability | None = Field(None, description="Set only when is_variable")
    archive: ArchiveCheck
    name: str | None = Field(None, description="Best catalogue name, when the object matches a known one")
    classification: str | None = Field(
        None,
        description="known_star | known_asteroid | variable_star | artifact | unknown_candidate | static; "
        "null when the catalogue check did not run",
    )
    matches: list[KnownMatch] = Field(default_factory=list, description="Catalogue matches, best first")


class MotionPass(BaseModel):
    start: str
    end: str
    n_frames: int


class MotionResult(BaseModel):
    n_frames: int = Field(description="Frames analysed (a sample of the visits, not all of them)")
    passes: list[MotionPass]
    detectors: list[int]
    pm_floor_arcsec_yr: float = Field(description="Systematic motion floor measured from the static stars")
    objects: list[MotionObject] = Field(description="Movers first, then candidates, then the rest")
    notes: list[str] = Field(description="Caveats from the engine and the archival check")


class MotionResponse(BaseModel):
    """State of a motion-engine run. Poll until `status` is no longer "running"."""

    ra: float = Field(description="Centre actually analysed (the request snapped to 0.001 deg)")
    dec: float
    size_deg: float
    status: str = Field(description="running | done | failed")
    stage: str = Field(description="queued | listing | downloading | analysing | checking")
    frames_done: int
    frames_total: int = Field(description="0 until the visits are listed")
    error: str | None = None
    result: MotionResult | None = None


class EvidenceFlux(BaseModel):
    """Brightness of the object in one pass and detector: the median over that pass's frames."""

    band: str = Field(description='Detector and measurement kind, e.g. "D3", or "D3-wing" for a saturated star')
    flux: float = Field(description="Only comparable with the same band in other passes")
    flux_err: float


class EvidencePoint(BaseModel):
    """Where the object was measured in one pass, as offsets from the patch centre."""

    east_arcsec: float = Field(description="East is positive (to the left in the images)")
    north_arcsec: float = Field(description="North is positive (up in the images)")
    sigma_arcsec: float = Field(description="Position error of this pass, per axis")
    mjd: float
    date: str
    n_frames: int = Field(description="Frames of the pass that detected the object")
    fluxes: list[EvidenceFlux]


class FieldScatter(BaseModel):
    """How far the field's other tracked objects appear to shift between the object's first
    pass and this one. Nearly all of them are fixed stars, so this is the measurement noise."""

    sigma_arcsec: float = Field(description="Per axis: 1.4826 x the median absolute shift")
    median_shift_arcsec: float = Field(description="Median total shift on the sky")
    n_stars: int


class EvidenceExposure(BaseModel):
    """One single SPHEREx exposure of the object's spot, as it came off the detector
    (background removed, aligned, no combining). Same centre, orientation and grey scale
    as the pass images; `MotionEvidence.exposure_px` pixels on a side."""

    mjd: float
    time: str = Field(description="UTC, YYYY-MM-DD HH:MM")
    detector: int
    wavelength_um: float | None = Field(None, description="Central wavelength of the exposure at the object")
    image_png_b64: str = Field(description="Base64 PNG, grey + alpha")
    point: EvidencePoint | None = Field(
        None, description="The engine's detection of the object in this exposure (fluxes empty, n_frames 1); "
        "null when this exposure did not detect it"
    )


class EvidencePass(BaseModel):
    """One survey pass: the image of the patch and the object's measurement in it."""

    index: int = Field(description="Pass number in the run, 0-based, time-ordered")
    start: str = Field(description="Date of the first exposure, YYYY-MM-DD")
    end: str
    n_frames: int = Field(description="Exposures combined into the image")
    image_png_b64: str = Field(description="Base64 PNG, grey + alpha; transparent where there is no data")
    coverage: float = Field(description="Fraction of the image that has data (below 1 at the edge of the field)")
    point: EvidencePoint | None = Field(None, description="Null when the object was not measured in this pass")
    field_scatter: FieldScatter | None = Field(
        None, description="Null for the object's first pass, and when too few other objects were seen in both passes"
    )
    n_cover: int = Field(0, description="Exposures of this pass that cover the object's position")
    exposures: list[EvidenceExposure] = Field(
        default_factory=list,
        description="A few single exposures spread over the pass, time-ordered; empty when the engine "
        "analysed per-pass stacks",
    )


class EvidenceCheck(BaseModel):
    """One independent test of whether the motion is real and belongs to one object."""

    key: str = Field(description="track_line | beyond_noise | images_show_motion | same_look | "
                     "detectors_agree | many_exposures | no_stand_in")
    title: str = Field(description="What the test asks, in plain words")
    status: str = Field(description="pass | weak | fail | unknown (the data cannot tell)")
    summary: str = Field(description="The result in one or two plain sentences, with its numbers")
    details: list[str] = Field(default_factory=list, description="Supporting numbers, one line each")
    value: float | None = Field(default=None, description="The test's main number (e.g. a significance), when it has one")


class EvidenceFit(BaseModel):
    """The engine's straight-line fit of the object's positions, as offsets from the patch centre."""

    ref_mjd: float
    east_arcsec: float = Field(description="Fitted position at ref_mjd")
    north_arcsec: float
    pm_east: float = Field(description="arcsec/yr, as MotionObject.pm_ra_cosdec")
    pm_north: float


class MotionEvidence(BaseModel):
    """What the engine saw of one object: the same patch of sky in every pass, and its positions.

    All images share one grid, grey scale and orientation: north up, east left, centred on
    the object's first measured position (`patch_ra`, `patch_dec`). The pixel at image column
    `(patch_px - 1) / 2 - east_arcsec / patch_pixel_arcsec` and row
    `(patch_px - 1) / 2 - north_arcsec / patch_pixel_arcsec` (0-based, row 0 at the top) is
    the sky at that offset.
    """

    object: int = Field(description="MotionObject.id in the run")
    ra: float = Field(description="The object's fitted position, as MotionObject.ra: tells which object this is")
    dec: float
    patch_ra: float
    patch_dec: float
    patch_arcsec: float = Field(description="Side of the square patch")
    patch_px: int = Field(description="Side of every image, pixels")
    patch_pixel_arcsec: float = Field(description="Image pixel size: half a SPHEREx pixel")
    spherex_pixel_arcsec: float
    passes: list[EvidencePass] = Field(description="Every pass of the run, time-ordered")
    difference_png_b64: str = Field(
        description="Last minus first pass in which the object was measured: mid-grey is no change, "
        "lighter is brighter in the last pass"
    )
    difference_passes: tuple[int, int] = Field(description="Pass indices (first, last) of the difference image")
    fit: EvidenceFit
    pm_floor_arcsec_yr: float = Field(description="As MotionResult.pm_floor_arcsec_yr")
    exposure_arcsec: float = Field(0.0, description="Side of the single-exposure images (same centre as the patch)")
    exposure_px: int = Field(0, description="Side of every single-exposure image, pixels")
    checks: list[EvidenceCheck] = Field(
        default_factory=list, description="Independent tests that the motion is real and is one object"
    )
    missing_passes: list[int] = Field(
        default_factory=list,
        description="Passes whose frames covered the fitted track but found nothing there: the engine then "
        "takes the positions for different objects, so the object is not a mover whatever its fitted motion",
    )


class SurveyImage(BaseModel):
    """An older survey's image of a square patch: same orientation and centre as MotionEvidence's.

    North up, east left; the image's centre pixel is the patch centre, and an offset
    (east, north) in arcsec lies at column `(patch_px - 1) / 2 - east / pixel_arcsec`,
    row `(patch_px - 1) / 2 - north / pixel_arcsec`.
    """

    survey: str = Field(description='"2mass" | "wise"')
    label: str = Field(description='e.g. "2MASS Ks"')
    mjd: float = Field(description="When the image was taken (AllWISE: the mean of its exposures)")
    date: str
    patch_px: int
    pixel_arcsec: float
    image_png_b64: str = Field(description="Grey + alpha PNG; transparent where the survey has no data")


class SurveyGap(BaseModel):
    survey: str
    label: str
    reason: str = Field(description="Why there is no image, for people")


class SurveyImages(BaseModel):
    ra: float = Field(description="Patch centre")
    dec: float
    patch_arcsec: float
    images: list[SurveyImage] = Field(description="Oldest first")
    unavailable: list[SurveyGap]


class SurveyCounts(BaseModel):
    pending: int = 0
    in_progress: int = 0
    done: int = 0
    failed: int = 0


class SurveyStatus(BaseModel):
    """Progress of the offline survey crawler (all zero before the queue exists)."""

    nside: int | None = Field(default=None, description="HEALPix nside of the patches; null before init")
    moc_order: int | None = Field(default=None, description="HEALPix order = log2(nside)")
    total: int = Field(description="Number of patches in the queue")
    counts: SurveyCounts
    current_rank: int | None = Field(
        default=None, description="Crawl rank of the lowest patch not yet done or failed; null when finished"
    )
    current_ra: float | None = Field(default=None, description="Centre RA of the patch at current_rank")
    current_dec: float | None = Field(default=None, description="Centre Dec of the patch at current_rank")
    current_radius_deg: float | None = Field(
        default=None, description="Distance of that patch from the North Ecliptic Pole (how far out the crawl is)"
    )
    readable: bool = Field(
        default=True,
        description="False if the survey database exists but could not be opened (counts are then zero)",
    )


# Survey patches of one status (done by default) as an Aladin Lite MOC JSON:
# {"<order>": [NESTED ipix, ...]}, with order = log2(nside). Pass straight to
# `A.MOCFromJSON(coverage)`. The list is empty while no patch has that status
# (skip drawing then).
SurveyCoverage = dict[str, list[int]]


class SurveyCandidate(BaseModel):
    """A notable object the offline survey stored: a mover, motion candidate or variable."""

    id: int = Field(description="Row id; changes when the survey re-processes the patch")
    ra: float = Field(description="Fitted position (deg, ICRS) at the mean date of the SPHEREx passes")
    dec: float
    name: str | None = Field(description="Catalogue name (Gaia/SIMBAD/SkyBoT), null when uncatalogued")
    classification: str | None = Field(
        description="known_star | known_asteroid | variable_star | artifact | unknown_candidate | static;"
        " null when the catalogue check could not run"
    )
    kind: str = Field(description="Engine motion class: mover | candidate (3-5 sigma) | static (a variable)")
    is_variable: bool
    pm_total: float = Field(description="SPHEREx proper motion, arcsec/yr")
    pm_ra_cosdec: float = Field(description="arcsec/yr, east positive")
    pm_dec: float = Field(description="arcsec/yr, north positive")
    significance: float = Field(description="pm_total / its error (sigma)")
    position_angle_deg: float = Field(description="Direction of motion, degrees east of north")
    score: float = Field(ge=0, le=1, description="How interesting, 0-1 (see docs/API.md)")
    nside: int = Field(description="HEALPix nside of the survey patch it was found in")
    ipix: int = Field(description="NESTED index of that patch")
    found: str = Field(description="UTC date (YYYY-MM-DD) of the run that stored it")
    field_ra: float = Field(
        description="Centre of the field it was measured in; `/api/motion` there with size_deg=0.2"
        " repeats the survey's run (a full run: up to ~54 cutouts of ~4 MB unless cached)"
    )
    field_dec: float
    ref_mjd: float = Field(description="MJD (UTC) at which ra, dec hold (the SPHEREx fit's reference date)")


class SurveyCandidates(BaseModel):
    total: int = Field(description="Candidates matching the filter (all pages)")
    candidates: list[SurveyCandidate] = Field(description="This page, best score first")


class SpectrumPoint(BaseModel):
    """Forced photometry of the object in one SPHEREx exposure."""

    detector: int
    mjd: float
    date: str
    wavelength_um: float = Field(description="Band centre at the object's pixel (WCS-WAVE)")
    bandwidth_um: float | None
    flux_mjy: float = Field(description="Total flux density F_nu, aperture corrected")
    flux_err_mjy: float
    band: int | None = Field(description="Index into the 102-band grid, None if outside its detector's range")
    outlier: bool = Field(description="Dropped from its band by the robust combination")


class SpectrumBand(BaseModel):
    """One of SPHEREx's 102 bands (6 detectors x 17), combined over exposures."""

    band: int = Field(description="0-101, shortest wavelength first")
    detector: int
    wavelength_um: float = Field(description="Band centre (geometric mean of the edges)")
    lo_um: float
    hi_um: float
    flux_mjy: float
    flux_err_mjy: float
    n_used: int
    n_rejected: int


class SpectrumFeature(BaseModel):
    """Depth of one absorption band against a simple continuum (see planetx.spherex.spectrum)."""

    key: str = Field(description="h2o_1.4 | ch4_1.6 | h2o_1.9 | ch4_2.2 | h2o_2.7 | ch4_3.3")
    molecule: str = Field(description="CH4 | H2O")
    label: str
    lo_um: float
    hi_um: float
    depth: float | None = Field(description="1 - F(window)/F(continuum): 0 none, 1 fully absorbed")
    depth_err: float | None
    significance: float | None = Field(description="depth / depth_err")
    continuum: str | None = Field(description='"two windows" or "Rayleigh-Jeans from one window"')
    detected: bool | None = Field(description="depth >= 0.15 at >= 3 sigma; None when not measurable")


class SpectrumCounts(BaseModel):
    """What happened to the exposures tried, by outcome."""

    ok: int = 0
    saturated: int = 0
    masked: int = 0
    off_image: int = 0
    no_data: int = 0
    failed: int = 0


class SpectrumResult(BaseModel):
    points: list[SpectrumPoint] = Field(description="One per measured exposure, by wavelength")
    bands: list[SpectrumBand] = Field(description="Bands with data, by wavelength")
    features: list[SpectrumFeature]
    suggestion: str = Field(description="brown_dwarf | cool_object | methane_only | none | unknown | saturated")
    summary: str = Field(description='Plain sentence; says "suggests", never "is"')
    saturated: bool = Field(description="Most exposures were rejected as saturated at the object")
    counts: SpectrumCounts
    aperture_arcsec: float
    notes: list[str]


class SpectrumResponse(BaseModel):
    """State of a spectrum run. Poll until `status` is no longer "running"."""

    ra: float
    dec: float
    pmra: float = Field(description="mas/yr (includes cos dec)")
    pmdec: float
    epoch: float = Field(description="Julian year of ra, dec")
    status: str = Field(description="running | done | failed")
    stage: str = Field(description="queued | listing | measuring")
    frames_done: int
    frames_total: int
    error: str | None = None
    result: SpectrumResult | None = None
