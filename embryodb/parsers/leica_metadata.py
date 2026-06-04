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
    """Subset of Properties.xml fields we map into MicroscopyMetadata.

    Three buckets land here:

    - **Scalar scope/scan settings** (objective, voxel sizes, pinhole, scan
      speed, image geometry, programmed timing, system identity, …) — most
      become typed columns on ``MicroscopyMetadata`` directly. The newer
      fields (bit_size, pixel_dwell_time_us, zoom, scan geometry, z range,
      programmed timing, instrument identity) are stashed into the
      ``acquisition_settings`` JSON column rather than spawning a dozen new
      typed columns we'd rarely filter on.
    - **Per-channel info** (``channels``) — one entry per displayed channel.
      Each entry merges the ``ChannelDescription`` legacy fields (LUT, min,
      max, …) with the cross-referenced active ``Detector`` (gain, dye, band)
      and matching ``Laser`` / ``LaserLineSetting`` (line, intensity %).
    - **Depth-compensation curves** (``depth_compensation``) — one curve per
      active channel, projected from the global
      ``CompensationInterpolationPoints`` block: ``(z_um, intensity_dev, gain)``
      points where the intensity/gain are the ones for *that channel's* laser
      line and detector.
    """

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

    # Per-channel info — see _extract_active_channels for the merge logic.
    channels: list[dict[str, Any]] = field(default_factory=list)

    # New in Phase 2:
    acquisition_settings: dict[str, Any] = field(default_factory=dict)
    depth_compensation: dict[str, Any] = field(default_factory=dict)


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

    # --- Scalar acquisition_settings (items 4-10 from the audit) ---------
    out.acquisition_settings = _extract_acquisition_settings(root, setting)

    # --- Per-channel info (items 1+2): merge ChannelDescription +
    # active Detector + matching Laser/LaserLineSetting --------------------
    out.channels = _extract_active_channels(root, setting)

    # --- Depth compensation (item 3): per-channel projected curves -------
    out.depth_compensation = _extract_depth_compensation(root, setting, out.channels)

    return out


# ---------------------------------------------------------------------------
# Phase 2 helpers: per-channel laser/detector + depth compensation + scalars
# ---------------------------------------------------------------------------


# Best-effort mapping for Detector AcquisitionMode numerical codes. The
# Leica XML often leaves AcquisitionModeName blank; this dict translates the
# code into human-readable text for GUI display. Unknown codes fall through
# as a stringified integer.
_ACQ_MODE_NAMES: dict[str, str] = {
    "5": "Photon counting",
    "2": "Intensity",
    "0": "Intensity",
    "1": "Analog",
    "-1": "",
}


def _bool_attr(el, key: str) -> bool | None:
    """Parse a 0/1-valued attribute as bool. Returns None when absent."""
    v = el.get(key) if el is not None else None
    if v is None:
        return None
    s = v.strip()
    if s == "1":
        return True
    if s == "0":
        return False
    return None


def _int_attr(el, key: str) -> int | None:
    v = el.get(key) if el is not None else None
    if v is None:
        return None
    s = v.strip()
    return int(s) if s.lstrip("-").isdigit() else None


def _m_to_um(meters: float | None) -> float | None:
    """Convert Leica's raw meter values (e.g. ``Begin="-0.00018"``) to µm."""
    return None if meters is None else meters * 1e6


def _extract_acquisition_settings(root, setting) -> dict[str, Any]:
    """Scalar acquisition settings (items 4-10 from the audit).

    These don't justify dedicated columns on MicroscopyMetadata — they're
    rarely the basis for filtering and are easier to grow as a JSON blob.
    """
    out: dict[str, Any] = {}
    if setting is not None:
        out["bit_size"] = _int_attr(setting, "BitSize")
        out["pixel_dwell_time_us"] = _num(setting.get("PixelDwellTime"))
        out["zoom"] = _num(setting.get("Zoom"))
        out["base_zoom"] = _num(setting.get("BaseZoom"))
        out["scan_mode"] = (setting.get("ScanMode") or "").strip()
        out["is_resonant_scanner"] = _bool_attr(setting, "IsResonantScanner")
        out["scan_direction_x"] = (setting.get("ScanDirectionXName") or "").strip()
        out["flip_x"] = _bool_attr(setting, "FlipX")
        out["flip_y"] = _bool_attr(setting, "FlipY")
        out["swap_xy"] = _bool_attr(setting, "SwapXY")
        out["rotator_angle_deg"] = _num(setting.get("RotatorAngle"))
        out["z_begin_um"] = _m_to_um(_num(setting.get("Begin")))
        out["z_end_um"] = _m_to_um(_num(setting.get("End")))
        out["cycle_time_s"] = _num(setting.get("CycleTime"))
        out["complete_time_s"] = _num(setting.get("CompleteTime"))
        out["frame_time_s"] = _num(setting.get("FrameTime"))
        out["line_time_s"] = _num(setting.get("LineTime"))
        out["system_serial_number"] = (setting.get("SystemSerialNumber") or "").strip()
        out["microscope_model"] = (setting.get("MicroscopeModel") or "").strip()
        out["software_version"] = (setting.get("VersionNumber") or "").strip()
    # Drop None entries to keep the JSON compact; absent values don't need
    # to be persisted.
    return {k: v for k, v in out.items() if v not in (None, "")}


def _collect_laser_lines(setting) -> dict[str, dict[str, Any]]:
    """Build ``{laser_line_str: {intensity_dev, is_visible}}`` from the
    top-level (non-compensation) AotfList. Used to look up the AOTF intensity
    for a channel's reference laser line.
    """
    lines: dict[str, dict[str, Any]] = {}
    if setting is None:
        return lines
    # The AotfList directly under setting (not the one inside
    # CompensationInterpolationPoints) is the master.
    aotf_list = setting.find("./AotfList")
    if aotf_list is None:
        return lines
    for lls in aotf_list.findall(".//LaserLineSetting"):
        line = lls.get("LaserLine", "").strip()
        if not line:
            continue
        intensity = _num(lls.get("IntensityDev"))
        visible = _bool_attr(lls, "IsVisible")
        # Multiple Aotf groups may carry the same line; prefer the visible
        # entry when present.
        prior = lines.get(line)
        if prior is None or (visible and not prior.get("is_visible")):
            lines[line] = {
                "intensity_dev": intensity,
                "is_visible": bool(visible),
            }
    return lines


def _collect_lasers(setting) -> dict[str, dict[str, Any]]:
    """Build ``{laser_name: {power_state, wavelength_nm}}`` from
    ``<LaserArray>`` so we can show which physical laser was on per channel."""
    out: dict[str, dict[str, Any]] = {}
    if setting is None:
        return out
    laser_array = setting.find("./LaserArray")
    if laser_array is None:
        return out
    for laser in laser_array.findall("./Laser"):
        name = (laser.get("LaserName") or "").strip()
        if not name:
            continue
        wavelength = _int_attr(laser, "Wavelength") or _num(
            laser.get("WavelengthDouble")
        )
        out[name] = {
            "power_state": (laser.get("PowerState") or "").strip(),
            "wavelength_nm": int(wavelength) if isinstance(wavelength, (int, float)) else None,
        }
    return out


def _resolve_laser_for_detector(
    det, atl, atl_laser_lines: dict[str, dict[str, Any]]
) -> tuple[str, int | None]:
    """Find the laser line that excites the given Detector.

    Modern Stellaris encodes this directly via
    ``<DetectionReferenceLine LaserName="OPSL 488" LaserWavelength="488"/>``.
    Older LASAF SP5 (~2011) sequential-scan blocks don't have that element
    — instead, each per-channel ATL block carries an ``<AotfList>`` whose
    one *visible*, non-zero ``<LaserLineSetting>`` is the line driving the
    detector inside that block. Fall back to it when DetectionReferenceLine
    is absent.
    """
    ref_line = det.find("./DetectionReferenceLine")
    if ref_line is not None:
        name = (ref_line.get("LaserName") or "").strip()
        wavelength = _int_attr(ref_line, "LaserWavelength")
        if name or wavelength is not None:
            return name, wavelength
    # LASAF fallback: pick the laser line with non-zero IntensityDev in
    # this ATL block's AotfList. There's typically exactly one.
    for line_key, info in atl_laser_lines.items():
        intensity = info.get("intensity_dev")
        if intensity and intensity > 0:
            try:
                return "", int(line_key)
            except (ValueError, TypeError):
                continue
    return "", None


def _extract_active_channels(root, setting) -> list[dict[str, Any]]:
    """One JSON entry per active Detector, merged with the matching
    LaserLineSetting + Laser + ChannelDescription (by order).

    Handles both schema generations:

    - **Modern Stellaris** ships all active detectors inside the master
      ``ATLConfocalSettingDefinition``'s ``DetectorList``. The global
      ``AotfList`` + ``LaserArray`` carry the laser settings used by all
      channels.
    - **2011 LASAF SP5** runs sequential acquisition with one
      ``ATLConfocalSettingDefinition`` *per channel*; each block carries its
      own ``DetectorList`` (with exactly one active detector) and its own
      ``AotfList`` (with one non-zero LaserLineSetting = that channel's
      laser line). The master block holds only the channel being displayed
      at the time of export.

    Walking every ATL block and deduplicating by detector ``Channel`` lets
    one parser cover both cases without branching.

    Inactive detectors are skipped — Leica ships a full bank in every file
    and recording them all just clutters the table.
    """
    # ChannelDescription elements are ordered by the displayed channel
    # index; match by row order to the active Detectors.
    descriptions = list(root.iter("ChannelDescription"))

    # Walk all ATL blocks. Dedup by (Channel number) so the master's copy
    # of a sequential-scan detector doesn't double-count.
    atl_blocks = list(root.iter("ATLConfocalSettingDefinition"))
    seen_channels: set[int] = set()
    records: list[tuple[Any, Any]] = []  # (detector_el, owning_atl)
    for atl in atl_blocks:
        det_list = atl.find("./DetectorList")
        if det_list is None:
            continue
        for det in det_list.findall("./Detector"):
            if not _bool_attr(det, "IsActive"):
                continue
            ch_num = _int_attr(det, "Channel")
            if ch_num is None or ch_num in seen_channels:
                continue
            seen_channels.add(ch_num)
            records.append((det, atl))
    # Stable order by Channel number for deterministic JSON output.
    records.sort(key=lambda r: _int_attr(r[0], "Channel") or 0)

    rows: list[dict[str, Any]] = []
    for idx, (det, owning_atl) in enumerate(records):
        # Per-ATL laser lookups so sequential blocks pick up their own
        # AotfList + LaserArray rather than the master's.
        atl_laser_lines = _collect_laser_lines(owning_atl)
        atl_lasers = _collect_lasers(owning_atl)

        laser_name, laser_line_nm = _resolve_laser_for_detector(
            det, owning_atl, atl_laser_lines
        )
        lls_match = (
            atl_laser_lines.get(str(laser_line_nm)) if laser_line_nm is not None else None
        )
        laser_match = atl_lasers.get(laser_name) if laser_name else None

        acq_mode_code = (det.get("AcquisitionMode") or "").strip()
        acq_mode_name = (
            (det.get("AcquisitionModeName") or "").strip()
            or _ACQ_MODE_NAMES.get(acq_mode_code, acq_mode_code)
        )

        # Detector naming differs by era:
        #   - Modern Stellaris: separate `Name` ("HyD S 2") and `Type`
        #     ("SiPM"); `ChannelName` ("Channel 2") and numeric `Channel`.
        #   - 2011 LASAF SP5:   no `Name` / no `ChannelName`; `Type`
        #     doubles as the model ("PMT 2"); only numeric `Channel`.
        # Fall back gracefully so old-era rows still populate meaningfully.
        channel_num = _int_attr(det, "Channel")
        det_name = (det.get("Name") or "").strip() or (det.get("Type") or "").strip()
        det_type = (det.get("Type") or "").strip()
        ch_name = (det.get("ChannelName") or "").strip() or (
            f"Channel {channel_num}" if channel_num is not None else ""
        )
        # Round at storage so the JSON stays clean ("9.0" rather than
        # "8.99713117255692"). Display-side formatters then trust the
        # value rather than re-rounding. AOTF % rounded to 1 decimal,
        # gain to 1 (PMT volts are big; HyD gauge units are small —
        # 1 decimal works for both).
        intensity_raw = (lls_match or {}).get("intensity_dev")
        gain_raw = _num(det.get("Gain"))
        entry: dict[str, Any] = {
            # Detector
            "channel_number": channel_num,
            "channel_name": ch_name,
            "is_active": True,
            "is_enabled": bool(_bool_attr(det, "IsEnabled")),
            "detector_name": det_name,
            "detector_type": det_type,
            "detector_gain": round(gain_raw, 1) if gain_raw is not None else None,
            "detector_offset": _num(det.get("Offset")),
            "dye_name": (det.get("DyeName") or "").strip(),
            "band": (det.get("Band") or "").strip(),
            "acquisition_mode": acq_mode_name,
            # Laser + AOTF
            "laser_name": laser_name,
            "laser_line_nm": laser_line_nm,
            "laser_power_state": (laser_match or {}).get("power_state", ""),
            "laser_intensity_dev": (
                round(intensity_raw, 1) if intensity_raw is not None else None
            ),
        }
        # Merge in matching ChannelDescription (by order) if present — keeps
        # the legacy fields (LUT, min/max, resolution) on the same row.
        if idx < len(descriptions):
            cd = descriptions[idx]
            entry.update(
                {
                    "tag": cd.get("ChannelTag", ""),
                    "lut_name": cd.get("LUTName", ""),
                    "data_type": cd.get("DataType", ""),
                    "min": cd.get("Min", ""),
                    "max": cd.get("Max", ""),
                    "unit": cd.get("Unit", ""),
                    "name_of_measured_quantity": cd.get("NameOfMeasuredQuantity", ""),
                    "resolution_bits": cd.get("Resolution", ""),
                }
            )
        rows.append(entry)

    # Annotate min/max from each channel's owning-ATL Begin/End Cond block
    # so the GUI can show "3.0 (3.0–5.0)" for ramped compensation rather
    # than just one endpoint. Only populates when the schema actually
    # carries those Cond blocks (i.e. LASAF SP5 generation).
    _annotate_lasaf_compensation_ranges(rows, records)

    # If there are ChannelDescriptions that didn't match any active detector,
    # surface them as legacy-only entries so we don't lose the field. Rare
    # but keeps the format flexible across vendor quirks.
    if len(descriptions) > len(records):
        for cd in descriptions[len(records):]:
            rows.append(
                {
                    "is_active": False,
                    "tag": cd.get("ChannelTag", ""),
                    "lut_name": cd.get("LUTName", ""),
                    "data_type": cd.get("DataType", ""),
                    "min": cd.get("Min", ""),
                    "max": cd.get("Max", ""),
                    "unit": cd.get("Unit", ""),
                    "name_of_measured_quantity": cd.get("NameOfMeasuredQuantity", ""),
                    "resolution_bits": cd.get("Resolution", ""),
                }
            )
    return rows


def _extract_depth_compensation(
    root, setting, channels: list[dict[str, Any]]
) -> dict[str, Any]:
    """Project the global ``CompensationInterpolationPoints`` curve onto each
    active channel, keeping only ``(z_um, intensity_dev, gain)`` for the
    channel's own laser line + detector.

    Inactive channels (no laser_line_nm or detector_name) are skipped.
    The XML can carry the same block nested per-sequential-scan-setting, but
    the top-level master curve is the one the microscope was programmed with;
    that's the one we capture.

    Two compensation schemas are handled:

    - **Stellaris / late SP5**: ``CompensationInterpolationPoints`` carries
      an ``IntensityCompArray`` of N ``IntensityComp`` points, each with a
      ``ZPosition`` and embedded ``LaserLineSetting`` + ``Detector`` lists.
    - **2011 LASAF SP5**: each per-channel ``ATLConfocalSettingDefinition``
      carries one ``CompensationAotfBeginCond`` (laser + gain at Z stack
      top) and one ``CompensationAotfEndCond`` (laser + gain at Z stack
      bottom). Curve is just two points per channel; ``Begin`` / ``End``
      Z attributes on the owning ATL give the µm range.
    """
    if setting is None:
        return {}

    active = [c for c in channels if c.get("is_active")]
    if not active:
        return {}

    # Prefer the modern InterpolationPoints curve when present and the
    # projection produces something useful — it carries N>2 points
    # (richer than Begin/End Cond's 2 points). Fall back to the 2011
    # LASAF SP5 Begin/End Cond schema when InterpolationPoints is
    # absent or empty after projection.
    comp_block = setting.find("./CompensationInterpolationPoints")
    if comp_block is None:
        return _extract_begin_end_cond_curves(root, active)

    # Walk every IntensityComp once and stash (z_um, {laser_line:intensity},
    # {detector_name:gain}) so we don't re-walk for every channel.
    points: list[dict[str, Any]] = []
    for comp in comp_block.findall(".//IntensityComp"):
        z_m = _num(comp.get("ZPosition"))
        if z_m is None:
            continue
        z_um = round(z_m * 1e6, 4)
        intensities: dict[str, float | None] = {}
        for lls in comp.findall(".//LaserLineSetting"):
            line = lls.get("LaserLine", "").strip()
            if not line:
                continue
            intensities[line] = _num(lls.get("IntensityDev"))
        gains: dict[str, float | None] = {}
        for det in comp.findall(".//Detector"):
            name = (det.get("Name") or "").strip()
            if not name:
                continue
            g = _num(det.get("Gain"))
            if g is not None:
                gains[name] = g
        points.append(
            {"z_um": z_um, "intensities": intensities, "gains": gains}
        )

    if not points:
        # CompensationInterpolationPoints existed but had no IntensityComp
        # children (2011 LASAF carries that block with the real data in
        # the sibling Begin/End Cond elements). Fall through to those.
        return _extract_begin_end_cond_curves(root, active)

    # Sort by z so the curve reads in physical order regardless of how it was
    # programmed.
    points.sort(key=lambda p: p["z_um"])

    per_channel: list[dict[str, Any]] = []
    for ch in active:
        line_key = (
            str(ch["laser_line_nm"])
            if ch.get("laser_line_nm") is not None
            else None
        )
        det_name = ch.get("detector_name") or ""
        proj_points: list[dict[str, Any]] = []
        for p in points:
            entry: dict[str, Any] = {"z_um": p["z_um"]}
            if line_key is not None and line_key in p["intensities"]:
                v = p["intensities"][line_key]
                if v is not None:
                    entry["intensity_dev"] = round(v, 1)
            if det_name and det_name in p["gains"]:
                entry["gain"] = round(p["gains"][det_name], 1)
            # Only keep points that contribute something for this channel.
            if "intensity_dev" in entry or "gain" in entry:
                proj_points.append(entry)
        if proj_points:
            per_channel.append(
                {
                    "channel_name": ch.get("channel_name") or "",
                    "channel_number": ch.get("channel_number"),
                    "laser_line_nm": ch.get("laser_line_nm"),
                    "detector_name": det_name,
                    "points": proj_points,
                }
            )
    if per_channel:
        return {"channels": per_channel}
    # Modern block was present but projection produced nothing useful
    # (2011 LASAF carries empty IntensityCompArray with the real data in
    # the sibling Begin/End Cond blocks). Fall through to the 2011 path.
    return _extract_begin_end_cond_curves(setting, active)


def _annotate_lasaf_compensation_ranges(
    rows: list[dict[str, Any]],
    records: list[tuple[Any, Any]],
) -> None:
    """Add ``laser_intensity_dev_min/max`` + ``detector_gain_min/max`` from
    each channel's owning-ATL Begin/End Cond blocks (LASAF SP5 schema).

    Mirrors what ``_annotate_channel_intensity_ranges`` does for the
    OME-XML walker, but reads from XML directly rather than per-tp
    sampling. The active-channels table renders these via
    ``_fmt_with_range`` so a ramped 3→5% channel shows as
    ``"3.0 (3.0–5.0)"``.
    """
    for entry, (_, atl) in zip(rows, records):
        begin_cond = atl.find("./CompensationAotfBeginCond")
        end_cond = atl.find("./CompensationAotfEndCond")
        det_begin_cond = atl.find("./CompensationDetectorBeginCond")
        det_end_cond = atl.find("./CompensationDetectorEndCond")
        line_key = (
            str(entry["laser_line_nm"])
            if entry.get("laser_line_nm") is not None
            else None
        )

        def _line_intensity(cond_el) -> float | None:
            if cond_el is None or line_key is None:
                return None
            for lls in cond_el.findall(".//LaserLineSetting"):
                if lls.get("LaserLine", "").strip() == line_key:
                    return _num(lls.get("IntensityDev"))
            return None

        def _active_gain(cond_el) -> float | None:
            if cond_el is None:
                return None
            for d in cond_el.findall(".//Detector"):
                if _bool_attr(d, "IsActive"):
                    return _num(d.get("Gain"))
            return None

        begin_int = _line_intensity(begin_cond)
        end_int = _line_intensity(end_cond)
        begin_gain = _active_gain(det_begin_cond)
        end_gain = _active_gain(det_end_cond)

        intensities = [v for v in (begin_int, end_int) if v is not None]
        if intensities:
            entry["laser_intensity_dev_min"] = round(min(intensities), 1)
            entry["laser_intensity_dev_max"] = round(max(intensities), 1)
            # Prefer the begin value as the "representative" because it's
            # the as-set programmed start; the END value (which the top-
            # level AotfList carries) reads as "post-compensation" and
            # buries the ramp. The min/max annotation surfaces both.
            if begin_int is not None:
                entry["laser_intensity_dev"] = round(begin_int, 1)
        gains = [v for v in (begin_gain, end_gain) if v is not None]
        if gains:
            entry["detector_gain_min"] = round(min(gains), 1)
            entry["detector_gain_max"] = round(max(gains), 1)


def _extract_begin_end_cond_curves(
    root, active_channels: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build per-channel Z curves from CompensationAotfBeginCond/EndCond.

    The 2011 LASAF SP5 schema records compensation as just two endpoints
    rather than an interpolation array: laser line % at the top of the Z
    stack (``CompensationAotfBeginCond``), the same at the bottom
    (``CompensationAotfEndCond``), and similarly for detector gain via
    ``CompensationDetectorBeginCond/EndCond``. The microscope linearly
    interpolates between them at acquisition time.

    Each per-channel ``ATLConfocalSettingDefinition`` carries one such
    pair. We look up the active detector inside that block to know which
    channel the curve belongs to, then read the laser line + detector
    values from the begin/end Cond children. Two-point curves are
    emitted per channel that has non-flat compensation; we still emit a
    single-point "flat" entry for channels with begin == end so the GUI
    can show "no compensation applied" explicitly.
    """
    by_channel_num = {
        c.get("channel_number"): c
        for c in active_channels
        if c.get("channel_number") is not None
    }
    if not by_channel_num:
        return {}

    per_channel_out: list[dict[str, Any]] = []
    for atl in root.iter("ATLConfocalSettingDefinition"):
        begin_cond = atl.find("./CompensationAotfBeginCond")
        end_cond = atl.find("./CompensationAotfEndCond")
        det_begin_cond = atl.find("./CompensationDetectorBeginCond")
        det_end_cond = atl.find("./CompensationDetectorEndCond")
        if begin_cond is None and end_cond is None:
            continue
        # Find this block's active detector (channel) so we know which
        # row of `active_channels` this curve attaches to.
        det_list = atl.find("./DetectorList")
        if det_list is None:
            continue
        active_dets = [
            d for d in det_list.findall("./Detector") if _bool_attr(d, "IsActive")
        ]
        if not active_dets:
            continue
        det = active_dets[0]
        ch_num = _int_attr(det, "Channel")
        channel = by_channel_num.get(ch_num)
        if channel is None:
            continue
        line_key = (
            str(channel["laser_line_nm"])
            if channel.get("laser_line_nm") is not None
            else None
        )
        det_name = channel.get("detector_name") or ""

        # Z range comes from the owning ATL's Begin/End attributes (meters).
        z_begin_m = _num(atl.get("Begin"))
        z_end_m = _num(atl.get("End"))
        z_begin_um = round(z_begin_m * 1e6, 2) if z_begin_m is not None else None
        z_end_um = round(z_end_m * 1e6, 2) if z_end_m is not None else None

        def _intensity_for_line(cond_el, line_key) -> float | None:
            if cond_el is None or line_key is None:
                return None
            for lls in cond_el.findall(".//LaserLineSetting"):
                if lls.get("LaserLine", "").strip() == line_key:
                    return _num(lls.get("IntensityDev"))
            return None

        def _gain_for_active(cond_el) -> float | None:
            if cond_el is None:
                return None
            for d in cond_el.findall(".//Detector"):
                if _bool_attr(d, "IsActive"):
                    return _num(d.get("Gain"))
            return None

        begin_int = _intensity_for_line(begin_cond, line_key)
        end_int = _intensity_for_line(end_cond, line_key)
        begin_gain = _gain_for_active(det_begin_cond)
        end_gain = _gain_for_active(det_end_cond)

        # Skip channels that have neither a laser line nor a detector
        # value at either endpoint — nothing useful to record.
        if all(v is None for v in (begin_int, end_int, begin_gain, end_gain)):
            continue

        points: list[dict[str, Any]] = []
        if z_begin_um is not None:
            p: dict[str, Any] = {"z_um": z_begin_um}
            if begin_int is not None:
                p["intensity_dev"] = round(begin_int, 1)
            if begin_gain is not None:
                p["gain"] = round(begin_gain, 1)
            points.append(p)
        if z_end_um is not None:
            p = {"z_um": z_end_um}
            if end_int is not None:
                p["intensity_dev"] = round(end_int, 1)
            if end_gain is not None:
                p["gain"] = round(end_gain, 1)
            points.append(p)
        if not points:
            continue
        per_channel_out.append(
            {
                "channel_name": channel.get("channel_name") or "",
                "channel_number": ch_num,
                "laser_line_nm": channel.get("laser_line_nm"),
                "detector_name": det_name,
                "points": points,
            }
        )

    if not per_channel_out:
        return {}

    # When master + sequential ATL blocks share the same active detector
    # (the master is a copy of one of the sequential channels), we get
    # multiple curves for the same channel_number. Keep only the curve
    # with the widest intensity spread — that's the one that actually
    # carries non-flat compensation if any does.
    def _spread(curve: dict[str, Any]) -> float:
        ints = [
            p["intensity_dev"]
            for p in curve["points"]
            if p.get("intensity_dev") is not None
        ]
        return max(ints) - min(ints) if ints else 0.0

    deduped: dict[Any, dict[str, Any]] = {}
    for c in per_channel_out:
        key = c.get("channel_number")
        if key is None:
            deduped[id(c)] = c
            continue
        prior = deduped.get(key)
        if prior is None or _spread(c) > _spread(prior):
            deduped[key] = c
    return {"channels": list(deduped.values())}


__all__ = ["LeicaMetadata", "parse_properties_xml"]
