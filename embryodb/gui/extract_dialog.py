"""Dialog for the legacy extract.sh steps.

Reachable from the browser right-click ("Run extract steps…" — applies to
the current selection) and from the dataset panel ("Run extract on this
dataset…" — applies to every member of the active dataset).

The actual subprocess is detached via `embryodb.external_tools.run_extract`;
the dialog returns immediately after launching and the GUI stays usable.
The user sees the log path and can open it in a viewer.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from qtpy import QtCore, QtWidgets

from ..external_tools import EXTRACT_STEPS, LaunchResult, run_extract


class ExtractDialog(QtWidgets.QDialog):
    """Pick which extract steps to run on a fixed list of series."""

    def __init__(
        self,
        session_cm: Callable,
        series_names: list[str],
        *,
        title_hint: str = "",
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_cm = session_cm
        self._series_names = list(series_names)
        self.setWindowTitle("Run extract steps")
        self.setMinimumWidth(520)
        self._launched_log: Path | None = None
        self._launch_result: LaunchResult | None = None
        self._chosen_keys: list[str] = []
        self._poll_timer: QtCore.QTimer | None = None
        self._build(title_hint)

    # --- UI ----------------------------------------------------------------

    def _build(self, title_hint: str) -> None:
        layout = QtWidgets.QVBoxLayout(self)

        n = len(self._series_names)
        if n == 0:
            layout.addWidget(QtWidgets.QLabel("No series selected."))
            close = QtWidgets.QPushButton("Close")
            close.clicked.connect(self.reject)
            layout.addWidget(close)
            return

        header = QtWidgets.QLabel(
            f"<b>Target:</b> {n} series"
            + (f" — {title_hint}" if title_hint else "")
        )
        layout.addWidget(header)

        if n == 1:
            layout.addWidget(QtWidgets.QLabel(self._series_names[0]))
        else:
            preview = QtWidgets.QListWidget()
            preview.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
            for name in self._series_names:
                preview.addItem(name)
            preview.setMaximumHeight(100)
            layout.addWidget(preview)

        steps_box = QtWidgets.QGroupBox("Steps to run (in order)")
        steps_vl = QtWidgets.QVBoxLayout(steps_box)
        self._step_checks: dict[str, QtWidgets.QCheckBox] = {}
        for step in EXTRACT_STEPS:
            cb = QtWidgets.QCheckBox(f"{step.label} — {step.description}")
            cb.setChecked(True)
            if step.key == "update_perms":
                cb.setEnabled(False)  # always runs; opt-out not allowed
            if step.key == "process_time":
                # v2 pipeline runs this at import. Off by default; user
                # opts in for legacy series that pre-date the new flow.
                cb.setChecked(False)
            self._step_checks[step.key] = cb
            steps_vl.addWidget(cb)
        layout.addWidget(steps_box)

        # Convenience: select-all / clear-all / "skip already-pipelined"
        sel_row = QtWidgets.QHBoxLayout()
        all_btn = QtWidgets.QPushButton("All")
        none_btn = QtWidgets.QPushButton("None")
        defaults_btn = QtWidgets.QPushButton("Skip pipeline steps")
        defaults_btn.setToolTip(
            "Uncheck RedExtractor1 and Measure1 — these typically run as part "
            "of the import pipeline and only need to be re-run via "
            "'Re-run pipeline' or after a parameter change."
        )
        all_btn.clicked.connect(lambda: self._set_all(True))
        none_btn.clicked.connect(lambda: self._set_all(False))
        defaults_btn.clicked.connect(self._set_skip_pipeline)
        sel_row.addWidget(all_btn)
        sel_row.addWidget(none_btn)
        sel_row.addWidget(defaults_btn)
        sel_row.addStretch(1)
        layout.addLayout(sel_row)

        # Consistency with the other launch dialogs: extract steps run as a
        # detached subprocess and start immediately — there is no off-hours
        # worker queue for them — so this is shown checked-and-disabled.
        immediate_note = QtWidgets.QCheckBox(
            "Run immediately (extract steps always start now)"
        )
        immediate_note.setChecked(True)
        immediate_note.setEnabled(False)
        immediate_note.setToolTip(
            "Extract launches a detached subprocess that begins right away; "
            "these steps aren't queued for the background worker, so there's "
            "nothing to defer."
        )
        layout.addWidget(immediate_note)

        # Status / log path label (populated after Launch)
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
        self._cancel_btn = btns.button(QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_launch)
        btns.rejected.connect(self.reject)
        self._view_log_btn = QtWidgets.QPushButton("View log")
        self._view_log_btn.setEnabled(False)
        self._view_log_btn.clicked.connect(self._view_log)
        btns.addButton(self._view_log_btn, QtWidgets.QDialogButtonBox.ActionRole)
        layout.addWidget(btns)

    # --- selection helpers ------------------------------------------------

    def _set_all(self, checked: bool) -> None:
        for key, cb in self._step_checks.items():
            if key == "update_perms" and not checked:
                continue  # permissions step always runs
            cb.setChecked(checked)

    def _set_skip_pipeline(self) -> None:
        for key, cb in self._step_checks.items():
            cb.setChecked(key not in {"red_extractor", "measure"})

    def _selected_keys(self) -> list[str]:
        return [k for k, cb in self._step_checks.items() if cb.isChecked()]

    # --- launch + log -----------------------------------------------------

    def _on_launch(self) -> None:
        keys = self._selected_keys()
        if not keys:
            QtWidgets.QMessageBox.information(
                self, "Nothing selected",
                "Pick at least one step to run."
            )
            return
        try:
            result: LaunchResult = run_extract(self._series_names, keys)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self, "Launch failed", f"Could not spawn subprocess:\n{exc}"
            )
            return
        self._launched_log = result.log_path
        self._launch_result = result
        self._chosen_keys = keys
        self._ok_btn.setEnabled(False)
        self._view_log_btn.setEnabled(True)
        # After a successful launch, "Cancel" no longer makes sense — the
        # job is already running detached and closing this dialog doesn't
        # stop it. Rename the button so the exit path reads as "I'm done
        # watching", not "abort the thing I just started".
        self._cancel_btn.setText("Close")
        # Start polling the log file for step-banner progress.
        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.setInterval(2000)
        self._poll_timer.timeout.connect(self._poll_log)
        self._poll_timer.start()
        self._poll_log()

    def _poll_log(self) -> None:
        """Read the log and update the status label with current step progress.

        Detects `== StepLabel ==` banners written by run_extract. The last
        banner seen = current (or most-recently-run) step. Stops polling once
        the subprocess exits.
        """
        if self._launch_result is None or self._launched_log is None:
            return
        try:
            content = self._launched_log.read_text(errors="replace")
        except OSError:
            return

        chosen_steps = [s for s in EXTRACT_STEPS if s.key in set(self._chosen_keys)]
        appeared = [s for s in chosen_steps if f"== {s.label} ==" in content]
        proc_alive = self._launch_result.proc.poll() is None

        if not appeared:
            msg = (
                f"<b>Launched</b> (pid {self._launch_result.proc.pid}) — "
                f"waiting for first step…"
            )
        elif proc_alive:
            current = appeared[-1].label
            done = [s.label for s in appeared[:-1]]
            pending = [s.label for s in chosen_steps if s not in appeared]
            parts = [f"<b>Running:</b> {current}"]
            if done:
                parts.append(f"Done: {', '.join(done)}")
            if pending:
                parts.append(f"Pending: {', '.join(pending)}")
            msg = " | ".join(parts)
        else:
            rc = self._launch_result.proc.returncode
            if rc == 0:
                msg = f"<b>Complete</b> — {len(chosen_steps)} step(s) finished."
            else:
                last = appeared[-1].label if appeared else "startup"
                msg = (
                    f"<b>Stopped</b> after {last} "
                    f"(exit {rc}). Check log for errors."
                )
            if self._poll_timer is not None:
                self._poll_timer.stop()

        self._status.setText(
            f"{msg}<br>"
            f"<b>Log:</b> <code>{self._launched_log}</code><br>"
            f"<b>Series list:</b> <code>{self._launch_result.series_list}</code>"
        )

    def _view_log(self) -> None:
        if self._launched_log is None:
            return
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"Log — {self._launched_log.name}")
        dlg.resize(800, 500)
        layout = QtWidgets.QVBoxLayout(dlg)
        view = QtWidgets.QPlainTextEdit()
        view.setReadOnly(True)
        view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        try:
            view.setPlainText(self._launched_log.read_text(errors="replace"))
        except OSError as exc:
            view.setPlainText(f"Could not read log:\n{exc}")
        layout.addWidget(view)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(
            lambda: view.setPlainText(
                self._launched_log.read_text(errors="replace") if self._launched_log else ""
            )
        )
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(dlg.accept)
        button_row = QtWidgets.QHBoxLayout()
        button_row.addWidget(refresh)
        button_row.addStretch(1)
        button_row.addWidget(close)
        layout.addLayout(button_row)
        dlg.exec()


__all__ = ["ExtractDialog"]
