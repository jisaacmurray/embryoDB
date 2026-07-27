"""Re-run pipeline dialog.

Lets the user pick which worker steps to re-queue (run_starrynite,
run_red_extract, run_measure) and edit per-series matlabParams tunable keys
for one or more series. Replaces the old "Re-run StarryNite" dialog with a
step-selectable version so a Measure-only failure doesn't force re-running
the whole pipeline.

Typical use:
  - Select one or more series in the browser → right-click → Re-run pipeline…
  - Or click the Re-run pipeline… button in the detail panel

On Confirm:
  1. If run_starrynite is checked: for each series, read its <image_loc>/
     matlabParams, apply overrides, write back via fsutil.safe_write_text.
  2. Reset only the checked worker steps to PENDING (clears log_path,
     error_excerpt, timestamps).
  3. Spawn the worker if not already running.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from qtpy import QtCore, QtWidgets

from ..parsers.matlab_params import TUNABLE_KEYS, load as load_params
from ..pipeline.rerun import (
    default_steps as _default_steps,
    refresh_matlab_params,
    reset_step as _reset_one_step,
)
from ..pipeline.worker import WORKER_STEPS, spawn_worker

_STEP_LABEL = {
    "stage_images": "Stage images (re-extract TIFFs from raw acquisition)",
    "run_starrynite": "StarryNite (cell detection + tracking)",
    "compute_difficulty": "Editing difficulty (curation-effort prediction)",
    "run_red_extract": "Red Extract (reporter signal)",
    "run_measure": "Measure (morphometry)",
}

_STEP_TOOLTIP = {
    "stage_images": (
        "Off by default. Re-copies every raw TIFF into the staged tree — slow, "
        "and only needed if the staged images are missing or corrupt. The usual "
        "rerun re-traces already-staged images, so leave this unchecked unless "
        "you specifically need to restage."
    ),
}


class RerunPipelineDialog(QtWidgets.QDialog):
    """Select pipeline steps and edit matlabParams, then re-queue for one or
    more series.

    Args:
        session_cm: context-manager factory (same shape as MainWindow._session_cm)
        series_names: list of series_name strings to re-run
    """

    def __init__(
        self,
        session_cm: Callable,
        series_names: list[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._session_cm = session_cm
        self._series_names = list(series_names)
        self._series_data: list[dict] = []
        self.setWindowTitle("Re-run pipeline")
        self.setMinimumWidth(560)
        self._load_series_data()
        self._build()

    # --- data loading -------------------------------------------------------

    def _load_series_data(self) -> None:
        from ..queries.series import get_by_name
        with self._session_cm() as s:
            for name in self._series_names:
                row = get_by_name(s, name)
                if row is None:
                    continue
                runs_by_step = {r.step: r.status for r in row.runs}
                micro = row.microscopy
                self._series_data.append({
                    "id": row.id,
                    "name": row.series_name,
                    "image_loc": row.image_loc,
                    "timepts": row.timepts or "",
                    "params_path": (
                        str(Path(row.image_loc) / "matlabParams") if row.image_loc else None
                    ),
                    "voxel_xy_um": micro.voxel_xy_um if micro else None,
                    "voxel_z_um": micro.voxel_z_um if micro else None,
                    "runs": runs_by_step,
                })

    def _default_checked_steps(self) -> set[str]:
        """Default-checked steps, via the shared core so CLI and GUI agree:
        the earliest incomplete step across all series plus everything
        downstream, minus ``stage_images`` (never checked by default)."""
        return set(_default_steps([sd["runs"] for sd in self._series_data]))

    # --- UI -----------------------------------------------------------------

    def _build(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        n = len(self._series_data)
        if n == 0:
            layout.addWidget(QtWidgets.QLabel("No valid series found."))
            btn = QtWidgets.QPushButton("Close")
            btn.clicked.connect(self.reject)
            layout.addWidget(btn)
            return

        # Series summary
        if n == 1:
            summary = self._series_data[0]["name"]
        else:
            summary = f"{self._series_data[0]['name']} … (+{n - 1} more)"
        layout.addWidget(QtWidgets.QLabel(f"<b>Series:</b> {summary}"))
        if n > 1:
            note = QtWidgets.QLabel(
                "The same step selection and parameter overrides will be "
                "applied to all selected series."
            )
            note.setWordWrap(True)
            note.setStyleSheet("color: grey; font-size: 11px;")
            layout.addWidget(note)

        # Step checkboxes
        steps_box = QtWidgets.QGroupBox("Steps to re-queue")
        steps_vl = QtWidgets.QVBoxLayout(steps_box)
        default_steps = self._default_checked_steps()
        self._step_checks: dict[str, QtWidgets.QCheckBox] = {}
        for step in WORKER_STEPS:
            cb = QtWidgets.QCheckBox(_STEP_LABEL.get(step, step))
            cb.setChecked(step in default_steps)
            if step in _STEP_TOOLTIP:
                cb.setToolTip(_STEP_TOOLTIP[step])
            self._step_checks[step] = cb
            steps_vl.addWidget(cb)
        layout.addWidget(steps_box)

        # Parameter table — relevant only when run_starrynite is checked.
        params_box = QtWidgets.QGroupBox("StarryNite parameter overrides")
        params_vl = QtWidgets.QVBoxLayout(params_box)
        params_vl.addWidget(QtWidgets.QLabel(
            "Blank = keep current value. Applied only if StarryNite is checked above."
        ))

        # Engine picker: old (legacy compiled-MATLAB) vs new (all-MATLAB v1).
        # "Keep current" (None) leaves each series' stored choice intact.
        engine_row = QtWidgets.QHBoxLayout()
        engine_row.addWidget(QtWidgets.QLabel("StarryNite engine:"))
        self._engine_combo = QtWidgets.QComboBox()
        self._engine_combo.addItem("Keep current choice", None)
        self._engine_combo.addItem("Old (legacy compiled MATLAB)", "old")
        self._engine_combo.addItem("New (all-MATLAB v1)", "new")
        self._engine_combo.setToolTip(
            "Which StarryNite to use for this rerun.\n"
            "Keep current: leave each series' existing engine choice (defaults "
            "to old if never set).\n"
            "New runs full MATLAB with the retrained tracker and lands into the "
            "same slot; it also records an effort estimate on the run."
        )
        engine_row.addWidget(self._engine_combo, 1)
        params_vl.addLayout(engine_row)

        # Protocol picker: select an existing Protocol and bulk-populate the
        # override column from its defaults. Useful when retuning across
        # multiple acquisitions or comparing brightness presets.
        proto_row = QtWidgets.QHBoxLayout()
        proto_row.addWidget(QtWidgets.QLabel("Load defaults from protocol:"))
        self._proto_combo = QtWidgets.QComboBox()
        self._proto_combo.addItem("— select to apply —", None)
        try:
            from sqlalchemy import select
            from ..models import Protocol
            with self._session_cm() as s:
                for row in s.execute(select(Protocol).order_by(Protocol.name)).scalars():
                    self._proto_combo.addItem(row.name, row.defaults or {})
        except Exception:
            pass
        proto_row.addWidget(self._proto_combo, 1)
        self._proto_combo.currentIndexChanged.connect(self._on_protocol_picked)
        params_vl.addLayout(proto_row)
        copy_row = QtWidgets.QHBoxLayout()
        copy_btn = QtWidgets.QPushButton("Copy current → new")
        copy_btn.setToolTip(
            "Pre-fill all New value cells from the current file so you only\n"
            "need to edit the parameters you want to change."
        )
        copy_btn.clicked.connect(self._on_copy_current)
        copy_row.addWidget(copy_btn)
        copy_row.addStretch(1)
        params_vl.addLayout(copy_row)

        self._params_table = QtWidgets.QTableWidget(len(TUNABLE_KEYS), 3)
        self._params_table.setHorizontalHeaderLabels(["Key", "Current value", "New value"])
        self._params_table.horizontalHeader().setStretchLastSection(True)
        self._params_table.verticalHeader().setVisible(False)
        self._params_table.setMinimumHeight(220)

        current_vals: dict[str, str] = {}
        first = self._series_data[0] if self._series_data else None
        if first and first["params_path"] and Path(first["params_path"]).exists():
            try:
                p = load_params(Path(first["params_path"]))
                current_vals = {k: p.get(k, "") or "" for k in TUNABLE_KEYS}
            except Exception:
                pass
        if first:
            tp = first.get("timepts", "")
            if tp and str(tp).strip().isdigit():
                current_vals["end_time"] = str(tp).strip()
                if "start_time" not in current_vals or not current_vals["start_time"]:
                    current_vals["start_time"] = "1"

        self._current_vals = current_vals
        for row_idx, key in enumerate(TUNABLE_KEYS):
            key_item = QtWidgets.QTableWidgetItem(key)
            key_item.setFlags(key_item.flags() & ~QtCore.Qt.ItemIsEditable)
            self._params_table.setItem(row_idx, 0, key_item)
            cur_item = QtWidgets.QTableWidgetItem(current_vals.get(key, ""))
            cur_item.setFlags(cur_item.flags() & ~QtCore.Qt.ItemIsEditable)
            cur_item.setForeground(QtWidgets.QApplication.palette().mid())
            self._params_table.setItem(row_idx, 1, cur_item)
            self._params_table.setItem(row_idx, 2, QtWidgets.QTableWidgetItem(""))
        params_vl.addWidget(self._params_table)
        layout.addWidget(params_box)

        self._params_box = params_box  # for enable/disable based on SN checkbox
        self._step_checks["run_starrynite"].toggled.connect(self._params_box.setEnabled)
        self._params_box.setEnabled(self._step_checks["run_starrynite"].isChecked())

        if first and first["params_path"] and not Path(first["params_path"]).exists():
            warn = QtWidgets.QLabel(
                f"⚠  matlabParams not found at {first['params_path']}. "
                "Parameters cannot be pre-filled, but overrides will still be appended."
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #b8860b;")
            layout.addWidget(warn)

        # Scheduling: start the worker now, or just queue the rows for later.
        self._immediate_check = QtWidgets.QCheckBox(
            "Run immediately (start the background worker now)"
        )
        self._immediate_check.setChecked(True)
        self._immediate_check.setToolTip(
            "Checked (default): start the background worker as soon as you "
            "re-queue, so the selected steps run right away.\n"
            "Unchecked: only re-queue the rows; they'll be picked up the next "
            "time a worker runs (e.g. the next import)."
        )
        layout.addWidget(self._immediate_check)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        btns.button(QtWidgets.QDialogButtonBox.Ok).setText("Re-queue")
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    # --- action -------------------------------------------------------------

    def _on_copy_current(self) -> None:
        """Pre-fill every non-empty New value cell from the Current value column."""
        for row_idx in range(self._params_table.rowCount()):
            cur = self._params_table.item(row_idx, 1)
            new = self._params_table.item(row_idx, 2)
            if cur and new and cur.text():
                new.setText(cur.text())

    def _on_protocol_picked(self, idx: int) -> None:
        """Populate the Override column from the selected protocol's defaults.

        Empty values in the protocol skip the corresponding row. The user
        can still tweak individual overrides afterwards.
        """
        if idx <= 0:
            return
        defaults = self._proto_combo.itemData(idx) or {}
        if not isinstance(defaults, dict):
            return
        for row_idx, key in enumerate(TUNABLE_KEYS):
            val = defaults.get(key, "")
            if val == "" or val is None:
                continue
            item = self._params_table.item(row_idx, 2)
            if item is not None:
                item.setText(str(val))

    def _overrides(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for row_idx, key in enumerate(TUNABLE_KEYS):
            item = self._params_table.item(row_idx, 2)
            if item:
                val = item.text().strip()
                if val:
                    result[key] = val
        return result

    def _checked_steps(self) -> list[str]:
        return [s for s in WORKER_STEPS if self._step_checks[s].isChecked()]

    def _on_accept(self) -> None:
        steps_to_run = self._checked_steps()
        if not steps_to_run:
            QtWidgets.QMessageBox.warning(
                self, "No steps selected", "Please select at least one step to re-queue."
            )
            return
        sn_checked = "run_starrynite" in steps_to_run
        overrides = self._overrides() if sn_checked else {}
        sn_engine = self._engine_combo.currentData() if sn_checked else None
        errors: list[str] = []

        with self._session_cm() as s:
            for sd in self._series_data:
                # When StarryNite is re-queued, rewrite matlabParams to (1) refresh
                # xyres/zres from the DB microscopy metadata — the single source of
                # truth, matching step_write_matlab_params at import time — and (2)
                # apply any user overrides. The resolution refresh runs even with no
                # overrides, so a stale or hand-edited resolution gets corrected on
                # rerun instead of feeding the wrong value to StarryNite's tracer.
                if sn_checked:
                    params_path = sd["params_path"]
                    if params_path:
                        try:
                            refresh_matlab_params(
                                Path(params_path),
                                voxel_xy_um=sd.get("voxel_xy_um"),
                                voxel_z_um=sd.get("voxel_z_um"),
                                overrides=overrides,
                            )
                        except Exception as exc:
                            errors.append(f"{sd['name']}: {exc}")
                for step in steps_to_run:
                    step_params = (
                        {"sn_engine": sn_engine}
                        if step == "run_starrynite" and sn_engine is not None
                        else None
                    )
                    _reset_one_step(s, sd["id"], step, params=step_params)
            s.flush()

        if errors:
            QtWidgets.QMessageBox.warning(
                self,
                "Some updates failed",
                "Parameter update failed for:\n" + "\n".join(errors[:10]),
            )

        if self._immediate_check.isChecked():
            spawn_worker()
        self.accept()


# Back-compat alias (in case anything still imports the old name).
RerunStarryNiteDialog = RerunPipelineDialog

__all__ = ["RerunPipelineDialog", "RerunStarryNiteDialog"]
