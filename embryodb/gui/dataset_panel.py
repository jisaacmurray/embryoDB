"""Dataset management panel.

Replaces the flat /murr/lists/ files with named DB-backed Datasets. v1 ops:
- create / rename / delete a Dataset
- add / remove selected series (from the browser) to / from a Dataset
- export a Dataset as a flat list file (legacy compatibility)
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from qtpy import QtCore, QtWidgets
from sqlalchemy.orm import Session

from ..queries import datasets as q_datasets


def _combo_popup_height(combo: QtWidgets.QComboBox, visible_rows: int) -> int:
    """Pixel height for a combo popup that shows `visible_rows` rows.

    Computed from the combo's current font metrics so the popup follows
    DPI/theme changes correctly.
    """
    line = combo.fontMetrics().height() + 6  # padding per row
    return line * visible_rows + 8


class DatasetPanel(QtWidgets.QWidget):
    """Compact panel: dropdown of datasets + Add/Remove/Export buttons."""

    addRequested = QtCore.Signal(str)  # dataset_name
    removeRequested = QtCore.Signal(str)
    exportRequested = QtCore.Signal(str, Path)
    # Legacy analysis tools applied to every member of the active dataset.
    extractRequested = QtCore.Signal(str)     # dataset_name
    printTreesRequested = QtCore.Signal(str)  # dataset_name
    freezeRequested = QtCore.Signal(str)      # dataset_name
    # Fired when the dataset selection or "Show only members" toggle changes
    # in a way that should narrow / unrestrict the browser table.
    # Args: (dataset_id_or_none, dataset_name_or_empty)
    filterChanged = QtCore.Signal(object, str)

    def __init__(
        self,
        session_factory: Callable[[], Session],
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_factory = session_factory
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(2)

        # Row 1: dataset selection + filter toggle.
        row1 = QtWidgets.QHBoxLayout()
        # Row 2: operations on the active dataset.
        row2 = QtWidgets.QHBoxLayout()

        # Tag dropdown is hidden for now — the search box covers the common
        # cases and the tag taxonomy is heuristic anyway. Kept in the schema
        # and queries layer so we can surface it later without a migration.
        self._tag_combo = QtWidgets.QComboBox(self)
        self._tag_combo.addItem("(any)", userData=None)
        self._tag_combo.currentIndexChanged.connect(self._on_filter_changed)
        self._tag_combo.hide()

        row1.addWidget(QtWidgets.QLabel("Search:"))
        self._search_box = QtWidgets.QLineEdit()
        self._search_box.setPlaceholderText("filter datasets…")
        self._search_box.setFixedWidth(180)
        self._search_box.textChanged.connect(self._on_filter_changed)
        row1.addWidget(self._search_box)

        row1.addWidget(QtWidgets.QLabel("Dataset:"))
        self._combo = QtWidgets.QComboBox()
        self._combo.setMinimumWidth(220)
        # Use an explicit QListView as the combo's popup so we can constrain
        # its height and force a scroll bar — without the `combobox-popup: 0`
        # stylesheet trick, which on X11-forwarded sessions sometimes fails
        # to render the popup at all (the "dropdown won't open" + black-box
        # symptoms). This approach plays nicely with native styles and
        # remote X11 servers (XQuartz, Xming).
        self._combo.setMaxVisibleItems(15)
        list_view = QtWidgets.QListView(self._combo)
        list_view.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        list_view.setUniformItemSizes(True)
        list_view.setFixedHeight(_combo_popup_height(self._combo, 15))
        self._combo.setView(list_view)
        row1.addWidget(self._combo, 1)

        self._new_btn = QtWidgets.QPushButton("New…")
        self._new_btn.clicked.connect(self._on_new)
        row1.addWidget(self._new_btn)

        # Toggling this narrows the browser table to the active dataset's
        # members. Updates immediately when checked or when the active dataset
        # changes while it's checked.
        self._filter_check = QtWidgets.QCheckBox("Show only members")
        self._filter_check.setToolTip(
            "Restrict the browser to series that are members of the selected "
            "dataset. Untick to see the full corpus."
        )
        self._filter_check.toggled.connect(self._on_filter_toggle)
        row1.addWidget(self._filter_check)
        row1.addStretch(1)

        # Row 2: per-dataset operations.
        self._add_btn = QtWidgets.QPushButton("Add selected")
        self._add_btn.clicked.connect(self._on_add)
        row2.addWidget(self._add_btn)

        self._remove_btn = QtWidgets.QPushButton("Remove selected")
        self._remove_btn.clicked.connect(self._on_remove)
        row2.addWidget(self._remove_btn)

        self._notes_btn = QtWidgets.QPushButton("Notes…")
        self._notes_btn.setToolTip("Edit the description / notes for this dataset.")
        self._notes_btn.clicked.connect(self._on_notes)
        row2.addWidget(self._notes_btn)

        self._show_btn = QtWidgets.QPushButton("Show members")
        self._show_btn.clicked.connect(self._on_show)
        row2.addWidget(self._show_btn)

        self._export_btn = QtWidgets.QPushButton("Export list…")
        self._export_btn.clicked.connect(self._on_export)
        row2.addWidget(self._export_btn)

        self._extract_btn = QtWidgets.QPushButton("Run extract…")
        self._extract_btn.setToolTip(
            "Run the legacy extract.sh steps on every member of the active dataset."
        )
        self._extract_btn.clicked.connect(self._on_run_extract)
        row2.addWidget(self._extract_btn)

        self._trees_btn = QtWidgets.QPushButton("Print trees…")
        self._trees_btn.setToolTip(
            "Run PrintTrees.pl (Tree1) on every member of the active dataset."
        )
        self._trees_btn.clicked.connect(self._on_print_trees)
        row2.addWidget(self._trees_btn)

        self._freeze_btn = QtWidgets.QPushButton("Freeze for phenotyping…")
        self._freeze_btn.setToolTip(
            "Freeze every member's CSVs into a per-user directory plus a "
            "ready-to-run config for the LineagePhenotyping R pipeline."
        )
        self._freeze_btn.clicked.connect(self._on_freeze)
        row2.addWidget(self._freeze_btn)

        row2.addStretch(1)

        outer.addLayout(row1)
        outer.addLayout(row2)

        # Whenever the active dataset changes, re-emit the filter signal
        # (with the new dataset id, or None) so MainWindow can refresh.
        self._combo.currentIndexChanged.connect(self._emit_filter)

    # --- public API -------------------------------------------------------

    def refresh(self) -> None:
        with self._session_factory() as session:
            tags = q_datasets.distinct_tags(session)
        current_tag = self._tag_combo.currentData()
        self._tag_combo.blockSignals(True)
        self._tag_combo.clear()
        self._tag_combo.addItem("(any)", userData=None)
        for t in tags:
            self._tag_combo.addItem(t, userData=t)
        # Restore selection if still present
        if current_tag:
            idx = self._tag_combo.findData(current_tag)
            if idx >= 0:
                self._tag_combo.setCurrentIndex(idx)
        self._tag_combo.blockSignals(False)
        self._refresh_dataset_combo()

    def _refresh_dataset_combo(self) -> None:
        tag = self._tag_combo.currentData()
        text = self._search_box.text().strip() or None
        with self._session_factory() as session:
            names = [
                d.name
                for d in q_datasets.list_datasets(
                    session, name_substring=text, tag=tag
                )
            ]
        current = self._combo.currentText()
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItems(names)
        if current and current in names:
            self._combo.setCurrentText(current)
        self._combo.blockSignals(False)

    def _on_filter_changed(self, *_args: object) -> None:
        self._refresh_dataset_combo()

    def current_dataset(self) -> str | None:
        return self._combo.currentText() or None

    def set_active_dataset(self, name: str) -> None:
        """Programmatically select a dataset in the combo, refreshing the list
        first if the name isn't currently visible (e.g. filtered out by tag)."""
        if not name:
            return
        if self._combo.findText(name) < 0:
            # Clear any active filters so the target dataset becomes visible.
            self._search_box.clear()
            self._tag_combo.blockSignals(True)
            self._tag_combo.setCurrentIndex(0)  # "(any)"
            self._tag_combo.blockSignals(False)
            self._refresh_dataset_combo()
        idx = self._combo.findText(name)
        if idx >= 0:
            self._combo.setCurrentIndex(idx)

    # --- handlers ---------------------------------------------------------

    def _on_new(self) -> None:
        name, ok = QtWidgets.QInputDialog.getText(self, "New dataset", "Name:")
        if not ok or not name.strip():
            return
        try:
            with self._session_factory() as session:
                q_datasets.create(session, name.strip())
        except q_datasets.DatasetError as exc:
            QtWidgets.QMessageBox.warning(self, "Create failed", str(exc))
            return
        self.refresh()
        self._combo.setCurrentText(name.strip())

    def _on_add(self) -> None:
        name = self.current_dataset()
        if not name:
            QtWidgets.QMessageBox.information(
                self, "No dataset", "Select a dataset first."
            )
            return
        self.addRequested.emit(name)

    def _on_remove(self) -> None:
        name = self.current_dataset()
        if not name:
            return
        self.removeRequested.emit(name)

    def _on_filter_toggle(self, _checked: bool) -> None:
        self._emit_filter()

    def _emit_filter(self, *_args: object) -> None:
        """Tell MainWindow whether/how to narrow the table.

        When the checkbox is unticked we emit (None, "") meaning "no dataset
        filter". When ticked, we look up the dataset id and emit it along with
        the name.
        """
        if not self._filter_check.isChecked():
            self.filterChanged.emit(None, "")
            return
        name = self.current_dataset()
        if not name:
            self.filterChanged.emit(None, "")
            return
        with self._session_factory() as session:
            ds = q_datasets.get_by_name(session, name)
            ds_id = ds.id if ds else None
        self.filterChanged.emit(ds_id, name)

    def _on_notes(self) -> None:
        name = self.current_dataset()
        if not name:
            QtWidgets.QMessageBox.information(
                self, "No dataset", "Select a dataset first."
            )
            return
        with self._session_factory() as session:
            ds = q_datasets.get_by_name(session, name)
            current = ds.description if ds else ""
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"Notes — {name}")
        dlg.resize(520, 320)
        layout = QtWidgets.QVBoxLayout(dlg)
        layout.addWidget(QtWidgets.QLabel(
            "Free-text notes for this dataset. Use this to capture\n"
            "common threads — perturbation, reporter, wild-type status,\n"
            "or any other context that helps group related lists."
        ))
        editor = QtWidgets.QPlainTextEdit()
        editor.setPlainText(current)
        layout.addWidget(editor)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        new_text = editor.toPlainText()
        with self._session_factory() as session:
            ds = q_datasets.get_by_name(session, name)
            if ds is None:
                QtWidgets.QMessageBox.warning(self, "Save failed", "dataset gone")
                return
            ds.description = new_text

    def _on_show(self) -> None:
        name = self.current_dataset()
        if not name:
            return
        with self._session_factory() as session:
            ds = q_datasets.get_by_name(session, name)
            members = sorted(s.series_name for s in ds.series) if ds else []
        QtWidgets.QMessageBox.information(
            self,
            f"{name} ({len(members)} series)",
            "\n".join(members) if members else "(empty)",
        )

    def _on_export(self) -> None:
        name = self.current_dataset()
        if not name:
            return
        path_str, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export list file",
            f"{name}.list",
            "List files (*.list);;All files (*)",
        )
        if not path_str:
            return
        self.exportRequested.emit(name, Path(path_str))

    def _on_run_extract(self) -> None:
        name = self.current_dataset()
        if not name:
            QtWidgets.QMessageBox.information(
                self, "No dataset selected",
                "Pick a dataset from the dropdown first."
            )
            return
        self.extractRequested.emit(name)

    def _on_print_trees(self) -> None:
        name = self.current_dataset()
        if not name:
            QtWidgets.QMessageBox.information(
                self, "No dataset selected",
                "Pick a dataset from the dropdown first."
            )
            return
        self.printTreesRequested.emit(name)

    def _on_freeze(self) -> None:
        name = self.current_dataset()
        if not name:
            QtWidgets.QMessageBox.information(
                self, "No dataset selected",
                "Pick a dataset from the dropdown first."
            )
            return
        self.freezeRequested.emit(name)
