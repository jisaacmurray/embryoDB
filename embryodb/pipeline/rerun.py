"""Re-queue worker pipeline steps for one or more series.

Shared, GUI-free core used by both the ``Re-run pipeline…`` dialog
(``gui/rerun_dialog.py``) and the ``embryodb pipeline rerun`` CLI command,
so resetting steps + refreshing matlabParams behaves identically from
either entry point.

A "re-run" is two things:
  1. (optional) rewrite the series' ``matlabParams`` — refresh xyres/zres
     from the DB microscopy metadata (the single source of truth, matching
     ``step_write_matlab_params`` at import) and apply any tunable overrides.
     Only relevant when ``run_starrynite`` is among the steps.
  2. reset the chosen worker steps back to PENDING (clearing log/error/
     timestamps) so the worker re-claims them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..fsutil import safe_write_text
from ..models import PipelineStepRun, RunStatus
from ..parsers.matlab_params import load as load_params, render as render_params
from .worker import WORKER_STEPS

_STEP_ORDER = {s: i for i, s in enumerate(WORKER_STEPS)}

# stage_images re-extracts every TIFF from the raw acquisition — slow, and it
# rewrites the staged tree the later steps read. The overwhelmingly common
# rerun is re-tracing/quantitating already-staged images, so stage_images is
# NEVER selected by default; the user must opt in explicitly. (A FAILED
# stage_images row whose images are in fact on disk would otherwise drag the
# default selection back to a needless restage — and block run_starrynite.)
_NEVER_DEFAULT_STEPS = frozenset({"stage_images"})


def reset_step(session, series_id: int, step: str) -> None:
    """Set one worker step back to PENDING for a series (create row if absent)."""
    run = (
        session.query(PipelineStepRun)
        .filter_by(series_id=series_id, step=step)
        .one_or_none()
    )
    if run is None:
        run = PipelineStepRun(series_id=series_id, step=step)
        session.add(run)
    run.status = RunStatus.PENDING
    run.started_at = None
    run.completed_at = None
    run.heartbeat_at = None
    run.error_excerpt = None
    run.log_path = None
    run.output_summary = {}


def earliest_incomplete_step(runs_by_step: dict[str, RunStatus]) -> str | None:
    """Return the first WORKER_STEP that is not COMPLETE/SKIPPED, else None."""
    terminal = {RunStatus.COMPLETE, RunStatus.SKIPPED}
    for step in WORKER_STEPS:
        if runs_by_step.get(step) not in terminal:
            return step
    return None


def default_steps(series_runs: list[dict[str, RunStatus]]) -> list[str]:
    """Default step selection: the earliest incomplete step across all the
    given series, plus everything downstream of it. If every series has all
    worker steps complete, default to all worker steps (explicit redo).

    ``stage_images`` is always dropped from the default (see
    :data:`_NEVER_DEFAULT_STEPS`); a rerun that genuinely needs to restage
    must select it explicitly."""
    earliest_idx: int | None = None
    for runs in series_runs:
        step = earliest_incomplete_step(runs)
        if step is not None:
            idx = _STEP_ORDER[step]
            if earliest_idx is None or idx < earliest_idx:
                earliest_idx = idx
    if earliest_idx is None:
        candidates = list(WORKER_STEPS)
    else:
        candidates = [s for s in WORKER_STEPS if _STEP_ORDER[s] >= earliest_idx]
    return [s for s in candidates if s not in _NEVER_DEFAULT_STEPS]


def refresh_matlab_params(
    params_path: Path,
    *,
    voxel_xy_um: float | None = None,
    voxel_z_um: float | None = None,
    overrides: dict[str, str] | None = None,
) -> bool:
    """Rewrite matlabParams: refresh xyres/zres from DB metadata, apply
    overrides. Returns False (no-op) if the file doesn't exist."""
    pp = Path(params_path)
    if not pp.exists():
        return False
    params = load_params(pp)
    if voxel_xy_um is not None:
        params.set("xyres", str(voxel_xy_um))
    if voxel_z_um is not None:
        params.set("zres", str(voxel_z_um))
    for k, v in (overrides or {}).items():
        params.set(k, v)
    safe_write_text(pp, render_params(params))
    return True


@dataclass
class RequeueResult:
    steps: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def requeue_series(
    session,
    series_names: list[str],
    *,
    steps: list[str] | None = None,
    overrides: dict[str, str] | None = None,
    refresh_resolution: bool = True,
) -> RequeueResult:
    """Reset worker steps to PENDING for each named series.

    Args:
        steps: explicit worker steps to reset. None → :func:`default_steps`
            (earliest incomplete across the resolved series + downstream).
        overrides: matlabParams tunable overrides; applied only when
            ``run_starrynite`` is among the steps.
        refresh_resolution: when ``run_starrynite`` is reset, rewrite
            matlabParams to refresh xyres/zres from DB microscopy metadata
            (plus apply ``overrides``). Mirrors the GUI dialog.

    Does not spawn the worker — callers decide whether to.
    """
    from ..queries.series import get_by_name

    result = RequeueResult()
    overrides = overrides or {}

    rows = []
    for name in series_names:
        row = get_by_name(session, name)
        if row is None:
            result.missing.append(name)
        else:
            rows.append(row)

    if steps is None:
        steps = default_steps([{r.step: r.status for r in row.runs} for row in rows])
    result.steps = list(steps)

    sn_checked = "run_starrynite" in steps
    for row in rows:
        if sn_checked and refresh_resolution and row.image_loc:
            micro = row.microscopy
            try:
                refresh_matlab_params(
                    Path(row.image_loc) / "matlabParams",
                    voxel_xy_um=micro.voxel_xy_um if micro else None,
                    voxel_z_um=micro.voxel_z_um if micro else None,
                    overrides=overrides,
                )
            except Exception as exc:  # surface, don't abort the whole batch
                result.errors.append(f"{row.series_name}: {exc}")
        for step in steps:
            reset_step(session, row.id, step)
        result.matched.append(row.series_name)

    return result


__all__ = [
    "RequeueResult",
    "default_steps",
    "earliest_incomplete_step",
    "refresh_matlab_params",
    "requeue_series",
    "reset_step",
]
