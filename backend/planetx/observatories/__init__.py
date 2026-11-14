"""Other observatories' data, combined with SPHEREx to measure motion better.

    from planetx.observatories import refine_objects
    result = refine_objects(engine_result.objects, observatories="multi")   # or "spherex"

`observatories="spherex"` is the SPHEREx-only switch; see `parse_observatories`.
"""
from .fusion import (
    FusedMotion,
    FusionParams,
    FusionResult,
    Verdict,
    fuse_track,
    refine_objects,
)
from .catalogs import ArchiveError, fetch_catalog
from .sources import (
    ALL,
    DEFAULT_MULTI,
    SPHEREX_ONLY,
    ArchivalSource,
    Observatory,
    parse_observatories,
)

__all__ = [
    "ALL",
    "DEFAULT_MULTI",
    "SPHEREX_ONLY",
    "ArchivalSource",
    "ArchiveError",
    "FusedMotion",
    "FusionParams",
    "FusionResult",
    "Observatory",
    "Verdict",
    "fetch_catalog",
    "fuse_track",
    "parse_observatories",
    "refine_objects",
]
