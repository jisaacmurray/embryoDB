"""Bulk-edit metadata across multiple series.

Reached via right-click → "Bulk edit metadata…" on a multi-row selection in
the browser. Fields left blank are not modified — only filled-in fields are
applied to all selected series. Status uses an explicit "(unchanged)" entry
because blank-string Status would be invalid.

Each updated series gets its version column bumped by 1 so optimistic
locking surfaces concurrent edits the same as a single-row save.
"""

from __future__ import annotations

from collections.abc import Callable

from qtpy import QtCore, QtWidgets

from ..config import settings
from .combo import use_exact_case
from ..identity import known_persons
from ..models import Series, Status
from ..queries import series as q_series

_UNCHANGED_LABEL = "(unchanged)"


class BulkEditMetadataDialog(QtWidgets.QDialog):
    """Apply the same field values to multiple series at once."""

    def __init__(
        self,
        session_cm: Callable,
        series_names: list[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._session_cm = session_cm
        self._series_names = list(series_names)
        self.setWindowTitle("Bulk edit metadata")
        self.setMinimumWidth(420)
        self._build()

    def _build(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)

        n = len(self._series_names)
        header = QtWidgets.QLabel(f"<b>Editing {n} series.</b>")
        layout.addWidget(header)
        note = QtWidgets.QLabel(
            "Blank fields leave the existing value unchanged. Filled fields "
            "overwrite the value on every selected series."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: grey; font-size: 11px;")
        layout.addWidget(note)

        form = QtWidgets.QFormLayout()
        layout.addLayout(form)

        # Autocomplete combos populated from existing distinct values.
        self._person_combo = self._editable_combo("person")
        form.addRow("Person:", self._person_combo)

        self._strain_combo = self._editable_combo("strain_name")
        form.addRow("Strain:", self._strain_combo)

        self._perturbation_combo = self._editable_combo("treatments")
        form.addRow("Perturbation:", self._perturbation_combo)

        self._reporter_combo = self._editable_combo("reporter_gene")
        form.addRow("Reporter:", self._reporter_combo)

        # Status is a closed enum, so use a non-editable combo with an
        # explicit "(unchanged)" placeholder.
        self._status_combo = QtWidgets.QComboBox()
        self._status_combo.addItem(_UNCHANGED_LABEL, None)
        for st in Status:
            self._status_combo.addItem(st.value, st.value)
        form.addRow("Status:", self._status_combo)

        # Show the targeted series as a small read-only list so the user can
        # confirm what they're about to mass-edit.
        preview = QtWidgets.QGroupBox(f"Targets ({n})")
        preview_vl = QtWidgets.QVBoxLayout(preview)
        listw = QtWidgets.QListWidget()
        listw.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        for name in self._series_names:
            listw.addItem(name)
        listw.setMaximumHeight(120)
        preview_vl.addWidget(listw)
        layout.addWidget(preview)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        btns.button(QtWidgets.QDialogButtonBox.Ok).setText(f"Apply to {n} series")
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _editable_combo(self, column: str) -> QtWidgets.QComboBox:
        combo = QtWidgets.QComboBox()
        combo.setEditable(True)
        use_exact_case(combo)
        combo.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        combo.setEditText("")  # default blank = unchanged
        # Populate suggestions from existing values
        try:
            with self._session_cm() as s:
                values = q_series.distinct_values(s, column)
            combo.addItem("")  # blank first so editText stays blank
            for v in values:
                combo.addItem(v)
        except Exception:
            combo.addItem("")
        combo.setEditText("")
        return combo

    def _on_accept(self) -> None:
        person = self._person_combo.currentText().strip()
        strain = self._strain_combo.currentText().strip()
        perturbation = self._perturbation_combo.currentText().strip()
        reporter = self._reporter_combo.currentText().strip()
        status_data = self._status_combo.currentData()

        # Build the update dict — only filled fields end up here.
        updates: dict[str, object] = {}
        if person:
            updates["person"] = person
        if strain:
            updates["strain_name"] = strain
        if perturbation:
            updates["treatments"] = perturbation
        if reporter:
            updates["reporter_gene"] = reporter
        if status_data is not None:
            updates["status"] = Status(status_data)

        if not updates:
            QtWidgets.QMessageBox.information(
                self, "Nothing to do", "No fields filled in — nothing to apply."
            )
            return

        # Guard a brand-new person attribution (typo guard) before mass-write.
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
                    "before.\n\nApply it as a new person to the selected "
                    "series?",
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No,
                )
                if reply != QtWidgets.QMessageBox.Yes:
                    return

        # Confirm before mass-writing.
        n = len(self._series_names)
        changed_summary = "\n  ".join(
            f"{k}: {v.value if hasattr(v, 'value') else v}" for k, v in updates.items()
        )
        reply = QtWidgets.QMessageBox.question(
            self,
            "Confirm bulk edit",
            f"Apply these changes to {n} series?\n\n  {changed_summary}",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return

        applied = 0
        not_found: list[str] = []
        applied_names: list[str] = []
        with self._session_cm() as s:
            for name in self._series_names:
                row = q_series.get_by_name(s, name)
                if row is None:
                    not_found.append(name)
                    continue
                for col, val in updates.items():
                    setattr(row, col, val)
                row.updated_by = settings.user
                row.version = (row.version or 0) + 1
                applied += 1
                applied_names.append(name)
            s.flush()

        # Mirror each updated series to its legacy XML so the legacy
        # Java/Perl tools see the bulk edits.
        from ..legacy_sync import sync_many
        n_written, n_failed = sync_many(applied_names)

        msg = f"Updated {applied} series."
        if not_found:
            msg += f"\nNot found: {len(not_found)}"
        if n_failed:
            msg += f"\nLegacy-XML sync failed for {n_failed} series (see stderr)."
        QtWidgets.QMessageBox.information(self, "Bulk edit complete", msg)
        self.accept()


__all__ = ["BulkEditMetadataDialog"]
