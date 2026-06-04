"""Background-jobs panel — list ongoing and recently-finished jobs.

Two kinds of background work show here:

- **Detached launches** (LIF import, extract, tree renders) that survive
  closing the GUI; their only trace is a log file under the runs directory.
- **Pipeline steps** (StarryNite / Red Extract / Measure and the import
  steps) run by the background worker and tracked as ``PipelineStepRun``
  rows in the DB — this is what "Re-run pipeline" queues.

Both are merged via :func:`embryodb.jobs.list_jobs`. Ongoing jobs are
always shown; a time-window control pulls back finished ones.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from qtpy import QtCore, QtGui, QtWidgets

from ..jobs import JobRow, list_jobs
from .log_viewer import LogViewerDialog

# (label, finished-job cutoff). timedelta(0) → running only; None → all.
_WINDOWS: tuple[tuple[str, timedelta | None], ...] = (
    ("Running only", timedelta(0)),
    ("Last 24 hours", timedelta(hours=24)),
    ("Last 7 days", timedelta(days=7)),
    ("Last 30 days", timedelta(days=30)),
    ("All", None),
)
_DEFAULT_WINDOW_INDEX = 1  # Last 24 hours

_COLS = ["Type", "Job", "Status", "Started", "Last activity"]


def _fmt_when(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "—"


class BackgroundJobsDialog(QtWidgets.QDialog):
    def __init__(
        self,
        session_cm: Callable | None = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_cm = session_cm
        self.setWindowTitle("Background jobs")
        self.resize(860, 440)
        self._rows: list[JobRow] = []

        vl = QtWidgets.QVBoxLayout(self)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Show:"))
        self._window_combo = QtWidgets.QComboBox()
        for label, _ in _WINDOWS:
            self._window_combo.addItem(label)
        self._window_combo.setCurrentIndex(_DEFAULT_WINDOW_INDEX)
        self._window_combo.currentIndexChanged.connect(self._refresh)
        top.addWidget(self._window_combo)
        top.addStretch(1)
        self._count_label = QtWidgets.QLabel("")
        self._count_label.setStyleSheet("color: #555;")
        top.addWidget(self._count_label)
        vl.addLayout(top)

        self._table = QtWidgets.QTableWidget(0, len(_COLS))
        self._table.setHorizontalHeaderLabels(_COLS)
        self._table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.itemDoubleClicked.connect(lambda _: self._view_log())
        vl.addWidget(self._table)

        btns = QtWidgets.QHBoxLayout()
        self._view_btn = QtWidgets.QPushButton("View log")
        self._view_btn.clicked.connect(self._view_log)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(self._refresh)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        btns.addWidget(self._view_btn)
        btns.addWidget(refresh)
        btns.addStretch(1)
        btns.addWidget(close)
        vl.addLayout(btns)

        # Auto-refresh so running→finished transitions appear without a click.
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

        self._refresh()

    def _cutoff(self) -> datetime | None:
        """Finished-job cutoff for the chosen window. ``datetime.max`` means
        running-only; ``None`` means no cutoff."""
        _, delta = _WINDOWS[self._window_combo.currentIndex()]
        if delta is None:
            return None
        if delta == timedelta(0):
            return datetime.max
        return datetime.now() - delta

    def _status_color(self, row: JobRow) -> QtGui.QColor:
        label = row.status_label
        if row.running or label in ("running", "queued"):
            return QtGui.QColor("#1565c0")
        if label == "done":
            return QtGui.QColor("#2e7d32")
        if label.startswith("failed"):
            return QtGui.QColor("#c62828")
        return QtGui.QColor("#ef6c00")  # skipped / interrupted

    def _refresh(self) -> None:
        selected = self._selected_key()
        self._rows = list_jobs(self._session_cm, since=self._cutoff())
        self._table.setRowCount(len(self._rows))
        n_running = 0
        for row, job in enumerate(self._rows):
            if job.running:
                n_running += 1
            cells = [
                job.kind,
                job.name,
                job.status_label,
                _fmt_when(job.started_at),
                _fmt_when(job.last_activity),
            ]
            for col, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                if col == 2:
                    item.setForeground(self._status_color(job))
                if col == 0:
                    item.setData(QtCore.Qt.UserRole, self._row_key(job))
                self._table.setItem(row, col, item)
        self._table.resizeColumnsToContents()
        self._count_label.setText(f"{len(self._rows)} job(s), {n_running} running")
        if selected is not None:
            self._reselect(selected)

    @staticmethod
    def _row_key(job: JobRow) -> str:
        return f"{job.source}:{job.name}"

    def _selected_key(self) -> str | None:
        items = self._table.selectedItems()
        if not items:
            return None
        first = self._table.item(items[0].row(), 0)
        return first.data(QtCore.Qt.UserRole) if first else None

    def _reselect(self, key: str) -> None:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item and item.data(QtCore.Qt.UserRole) == key:
                self._table.selectRow(row)
                return

    def _current_job(self) -> JobRow | None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return None
        idx = rows[0].row()
        return self._rows[idx] if 0 <= idx < len(self._rows) else None

    def _view_log(self) -> None:
        job = self._current_job()
        if job is None:
            QtWidgets.QMessageBox.information(
                self, "No selection", "Select a job to view its log."
            )
            return
        if job.log_path is None:
            QtWidgets.QMessageBox.information(
                self, "No log yet",
                "This step hasn't started, so it has no log file yet.",
            )
            return
        LogViewerDialog(job.log_path, follow=job.running, parent=self).exec()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._timer.stop()
        super().closeEvent(event)


__all__ = ["BackgroundJobsDialog"]
