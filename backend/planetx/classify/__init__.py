"""What the tracked objects are: catalogue check and classification.

    from planetx.classify import check_objects
    checked = check_objects(engine_result.objects, fusion.fused, pixel_arcsec)

`check_objects` fetches Gaia DR3 and SIMBAD once for the field, settles which
objects really move (`Known.kind`), and names and classifies each one. The rules
are pure functions in `rules.py`; the fetchers and their parsers are in
`catalogs.py`.
"""
from .catalogs import CatalogError, CatalogObject, SolarSystemBody
from .check import CheckResult, Known, check_objects, motion_kind, unchecked
from .rules import CLASSIFICATIONS, FieldCatalog, Match, TrackCheck

__all__ = [
    "CLASSIFICATIONS",
    "CatalogError",
    "CatalogObject",
    "CheckResult",
    "FieldCatalog",
    "Known",
    "Match",
    "SolarSystemBody",
    "TrackCheck",
    "check_objects",
    "motion_kind",
    "unchecked",
]
