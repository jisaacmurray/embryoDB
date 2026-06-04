"""Dialog for importing a Leica `.lif` file straight into embryoDB.

Wraps ``embryodb pipeline import-lif``: the user picks a LIF, one of its
series (TileScan N), a Protocol, and confirms the channel→role mapping;
Launch spawns the CLI **detached** (via
:func:`embryodb.external_tools.run_lif_import`) so the multi-hour TIF
extraction survives closing the dialog or the whole GUI.

Modelled on :class:`embryodb.gui.extract_dialog.ExtractDialog` for the
launch + log-polling pattern. The series + channel metadata is read with a
fast, pixel-free walk (:meth:`LifExtractor.list_series`) when the user
selects a file.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from qtpy import QtCore, QtWidgets

from ..external_tools import LaunchResult, run_lif_import

# Roles the user can assign per channel. role_subdir() maps histone→tif/,
# reporter→tifR/, anything else→tifC<n>/.
_ROLES = ["histone", "reporter", "extra"]

_PLANES_RE = re.compile(r":\s*(\d+)\s*/\s*(\d+)\s*planes")


class LifImportDialog(QtWidgets.QDialog):
    """Pick a LIF + series + protocol; launch a detached import."""

    def __init__(
        self,
        session_cm: Callable,
        *,
        default_dir: str = "/murrlab3/Images",
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_cm = session_cm
        self._default_dir = default_dir
        self.setWindowTitle("Import from LIF")
        self.setMinimumWidth(600)

        self._lif_path: Path | None = None
        self._series_list: list = []          # list[LifSeriesInfo]
        self._channel_combos: dict[int, QtWidgets.QComboBox] = {}
        self._launch_result: LaunchResult | None = None
        self._log_path: Path | None = None
        self._poll_timer: QtCore.QTimer | None = None

        self._build()

    # --- UI ----------------------------------------------------------------

    def _build(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)

        # File picker row
        file_row = QtWidgets.QHBoxLayout()
        self._file_edit = QtWidgets.QLineEdit()
        self._file_edit.setPlaceholderText("Path to a .lif file…")
        self._file_edit.setReadOnly(True)
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(self._on_browse)
        file_row.addWidget(QtWidgets.QLabel("LIF file:"))
        file_row.addWidget(self._file_edit, 1)
        file_row.addWidget(browse)
        layout.addLayout(file_row)

        # Series picker
        self._series_combo = QtWidgets.QComboBox()
        self._series_combo.setEnabled(False)
        self._series_combo.currentIndexChanged.connect(self._on_series_changed)
        layout.addWidget(QtWidgets.QLabel("Series (TileScan):"))
        layout.addWidget(self._series_combo)

        # Protocol picker
        self._protocol_combo = QtWidgets.QComboBox()
        self._protocol_combo.currentIndexChanged.connect(
            self._on_protocol_changed
        )
        layout.addWidget(QtWidgets.QLabel("Protocol:"))
        layout.addWidget(self._protocol_combo)

        # Channel role mapping (filled when a series is chosen)
        self._channels_box = QtWidgets.QGroupBox(
            "Channel → role (adjust if mislabeled)"
        )
        self._channels_vl = QtWidgets.QVBoxLayout(self._channels_box)
        layout.addWidget(self._channels_box)

        # Acquisition metadata fields
        meta = QtWidgets.QFormLayout()
        self._user_edit = QtWidgets.QLineEdit()
        self._person_edit = QtWidgets.QLineEdit()
        self._strain_edit = QtWidgets.QLineEdit()
        self._perturb_edit = QtWidgets.QLineEdit()
        self._reporter_edit = QtWidgets.QLineEdit()
        meta.addRow("User:", self._user_edit)
        meta.addRow("Person:", self._person_edit)
        meta.addRow("Strain:", self._strain_edit)
        meta.addRow("Perturbation:", self._perturb_edit)
        meta.addRow("Reporter:", self._reporter_edit)
        layout.addLayout(meta)

        # Advanced (collapsible-ish): bit depth, compression, overwrite
        adv = QtWidgets.QGroupBox("Advanced")
        adv.setCheckable(True)
        adv.setChecked(False)
        adv_form = QtWidgets.QFormLayout(adv)
        self._bit_depth_combo = QtWidgets.QComboBox()
        self._bit_depth_combo.addItems(["downcast", "pass", "fail"])
        adv_form.addRow("Bit-depth policy:", self._bit_depth_combo)
        self._no_compress_cb = QtWidgets.QCheckBox("Disable LZW compression")
        adv_form.addRow(self._no_compress_cb)
        self._overwrite_cb = QtWidgets.QCheckBox("Overwrite existing TIFs")
        adv_form.addRow(self._overwrite_cb)
        layout.addWidget(adv)

        # Status line
        self._status = QtWidgets.QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: #555;")
        layout.addWidget(self._status)

        # Buttons
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        self._ok_btn = btns.button(QtWidgets.QDialogButtonBox.Ok)
        self._ok_btn.setText("Launch")
        self._ok_btn.setEnabled(False)
        self._cancel_btn = btns.button(QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_launch)
        btns.rejected.connect(self.reject)
        self._view_log_btn = QtWidgets.QPushButton("View log")
        self._view_log_btn.setEnabled(False)
        self._view_log_btn.clicked.connect(self._view_log)
        btns.addButton(self._view_log_btn, QtWidgets.QDialogButtonBox.ActionRole)
        layout.addWidget(btns)

        self._populate_protocols()

    # --- population --------------------------------------------------------

    def _populate_protocols(self) -> None:
        from sqlalchemy import select

        from ..models import Protocol

        self._protocol_combo.clear()
        self._protocol_combo.addItem("— select —", None)
        try:
            with self._session_cm() as s:
                rows = list(
                    s.execute(select(Protocol).order_by(Protocol.name)).scalars()
                )
                for row in rows:
                    # Stash the channel_map on the item so role defaults are
                    # available without a second query.
                    self._protocol_combo.addItem(row.name, dict(row.channel_map or {}))
        except Exception as exc:  # noqa: BLE001
            self._status.setText(f"Could not load protocols: {exc}")

    def _on_browse(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Choose a .lif file", self._default_dir, "Leica LIF (*.lif)"
        )
        if not path:
            return
        self._file_edit.setText(path)
        self._lif_path = Path(path)
        self._load_series()

    def _load_series(self) -> None:
        """Read the LIF's series metadata (pixel-free) and fill the picker."""
        if self._lif_path is None:
            return
        from ..lif import LifExtractor

        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            self._series_list = LifExtractor(self._lif_path).list_series()
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.warning(
                self, "Could not read LIF", f"Failed to read series:\n{exc}"
            )
            return
        QtWidgets.QApplication.restoreOverrideCursor()

        self._series_combo.blockSignals(True)
        self._series_combo.clear()
        for s in self._series_list:
            p0 = s.positions[0] if s.positions else None
            label = (
                f"{s.name}  ({len(s.positions)} pos × "
                f"{p0.n_timepoints if p0 else 0} tp × "
                f"{p0.n_planes if p0 else 0} planes)"
            )
            self._series_combo.addItem(label, s.name)
        self._series_combo.setEnabled(True)
        self._series_combo.blockSignals(False)
        # Default to the largest series (most timepoints) — usually the main
        # acquisition rather than the setup/QC scans.
        if self._series_list:
            biggest = max(
                range(len(self._series_list)),
                key=lambda i: (
                    self._series_list[i].positions[0].n_timepoints
                    if self._series_list[i].positions else 0
                ),
            )
            self._series_combo.setCurrentIndex(biggest)
        self._on_series_changed()

    def _current_series(self):
        name = self._series_combo.currentData()
        return next((s for s in self._series_list if s.name == name), None)

    def _on_series_changed(self) -> None:
        self._rebuild_channel_rows()
        self._update_launch_enabled()

    def _on_protocol_changed(self) -> None:
        self._apply_protocol_roles()
        self._update_launch_enabled()

    def _rebuild_channel_rows(self) -> None:
        # Clear existing rows.
        while self._channels_vl.count():
            item = self._channels_vl.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._channel_combos.clear()

        series = self._current_series()
        if series is None:
            return
        for ch in series.channels:
            row = QtWidgets.QHBoxLayout()
            label = QtWidgets.QLabel(
                f"raw_ch {ch.raw_index}: {ch.dye_name or '(no dye)'} · "
                f"{ch.laser_line_nm} nm · {ch.detector_name or '?'}"
            )
            combo = QtWidgets.QComboBox()
            combo.addItems(_ROLES)
            row.addWidget(label, 1)
            row.addWidget(combo)
            container = QtWidgets.QWidget()
            container.setLayout(row)
            self._channels_vl.addWidget(container)
            self._channel_combos[ch.raw_index] = combo
        self._apply_protocol_roles()

    def _apply_protocol_roles(self) -> None:
        """Pre-fill each channel's role dropdown from the protocol's map."""
        channel_map = self._protocol_combo.currentData() or {}
        for raw_idx, combo in self._channel_combos.items():
            role = channel_map.get(str(raw_idx))
            if role in _ROLES:
                combo.setCurrentText(role)
            elif role:
                # Unknown role string → treat as extra.
                combo.setCurrentText("extra")

    def _update_launch_enabled(self) -> None:
        ok = (
            self._lif_path is not None
            and self._current_series() is not None
            and self._protocol_combo.currentData() is not None
            and self._launch_result is None
        )
        self._ok_btn.setEnabled(ok)

    # --- launch + log ------------------------------------------------------

    def _on_launch(self) -> None:
        series = self._current_series()
        protocol = self._protocol_combo.currentText()
        if series is None or self._protocol_combo.currentData() is None:
            QtWidgets.QMessageBox.information(
                self, "Incomplete", "Pick a series and a protocol first."
            )
            return
        channel_roles = {
            raw_idx: combo.currentText()
            for raw_idx, combo in self._channel_combos.items()
        }
        # Confirm — extraction is long and irreversible-ish (writes lots of
        # files). Surface the channel mapping one more time.
        mapping = "\n".join(
            f"  raw_ch {i} → {r}" for i, r in sorted(channel_roles.items())
        )
        confirm = QtWidgets.QMessageBox.question(
            self, "Launch import?",
            f"Import series {series.name!r} from\n{self._lif_path}\n\n"
            f"Channel mapping:\n{mapping}\n\n"
            "Extraction can take hours; it runs detached so you can close "
            "this dialog. Proceed?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if confirm != QtWidgets.QMessageBox.Yes:
            return

        try:
            result = run_lif_import(
                self._lif_path,
                series.name,
                protocol,
                user=self._user_edit.text().strip() or None,
                person=self._person_edit.text().strip(),
                strain=self._strain_edit.text().strip(),
                perturbation=self._perturb_edit.text().strip(),
                reporter=self._reporter_edit.text().strip(),
                channel_roles=channel_roles,
                bit_depth_policy=self._bit_depth_combo.currentText(),
                no_compress=self._no_compress_cb.isChecked(),
                overwrite=self._overwrite_cb.isChecked(),
            )
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(
                self, "Launch failed", f"Could not spawn import:\n{exc}"
            )
            return

        self._launch_result = result
        self._log_path = result.log_path
        self._ok_btn.setEnabled(False)
        self._view_log_btn.setEnabled(True)
        # The job is detached now — closing this dialog won't stop it.
        self._cancel_btn.setText("Close")
        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.setInterval(2000)
        self._poll_timer.timeout.connect(self._poll_log)
        self._poll_timer.start()
        self._poll_log()

    def _poll_log(self) -> None:
        if self._launch_result is None or self._log_path is None:
            return
        try:
            content = self._log_path.read_text(errors="replace")
        except OSError:
            return

        proc_alive = self._launch_result.proc.poll() is None
        matches = _PLANES_RE.findall(content)
        if matches:
            done, total = matches[-1]
            progress = f"{done}/{total} planes"
        else:
            progress = "starting…"

        if proc_alive:
            msg = f"<b>Running</b> (pid {self._launch_result.proc.pid}) — {progress}"
        else:
            rc = self._launch_result.proc.returncode
            if rc == 0:
                msg = "<b>Complete</b> — import finished."
            else:
                msg = f"<b>Stopped</b> (exit {rc}) — check the log for errors."
            if self._poll_timer is not None:
                self._poll_timer.stop()

        self._status.setText(
            f"{msg}<br><b>Log:</b> <code>{self._log_path}</code>"
        )

    def _view_log(self) -> None:
        if self._log_path is None:
            return
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"Log — {self._log_path.name}")
        dlg.resize(800, 500)
        vl = QtWidgets.QVBoxLayout(dlg)
        view = QtWidgets.QPlainTextEdit()
        view.setReadOnly(True)
        view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        try:
            view.setPlainText(self._log_path.read_text(errors="replace"))
        except OSError as exc:
            view.setPlainText(f"Could not read log:\n{exc}")
        vl.addWidget(view)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(
            lambda: view.setPlainText(
                self._log_path.read_text(errors="replace") if self._log_path else ""
            )
        )
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(dlg.accept)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(refresh)
        row.addStretch(1)
        row.addWidget(close)
        vl.addLayout(row)
        dlg.exec()


__all__ = ["LifImportDialog"]
