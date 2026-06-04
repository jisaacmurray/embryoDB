"""OME-XML parser for legacy Leica SP5 acquisitions (and any vendor whose
Bio-Formats export lands as ``omxml.xml(.gz)``).

The pre-2021 lab pipeline wrote per-acquisition OME-XML to
``<annot_loc>/dats/omxml.xml.gz`` plus a companion ``imageList.tsv`` that
maps the L-series's timepoints to OME ``<Image>`` Name attributes. The
same ``omxml.xml.gz`` is shared by every L of one acquisition; the
``imageList.tsv`` selects this L's subset.

This module produces a :class:`embryodb.parsers.leica_metadata.LeicaMetadata`
so :func:`embryodb.parsers.microscopy.parse_microscopy` can dispatch
transparently between Stellaris and SP5 without callers caring which.

Differences from Stellaris worth knowing:

- **Detector gain units differ.** SP5 used PMTs; gain values are in volts
  (~1100). Stellaris uses HyD detectors with arbitrary 0–100 gauge units.
  We store the raw value either way — the GUI shows ``detector_type``
  alongside so the user can interpret.
- **Laser intensity**. ``LightSourceSettings.Attenuation`` is **0 = full
  transmission, 1 = fully blocked** per the OME schema — i.e., the
  fraction *blocked*, not the fraction *passed*. The Stellaris parser
  already reports AOTF intensity in % transmitted, so we mirror that
  convention here: ``laser_intensity_dev = (1 - Attenuation) * 100``.
  A 0.84 Attenuation in the XML therefore surfaces as a 16% laser
  intensity, matching what the lab sees at the microscope and what
  Stellaris records natively.
- **Depth compensation** has no analogous block in OME-XML; the SP5 era
  pre-dates programmed Z compensation in our pipeline. Returned as ``{}``.
"""

from __future__ import annotations

import csv
import gzip
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .leica_metadata import LeicaMetadata


# ---------------------------------------------------------------------------
# XML loading + namespace helpers
# ---------------------------------------------------------------------------


def _load_root(path: Path) -> ET.Element:
    """Load an OME-XML root from either a ``.xml`` or ``.xml.gz`` file."""
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as f:
            return ET.parse(f).getroot()
    return ET.parse(path).getroot()


def _ns_of(root: ET.Element) -> dict[str, str]:
    """Return ``{'ome': namespace_uri}`` for ElementTree's XPath lookups."""
    tag = root.tag
    if tag.startswith("{"):
        return {"ome": tag[1 : tag.index("}")]}
    return {"ome": ""}


def _f(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _i(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# imageList.tsv loader
# ---------------------------------------------------------------------------


def load_image_list(path: Path) -> dict[int, str]:
    """Parse a ``dats/imageList.tsv`` into ``{timepoint: image_name}``.

    The file is two columns, tab-separated, with a header row of
    ``time\\timage``. Returns an empty dict on read errors so callers can
    soft-fail.
    """
    out: dict[int, str] = {}
    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                tp_raw = row.get("time", "")
                name = row.get("image", "")
                if not tp_raw.strip() or not name:
                    continue
                try:
                    out[int(tp_raw)] = name
                except ValueError:
                    continue
    except OSError:
        return {}
    return out


def find_image_list(dats_dir: Path) -> Path | None:
    """Look for ``imageList.tsv`` in ``dats_dir``."""
    cand = dats_dir / "imageList.tsv"
    return cand if cand.is_file() else None


# ---------------------------------------------------------------------------
# Per-image microscopy metadata
# ---------------------------------------------------------------------------


@dataclass
class _OmeImageIndex:
    """Cached parse of one OME-XML file. Built lazily and reused per series."""

    root: ET.Element
    ns: dict[str, str]
    images_by_name: dict[str, ET.Element]
    instruments_by_id: dict[str, ET.Element]


def _index(path: Path) -> _OmeImageIndex:
    root = _load_root(path)
    ns = _ns_of(root)
    images_by_name: dict[str, ET.Element] = {}
    for im in root.findall("ome:Image", ns):
        name = im.get("Name", "")
        if name:
            images_by_name[name] = im
    instruments_by_id: dict[str, ET.Element] = {}
    for inst in root.findall("ome:Instrument", ns):
        inst_id = inst.get("ID", "")
        if inst_id:
            instruments_by_id[inst_id] = inst
    return _OmeImageIndex(
        root=root,
        ns=ns,
        images_by_name=images_by_name,
        instruments_by_id=instruments_by_id,
    )


def parse_acquisition_dates(
    path: Path, image_names: list[str]
) -> list[datetime | None]:
    """Resolve a sequence of image names → AcquisitionDate datetimes.

    Returns parallel-indexed datetimes (None where the name didn't match
    an Image element or the date didn't parse). The whole-file parse is
    done once and reused.
    """
    idx = _index(path)
    out: list[datetime | None] = []
    for name in image_names:
        im = idx.images_by_name.get(name)
        if im is None:
            out.append(None)
            continue
        acq = im.find("ome:AcquisitionDate", idx.ns)
        if acq is None or not (acq.text or "").strip():
            out.append(None)
            continue
        try:
            # OME-XML AcquisitionDate is ISO-8601; SP5 exports it without a
            # timezone (it's local clock time on the acquisition machine).
            # We treat it as naive — delta calculations are unaffected and
            # absolute_seconds doesn't depend on absolute UTC.
            out.append(datetime.fromisoformat(acq.text.strip()))
        except ValueError:
            out.append(None)
    return out


# ---------------------------------------------------------------------------
# Microscopy metadata projection → LeicaMetadata
# ---------------------------------------------------------------------------


def parse_ome_xml_as_metadata(
    path: Path,
    *,
    image_name: str | None = None,
    image_list: dict[int, str] | None = None,
) -> LeicaMetadata:
    """Parse an OME-XML file into a ``LeicaMetadata`` (the shape the
    backfill/orchestrator already knows how to store).

    Args:
      path: ``omxml.xml`` or ``omxml.xml.gz`` file.
      image_name: representative ``<Image>`` Name for per-channel +
        scalar settings. Ignored if ``image_list`` is provided
        (the first imageList entry takes precedence).
      image_list: ``{timepoint: image_name}`` map from the L's
        ``imageList.tsv``. When provided, the parser walks every entry
        to build a per-channel **time-indexed** compensation curve
        (``depth_compensation.axis == "timepoint"``) capturing any
        ramped laser attenuation / detector gain across the acquisition
        — the SP5-era lab convention for "depth compensation". Without
        an image_list we can't tell which Images belong to this L (multi-
        position acquisitions share one omxml.xml.gz), so we fall back to
        single-Image extraction and emit no compensation curve.
    """
    idx = _index(path)
    out = LeicaMetadata()

    # Pick a representative image. Multi-position acquisitions share one
    # omxml.xml.gz across all L's; the L's first imageList entry selects
    # the right one. Otherwise fall back to first non-DriftAF.
    target = None
    if image_list:
        first_tp = min(image_list)
        target = idx.images_by_name.get(image_list[first_tp])
    if target is None and image_name is not None:
        target = idx.images_by_name.get(image_name)
    if target is None:
        for name, im in idx.images_by_name.items():
            if "DriftAF" not in name:
                target = im
                break
    if target is None:
        return out

    _fill_instrument(out, idx, target)
    _fill_pixels(out, idx, target)
    _fill_channels_and_settings(out, idx, target)

    # Time-varying "depth" compensation: walk every imageList entry,
    # collect per-channel (tp, attenuation, gain), emit a curve with
    # change points only. Empty if nothing varies across the run.
    if image_list:
        out.depth_compensation = _extract_time_compensation_curve(
            idx, image_list, out.channels
        )
        # Also surface min/max attenuation in each channel JSON so the
        # GUI's "Active channels" table can show a range rather than a
        # single tp=1 number when compensation was ramped.
        _annotate_channel_intensity_ranges(out.channels, idx, image_list)
    else:
        out.depth_compensation = {}
    return out


def _extract_time_compensation_curve(
    idx: _OmeImageIndex,
    image_list: dict[int, str],
    channels: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a per-channel time-indexed compensation curve.

    For each active channel, walks all imageList entries and emits a
    ``points`` list of ``{timepoint, intensity_dev, gain}`` for each
    *change* in either value. Constant-throughout channels collapse to
    a single point. Returns ``{}`` when no channel had anything to
    report (e.g. all imageList entries missing from the OME index).
    """
    if not channels:
        return {}
    tps_sorted = sorted(image_list)

    per_channel_out: list[dict[str, Any]] = []
    for ch_idx, ch in enumerate(channels):
        if not ch.get("is_active"):
            continue
        # Transmission/DIC channels have no laser to track — skip them
        # so the compensation table doesn't show meaningless "intensity"
        # rows for them.
        if ch.get("channel_type") == "transmission":
            continue
        # Walk the timepoint sequence, pull this channel's
        # LightSourceSettings.Attenuation and DetectorSettings.Gain from
        # each Image. ChannelDescription order is shared across all
        # Images, so ch_idx is the right index into the Image's Channel
        # list.
        points: list[dict[str, Any]] = []
        last_intensity: float | None = None
        last_gain: float | None = None
        for tp in tps_sorted:
            name = image_list[tp]
            im = idx.images_by_name.get(name)
            if im is None:
                continue
            pix = im.find("ome:Pixels", idx.ns)
            if pix is None:
                continue
            im_channels = pix.findall("ome:Channel", idx.ns)
            if ch_idx >= len(im_channels):
                continue
            im_ch = im_channels[ch_idx]
            ls = im_ch.find("ome:LightSourceSettings", idx.ns)
            ds = im_ch.find("ome:DetectorSettings", idx.ns)
            # OME Attenuation = fraction blocked; convert to % transmitted.
            atten = _f(ls.get("Attenuation")) if ls is not None else None
            gain = _f(ds.get("Gain")) if ds is not None else None
            intensity_pct = (
                round((1.0 - atten) * 100, 1) if atten is not None else None
            )
            gain = round(gain, 1) if gain is not None else None
            # Emit a point only when something CHANGED (or it's the first).
            changed = (
                not points
                or (
                    intensity_pct is not None
                    and last_intensity is not None
                    and abs(intensity_pct - last_intensity) > 1e-6
                )
                or (
                    gain is not None
                    and last_gain is not None
                    and abs(gain - last_gain) > 1e-6
                )
            )
            if changed:
                points.append(
                    {
                        "timepoint": tp,
                        "intensity_dev": intensity_pct,
                        "gain": gain,
                    }
                )
                last_intensity = intensity_pct
                last_gain = gain
        if points:
            per_channel_out.append(
                {
                    "channel_name": ch.get("channel_name") or "",
                    "channel_number": ch.get("channel_number"),
                    "laser_line_nm": ch.get("laser_line_nm"),
                    "detector_name": ch.get("detector_name") or "",
                    "points": points,
                }
            )
    if not per_channel_out:
        return {}
    return {"axis": "timepoint", "channels": per_channel_out}


def _annotate_channel_intensity_ranges(
    channels: list[dict[str, Any]],
    idx: _OmeImageIndex,
    image_list: dict[int, str],
) -> None:
    """Add ``laser_intensity_dev_min/max`` + ``detector_gain_min/max`` to
    each active channel so the GUI can show a range (e.g. "78–91 %")
    instead of just the tp=1 value when compensation was ramped.
    """
    tps_sorted = sorted(image_list)
    for ch_idx, ch in enumerate(channels):
        if not ch.get("is_active"):
            continue
        intensities: list[float] = []
        gains: list[float] = []
        for tp in tps_sorted:
            im = idx.images_by_name.get(image_list[tp])
            if im is None:
                continue
            pix = im.find("ome:Pixels", idx.ns)
            if pix is None:
                continue
            im_channels = pix.findall("ome:Channel", idx.ns)
            if ch_idx >= len(im_channels):
                continue
            im_ch = im_channels[ch_idx]
            ls = im_ch.find("ome:LightSourceSettings", idx.ns)
            ds = im_ch.find("ome:DetectorSettings", idx.ns)
            atten = _f(ls.get("Attenuation")) if ls is not None else None
            if atten is not None:
                # OME Attenuation = fraction blocked; convert to % transmitted.
                intensities.append((1.0 - atten) * 100)
            gain = _f(ds.get("Gain")) if ds is not None else None
            if gain is not None:
                gains.append(gain)
        if intensities:
            # Match the 1-decimal display convention used everywhere else
            # so the GUI shows "16.0–22.0" not "15.998–21.998".
            ch["laser_intensity_dev_min"] = round(min(intensities), 1)
            ch["laser_intensity_dev_max"] = round(max(intensities), 1)
        if gains:
            ch["detector_gain_min"] = round(min(gains), 1)
            ch["detector_gain_max"] = round(max(gains), 1)


def _fill_instrument(
    out: LeicaMetadata, idx: _OmeImageIndex, im: ET.Element
) -> None:
    """Pull objective + microscope identity out of the referenced Instrument."""
    inst_ref = im.find("ome:InstrumentRef", idx.ns)
    inst_id = inst_ref.get("ID") if inst_ref is not None else None
    inst = idx.instruments_by_id.get(inst_id) if inst_id else None
    if inst is None:
        return

    # Objective: prefer the one ObjectiveSettings points to, else first.
    obj_settings = im.find("ome:ObjectiveSettings", idx.ns)
    obj_id = obj_settings.get("ID") if obj_settings is not None else None
    obj = None
    if obj_id:
        for o in inst.findall("ome:Objective", idx.ns):
            if o.get("ID") == obj_id:
                obj = o
                break
    if obj is None:
        obj = inst.find("ome:Objective", idx.ns)
    if obj is not None:
        out.objective = (obj.get("Model") or "").strip()
        out.objective_NA = _f(obj.get("LensNA"))
        out.magnification = _f(obj.get("NominalMagnification"))
        out.immersion = (obj.get("Immersion") or "").strip()
    if obj_settings is not None:
        out.refractive_index = _f(obj_settings.get("RefractiveIndex"))

    # Microscope model + serial → acquisition_settings (parity with Stellaris).
    scope = inst.find("ome:Microscope", idx.ns)
    if scope is not None:
        out.acquisition_settings.setdefault(
            "microscope_model", (scope.get("Model") or "").strip()
        )
    if obj is not None and obj.get("SerialNumber"):
        out.acquisition_settings.setdefault(
            "objective_serial_number", obj.get("SerialNumber")
        )
    # Drop any empty-string entries to keep the JSON compact.
    out.acquisition_settings = {
        k: v for k, v in out.acquisition_settings.items() if v not in (None, "")
    }


def _fill_pixels(
    out: LeicaMetadata, idx: _OmeImageIndex, im: ET.Element
) -> None:
    """Pull voxel + image-geometry numbers from the Pixels element."""
    pix = im.find("ome:Pixels", idx.ns)
    if pix is None:
        return
    out.voxel_xy_um = _f(pix.get("PhysicalSizeX")) or _f(pix.get("PhysicalSizeY"))
    out.voxel_z_um = _f(pix.get("PhysicalSizeZ"))
    out.x_pixels = _i(pix.get("SizeX"))
    out.y_pixels = _i(pix.get("SizeY"))
    out.planes_per_volume = _i(pix.get("SizeZ"))
    # n_timepoints for OME is per-Image (typically 1 for SP5 multi-position
    # exports) — leave None and let the caller / acquisition_settings fill
    # the across-Image count from imageList.tsv if needed.


def _fill_channels_and_settings(
    out: LeicaMetadata, idx: _OmeImageIndex, im: ET.Element
) -> None:
    """Cross-reference per-Channel info with the Instrument's Detector +
    LightSource elements, producing the same channel-row shape as the
    Stellaris parser."""
    inst_ref = im.find("ome:InstrumentRef", idx.ns)
    inst_id = inst_ref.get("ID") if inst_ref is not None else None
    inst = idx.instruments_by_id.get(inst_id) if inst_id else None

    # Build per-detector and per-laser lookups for cross-ref.
    detectors_by_id: dict[str, ET.Element] = {}
    lasers_by_id: dict[str, ET.Element] = {}
    if inst is not None:
        for d in inst.findall("ome:Detector", idx.ns):
            if d.get("ID"):
                detectors_by_id[d.get("ID")] = d
        for ls in inst.findall("ome:LightSource", idx.ns):
            if ls.get("ID"):
                lasers_by_id[ls.get("ID")] = ls

    pix = im.find("ome:Pixels", idx.ns)
    if pix is None:
        return
    channels = pix.findall("ome:Channel", idx.ns)
    rows: list[dict[str, Any]] = []
    for ch_idx, ch in enumerate(channels):
        ch_id = ch.get("ID", "")
        # Each Channel has nested DetectorSettings + LightSourceSettings.
        det_settings = ch.find("ome:DetectorSettings", idx.ns)
        ls_settings = ch.find("ome:LightSourceSettings", idx.ns)
        det = (
            detectors_by_id.get(det_settings.get("ID")) if det_settings is not None else None
        )
        laser_el = lasers_by_id.get(ls_settings.get("ID")) if ls_settings is not None else None
        laser_inner = (
            laser_el.find("ome:Laser", idx.ns) if laser_el is not None else None
        )

        # OME Attenuation = fraction blocked (0 = full transmission, 1 = full
        # block). Express as % transmitted so the value lines up with the
        # Stellaris parser and the microscope's own "Intensity" display.
        # Round at storage so the JSON stays clean ("9.0" not "8.997…").
        attenuation = _f(ls_settings.get("Attenuation")) if ls_settings is not None else None
        intensity_pct = (
            round((1.0 - attenuation) * 100, 1) if attenuation is not None else None
        )

        # Distinguish fluorescence channels (have a Laser LightSource) from
        # transmission/DIC channels (no LightSourceSettings, or one that
        # resolves to a non-Laser source). Two-channel acquisitions with
        # 1 fluorescence + 1 DIC are common; tagging the latter lets the
        # GUI render it as "(DIC)" instead of blank cells, and the
        # compensation extractor skips it (no laser to track).
        has_laser = (
            laser_inner is not None
            and (_i(laser_inner.get("Wavelength")) is not None)
        )
        channel_type = "fluorescence" if has_laser else "transmission"

        entry: dict[str, Any] = {
            # Channel
            "is_active": True,
            "channel_number": ch_idx + 1,
            "channel_name": (ch.get("Name") or f"Channel {ch_idx + 1}"),
            "dye_name": (ch.get("Name") or "").strip(),
            "channel_type": channel_type,
            "tag": str(ch_idx),
            # Detector
            "detector_name": (det.get("Model") if det is not None else "") or "",
            "detector_type": (det.get("Type") if det is not None else "") or "",
            "detector_gain": (
                round(_f(det_settings.get("Gain")), 1)
                if det_settings is not None and _f(det_settings.get("Gain")) is not None
                else None
            ),
            "detector_offset": (
                _f(det_settings.get("Offset")) if det_settings is not None else None
            ),
            # Laser
            "laser_name": (laser_el.get("ID") if laser_el is not None else "") or "",
            "laser_line_nm": _i(
                laser_inner.get("Wavelength") if laser_inner is not None else None
            ),
            "laser_intensity_dev": intensity_pct,
            "laser_power_state": "On" if attenuation else "",
            # Other channel hints from OME
            "excitation_wavelength_nm": _i(ch.get("ExcitationWavelength")),
            "pinhole_size_um": _f(ch.get("PinholeSize")),
            "band": _band_from_emission_filter(ch, inst, idx),
            "acquisition_mode": "",  # SP5 PMTs don't expose this slot
        }
        rows.append(entry)
    out.channels = rows


def _band_from_emission_filter(
    ch: ET.Element, inst: ET.Element | None, idx: _OmeImageIndex
) -> str:
    """Resolve the channel's EmissionFilterRef → Filter TransmittanceRange
    so we can show the channel's spectral detection window (CutIn–CutOut)
    in the same "Band" slot as the Stellaris detector band."""
    if inst is None:
        return ""
    light_path = ch.find("ome:LightPath", idx.ns)
    if light_path is None:
        return ""
    em_ref = light_path.find("ome:EmissionFilterRef", idx.ns)
    if em_ref is None or not em_ref.get("ID"):
        return ""
    target_id = em_ref.get("ID")
    for filt in inst.findall("ome:Filter", idx.ns):
        if filt.get("ID") == target_id:
            tr = filt.find("ome:TransmittanceRange", idx.ns)
            if tr is None:
                return ""
            cut_in = tr.get("CutIn")
            cut_out = tr.get("CutOut")
            if cut_in and cut_out:
                return f"({cut_in}nm - {cut_out}nm)"
    return ""


__all__ = [
    "find_image_list",
    "load_image_list",
    "parse_acquisition_dates",
    "parse_ome_xml_as_metadata",
]
