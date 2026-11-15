"""Which observatories the motion engine may use, and what an archival position is.

SPHEREx is always on. The others are older NASA all-sky infrared surveys whose
catalogs give the same sources' positions 10-27 years earlier, which is what
turns a noisy 1-2 year SPHEREx motion into a well-measured one.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from planetx.config import settings


class Observatory(StrEnum):
    SPHEREX = "spherex"
    TWOMASS = "2mass"  # 2MASS Point Source Catalog, 1997-2001, J/H/Ks
    ALLWISE = "allwise"  # WISE cryogenic + post-cryo, 2010-2011, 3.4/4.6 um
    CATWISE = "catwise"  # CatWISE2020 (WISE + NEOWISE 2010-2018), position at 2015.4
    NEOWISE = "neowise"  # NEOWISE-R single exposures, 2013-2024 (slow: opt-in)


SPHEREX_ONLY: frozenset[Observatory] = frozenset({Observatory.SPHEREX})
# NEOWISE single exposures are left out by default: near the ecliptic poles a
# 20" cone returns ~45,000 rows and takes about a minute.
DEFAULT_MULTI: frozenset[Observatory] = frozenset(
    {Observatory.SPHEREX, Observatory.TWOMASS, Observatory.ALLWISE, Observatory.CATWISE}
)
ALL: frozenset[Observatory] = frozenset(Observatory)

_PRESETS = {
    "spherex": SPHEREX_ONLY,
    "spherex-only": SPHEREX_ONLY,
    "multi": DEFAULT_MULTI,
    "all": ALL,
}

# 2MASS is independent of the WISE family; AllWISE, CatWISE and NEOWISE share
# a telescope and (partly) the same exposures.
_WISE_FAMILY = frozenset({Observatory.ALLWISE, Observatory.CATWISE, Observatory.NEOWISE})


def family(o: Observatory) -> str:
    """Name of the group of observatories whose detections are not independent of `o`'s."""
    return "wise" if o in _WISE_FAMILY else o.value


def parse_observatories(value: str | Iterable[str] | None = None) -> frozenset[Observatory]:
    """Turn a toggle value into the set of observatories to use.

    Accepts a preset ("spherex" = SPHEREx only, "multi" = SPHEREx + 2MASS +
    AllWISE + CatWISE, "all" = multi + NEOWISE single exposures), a
    comma-separated list ("2mass,catwise") or an iterable of names. `None`
    reads the PLANETX_OBSERVATORIES setting. SPHEREx is always included.
    """
    if value is None:
        value = settings.observatories
    names = value.split(",") if isinstance(value, str) else list(value)
    names = [str(n).strip().lower() for n in names if str(n).strip()]
    if not names:
        raise ValueError("no observatories given; use 'spherex' for SPHEREx only")
    chosen: set[Observatory] = {Observatory.SPHEREX}
    for name in names:
        if name in _PRESETS:
            chosen |= _PRESETS[name]
            continue
        try:
            chosen.add(Observatory(name))
        except ValueError:
            valid = sorted(_PRESETS) + sorted(o.value for o in Observatory)
            raise ValueError(
                f"unknown observatory {name!r}; valid values: {', '.join(dict.fromkeys(valid))}"
            ) from None
    return frozenset(chosen)


@dataclass(frozen=True)
class ArchivalSource:
    """One catalog position of one source at one past date."""

    observatory: Observatory
    ra: float  # degrees, ICRS
    dec: float
    mjd: float  # date the position refers to
    sigma_arcsec: float  # 1-sigma positional error per axis
    mag: float = float("nan")  # Vega magnitude in `band`
    band: str = ""
    name: str = ""
    # Sources with the same (observatory, visit) are alternatives for ONE epoch
    # of an object; different visits of one observatory are separate epochs.
    visit: int = 0
    # The catalog's own proper motion (arcsec/yr, east and north; error = the larger per-axis
    # error), where it measures one (CatWISE2020 does, from 2010-2018 WISE images); else NaN.
    pm_ra_cosdec: float = float("nan")
    pm_dec: float = float("nan")
    pm_err: float = float("nan")
