"""Unified entry point for microscopy-metadata parsing.

Dispatches to the right vendor-specific parser based on the file's name:

- ``*_Position N_Properties.xml`` → Leica Stellaris (modern format)
- ``omxml.xml`` / ``omxml.xml.gz``  → OME-XML (legacy SP5 era; any vendor
  whose Bio-Formats export lands as ``omxml.xml(.gz)``)

All parsers return a :class:`embryodb.parsers.leica_metadata.LeicaMetadata`
so callers (orchestrator, backfill CLI) don't need to special-case the
vendor — they just persist the same JSON shape.

If a new vendor format shows up:

1. Add a parser module that produces a ``LeicaMetadata``.
2. Add a ``can_parse(path)`` helper (cheap probe).
3. Register both in :data:`METADATA_PARSERS` below.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .leica_metadata import LeicaMetadata, parse_properties_xml
from .ome_xml import (
    find_image_list,
    load_image_list,
    parse_ome_xml_as_metadata,
)


@dataclass
class _MetadataParser:
    name: str
    can_parse: Callable[[Path], bool]
    # Adapter signature: (path, dats_dir_hint) -> LeicaMetadata. The hint
    # is used by parsers that need a companion file (e.g. SP5's
    # imageList.tsv to pick the right Image element); other parsers ignore it.
    parse: Callable[[Path, Path | None], LeicaMetadata]


def _can_stellaris(path: Path) -> bool:
    return (
        path.is_file()
        and "_Position " in path.name
        and path.name.endswith("_Properties.xml")
    )


def _parse_stellaris(path: Path, dats_hint: Path | None) -> LeicaMetadata:
    return parse_properties_xml(path)


# 2011 LASAF SP5: per-tp files in info/ sibling of dats/.
# The schema is largely a subset of modern Stellaris — same
# ATLConfocalSettingDefinition, AotfList, DetectorList — so reusing
# parse_properties_xml is the right call. We just need a different
# file-detection rule and to know the parent dir is `info/`.
import re as _re  # local alias so we don't expose `re` from this module
_LASAF_TP_RE = _re.compile(r"_t(\d+)_Properties\.xml$")


def _can_lasaf_sp5(path: Path) -> bool:
    return (
        path.is_file()
        and path.parent.name == "info"
        and _LASAF_TP_RE.search(path.name) is not None
    )


def _parse_lasaf_sp5(path: Path, dats_hint: Path | None) -> LeicaMetadata:
    # The format reuses Stellaris's ATLConfocalSettingDefinition / DetectorList
    # nearly verbatim; the existing parser handles it with the detector-name
    # / channel-name fallbacks added in v2.8.
    return parse_properties_xml(path)


def _can_ome_xml(path: Path) -> bool:
    return path.is_file() and path.name in ("omxml.xml", "omxml.xml.gz")


def _parse_ome_xml(path: Path, dats_hint: Path | None) -> LeicaMetadata:
    # Forward the full L imageList so the OME-XML parser can:
    # 1. Pick the L's first timepoint Image as the representative (rather
    #    than the multi-position file's first non-DriftAF entry, which
    #    might belong to a different L).
    # 2. Walk every timepoint to build the time-indexed compensation
    #    curve (`depth_compensation.axis == "timepoint"`) capturing any
    #    ramped laser intensity / detector gain across the acquisition.
    dats = dats_hint or path.parent
    ilist_path = find_image_list(dats)
    image_list: dict[int, str] | None = None
    if ilist_path is not None:
        image_list = load_image_list(ilist_path) or None
    return parse_ome_xml_as_metadata(path, image_list=image_list)


METADATA_PARSERS: list[_MetadataParser] = [
    _MetadataParser(
        name="leica_stellaris_properties",
        can_parse=_can_stellaris,
        parse=_parse_stellaris,
    ),
    _MetadataParser(
        name="leica_sp5_ome_xml",
        can_parse=_can_ome_xml,
        parse=_parse_ome_xml,
    ),
    _MetadataParser(
        name="leica_lasaf_sp5_info",
        can_parse=_can_lasaf_sp5,
        parse=_parse_lasaf_sp5,
    ),
]


def parse_microscopy(
    path: Path, *, dats_dir: Path | None = None
) -> LeicaMetadata | None:
    """Parse a microscopy-metadata file. Returns None if no parser claims it.

    Args:
      path: the metadata file (e.g. Properties.xml or omxml.xml.gz).
      dats_dir: optional override for the directory used to look up
        companion files (currently SP5's imageList.tsv). Defaults to
        ``path.parent``.
    """
    for parser in METADATA_PARSERS:
        if parser.can_parse(path):
            return parser.parse(path, dats_dir)
    return None


def find_metadata_file(annot_loc: Path, position: int) -> Path | None:
    """Locate the best microscopy-metadata file for a series.

    Preference order (most-specific to most-general):

    1. ``<annot_loc>/dats/*_Position {position}_Properties.xml`` (Stellaris)
    2. ``<annot_loc>/*_Position {position}_Properties.xml`` (rare legacy)
    3. ``<annot_loc>/dats/omxml.xml.gz`` or ``omxml.xml`` (SP5 OME export)
    4. ``<annot_loc>/omxml.xml.gz`` or ``omxml.xml`` (rare legacy)
    5. ``<annot_loc>/info/*_t001_Properties.xml`` (2011 LASAF SP5)
    6. ``<annot_loc>/info/*_t<smallest N>_Properties.xml`` (LASAF SP5
       with non-001 first tp)

    Returns the first hit on disk, or None if nothing matches.
    """
    for parent in (annot_loc / "dats", annot_loc):
        if not parent.is_dir():
            continue
        for entry in sorted(parent.iterdir()):
            if (
                entry.is_file()
                and entry.name.endswith(f"_Position {position}_Properties.xml")
            ):
                return entry
    # Fall back to OME-XML for legacy SP5.
    for parent in (annot_loc / "dats", annot_loc):
        if not parent.is_dir():
            continue
        for fname in ("omxml.xml.gz", "omxml.xml"):
            cand = parent / fname
            if cand.is_file():
                return cand
    # Fall back to 2011 LASAF per-tp Properties.xml in info/.
    info_dir = annot_loc / "info"
    if info_dir.is_dir():
        per_tp: list[tuple[int, Path]] = []
        for entry in info_dir.iterdir():
            if not entry.is_file():
                continue
            m = _LASAF_TP_RE.search(entry.name)
            if m is not None:
                per_tp.append((int(m.group(1)), entry))
        if per_tp:
            # Use the smallest-tp file as the representative — this is the
            # acquisition's first timepoint, so it carries the as-set scope
            # configuration before any compensation kicked in.
            per_tp.sort(key=lambda x: x[0])
            return per_tp[0][1]
    return None


__all__ = [
    "METADATA_PARSERS",
    "find_metadata_file",
    "parse_microscopy",
]
