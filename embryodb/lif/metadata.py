"""Slice and transform LIF-embedded XML into Stellaris-shaped Properties.xml.

The LIF's embedded XML (``LifFile.xml_root``) carries the same per-image
acquisition metadata that LAS X writes out as ``<series>_Position N_Properties.xml``
on TIF export, but with a few small structural differences:

- ``DimensionDescription/@DimID`` uses ``"1" "2" "3" "4"`` (LIF) instead of
  ``"X" "Y" "Z" "T"`` (export).
- ``DimensionDescription`` carries ``Length`` + ``NumberOfElements`` rather
  than a ``Voxel`` attribute (the export pre-computes Length/N → Voxel).
- ``<NumberOfChannels>`` element is absent — the channel count must be
  inferred from the ``<ChannelDescription>`` count.

We compensate for those during slicing so the downstream
:func:`embryodb.parsers.leica_metadata.parse_properties_xml` and the
Stellaris timestamp parser (which keys on ``ATL/@Sections`` ×
``<NumberOfChannels>``) consume the result without modification.

LIF tree shape (verified on a real Stellaris LIF):

    LMSDataContainerHeader
      Element Name="Project"
        Data, Attributes, Memory
        Children
          Element Name="TileScan 1"         ← series 1
            Data, Memory, Children
              Element Name="Position 1"     ← one per position
                Data/Image                  ← the Stellaris-style metadata
              Element Name="Position 2"
              ...
          Element Name="TileScan 2"         ← series 2
          ...
"""

from __future__ import annotations

import copy
from xml.etree import ElementTree as ET


# LIF's numeric DimID → the letter DimID our parser expects. Indexed by
# the string the LIF stores so the lookup is keyless-on-missing-safe.
_DIM_MAP: dict[str, str] = {"1": "X", "2": "Y", "3": "Z", "4": "T"}


def _project_element(xml_root: ET.Element) -> ET.Element:
    """Return the top-level ``<Element Name="Project">`` node."""
    proj = xml_root.find("Element")
    if proj is None:
        raise ValueError("LIF xml_root has no Project element")
    return proj


def _series_elements(xml_root: ET.Element) -> list[ET.Element]:
    """Return the top-level series Elements (one per ``TileScan N``)."""
    proj = _project_element(xml_root)
    children = proj.find("Children")
    if children is None:
        return []
    return list(children.findall("Element"))


def _position_elements(series_el: ET.Element) -> list[ET.Element]:
    """Return the Position Elements under a series.

    Each per-position Element wraps ``Data/Image`` with the metadata block.
    Other children (Data without Image, Memory, etc.) are filtered out.
    """
    children = series_el.find("Children")
    if children is None:
        return []
    out: list[ET.Element] = []
    for el in children.findall("Element"):
        # Only return entries that have an Image subnode — Position
        # elements always do; other Element children may not.
        if el.find("Data/Image") is not None:
            out.append(el)
    return out


def find_series(xml_root: ET.Element, name: str) -> ET.Element | None:
    """Look up a series Element by ``Name``. Returns None when missing."""
    for el in _series_elements(xml_root):
        if el.get("Name") == name:
            return el
    return None


def find_position(series_el: ET.Element, name: str) -> ET.Element | None:
    """Look up a position Element by ``Name`` within a series."""
    for el in _position_elements(series_el):
        if el.get("Name") == name:
            return el
    return None


def slice_position_xml(
    xml_root: ET.Element,
    series_name: str,
    position_name: str,
) -> str:
    """Extract one position's metadata and shape it into a Properties.xml string.

    Returns the serialized XML body (caller writes via ``safe_write_text``).
    The returned envelope is ``<Data><Image>…</Image></Data>`` which matches
    the on-disk Stellaris export and is what
    :func:`embryodb.parsers.leica_metadata.parse_properties_xml` consumes.

    Three transformations are applied before serialization:

    1. ``DimensionDescription/@DimID`` numeric → letter (``"1"`` → ``"X"``).
    2. ``DimensionDescription/@Voxel`` computed from ``Length / N`` when
       absent (Length is in meters; we convert to µm so the parser's
       existing ``_num`` unit-strip stays valid).
    3. ``<NumberOfChannels>`` injected under ``<ImageDescription>`` from
       the count of ``<ChannelDescription>`` entries. Required for the
       Stellaris timestamp parser to decimate ``<TimeStampList>`` by
       ``planes × channels``.
    """
    series = find_series(xml_root, series_name)
    if series is None:
        raise ValueError(f"series {series_name!r} not found in LIF")
    pos = find_position(series, position_name)
    if pos is None:
        raise ValueError(
            f"position {position_name!r} not found in series {series_name!r}"
        )

    image = pos.find("Data/Image")
    if image is None:
        raise ValueError(
            f"position {position_name!r} has no Data/Image subtree"
        )

    # Deep-copy so we can mutate without disturbing the LifFile's cached tree.
    image_copy = copy.deepcopy(image)

    _normalize_dimensions(image_copy)
    _inject_number_of_channels(image_copy)

    envelope = ET.Element("Data")
    envelope.append(image_copy)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        + ET.tostring(envelope, encoding="unicode")
    )


def _normalize_dimensions(image: ET.Element) -> None:
    """In-place: rewrite each ``DimensionDescription`` so the parser sees it.

    - ``DimID`` numeric → letter (``"1"`` → ``"X"`` etc.).
    - ``Voxel`` computed as ``Length / NumberOfElements`` when absent.
      For X/Y/Z dims the LIF stores Length in metres; we report µm to
      match the convention LAS X uses on export. T dim has no consistent
      unit (Length is duration in seconds for Stellaris but blank-unit);
      we still compute a Voxel for it so the field is non-empty, but the
      parser doesn't read T voxel anyway.
    """
    for dd in image.findall(".//DimensionDescription"):
        dim_id = dd.get("DimID") or ""
        if dim_id in _DIM_MAP:
            dd.set("DimID", _DIM_MAP[dim_id])

        if dd.get("Voxel"):
            continue  # already populated; respect what's there
        length_str = dd.get("Length")
        n_str = dd.get("NumberOfElements")
        if not (length_str and n_str):
            continue
        try:
            length = float(length_str)
            n = int(n_str)
        except ValueError:
            continue
        if n <= 0:
            continue
        unit = (dd.get("Unit") or "").strip()
        # LIF stores X/Y/Z lengths in metres; convert to µm so the parser
        # gets the same numeric scale as a LAS X-exported file (where Voxel
        # is reported as a unitless number whose context is µm).
        if unit == "m":
            voxel = (length / n) * 1e6
        else:
            voxel = length / n
        # Preserve at least 6 significant digits — voxel sizes are ~0.087
        # for XY and ~0.5 for Z, so 6 digits is plenty.
        dd.set("Voxel", f"{voxel:.10g}")


def _inject_number_of_channels(image: ET.Element) -> None:
    """In-place: add ``<NumberOfChannels>`` under ``<ImageDescription>``.

    The LIF doesn't carry it explicitly; the Stellaris timestamp parser
    needs it to decimate ``<TimeStampList>`` (one FILETIME per acquired
    plane → one per volume by ``planes × channels`` stride).
    """
    image_desc = image.find("ImageDescription")
    if image_desc is None:
        return
    if image_desc.find("NumberOfChannels") is not None:
        return  # already present, defensive
    channels_el = image_desc.find("Channels")
    if channels_el is None:
        return
    n_ch = len(channels_el.findall("ChannelDescription"))
    if n_ch <= 0:
        return
    nch = ET.Element("NumberOfChannels")
    nch.text = str(n_ch)
    image_desc.append(nch)


__all__ = [
    "find_position",
    "find_series",
    "slice_position_xml",
]
