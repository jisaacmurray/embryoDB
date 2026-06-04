"""Dialog showing per-series acquisition metadata.

Surfaces the "high-value" subset of what the import pipeline parses out of
the vendor metadata (today: Leica Stellaris ``Properties.xml``):

- **Channels** — one row per active detector channel, with the laser line
  + AOTF intensity that drove it, the detector name + gain + dye, and the
  spectral detection band. This is item 1+2 from the Phase 2 audit.
- **Depth compensation** — per-channel projected curve of
  ``(Z µm, AOTF intensity %, detector gain)`` points. Item 3 from the
  audit. Each active channel's curve is shown in its own small table so
  the user can spot the typical "intensity ramps up with depth to
  compensate for light loss" pattern at a glance.

Everything else the parser captures (bit depth, pixel dwell time, zoom,
scan geometry, programmed timing, instrument serial number — items 4-10)
is stored in ``MicroscopyMetadata.acquisition_settings`` for queryability
but intentionally NOT surfaced here. Use the SQL CLI or a future
"Advanced acquisition settings…" dialog for that.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from qtpy import QtCore, QtWidgets

from ..models import MicroscopyMetadata
from ..queries import series as q_series


def _fmt_num(value: Any, decimals: int = 2) -> str:
    """Render numeric values with a tunable decimal count; blank for None."""
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_with_range(value: Any, lo: Any, hi: Any, decimals: int = 1) -> str:
    """Render ``value`` with a ``(min–max)`` suffix when the range is wider
    than rounding noise. Used to surface ramped intensity/gain at a glance
    in the active-channels table without a separate column.
    """
    base = _fmt_num(value, decimals)
    try:
        lo_f = float(lo) if lo is not None else None
        hi_f = float(hi) if hi is not None else None
    except (TypeError, ValueError):
        return base
    if lo_f is None or hi_f is None:
        return base
    if abs(hi_f - lo_f) < 10 ** (-decimals):
        return base  # effectively flat after rounding
    return f"{base} ({_fmt_num(lo_f, decimals)}–{_fmt_num(hi_f, decimals)})"


class MicroscopyDialog(QtWidgets.QDialog):
    """Read-only view of one series's parsed microscopy metadata."""

    def __init__(
        self,
        session_cm: Callable,
        series_name: str,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_cm = session_cm
        self._series_name = series_name
        self.setWindowTitle(f"Microscopy details — {series_name}")
        self.resize(720, 560)
        self._build()
        self._populate()

    # --- UI -----------------------------------------------------------

    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)

        self._header = QtWidgets.QLabel("")
        self._header.setStyleSheet("color: #555;")
        self._header.setWordWrap(True)
        outer.addWidget(self._header)

        # Channels table (top half) — one row per active channel
        ch_box = QtWidgets.QGroupBox("Active channels")
        ch_layout = QtWidgets.QVBoxLayout(ch_box)
        ch_layout.setContentsMargins(6, 12, 6, 6)
        self._channels_table = QtWidgets.QTableWidget(0, 8)
        self._channels_table.setHorizontalHeaderLabels(
            ["Channel", "Dye", "Laser", "Line (nm)", "AOTF %", "Detector", "Gain", "Band"]
        )
        self._channels_table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeToContents
        )
        self._channels_table.horizontalHeader().setStretchLastSection(True)
        self._channels_table.verticalHeader().setVisible(False)
        self._channels_table.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers
        )
        self._channels_table.setSelectionMode(
            QtWidgets.QAbstractItemView.NoSelection
        )
        self._channels_table.setMinimumHeight(120)
        ch_layout.addWidget(self._channels_table)
        outer.addWidget(ch_box)

        # Depth compensation (bottom half) — scrollable area with one
        # small table per active channel so the user can see the curve
        # shape for each one independently.
        dc_box = QtWidgets.QGroupBox("Depth compensation")
        dc_box.setToolTip(
            "Per-channel programmed compensation curve: AOTF intensity (%) "
            "and detector gain at each Z position the microscope was set "
            "to interpolate between. Typical embryo imaging ramps both up "
            "with depth to offset light loss."
        )
        dc_layout = QtWidgets.QVBoxLayout(dc_box)
        dc_layout.setContentsMargins(6, 12, 6, 6)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        self._dc_container = QtWidgets.QWidget()
        self._dc_vbox = QtWidgets.QVBoxLayout(self._dc_container)
        self._dc_vbox.setContentsMargins(2, 2, 2, 2)
        self._dc_vbox.setSpacing(8)
        scroll.setWidget(self._dc_container)
        dc_layout.addWidget(scroll, 1)
        outer.addWidget(dc_box, 1)

        # Close button
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        outer.addLayout(btn_row)

    # --- population ---------------------------------------------------

    def _populate(self) -> None:
        with self._session_cm() as session:
            row = q_series.get_by_name(session, self._series_name)
            if row is None:
                self._header.setText("<i>Series not found.</i>")
                return
            md: MicroscopyMetadata | None = row.microscopy
            if md is None:
                self._header.setText(
                    "<i>No microscopy metadata captured for this series. "
                    "(Imported before v2.6, or the per-position "
                    "Properties.xml wasn't present at import time.)</i>"
                )
                return
            channels = md.channels or []
            compensation = md.depth_compensation or {}

        # Header summary — objective + instrument identity from the
        # acquisition_settings blob if available.
        bits = []
        if md.objective:
            na = f" NA {md.objective_NA}" if md.objective_NA else ""
            bits.append(f"<b>{md.objective}</b>{na}")
        settings = md.acquisition_settings or {}
        model = settings.get("microscope_model") or md.instrument
        if model:
            bits.append(model)
        if md.voxel_xy_um and md.voxel_z_um:
            bits.append(
                f"voxel {md.voxel_xy_um:.3f} × {md.voxel_xy_um:.3f} × "
                f"{md.voxel_z_um:.3f} µm"
            )
        if md.planes_per_volume:
            bits.append(f"{md.planes_per_volume} planes")
        self._header.setText(" · ".join(bits))

        # Channels table
        active = [c for c in channels if c.get("is_active")]
        self._channels_table.setRowCount(len(active))
        for r, ch in enumerate(active):
            is_transmission = ch.get("channel_type") == "transmission"
            self._channels_table.setItem(
                r, 0, QtWidgets.QTableWidgetItem(str(ch.get("channel_name") or ""))
            )
            # For transmission/DIC channels, the "dye" slot reads naturally
            # as the channel type — saves a row of blank cells.
            dye_text = (
                "(DIC / transmission)"
                if is_transmission
                else str(ch.get("dye_name") or "")
            )
            self._channels_table.setItem(r, 1, QtWidgets.QTableWidgetItem(dye_text))
            self._channels_table.setItem(
                r,
                2,
                QtWidgets.QTableWidgetItem(
                    "—" if is_transmission else str(ch.get("laser_name") or "")
                ),
            )
            self._channels_table.setItem(
                r,
                3,
                QtWidgets.QTableWidgetItem(
                    "—" if is_transmission else str(ch.get("laser_line_nm") or "")
                ),
            )
            self._channels_table.setItem(
                r,
                4,
                QtWidgets.QTableWidgetItem(
                    "—"
                    if is_transmission
                    else _fmt_with_range(
                        ch.get("laser_intensity_dev"),
                        ch.get("laser_intensity_dev_min"),
                        ch.get("laser_intensity_dev_max"),
                        decimals=1,
                    )
                ),
            )
            self._channels_table.setItem(
                r, 5, QtWidgets.QTableWidgetItem(str(ch.get("detector_name") or ""))
            )
            self._channels_table.setItem(
                r,
                6,
                QtWidgets.QTableWidgetItem(
                    _fmt_with_range(
                        ch.get("detector_gain"),
                        ch.get("detector_gain_min"),
                        ch.get("detector_gain_max"),
                        decimals=1,
                    )
                ),
            )
            self._channels_table.setItem(
                r, 7, QtWidgets.QTableWidgetItem(str(ch.get("band") or ""))
            )

        if not active:
            placeholder = QtWidgets.QTableWidgetItem(
                "(no active channels recorded)"
            )
            placeholder.setFlags(QtCore.Qt.NoItemFlags)
            self._channels_table.setRowCount(1)
            self._channels_table.setItem(0, 0, placeholder)
            self._channels_table.setSpan(0, 0, 1, 8)

        # Depth-compensation: one mini-table per active channel
        # Clear any prior children first (in case _populate is re-run).
        while self._dc_vbox.count():
            item = self._dc_vbox.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        per_channel = (compensation or {}).get("channels") or []
        axis = (compensation or {}).get("axis", "z_um")
        if not per_channel:
            empty = QtWidgets.QLabel(
                "<i>No depth-compensation curve recorded.</i>"
            )
            empty.setStyleSheet("color: #888;")
            self._dc_vbox.addWidget(empty)
        else:
            for entry in per_channel:
                self._dc_vbox.addWidget(
                    self._build_compensation_table(entry, axis=axis)
                )
            self._dc_vbox.addStretch(1)

    def _build_compensation_table(
        self, entry: dict[str, Any], *, axis: str = "z_um"
    ) -> QtWidgets.QWidget:
        """One section per channel: a small label + an (axis, intensity, gain)
        table. The axis column label and value-key depend on what the parser
        produced:

        - Stellaris uses ``z_um`` (programmed Z compensation points).
        - SP5 uses ``timepoint`` (intensity ramped across the developmental
          time course; constant within each Z stack).
        """
        container = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)

        ch_name = entry.get("channel_name") or "—"
        laser = entry.get("laser_line_nm")
        det = entry.get("detector_name") or "—"
        title = QtWidgets.QLabel(
            f"<b>{ch_name}</b> — laser {laser} nm, detector {det}"
        )
        v.addWidget(title)

        points = entry.get("points") or []
        # Pick the axis column based on what the parser emitted. Default to
        # Z (Stellaris) when the curve didn't declare an axis.
        if axis == "timepoint":
            axis_label, axis_key, axis_decimals = "Timepoint", "timepoint", 0
        else:
            axis_label, axis_key, axis_decimals = "Z (µm)", "z_um", 2

        table = QtWidgets.QTableWidget(len(points), 3)
        table.setHorizontalHeaderLabels([axis_label, "Intensity %", "Gain"])
        table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeToContents
        )
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        # Keep each per-channel table compact — most curves have 2-10 points.
        row_h = table.fontMetrics().height() + 6
        table.setMaximumHeight(row_h * min(len(points) + 1, 8) + 4)
        for r, p in enumerate(points):
            table.setItem(
                r, 0, QtWidgets.QTableWidgetItem(_fmt_num(p.get(axis_key), axis_decimals))
            )
            table.setItem(r, 1, QtWidgets.QTableWidgetItem(_fmt_num(p.get("intensity_dev"))))
            table.setItem(r, 2, QtWidgets.QTableWidgetItem(_fmt_num(p.get("gain"), 1)))
        v.addWidget(table)
        return container


__all__ = ["MicroscopyDialog"]
