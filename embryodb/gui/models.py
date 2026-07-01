"""Qt table model wrapping Series rows.

Reads through `queries.series.list_series` so the GUI sees identical results
to the CLI. The model is refreshable: call `refresh(filters)` whenever filters
change to re-query the DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from qtpy import QtCore
from sqlalchemy.orm import Session

from ..models import PipelineStepRun, RunStatus, Series, Status
from ..queries import series as q_series


@dataclass
class Filters:
    """All 8 filter values plus dataset/pagination knobs. None / empty = no filter."""

    person: list[str] = field(default_factory=list)
    strain: list[str] = field(default_factory=list)
    treatments: list[str] = field(default_factory=list)
    edited_by: list[str] = field(default_factory=list)
    reporter_gene: list[str] = field(default_factory=list)
    status: list[Status] = field(default_factory=list)
    date_before: str | None = None
    date_after: str | None = None
    text: str | None = None
    text_in_comments_only: bool = False
    dataset_id: int | None = None
    limit: int | None = None


# Columns shown in the browser table — order maps directly to model columns.
# "Perturbation" is the display name of the `treatments` field; the underlying
# database column keeps the legacy name for round-trip XML compatibility.
COLUMNS: list[tuple[str, str]] = [
    ("series_name", "Series"),
    ("date_acquired", "Date"),
    ("person", "Person"),
    ("strain_name", "Strain"),
    ("treatments", "Perturbation"),
    ("reporter_gene", "Reporter"),
    ("status", "Status"),
    # v2: pipeline progress summary. Derived from PipelineStepRun rows
    # joined when the model refreshes; static text per series.
    ("pipeline_summary", "Pipeline"),
    ("edited_by", "Editor"),
    # edited_timepts is the column downstream tools actually use to scope
    # to curated data (modulated by Partial.pl when there's a partial
    # editing code). edited_cells is more approximate. Labelled "Edited
    # Time" to distinguish from Timepts (total acquired timepoints).
    ("edited_timepts", "Edited Time"),
    ("edited_cells", "Cells"),
    ("partial_editing_code", "Editing"),
    ("updated_at", "Updated"),
    # Comments is intentionally last so QHeaderView.setStretchLastSection
    # in the browser absorbs any horizontal slack — comments are the widest
    # free-text content and benefit most from the room.
    ("comments", "Comments"),
]


# Compact step labels for the pipeline summary cell.
_STEP_SHORT: dict[str, str] = {
    "stage_images": "stage",
    "stage_metadata": "meta",
    "write_acetree_config": "cfg",
    "write_embryodb_xml": "xml",
    "create_alias_symlink": "lnk",
    "write_matlab_params": "prm",
    "run_starrynite": "SN",
    "run_red_extract": "red",
    "run_measure": "meas",
}

_STATUS_GLYPH: dict[RunStatus, str] = {
    RunStatus.PENDING: ".",
    RunStatus.RUNNING: "*",
    RunStatus.COMPLETE: "✓",
    RunStatus.FAILED: "✗",
    RunStatus.SKIPPED: "-",
}


def _summarize_runs(runs: list[PipelineStepRun]) -> str:
    """One-line summary of a series' pipeline state.

    Empty for series that have no PipelineStepRun rows (legacy imports).
    For pipelined series, returns the count of completed steps + a compact
    glyph string showing per-step status in the canonical order.
    """
    if not runs:
        return ""
    by_step = {r.step: r.status for r in runs}
    parts: list[str] = []
    n_done = sum(1 for s in by_step.values() if s == RunStatus.COMPLETE)
    n_failed = sum(1 for s in by_step.values() if s == RunStatus.FAILED)
    for step in (
        "stage_images", "stage_metadata", "write_acetree_config",
        "write_embryodb_xml", "create_alias_symlink", "write_matlab_params",
        "run_starrynite", "run_red_extract", "run_measure",
    ):
        status = by_step.get(step)
        if status is None:
            continue
        parts.append(f"{_STEP_SHORT.get(step, step[:4])}{_STATUS_GLYPH.get(status, '?')}")
    head = f"{n_done}/{len(by_step)}"
    if n_failed:
        head += f"  {n_failed}!"
    return f"{head}  " + " ".join(parts)


class SeriesTableModel(QtCore.QAbstractTableModel):
    """List-of-rows table; one row per Series. Pure read model.

    The model owns no Session — it accepts a Session per refresh so the
    caller controls the unit of work. Rows are cached as plain dicts so the
    UI can render even after the session closes.
    """

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, object]] = []
        # Remember the user's chosen sort so a periodic refresh() (which
        # re-queries in DB order) can re-apply it — otherwise the live
        # pipeline poll silently resets the sort every few seconds.
        self._sort_column: int | None = None
        self._sort_order: int = QtCore.Qt.AscendingOrder

    # --- public API ------------------------------------------------------

    def refresh(self, session: Session, filters: Filters) -> None:
        self.beginResetModel()
        rows = q_series.list_series(
            session,
            person=filters.person,
            strain=filters.strain,
            treatments=filters.treatments,
            edited_by=filters.edited_by,
            reporter_gene=filters.reporter_gene,
            status=filters.status,
            date_before=filters.date_before,
            date_after=filters.date_after,
            text=filters.text,
            text_in_comments_only=filters.text_in_comments_only,
            dataset_id=filters.dataset_id,
            limit=filters.limit,
        )
        # Bulk-fetch PipelineStepRun rows in one query so we don't N+1.
        from sqlalchemy import select
        ids = [r.id for r in rows]
        runs_by_series: dict[int, list[PipelineStepRun]] = {i: [] for i in ids}
        if ids:
            for run in session.execute(
                select(PipelineStepRun).where(PipelineStepRun.series_id.in_(ids))
            ).scalars():
                runs_by_series.setdefault(run.series_id, []).append(run)
        self._rows = [
            {**self._row_to_dict(r), "pipeline_summary": _summarize_runs(runs_by_series.get(r.id, []))}
            for r in rows
        ]
        self._apply_sort()
        self.endResetModel()

    def row_at(self, row: int) -> dict[str, object] | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def series_name_at(self, row: int) -> str | None:
        r = self.row_at(row)
        return r["series_name"] if r else None  # type: ignore[return-value]

    def row_for_series(self, name: str) -> int | None:
        for i, r in enumerate(self._rows):
            if r.get("series_name") == name:
                return i
        return None

    # --- QAbstractTableModel ---------------------------------------------

    def rowCount(self, parent: QtCore.QModelIndex = QtCore.QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QtCore.QModelIndex = QtCore.QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(
        self,
        section: int,
        orientation: int,
        role: int = QtCore.Qt.DisplayRole,
    ) -> object:
        if role != QtCore.Qt.DisplayRole:
            return None
        if orientation == QtCore.Qt.Horizontal:
            return COLUMNS[section][1]
        return section + 1

    def data(self, index: QtCore.QModelIndex, role: int = QtCore.Qt.DisplayRole) -> object:
        if not index.isValid():
            return None
        if role == QtCore.Qt.DisplayRole:
            attr = COLUMNS[index.column()][0]
            value = self._rows[index.row()].get(attr, "")
            return self._render(value)
        if role == QtCore.Qt.ToolTipRole:
            attr = COLUMNS[index.column()][0]
            return self._render(self._rows[index.row()].get(attr, ""))
        return None

    def sort(self, column: int, order: int) -> None:
        if not (0 <= column < len(COLUMNS)):
            return
        self._sort_column = column
        self._sort_order = order
        self.beginResetModel()
        self._apply_sort()
        self.endResetModel()

    def _apply_sort(self) -> None:
        """Sort `self._rows` in place by the remembered column/order.

        No-op until the user has picked a sort. Called both from `sort()`
        (user click) and `refresh()` (so a periodic re-query keeps the
        user's chosen order instead of reverting to DB order). Caller owns
        the beginResetModel/endResetModel bracket.
        """
        if self._sort_column is None:
            return
        attr = COLUMNS[self._sort_column][0]
        reverse = self._sort_order == QtCore.Qt.DescendingOrder
        self._rows.sort(
            key=lambda r: ("" if r.get(attr) is None else self._render(r[attr]).lower()),
            reverse=reverse,
        )

    # --- helpers ---------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: Series) -> dict[str, object]:
        return {
            "series_name": row.series_name,
            "date_acquired": row.date_acquired,
            "person": row.person,
            "strain_name": row.strain_name,
            "treatments": row.treatments,
            "reporter_gene": row.reporter_gene,
            "image_loc": row.image_loc,
            "timepts": row.timepts,
            "annot_loc": row.annot_loc,
            "acetree_config": row.acetree_config,
            "edited_by": row.edited_by,
            "edited_timepts": row.edited_timepts,
            "edited_cells": row.edited_cells,
            "partial_editing_code": row.partial_editing_code,
            "comments": row.comments,
            "status": row.status,
            "updated_at": row.updated_at,
            "version": row.version,
        }

    @staticmethod
    def _render(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, Status):
            return value.value
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M")
        return str(value)
