"""HEALPix sky patches and the order the survey crawls them in.

Patches are HEALPix pixels in NESTED ordering, so any set of patch indices at
one nside is also a valid MOC (order = log2(nside)) that Aladin Lite can draw.

The crawl starts at the North Ecliptic Pole, where SPHEREx revisits the sky
most often, and moves outward: patches are sorted by the angular distance of
their centre from the NEP, ties broken by ecliptic longitude.
"""
import math

import astropy.units as u
import numpy as np
from astropy.coordinates import ICRS, BarycentricMeanEcliptic, SkyCoord
from astropy_healpix import HEALPix
from numpy.typing import ArrayLike

# nside=64: 49,152 patches of ~0.92 deg (about 530 SPHEREx pixels across).
DEFAULT_NSIDE = 64
# Upper bound for nside: 1024 is already 12.6M patches (~1 GB of queue rows).
MAX_NSIDE = 1024

# The J2000 North Ecliptic Pole (ecliptic latitude +90): ICRS RA 270.0, Dec +66.56.
NEP = SkyCoord(lon=0 * u.deg, lat=90 * u.deg, frame=BarycentricMeanEcliptic()).icrs

# Distances equal to within this many degrees count as ties (broken by longitude).
_TIE_DEG = 1e-9


def validate_nside(nside: int) -> int:
    """Return `nside` if it is a power of two in [1, MAX_NSIDE], else raise ValueError."""
    if isinstance(nside, bool) or not isinstance(nside, int):
        raise ValueError(f"nside must be an int, got {nside!r}")
    if not 1 <= nside <= MAX_NSIDE or nside & (nside - 1):
        raise ValueError(f"nside must be a power of two between 1 and {MAX_NSIDE}, got {nside}")
    return nside


def moc_order(nside: int) -> int:
    """HEALPix order (MOC depth) for `nside`: nside = 2**order."""
    return int(math.log2(validate_nside(nside)))


def n_patches(nside: int) -> int:
    """Total number of patches covering the sky at `nside`."""
    return 12 * validate_nside(nside) ** 2


def _healpix(nside: int) -> HEALPix:
    return HEALPix(nside=validate_nside(nside), order="nested", frame=ICRS())


def _validate_ipix(ipix: int, nside: int) -> int:
    if isinstance(ipix, bool) or not isinstance(ipix, (int, np.integer)):
        raise ValueError(f"ipix must be an int, got {ipix!r}")
    if not 0 <= ipix < n_patches(nside):
        raise ValueError(f"ipix {ipix} out of range for nside {nside}")
    return int(ipix)


def patch_containing(ra: float, dec: float, nside: int = DEFAULT_NSIDE) -> int:
    """NESTED index of the patch that contains the ICRS position (degrees)."""
    if not (math.isfinite(ra) and math.isfinite(dec) and -90 <= dec <= 90):
        raise ValueError(f"invalid position ra={ra}, dec={dec}")
    coord = SkyCoord(ra * u.deg, dec * u.deg, frame="icrs")
    return int(_healpix(nside).skycoord_to_healpix(coord))


def patches_containing(ra: ArrayLike, dec: ArrayLike, nside: int = DEFAULT_NSIDE) -> np.ndarray:
    """NESTED index of the patch containing each ICRS position (degree arrays)."""
    ra, dec = np.asarray(ra, dtype=float), np.asarray(dec, dtype=float)
    if not (np.all(np.isfinite(ra)) and np.all(np.isfinite(dec)) and np.all(np.abs(dec) <= 90)):
        raise ValueError("invalid positions (non-finite, or |dec| > 90)")
    return np.asarray(_healpix(nside).lonlat_to_healpix(ra * u.deg, dec * u.deg), dtype=np.int64)


def children(ipix: int, nside: int, child_nside: int) -> np.ndarray:
    """NESTED indices at `child_nside` of the pixels that tile patch `ipix` (ascending).

    In NESTED ordering the children of pixel p at k times the nside are the
    k**2 consecutive indices from p * k**2.
    """
    ipix = _validate_ipix(ipix, nside)
    validate_nside(child_nside)
    if child_nside < nside:
        raise ValueError(f"child_nside {child_nside} is coarser than nside {nside}")
    n = (child_nside // nside) ** 2
    return np.arange(ipix * n, (ipix + 1) * n, dtype=np.int64)


def centers(ipix: ArrayLike, nside: int = DEFAULT_NSIDE) -> tuple[np.ndarray, np.ndarray]:
    """ICRS (ra, dec) arrays in degrees of the centres of patches `ipix`."""
    lon, lat = _healpix(nside).healpix_to_lonlat(np.asarray(ipix))
    return lon.to_value(u.deg), lat.to_value(u.deg)


def patch_center(ipix: int, nside: int = DEFAULT_NSIDE) -> tuple[float, float]:
    """ICRS (ra, dec) in degrees of the centre of patch `ipix`."""
    coord = _healpix(nside).healpix_to_skycoord(_validate_ipix(ipix, nside))
    return float(coord.ra.deg), float(coord.dec.deg)


def patch_corners(ipix: int, nside: int = DEFAULT_NSIDE) -> list[tuple[float, float]]:
    """ICRS (ra, dec) in degrees of the 4 corners of patch `ipix`, in boundary order.

    Consecutive corners are joined by a patch edge, so the list can be drawn
    directly as a closed polygon.
    """
    corners = _healpix(nside).boundaries_skycoord(np.array([_validate_ipix(ipix, nside)]), step=1)[0]
    return [(float(c.ra.deg), float(c.dec.deg)) for c in corners]


def nep_distance(ipix: ArrayLike, nside: int = DEFAULT_NSIDE) -> np.ndarray:
    """Angular distance (degrees) of each patch centre from the NEP."""
    centres = _healpix(nside).healpix_to_skycoord(np.asarray(ipix))
    return centres.separation(NEP).deg


def crawl_order(nside: int = DEFAULT_NSIDE) -> np.ndarray:
    """All NESTED patch indices at `nside`, in crawl order (index in the array = rank).

    Sorted by centre distance from the NEP, then ecliptic longitude, so the
    crawl sweeps around the pole ring by ring. HEALPix pixels are not Voronoi
    cells, so the patch with the nearest centre need not contain the NEP; the
    patch that does is forced to rank 0.
    """
    ipix = np.arange(n_patches(nside))
    ecl = _healpix(nside).healpix_to_skycoord(ipix).transform_to(BarycentricMeanEcliptic())
    # Distance from the ecliptic pole is the ecliptic colatitude.
    dist = 90.0 - ecl.lat.deg
    order = np.lexsort((ecl.lon.deg, np.round(dist / _TIE_DEG)))
    first = patch_containing(NEP.ra.deg, NEP.dec.deg, nside)
    return np.concatenate(([first], order[order != first])).astype(np.int64)
