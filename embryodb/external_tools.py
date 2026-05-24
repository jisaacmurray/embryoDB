"""Detached launchers for legacy analysis tools.

Wrappers around the eight `extract.sh` steps and `PrintTrees.pl` (Tree1).
Each function spawns a fully-detached subprocess (same pattern as
`external.launch_acetree`) so the GUI can fire-and-forget without blocking.

These are *analysis* operations — distinct from the v2 *import* pipeline
that lives in `embryodb.pipeline.subprocess_steps`. They don't write
`PipelineStepRun` rows; outputs land where the legacy tools always put
them (under each series' `dats/` directory for extract; under
`/gpfs/fs0/l/murr/trees/` for Tree1). The Popen handle is returned for
callers that want a PID; the typical use is just to spawn and forget.

The same subprocess pattern works on either a single series or a whole
dataset — the unit is "a list of series names", and the caller supplies
either kind.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import settings


# ---------------------------------------------------------------------------
# Extract steps — the eight invocations from /gpfs/fs0/l/murr/tools3/extract.sh
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractStep:
    key: str           # short id used in the GUI / API
    label: str         # human-readable
    description: str   # tooltip
    kind: str          # "java" or "perl"
    target: str        # JAR-relative class name (java) or script filename (perl)


# Ordered exactly as in extract.sh. The default UI selection mirrors this
# order; users can deselect any subset.
EXTRACT_STEPS: tuple[ExtractStep, ...] = (
    ExtractStep("red_extractor", "RedExtractor1",
        "Quantitate red-channel signal per nucleus.",
        "java", "RedExtractor1"),
    ExtractStep("red_excel1", "RedExcel1",
        "Format RedExtractor output into per-series S<name>.csv.",
        "java", "RedExcel1"),
    ExtractStep("red_excel2", "RedExcel2",
        "Combine positions + expression into CD<name>.csv.",
        "java", "RedExcel2"),
    ExtractStep("partial", "Partial",
        "Trim per-cell tables to the curated extent recorded in checkedby.",
        "perl", "Partial.pl"),
    ExtractStep("measure", "Measure1",
        "Nuclear morphometry; writes <series>AuxInfo.csv.",
        "java", "Measure1"),
    ExtractStep("align", "Align1",
        "Sulston-lineage alignment.",
        "java", "Align1"),
    ExtractStep("process_time", "ProcessTime",
        "Per-timepoint absolute timestamps; writes TIME<series>.csv.",
        "perl", "ProcessTime.pl"),
    ExtractStep("update_perms", "UpdatePermissions",
        "chgrp / chmod across dats/ so other lab members can read outputs.",
        "perl", "UpdatePermissions.pl"),
)

EXTRACT_STEPS_BY_KEY: dict[str, ExtractStep] = {s.key: s for s in EXTRACT_STEPS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _embryodb_runs_dir() -> Path:
    """Where to put the temp series-list file + log file for one invocation.

    /tmp is fine — these are ephemeral. Callers can override via env if
    they want runs to persist somewhere shared.
    """
    return Path(os.environ.get("EMBRYODB_RUNS_DIR", tempfile.gettempdir()))


def _write_series_list(series_names: list[str], tag: str) -> Path:
    """Write a one-name-per-line series-list file. Returns its path."""
    runs = _embryodb_runs_dir()
    runs.mkdir(parents=True, exist_ok=True)
    list_file = runs / f"embryodb-{tag}-{int(time.time())}-{os.getpid()}.list"
    list_file.write_text("\n".join(series_names) + "\n", encoding="utf-8")
    return list_file


def _spawn_detached(shell_command: str, log_path: Path) -> subprocess.Popen:
    """Spawn `shell_command` via bash with stdout/stderr redirected to log_path.

    Detached (`start_new_session=True`) so SSH logout / GUI exit don't kill it.
    Uses bash -c for the convenience of `&&` chaining, file redirection,
    and shell quoting of paths with spaces.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)
    full = f"({shell_command}) >> {shell_quote(str(log_path))} 2>&1"
    return subprocess.Popen(
        ["bash", "-c", full],
        cwd=str(settings.tools3_dir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def shell_quote(s: str) -> str:
    """Minimal POSIX-safe quoting for bash -c strings (single-quote escape)."""
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class LaunchResult:
    """Returned by every launch function so callers can show status."""
    proc: subprocess.Popen
    log_path: Path
    series_list: Path


def run_extract(
    series_names: list[str],
    step_keys: list[str],
    *,
    tools3_dir: Path | None = None,
) -> LaunchResult:
    """Spawn one detached job that runs the chosen extract steps on the given
    series list, in canonical extract.sh order.

    Args:
        series_names: one or more series names. The caller resolved them
            from either a selection in the browser or a Dataset.
        step_keys: subset of EXTRACT_STEPS_BY_KEY keys. Empty list raises.
        tools3_dir: override for tests; defaults to settings.tools3_dir.

    Returns LaunchResult with the Popen handle, the log path the caller
    should surface in the GUI, and the series-list temp file path.
    """
    if not series_names:
        raise ValueError("run_extract: series_names is empty")
    if not step_keys:
        raise ValueError("run_extract: step_keys is empty")
    unknown = [k for k in step_keys if k not in EXTRACT_STEPS_BY_KEY]
    if unknown:
        raise ValueError(f"run_extract: unknown step keys: {unknown}")

    base_dir = Path(tools3_dir or settings.tools3_dir)
    list_file = _write_series_list(series_names, tag="extract")
    log_path = list_file.with_suffix(".log")

    # Run steps in canonical order (subset of EXTRACT_STEPS).
    chosen = [s for s in EXTRACT_STEPS if s.key in set(step_keys)]
    parts: list[str] = []
    for step in chosen:
        if step.kind == "java":
            parts.append(
                f"echo '== {step.label} ==' && "
                f"nice java -mx500m -cp {shell_quote(str(base_dir / 'acebatch3.jar'))} "
                f"{step.target} {shell_quote(str(list_file))}"
            )
        elif step.kind == "perl":
            parts.append(
                f"echo '== {step.label} ==' && "
                f"nice perl {shell_quote(str(base_dir / step.target))} "
                f"{shell_quote(str(list_file))}"
            )
        else:
            raise AssertionError(f"unknown step kind {step.kind!r}")
    # `&&` so a failed step halts the rest. Each step echoes a banner so
    # the log is human-skimmable.
    shell = " && ".join(parts)
    proc = _spawn_detached(shell, log_path)
    return LaunchResult(proc=proc, log_path=log_path, series_list=list_file)


def run_print_trees(
    series_names: list[str],
    *,
    min_expr: float | None = None,
    max_expr: float | None = None,
    color_scheme: str = "rainbow",
    linewidth: int = 3,
    tools3_dir: Path | None = None,
) -> LaunchResult:
    """Spawn `acexpress_CL2.jar Tree1` detached against the given series.

    Tree1 args (positional, per accessory inventory):
        <series_list_file> [minExpr] [maxExpr] [colorScheme|rootCell] [linewidth]

    Output PNGs land in /gpfs/fs0/l/murr/trees/ (hardcoded inside Tree1).
    """
    if not series_names:
        raise ValueError("run_print_trees: series_names is empty")
    base_dir = Path(tools3_dir or settings.tools3_dir)
    list_file = _write_series_list(series_names, tag="trees")
    log_path = list_file.with_suffix(".log")

    args = [shell_quote(str(list_file))]
    if min_expr is not None:
        args.append(str(min_expr))
        if max_expr is not None:
            args.append(str(max_expr))
            args.append(shell_quote(color_scheme))
            args.append(str(linewidth))

    shell = (
        f"echo '== PrintTrees ({len(series_names)} series) ==' && "
        f"nice java -Xmx1000m -cp {shell_quote(str(base_dir / 'acexpress_CL2.jar'))} "
        f"Tree1 {' '.join(args)}"
    )
    proc = _spawn_detached(shell, log_path)
    return LaunchResult(proc=proc, log_path=log_path, series_list=list_file)


__all__ = [
    "EXTRACT_STEPS",
    "EXTRACT_STEPS_BY_KEY",
    "ExtractStep",
    "LaunchResult",
    "run_extract",
    "run_print_trees",
    "shell_quote",
]
