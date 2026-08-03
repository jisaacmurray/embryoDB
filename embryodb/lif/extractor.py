"""LIF→TIF extraction with metadata sidecar.

The :class:`LifExtractor` is intentionally narrow: it walks an OPEN
:class:`readlif.reader.LifFile`, emits per-plane LZW-compressed TIFFs at
paths the caller chooses (via a ``path_fn`` callback), and writes the
sliced + transformed per-position ``Properties.xml`` next to them.

It deliberately knows nothing about :class:`Acquisition` rows, the
:class:`Protocol` table, or pipeline orchestration. The CLI / GUI / tests
build the right ``path_fn`` for their layout (source-dir vs staged
image_loc) and call the extractor with it.

Bit-depth policy mirrors the brief in the master plan:

- ``pass`` — write whatever dtype readlif returns. Breaks StarryNite on
  16-bit; user accepts.
- ``downcast`` (default) — if the array dtype is wider than 8-bit,
  right-shift by ``(stored_bits - 8)`` so the values fit into uint8 without
  saturating. Warns once per position.
- ``fail`` — raise on any non-uint8 input.

Idempotency: if the destination TIF already exists and ``overwrite``
is False, the write is skipped. This matches
:func:`embryodb.pipeline.stage.stage_position`'s contract so the
orchestrator's later ``stage_images`` step no-ops on already-staged files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np
import tifffile

from ..fsutil import (
    DEFAULT_FILE_MODE,
    chgrp_if_possible,
    chmod_if_possible,
    ensure_dir,
    safe_write_text,
)
from .metadata import (
    _position_elements,
    _series_elements,
    slice_position_xml,
)

log = logging.getLogger(__name__)


# Path callback shape. Called once per (timepoint, z_plane, raw_channel)
# during extraction; returns the destination Path for that plane. Callers
# embed any layout decision they want (Z-inversion, channel routing) in
# this closure.
# Returning None omits that plane entirely — used to drop a whole channel
# (e.g. DIC) without writing it anywhere.
PathFn = Callable[[int, int, int], "Path | None"]


# ---------------------------------------------------------------------------
# Per-series planning data (read-only metadata walk; no pixel reads)
# ---------------------------------------------------------------------------


@dataclass
class LifChannelInfo:
    """Per-channel summary, populated from the LIF's embedded XML.

    The cross-reference here mirrors what
    :func:`embryodb.parsers.leica_metadata.parse_properties_xml` does on
    the on-disk Properties.xml, but we walk the same elements lazily for
    ``lif-inspect`` (which doesn't need to materialize a full
    LeicaMetadata).
    """

    raw_index: int
    dye_name: str = ""
    laser_line_nm: int | None = None
    laser_intensity_dev: float | None = None
    detector_name: str = ""
    detector_gain: float | None = None


@dataclass
class LifPositionInfo:
    """One position inside a series — pre-pixel-read metadata snapshot."""

    name: str               # e.g. "Position 1"
    raw_index: int          # 0-based index within readlif's iterator
    n_timepoints: int
    n_planes: int
    x_pixels: int
    y_pixels: int
    bit_depth: tuple[int, ...]


@dataclass
class LifSeriesInfo:
    """One top-level series (TileScan N) inside the LIF.

    Constructed by :func:`inspect_lif`; consumed by the CLI / GUI to
    display the series picker without ever touching pixel data.
    """

    name: str
    positions: list[LifPositionInfo] = field(default_factory=list)
    channels: list[LifChannelInfo] = field(default_factory=list)
    voxel_xy_um: float | None = None
    voxel_z_um: float | None = None
    cycle_time_s: float | None = None
    objective: str = ""
    microscope_model: str = ""

    # Total estimated size for reporting; rough upper-bound assuming
    # raw uncompressed planes (the actual on-disk size after LZW is
    # typically 40-70% of this).
    @property
    def total_planes(self) -> int:
        if not self.positions:
            return 0
        per_pos = self.positions[0]
        # Assumes uniform shape across positions; mixed-shape series
        # would need a per-position sum but that's atypical.
        return (
            len(self.positions)
            * per_pos.n_timepoints
            * per_pos.n_planes
            * len(self.channels)
        )

    def estimated_uncompressed_bytes(self) -> int:
        if not self.positions:
            return 0
        per_pos = self.positions[0]
        bytes_per_pixel = 1  # 8-bit; downcast policy enforces this
        return (
            self.total_planes
            * per_pos.x_pixels
            * per_pos.y_pixels
            * bytes_per_pixel
        )


# ---------------------------------------------------------------------------
# Extraction outcome — one report per position extracted
# ---------------------------------------------------------------------------


@dataclass
class ExtractionReport:
    """Per-position outcome from :meth:`LifExtractor.extract_position`."""

    series_name: str
    position_name: str
    planes_total: int = 0
    planes_written: int = 0
    planes_skipped: int = 0       # dst already existed; not overwritten
    planes_omitted: int = 0       # channel dropped by role 'skip'
    bytes_written: int = 0
    bit_depth_warning: str | None = None
    error: str | None = None

    @property
    def completed(self) -> bool:
        return self.error is None


@dataclass
class ExtractionPlan:
    """What :meth:`LifExtractor.plan_series` returns — enough info for the
    CLI / GUI to display a confirmation page before launch."""

    series: LifSeriesInfo
    positions: list[LifPositionInfo]      # subset; user may have filtered
    bit_depth_policy: str
    compress: bool


# ---------------------------------------------------------------------------
# LifExtractor
# ---------------------------------------------------------------------------


class LifExtractor:
    """Plan + extract per-plane TIFs from a Leica .lif file.

    Caller pattern (CLI / GUI):

    >>> ex = LifExtractor("/murrlab3/Images/foo.lif")
    >>> series_list = ex.list_series()
    >>> # ... user picks "TileScan 2" ...
    >>> plan = ex.plan_series("TileScan 2", bit_depth_policy="downcast", compress=True)
    >>> # ... user confirms ...
    >>> for position in plan.positions:
    ...     def path_fn(t, z, raw_ch):
    ...         return out_dir / f"foo_Position {position.raw_index+1}_t{t:03d}_z{z:02d}_ch{raw_ch:02d}.tif"
    ...     report = ex.extract_position("TileScan 2", position.name, path_fn=path_fn, compress=True)
    """

    def __init__(self, lif_path: Path | str):
        # readlif is imported lazily so the test suite can import this
        # module without the dep when needed.
        from readlif.reader import LifFile

        self.path = Path(lif_path)
        self.lif = LifFile(str(self.path))
        # Build name → LifImage index once for fast position lookups.
        self._images_by_name = {im.name: im for im in self.lif.get_iter_image()}

    # --- planning (no pixel reads) ----------------------------------------

    def list_series(self) -> list[LifSeriesInfo]:
        """Walk LIF metadata; return one entry per top-level series.

        Pure-metadata operation — does not load any pixel data. Suitable
        for ``lif-inspect`` and the GUI series picker.
        """
        out: list[LifSeriesInfo] = []
        for series_el in _series_elements(self.lif.xml_root):
            sname = series_el.get("Name") or ""
            info = LifSeriesInfo(name=sname)
            # Per-position info from the LifImage iterator (which has
            # dims/bit_depth already); join by "<series>/<position>".
            for pos_el in _position_elements(series_el):
                pname = pos_el.get("Name") or ""
                full = f"{sname}/{pname}"
                im = self._images_by_name.get(full)
                if im is None:
                    continue
                # Use first position's metadata to fill the series-level
                # voxel sizes / objective / channels (uniform across
                # positions for a TileScan).
                if not info.positions:
                    info.voxel_xy_um, info.voxel_z_um, info.cycle_time_s = (
                        self._derive_scalars(im)
                    )
                    info.objective, info.microscope_model = (
                        self._derive_instrument(pos_el)
                    )
                    info.channels = self._derive_channels(pos_el)
                info.positions.append(
                    LifPositionInfo(
                        name=pname,
                        raw_index=len(info.positions),
                        n_timepoints=im.nt,
                        n_planes=im.nz,
                        x_pixels=im.dims.x,
                        y_pixels=im.dims.y,
                        bit_depth=tuple(im.bit_depth),
                    )
                )
            out.append(info)
        return out

    def plan_series(
        self,
        series_name: str,
        *,
        bit_depth_policy: str = "downcast",
        compress: bool = True,
        positions: list[str] | None = None,
    ) -> ExtractionPlan:
        """Build an :class:`ExtractionPlan` for a chosen series.

        ``positions``: optional list of position names to filter (default:
        all positions in the series).
        """
        series = next(
            (s for s in self.list_series() if s.name == series_name), None
        )
        if series is None:
            raise ValueError(f"series {series_name!r} not found in LIF")
        pos_subset = (
            [p for p in series.positions if p.name in positions]
            if positions is not None
            else list(series.positions)
        )
        return ExtractionPlan(
            series=series,
            positions=pos_subset,
            bit_depth_policy=bit_depth_policy,
            compress=compress,
        )

    # --- extraction (pixel reads) -----------------------------------------

    def extract_position(
        self,
        series_name: str,
        position_name: str,
        *,
        path_fn: PathFn,
        bit_depth_policy: str = "downcast",
        compress: bool = True,
        overwrite: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> ExtractionReport:
        """Write one position's TIFs.

        ``path_fn(t, z, raw_channel)`` is called once per acquired plane;
        returns the destination path. Parent directories are created
        automatically; permissions go through
        :func:`embryodb.fsutil.chmod_if_possible` + ``chgrp_if_possible``
        for group-write consistency with the rest of the pipeline.

        Bit-depth policy semantics:

        - ``pass``: ``tifffile.imwrite`` is called with the raw dtype.
        - ``downcast``: uint16 inputs are right-shifted to fit uint8.
          A single warning is emitted (per position).
        - ``fail``: any non-uint8 input raises ``ValueError``.
        """
        full = f"{series_name}/{position_name}"
        im = self._images_by_name.get(full)
        if im is None:
            return ExtractionReport(
                series_name=series_name,
                position_name=position_name,
                error=f"position {full!r} not in LIF",
            )

        nt = im.nt or 1
        nz = im.nz or 1
        nc = im.channels or 1
        bit_depth = im.bit_depth or (8,)

        report = ExtractionReport(
            series_name=series_name,
            position_name=position_name,
            planes_total=nt * nz * nc,
        )

        warned_bit_depth = False
        done = 0
        for t in range(nt):
            for z in range(nz):
                for raw_ch in range(nc):
                    dst = path_fn(t, z, raw_ch)
                    if dst is None:
                        report.planes_omitted += 1
                        done += 1
                        if on_progress is not None:
                            on_progress(done, report.planes_total)
                        continue
                    if dst.exists() and not overwrite:
                        report.planes_skipped += 1
                        done += 1
                        if on_progress is not None:
                            on_progress(done, report.planes_total)
                        continue
                    try:
                        # readlif returns a PIL Image; flatten to ndarray
                        # so tifffile can write it and the bit-depth policy
                        # can introspect dtype.
                        arr = np.asarray(im.get_frame(z=z, t=t, c=raw_ch))
                    except Exception as exc:  # noqa: BLE001
                        report.error = f"frame read failed at (t={t},z={z},c={raw_ch}): {exc}"
                        return report
                    arr_to_write, warned = self._apply_bit_depth_policy(
                        arr,
                        bit_depth[raw_ch] if raw_ch < len(bit_depth) else 8,
                        bit_depth_policy,
                        warned_bit_depth,
                    )
                    warned_bit_depth = warned_bit_depth or warned
                    ensure_dir(dst.parent)
                    tifffile.imwrite(
                        str(dst),
                        arr_to_write,
                        compression="lzw" if compress else None,
                    )
                    chmod_if_possible(dst, DEFAULT_FILE_MODE)
                    chgrp_if_possible(dst)
                    try:
                        report.bytes_written += dst.stat().st_size
                    except OSError:
                        pass
                    report.planes_written += 1
                    done += 1
                    if on_progress is not None:
                        on_progress(done, report.planes_total)

        if warned_bit_depth and report.bit_depth_warning is None:
            report.bit_depth_warning = (
                f"downcast applied: stored bit_depth={bit_depth} → uint8"
            )
        return report

    def write_position_xml(
        self,
        series_name: str,
        position_name: str,
        dst: Path,
    ) -> Path:
        """Slice + transform LIF metadata; write as a Properties.xml.

        Filename should follow the Stellaris convention
        (``<stem>_Position N_Properties.xml``) so the existing
        :func:`parse_properties_xml` dispatcher picks it up. The caller
        decides where to put it (typically ``<annot_loc>/dats/``).
        """
        body = slice_position_xml(self.lif.xml_root, series_name, position_name)
        safe_write_text(dst, body)
        return dst

    # --- internals --------------------------------------------------------

    @staticmethod
    def _apply_bit_depth_policy(
        arr: np.ndarray,
        stored_bits: int,
        policy: str,
        already_warned: bool,
    ) -> tuple[np.ndarray, bool]:
        if arr.dtype == np.uint8:
            return arr, False
        if policy == "pass":
            return arr, False
        if policy == "fail":
            raise ValueError(
                f"non-uint8 input (dtype={arr.dtype}, bit_depth={stored_bits}) "
                "rejected by bit_depth_policy='fail'"
            )
        # downcast — right-shift so 12-in-16-bit (etc.) maps cleanly into uint8.
        shift = max(stored_bits - 8, 0)
        if shift > 0:
            downcast = (arr >> shift).astype(np.uint8)
        else:
            downcast = np.clip(arr, 0, 255).astype(np.uint8)
        warned = not already_warned
        if warned:
            log.warning(
                "Bit-depth downcast: %d-bit input → uint8 (shift %d)",
                stored_bits,
                shift,
            )
        return downcast, warned

    @staticmethod
    def _derive_scalars(
        im: Any,
    ) -> tuple[float | None, float | None, float | None]:
        """Voxel sizes + cycle time from a LifImage's `scale` tuple.

        readlif exposes ``scale = (1/voxel_x, 1/voxel_y, 1/voxel_z, 1/cycle_s)``
        with µm-reciprocals for XYZ and seconds-reciprocal for T (when
        present). ``None`` slots correspond to dimensions the acquisition
        didn't have.
        """
        sc = getattr(im, "scale", None)
        if sc is None:
            return None, None, None
        sx, sy, sz, st = (sc + (None, None, None, None))[:4]
        voxel_xy = (1.0 / sx) if (sx and sx > 0) else None
        voxel_z = (1.0 / sz) if (sz and sz > 0) else None
        cycle_s = (1.0 / st) if (st and st > 0) else None
        return voxel_xy, voxel_z, cycle_s

    def _derive_instrument(self, pos_el: Any) -> tuple[str, str]:
        """Pull objective name + microscope model out of a position's ATL."""
        atl = pos_el.find(".//ATLConfocalSettingDefinition")
        if atl is None:
            return "", ""
        obj = (atl.get("ObjectiveName") or "").strip()
        model = (atl.get("MicroscopeModel") or "").strip()
        return obj, model

    def _derive_channels(self, pos_el: Any) -> list[LifChannelInfo]:
        """Pull active-detector + matching-laser info out of the LIF XML.

        Mimics what :func:`embryodb.parsers.leica_metadata._extract_active_channels`
        does, but we don't need the full MicroscopyMetadata — just enough
        to display in the CLI/GUI picker (dye, wavelength, intensity,
        detector, gain).

        ``raw_index`` is the LIF's per-image channel index that
        :func:`readlif.reader.LifImage.get_frame` accepts as ``c=``. It
        corresponds to the ENUMERATION ORDER of active detectors in the
        XML (after de-duping by the detector's ``Channel`` attribute),
        NOT to the ``Channel`` attribute itself — Leica numbers physical
        detectors 1–N but only writes the active ones into the binary
        plane stream.
        """
        # Walk every ATL block (sequential scans put one active detector
        # per block). Dedup by the XML ``Channel`` attribute so the master
        # block's copy of a sequential-scan detector doesn't double-count.
        # Order by XML Channel ascending = the order Leica writes active
        # detector data into the binary stream.
        seen: dict[int, dict[str, Any]] = {}  # xml_channel → record
        for atl in pos_el.findall(".//ATLConfocalSettingDefinition"):
            det_list = atl.find("./DetectorList")
            if det_list is None:
                continue
            atl_aotf = atl.find("./AotfList")
            for det in det_list.findall("./Detector"):
                if (det.get("IsActive") or "0").strip() != "1":
                    continue
                ch_str = det.get("Channel") or ""
                if not ch_str.isdigit():
                    continue
                xml_ch = int(ch_str)
                if xml_ch in seen:
                    continue
                ref = det.find("./DetectionReferenceLine")
                wl = ref.get("LaserWavelength") if ref is not None else None
                intensity: float | None = None
                if atl_aotf is not None and wl:
                    for lls in atl_aotf.findall(".//LaserLineSetting"):
                        if lls.get("LaserLine") == wl:
                            try:
                                intensity = round(float(lls.get("IntensityDev", "0")), 1)
                            except ValueError:
                                pass
                            break
                try:
                    gain = round(float(det.get("Gain") or "0"), 1)
                except ValueError:
                    gain = None
                seen[xml_ch] = {
                    "dye_name": (det.get("DyeName") or "").strip(),
                    "laser_line_nm": int(wl) if wl and wl.isdigit() else None,
                    "laser_intensity_dev": intensity,
                    "detector_name": (det.get("Name") or det.get("Type") or "").strip(),
                    "detector_gain": gain,
                }
        # raw_index is the enumeration order (0-based) of active detectors
        # sorted by their XML Channel attribute — matches what
        # LifImage.get_frame(c=N) reads.
        out: list[LifChannelInfo] = []
        for raw_idx, xml_ch in enumerate(sorted(seen)):
            out.append(LifChannelInfo(raw_index=raw_idx, **seen[xml_ch]))
        return out


# ---------------------------------------------------------------------------
# Convenience: standalone inspect entrypoint for the CLI
# ---------------------------------------------------------------------------


def inspect_lif(lif_path: Path | str) -> list[LifSeriesInfo]:
    """Open a LIF file and return the per-series summary (no pixel reads)."""
    return LifExtractor(lif_path).list_series()


__all__ = [
    "ExtractionPlan",
    "ExtractionReport",
    "LifChannelInfo",
    "LifExtractor",
    "LifPositionInfo",
    "LifSeriesInfo",
    "PathFn",
    "inspect_lif",
]
