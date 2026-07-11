"""Per-series execution of the legacy extract chain, with per-step status.

The old extract launcher rendered the whole chain (all steps × all series) as
one ``&&``-joined bash string and ran it as a single fire-and-forget job:

- **No isolation** — the first failure anywhere halted *everything* downstream,
  including unrelated embryos (a Partial crash on series #1 left the other 11
  with no Measure/Align/permissions).
- **No visibility** — nothing was written to ``PipelineStepRun``, so a failure
  was invisible in the GUI Pipeline field; the only trace was a logfile.

This module fixes both:

- :func:`run_extract_for_series` runs one series' chosen steps in canonical
  order, writing a ``PipelineStepRun`` row per step (RUNNING → COMPLETE/FAILED
  with an error excerpt), and halts *that series* on the first failure (so a
  failed RedExtract still skips its Measure).
- :func:`run_extract_batch` loops series and continues past a series whose
  chain fails — one bad embryo no longer stops the rest.

Three extract steps **dedup** onto the existing import-pipeline slots because
they are the same underlying operation: ``RedExtractor1`` == ``run_red_extract``,
``Measure1`` == ``run_measure``, ``ProcessTime`` == ``compute_timestamps``. The
remaining steps roll up into three display categories for the Pipeline field:
``output_excel`` (RedExcel1 + RedExcel2 + Partial), ``align`` (Align1, plus a
reserved ``getacd`` slot for the future R rewrite), and ``permissions``
(UpdatePermissions).
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .. import database
from ..config import settings
from ..external_tools import EXTRACT_STEPS, _step_invocation
from ..fsutil import safe_write_text
from ..models import PipelineStepRun, RunStatus
from ..queries import series as q_series


# Extract step key → PipelineStepRun.step name. The three dedup mappings share a
# row with the import pipeline (same operation, one status slot); the rest get
# their own step names, grouped into display categories below.
EXTRACT_STEP_PIPELINE_NAME: dict[str, str] = {
    "red_extractor": "run_red_extract",     # dedup: == RedExtractor1
    "red_excel1": "red_excel1",
    "red_excel2": "red_excel2",
    "partial": "partial",
    "measure": "run_measure",               # dedup: == Measure1
    "align": "align1",
    "getacd": "getacd",
    "process_time": "compute_timestamps",    # dedup: == ProcessTime
    "update_perms": "update_perms",
}

# Display roll-up categories: ``(category_name, [component step names])``. A
# category is COMPLETE only when every *present* component is COMPLETE, and
# FAILED if any component FAILED. ``getacd`` is a reserved component of
# ``align`` — the updated R GetACD isn't wired into the extract chain yet, so no
# row is written for it; the roll-up simply skips absent components.
EXTRACT_CATEGORIES: list[tuple[str, list[str]]] = [
    ("output_excel", ["red_excel1", "red_excel2", "partial"]),
    ("align", ["align1", "getacd"]),
    ("permissions", ["update_perms"]),
]

EXTRACT_CATEGORY_COMPONENTS: dict[str, list[str]] = {
    cat: comps for cat, comps in EXTRACT_CATEGORIES
}

# Compact summary order for the Pipeline cell: import steps followed by the
# analysis phase with the extract categories rolled up in execution order.
SUMMARY_STEP_ORDER: tuple[str, ...] = (
    "stage_images", "stage_metadata", "compute_timestamps",
    "write_acetree_config", "write_embryodb_xml", "create_alias_symlink",
    "write_matlab_params", "run_starrynite",
    "run_red_extract", "output_excel", "run_measure", "align", "permissions",
)

# Fine-grained order for the per-step Details popup (no roll-up).
DETAIL_STEP_ORDER: tuple[str, ...] = (
    "stage_images", "stage_metadata", "compute_timestamps",
    "write_acetree_config", "write_embryodb_xml", "create_alias_symlink",
    "write_matlab_params", "run_starrynite",
    "run_red_extract", "red_excel1", "red_excel2", "partial",
    "run_measure", "align1", "getacd", "update_perms",
)


def rollup_category(
    by_step: dict[str, RunStatus], components: list[str]
) -> RunStatus | None:
    """Combine component step statuses into one category status.

    Returns ``None`` when no component has a row yet (category not started, so
    the caller skips it). Worst-status wins: FAILED > RUNNING > PENDING; all
    present-and-COMPLETE → COMPLETE; otherwise SKIPPED.
    """
    present = [by_step[c] for c in components if c in by_step]
    if not present:
        return None
    if any(s == RunStatus.FAILED for s in present):
        return RunStatus.FAILED
    if any(s == RunStatus.RUNNING for s in present):
        return RunStatus.RUNNING
    if any(s == RunStatus.PENDING for s in present):
        return RunStatus.PENDING
    if all(s == RunStatus.COMPLETE for s in present):
        return RunStatus.COMPLETE
    return RunStatus.SKIPPED


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _upsert_run(session, series_id: int, step: str, **fields) -> None:
    """Get-or-create the (series_id, step) row and set the given fields."""
    row = (
        session.query(PipelineStepRun)
        .filter(
            PipelineStepRun.series_id == series_id,
            PipelineStepRun.step == step,
        )
        .one_or_none()
    )
    if row is None:
        row = PipelineStepRun(series_id=series_id, step=step)
        session.add(row)
    for k, v in fields.items():
        setattr(row, k, v)


def _run_step(invocation: str, base_dir: Path) -> tuple[int, str]:
    """Run one step's shell invocation from ``base_dir``; capture combined output.

    The output is echoed to stdout (so it lands in the surrounding job log, which
    the launcher/worker redirects there) and also returned so a failing step can
    stash a tail in ``error_excerpt``.
    """
    proc = subprocess.run(
        ["bash", "-c", invocation],
        cwd=str(base_dir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.stdout:
        print(proc.stdout, end="", flush=True)
    return proc.returncode, proc.stdout or ""


def _tail(text: str, n: int = 30) -> str:
    return "\n".join(text.splitlines()[-n:])


def run_extract_for_series(
    series_name: str,
    step_keys: list[str],
    *,
    base_dir: Path | None = None,
    claimed_by: str | None = None,
) -> int:
    """Run one series' extract steps in canonical order, recording status.

    Each step upserts its ``PipelineStepRun`` row (RUNNING → COMPLETE/FAILED).
    The chain halts on the first failing step (so downstream steps don't run on
    bad inputs), returning that step's non-zero code. Returns 0 on full success,
    or 2 if the series isn't in the DB.
    """
    base = Path(base_dir or settings.tools3_dir)
    steps = [s for s in EXTRACT_STEPS if s.key in set(step_keys)]

    with database.session_scope() as session:
        row = q_series.get_by_name(session, series_name)
        if row is None:
            print(f"extract: series {series_name!r} not found in DB", flush=True)
            return 2
        series_id = row.id

    for step in steps:
        pstep = EXTRACT_STEP_PIPELINE_NAME[step.key]
        # per_series tools take the name directly; batch-mode tools want a
        # newline-delimited list file — write a one-line one for this series.
        if step.per_series:
            arg = series_name
        else:
            lf = Path(settings.command_log_dir) / f"{series_name}.{pstep}.list"
            safe_write_text(lf, series_name + "\n")
            arg = str(lf)

        with database.session_scope() as session:
            _upsert_run(
                session, series_id, pstep,
                status=RunStatus.RUNNING, started_at=_now(), heartbeat_at=_now(),
                completed_at=None, error_excerpt=None, claimed_by=claimed_by,
            )

        print(f"== {step.label} ({series_name}) ==", flush=True)
        rc, output = _run_step(_step_invocation(step, arg, base), base)

        with database.session_scope() as session:
            if rc == 0:
                _upsert_run(
                    session, series_id, pstep,
                    status=RunStatus.COMPLETE, completed_at=_now(),
                    heartbeat_at=_now(),
                )
            else:
                _upsert_run(
                    session, series_id, pstep,
                    status=RunStatus.FAILED, completed_at=_now(),
                    heartbeat_at=_now(), error_excerpt=_tail(output)[:4000],
                )
        if rc != 0:
            print(
                f"extract: {series_name}: step {step.label} failed (rc={rc}); "
                f"halting this series",
                flush=True,
            )
            return rc
    return 0


def run_extract_batch(
    series_names: list[str],
    step_keys: list[str],
    *,
    base_dir: Path | None = None,
    claimed_by: str | None = None,
) -> int:
    """Run the extract chain for each series independently.

    A series whose chain fails does not stop the others — the whole point of the
    per-embryo split. Returns the worst per-series return code (0 iff every
    series completed all its steps).
    """
    base = Path(base_dir or settings.tools3_dir)
    worst = 0
    for name in series_names:
        try:
            rc = run_extract_for_series(
                name, step_keys, base_dir=base, claimed_by=claimed_by
            )
        except Exception as exc:  # never let one series abort the batch
            print(f"extract: {name}: unexpected error: {exc}", flush=True)
            rc = 1
        if rc != 0:
            worst = rc
    return worst
