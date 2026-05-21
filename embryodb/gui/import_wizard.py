"""Multi-page import wizard for pipeline acquisitions.

Reachable from File → "Import acquisition…". Guides the user through:
  Page 1 (Source):   pick acquisition dir + protocol + parser; preview positions
  Page 2 (Metadata): per-acquisition fields + tunable parameter overrides
  Page 3 (Targets):  image_loc_root, alias_root, legacy_xml_dir
  Page 4 (Confirm):  series list; Finish triggers import + worker spawn

On Finish the wizard calls import_acquisition() synchronously (inline steps
1-6) then spawns the worker for steps 7-9.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from qtpy import QtCore, QtWidgets

from ..config import settings
from ..parsers.filename import list_parsers

_DEFAULT_ACQ_DIR = "/murrlab3/Images"
_SETTINGS_KEY = "acquisition/default_dir"


def _acq_dir_default() -> str:
    """Return the user's saved default acquisition directory, or /murrlab3/Images."""
    s = QtCore.QSettings("MurrayLab", "embryoDB")
    saved = s.value(_SETTINGS_KEY, "")
    if saved and Path(str(saved)).is_dir():
        return str(saved)
    return _DEFAULT_ACQ_DIR if Path(_DEFAULT_ACQ_DIR).is_dir() else str(Path.home())


def _save_acq_dir_default(chosen: str) -> None:
    """Persist the parent of the chosen directory as the new default."""
    parent = str(Path(chosen).parent)
    QtCore.QSettings("MurrayLab", "embryoDB").setValue(_SETTINGS_KEY, parent)
from ..parsers.matlab_params import TUNABLE_KEYS
from ..pipeline.orchestrate import (
    DEFAULT_ALIAS_ROOT,
    DEFAULT_IMAGE_LOC_ROOT,
    ImportOptions,
    import_acquisition,
)
from ..pipeline.stage import StagePlan, plan_acquisition


# ---------------------------------------------------------------------------
# Page 1 — Source
# ---------------------------------------------------------------------------


class SourcePage(QtWidgets.QWizardPage):
    """Pick acquisition directory, Protocol, and filename parser."""

    def __init__(self, session_cm: Callable, parent=None) -> None:
        super().__init__(parent)
        self._session_cm = session_cm
        self._plan: StagePlan | None = None
        self.setTitle("Step 1 of 4 — Source")
        self.setSubTitle(
            "Choose the raw acquisition directory and the protocol that was used."
        )
        self._build()

    # --- construction --------------------------------------------------------

    def _build(self) -> None:
        layout = QtWidgets.QFormLayout(self)

        # Source directory
        dir_row = QtWidgets.QWidget()
        dir_hl = QtWidgets.QHBoxLayout(dir_row)
        dir_hl.setContentsMargins(0, 0, 0, 0)
        self._dir_edit = QtWidgets.QLineEdit()
        self._dir_btn = QtWidgets.QPushButton("Browse…")
        self._dir_btn.clicked.connect(self._browse_dir)
        dir_hl.addWidget(self._dir_edit)
        dir_hl.addWidget(self._dir_btn)
        layout.addRow("Acquisition dir:", dir_row)

        # Protocol combo (populated on initializePage)
        self._protocol_combo = QtWidgets.QComboBox()
        self._protocol_combo.setMinimumWidth(220)
        layout.addRow("Protocol:", self._protocol_combo)

        # Parser combo
        self._parser_combo = QtWidgets.QComboBox()
        for p in list_parsers():
            self._parser_combo.addItem(p.name, p.name)
        layout.addRow("Filename parser:", self._parser_combo)

        # Scan button
        self._scan_btn = QtWidgets.QPushButton("Scan for positions")
        self._scan_btn.clicked.connect(self._scan)
        layout.addRow("", self._scan_btn)

        # Results table
        self._positions_table = QtWidgets.QTableWidget(0, 5)
        self._positions_table.setHorizontalHeaderLabels(
            ["Position", "Series name", "Timepoints", "Planes", "Channels"]
        )
        self._positions_table.horizontalHeader().setStretchLastSection(True)
        self._positions_table.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers
        )
        self._positions_table.setSelectionMode(
            QtWidgets.QAbstractItemView.NoSelection
        )
        self._positions_table.setMinimumHeight(120)
        layout.addRow(self._positions_table)

        # Register fields (wizard's built-in required-field mechanism)
        self.registerField("source_dir*", self._dir_edit)

    def initializePage(self) -> None:
        self._populate_protocols()

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
                self._protocol_combo.addItem(row.name, row.id)
        except Exception:
            pass

    def _browse_dir(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose acquisition directory", _acq_dir_default()
        )
        if path:
            self._dir_edit.setText(path)
            _save_acq_dir_default(path)

    def _scan(self) -> None:
        source = self._dir_edit.text().strip()
        if not source or not Path(source).is_dir():
            QtWidgets.QMessageBox.warning(
                self, "Invalid directory", "Please choose a valid acquisition directory."
            )
            return
        proto = self._selected_protocol()
        if proto is None:
            QtWidgets.QMessageBox.warning(
                self, "No protocol", "Please select a protocol first."
            )
            return
        parser_name = self._parser_combo.currentData()

        progress = QtWidgets.QProgressDialog("Scanning…", None, 0, 0, self)
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.show()
        QtWidgets.QApplication.processEvents()
        try:
            self._plan = plan_acquisition(Path(source), proto, parser_name=parser_name)
        except Exception as exc:
            progress.close()
            QtWidgets.QMessageBox.critical(self, "Scan failed", str(exc))
            return
        progress.close()

        self._populate_positions_table()
        # Propagate the plan to the wizard so other pages can read it.
        self.wizard()._plan = self._plan  # type: ignore[union-attr]
        self.completeChanged.emit()

    def _selected_protocol(self):
        from sqlalchemy import select
        from ..models import Protocol

        proto_id = self._protocol_combo.currentData()
        if proto_id is None:
            return None
        try:
            with self._session_cm() as s:
                return s.get(Protocol, proto_id)
        except Exception:
            return None

    def _populate_positions_table(self) -> None:
        plan = self._plan
        if plan is None:
            return
        self._positions_table.setRowCount(len(plan.position_numbers))
        for row_idx, pos in enumerate(plan.position_numbers):
            pp = plan.positions[pos]
            n_channels = len(pp.files_by_channel)
            for col_idx, text in enumerate(
                [
                    str(pos),
                    pp.series_name,
                    str(pp.n_timepoints),
                    str(pp.planes_per_volume),
                    str(n_channels),
                ]
            ):
                item = QtWidgets.QTableWidgetItem(text)
                item.setTextAlignment(QtCore.Qt.AlignCenter)
                self._positions_table.setItem(row_idx, col_idx, item)
        self._positions_table.resizeColumnsToContents()

    def validatePage(self) -> bool:
        if not Path(self._dir_edit.text().strip()).is_dir():
            QtWidgets.QMessageBox.warning(
                self, "Invalid directory", "Acquisition directory does not exist."
            )
            return False
        if self._protocol_combo.currentData() is None:
            QtWidgets.QMessageBox.warning(
                self, "No protocol", "Please select a protocol."
            )
            return False
        if self._plan is None or not self._plan.positions:
            QtWidgets.QMessageBox.warning(
                self,
                "No positions found",
                "Scan the directory first, or no positions were detected.",
            )
            return False
        return True

    def isComplete(self) -> bool:
        return bool(
            self._dir_edit.text().strip()
            and self._protocol_combo.currentData() is not None
            and self._plan is not None
            and bool(self._plan.positions)
        )

    # Expose for wizard.accept()
    def source_dir(self) -> Path:
        return Path(self._dir_edit.text().strip())

    def protocol_id(self) -> int | None:
        return self._protocol_combo.currentData()

    def parser_name(self) -> str:
        return self._parser_combo.currentData() or "leica_tilescan"


# ---------------------------------------------------------------------------
# Page 2 — Metadata
# ---------------------------------------------------------------------------


class MetadataPage(QtWidgets.QWizardPage):
    """Per-acquisition metadata + tunable parameter overrides."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setTitle("Step 2 of 4 — Metadata")
        self.setSubTitle(
            "Enter per-acquisition metadata. Overrides apply on top of the "
            "protocol defaults; leave blank to keep the protocol value."
        )
        self._build()

    def _build(self) -> None:
        layout = QtWidgets.QFormLayout(self)

        self._person_edit = QtWidgets.QLineEdit(settings.user)
        layout.addRow("Person:", self._person_edit)

        self._strain_edit = QtWidgets.QLineEdit()
        layout.addRow("Strain:", self._strain_edit)

        self._perturbation_edit = QtWidgets.QLineEdit()
        layout.addRow("Perturbation:", self._perturbation_edit)

        self._reporter_edit = QtWidgets.QLineEdit()
        layout.addRow("Reporter:", self._reporter_edit)

        self._comments_edit = QtWidgets.QPlainTextEdit()
        self._comments_edit.setFixedHeight(60)
        layout.addRow("Comments:", self._comments_edit)

        layout.addRow(QtWidgets.QLabel("Parameter overrides (blank = use protocol default):"))

        self._params_table = QtWidgets.QTableWidget(len(TUNABLE_KEYS), 3)
        self._params_table.setHorizontalHeaderLabels(["Key", "Protocol default", "Override"])
        self._params_table.horizontalHeader().setStretchLastSection(True)
        self._params_table.verticalHeader().setVisible(False)
        self._params_table.setMinimumHeight(200)
        for row_idx, key in enumerate(TUNABLE_KEYS):
            key_item = QtWidgets.QTableWidgetItem(key)
            key_item.setFlags(key_item.flags() & ~QtCore.Qt.ItemIsEditable)
            self._params_table.setItem(row_idx, 0, key_item)
            self._params_table.setItem(row_idx, 1, QtWidgets.QTableWidgetItem(""))
            self._params_table.setItem(row_idx, 2, QtWidgets.QTableWidgetItem(""))
        layout.addRow(self._params_table)

    def initializePage(self) -> None:
        """Pre-fill protocol defaults into the table."""
        wizard = self.wizard()
        plan = getattr(wizard, "_plan", None)
        # Try to fetch protocol defaults from DB.
        source_page: SourcePage = wizard.page(0)  # type: ignore[assignment]
        proto_id = source_page.protocol_id() if source_page else None
        if proto_id is None:
            return
        try:
            with wizard._session_cm() as s:  # type: ignore[attr-defined]
                from ..models import Protocol
                proto = s.get(Protocol, proto_id)
                if proto and proto.defaults:
                    for row_idx, key in enumerate(TUNABLE_KEYS):
                        val = proto.defaults.get(key, "")
                        self._params_table.item(row_idx, 1).setText(str(val))
        except Exception:
            pass

    def parameter_overrides(self) -> dict[str, str]:
        overrides: dict[str, str] = {}
        for row_idx, key in enumerate(TUNABLE_KEYS):
            override_item = self._params_table.item(row_idx, 2)
            if override_item:
                val = override_item.text().strip()
                if val:
                    overrides[key] = val
        return overrides

    # Accessors for wizard.accept()
    def person(self) -> str:
        return self._person_edit.text().strip()

    def strain(self) -> str:
        return self._strain_edit.text().strip()

    def perturbation(self) -> str:
        return self._perturbation_edit.text().strip()

    def reporter(self) -> str:
        return self._reporter_edit.text().strip()

    def comments(self) -> str:
        return self._comments_edit.toPlainText().strip()


# ---------------------------------------------------------------------------
# Page 3 — Targets
# ---------------------------------------------------------------------------


class TargetsPage(QtWidgets.QWizardPage):
    """Confirm output paths and permissions."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setTitle("Step 3 of 4 — Targets")
        self.setSubTitle(
            "Confirm where staged images and metadata will be written. "
            "Defaults are the lab-standard locations."
        )
        self._build()

    def _build(self) -> None:
        layout = QtWidgets.QFormLayout(self)

        self._image_root_edit, row1 = self._path_row(str(DEFAULT_IMAGE_LOC_ROOT))
        layout.addRow("Image loc root:", row1)

        # Warn if the user-specific subdirectory doesn't exist yet.
        self._user_warn = QtWidgets.QLabel("")
        self._user_warn.setStyleSheet("color: #b8860b;")  # dark-yellow
        self._user_warn.setWordWrap(True)
        layout.addRow("", self._user_warn)
        self._image_root_edit.textChanged.connect(self._update_user_warning)

        # Alias: checkbox to enable/disable; location is always DEFAULT_ALIAS_ROOT
        # (not user-editable — the lab convention is fixed).
        alias_row = QtWidgets.QWidget()
        alias_hl = QtWidgets.QHBoxLayout(alias_row)
        alias_hl.setContentsMargins(0, 0, 0, 0)
        self._alias_check = QtWidgets.QCheckBox("Create symlink")
        self._alias_check.setChecked(True)
        self._alias_label = QtWidgets.QLabel(str(DEFAULT_ALIAS_ROOT))
        self._alias_label.setStyleSheet("color: grey;")
        self._alias_check.toggled.connect(self._alias_label.setEnabled)
        alias_hl.addWidget(self._alias_check)
        alias_hl.addWidget(self._alias_label)
        alias_hl.addStretch(1)
        layout.addRow("Alias symlink:", alias_row)

        self._legacy_xml_edit, row3 = self._path_row(str(settings.source_dir))
        layout.addRow("Legacy XML dir:", row3)

        # Overwrite checkbox: replaces existing staged TIFs in image_loc with
        # freshly-staged copies from the source directory. Useful when the
        # source data has been updated (e.g. additional timepoints copied in).
        self._overwrite_check = QtWidgets.QCheckBox(
            "Overwrite existing staged images (re-copy from source)"
        )
        self._overwrite_check.setToolTip(
            "When unchecked (default), stage_images skips files that already exist "
            "on disk — safe for re-runs that just want to fill in missing positions. "
            "Check this when the source acquisition has new or updated content that "
            "must replace what's already staged."
        )
        self._overwrite_check.setChecked(False)
        layout.addRow("", self._overwrite_check)

        self._disk_label = QtWidgets.QLabel("—")
        layout.addRow("Est. disk usage:", self._disk_label)

    def _path_row(self, default: str) -> tuple[QtWidgets.QLineEdit, QtWidgets.QWidget]:
        widget = QtWidgets.QWidget()
        hl = QtWidgets.QHBoxLayout(widget)
        hl.setContentsMargins(0, 0, 0, 0)
        edit = QtWidgets.QLineEdit(default)
        btn = QtWidgets.QPushButton("Browse…")
        btn.clicked.connect(lambda: self._browse_path(edit))
        hl.addWidget(edit)
        hl.addWidget(btn)
        return edit, widget

    def _browse_path(self, edit: QtWidgets.QLineEdit) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose directory", edit.text()
        )
        if path:
            edit.setText(path)

    def _update_user_warning(self) -> None:
        wizard = self.wizard()
        meta: MetadataPage | None = wizard.page(1) if wizard else None  # type: ignore
        user = meta.person() if meta else settings.user
        if not user:
            self._user_warn.setText("")
            return
        root = Path(self._image_root_edit.text().strip())
        user_dir = root / user / "images"
        if not user_dir.exists():
            self._user_warn.setText(
                f"Note: {user_dir} does not exist yet — it will be created on import."
            )
        else:
            self._user_warn.setText("")

    def initializePage(self) -> None:
        plan = getattr(self.wizard(), "_plan", None)
        if plan is not None:
            total_files = 0
            for pp in plan.positions.values():
                total_files += pp.n_timepoints * pp.planes_per_volume * len(pp.files_by_channel)
            est_gb = total_files * 4 / 2 / 1024
            self._disk_label.setText(f"~{est_gb:.1f} GB (estimate)")
        self._update_user_warning()

    def image_loc_root(self) -> Path:
        return Path(self._image_root_edit.text().strip())

    def alias_root(self) -> Path | None:
        if not self._alias_check.isChecked():
            return None
        return DEFAULT_ALIAS_ROOT

    def legacy_xml_dir(self) -> Path:
        return Path(self._legacy_xml_edit.text().strip())

    def overwrite_existing_images(self) -> bool:
        return self._overwrite_check.isChecked()


# ---------------------------------------------------------------------------
# Page 4 — Confirm
# ---------------------------------------------------------------------------


class ConfirmPage(QtWidgets.QWizardPage):
    """Final review before submitting."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setTitle("Step 4 of 4 — Confirm")
        self.setSubTitle(
            "Review the series that will be created. Click Finish to start the import."
        )
        self._build()

    def _build(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)

        self._table = QtWidgets.QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["Position", "Series name", "Image loc", "Metadata"]
        )
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        layout.addWidget(self._table)

    def initializePage(self) -> None:
        wizard: ImportWizard = self.wizard()  # type: ignore[assignment]
        plan = getattr(wizard, "_plan", None)
        if plan is None:
            return

        targets: TargetsPage = wizard.page(2)  # type: ignore[assignment]
        meta: MetadataPage = wizard.page(1)  # type: ignore[assignment]

        image_root = targets.image_loc_root()
        user = meta.person() or settings.user
        metadata_summary = ", ".join(
            x for x in [meta.strain(), meta.perturbation(), meta.reporter()] if x
        ) or "—"

        self._table.setRowCount(len(plan.position_numbers))
        for row_idx, pos in enumerate(plan.position_numbers):
            pp = plan.positions[pos]
            image_loc = image_root / user / "images" / pp.series_name
            for col_idx, text in enumerate(
                [str(pos), pp.series_name, str(image_loc), metadata_summary]
            ):
                item = QtWidgets.QTableWidgetItem(text)
                self._table.setItem(row_idx, col_idx, item)
        self._table.resizeColumnsToContents()


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------


class ImportWizard(QtWidgets.QWizard):
    """4-page acquisition import wizard.

    Usage:
        wizard = ImportWizard(session_cm, parent=main_window)
        if wizard.exec() == QWizard.Accepted:
            main_window._refresh_all()
    """

    def __init__(self, session_cm: Callable, parent=None) -> None:
        super().__init__(parent)
        self._session_cm = session_cm
        self._plan: StagePlan | None = None
        self.setWindowTitle("Import acquisition")
        self.setWizardStyle(QtWidgets.QWizard.ModernStyle)
        self.setMinimumSize(700, 520)
        self.setOption(QtWidgets.QWizard.NoBackButtonOnStartPage)

        self.addPage(SourcePage(session_cm, self))
        self.addPage(MetadataPage(self))
        self.addPage(TargetsPage(self))
        self.addPage(ConfirmPage(self))

    def accept(self) -> None:
        """Run the import when the user clicks Finish."""
        source_page: SourcePage = self.page(0)  # type: ignore[assignment]
        meta_page: MetadataPage = self.page(1)  # type: ignore[assignment]
        targets_page: TargetsPage = self.page(2)  # type: ignore[assignment]

        source_dir = source_page.source_dir()
        proto_id = source_page.protocol_id()
        parser_name = source_page.parser_name()

        opts = ImportOptions(
            image_loc_root=targets_page.image_loc_root(),
            alias_root=targets_page.alias_root(),
            user=meta_page.person() or settings.user,
            parameter_overrides=meta_page.parameter_overrides(),
            overwrite_existing_images=targets_page.overwrite_existing_images(),
            run_through_step="write_matlab_params",
        )

        progress = QtWidgets.QProgressDialog(
            "Running import (inline steps)…", None, 0, 0, self
        )
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.show()
        QtWidgets.QApplication.processEvents()

        try:
            with self._session_cm() as s:
                from ..models import Protocol
                proto = s.get(Protocol, proto_id)
                if proto is None:
                    raise ValueError(f"Protocol id={proto_id} not found")
                result = import_acquisition(
                    s,
                    source_dir=source_dir,
                    protocol=proto,
                    options=opts,
                    parser_name=parser_name,
                    person=meta_page.person(),
                    strain_name=meta_page.strain(),
                    treatments=meta_page.perturbation(),
                    reporter_gene=meta_page.reporter(),
                    comments=meta_page.comments(),
                    legacy_xml_dir=targets_page.legacy_xml_dir(),
                )
        except Exception as exc:
            progress.close()
            QtWidgets.QMessageBox.critical(
                self, "Import failed", f"Import did not complete:\n{exc}"
            )
            return
        finally:
            try:
                progress.close()
            except Exception:
                pass

        failed = [o for o in result.series_outcomes if o.failed_step]
        if failed:
            msg = "\n".join(
                f"  {o.series_name}: {o.failed_step} — {o.error}" for o in failed[:10]
            )
            QtWidgets.QMessageBox.warning(
                self,
                "Some series failed",
                f"{len(failed)} series had errors during inline steps:\n{msg}\n\n"
                "Completed series will proceed to StarryNite via the worker.",
            )

        # Spawn worker for steps 7-9.
        from ..pipeline.worker import spawn_worker
        spawned = spawn_worker()
        if spawned is None:
            status_msg = "Worker already running; new series will be picked up."
        else:
            status_msg = "Worker started for StarryNite + extraction steps."

        QtWidgets.QMessageBox.information(
            self,
            "Import queued",
            f"Created {len(result.series_outcomes)} series. {status_msg}",
        )
        super().accept()


__all__ = ["ImportWizard"]
