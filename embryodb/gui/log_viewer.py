"""A read-only log viewer that optionally tails a file while it grows.

Used by the Background-jobs panel to inspect a detached job's log. When
``follow`` is set it re-reads the file on a timer and keeps the view
scrolled to the bottom, so a running job's output streams in live.
"""

from __future__ import annotations

from pathlib import Path

from qtpy import QtCore, QtGui, QtWidgets


class LogViewerDialog(QtWidgets.QDialog):
    def __init__(
        self,
        log_path: Path,
        *,
        follow: bool = False,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._log_path = Path(log_path)
        self.setWindowTitle(f"Log — {self._log_path.name}")
        self.resize(820, 520)

        vl = QtWidgets.QVBoxLayout(self)
        self._view = QtWidgets.QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        font = self._view.font()
        font.setStyleHint(font.Monospace)
        font.setFamily("monospace")
        self._view.setFont(font)
        vl.addWidget(self._view)

        row = QtWidgets.QHBoxLayout()
        self._follow_cb = QtWidgets.QCheckBox("Follow")
        self._follow_cb.setChecked(follow)
        self._follow_cb.toggled.connect(self._on_follow_toggled)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(self._reload)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(self._follow_cb)
        row.addStretch(1)
        row.addWidget(refresh)
        row.addWidget(close)
        vl.addLayout(row)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._reload)

        self._reload()
        if follow:
            self._timer.start()

    def _reload(self) -> None:
        try:
            text = self._log_path.read_text(errors="replace")
        except OSError as exc:
            self._view.setPlainText(f"Could not read log:\n{exc}")
            return
        follow = self._follow_cb.isChecked()
        self._view.setPlainText(text)
        if follow:
            self._view.moveCursor(QtGui.QTextCursor.End)

    def _on_follow_toggled(self, on: bool) -> None:
        if on:
            self._reload()
            self._timer.start()
        else:
            self._timer.stop()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._timer.stop()
        super().closeEvent(event)


__all__ = ["LogViewerDialog"]
