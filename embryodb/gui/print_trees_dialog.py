"""Dialog for `PrintTrees.pl` / Tree1.

Reachable from the browser right-click ("Print trees…" — selection) and
from the dataset panel ("Print trees for this dataset…" — dataset members).

Tree1 produces PNG files at `/gpfs/fs0/l/murr/trees/<series>{root}.png`
(hardcoded inside the Java tool). The dialog surfaces that path so the user
knows where to look.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from qtpy import QtCore, QtWidgets

from ..config import settings
from ..external_tools import LaunchResult, run_print_trees

_CD_PREFIXES: list[tuple[str, str]] = [
    ("CD",  "CD — raw expression (default)"),
    ("SCD", "SCD — Sulston-aligned timing"),
    ("ACD", "ACD — reference-embryo aligned"),
]


_TREE_OUTPUT_DIR = settings.trees_dir


class PrintTreesDialog(QtWidgets.QDialog):
    """Pick renderer + optional parameters and launch the tree-printing job."""

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
        self.setWindowTitle("Print trees")
        self.setMinimumWidth(480)
        self._launched_log: Path | None = None
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

        # Parameter form
        form = QtWidgets.QGroupBox("Tree1 parameters (all optional)")
        fl = QtWidgets.QFormLayout(form)

        self._min_expr_check = QtWidgets.QCheckBox("Override expression range")
        self._min_expr_check.setToolTip(
            "Without this, Tree1 picks expression bounds automatically per series."
        )
        fl.addRow(self._min_expr_check)

        # Tree1 parses these via Integer.parseInt — must be ints, not floats.
        self._min_expr = QtWidgets.QSpinBox()
        self._min_expr.setRange(-1_000_000, 1_000_000)
        self._min_expr.setValue(0)
        self._min_expr.setEnabled(False)
        fl.addRow("Min expression:", self._min_expr)

        self._max_expr = QtWidgets.QSpinBox()
        self._max_expr.setRange(-1_000_000, 1_000_000)
        self._max_expr.setValue(5000)
        self._max_expr.setEnabled(False)
        fl.addRow("Max expression:", self._max_expr)

        self._color_scheme = QtWidgets.QComboBox()
        self._color_scheme.setEditable(True)
        self._color_scheme.addItems(["rainbow", "blueyellow"])
        self._color_scheme.setToolTip(
            "Built-in: 'rainbow' or 'blueyellow'. Or type a root cell name to "
            "draw only that sub-lineage (e.g. 'ABal')."
        )
        self._color_scheme.setEnabled(False)
        fl.addRow("Color scheme / root:", self._color_scheme)

        self._linewidth = QtWidgets.QSpinBox()
        self._linewidth.setRange(1, 10)
        self._linewidth.setValue(3)
        self._linewidth.setEnabled(False)
        fl.addRow("Line width:", self._linewidth)

        self._min_expr_check.toggled.connect(self._min_expr.setEnabled)
        self._min_expr_check.toggled.connect(self._max_expr.setEnabled)
        self._min_expr_check.toggled.connect(self._color_scheme.setEnabled)
        self._min_expr_check.toggled.connect(self._linewidth.setEnabled)

        self._cd_prefix = QtWidgets.QComboBox()
        for key, label in _CD_PREFIXES:
            self._cd_prefix.addItem(label, key)
        self._cd_prefix.setToolTip(
            "Which expression CSV file Tree1 reads per series:\n"
            "  CD   — raw RedExtract output (default)\n"
            "  SCD  — Sulston-aligned: timing warped to canonical cell-cycle lengths\n"
            "  ACD  — spatially aligned to the Richards 2013 reference embryo\n"
            "SCD/ACD require the patched acexpress_CL2.jar."
        )
        fl.addRow("Expression file:", self._cd_prefix)

        self._renderer = QtWidgets.QComboBox()
        self._renderer.addItem("LIVEtools (R/ggtree)", "livetools")
        self._renderer.addItem("Tree1 (legacy Java)", "java")
        self._renderer.setCurrentIndex(
            0 if settings.tree_renderer == "livetools" else 1
        )
        self._renderer.setToolTip(
            "LIVEtools — the default; reads any of CD/SCD/ACD and draws the "
            "whole movie.\n"
            "Tree1 — the legacy renderer the existing PNG corpus was drawn "
            "with. It cannot read ACD files (their coordinates are signed "
            "floats, which it parses as ints) and clips trees at its own "
            "end-time limits."
        )
        self._renderer.currentIndexChanged.connect(self._sync_renderer)
        fl.addRow("Renderer:", self._renderer)

        layout.addWidget(form)

        self._on_screen = QtWidgets.QCheckBox("Show the tree on screen (quick QC)")
        self._on_screen.setToolTip(
            "Open Tree1's own window in addition to writing the PNGs.\n"
            "Unavailable in remote mode (the job runs on the penticton worker) "
            "or without an X display."
        )
        if settings.remote:
            self._on_screen.setEnabled(False)
            self._on_screen.setToolTip(
                "Remote mode: the job runs on the penticton worker, so its "
                "window would open there. View the PNGs instead."
            )
        elif not os.environ.get("DISPLAY"):
            self._on_screen.setEnabled(False)
            self._on_screen.setToolTip("$DISPLAY is unset — no X display to draw on.")
        layout.addWidget(self._on_screen)
        self._on_screen_allowed = self._on_screen.isEnabled()
        self._sync_renderer()

        info = QtWidgets.QLabel(
            f"PNG output: <code>{_TREE_OUTPUT_DIR}/&lt;series&gt;.png</code> "
            f"(directory must exist and be writeable by your account)."
        )
        info.setStyleSheet("color: #555;")
        info.setWordWrap(True)
        layout.addWidget(info)

        # Status
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
        btns.accepted.connect(self._on_launch)
        btns.rejected.connect(self.reject)
        self._view_log_btn = QtWidgets.QPushButton("View log")
        self._view_log_btn.setEnabled(False)
        self._view_log_btn.clicked.connect(self._view_log)
        btns.addButton(self._view_log_btn, QtWidgets.QDialogButtonBox.ActionRole)
        layout.addWidget(btns)

    # --- launch + log -----------------------------------------------------

    def _sync_renderer(self) -> None:
        """On-screen display is a Tree1 JFrame; LIVEtools only writes PNGs."""
        is_java = self._renderer.currentData() == "java"
        self._on_screen.setEnabled(self._on_screen_allowed and is_java)
        if not is_java:
            self._on_screen.setChecked(False)

    def _on_launch(self) -> None:
        kwargs: dict = {"renderer": self._renderer.currentData()}
        if self._min_expr_check.isChecked():
            kwargs["min_expr"] = int(self._min_expr.value())
            kwargs["max_expr"] = int(self._max_expr.value())
            kwargs["color_scheme"] = self._color_scheme.currentText().strip() or "rainbow"
            kwargs["linewidth"] = int(self._linewidth.value())
        cd_prefix = self._cd_prefix.currentData() or "CD"
        if cd_prefix != "CD":
            kwargs["cd_prefix"] = cd_prefix
        if self._on_screen.isChecked():
            kwargs["on_screen"] = True
        try:
            result: LaunchResult = run_print_trees(self._series_names, **kwargs)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self, "Launch failed", f"Could not spawn Tree1:\n{exc}"
            )
            return
        self._launched_log = result.log_path
        if result.proc is None:
            # Remote mode: queued for the penticton worker.
            self._status.setText(
                f"<b>Queued</b> as job #{result.job_id} — will run on the "
                f"penticton worker.<br>Track it in <b>Background jobs…</b>.<br><br>"
                f"<b>Log (when it starts):</b> <code>{result.log_path}</code><br>"
                f"<b>PNGs:</b> <code>{_TREE_OUTPUT_DIR}/&lt;series&gt;.png</code>"
            )
        else:
            self._status.setText(
                f"<b>Launched</b> (pid {result.proc.pid}). Detached — survives "
                f"GUI close.<br><br>"
                f"<b>Log:</b> <code>{result.log_path}</code><br>"
                f"<b>PNGs:</b> <code>{_TREE_OUTPUT_DIR}/&lt;series&gt;.png</code>"
            )
        self._ok_btn.setEnabled(False)
        self._view_log_btn.setEnabled(True)

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


__all__ = ["PrintTreesDialog"]
