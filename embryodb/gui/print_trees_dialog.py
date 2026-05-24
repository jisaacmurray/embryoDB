"""Dialog for `PrintTrees.pl` / Tree1.

Reachable from the browser right-click ("Print trees…" — selection) and
from the dataset panel ("Print trees for this dataset…" — dataset members).

Tree1 produces PNG files at `/gpfs/fs0/l/murr/trees/<series>{root}.png`
(hardcoded inside the Java tool). The dialog surfaces that path so the user
knows where to look.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from qtpy import QtCore, QtWidgets

from ..external_tools import LaunchResult, run_print_trees


_TREE_OUTPUT_DIR = Path("/gpfs/fs0/l/murr/trees")


class PrintTreesDialog(QtWidgets.QDialog):
    """Pick optional Tree1 parameters and launch the tree-printing job."""

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

        self._min_expr = QtWidgets.QDoubleSpinBox()
        self._min_expr.setRange(-1e6, 1e6)
        self._min_expr.setDecimals(3)
        self._min_expr.setValue(0.0)
        self._min_expr.setEnabled(False)
        fl.addRow("Min expression:", self._min_expr)

        self._max_expr = QtWidgets.QDoubleSpinBox()
        self._max_expr.setRange(-1e6, 1e6)
        self._max_expr.setDecimals(3)
        self._max_expr.setValue(5000.0)
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

        layout.addWidget(form)

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

    def _on_launch(self) -> None:
        kwargs: dict = {}
        if self._min_expr_check.isChecked():
            kwargs["min_expr"] = float(self._min_expr.value())
            kwargs["max_expr"] = float(self._max_expr.value())
            kwargs["color_scheme"] = self._color_scheme.currentText().strip() or "rainbow"
            kwargs["linewidth"] = int(self._linewidth.value())
        try:
            result: LaunchResult = run_print_trees(self._series_names, **kwargs)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self, "Launch failed", f"Could not spawn Tree1:\n{exc}"
            )
            return
        self._launched_log = result.log_path
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
