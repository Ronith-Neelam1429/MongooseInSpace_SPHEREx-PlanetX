"""A single SPHEREx exposure as the engine sees it.

The engine never downloads anything: callers hand it `Frame`s built from
SPHEREx spectral-image MEFs (full files or IRSA cutouts).
"""
from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field

import numpy as np
from astropy.io import fits

from astropy.wcs import WCS, FITSFixedWarning
from astropy.wcs.utils import proj_plane_pixel_scales

# Mask planes that make a pixel unusable for photometry/centroiding. Names come
# from the MSKNnnnn keywords in the FLAGS header, so bit numbers are never
# hard-coded. Deliberately *not* here: SOURCE (every star sets it), TRANSIENT
# and SUR_ERROR (set on the cores of bright stars), NONLINEAR, GHOST, DICHROIC.
BAD_FLAG_NAMES = frozenset({"OVERFLOW", "BLOOM", "NONFUNC", "MISSING_DATA", "HOT", "COLD"})
# Planes marking a saturated star. Its core is unusable, but the masked patch
# itself is centred on the star, so detection uses its shape as a position.
SATURATION_FLAG_NAMES = frozenset({"OVERFLOW", "BLOOM"})
# Planes where extra light from elsewhere (ghosts and halos of bright stars,
# crosstalk, persistence, streaks) biases brightness. Positions there are kept,
# but such detections never take part in brightness comparisons.
CONTAMINATION_FLAG_NAMES = frozenset(
    {"GHOST", "GHOST_EXT", "GHOST_FPA", "HALO", "CROSSTALK", "PERSIST", "STREAK", "SNOWBALL"}
)


@dataclass
class Frame:
    """One exposure: calibrated image, per-pixel variance and usability mask."""

    data: np.ndarray  # surface brightness (MJy/sr); NaN = unusable
    variance: np.ndarray  # same shape; NaN where unknown
    wcs: WCS  # celestial WCS of `data`
    mjd: float  # mid-exposure time
    detector: int  # SPHEREx detector array 1-6
    obs_id: str = ""
    wavelength_um: float = float("nan")  # central wavelength at the cutout centre
    psf_fwhm_arcsec: float = float("nan")
    saturated: np.ndarray | None = None  # bool; pixels of saturated stars
    contaminated: np.ndarray | None = None  # bool; extra light biases photometry
    meta: dict = field(default_factory=dict)

    @property
    def pixel_scale_arcsec(self) -> float:
        # Plain floats in the WCS's units (deg for SPHEREx).
        return float(np.mean(proj_plane_pixel_scales(self.wcs)) * 3600)


def flag_bits(header: fits.Header) -> dict[str, int]:
    """Map mask-plane name -> bitmask from MSKN/MSKM header keywords."""
    bits: dict[str, int] = {}
    for key in header:
        if key.startswith("MSKN"):
            n = key[4:]
            mask_key = f"MSKM{n}"
            if mask_key in header:
                bits[str(header[key]).strip()] = int(header[mask_key])
    return bits


def flag_mask(flags: np.ndarray, header: fits.Header, names) -> np.ndarray:
    """Boolean mask of pixels with any of the named mask planes set."""
    bits = flag_bits(header)
    want = 0
    for name in names:
        want |= bits.get(name, 0)
    return (flags.astype(np.int64) & want) != 0


def bad_pixel_mask(flags: np.ndarray, header: fits.Header) -> np.ndarray:
    """Boolean mask of pixels to ignore, from the FLAGS plane and its header."""
    return flag_mask(flags, header, BAD_FLAG_NAMES)


def celestial_header(header: fits.Header) -> fits.Header:
    """SPHEREx headers carry SIP distortion terms without the '-SIP' CTYPE
    suffix. astropy applies them anyway but logs a long INFO per frame; say
    explicitly that they apply."""
    h = header.copy()
    if "A_ORDER" in h:
        for key in ("CTYPE1", "CTYPE2"):
            if key in h and not str(h[key]).endswith("-SIP"):
                h[key] = str(h[key]) + "-SIP"
    return h


_SIP_CARD = re.compile(r"(A|B|AP|BP)_(ORDER|\d+_\d+)")


def _wavelength_wcs(header: fits.Header, hdul: fits.HDUList) -> WCS:
    """The per-pixel (wavelength, bandwidth) lookup WCS (alternate key W).

    Its WCS-WAVE table is indexed by raw pixel coordinates, but SIP cards
    carry no alternate key, so astropy would apply the celestial distortion
    to it too (shifting the looked-up wavelength by up to ~0.8%). They are
    dropped first, as in `planetx.spherex.cutouts`.
    """
    header = header.copy()
    for key in [k for k in header if _SIP_CARD.fullmatch(k)]:
        del header[key]
    return WCS(header, fobj=hdul, key="W")


def frame_from_hdul(hdul: fits.HDUList, obs_id: str = "") -> Frame:
    """Build a Frame from an opened SPHEREx spectral-image MEF (or cutout)."""
    image_hdu = hdul["IMAGE"]
    header = image_hdu.header
    data = np.array(image_hdu.data, dtype=np.float64)
    variance = np.array(hdul["VARIANCE"].data, dtype=np.float64)
    flags, flags_header = hdul["FLAGS"].data, hdul["FLAGS"].header
    mask = bad_pixel_mask(flags, flags_header)
    saturated = flag_mask(flags, flags_header, SATURATION_FLAG_NAMES)
    contaminated = flag_mask(flags, flags_header, CONTAMINATION_FLAG_NAMES)
    data[mask] = np.nan
    variance[mask] = np.nan
    variance[~(variance > 0)] = np.nan

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        wcs = WCS(celestial_header(header)).celestial
        wavelength = float("nan")
        if "WCS-WAVE" in hdul:
            try:
                wave_wcs = _wavelength_wcs(header, hdul)
                ny, nx = data.shape
                wavelength = float(wave_wcs.all_pix2world((nx - 1) / 2, (ny - 1) / 2, 0)[0])
            except Exception as exc:  # wavelength is metadata; don't lose the frame
                warnings.warn(f"could not evaluate WCS-WAVE: {exc}")

    if "MJD-AVG" not in header:
        raise ValueError("IMAGE header has no MJD-AVG; cannot time-stamp frame")
    return Frame(
        data=data,
        variance=variance,
        wcs=wcs,
        mjd=float(header["MJD-AVG"]),
        detector=int(header.get("DETECTOR", 0)),
        obs_id=obs_id,
        wavelength_um=wavelength,
        psf_fwhm_arcsec=float(header.get("PSF_FWHM", float("nan"))),
        saturated=saturated,
        contaminated=contaminated,
    )
