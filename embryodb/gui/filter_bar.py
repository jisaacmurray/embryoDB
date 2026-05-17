"""Filter bar above the browser table.

Houses the 8 filters from the Java GUI:
  person | reporter_gene | strain | treatments | edited_by | status | date | text

Each filter is a multi-select dropdown except date (range) and text (line edit).
Filters compose live: the bar emits `filtersChanged` whenever anything changes,
and the MainWindow re-runs the query.
"""

from __future__ import annotations

from qtpy import QtCore, QtWidgets
from sqlalchemy.orm import Session

from ..models import Status
from ..queries import series as q_series
from .models import Filters


class _MultiSelectButton(QtWidgets.QToolButton):
    """Compact multi-select dropdown with a search box.

    For large vocabularies (strains, editors, …) plain dropdowns are
    unusable. We embed a QLineEdit at the top of the menu; typing substring
    text hides any item whose label doesn't contain the substring
    (case-insensitive). Selected items always stay visible at the top so
    the user can see what they've chosen even while filtering.
    """

    selectionChanged = QtCore.Signal(list)

    def __init__(self, title: str, values: list[str], parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self._values: list[str] = values
        self._selected: set[str] = set()
        self._actions: dict[str, QtWidgets.QAction] = {}

        self.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)

        self._menu = QtWidgets.QMenu(self)
        # Cap menu height (~15 rows) + width (~50 chars). Long values are
        # ellipsized in the action label; full text lives in the action's
        # tooltip. Real lab data has occasional megabyte-long strings from
        # database errors; we don't want them to blow out the screen.
        fm = self.fontMetrics()
        self._menu.setMaximumHeight(fm.height() * 15 + 96)
        self._menu.setMaximumWidth(fm.averageCharWidth() * 55 + 48)
        self._menu.setStyleSheet("QMenu { menu-scrollable: 1; }")
        # Search line + clear action are persistent: we never call menu.clear()
        # because that would delete the QWidgetAction wrapping our search box.
        # Instead we remove only the value-row QActions.
        self._search_widget = QtWidgets.QWidgetAction(self._menu)
        self._search_box = QtWidgets.QLineEdit()
        self._search_box.setPlaceholderText("Search…")
        self._search_box.setClearButtonEnabled(True)
        self._search_box.textChanged.connect(self._apply_search)
        container = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(container)
        h.setContentsMargins(6, 4, 6, 4)
        h.addWidget(self._search_box)
        self._search_widget.setDefaultWidget(container)
        self._menu.addAction(self._search_widget)

        self._clear_action = self._menu.addAction("Clear")
        self._clear_action.triggered.connect(self.clear_selection)
        self._separator = self._menu.addSeparator()

        self._populate_menu()
        self.setMenu(self._menu)
        self._refresh_label()

    def set_values(self, values: list[str]) -> None:
        self._values = values
        self._selected &= set(values)
        self._populate_menu()
        self._refresh_label()

    def selected(self) -> list[str]:
        return sorted(self._selected)

    def clear_selection(self) -> None:
        if not self._selected:
            return
        self._selected.clear()
        for action in self._actions.values():
            action.setChecked(False)
        self._refresh_label()
        self.selectionChanged.emit([])

    # --- menu construction / filtering -----------------------------------

    def _populate_menu(self) -> None:
        # Remove only the value-row actions; keep search box, clear action,
        # and separator alive (clearing the menu would delete the QWidgetAction).
        for action in self._actions.values():
            self._menu.removeAction(action)
        self._actions.clear()

        self._clear_action.setText(f"Clear (showing {len(self._values)})")

        order = sorted(
            self._values, key=lambda v: (v not in self._selected, v.lower())
        )
        for v in order:
            display = v if len(v) <= 50 else v[:47] + "…"
            action = self._menu.addAction(display)
            if display != v:
                action.setToolTip(v)
            action.setCheckable(True)
            action.setChecked(v in self._selected)
            action.toggled.connect(lambda checked, value=v: self._toggle(value, checked))
            self._actions[v] = action

        self._search_box.blockSignals(True)
        self._search_box.clear()
        self._search_box.blockSignals(False)
        self._apply_search("")

    def _apply_search(self, text: str) -> None:
        needle = text.strip().lower()
        for value, action in self._actions.items():
            visible = (
                not needle
                or needle in value.lower()
                or value in self._selected
            )
            action.setVisible(visible)

    def _toggle(self, value: str, checked: bool) -> None:
        if checked:
            self._selected.add(value)
        else:
            self._selected.discard(value)
        self._refresh_label()
        self.selectionChanged.emit(self.selected())

    def _refresh_label(self) -> None:
        n = len(self._selected)
        suffix = f" ({n})" if n else ""
        self.setText(f"{self._title}{suffix}")

    # --- focus polish ----------------------------------------------------

    def showMenu(self) -> None:  # type: ignore[override]
        super().showMenu()
        # Give the search box focus when the menu opens so the user can type immediately.
        self._search_box.setFocus()


class FilterBar(QtWidgets.QWidget):
    filtersChanged = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._person = _MultiSelectButton("Person", [])
        self._strain = _MultiSelectButton("Strain", [])
        self._gene = _MultiSelectButton("Reporter", [])
        self._treatments = _MultiSelectButton("Perturbation", [])
        self._edited_by = _MultiSelectButton("Editor", [])
        self._status = _MultiSelectButton(
            "Status", [s.value for s in Status]
        )

        for btn in (
            self._person, self._strain, self._gene, self._treatments,
            self._edited_by, self._status,
        ):
            btn.selectionChanged.connect(self.filtersChanged.emit)
            layout.addWidget(btn)

        # Date range
        layout.addWidget(QtWidgets.QLabel("Date"))
        self._date_after = QtWidgets.QLineEdit()
        self._date_after.setPlaceholderText("after YYYYMMDD")
        self._date_after.setFixedWidth(110)
        self._date_after.editingFinished.connect(self.filtersChanged.emit)
        layout.addWidget(self._date_after)
        self._date_before = QtWidgets.QLineEdit()
        self._date_before.setPlaceholderText("before YYYYMMDD")
        self._date_before.setFixedWidth(110)
        self._date_before.editingFinished.connect(self.filtersChanged.emit)
        layout.addWidget(self._date_before)

        layout.addStretch(1)

        # Text search
        self._text_in_comments_only = QtWidgets.QCheckBox("Comments only")
        self._text_in_comments_only.setChecked(True)
        self._text_in_comments_only.toggled.connect(self.filtersChanged.emit)
        layout.addWidget(self._text_in_comments_only)

        self._text = QtWidgets.QLineEdit()
        self._text.setPlaceholderText("Search…")
        self._text.setFixedWidth(220)
        self._text.textChanged.connect(self._on_text_changed)
        layout.addWidget(self._text)

        # debounce text edits so we don't re-query on every keystroke
        self._text_timer = QtCore.QTimer(self)
        self._text_timer.setSingleShot(True)
        self._text_timer.setInterval(250)
        self._text_timer.timeout.connect(self.filtersChanged.emit)

    # --- public API -------------------------------------------------------

    def populate_from_db(self, session: Session) -> None:
        """Fill the multi-select dropdowns with distinct values from the DB."""
        self._person.set_values(q_series.distinct_values(session, "person"))
        self._strain.set_values(q_series.distinct_values(session, "strain_name"))
        self._gene.set_values(q_series.distinct_values(session, "reporter_gene"))
        self._treatments.set_values(q_series.distinct_values(session, "treatments"))
        self._edited_by.set_values(q_series.distinct_values(session, "edited_by"))

    def current_filters(self) -> Filters:
        return Filters(
            person=self._person.selected(),
            strain=self._strain.selected(),
            treatments=self._treatments.selected(),
            edited_by=self._edited_by.selected(),
            reporter_gene=self._gene.selected(),
            status=[Status(v) for v in self._status.selected()],
            date_before=self._date_before.text().strip() or None,
            date_after=self._date_after.text().strip() or None,
            text=self._text.text().strip() or None,
            text_in_comments_only=self._text_in_comments_only.isChecked(),
        )

    # --- internals --------------------------------------------------------

    def _on_text_changed(self, _: str) -> None:
        self._text_timer.start()
