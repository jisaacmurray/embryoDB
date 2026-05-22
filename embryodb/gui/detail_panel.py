"""Detail / edit panel for a single Series.

Shows the 16 XML fields plus provenance/version metadata. Editable fields
write back through optimistic locking: the Save button compares the row's
current `version` to the version loaded into the form, and if they differ
the user gets a conflict dialog instead of a silent overwrite.

For v1, controlled-vocabulary fields are presented as comboboxes that
auto-complete from distinct values but accept any text (no normalization
on save — raw values are preserved).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from qtpy import QtCore, QtWidgets
from sqlalchemy.orm import Session

from .. import external
from ..models import RunStatus, Series, Status
from ..queries import series as q_series
from .acetree_config_dialog import AceTreeConfigDialog


# Field id -> display label. Order is the same as XML / Java app for familiarity.
EDIT_FIELDS: list[tuple[str, str, str]] = [
    # (column, label, kind: 'line' | 'multi' | 'combo' | 'status')
    ("series_name", "Series", "line"),
    ("date_acquired", "Date", "line"),
    ("person", "Person", "combo"),
    ("strain_name", "Strain", "combo"),
    ("treatments", "Perturbation", "combo"),
    ("reporter_gene", "Reporter", "combo"),
    ("image_loc", "Image loc", "line"),
    ("timepts", "Timepts", "line"),
    ("annot_loc", "Annot loc", "line"),
    ("acetree_config", "AceTree config", "line"),
    ("edited_by", "Edited by", "combo"),
    ("edited_timepts", "Edited tpts", "line"),
    ("edited_cells", "Edited cells", "line"),
    ("partial_editing_code", "Partial editing code", "line"),
    ("comments", "Comments", "multi"),
    ("status", "Status", "status"),
]

# Fields that link to files on disk or downstream tools — warn before saving.
PROTECTED_FIELDS: frozenset[str] = frozenset(
    {"series_name", "image_loc", "annot_loc", "date_acquired", "timepts"}
)

PROVENANCE_FIELDS: list[tuple[str, str]] = [
    ("version", "Version"),
    ("updated_at", "Updated at"),
    ("updated_by", "Updated by"),
    ("xml_source_path", "XML source"),
    ("xml_hash", "XML hash"),
    ("imported_at", "Imported at"),
    ("imported_by", "Imported by"),
]


class DetailPanel(QtWidgets.QWidget):
    saved = QtCore.Signal(str)  # emits series_name on successful save
    datasetActivationRequested = QtCore.Signal(str)  # emits dataset_name from Member-of double-click

    def __init__(
        self,
        session_factory: Callable[[], Session],
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_factory = session_factory
        self._series_name: str | None = None
        self._loaded_version: int | None = None
        self._widgets: dict[str, QtWidgets.QWidget] = {}
        self._dirty: bool = False
        self._build()

    # --- dirty state ------------------------------------------------------

    def is_dirty(self) -> bool:
        """True if the user has typed edits that haven't been saved."""
        return self._dirty and self._series_name is not None

    def loaded_series_name(self) -> str | None:
        return self._series_name

    # --- public API -------------------------------------------------------

    def load_series(self, series_name: str) -> None:
        """Populate the form from the latest DB state of `series_name`."""
        self._series_name = series_name
        with self._session_factory() as session:
            row = q_series.get_by_name(session, series_name)
            if row is None:
                self._clear()
                self._title.setText(f"(no such series: {series_name})")
                return
            self._populate_from_row(row, session)

    def populate_combo_choices(self, session: Session) -> None:
        """Refresh the auto-complete suggestions for combo fields."""
        for col, _, kind in EDIT_FIELDS:
            if kind != "combo":
                continue
            combo = self._widgets[col]
            assert isinstance(combo, QtWidgets.QComboBox)
            current = combo.currentText()
            combo.clear()
            combo.addItems(q_series.distinct_values(session, col))
            combo.setCurrentText(current)

    # --- UI construction --------------------------------------------------

    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)

        self._title = QtWidgets.QLabel("(select a series)")
        title_font = self._title.font()
        title_font.setBold(True)
        title_font.setPointSize(title_font.pointSize() + 2)
        self._title.setFont(title_font)
        outer.addWidget(self._title)

        # Two-column body: left = editable fields (with Comments as the last,
        # tall row); right = Provenance + Member-of. Splitter so the user can
        # rebalance.
        body_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        outer.addWidget(body_splitter, 1)

        # --- left column: editable fields --------------------------------
        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(2, 2, 2, 2)
        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignRight)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        for column, label, kind in EDIT_FIELDS:
            widget = self._make_widget(kind)
            self._widgets[column] = widget
            form.addRow(label + ":", widget)
        left_layout.addLayout(form)
        left_layout.addStretch(1)
        body_splitter.addWidget(left)

        # --- right column: provenance + member-of ------------------------
        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(2, 2, 2, 2)

        ds_box = QtWidgets.QGroupBox("Member of")
        ds_layout = QtWidgets.QVBoxLayout(ds_box)
        self._datasets_list = QtWidgets.QListWidget()
        self._datasets_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SingleSelection
        )
        self._datasets_list.setToolTip(
            "Datasets that include this series. Double-click a name to "
            "surface that dataset in the dataset bar above."
        )
        self._datasets_list.itemDoubleClicked.connect(self._on_dataset_dblclick)
        ds_layout.addWidget(self._datasets_list)
        right_layout.addWidget(ds_box)

        prov_box = QtWidgets.QGroupBox("Provenance")
        prov_layout = QtWidgets.QFormLayout(prov_box)
        prov_layout.setLabelAlignment(QtCore.Qt.AlignRight)
        self._prov_widgets: dict[str, QtWidgets.QLabel] = {}
        for column, label in PROVENANCE_FIELDS:
            value_label = QtWidgets.QLabel("")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            self._prov_widgets[column] = value_label
            prov_layout.addRow(label + ":", value_label)
        right_layout.addWidget(prov_box)

        # Pipeline steps table
        pipe_box = QtWidgets.QGroupBox("Pipeline")
        pipe_vl = QtWidgets.QVBoxLayout(pipe_box)
        self._pipeline_table = QtWidgets.QTableWidget(0, 3)
        self._pipeline_table.setHorizontalHeaderLabels(["Step", "Status", ""])
        self._pipeline_table.horizontalHeader().setStretchLastSection(False)
        self._pipeline_table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeToContents
        )
        self._pipeline_table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeToContents
        )
        self._pipeline_table.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.Stretch
        )
        self._pipeline_table.verticalHeader().setVisible(False)
        self._pipeline_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self._pipeline_table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self._pipeline_table.setMinimumHeight(120)
        pipe_vl.addWidget(self._pipeline_table)

        self._rerun_btn = QtWidgets.QPushButton("Re-run pipeline…")
        self._rerun_btn.setEnabled(False)
        self._rerun_btn.clicked.connect(self._on_rerun_pipeline)
        pipe_vl.addWidget(self._rerun_btn)

        right_layout.addWidget(pipe_box)
        right_layout.addStretch(1)
        body_splitter.addWidget(right)

        body_splitter.setStretchFactor(0, 2)
        body_splitter.setStretchFactor(1, 1)

        self._dirty_label = QtWidgets.QLabel("")
        outer.addWidget(self._dirty_label)

        # External-tool launcher + Save / Reload buttons
        button_row = QtWidgets.QHBoxLayout()
        self._acetree_button = QtWidgets.QPushButton("Launch AceTree")
        self._acetree_button.setToolTip(
            "Spawn the legacy AceTree JAR against this series' config file. "
            "Equivalent to the old EmbryoDB.jar 'AceTreeX' button."
        )
        self._acetree_button.clicked.connect(self._on_launch_acetree)
        button_row.addWidget(self._acetree_button)

        self._edit_acetree_button = QtWidgets.QPushButton("Edit AceTree config…")
        self._edit_acetree_button.setToolTip(
            "Edit the per-series AceTree XML at <annot_loc>/dats/<series>.xml — "
            "start/end timepoint, axis convention, voxel resolution."
        )
        self._edit_acetree_button.clicked.connect(self._on_edit_acetree_config)
        button_row.addWidget(self._edit_acetree_button)

        self._edit_auxinfo_button = QtWidgets.QPushButton("Edit AuxInfo…")
        self._edit_auxinfo_button.setToolTip(
            "Edit the per-series AuxInfo CSV at <annot_loc>/dats/<series>AuxInfo.csv — "
            "ellipse center/axes/rotation, Z scaling, axis convention (ADL/AVR/PVL/PDR/XXX). "
            "Generated by Measure; edit when the fit is off."
        )
        self._edit_auxinfo_button.clicked.connect(self._on_edit_auxinfo)
        button_row.addWidget(self._edit_auxinfo_button)

        self._delete_button = QtWidgets.QPushButton("Mark for deletion")
        self._delete_button.setToolTip(
            "Flag this series for deletion. Image data stays on disk until "
            "`embryodb gc-deleted` runs (default 30-day grace). "
            "Metadata (dats/, AuxInfo, edit.zip) is preserved indefinitely."
        )
        self._delete_button.clicked.connect(self._on_toggle_delete)
        button_row.addWidget(self._delete_button)

        button_row.addStretch(1)
        self._reload_button = QtWidgets.QPushButton("Reload")
        self._reload_button.clicked.connect(self._on_reload)
        button_row.addWidget(self._reload_button)
        self._save_button = QtWidgets.QPushButton("Save")
        self._save_button.clicked.connect(self._on_save)
        self._save_button.setDefault(True)
        button_row.addWidget(self._save_button)
        outer.addLayout(button_row)
        outer.addStretch(1)

        self._set_enabled(False)

    def _make_widget(self, kind: str) -> QtWidgets.QWidget:
        if kind == "line":
            w = QtWidgets.QLineEdit()
            w.textEdited.connect(self._on_dirty)
            return w
        if kind == "multi":
            w = QtWidgets.QPlainTextEdit()
            w.setFixedHeight(72)
            w.textChanged.connect(self._on_dirty)
            return w
        if kind == "combo":
            w = QtWidgets.QComboBox()
            w.setEditable(True)
            w.lineEdit().textEdited.connect(self._on_dirty)
            return w
        if kind == "status":
            w = QtWidgets.QComboBox()
            w.addItems([s.value for s in Status])
            w.currentIndexChanged.connect(lambda _idx: self._on_dirty())
            return w
        raise ValueError(f"unknown widget kind: {kind}")

    # --- population / save ------------------------------------------------

    def _populate_pipeline_table(self, row: Series) -> None:
        from ..pipeline.orchestrate import STEPS
        runs_by_step = {r.step: r for r in row.runs}
        has_any = bool(runs_by_step)
        self._pipeline_table.setRowCount(len(STEPS))
        _STATUS_COLOR = {
            RunStatus.COMPLETE: "#2a8a2a",
            RunStatus.FAILED: "#b00000",
            RunStatus.RUNNING: "#0050b0",
            RunStatus.SKIPPED: "#888888",
            RunStatus.PENDING: "#555555",
        }
        for r_idx, step in enumerate(STEPS):
            run = runs_by_step.get(step)
            status = run.status if run else None
            short = step.replace("write_", "").replace("stage_", "stage/").replace("create_", "")
            step_item = QtWidgets.QTableWidgetItem(short)
            self._pipeline_table.setItem(r_idx, 0, step_item)

            if status is None:
                status_item = QtWidgets.QTableWidgetItem("—")
            else:
                glyphs = {
                    RunStatus.COMPLETE: "✓", RunStatus.FAILED: "✗",
                    RunStatus.RUNNING: "⟳", RunStatus.SKIPPED: "–",
                    RunStatus.PENDING: "·",
                }
                status_item = QtWidgets.QTableWidgetItem(
                    f"{glyphs.get(status, '?')} {status.value}"
                )
                color = _STATUS_COLOR.get(status)
                if color:
                    status_item.setForeground(
                        QtWidgets.QApplication.palette().text()
                        if status == RunStatus.PENDING
                        else __import__("qtpy.QtGui", fromlist=["QColor"]).QColor(color)
                    )
            self._pipeline_table.setItem(r_idx, 1, status_item)

            # Log button for steps with a log file
            if run and run.log_path and Path(run.log_path).exists():
                log_btn = QtWidgets.QPushButton("Log")
                log_btn.setMaximumWidth(42)
                log_btn.setFlat(True)
                log_path = run.log_path
                log_btn.clicked.connect(lambda _, p=log_path: self._open_log(p))
                self._pipeline_table.setCellWidget(r_idx, 2, log_btn)
            elif run and run.error_excerpt:
                err_item = QtWidgets.QTableWidgetItem(run.error_excerpt[:80])
                err_item.setForeground(
                    __import__("qtpy.QtGui", fromlist=["QColor"]).QColor("#b00000")
                )
                err_item.setToolTip(run.error_excerpt)
                self._pipeline_table.setItem(r_idx, 2, err_item)
            else:
                self._pipeline_table.setItem(r_idx, 2, QtWidgets.QTableWidgetItem(""))

        # Enable Re-run StarryNite if any worker step exists
        has_sn = "run_starrynite" in runs_by_step
        self._rerun_btn.setEnabled(has_sn)

    def _open_log(self, log_path: str) -> None:
        """Open a log file in a simple read-only viewer dialog."""
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"Log — {Path(log_path).name}")
        dlg.resize(700, 500)
        layout = QtWidgets.QVBoxLayout(dlg)
        text_edit = QtWidgets.QPlainTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        try:
            text_edit.setPlainText(Path(log_path).read_text(errors="replace"))
        except OSError as exc:
            text_edit.setPlainText(f"Could not read log:\n{exc}")
        layout.addWidget(text_edit)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn)
        dlg.exec()

    def _on_toggle_delete(self) -> None:
        """Toggle the soft-delete flag on the loaded series."""
        if not self._series_name:
            return
        from datetime import datetime, timezone
        from ..config import settings
        with self._session_factory() as session:
            row = q_series.get_by_name(session, self._series_name)
            if row is None:
                return
            if row.deleted_at is None:
                reply = QtWidgets.QMessageBox.question(
                    self,
                    "Mark for deletion?",
                    f"Mark {row.series_name!r} for deletion?\n\n"
                    "Image data will be removed after the 30-day grace period\n"
                    "when `embryodb gc-deleted` next runs. Metadata is preserved.\n"
                    "You can Restore at any time before then.",
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No,
                )
                if reply != QtWidgets.QMessageBox.Yes:
                    return
                row.deleted_at = datetime.now(tz=timezone.utc)
                row.deleted_by = settings.user
                row.status = Status.DEL1
            else:
                row.deleted_at = None
                row.deleted_by = None
                if row.status == Status.DEL1:
                    row.status = Status.NEW
            row.version = (row.version or 0) + 1
            session.flush()
        self.load_series(self._series_name)
        self.saved.emit(self._series_name)

    def _on_dataset_dblclick(self, item: QtWidgets.QListWidgetItem) -> None:
        """Emit a request to switch the dataset bar to the double-clicked dataset."""
        if item is None:
            return
        name = item.text()
        # Skip the "(not in any dataset)" placeholder
        if not (item.flags() & QtCore.Qt.ItemIsEnabled):
            return
        if name:
            self.datasetActivationRequested.emit(name)

    def _on_rerun_pipeline(self) -> None:
        if not self._series_name:
            return
        from .rerun_dialog import RerunPipelineDialog
        dlg = RerunPipelineDialog(
            self._session_factory, [self._series_name], parent=self
        )
        if dlg.exec():
            self.load_series(self._series_name)

    def _clear(self) -> None:
        for column, _, kind in EDIT_FIELDS:
            self._set_value(column, "", kind)
        for label in self._prov_widgets.values():
            label.setText("")
        self._datasets_list.clear()
        self._pipeline_table.setRowCount(0)
        self._rerun_btn.setEnabled(False)
        self._set_enabled(False)
        self._loaded_version = None
        self._dirty = False
        self._dirty_label.setText("")

    def _populate_from_row(self, row: Series, session: Session) -> None:
        self._title.setText(row.series_name)
        for column, _, kind in EDIT_FIELDS:
            value = getattr(row, column)
            if isinstance(value, Status):
                value = value.value
            self._set_value(column, value or "", kind)
        for column, _ in PROVENANCE_FIELDS:
            value = getattr(row, column, None)
            self._prov_widgets[column].setText(str(value) if value is not None else "")
        self._datasets_list.clear()
        dataset_names = sorted(d.name for d in row.datasets)
        if dataset_names:
            for name in dataset_names:
                self._datasets_list.addItem(name)
        else:
            placeholder = QtWidgets.QListWidgetItem("(not in any dataset)")
            placeholder.setFlags(QtCore.Qt.NoItemFlags)
            self._datasets_list.addItem(placeholder)
        self._loaded_version = row.version
        self._populate_pipeline_table(row)
        self._update_delete_button(row)
        self._set_enabled(True)
        self._dirty = False
        self._dirty_label.setText("")
        # Refresh combo suggestions
        self.populate_combo_choices(session)

    def _update_delete_button(self, row: Series) -> None:
        if row.deleted_at is not None:
            self._delete_button.setText(f"Restore (marked {row.deleted_at:%Y-%m-%d})")
        else:
            self._delete_button.setText("Mark for deletion")

    def _set_value(self, column: str, value: str, kind: str) -> None:
        w = self._widgets[column]
        w.blockSignals(True)
        try:
            if kind in ("line",):
                assert isinstance(w, QtWidgets.QLineEdit)
                w.setText(value)
            elif kind == "multi":
                assert isinstance(w, QtWidgets.QPlainTextEdit)
                w.setPlainText(value)
            elif kind in ("combo", "status"):
                assert isinstance(w, QtWidgets.QComboBox)
                if kind == "status":
                    idx = w.findText(value)
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                else:
                    w.setEditText(value)
        finally:
            w.blockSignals(False)

    def _get_value(self, column: str, kind: str) -> str:
        w = self._widgets[column]
        if kind == "line":
            return w.text()
        if kind == "multi":
            return w.toPlainText()
        if kind in ("combo", "status"):
            return w.currentText()
        raise ValueError(kind)

    def _set_enabled(self, enabled: bool) -> None:
        for w in self._widgets.values():
            w.setEnabled(enabled)
        self._save_button.setEnabled(enabled)
        self._reload_button.setEnabled(enabled)
        self._acetree_button.setEnabled(enabled)
        self._edit_acetree_button.setEnabled(enabled)
        self._edit_auxinfo_button.setEnabled(enabled)
        self._delete_button.setEnabled(enabled)

    def _on_dirty(self, *_args: object) -> None:
        self._dirty = True
        self._dirty_label.setText("• unsaved changes")

    def _on_reload(self) -> None:
        if self._series_name:
            self.load_series(self._series_name)

    def _on_save(self) -> None:
        if not self._series_name:
            return
        with self._session_factory() as session:
            row = q_series.get_by_name(session, self._series_name)
            if row is None:
                QtWidgets.QMessageBox.warning(
                    self, "Save failed", f"Series {self._series_name!r} no longer exists."
                )
                return
            if self._loaded_version is not None and row.version != self._loaded_version:
                self._handle_conflict(row)
                return
            # Warn before saving changes to fields that affect disk paths or
            # downstream tools.
            changed_protected = [
                label
                for column, label, kind in EDIT_FIELDS
                if column in PROTECTED_FIELDS
                and str(getattr(row, column) or "") != self._get_value(column, kind)
            ]
            if changed_protected:
                reply = QtWidgets.QMessageBox.warning(
                    self,
                    "Sensitive field changed",
                    "You've modified:\n  " + "\n  ".join(changed_protected) + "\n\n"
                    "These fields link to files on disk and are used by AceTree, "
                    "StarryNite, and the legacy embryoDB XML. Saving an incorrect "
                    "value can break those tools.\n\nSave anyway?",
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No,
                )
                if reply != QtWidgets.QMessageBox.Yes:
                    return
            for column, _, kind in EDIT_FIELDS:
                value = self._get_value(column, kind)
                if column == "status":
                    setattr(row, column, Status(value))
                else:
                    setattr(row, column, value)
            row.version += 1
            session.flush()
            new_version = row.version
        self._loaded_version = new_version
        self._dirty = False
        self._dirty_label.setText(f"saved (version {new_version})")
        self.saved.emit(self._series_name)
        self.load_series(self._series_name)  # refresh provenance fields

    def _on_edit_acetree_config(self) -> None:
        if not self._series_name:
            return
        with self._session_factory() as session:
            row = q_series.get_by_name(session, self._series_name)
            if row is None or not row.annot_loc or not row.acetree_config:
                QtWidgets.QMessageBox.warning(
                    self, "AceTree config",
                    "Series has no annot_loc/acetree_config recorded."
                )
                return
            cfg_path = Path(row.annot_loc) / "dats" / row.acetree_config
        if not cfg_path.exists():
            QtWidgets.QMessageBox.warning(
                self, "AceTree config",
                f"Config file not found:\n{cfg_path}"
            )
            return
        dlg = AceTreeConfigDialog(cfg_path, self)
        dlg.exec()

    def _on_edit_auxinfo(self) -> None:
        if not self._series_name:
            return
        with self._session_factory() as session:
            row = q_series.get_by_name(session, self._series_name)
            if row is None or not row.annot_loc:
                QtWidgets.QMessageBox.warning(
                    self, "AuxInfo", "Series has no annot_loc recorded."
                )
                return
            aux_path = Path(row.annot_loc) / "dats" / f"{row.series_name}AuxInfo.csv"
        if not aux_path.exists():
            reply = QtWidgets.QMessageBox.question(
                self, "AuxInfo not found",
                f"{aux_path}\ndoes not exist yet — Measure normally writes it.\n\n"
                "Create a blank file to edit?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply != QtWidgets.QMessageBox.Yes:
                return
            from .auxinfo_dialog import AuxInfo, render_auxinfo
            from ..fsutil import ensure_dir, safe_write_text
            ensure_dir(aux_path.parent)
            blank = AuxInfo(name=row.series_name)
            safe_write_text(aux_path, render_auxinfo(blank))
        from .auxinfo_dialog import AuxInfoDialog
        dlg = AuxInfoDialog(aux_path, self)
        dlg.exec()

    def _on_launch_acetree(self) -> None:
        if not self._series_name:
            return
        with self._session_factory() as session:
            row = q_series.get_by_name(session, self._series_name)
            if row is None:
                QtWidgets.QMessageBox.warning(
                    self, "AceTree", f"Series {self._series_name!r} not found."
                )
                return
            # Pull values out before the session closes; Popen happens outside.
            row_snapshot = row
            try:
                proc = external.launch_acetree(row_snapshot)
            except external.LaunchError as exc:
                QtWidgets.QMessageBox.warning(self, "AceTree launch failed", str(exc))
                return
        self._dirty_label.setText(f"AceTree launched (pid {proc.pid})")

    def _handle_conflict(self, db_row: Series) -> None:
        msg = QtWidgets.QMessageBox(self)
        msg.setIcon(QtWidgets.QMessageBox.Warning)
        msg.setWindowTitle("Save conflict")
        msg.setText(
            f"This series was modified by someone else (loaded v{self._loaded_version}, "
            f"current v{db_row.version}). Your changes were NOT saved."
        )
        msg.setInformativeText(
            "Choose 'Reload' to discard your edits and see the current data, "
            "or 'Cancel' to keep your edits in the form and reconcile manually."
        )
        reload_btn = msg.addButton("Reload", QtWidgets.QMessageBox.AcceptRole)
        msg.addButton("Cancel", QtWidgets.QMessageBox.RejectRole)
        msg.exec()
        if msg.clickedButton() is reload_btn:
            self.load_series(self._series_name or "")
