"""LIF (Leica Image File) ingestion.

Reads `.lif` files via :mod:`readlif`, slices per-position metadata into the
on-disk ``Position N_Properties.xml`` shape (so the existing
:func:`embryodb.parsers.leica_metadata.parse_properties_xml` consumes it
unchanged), and writes per-plane LZW-compressed TIFs straight into the
staged image_loc layout.

The pipeline orchestrator runs unchanged: ``stage_images`` no-ops on the
already-staged TIFs via its existing ``if dst.exists()`` idempotency
(see :mod:`embryodb.pipeline.stage`).

See the master plan at
``~/.claude/plans/take-a-look-at-keen-milner.md`` for context, the
direct-to-target vs two-step trade-off, and the long-term direction
(multichannel TIFFs).
"""

from .metadata import slice_position_xml

__all__ = [
    "slice_position_xml",
]
# The extractor module's symbols are re-exported below once extractor.py
# lands; until then we keep this lean so partial-implementation states
# don't break `embryodb.lif` imports for the metadata path.
try:
    from .extractor import (  # noqa: F401
        ExtractionPlan,
        ExtractionReport,
        LifExtractor,
        LifSeriesInfo,
        inspect_lif,
    )

    __all__ += [
        "ExtractionPlan",
        "ExtractionReport",
        "LifExtractor",
        "LifSeriesInfo",
        "inspect_lif",
    ]
except ImportError:
    pass
