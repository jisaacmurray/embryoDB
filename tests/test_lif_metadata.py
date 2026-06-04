"""Tests for the LIF→Properties.xml metadata slicer.

The slicer (:func:`embryodb.lif.metadata.slice_position_xml`) takes the
LIF's embedded XML tree and emits a per-position ``Properties.xml`` body
shaped like a LAS X TIF export, so the existing
:func:`embryodb.parsers.leica_metadata.parse_properties_xml` consumes it
unchanged. Three transforms are exercised:

1. ``DimensionDescription/@DimID`` numeric ("1".."4") → letter (X/Y/Z/T).
2. ``@Voxel`` computed from ``Length / NumberOfElements`` (metres → µm).
3. ``<NumberOfChannels>`` injected from the ``<ChannelDescription>`` count.

Plus a full round-trip: slice → write → ``parse_properties_xml`` →
assert the voxel sizes / plane / timepoint counts survive.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from embryodb.lif.metadata import (
    find_position,
    find_series,
    slice_position_xml,
)
from embryodb.parsers.leica_metadata import parse_properties_xml


# Real fixture values from the 131 GB JIM593 LIF.
VOXEL_XY_UM = 0.0865
VOXEL_Z_UM = 0.5002
X_PIXELS = 512
Y_PIXELS = 512
N_PLANES = 67
N_TIMEPOINTS = 240


def _lif_xml(
    *,
    series_names: tuple[str, ...] = ("TileScan 1", "TileScan 2"),
    position_names: tuple[str, ...] = ("Position 1", "Position 2"),
    n_channels: int = 2,
) -> ET.Element:
    """Build a minimal LIF-shaped xml_root.

    Mirrors the nested ``LMSDataContainerHeader > Project > Children >
    TileScan N > Children > Position N > Data/Image`` shape that
    :mod:`embryodb.lif.metadata` walks. Dimensions use LIF conventions:
    numeric ``DimID`` and ``Length`` (metres) + ``NumberOfElements`` rather
    than a precomputed ``Voxel``.
    """
    root = ET.Element("LMSDataContainerHeader")
    project = ET.SubElement(root, "Element", Name="Project")
    proj_children = ET.SubElement(project, "Children")

    for sname in series_names:
        series = ET.SubElement(proj_children, "Element", Name=sname)
        series_children = ET.SubElement(series, "Children")
        for pname in position_names:
            pos = ET.SubElement(series_children, "Element", Name=pname)
            data = ET.SubElement(pos, "Data")
            image = ET.SubElement(data, "Image")
            image_desc = ET.SubElement(image, "ImageDescription")

            dims = ET.SubElement(image_desc, "Dimensions")
            # X (DimID 1), Y (2), Z (3), T (4). Length in metres so the
            # slicer's m→µm conversion is exercised.
            for dim_id, n, voxel_um in (
                ("1", X_PIXELS, VOXEL_XY_UM),
                ("2", Y_PIXELS, VOXEL_XY_UM),
                ("3", N_PLANES, VOXEL_Z_UM),
                ("4", N_TIMEPOINTS, 0.0),  # T: Length irrelevant to the parser
            ):
                length_m = (voxel_um * 1e-6) * n
                ET.SubElement(
                    dims,
                    "DimensionDescription",
                    DimID=dim_id,
                    NumberOfElements=str(n),
                    Length=repr(length_m),
                    Unit="m",
                )

            channels = ET.SubElement(image_desc, "Channels")
            for ci in range(n_channels):
                ET.SubElement(
                    channels,
                    "ChannelDescription",
                    ChannelTag=str(ci),
                    LUTName=("Green", "Red")[ci % 2],
                    DataType="0",
                    Resolution="8",
                )
    return root


# ---------------------------------------------------------------------------
# Tree navigation
# ---------------------------------------------------------------------------


def test_find_series_and_position():
    root = _lif_xml()
    assert find_series(root, "TileScan 2") is not None
    assert find_series(root, "TileScan 99") is None
    series = find_series(root, "TileScan 1")
    assert find_position(series, "Position 2") is not None
    assert find_position(series, "Position 99") is None


def test_slice_missing_series_raises():
    root = _lif_xml()
    with pytest.raises(ValueError, match="series 'Nope' not found"):
        slice_position_xml(root, "Nope", "Position 1")


def test_slice_missing_position_raises():
    root = _lif_xml()
    with pytest.raises(ValueError, match="position 'Nope' not found"):
        slice_position_xml(root, "TileScan 1", "Nope")


# ---------------------------------------------------------------------------
# The three transforms
# ---------------------------------------------------------------------------


def test_dim_ids_rewritten_to_letters():
    root = _lif_xml()
    body = slice_position_xml(root, "TileScan 2", "Position 1")
    sliced = ET.fromstring(body)
    dim_ids = {dd.get("DimID") for dd in sliced.iter("DimensionDescription")}
    assert dim_ids == {"X", "Y", "Z", "T"}


def test_voxel_computed_from_length_in_um():
    root = _lif_xml()
    body = slice_position_xml(root, "TileScan 2", "Position 1")
    sliced = ET.fromstring(body)
    voxels = {
        dd.get("DimID"): float(dd.get("Voxel"))
        for dd in sliced.iter("DimensionDescription")
    }
    assert voxels["X"] == pytest.approx(VOXEL_XY_UM, rel=1e-6)
    assert voxels["Y"] == pytest.approx(VOXEL_XY_UM, rel=1e-6)
    assert voxels["Z"] == pytest.approx(VOXEL_Z_UM, rel=1e-6)


def test_existing_voxel_is_respected():
    """A DimensionDescription that already carries @Voxel is left alone."""
    root = _lif_xml()
    series = find_series(root, "TileScan 1")
    pos = find_position(series, "Position 1")
    xdim = pos.find(".//DimensionDescription")
    xdim.set("Voxel", "9.999")
    body = slice_position_xml(root, "TileScan 1", "Position 1")
    sliced = ET.fromstring(body)
    xout = next(
        dd for dd in sliced.iter("DimensionDescription") if dd.get("DimID") == "X"
    )
    assert xout.get("Voxel") == "9.999"


def test_number_of_channels_injected():
    root = _lif_xml(n_channels=3)
    body = slice_position_xml(root, "TileScan 1", "Position 1")
    sliced = ET.fromstring(body)
    nch = sliced.find(".//ImageDescription/NumberOfChannels")
    assert nch is not None
    assert nch.text == "3"


def test_envelope_is_data_image():
    root = _lif_xml()
    body = slice_position_xml(root, "TileScan 1", "Position 1")
    assert body.startswith("<?xml")
    sliced = ET.fromstring(body)
    assert sliced.tag == "Data"
    assert sliced.find("Image") is not None


def test_slice_does_not_mutate_source_tree():
    """Deep-copy guard: slicing must not rewrite the LifFile's cached tree."""
    root = _lif_xml()
    slice_position_xml(root, "TileScan 1", "Position 1")
    series = find_series(root, "TileScan 1")
    pos = find_position(series, "Position 1")
    # DimIDs in the source are still numeric (slicer worked on a copy).
    dim_ids = {dd.get("DimID") for dd in pos.iter("DimensionDescription")}
    assert dim_ids == {"1", "2", "3", "4"}


# ---------------------------------------------------------------------------
# Round-trip through the production parser
# ---------------------------------------------------------------------------


def test_round_trip_through_parse_properties_xml(tmp_path):
    root = _lif_xml()
    body = slice_position_xml(root, "TileScan 2", "Position 1")
    dst = tmp_path / "JIM593_Position 1_Properties.xml"
    dst.write_text(body, encoding="utf-8")

    md = parse_properties_xml(dst)
    assert md.voxel_xy_um == pytest.approx(VOXEL_XY_UM, rel=1e-6)
    assert md.voxel_z_um == pytest.approx(VOXEL_Z_UM, rel=1e-6)
    assert md.x_pixels == X_PIXELS
    assert md.y_pixels == Y_PIXELS
    assert md.planes_per_volume == N_PLANES
    assert md.n_timepoints == N_TIMEPOINTS
