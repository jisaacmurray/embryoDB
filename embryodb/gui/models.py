"""Qt table model wrapping Series rows.

Reads through `queries.series.list_series` so the GUI sees identical results
to the CLI. The model is refreshable: call `refresh(filters)` whenever filters
change to re-query the DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from qtpy import QtCore, QtGui
from sqlalchemy.orm import Session

from ..models import PipelineStepRun, RunStatus, Series, SeriesDifficulty, Status
from ..queries import series as q_series
from ..queries.difficulty import OUT_OF_SCOPE_SUFFIX, preferred_model_version


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
    # v2: GT-free StarryNite curation-effort estimate, adjacent to the name so
    # the triage bucket reads at a glance. Derived from the latest run_starrynite
    # run's output_summary; color-coded via _EFFORT_COLORS in data().
    ("effort", "Effort"),
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
    "compute_timestamps": "time",
    "write_acetree_config": "cfg",
    "write_embryodb_xml": "xml",
    "create_alias_symlink": "lnk",
    "write_matlab_params": "prm",
    "run_starrynite": "SN",
    "compute_difficulty": "diff",
    "run_red_extract": "red",
    "output_excel": "xls",
    "run_measure": "meas",
    "align": "aln",
    "permissions": "perm",
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
    Otherwise a `done/total` head plus a compact per-unit glyph string in
    canonical order. The extract phase is shown as three rolled-up categories
    (output_excel / align / permissions) so a Partial or permissions failure is
    visible in the cell; the Details… popup breaks them back into steps.
    """
    if not runs:
        return ""
    from ..pipeline.extract_run import (
        EXTRACT_CATEGORY_COMPONENTS,
        SUMMARY_STEP_ORDER,
        rollup_category,
    )

    by_step = {r.step: r.status for r in runs}
    units: list[tuple[str, RunStatus]] = []
    for name in SUMMARY_STEP_ORDER:
        if name in EXTRACT_CATEGORY_COMPONENTS:
            status = rollup_category(by_step, EXTRACT_CATEGORY_COMPONENTS[name])
        else:
            status = by_step.get(name)
        if status is None:
            continue
        units.append((name, status))
    if not units:
        return ""
    n_done = sum(1 for _, s in units if s == RunStatus.COMPLETE)
    n_failed = sum(1 for _, s in units if s == RunStatus.FAILED)
    parts = [
        f"{_STEP_SHORT.get(n, n[:4])}{_STATUS_GLYPH.get(s, '?')}" for n, s in units
    ]
    head = f"{n_done}/{len(units)}"
    if n_failed:
        head += f"  {n_failed}!"
    return f"{head}  " + " ".join(parts)


# Triage bucket -> (friendly label, cell background). Buckets are matched by
# keyword because the predictor's triage token has varied ("hard",
# "curate/moderate", "leave-easy"). Backgrounds are deliberately pale so the
# black cell text stays readable.
_EFFORT_BUCKETS: list[tuple[tuple[str, ...], str, QtGui.QColor]] = [
    (("hard",), "Hard", QtGui.QColor(244, 199, 195)),        # pale red
    (("moderate", "curate"), "Moderate", QtGui.QColor(252, 232, 178)),  # pale amber
    (("easy", "leave"), "Easy", QtGui.QColor(199, 232, 201)),  # pale green
]


def _classify_triage(triage: str) -> tuple[str, QtGui.QColor | None]:
    """Map a raw predictor triage token to a (label, background) pair."""
    t = (triage or "").lower()
    for keys, label, color in _EFFORT_BUCKETS:
        if any(k in t for k in keys):
            return label, color
    return (triage or ""), None


def _difficulty_display(difficulty_rows: list) -> tuple[str, QtGui.QColor | None]:
    """Compact Effort-cell (text, background) from SeriesDifficulty rows.

    Colors by the to350 bucket; shows rounded to100 · to350 · toEnd integers.
    An out-of-scope vintage is marked `~` so a miscalibrated number is never
    read as a confident one.
    """
    if not difficulty_rows:
        return "", None
    # A series can carry several model vintages with differing stage sets;
    # keying by stage across them would mix vintages.
    chosen = preferred_model_version(difficulty_rows)
    by_stage = {r.stage: r for r in difficulty_rows if r.model_version == chosen}
    to350 = by_stage.get("to350")
    color: QtGui.QColor | None = None
    bucket_label = ""
    if to350 and to350.effort_bucket:
        bucket_label, color = _classify_triage(to350.effort_bucket)
    vals = []
    # to600 is the preferred "all" endpoint; fall back to toEnd for older rows.
    end_stage = "to600" if "to600" in by_stage else "toEnd"
    for stage in ("to100", "to350", end_stage):
        r = by_stage.get(stage)
        if r is not None and r.effort_predicted is not None:
            vals.append(str(int(round(r.effort_predicted))))
    if not vals and not bucket_label:
        return "", None
    label = (bucket_label + " — " if bucket_label else "") + " · ".join(vals)
    if chosen.endswith(OUT_OF_SCOPE_SUFFIX):
        label = "~" + label
    return label, color


def _summarize_effort(
    runs: list[PipelineStepRun],
    difficulty_rows: list = (),
) -> tuple[str, QtGui.QColor | None]:
    """(display text, background color) for the Effort cell. Empty when none."""
    return _difficulty_display(difficulty_rows)


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
        # Bulk-fetch PipelineStepRun and SeriesDifficulty rows to avoid N+1.
        from sqlalchemy import select
        ids = [r.id for r in rows]
        runs_by_series: dict[int, list[PipelineStepRun]] = {i: [] for i in ids}
        difficulty_by_series: dict[int, list] = {i: [] for i in ids}
        if ids:
            for run in session.execute(
                select(PipelineStepRun).where(PipelineStepRun.series_id.in_(ids))
            ).scalars():
                runs_by_series.setdefault(run.series_id, []).append(run)
            for diff in session.execute(
                select(SeriesDifficulty).where(SeriesDifficulty.series_id.in_(ids))
            ).scalars():
                difficulty_by_series.setdefault(diff.series_id, []).append(diff)
        self._rows = []
        for r in rows:
            runs = runs_by_series.get(r.id, [])
            diff = difficulty_by_series.get(r.id, [])
            effort_text, effort_color = _summarize_effort(runs, diff)
            self._rows.append({
                **self._row_to_dict(r),
                "pipeline_summary": _summarize_runs(runs),
                "effort": effort_text,
                "effort_color": effort_color,
            })
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
        if role == QtCore.Qt.BackgroundRole and COLUMNS[index.column()][0] == "effort":
            return self._rows[index.row()].get("effort_color")
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
