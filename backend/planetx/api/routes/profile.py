"""Object profile endpoint: size, distance, temperature, path, why it moves, what pulls on it.

A plain ``def`` handler (runs in the threadpool): it blocks on Gaia, VizieR and SIMBAD
for up to ~30 s on a first request; repeats are served from memory for an hour.
"""
from typing import Annotated

from fastapi import APIRouter, Query

from planetx.api.routes.sky import Dec, Ra
from planetx.api.schemas import ObjectProfile
from planetx.profile import service

router = APIRouter(tags=["profile"])

MAX_PM_MAS = 20000.0  # Barnard's Star is 10,400 mas/yr

ProperMotion = Annotated[float | None, Query(
    ge=-MAX_PM_MAS, le=MAX_PM_MAS, allow_inf_nan=False,
    description="Measured proper motion, mas/yr (the RA component includes cos dec)",
)]


@router.get("/object/profile", response_model_by_alias=True)
def object_profile(
    ra: Ra,
    dec: Dec,
    gaia_id: Annotated[str | None, Query(
        pattern=r"^[0-9]{1,19}$", description="Gaia DR3 source_id, when already known",
    )] = None,
    name: Annotated[str | None, Query(
        min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$",
        description="SIMBAD name, when known (e.g. \"Barnard's Star\")",
    )] = None,
    pmra: ProperMotion = None,
    pmdec: ProperMotion = None,
    pm_err: Annotated[float | None, Query(
        ge=0, le=MAX_PM_MAS, allow_inf_nan=False, description="Error of the measured motion, mas/yr",
    )] = None,
    mag: Annotated[float | None, Query(
        ge=-30, le=40, allow_inf_nan=False, description="Brightness the page knows (for estimates only)",
    )] = None,
) -> ObjectProfile:
    """Everything the catalogues say about one object, with plain-language explanations.

    Any part may be null; a catalogue that fails or answers late becomes a sentence in
    `notes`, never an error status.
    """
    return service.get_profile(service.ProfileRequest(
        ra=ra, dec=dec, gaia_id=gaia_id, name=name.strip() if name and name.strip() else None,
        pmra=pmra, pmdec=pmdec, pm_err=pm_err, mag=mag,
    ))
