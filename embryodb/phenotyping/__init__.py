"""LineagePhenotyping bridge: dataset resolution + file freeze.

embryoDB owns dataset resolution and the per-embryo CSV freeze (the legacy
``LineagePhenotyping/GetFiles.pl`` role) plus the ready-to-run R config. The
numeric extraction (``build_inputs.R``) and the 8-step analysis
(``run_pipeline.R``) live in the LineagePhenotyping repo so they run
standalone from a freeze, with no embryoDB install required.
"""

from __future__ import annotations

from .freeze import (
    REFERENCE_SERIES,
    FreezeReport,
    SeriesFreeze,
    freeze_dataset,
)

__all__ = [
    "REFERENCE_SERIES",
    "FreezeReport",
    "SeriesFreeze",
    "freeze_dataset",
]
