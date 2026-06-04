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
import sys
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
    per_series: bool = False  # True = tool takes one series name, not a list file


# Ordered exactly as in extract.sh. The default UI selection mirrors this
# order; users can deselect any subset.
#
# per_series=True: the legacy tool takes a single series name as args[0] and
# has no list-file batch mode. We must invoke it once per series.
#   - RedExtractor1: makeConfigPathFromName() processes args[0] directly as a
#     series name; if args[0] is a path to an existing file it strips the
#     directory and extension to get the stem, then looks up that stem in
#     embryoDB — so a /tmp/embryodb-extract-*.list file produces the wrong stem.
#   - Partial.pl / UpdatePermissions.pl: $ARGV[0] is used verbatim as the
#     series name to construct {DBloc}/{series}.xml; no list-file branch exists.
# per_series=False: the tool checks if args[0] / $ARGV[0] refers to an
# existing file and, if so, reads it as a newline-delimited series list.
#
# Step kinds:
#   - "java"        → java -mx500m -cp acebatch3.jar <Target> <arg>
#   - "perl"        → perl <target.pl> <arg>
#   - "python_cli"  → <sys.executable> -m embryodb.cli <target> <arg>
#                     (used by Partial, which has been ported off Partial.pl
#                     so that partial_editing_code is read from the DB, not
#                     from the stale legacy XML).
EXTRACT_STEPS: tuple[ExtractStep, ...] = (
    ExtractStep("red_extractor", "RedExtractor1",
        "Quantitate red-channel signal per nucleus.",
        "java", "RedExtractor1", per_series=True),
    ExtractStep("red_excel1", "RedExcel1",
        "Format RedExtractor output into per-series S<name>.csv.",
        "java", "RedExcel1"),
    ExtractStep("red_excel2", "RedExcel2",
        "Combine positions + expression into CD<name>.csv.",
        "java", "RedExcel2"),
    ExtractStep("partial", "Partial",
        "Trim per-cell tables using partial_editing_code from the new DB "
        "(replaces legacy Partial.pl which read stale XML).",
        "python_cli", "partial", per_series=True),
    ExtractStep("measure", "Measure1",
        "Nuclear morphometry; writes <series>AuxInfo.csv.",
        "java", "Measure1"),
    ExtractStep("align", "Align1",
        "Sulston-lineage alignment.",
        "java", "Align1"),
    ExtractStep("process_time", "ProcessTime",
        "Per-timepoint timestamps via legacy ProcessTime.pl (SP5 era + older "
        "Stellaris exports). v2-imported acquisitions already have these in "
        "the DB and on disk from the import-time compute_timestamps step; "
        "only run this for series that pre-date the v2 pipeline.",
        "perl", "ProcessTime.pl"),
    ExtractStep("update_perms", "UpdatePermissions",
        "chgrp / chmod across dats/ so other lab members can read outputs.",
        "perl", "UpdatePermissions.pl", per_series=True),
)

EXTRACT_STEPS_BY_KEY: dict[str, ExtractStep] = {s.key: s for s in EXTRACT_STEPS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Trailer line the launcher appends to every job log once the command group
# exits, so a job's exit status is recoverable from the log alone after the
# GUI (and its in-memory Popen handle) are gone. Read back by embryodb.jobs.
_EXIT_TRAILER = "__EMBRYODB_EXIT"


def _embryodb_runs_dir() -> Path:
    """Where to put the temp series-list file + log file for one invocation.

    Defaults to ``~/.embryodb/runs`` — a stable, per-user location that
    survives reboots (unlike ``/tmp``) so a restarted GUI can rediscover
    ongoing/finished jobs (see :mod:`embryodb.jobs`). Override with
    ``EMBRYODB_RUNS_DIR`` to point runs somewhere shared.
    """
    override = os.environ.get("EMBRYODB_RUNS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".embryodb" / "runs"


def _pidfile_for(log_path: Path) -> Path:
    """Sidecar that records the detached child's pid next to its log.

    The pid encoded in the log *filename* is the launcher's (the GUI/CLI
    process that called the run_* function), which is useless for liveness
    once that process exits. The real, checkable pid is the detached bash
    process; we stash it here so :mod:`embryodb.jobs` can tell running from
    finished after a GUI restart.
    """
    return Path(str(log_path) + ".pid")


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
    q = shell_quote(str(log_path))
    # `(...)` groups the whole `&&` chain so its combined stdout/stderr land
    # in the log; the trailing `echo` runs unconditionally and records the
    # group's exit status ($? is unaffected by the redirect) so finished
    # jobs report success/failure even after the GUI is gone.
    full = f"({shell_command}) >> {q} 2>&1; echo \"{_EXIT_TRAILER}=$?\" >> {q}"
    proc = subprocess.Popen(
        ["bash", "-c", full],
        cwd=str(settings.tools3_dir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        _pidfile_for(log_path).write_text(str(proc.pid), encoding="utf-8")
    except OSError:
        pass  # liveness falls back to the exit trailer / log mtime
    return proc


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


def _step_invocation(step: ExtractStep, arg: str, base_dir: Path) -> str:
    """Render one shell invocation for `step` against `arg`.

    `arg` is either a series name (per_series steps) or a path to the
    series-list temp file (batch-mode steps).
    """
    if step.kind == "java":
        return (
            f"nice java -mx500m "
            f"-cp {shell_quote(str(base_dir / 'acebatch3.jar'))} "
            f"{step.target} {shell_quote(arg)}"
        )
    if step.kind == "perl":
        return (
            f"nice perl {shell_quote(str(base_dir / step.target))} "
            f"{shell_quote(arg)}"
        )
    if step.kind == "python_cli":
        # Use sys.executable so the detached subprocess uses the same Python
        # / venv as the GUI that spawned it — PATH lookups for `embryodb`
        # aren't reliable in detached bash sessions.
        return (
            f"nice {shell_quote(sys.executable)} -m embryodb.cli "
            f"{step.target} {shell_quote(arg)}"
        )
    raise AssertionError(f"unknown step kind {step.kind!r}")


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
        # per_series steps have no list-file batch mode: invoke once per name.
        # All other steps accept a list file directly.
        if step.per_series:
            invocations: list[str] = []
            for name in series_names:
                invocations.append(_step_invocation(step, name, base_dir))
            parts.append(
                f"echo '== {step.label} ==' && " + " && ".join(invocations)
            )
        else:
            parts.append(
                f"echo '== {step.label} ==' && "
                + _step_invocation(step, str(list_file), base_dir)
            )
    # `&&` so a failed step halts the rest. Each step echoes a banner so
    # the log is human-skimmable.
    shell = " && ".join(parts)
    proc = _spawn_detached(shell, log_path)
    return LaunchResult(proc=proc, log_path=log_path, series_list=list_file)


def run_print_trees(
    series_names: list[str],
    *,
    min_expr: int | None = None,
    max_expr: int | None = None,
    color_scheme: str = "rainbow",
    linewidth: int = 3,
    tools3_dir: Path | None = None,
) -> LaunchResult:
    """Spawn `acexpress_CL2.jar Tree1` detached against the given series.

    Tree1 args (positional, per accessory inventory):
        <series_list_file> [minExpr] [maxExpr] [colorScheme|rootCell] [linewidth]

    minExpr/maxExpr/linewidth are parsed by Tree1 via `Integer.parseInt` — they
    must be ints (passing "0.0" fails with NumberFormatException).

    Output PNGs land in /gpfs/fs0/l/murr/trees/ (hardcoded inside Tree1).
    """
    if not series_names:
        raise ValueError("run_print_trees: series_names is empty")
    base_dir = Path(tools3_dir or settings.tools3_dir)
    list_file = _write_series_list(series_names, tag="trees")
    log_path = list_file.with_suffix(".log")

    args = [shell_quote(str(list_file))]
    if min_expr is not None:
        args.append(str(int(min_expr)))
        if max_expr is not None:
            args.append(str(int(max_expr)))
            args.append(shell_quote(color_scheme))
            args.append(str(int(linewidth)))

    shell = (
        f"echo '== PrintTrees ({len(series_names)} series) ==' && "
        f"nice java -Xmx1000m -cp {shell_quote(str(base_dir / 'acexpress_CL2.jar'))} "
        f"Tree1 {' '.join(args)}"
    )
    proc = _spawn_detached(shell, log_path)
    return LaunchResult(proc=proc, log_path=log_path, series_list=list_file)


def run_lif_import(
    lif_path: Path,
    series: str,
    protocol: str,
    *,
    user: str | None = None,
    person: str = "",
    strain: str = "",
    perturbation: str = "",
    reporter: str = "",
    comments: str = "",
    positions: list[str] | None = None,
    channel_roles: dict[int, str] | None = None,
    bit_depth_policy: str = "downcast",
    no_compress: bool = False,
    overwrite: bool = False,
    image_loc_root: Path | None = None,
    alias_root: Path | None = None,
    run_through: str | None = None,
) -> LaunchResult:
    """Spawn ``embryodb pipeline import-lif`` detached.

    The TIF extraction is the multi-hour part; detaching it (same mechanism
    as ``run_extract``) lets the GUI fire-and-forget so closing the dialog
    or the whole app doesn't kill the import. The caller already showed and
    confirmed the channel mapping, so ``--yes`` skips the CLI's own prompt.

    Progress is observable by polling the returned ``log_path`` for the
    ``<series>: <done>/<total> planes`` lines the CLI emits.
    """
    cmd = [
        shell_quote(sys.executable), "-m", "embryodb.cli",
        "pipeline", "import-lif", shell_quote(str(lif_path)),
        "--series", shell_quote(series),
        "--protocol", shell_quote(protocol),
        "--yes",
        "--bit-depth-policy", shell_quote(bit_depth_policy),
    ]
    if user:
        cmd += ["--user", shell_quote(user)]
    if person:
        cmd += ["--person", shell_quote(person)]
    if strain:
        cmd += ["--strain", shell_quote(strain)]
    if perturbation:
        cmd += ["--perturbation", shell_quote(perturbation)]
    if reporter:
        cmd += ["--reporter", shell_quote(reporter)]
    if comments:
        cmd += ["--comments", shell_quote(comments)]
    for pos in positions or []:
        cmd += ["--position", shell_quote(pos)]
    for raw_ch, role in (channel_roles or {}).items():
        cmd += ["--channel-role", shell_quote(f"{raw_ch}={role}")]
    if no_compress:
        cmd += ["--no-compress"]
    if overwrite:
        cmd += ["--overwrite"]
    if image_loc_root is not None:
        cmd += ["--image-loc-root", shell_quote(str(image_loc_root))]
    if alias_root is not None:
        cmd += ["--alias-root", shell_quote(str(alias_root))]
    if run_through:
        cmd += ["--run-through", shell_quote(run_through)]

    runs = _embryodb_runs_dir()
    runs.mkdir(parents=True, exist_ok=True)
    log_path = runs / f"embryodb-lif-import-{int(time.time())}-{os.getpid()}.log"
    proc = _spawn_detached("nice " + " ".join(cmd), log_path)
    return LaunchResult(proc=proc, log_path=log_path, series_list=log_path)


__all__ = [
    "EXTRACT_STEPS",
    "EXTRACT_STEPS_BY_KEY",
    "ExtractStep",
    "LaunchResult",
    "run_extract",
    "run_lif_import",
    "run_print_trees",
    "shell_quote",
]
