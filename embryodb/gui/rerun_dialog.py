"""Re-run StarryNite dialog.

Lets the user edit per-series matlabParams tunable keys and re-queue the
subprocess steps (run_starrynite + run_red_extract + run_measure) for one
or more series from the same (or different) acquisitions.

Typical use:
  - Select one or more series in the browser → right-click → Re-run StarryNite…
  - Or click the Re-run StarryNite… button in the detail panel

On Confirm:
  1. For each series: read its <image_loc>/matlabParams, apply overrides,
     write back via fsutil.safe_write_text.
  2. Reset run_starrynite / run_red_extract / run_measure to PENDING (clears
     log_path, error_excerpt, timestamps).
  3. Spawn the worker if not already running.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from qtpy import QtCore, QtWidgets

from ..config import settings
from ..fsutil import safe_write_text
from ..models import PipelineStepRun, RunStatus
from ..parsers.matlab_params import TUNABLE_KEYS, load as load_params, render as render_params
from ..pipeline.worker import WORKER_STEPS, spawn_worker

_STEP_ORDER = {s: i for i, s in enumerate(WORKER_STEPS)}


def _reset_worker_steps(session, series_id: int) -> None:
    """Set all three worker steps back to PENDING for a series."""
    for step in WORKER_STEPS:
        run = (
            session.query(PipelineStepRun)
            .filter_by(series_id=series_id, step=step)
            .one_or_none()
        )
        if run is None:
            run = PipelineStepRun(series_id=series_id, step=step)
            session.add(run)
        run.status = RunStatus.PENDING
        run.started_at = None
        run.completed_at = None
        run.heartbeat_at = None
        run.error_excerpt = None
        run.log_path = None
        run.output_summary = {}
    session.flush()


class RerunStarryNiteDialog(QtWidgets.QDialog):
    """Edit matlabParams and re-queue StarryNite for one or more series.

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
        self._series_data: list[dict] = []  # id, name, image_loc, params_path
        self.setWindowTitle("Re-run StarryNite")
        self.setMinimumWidth(500)
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
                self._series_data.append({
                    "id": row.id,
                    "name": row.series_name,
                    "image_loc": row.image_loc,
                    "timepts": row.timepts or "",
                    "params_path": (
                        str(Path(row.image_loc) / "matlabParams") if row.image_loc else None
                    ),
                })

    # --- UI -----------------------------------------------------------------

    def _build(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)

        # Series list
        n = len(self._series_data)
        if n == 0:
            layout.addWidget(QtWidgets.QLabel("No valid series found."))
            btn = QtWidgets.QPushButton("Close")
            btn.clicked.connect(self.reject)
            layout.addWidget(btn)
            return

        if n == 1:
            summary = self._series_data[0]["name"]
        else:
            summary = f"{self._series_data[0]['name']} … (+{n - 1} more)"
        layout.addWidget(QtWidgets.QLabel(f"<b>Series:</b> {summary}"))
        if n > 1:
            names_label = QtWidgets.QLabel(
                "The same parameter overrides will be applied to all selected series."
            )
            names_label.setWordWrap(True)
            names_label.setStyleSheet("color: grey; font-size: 11px;")
            layout.addWidget(names_label)

        # Parameter table
        layout.addWidget(QtWidgets.QLabel("Parameter overrides (blank = keep current value):"))
        self._params_table = QtWidgets.QTableWidget(len(TUNABLE_KEYS), 3)
        self._params_table.setHorizontalHeaderLabels(["Key", "Current value", "New value"])
        self._params_table.horizontalHeader().setStretchLastSection(True)
        self._params_table.verticalHeader().setVisible(False)
        self._params_table.setMinimumHeight(220)

        # Pre-fill current values from the first series' matlabParams file.
        # end_time and start_time are also seeded from the DB timepts field
        # so the dialog always shows the correct acquisition length even if
        # the matlabParams file still has a stale value.
        current_vals: dict[str, str] = {}
        first = self._series_data[0] if self._series_data else None
        if first and first["params_path"] and Path(first["params_path"]).exists():
            try:
                p = load_params(Path(first["params_path"]))
                current_vals = {k: p.get(k, "") or "" for k in TUNABLE_KEYS}
            except Exception:
                pass
        # Override end_time from DB timepts if it's a valid integer.
        if first:
            tp = first.get("timepts", "")
            if tp and str(tp).strip().isdigit():
                current_vals["end_time"] = str(tp).strip()
                if "start_time" not in current_vals or not current_vals["start_time"]:
                    current_vals["start_time"] = "1"

        for row_idx, key in enumerate(TUNABLE_KEYS):
            key_item = QtWidgets.QTableWidgetItem(key)
            key_item.setFlags(key_item.flags() & ~QtCore.Qt.ItemIsEditable)
            self._params_table.setItem(row_idx, 0, key_item)
            cur = current_vals.get(key, "")
            cur_item = QtWidgets.QTableWidgetItem(cur)
            cur_item.setFlags(cur_item.flags() & ~QtCore.Qt.ItemIsEditable)
            cur_item.setForeground(
                QtWidgets.QApplication.palette().mid()
            )
            self._params_table.setItem(row_idx, 1, cur_item)
            self._params_table.setItem(row_idx, 2, QtWidgets.QTableWidgetItem(""))

        layout.addWidget(self._params_table)

        # Warning if no matlabParams file found
        if first and first["params_path"] and not Path(first["params_path"]).exists():
            warn = QtWidgets.QLabel(
                f"⚠  matlabParams not found at {first['params_path']}. "
                "Parameters cannot be pre-filled, but overrides will still be appended."
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #b8860b;")
            layout.addWidget(warn)

        # Button row
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        btns.button(QtWidgets.QDialogButtonBox.Ok).setText("Re-queue StarryNite")
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    # --- action -------------------------------------------------------------

    def _overrides(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for row_idx, key in enumerate(TUNABLE_KEYS):
            item = self._params_table.item(row_idx, 2)
            if item:
                val = item.text().strip()
                if val:
                    result[key] = val
        return result

    def _on_accept(self) -> None:
        overrides = self._overrides()
        errors: list[str] = []

        with self._session_cm() as s:
            for sd in self._series_data:
                params_path = sd["params_path"]
                if params_path:
                    pp = Path(params_path)
                    try:
                        params = load_params(pp) if pp.exists() else None
                        if params is not None and overrides:
                            for k, v in overrides.items():
                                params.set(k, v)
                            safe_write_text(pp, render_params(params))
                    except Exception as exc:
                        errors.append(f"{sd['name']}: {exc}")
                _reset_worker_steps(s, sd["id"])

        if errors:
            QtWidgets.QMessageBox.warning(
                self,
                "Some updates failed",
                "Parameter update failed for:\n" + "\n".join(errors[:10]),
            )

        spawn_worker()
        self.accept()


__all__ = ["RerunStarryNiteDialog"]
