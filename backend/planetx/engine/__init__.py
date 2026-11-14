"""Motion engine: SPHEREx frames of one patch in, movers and variables out.

Pure computation: no network access. Build `Frame`s with `frame_from_hdul`
and call `find_motion`.
"""
from .detect import Detection, detect_sources
from .frames import Frame, frame_from_hdul
from .motion import (
    EngineResult,
    PassSource,
    TrackedObject,
    find_motion,
    fit_motion,
    group_into_passes,
)

__all__ = [
    "Detection",
    "EngineResult",
    "Frame",
    "PassSource",
    "TrackedObject",
    "detect_sources",
    "find_motion",
    "fit_motion",
    "frame_from_hdul",
    "group_into_passes",
]
