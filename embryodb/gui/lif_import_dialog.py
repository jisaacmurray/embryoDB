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
from ..identity import current_user, known_persons, system_users, user_has_image_dir

# Roles the user can assign per channel. role_subdir() maps histone→tif/,
# reporter→tifR/, anything else→tifC<n>/.
_ROLES = ["histone", "reporter", "extra"]
# Roles that stage into a single fixed dir (tif/ resp. tifR/), so at most one
# channel may hold each — two would collide on disk. ``extra`` is exempt
# (each lands in its own tifC<n>/).
_EXCLUSIVE_ROLES = ("histone", "reporter")

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

        # Append other movies onto the main time course. The scope sometimes
        # saves a late extra volume as a SEPARATE object (often, but not
        # reliably, named '<series>_t<N>'). Any geometry-compatible movie can
        # be appended; the '_tN' ones are pre-checked since they're the clear
        # case. Matched per-position by name.
        self._append_box = QtWidgets.QGroupBox(
            "Append other movies (same geometry) to the main time course"
        )
        self._append_vl = QtWidgets.QVBoxLayout(self._append_box)
        self._append_checks: dict[str, QtWidgets.QCheckBox] = {}
        layout.addWidget(self._append_box)
        self._append_label = QtWidgets.QLabel("")
        self._append_label.setWordWrap(True)
        self._append_label.setStyleSheet("color: #555;")
        layout.addWidget(self._append_label)

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

        # User = OS account that OWNS the staged files (picks the
        # /murrlab3/<user>/images/ tree). Restricted to real accounts so a
        # typo can't scatter data under a non-account dir owned by the wrong
        # uid. Defaults to whoever is running embryoDB.
        self._user_combo = QtWidgets.QComboBox()
        self._user_combo.setEditable(False)
        for name in system_users():
            self._user_combo.addItem(name)
        _idx = self._user_combo.findText(current_user())
        if _idx >= 0:
            self._user_combo.setCurrentIndex(_idx)
        self._user_combo.currentTextChanged.connect(self._update_user_warning)

        # Person = free-form scientific attribution (who collected the data),
        # NOT the filesystem owner. Editable combo of known persons so a new
        # name is visibly absent and gets a confirm before it's created.
        self._person_combo = QtWidgets.QComboBox()
        self._person_combo.setEditable(True)
        self._person_combo.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        try:
            with self._session_cm() as s:
                for name in known_persons(s):
                    self._person_combo.addItem(name)
        except Exception:
            pass
        self._person_combo.setEditText(current_user())

        self._strain_edit = QtWidgets.QLineEdit()
        self._perturb_edit = QtWidgets.QLineEdit()
        self._reporter_edit = QtWidgets.QLineEdit()
        meta.addRow("User (owns files):", self._user_combo)
        meta.addRow("Person:", self._person_combo)
        meta.addRow("Strain:", self._strain_edit)
        meta.addRow("Perturbation:", self._perturb_edit)
        meta.addRow("Reporter:", self._reporter_edit)
        layout.addLayout(meta)

        self._user_warn = QtWidgets.QLabel("")
        self._user_warn.setStyleSheet("color: #b8860b;")  # dark-yellow
        self._user_warn.setWordWrap(True)
        layout.addWidget(self._user_warn)
        self._update_user_warning()

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
        # StarryNite engine: old (legacy compiled MATLAB, default) vs new
        # (all-MATLAB v1 + retrained tracker + effort estimate). Same output slot.
        self._engine_combo = QtWidgets.QComboBox()
        self._engine_combo.addItem("Old (legacy compiled MATLAB)", "old")
        self._engine_combo.addItem("New (all-MATLAB v1)", "new")
        self._engine_combo.setToolTip(
            "Which StarryNite to run. Old (default) is the legacy compiled-MATLAB "
            "tracer; New runs full MATLAB with the retrained tracker, lands into "
            "the same slot, and records an effort estimate on the run."
        )
        adv_form.addRow("StarryNite engine:", self._engine_combo)
        layout.addWidget(adv)

        # Scheduling: run the queued analysis steps now, or defer to off-hours.
        self._immediate_cb = QtWidgets.QCheckBox(
            "Run pipeline immediately (StarryNite / Red Extract / Measure)"
        )
        self._immediate_cb.setChecked(True)
        self._immediate_cb.setToolTip(
            "Checked (default): start the background worker right after the "
            "import so StarryNite, Red Extract and Measure run as soon as a CPU "
            "is free.\n"
            "Unchecked: defer those steps to off-hours (~9 PM). The worker still "
            "starts, but each step waits until then."
        )
        layout.addWidget(self._immediate_cb)

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
        self._rebuild_append_checks()
        self._update_launch_enabled()

    def _rebuild_append_checks(self) -> None:
        """List every geometry-compatible movie as a checkbox; pre-check the
        '_tN' siblings (the confident extra-timepoint case)."""
        while self._append_vl.count():
            item = self._append_vl.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._append_checks.clear()

        series = self._current_series()
        if series is None:
            self._append_box.setVisible(False)
            self._append_label.setText("")
            return
        from ..lif.import_flow import (
            appendable_candidates,
            extra_timepoint_siblings,
        )
        candidates = appendable_candidates(self._series_list, series.name)
        auto = set(extra_timepoint_siblings(self._series_list, series.name))
        self._append_box.setVisible(bool(candidates))
        if not candidates:
            self._append_label.setText("")
            return
        for name in candidates:
            info = next((s for s in self._series_list if s.name == name), None)
            n = info.positions[0].n_timepoints if info and info.positions else 0
            cb = QtWidgets.QCheckBox(f"{name}  (+{n} tp)")
            cb.setChecked(name in auto)
            cb.toggled.connect(self._update_append_label)
            self._append_vl.addWidget(cb)
            self._append_checks[name] = cb
        self._update_append_label()

    def _selected_append_names(self) -> list[str]:
        return [n for n, cb in self._append_checks.items() if cb.isChecked()]

    def _update_append_label(self) -> None:
        series = self._current_series()
        names = self._selected_append_names()
        if not names:
            self._append_label.setText("")
            return
        extra = 0
        for name in names:
            info = next((s for s in self._series_list if s.name == name), None)
            if info and info.positions:
                extra += info.positions[0].n_timepoints
        base = (
            series.positions[0].n_timepoints
            if series and series.positions else 0
        )
        self._append_label.setText(
            f"Appending {', '.join(names)} (+{extra} tp) → {base + extra} tp total"
        )

    def _update_user_warning(self) -> None:
        user = self._user_combo.currentText().strip()
        if not user:
            self._user_warn.setText("")
            return
        msgs: list[str] = []
        me = current_user()
        if user != me:
            msgs.append(
                f"User {user!r} differs from the account running embryoDB "
                f"({me!r}): files will be OWNED by {me!r}, which can cause "
                "permission problems."
            )
        if not user_has_image_dir(user):
            msgs.append(
                f"/murrlab3/{user} has no image tree yet — it will be created "
                f"and owned by {me!r}."
            )
        self._user_warn.setText("\n".join(msgs))

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
        """Pre-fill each channel's role dropdown from the protocol's map.

        A channel the protocol does not map (e.g. a Trans-PMT/DIC channel)
        must default to ``extra``, never to the combo's index-0 ``histone``:
        two histone channels would both stage into ``tif/`` and collide.
        """
        channel_map = self._protocol_combo.currentData() or {}
        for raw_idx, combo in self._channel_combos.items():
            role = channel_map.get(str(raw_idx))
            if role in _ROLES:
                combo.setCurrentText(role)
            else:
                # Unmapped or unknown role string → treat as extra.
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
        # histone/reporter are exclusive: each stages into a single dir
        # (tif/ resp. tifR/), so assigning two channels the same one would
        # collide on disk. Block it before any extraction runs.
        dupes = [
            role
            for role in _EXCLUSIVE_ROLES
            if list(channel_roles.values()).count(role) > 1
        ]
        if dupes:
            chans = {
                role: [i for i, r in sorted(channel_roles.items()) if r == role]
                for role in dupes
            }
            detail = "; ".join(
                f"{role} → raw_ch {', '.join(map(str, idxs))}"
                for role, idxs in chans.items()
            )
            QtWidgets.QMessageBox.warning(
                self,
                "Duplicate channel role",
                f"Each of histone/reporter may be assigned to only one "
                f"channel ({detail}). Reassign the extra channel(s) to "
                f"'extra' before launching.",
            )
            return
        # Confirm — extraction is long and irreversible-ish (writes lots of
        # files). Surface the channel mapping + any appended movies one more time.
        mapping = "\n".join(
            f"  raw_ch {i} → {r}" for i, r in sorted(channel_roles.items())
        )
        append_names = self._selected_append_names()
        append_msg = ""
        if append_names:
            base = series.positions[0].n_timepoints if series.positions else 0
            extra = 0
            for name in append_names:
                info = next(
                    (s for s in self._series_list if s.name == name), None
                )
                if info and info.positions:
                    extra += info.positions[0].n_timepoints
            append_msg = (
                f"Appending {len(append_names)} movie(s): "
                f"{', '.join(append_names)}\n"
                f"  combined time course: {base} + {extra} = {base + extra} tp\n\n"
            )
        # Guard a brand-new person attribution (typo guard).
        person = self._person_combo.currentText().strip()
        if person:
            try:
                with self._session_cm() as s:
                    is_new = person not in known_persons(s)
            except Exception:
                is_new = False
            if is_new:
                reply = QtWidgets.QMessageBox.question(
                    self, "Create new person?",
                    f"'{person}' has not been used as a person attribution "
                    "before.\n\nCreate it as a new person?",
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No,
                )
                if reply != QtWidgets.QMessageBox.Yes:
                    return

        user = self._user_combo.currentText().strip()
        user_msg = ""
        if user and user != current_user():
            user_msg = (
                f"User {user!r} differs from {current_user()!r} — files will "
                f"be owned by {current_user()!r}.\n\n"
            )
        confirm = QtWidgets.QMessageBox.question(
            self, "Launch import?",
            f"Import series {series.name!r} from\n{self._lif_path}\n\n"
            f"Channel mapping:\n{mapping}\n\n"
            f"{append_msg}"
            f"{user_msg}"
            "Extraction can take hours; it runs detached so you can close "
            "this dialog. Proceed?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if confirm != QtWidgets.QMessageBox.Yes:
            return

        if self._immediate_cb.isChecked():
            delay_hours = 0.0
        else:
            from .import_wizard import _default_delay_until_9pm
            delay_hours = _default_delay_until_9pm()

        try:
            result = run_lif_import(
                self._lif_path,
                series.name,
                protocol,
                user=self._user_combo.currentText().strip() or None,
                person=self._person_combo.currentText().strip(),
                strain=self._strain_edit.text().strip(),
                perturbation=self._perturb_edit.text().strip(),
                reporter=self._reporter_edit.text().strip(),
                channel_roles=channel_roles,
                append_series=self._selected_append_names(),
                auto_append_extra=False,
                bit_depth_policy=self._bit_depth_combo.currentText(),
                no_compress=self._no_compress_cb.isChecked(),
                overwrite=self._overwrite_cb.isChecked(),
                delay_hours=delay_hours,
                sn_engine=self._engine_combo.currentData() or "old",
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
