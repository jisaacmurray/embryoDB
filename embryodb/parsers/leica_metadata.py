"""Parse Leica `Position N_Properties.xml` into MicroscopyMetadata fields.

The Leica Stellaris export writes a per-position `<acq>_Position N_Properties.xml`
that contains the rich acquisition metadata we want to capture in the new
schema: voxel sizes per dimension, objective, NA, channel descriptions,
pinhole, scan settings, stage position.

The XML is well-formed but verbose. We extract the few fields we care about
defensively — missing attributes are tolerated; we'd rather record a partial
metadata row than fail import on a vendor quirk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


_UNIT_NUM_RE = re.compile(r"^\s*(-?[\d\.eE+-]+)")


def _num(value: str | None) -> float | None:
    """Extract a leading number from values like '154.4 µm' or '8,000 Hz'."""
    if value is None:
        return None
    s = value.replace(",", "")
    m = _UNIT_NUM_RE.match(s)
    if m is None:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


@dataclass
class LeicaMetadata:
    """Subset of Properties.xml fields we map into MicroscopyMetadata."""

    objective: str = ""
    objective_NA: float | None = None
    magnification: float | None = None
    immersion: str = ""
    refractive_index: float | None = None

    voxel_xy_um: float | None = None
    voxel_z_um: float | None = None
    planes_per_volume: int | None = None
    n_timepoints: int | None = None
    x_pixels: int | None = None
    y_pixels: int | None = None
    cycle_time_s: float | None = None

    pinhole_um: float | None = None
    pinhole_airy: float | None = None
    scan_speed_hz: float | None = None
    line_averaging: int | None = None
    frame_averaging: int | None = None

    stage_x_um: float | None = None
    stage_y_um: float | None = None
    stage_z_um: float | None = None

    channels: list[dict[str, Any]] = field(default_factory=list)


def parse_properties_xml(path: Path) -> LeicaMetadata:
    """Best-effort parse of one Leica Properties.xml file."""
    tree = ET.parse(path)
    root = tree.getroot()
    out = LeicaMetadata()

    # --- DimensionDescription per axis (X, Y, Z, T) ----------------------
    for dim in root.iter("DimensionDescription"):
        dim_id = (dim.get("DimID") or "").upper()
        voxel = _num(dim.get("Voxel"))
        n = dim.get("NumberOfElements")
        n_int = int(n) if (n and n.isdigit()) else None
        if dim_id == "X":
            out.voxel_xy_um = voxel if voxel is not None else out.voxel_xy_um
            out.x_pixels = n_int
        elif dim_id == "Y":
            # Y voxel should match X; prefer X but fall back to Y if missing.
            if out.voxel_xy_um is None:
                out.voxel_xy_um = voxel
            out.y_pixels = n_int
        elif dim_id == "Z":
            out.voxel_z_um = voxel
            out.planes_per_volume = n_int
        elif dim_id == "T":
            out.n_timepoints = n_int
            out.cycle_time_s = _num(dim.get("Voxel"))

    # --- ATLConfocalSettingDefinition: objective + scan settings ---------
    # There can be several of these nested for sequential scans; the top-level
    # one carries the per-acquisition settings.
    setting = root.find(".//ATLConfocalSettingDefinition")
    if setting is not None:
        out.objective = setting.get("ObjectiveName", "").strip()
        out.objective_NA = _num(setting.get("NumericalAperture"))
        out.magnification = _num(setting.get("Magnification"))
        out.immersion = setting.get("Immersion", "").strip()
        out.refractive_index = _num(setting.get("RefractionIndex"))
        out.pinhole_um = _num(setting.get("Pinhole"))
        out.pinhole_airy = _num(setting.get("PinholeAiry"))
        out.scan_speed_hz = _num(setting.get("ScanSpeed"))
        out.line_averaging = (
            int(setting.get("LineAverage")) if setting.get("LineAverage", "").isdigit() else None
        )
        out.frame_averaging = (
            int(setting.get("FrameAverage")) if setting.get("FrameAverage", "").isdigit() else None
        )
        out.stage_x_um = _num(setting.get("StagePosX"))
        out.stage_y_um = _num(setting.get("StagePosY"))
        out.stage_z_um = _num(setting.get("ZPosition"))

    # --- ChannelDescription per imaging channel --------------------------
    for ch in root.iter("ChannelDescription"):
        out.channels.append(
            {
                "tag": ch.get("ChannelTag", ""),
                "lut_name": ch.get("LUTName", ""),
                "data_type": ch.get("DataType", ""),
                "min": ch.get("Min", ""),
                "max": ch.get("Max", ""),
                "unit": ch.get("Unit", ""),
                "name_of_measured_quantity": ch.get("NameOfMeasuredQuantity", ""),
                "resolution_bits": ch.get("Resolution", ""),
            }
        )

    return out


__all__ = ["LeicaMetadata", "parse_properties_xml"]
