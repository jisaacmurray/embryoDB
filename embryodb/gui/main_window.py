"""Main window: filter bar + dataset bar + browser table + detail panel.

Layout:
  +-------------------------------------------------------------+
  |  filter bar                                                 |
  |  dataset bar                                                |
  +------------------------- splitter -------------------------+
  |                                  |                          |
  |  series browser (QTableView)     |   detail / edit panel    |
  |                                  |                          |
  +-------------------------------------------------------------+
  |  status bar: counts, conn info                              |
  +-------------------------------------------------------------+
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from qtpy import QtCore, QtGui, QtWidgets

from .. import database
from ..config import settings
from ..exporters.xml_exporter import export_all
from ..importers.xml_importer import import_dir
from ..queries import datasets as q_datasets
from .dataset_panel import DatasetPanel
from .detail_panel import DetailPanel
from .filter_bar import FilterBar
from .models import Filters, SeriesTableModel


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("embryoDB")
        self._build()
        self._wire()
        self._maybe_init_db_and_refresh()

    # --- construction ----------------------------------------------------

    def _build(self) -> None:
        # Browser is the central widget — it always has all the horizontal
        # space the side/bottom docks don't claim.
        self._model = SeriesTableModel(self)
        self._table = QtWidgets.QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self._table.setSortingEnabled(True)
        self._table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.Interactive
        )
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.verticalHeader().setVisible(False)
        self.setCentralWidget(self._table)

        # Filter bar lives in a top toolbar — always on, no docking needed.
        self._filter_bar = FilterBar()
        self._filter_toolbar = QtWidgets.QToolBar("Filters", self)
        self._filter_toolbar.setMovable(False)
        self._filter_toolbar.addWidget(self._filter_bar)
        self.addToolBar(QtCore.Qt.TopToolBarArea, self._filter_toolbar)

        # Detail + Dataset live in a single dockable widget at the bottom.
        # Each is wrapped in a thin styled frame (no title) so the two
        # sections are visually separated without spending vertical rows on
        # header text — laptop real estate is at a premium.
        self._detail = DetailPanel(self._session_cm)
        self._dataset_panel = DatasetPanel(self._session_cm)

        dataset_frame = QtWidgets.QFrame()
        dataset_frame.setFrameShape(QtWidgets.QFrame.StyledPanel)
        dataset_frame_layout = QtWidgets.QVBoxLayout(dataset_frame)
        dataset_frame_layout.setContentsMargins(4, 4, 4, 4)
        dataset_frame_layout.addWidget(self._dataset_panel)

        detail_frame = QtWidgets.QFrame()
        detail_frame.setFrameShape(QtWidgets.QFrame.StyledPanel)
        detail_frame_layout = QtWidgets.QVBoxLayout(detail_frame)
        detail_frame_layout.setContentsMargins(4, 4, 4, 4)
        detail_frame_layout.addWidget(self._detail)

        dock_container = QtWidgets.QWidget()
        dock_layout = QtWidgets.QVBoxLayout(dock_container)
        dock_layout.setContentsMargins(2, 2, 2, 2)
        dock_layout.setSpacing(4)
        dock_layout.addWidget(dataset_frame)
        dock_layout.addWidget(detail_frame, 1)

        self._detail_dock = QtWidgets.QDockWidget("Detail / Dataset", self)
        self._detail_dock.setObjectName("DetailDock")  # for saveState/restoreState
        self._detail_dock.setAllowedAreas(
            QtCore.Qt.BottomDockWidgetArea | QtCore.Qt.RightDockWidgetArea
            | QtCore.Qt.LeftDockWidgetArea | QtCore.Qt.TopDockWidgetArea
        )
        self._detail_dock.setWidget(dock_container)
        # Hide the dock's own title bar — it's redundant with the framed
        # subsections, and the View menu's toggle action covers show/hide.
        # An empty title-bar widget removes the drag handle too; that's an
        # accepted trade-off for ~22px of vertical room on laptops.
        self._detail_dock.setTitleBarWidget(QtWidgets.QWidget())
        self.addDockWidget(QtCore.Qt.BottomDockWidgetArea, self._detail_dock)

        self._build_menu()
        self.statusBar().showMessage("not connected")

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        act_import_acq = QtGui.QAction("Import acquisition…", self)
        act_import_acq.triggered.connect(self._on_import_acquisition)
        file_menu.addAction(act_import_acq)

        act_import = QtGui.QAction("Import from source-dir…", self)
        act_import.triggered.connect(self._on_import)
        file_menu.addAction(act_import)

        act_export = QtGui.QAction("Export all to export-dir…", self)
        act_export.triggered.connect(self._on_export)
        file_menu.addAction(act_export)

        file_menu.addSeparator()
        act_quit = QtGui.QAction("&Quit", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        view_menu = self.menuBar().addMenu("&View")
        self._view_menu = view_menu  # for future viz docks to register into
        act_refresh = QtGui.QAction("&Refresh", self)
        act_refresh.setShortcut("Ctrl+R")
        act_refresh.triggered.connect(self._refresh_all)
        view_menu.addAction(act_refresh)
        view_menu.addSeparator()
        # Each dock's toggleViewAction shows up here and stays in sync with
        # docked / floating / hidden state automatically.
        view_menu.addAction(self._detail_dock.toggleViewAction())

    def _wire(self) -> None:
        self._filter_bar.filtersChanged.connect(self._refresh_table)
        self._table.selectionModel().currentRowChanged.connect(self._on_selection_changed)
        self._detail.saved.connect(lambda _name: self._refresh_table())
        self._detail.datasetActivationRequested.connect(self._dataset_panel.set_active_dataset)
        self._dataset_panel.addRequested.connect(self._on_dataset_add)
        self._dataset_panel.removeRequested.connect(self._on_dataset_remove)
        self._dataset_panel.exportRequested.connect(self._on_dataset_export)
        self._dataset_panel.extractRequested.connect(self._on_run_extract_for_dataset)
        self._dataset_panel.printTreesRequested.connect(self._on_print_trees_for_dataset)
        self._dataset_panel.filterChanged.connect(self._on_dataset_filter_changed)
        self._dataset_filter_id: int | None = None

        # Right-click on column header → show/hide any column.
        hdr = self._table.horizontalHeader()
        hdr.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        hdr.customContextMenuRequested.connect(self._on_header_context_menu)

        # Right-click on browser rows → actions for selected series.
        self._table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_row_context_menu)

        # Live pipeline-status polling: refresh the Pipeline column whenever any
        # PipelineStepRun row has status=RUNNING (i.e., the worker is active).
        self._pipeline_timer = QtCore.QTimer(self)
        self._pipeline_timer.setInterval(5000)
        self._pipeline_timer.timeout.connect(self._poll_pipeline_status)
        self._pipeline_timer.start()

    # --- session helpers --------------------------------------------------

    @contextmanager
    def _session_cm(self):
        with database.session_scope() as session:
            yield session

    def _maybe_init_db_and_refresh(self) -> None:
        try:
            database.create_all()
        except Exception as exc:  # pragma: no cover — first-launch DB failure
            QtWidgets.QMessageBox.critical(
                self,
                "Database error",
                f"Could not connect / initialize DB at {settings.db_url}:\n{exc}",
            )
            return
        self._refresh_all()
        self.statusBar().showMessage(
            f"connected: {settings.db_url}  |  source: {settings.source_dir}  |  export: {settings.export_dir}"
        )

    def _current_filters(self):
        f = self._filter_bar.current_filters()
        f.dataset_id = self._dataset_filter_id
        return f

    def _refresh_all(self) -> None:
        with self._session_cm() as session:
            self._filter_bar.populate_from_db(session)
            self._model.refresh(session, self._current_filters())
            self._dataset_panel.refresh()
        self._apply_default_column_widths()
        self._update_row_count()

    def _refresh_table(self) -> None:
        with self._session_cm() as session:
            self._model.refresh(session, self._current_filters())
        self._update_row_count()

    def _apply_default_column_widths(self) -> None:
        """Initial column widths tuned for typical lab data.

        Idempotent — called once after model populate; users can drag columns
        afterwards and we don't overwrite their choice.
        """
        if getattr(self, "_widths_set", False):
            return
        widths = {
            "series_name": 280,
            "date_acquired": 90,
            "person": 80,
            "strain_name": 100,
            "treatments": 140,
            "reporter_gene": 100,
            "status": 80,
            "pipeline_summary": 140,
            "edited_by": 60,
            "edited_timepts": 75,
            "edited_cells": 55,
            "partial_editing_code": 220,
            # comments stretches via setStretchLastSection earlier; no width here.
            "updated_at": 130,
        }
        from .models import COLUMNS
        for idx, (col, _) in enumerate(COLUMNS):
            px = widths.get(col)
            if px is not None:
                self._table.horizontalHeader().resizeSection(idx, px)
        self._widths_set = True

    def _on_dataset_filter_changed(self, dataset_id, dataset_name: str) -> None:
        self._dataset_filter_id = dataset_id
        self._refresh_table()
        if dataset_id is None:
            self.statusBar().showMessage("dataset filter cleared", 3000)
        else:
            self.statusBar().showMessage(
                f"filtering table to dataset {dataset_name!r}", 3000
            )

    def _update_row_count(self) -> None:
        n = self._model.rowCount()
        msg = self.statusBar().currentMessage()
        # Replace any prior counts suffix
        base = msg.split(" | rows: ")[0]
        self.statusBar().showMessage(f"{base} | rows: {n}")

    # --- table → detail ---------------------------------------------------

    def _on_selection_changed(
        self, current: QtCore.QModelIndex, previous: QtCore.QModelIndex
    ) -> None:
        if not current.isValid():
            return
        name = self._model.series_name_at(current.row())
        if not name:
            return
        if name == self._detail.loaded_series_name():
            return
        if self._detail.is_dirty() and not self._prompt_discard_unsaved(
            f"You have unsaved changes to {self._detail.loaded_series_name()!r}."
        ):
            # Revert selection to the previously loaded series without retriggering this handler.
            if previous.isValid():
                self._table.selectionModel().blockSignals(True)
                try:
                    self._table.selectionModel().setCurrentIndex(
                        previous, QtCore.QItemSelectionModel.ClearAndSelect | QtCore.QItemSelectionModel.Rows
                    )
                finally:
                    self._table.selectionModel().blockSignals(False)
            return
        self._detail.load_series(name)

    def _prompt_discard_unsaved(self, message: str) -> bool:
        """Ask the user whether to discard unsaved detail-panel edits.

        Returns True if the caller should proceed (user chose Discard or Save).
        Returns False if the user chose Cancel and the caller should abort.
        """
        msg = QtWidgets.QMessageBox(self)
        msg.setIcon(QtWidgets.QMessageBox.Warning)
        msg.setWindowTitle("Unsaved changes")
        msg.setText(message)
        msg.setInformativeText("Save your changes, discard them, or cancel?")
        save_btn = msg.addButton("Save", QtWidgets.QMessageBox.AcceptRole)
        discard_btn = msg.addButton("Discard", QtWidgets.QMessageBox.DestructiveRole)
        msg.addButton("Cancel", QtWidgets.QMessageBox.RejectRole)
        msg.setDefaultButton(save_btn)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked is save_btn:
            self._detail._on_save()  # noqa: SLF001 — internal call by design
            return not self._detail.is_dirty()  # if save was vetoed (conflict), abort
        if clicked is discard_btn:
            return True
        return False  # Cancel

    def closeEvent(self, event) -> None:
        if self._detail.is_dirty():
            if not self._prompt_discard_unsaved(
                f"You have unsaved changes to {self._detail.loaded_series_name()!r}."
            ):
                event.ignore()
                return
        event.accept()

    def _selected_series_names(self) -> list[str]:
        rows = self._table.selectionModel().selectedRows()
        names: list[str] = []
        for idx in rows:
            n = self._model.series_name_at(idx.row())
            if n:
                names.append(n)
        return names

    # --- menu actions -----------------------------------------------------

    def _on_import(self) -> None:
        src_str = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Choose XML source directory (will be read-only)",
            str(settings.source_dir),
        )
        if not src_str:
            return
        progress = QtWidgets.QProgressDialog(
            "Importing XML files…", "Cancel", 0, 0, self
        )
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.show()
        QtWidgets.QApplication.processEvents()
        try:
            with self._session_cm() as session:
                report = import_dir(session, source_dir=Path(src_str))
        finally:
            progress.close()
        QtWidgets.QMessageBox.information(self, "Import complete", report.summary())
        self._refresh_all()

    def _on_export(self) -> None:
        out_str = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Choose export directory (source-dir is never written to)",
            str(settings.export_dir),
        )
        if not out_str:
            return
        with self._session_cm() as session:
            report = export_all(session, export_dir=Path(out_str))
        QtWidgets.QMessageBox.information(self, "Export complete", report.summary())

    # --- dataset actions --------------------------------------------------

    def _on_dataset_add(self, dataset_name: str) -> None:
        selected = self._selected_series_names()
        if not selected:
            QtWidgets.QMessageBox.information(
                self, "No selection", "Select series in the browser first."
            )
            return
        try:
            with self._session_cm() as session:
                q_datasets.add_series(session, dataset_name, selected)
        except q_datasets.DatasetError as exc:
            QtWidgets.QMessageBox.warning(self, "Add failed", str(exc))
            return
        self.statusBar().showMessage(
            f"added {len(selected)} series to dataset {dataset_name!r}",
            3000,
        )
        self._reload_detail_if_loaded()

    def _on_dataset_remove(self, dataset_name: str) -> None:
        selected = self._selected_series_names()
        if not selected:
            return
        try:
            with self._session_cm() as session:
                q_datasets.remove_series(session, dataset_name, selected)
        except q_datasets.DatasetError as exc:
            QtWidgets.QMessageBox.warning(self, "Remove failed", str(exc))
            return
        self.statusBar().showMessage(
            f"removed {len(selected)} series from {dataset_name!r}", 3000
        )
        self._reload_detail_if_loaded()

    def _reload_detail_if_loaded(self) -> None:
        """Refresh the detail panel so its 'Member of' list reflects the
        membership change the user just made via the dataset bar."""
        name = self._detail._series_name  # noqa: SLF001 - tiny GUI internal
        if name:
            self._detail.load_series(name)

    def _on_dataset_export(self, dataset_name: str, output: Path) -> None:
        try:
            with self._session_cm() as session:
                q_datasets.export_list_file(session, dataset_name, output)
        except q_datasets.DatasetError as exc:
            QtWidgets.QMessageBox.warning(self, "Export failed", str(exc))
            return
        QtWidgets.QMessageBox.information(
            self, "Export complete", f"Wrote {output}"
        )

    # --- column header context menu (show/hide columns) ------------------

    def _on_header_context_menu(self, pos: QtCore.QPoint) -> None:
        from .models import COLUMNS
        header = self._table.horizontalHeader()
        menu = QtWidgets.QMenu(self)
        for idx, (_, label) in enumerate(COLUMNS):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(not header.isSectionHidden(idx))
            action.toggled.connect(
                lambda checked, i=idx: self._table.horizontalHeader().setSectionHidden(i, not checked)
            )
        menu.exec(header.mapToGlobal(pos))

    # --- row right-click context menu ------------------------------------

    def _on_row_context_menu(self, pos: QtCore.QPoint) -> None:
        selected = self._selected_series_names()
        if not selected:
            return
        menu = QtWidgets.QMenu(self)
        n = len(selected)
        n_suffix = f" ({n} selected)" if n > 1 else ""

        # Selection expansion
        act_expand = menu.addAction(f"Select all from same acquisition{n_suffix}")
        act_expand.triggered.connect(self._on_select_same_acquisition)
        menu.addSeparator()

        # Bulk edit metadata
        act_bulk = menu.addAction(f"Bulk edit metadata…{n_suffix}")
        act_bulk.triggered.connect(self._on_bulk_edit_metadata)

        # Microscopy details — single-series only (the dialog is per-series).
        # Greyed out when more than one row is selected; the rest of the menu
        # works as before.
        act_micro = menu.addAction("Microscopy details…")
        act_micro.setEnabled(n == 1)
        if n == 1:
            act_micro.triggered.connect(self._on_microscopy_details)

        # Re-run pipeline
        act_rerun = menu.addAction(f"Re-run pipeline…{n_suffix}")
        act_rerun.triggered.connect(self._on_rerun_pipeline)

        menu.addSeparator()

        # Legacy analysis tools (extract.sh + PrintTrees.pl)
        act_extract = menu.addAction(f"Run extract steps…{n_suffix}")
        act_extract.triggered.connect(self._on_run_extract)
        act_trees = menu.addAction(f"Print trees…{n_suffix}")
        act_trees.triggered.connect(self._on_print_trees)

        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _on_select_same_acquisition(self) -> None:
        """Expand the current selection to include every series sharing an
        acquisition with any currently-selected row.

        For legacy imports with no acquisition_id, falls back to name-based
        grouping: series whose names share a `<stem>_L<N>` prefix are
        treated as one acquisition.
        """
        import re
        from sqlalchemy import select
        from ..models import Series

        # `<stem>_L<N>` with optional `_xx`/`_yy` annotation suffix (real legacy data).
        _LSUFFIX_RE = re.compile(r"^(?P<stem>.+?)_L\d+(?:_(?:xx|yy))?$")

        def _name_stem(name: str) -> str | None:
            m = _LSUFFIX_RE.match(name)
            return m.group("stem") if m else None

        seed = self._selected_series_names()
        if not seed:
            return
        with self._session_cm() as s:
            acq_ids: set[int] = set()
            seed_stems: set[str] = set()
            for name in seed:
                row = s.execute(
                    select(Series).where(Series.series_name == name)
                ).scalar_one_or_none()
                if row is None:
                    continue
                if row.acquisition_id is not None:
                    acq_ids.add(row.acquisition_id)
                else:
                    stem = _name_stem(name)
                    if stem:
                        seed_stems.add(stem)
            if not acq_ids and not seed_stems:
                QtWidgets.QMessageBox.information(
                    self,
                    "No acquisition",
                    "None of the selected series are linked to an acquisition,\n"
                    "and their names don't match the legacy `<stem>_L<N>` pattern.",
                )
                return
            all_names: set[str] = set()
            if acq_ids:
                all_names.update(
                    s.execute(
                        select(Series.series_name).where(Series.acquisition_id.in_(acq_ids))
                    ).scalars()
                )
            if seed_stems:
                # Pull candidates that COULD match — server-side prefix filter
                # avoids loading every Series. Then verify the stem extraction
                # in Python (regex anchor is post-prefix).
                for stem in seed_stems:
                    candidates = s.execute(
                        select(Series.series_name).where(
                            Series.series_name.like(f"{stem}_L%")
                        )
                    ).scalars()
                    for cand in candidates:
                        if _name_stem(cand) == stem:
                            all_names.add(cand)
        # Always keep the seed selection itself.
        all_names.update(seed)
        # Update the table selection in-place.
        sel_model = self._table.selectionModel()
        sel_model.blockSignals(True)
        try:
            sel_model.clearSelection()
            n_cols = self._model.columnCount()
            for r in range(self._model.rowCount()):
                name = self._model.series_name_at(r)
                if name in all_names:
                    top = self._model.index(r, 0)
                    bottom = self._model.index(r, n_cols - 1)
                    sel_model.select(
                        QtCore.QItemSelection(top, bottom),
                        QtCore.QItemSelectionModel.Select,
                    )
        finally:
            sel_model.blockSignals(False)
        self.statusBar().showMessage(
            f"Selection expanded to {len(all_names)} series ({len(acq_ids)} acquisitions).",
            5000,
        )

    def _on_microscopy_details(self) -> None:
        names = self._selected_series_names()
        if len(names) != 1:
            return
        from .microscopy_dialog import MicroscopyDialog
        MicroscopyDialog(self._session_cm, names[0], parent=self).exec()

    def _on_bulk_edit_metadata(self) -> None:
        selected = self._selected_series_names()
        if not selected:
            return
        from .bulk_edit_dialog import BulkEditMetadataDialog
        dlg = BulkEditMetadataDialog(self._session_cm, selected, parent=self)
        if dlg.exec():
            self._refresh_table()

    def _on_rerun_pipeline(self) -> None:
        selected = self._selected_series_names()
        if not selected:
            return
        from .rerun_dialog import RerunPipelineDialog
        dlg = RerunPipelineDialog(self._session_cm, selected, parent=self)
        if dlg.exec():
            self._refresh_table()
            self.statusBar().showMessage(
                f"Re-queued pipeline for {len(selected)} series.", 5000
            )

    # --- legacy analysis tools (extract.sh + PrintTrees.pl) ---------------

    def _on_run_extract(self) -> None:
        names = self._selected_series_names()
        if not names:
            return
        from .extract_dialog import ExtractDialog
        ExtractDialog(
            self._session_cm, names,
            title_hint=f"{len(names)} selected", parent=self,
        ).exec()

    def _on_print_trees(self) -> None:
        names = self._selected_series_names()
        if not names:
            return
        from .print_trees_dialog import PrintTreesDialog
        PrintTreesDialog(
            self._session_cm, names,
            title_hint=f"{len(names)} selected", parent=self,
        ).exec()

    def _resolve_dataset_members(self, dataset_name: str) -> list[str]:
        from ..queries import datasets as q_datasets
        with self._session_cm() as session:
            ds = q_datasets.get_by_name(session, dataset_name)
            if ds is None:
                return []
            return sorted(s.series_name for s in ds.series)

    def _on_run_extract_for_dataset(self, dataset_name: str) -> None:
        names = self._resolve_dataset_members(dataset_name)
        if not names:
            QtWidgets.QMessageBox.information(
                self, "Empty dataset",
                f"Dataset {dataset_name!r} has no member series."
            )
            return
        from .extract_dialog import ExtractDialog
        ExtractDialog(
            self._session_cm, names,
            title_hint=f"dataset {dataset_name!r}", parent=self,
        ).exec()

    def _on_print_trees_for_dataset(self, dataset_name: str) -> None:
        names = self._resolve_dataset_members(dataset_name)
        if not names:
            QtWidgets.QMessageBox.information(
                self, "Empty dataset",
                f"Dataset {dataset_name!r} has no member series."
            )
            return
        from .print_trees_dialog import PrintTreesDialog
        PrintTreesDialog(
            self._session_cm, names,
            title_hint=f"dataset {dataset_name!r}", parent=self,
        ).exec()

    # --- pipeline import wizard -------------------------------------------

    def _on_import_acquisition(self) -> None:
        from .import_wizard import ImportWizard
        wizard = ImportWizard(self._session_cm, parent=self)
        if wizard.exec() == QtWidgets.QWizard.Accepted:
            self._refresh_all()
            self.statusBar().showMessage(
                "Acquisition import queued; worker running in background.", 8000
            )

    # --- live pipeline-status polling -------------------------------------

    def _poll_pipeline_status(self) -> None:
        """Refresh the Pipeline column if any step is currently RUNNING."""
        from sqlalchemy import func, select
        from ..models import PipelineStepRun, RunStatus
        try:
            with self._session_cm() as s:
                n_running = s.scalar(
                    select(func.count())
                    .select_from(PipelineStepRun)
                    .where(PipelineStepRun.status == RunStatus.RUNNING)
                )
        except Exception:
            return
        if n_running:
            self._refresh_table()
