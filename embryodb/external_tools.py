"""Detached launchers for legacy analysis tools.

Wrappers around the eight `extract.sh` steps and `PrintTrees.pl` (Tree1).
Each function spawns a fully-detached subprocess (same pattern as
`external.launch_acetree`) so the GUI can fire-and-forget without blocking.

These are *analysis* operations — distinct from the v2 *import* pipeline
that lives in `embryodb.pipeline.subprocess_steps`. Outputs land where the
legacy tools always put them (under each series' `dats/` directory for
extract; under `/gpfs/fs0/l/murr/trees/` for Tree1). The Popen handle is
returned for callers that want a PID; the typical use is just to spawn and
forget.

The **extract** chain is the exception: it delegates to
`embryodb.pipeline.extract_run`, which runs each series independently (a
failure on one embryo no longer halts the rest) and records a
`PipelineStepRun` row per step so the GUI Pipeline field shows extract
progress/failures. `print_trees` / `getacd` remain opaque batch shells.

The same subprocess pattern works on either a single series or a whole
dataset — the unit is "a list of series names", and the caller supplies
either kind.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import settings
from .external import LaunchError


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
    ExtractStep("getacd", "GetACD",
        "Map trajectories onto the Richards 2013 reference embryo "
        "(writes ACD<series>.csv via get_acd.R). Requires AuxInfo.csv from Measure.",
        "python_cli", "extract-getacd", per_series=True),
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
    """Returned by every launch function so callers can show status.

    Two shapes:
      * **local** — `proc` is the detached subprocess, `log_path` its live log,
        `series_list` the temp name-list file. `job_id` is None.
      * **queued** (remote mode) — `proc` and `series_list` are None; `job_id`
        is the enqueued :class:`~embryodb.models.CommandJob` id and `log_path`
        is where the penticton worker will write once it claims the job.
    """
    proc: subprocess.Popen | None
    log_path: Path
    series_list: Path | None
    job_id: int | None = None


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


# ---------------------------------------------------------------------------
# Pure command builders — render the shell string only (no I/O, no spawn).
#
# Split out from the run_* launchers so the SAME shell is produced whether a
# job runs locally (detached, here) or is claimed and run by a penticton worker
# from a queued CommandJob. `list_file` is the path the batch steps read names
# from; the caller (local launcher or worker) is responsible for writing it.
# ---------------------------------------------------------------------------


def build_extract_command(
    series_names: list[str],
    step_keys: list[str],
    list_file: Path | str,
    base_dir: Path,
) -> str:
    """Render the extract shell: delegate to the per-series tracked runner.

    Rather than one global `&&` chain (whose first failure halted every step of
    every series), this hands the list file + step keys to
    ``embryodb.cli run-extract-batch``, which runs each series independently and
    records a ``PipelineStepRun`` row per step (see
    :mod:`embryodb.pipeline.extract_run`). A crash on one embryo no longer
    stops the others, and failures show up in the GUI Pipeline field.

    Uses `sys.executable` so the detached subprocess (or the worker) runs the
    same interpreter/venv that rendered the command.
    """
    chosen = [s.key for s in EXTRACT_STEPS if s.key in set(step_keys)]
    return (
        f"echo '== extract: {len(series_names)} series, "
        f"{len(chosen)} step(s) ==' && "
        f"nice {shell_quote(sys.executable)} -m embryodb.cli run-extract-batch "
        f"--list {shell_quote(str(list_file))} "
        f"--steps {shell_quote(','.join(chosen))}"
    )


def build_livetools_trees_command(
    series_names: list[str],
    list_file: Path | str,
    *,
    min_expr: int | None = None,
    max_expr: int | None = None,
    color_scheme: str = "rainbow",
    linewidth: int | None = None,
    cd_prefix: str = "CD",
    output_dir: Path | str | None = None,
) -> str:
    """Render the LIVEtools (R/ggtree) tree shell.

    Delegates to `embryodb render-trees-batch`, which resolves each series'
    CSV and PNG path from the DB before handing R a manifest — R never
    discovers work on the filesystem.
    """
    args = [
        f"--list {shell_quote(str(list_file))}",
        f"--cd-prefix {shell_quote(cd_prefix)}",
        f"--color-scheme {shell_quote(color_scheme)}",
    ]
    # Left unset, the renderer derives a stroke that keeps a gap between
    # adjacent terminal branches; an explicit value overrides that.
    if linewidth is not None:
        args.append(f"--linewidth {int(linewidth)}")
    if min_expr is not None:
        args.append(f"--min-expr {int(min_expr)}")
    if max_expr is not None:
        args.append(f"--max-expr {int(max_expr)}")
    if output_dir is not None:
        args.append(f"--output-dir {shell_quote(str(output_dir))}")
    return (
        f"echo '== PrintTrees/LIVEtools ({len(series_names)} series, {cd_prefix}) ==' && "
        f"nice {shell_quote(sys.executable)} -m embryodb.cli render-trees-batch "
        + " ".join(args)
    )


def build_print_trees_command(
    series_names: list[str],
    list_file: Path | str,
    *,
    min_expr: int | None = None,
    max_expr: int | None = None,
    color_scheme: str = "rainbow",
    linewidth: int | None = None,
    cd_prefix: str = "CD",
    heap_mb: int = 4000,
    on_screen: bool = False,
    renderer: str | None = None,
    output_dir: Path | str | None = None,
    base_dir: Path,
) -> str:
    """Render the tree-drawing shell for the selected renderer.

    `renderer` picks between "livetools" (R/ggtree) and "java" (Tree1);
    None takes `settings.tree_renderer`. The Java-only options (`heap_mb`,
    `on_screen`) are ignored by the LIVEtools path, which has no JVM and no
    window.

    Everything below documents the Java path.

    When `cd_prefix` is not "CD" each series-list line is written as
    ``<series> - <cd_prefix>`` so Tree1's series-list parser uses the chosen
    file type (SCD or ACD) instead of the default CD<series>.csv.  The "-"
    sentinel tells the patched Tree1 that no editRules are active.

    `on_screen` shows Tree1's window on the caller's X display instead of
    rendering into a throwaway one — the quick-QC path, where looking at the
    tree is the point and the PNG is a side effect.
    """
    if (renderer or settings.tree_renderer) == "livetools":
        return build_livetools_trees_command(
            series_names,
            list_file,
            min_expr=min_expr,
            max_expr=max_expr,
            color_scheme=color_scheme,
            linewidth=linewidth,
            cd_prefix=cd_prefix,
            output_dir=output_dir,
        )
    list_path = Path(list_file)
    if cd_prefix != "CD":
        list_path.write_text(
            "\n".join(f"{n} - {cd_prefix}" for n in series_names) + "\n",
            encoding="utf-8",
        )
    args = [shell_quote(str(list_path))]
    if min_expr is not None:
        args.append(str(int(min_expr)))
        if max_expr is not None:
            args.append(str(int(max_expr)))
            args.append(shell_quote(color_scheme))
            args.append(str(int(linewidth if linewidth is not None else 3)))
    # Tree1 builds a JFrame even though it also writes PNGs, so it always needs a
    # display: headless mode throws HeadlessException. Batch runs get a throwaway
    # one from xvfb-run, which also stops a dead ssh X-forward from killing the
    # run; on_screen instead inherits the caller's $DISPLAY so the window is
    # visible. Tree1 holds every rendered tree in memory, so deeply-curated
    # embryos (~600+ cells) blow through the old 1000m heap partway down a list.
    java = (
        f"nice java -Xmx{int(heap_mb)}m "
        f"-cp {shell_quote(str(base_dir / 'acexpress_CL2.jar'))} "
        f"Tree1 {' '.join(args)}"
    )
    launcher = java if on_screen else f"xvfb-run -a {java}"
    return (
        f"echo '== PrintTrees ({len(series_names)} series, {cd_prefix}) ==' && "
        f"{launcher}"
    )


def build_getacd_command(
    series_names: list[str],
    list_file: Path | str,
    base_dir: Path,
) -> str:
    """Render the legacy `GetACD.pl` stopgap shell."""
    return (
        f"echo '== GetACD STOPGAP ({len(series_names)} series) ==' && "
        "mkdir -p CDs AuxInfos && "
        f"nice perl {shell_quote(str(base_dir / 'GetACD.pl'))} "
        f"{shell_quote(str(list_file))}"
    )


# Command-job kinds → the params each builder reads from CommandJob.params.
COMMAND_JOB_KINDS = ("extract", "print_trees", "getacd")


def build_command_for_kind(
    kind: str, params: dict, list_file: Path | str, base_dir: Path
) -> str:
    """Dispatch a queued CommandJob's (kind, params) to the matching builder.

    Used by the worker when it claims a job; the local launchers call the
    `build_*_command` builders directly.
    """
    names = list(params.get("series_names") or [])
    if kind == "extract":
        return build_extract_command(names, params["step_keys"], list_file, base_dir)
    if kind == "print_trees":
        return build_print_trees_command(
            names,
            list_file,
            min_expr=params.get("min_expr"),
            max_expr=params.get("max_expr"),
            color_scheme=params.get("color_scheme", "rainbow"),
            linewidth=params.get("linewidth"),
            cd_prefix=params.get("cd_prefix", "CD"),
            heap_mb=params.get("heap_mb", 4000),
            renderer=params.get("renderer"),
            output_dir=params.get("output_dir"),
            base_dir=base_dir,
        )
    if kind == "getacd":
        return build_getacd_command(names, list_file, base_dir)
    raise ValueError(f"unknown command-job kind {kind!r}")


def command_job_log_path(job_id: int) -> Path:
    """Deterministic shared log path for a queued CommandJob (worker writes it)."""
    return Path(settings.command_log_dir) / f"command-{job_id}.log"


def enqueue_command_job(kind: str, params: dict) -> LaunchResult:
    """Insert a PENDING CommandJob for a penticton worker to claim and run.

    Used by the run_* launchers in remote-client mode instead of spawning the
    tool locally. Returns a LaunchResult with `proc=None`, `job_id` set, and
    `log_path` pointing at where the worker will write — so the GUI/CLI can
    surface "queued as job #N" and the Background-jobs panel can follow it.
    """
    from .database import session_scope
    from .models import CommandJob

    submitter = f"{settings.user}@{socket.gethostname()}"
    with session_scope() as s:
        job = CommandJob(kind=kind, params=params, submitted_by=submitter)
        s.add(job)
        s.flush()  # assigns job.id
        job.log_path = str(command_job_log_path(job.id))
        job_id = job.id
        log_path = Path(job.log_path)
    return LaunchResult(proc=None, log_path=log_path, series_list=None, job_id=job_id)


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

    # Remote client: don't run the Java/Perl tools locally over sshfs — queue a
    # CommandJob for the penticton-resident worker to claim and execute.
    if settings.remote:
        return enqueue_command_job(
            "extract", {"series_names": list(series_names), "step_keys": list(step_keys)}
        )

    base_dir = Path(tools3_dir or settings.tools3_dir)
    list_file = _write_series_list(series_names, tag="extract")
    log_path = list_file.with_suffix(".log")
    shell = build_extract_command(series_names, step_keys, list_file, base_dir)
    proc = _spawn_detached(shell, log_path)
    return LaunchResult(proc=proc, log_path=log_path, series_list=list_file)


def run_print_trees(
    series_names: list[str],
    *,
    min_expr: int | None = None,
    max_expr: int | None = None,
    color_scheme: str = "rainbow",
    linewidth: int | None = None,
    cd_prefix: str = "CD",
    heap_mb: int = 4000,
    on_screen: bool = False,
    renderer: str | None = None,
    output_dir: Path | str | None = None,
    tools3_dir: Path | None = None,
) -> LaunchResult:
    """Spawn a detached tree render against the given series.

    `renderer` selects "livetools" (R/ggtree, the default) or "java" (Tree1);
    None takes `settings.tree_renderer`. `heap_mb` and `on_screen` apply only
    to the Java path.

    Everything below documents the Java path.

    Tree1 args (positional, per accessory inventory):
        <series_list_file> [minExpr] [maxExpr] [colorScheme|rootCell] [linewidth]

    minExpr/maxExpr/linewidth are parsed by Tree1 via `Integer.parseInt` — they
    must be ints (passing "0.0" fails with NumberFormatException).

    `cd_prefix` selects which CSV type Tree1 reads for expression data: "CD"
    (default), "SCD" (Sulston-aligned), or "ACD" (reference-embryo aligned).
    Requires the patched acexpress_CL2.jar that handles the three-token series
    list format (``<series> - <prefix>``).

    Output PNGs land in /gpfs/fs0/l/murr/trees/ (hardcoded inside Tree1).

    `on_screen` puts Tree1's window on the caller's display for quick visual QC.
    PNGs are still written either way — it only controls whether you also see
    the tree. Not available in remote mode, where the job runs on the worker.
    """
    if not series_names:
        raise ValueError("run_print_trees: series_names is empty")

    chosen = renderer or settings.tree_renderer
    if chosen not in ("livetools", "java"):
        raise LaunchError(f"unknown tree renderer {chosen!r}; expected 'livetools' or 'java'")

    if on_screen and chosen == "livetools":
        raise LaunchError(
            "print-trees --on-screen is a Tree1 feature (it shows the JFrame); "
            "the LIVEtools renderer only writes PNGs. Add --renderer java, or "
            "drop --on-screen and open the PNG."
        )

    if output_dir is not None and chosen == "java":
        raise LaunchError(
            "print-trees --output-dir is a LIVEtools option; Tree1 hardcodes its "
            f"output at {settings.trees_dir}. Drop --renderer java to choose a "
            "destination."
        )

    if on_screen and not os.environ.get("DISPLAY"):
        raise LaunchError(
            "print-trees --on-screen needs an X display, but $DISPLAY is unset. "
            "Use ssh -X / -Y, or drop --on-screen to render PNGs headlessly."
        )

    if settings.remote:
        if on_screen:
            raise LaunchError(
                "print-trees --on-screen is not available in remote mode: the "
                "job runs on the penticton worker, so its window would open "
                "there. Drop --on-screen and view the PNGs instead."
            )
        return enqueue_command_job(
            "print_trees",
            {
                "series_names": list(series_names),
                "min_expr": min_expr,
                "max_expr": max_expr,
                "color_scheme": color_scheme,
                "linewidth": linewidth,
                "cd_prefix": cd_prefix,
                "heap_mb": heap_mb,
                "renderer": chosen,
                "output_dir": str(output_dir) if output_dir is not None else None,
            },
        )

    base_dir = Path(tools3_dir or settings.tools3_dir)
    list_file = _write_series_list(series_names, tag="trees")
    log_path = list_file.with_suffix(".log")
    shell = build_print_trees_command(
        series_names,
        list_file,
        min_expr=min_expr,
        max_expr=max_expr,
        color_scheme=color_scheme,
        linewidth=linewidth,
        cd_prefix=cd_prefix,
        heap_mb=heap_mb,
        on_screen=on_screen,
        renderer=chosen,
        output_dir=output_dir,
        base_dir=base_dir,
    )
    proc = _spawn_detached(shell, log_path)
    return LaunchResult(proc=proc, log_path=log_path, series_list=list_file)


def run_getacd(
    series_names: list[str],
    *,
    tools3_dir: Path | None = None,
) -> LaunchResult:
    """**TEMPORARY STOPGAP** — wrap the legacy Perl ``GetACD.pl`` to generate
    ``ACD<series>.csv`` files (aligned/derotated cell coordinates in microns)
    before a phenotyping freeze.

    ``GetACD.pl`` takes a single series-list file, looks each series up via the
    legacy Perl ``MakeDB`` (same embryoDB XML corpus as the Python DB), reads
    ``CD<series>.csv`` + ``<series>AuxInfo.csv`` from each series' ``dats/``,
    and writes ``ACD<series>.csv`` **in place** into that same ``dats/`` dir —
    exactly where the freeze then picks it up. It also depends on
    ``SupplementalTable2_DivisionTimes.txt`` and ``MakeDB.pm`` in the working
    directory; both live in ``tools3_dir`` (the cwd ``_spawn_detached`` uses),
    so we run from there. The ``CDs/``/``AuxInfos/`` scratch dirs the script
    copies into are pre-created so its incidental ``cp`` calls don't error.

    Granularity note: the script runs on a **dataset/list**, not a single
    embryo — there is no per-embryo mode without hacking the script.

    HIGH PRIORITY follow-up: replace this with the in-progress R ``GetACD``
    rewrite and integrate ACD generation cleanly into the freeze/extract flow
    (see embryoDB CLAUDE.md, "LineagePhenotyping bridge").
    """
    if not series_names:
        raise ValueError("run_getacd: series_names is empty")

    if settings.remote:
        return enqueue_command_job("getacd", {"series_names": list(series_names)})

    base_dir = Path(tools3_dir or settings.tools3_dir)
    list_file = _write_series_list(series_names, tag="getacd")
    log_path = list_file.with_suffix(".log")
    shell = build_getacd_command(series_names, list_file, base_dir)
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
    append_series: list[str] | None = None,
    auto_append_extra: bool = True,
    bit_depth_policy: str = "downcast",
    no_compress: bool = False,
    overwrite: bool = False,
    image_loc_root: Path | None = None,
    alias_root: Path | None = None,
    run_through: str | None = None,
    delay_hours: float = 0.0,
    sn_engine: str | None = None,
    spawn_worker: bool = True,
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
    for name in append_series or []:
        cmd += ["--append-series", shell_quote(name)]
    if not auto_append_extra:
        cmd += ["--no-auto-append"]
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
    if delay_hours and delay_hours > 0:
        cmd += ["--delay-hours", shell_quote(f"{delay_hours:g}")]
    if sn_engine:
        cmd += ["--sn-engine", shell_quote(sn_engine)]
    if not spawn_worker:
        cmd += ["--no-worker"]

    runs = _embryodb_runs_dir()
    runs.mkdir(parents=True, exist_ok=True)
    log_path = runs / f"embryodb-lif-import-{int(time.time())}-{os.getpid()}.log"
    proc = _spawn_detached("nice " + " ".join(cmd), log_path)
    return LaunchResult(proc=proc, log_path=log_path, series_list=log_path)


__all__ = [
    "EXTRACT_STEPS",
    "EXTRACT_STEPS_BY_KEY",
    "COMMAND_JOB_KINDS",
    "ExtractStep",
    "LaunchResult",
    "build_command_for_kind",
    "build_extract_command",
    "build_getacd_command",
    "build_livetools_trees_command",
    "build_print_trees_command",
    "command_job_log_path",
    "enqueue_command_job",
    "run_extract",
    "run_getacd",
    "run_lif_import",
    "run_print_trees",
    "shell_quote",
]
