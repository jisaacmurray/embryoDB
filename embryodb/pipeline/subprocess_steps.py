"""Subprocess wrappers for the three long-running pipeline steps.

These functions are called by the background worker (worker.py). Each one:
- Manages its own DB sessions (so heartbeats commit independently of the
  caller's transaction context).
- Writes a log file under <image_loc>/dats/.
- Updates PipelineStepRun.status, .completed_at, .log_path, and .error_excerpt.
- Updates PipelineStepRun.heartbeat_at every HEARTBEAT_INTERVAL seconds so the
  GUI can detect a stale worker.

None of these functions call _begin()/_complete()/_fail() from orchestrate.py —
the worker already set status=RUNNING before calling; these functions transition
to COMPLETE, FAILED, or SKIPPED and commit.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..database import session_scope
from ..fsutil import chmod_if_possible, ensure_dir
from ..models import PipelineStepRun, RunStatus, Series
from .annotation_archive import archive_annotations

HEARTBEAT_INTERVAL = 30  # seconds between DB heartbeat writes during subprocess run

# Watchdog: detect a subprocess that has crashed but not exited. The classic
# case is the ancient MCR (StarryNite's MATLAB runtime) printing a heap-abort
# (`malloc_consolidate(): invalid chunk size`) and then wedging with its
# threads stuck — `proc.poll()` never returns, so the plain heartbeat loop
# would hang the whole serial queue forever. Two triggers:
#   1. a known crash signature appears in the log (fail in seconds), or
#   2. the process group burns ~0 CPU AND the log stops growing for a
#      conservative window (catches silent wedges with no tell-tale string).
# On either, the watchdog kills the whole process group and the step is
# marked FAILED with a descriptive excerpt.
WATCHDOG_IDLE_SECONDS = 600  # ~10 min of no CPU + no log growth => wedged

# Abort-specific strings — deliberately NOT generic ("error") so normal tool
# chatter doesn't false-positive.
_CRASH_SIGNATURES = (
    "malloc_consolidate",
    "*** glibc detected ***",
    "free(): invalid",
    "double free or corruption",
    "Segmentation fault",
    "terminate called",
    # MCR/C++ runtime aborts that deadlock instead of exiting: the process
    # prints one of these then wedges its threads in futex_wait, so poll()
    # never returns. Caught the JIM799 6-day hang (2026-06-18) that the
    # CPU/log-idle heuristic missed (periodic java helpers reset its timer).
    "pure virtual method called",
    "Assertion `",
    "what():  std::",
)


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


def _proc_group_cpu_ticks(pgid: int) -> int:
    """Sum utime+stime (clock ticks) across every live process in `pgid`.

    Reads /proc/<pid>/stat for all processes; robust to a `comm` field that
    contains spaces/parens by splitting after the final ')'.
    """
    total = 0
    try:
        entries = os.listdir("/proc")
    except OSError:
        return total
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as fh:
                data = fh.read().decode("latin-1")
            rparen = data.rfind(")")
            fields = data[rparen + 2:].split()
            # fields[0]=state(3) ... index N-3 maps to stat field N:
            # pgrp=field5->idx2, utime=field14->idx11, stime=field15->idx12
            if int(fields[2]) != pgid:
                continue
            total += int(fields[11]) + int(fields[12])
        except (OSError, ValueError, IndexError):
            continue
    return total


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM then SIGKILL the subprocess's whole process group.

    Relies on the process having been started with start_new_session=True so
    it leads its own group (perl -> sh -> commandLineDriver -> MCR threads all
    share the group and die together).
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, OSError):
            return
        for _ in range(20):  # up to ~10 s grace before escalating
            if proc.poll() is not None:
                return
            time.sleep(0.5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tail_log(path: Path, n: int = 30) -> str:
    """Return last `n` lines of a log file as a single string."""
    try:
        lines = path.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return "(log unreadable)"


def diagnose_starrynite_failure(log_path: Path) -> str | None:
    """Recognize known StarryNite failure mechanisms from the log tail.

    Returns a human-readable diagnostic to surface in the run's error_excerpt,
    or None if the cause isn't one we specifically recognize (in which case the
    raw log tail is shown instead).

    Two recognized mechanisms:
      * cell_ct_limit overflow in the C tracer — detection finished (per-timepoint
        nuclei written) and the tracing/lineaging phase began ("Start cell tracing"),
        but it segfaulted before "End time". The compiled MATLAB loader hardcodes
        cell_ct_limit=5000, which sizes the tracer's centroid scratch buffers; dense
        embryos at full resolution overflow it and corrupt the heap. matlab_SN_cluster.pl
        now raises the limit before lineaging.
      * MCR detection-teardown heap abort — a crash signature after "Elapsed time"
        but before tracing began, typically a corrupted/shared MCR cache (now
        isolated per run via MCR_CACHE_ROOT).
    """
    try:
        text = log_path.read_text(errors="replace")
    except OSError:
        return None

    if "End time" in text:  # tracer completed; any failure is unrelated to these
        return None

    started_tracing = ("Start cell tracing" in text) or ("tracing, round" in text)
    crashed = any(sig in text for sig in _CRASH_SIGNATURES)
    sn_failed = "SN FAILED" in text

    if started_tracing and (crashed or sn_failed):
        return (
            "StarryNite C tracer crashed during the cell-tracing (lineaging) phase: "
            "nuclear detection completed for all timepoints, but tracing never reached "
            "'End time'. This is the known cell_ct_limit buffer overflow — the compiled "
            "MATLAB loader hardcodes cell_ct_limit=5000, which sizes the tracer's centroid "
            "scratch buffers. Dense embryos at full (correct) resolution overflow it and "
            "corrupt the heap (segfault after 'allocated neighbor arrays'). "
            "matlab_SN_cluster.pl now raises cell_ct_limit to 50000 before lineaging — "
            "re-run this series. If it still fails, raise the limit further in "
            "matlab_SN_cluster.pl. (Note: this hybrid MATLAB+C StarryNite is slated for "
            "replacement by a MATLAB-only approach.)"
        )

    if crashed and ("Elapsed time" in text):
        return (
            "StarryNite crashed during MATLAB detection teardown (heap abort after "
            "'Elapsed time', before cell tracing began). This is usually a "
            "corrupted/shared MCR cache; embryoDB isolates MCR_CACHE_ROOT per run to "
            "avoid it. Re-run this series."
        )

    return None


def _run_with_heartbeat(
    cmd: list[str],
    log_path: Path,
    run_id: int,
    *,
    cwd: str | None = None,
    env: dict | None = None,
    max_seconds: int | None = None,
) -> int:
    """Spawn cmd, stream output to log_path, heartbeat the DB row every
    HEARTBEAT_INTERVAL seconds, and watchdog for a crashed-but-wedged process.

    Returns the process returncode, or a negative sentinel if the watchdog
    had to kill a wedged process (so the caller marks the step FAILED).

    `max_seconds`, when set, is a HARD wall-clock cap: the process is killed
    once it has run that long regardless of CPU/log activity. Unlike the
    CPU/log-idle heuristic below — which periodic MCR/java child processes can
    defeat by intermittently burning CPU — this ceiling is deterministic and
    cannot be fooled by a busy-but-stuck job. None = no cap (the Java steps,
    which are bounded in practice).
    """
    ensure_dir(log_path.parent)
    with open(log_path, "w", encoding="utf-8", errors="replace") as log_fp:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env,
            start_new_session=True,  # own process group => watchdog can killpg
        )

    pid = getattr(proc, "pid", None)
    try:
        pgid = os.getpgid(pid) if pid is not None else None
    except (ProcessLookupError, OSError):
        pgid = pid

    started = time.monotonic()
    last_cpu = _proc_group_cpu_ticks(pgid)
    last_size = _safe_size(log_path)
    idle_seconds = 0
    wedged_reason: str | None = None

    # Polling loop: heartbeat + watchdog while the subprocess is running.
    while proc.poll() is None:
        time.sleep(HEARTBEAT_INTERVAL)
        try:
            with session_scope() as s:
                run = s.get(PipelineStepRun, run_id)
                if run is not None:
                    run.heartbeat_at = datetime.now(tz=timezone.utc)
        except Exception:
            pass  # Heartbeat failures are non-fatal; the step continues.

        # (1) Fast-fail on a known crash signature in the log tail.
        tail = _tail_log(log_path, 40)
        hit = next((sig for sig in _CRASH_SIGNATURES if sig in tail), None)
        if hit is not None:
            wedged_reason = f"crash signature in log: {hit!r}"
            break

        # (2) Wedge detection: no CPU AND no new log output for the window.
        cpu = _proc_group_cpu_ticks(pgid)
        size = _safe_size(log_path)
        if cpu == last_cpu and size == last_size:
            idle_seconds += HEARTBEAT_INTERVAL
        else:
            idle_seconds = 0
        last_cpu, last_size = cpu, size
        if idle_seconds >= WATCHDOG_IDLE_SECONDS:
            wedged_reason = (
                f"no CPU activity or log output for {idle_seconds}s "
                "(crashed but did not exit?)"
            )
            break

        # (3) Hard wall-clock cap — deterministic backstop to (2).
        if max_seconds is not None and (time.monotonic() - started) >= max_seconds:
            elapsed = int(time.monotonic() - started)
            wedged_reason = (
                f"exceeded hard wall-clock cap ({elapsed}s >= {max_seconds}s); "
                "killing (stuck or pathologically slow run?)"
            )
            break

    if wedged_reason is not None:
        try:
            with open(log_path, "a", encoding="utf-8", errors="replace") as fp:
                fp.write(
                    f"\n*** embryoDB watchdog: {wedged_reason}; "
                    "killing process group ***\n"
                )
        except OSError:
            pass
        _kill_process_group(proc)
        chmod_if_possible(log_path, 0o664)
        return -1  # sentinel => _finish_run marks FAILED

    # Apply project permissions to the log file.
    chmod_if_possible(log_path, 0o664)

    return proc.returncode


def _finish_run(
    run_id: int,
    log_path: Path,
    returncode: int,
    *,
    diagnostic: str | None = None,
    output_summary: dict | None = None,
) -> None:
    """Commit the final COMPLETE or FAILED status for a run row.

    When `diagnostic` is given (a recognized failure cause), it is prepended to
    the error_excerpt so the user sees the explanation above the raw log tail.
    `output_summary`, when given, is recorded as structured run metadata (e.g.
    a detection-collapse analysis with the recommended recovery action).
    """
    with session_scope() as s:
        run = s.get(PipelineStepRun, run_id)
        if run is None:
            return
        run.log_path = str(log_path)
        run.completed_at = datetime.now(tz=timezone.utc)
        if output_summary is not None:
            run.output_summary = output_summary
        if returncode == 0:
            run.status = RunStatus.COMPLETE
        else:
            run.status = RunStatus.FAILED
            excerpt = _tail_log(log_path)
            if diagnostic:
                excerpt = f"{diagnostic}\n\n--- log tail ---\n{excerpt}"
            run.error_excerpt = excerpt


def _normalize_dats_perms(series_name: str) -> None:
    """After a legacy jar writes into `dats/`, bring those files back to the
    project's group + mode policy (the jar writes with its own umask/group, so
    the next lab member couldn't otherwise re-save). Never lets a perms fixup
    break the worker — failures are swallowed (mode bits are recoverable via
    `embryodb fix-permissions`).
    """
    try:
        from .. import permissions

        with session_scope() as s:
            permissions.normalize_series(s, series_name, dats_only=True)
    except Exception:
        pass


def _skip_run(run_id: int, reason: str) -> None:
    """Mark a run as SKIPPED (e.g. no reporter channel)."""
    with session_scope() as s:
        run = s.get(PipelineStepRun, run_id)
        if run is None:
            return
        run.status = RunStatus.SKIPPED
        run.completed_at = datetime.now(tz=timezone.utc)
        run.error_excerpt = reason


def _write_series_list(tmp_dir: Path, series_name: str) -> Path:
    """Write a single-line acebatch3 series list file. Returns the path.

    acebatch3 treats each line as a series name, looks up
    <embryoDB_source_dir>/<name>.xml, and derives image/annot paths from there.
    Do NOT write the image_loc path — the Java tool would prepend source_dir to
    it and produce a nonsensical combined path.
    """
    list_file = tmp_dir / f"{series_name}.list"
    list_file.write_text(series_name + "\n", encoding="utf-8")
    return list_file


# ---------------------------------------------------------------------------
# Public step functions
# ---------------------------------------------------------------------------


def step_run_starrynite(
    series_id: int,
    series_name: str,
    image_loc: Path,
    run_id: int,
) -> None:
    """Wrap matlab_SN_cluster.pl for one series.

    Command shape (derived from matlabRunner.pl line 21):
        nice perl <tools3_dir>/matlab_SN_cluster.pl \\
             <image_loc>/tif/<series_name> \\
             <image_loc>/matlabParams

    The Perl script calls chdir($SCRIPTLOCATION) internally, so we set
    cwd=tools3_dir so it can find starrynite_traceonly/starrynite relative
    to itself.

    Each run gets a private MCR_CACHE_ROOT. The compiled-Matlab core (MCR
    v714) extracts its CTF archive into this cache on startup; a shared cache
    across concurrent/sequential runs can corrupt and has been implicated in
    heap-corruption aborts during MCR teardown (`double free` /
    `malloc_consolidate` after detection completes). Isolating it per run
    removes that cross-run interference. Cleaned up after the run.
    """
    log_path = ensure_dir(image_loc / "logs") / f"{series_name}-run_starrynite.log"

    # Record log_path before the subprocess starts so the GUI can open it, and
    # read the per-run engine choice (params.sn_engine; default "old").
    engine = "old"
    run_params: dict = {}
    with session_scope() as s:
        run = s.get(PipelineStepRun, run_id)
        if run is not None:
            run.log_path = str(log_path)
            run_params = dict(run.params or {})
            engine = run_params.get("sn_engine", "old")

    if engine == "new":
        _run_starrynite_new(series_id, series_name, image_loc, run_id, log_path)
        return
    if engine == "prod":
        _run_starrynite_prod(
            series_id, series_name, image_loc, run_id, log_path, run_params
        )
        return

    # The Perl script writes the lineage zips itself, so unlike the new-SN path
    # there is no Python write to hook -- archive before handing over control.
    archived = archive_annotations(
        image_loc / "dats", series_name,
        reason="run_starrynite (sn_engine=old) about to rerun",
    )
    if archived:
        _append_log(log_path, f"embryoDB archived existing lineage zips: {archived}")

    cmd = [
        "nice",
        "perl",
        str(settings.tools3_dir / "matlab_SN_cluster.pl"),
        str(image_loc / "tif" / series_name),
        str(image_loc / "matlabParams"),
    ]
    mcr_cache = tempfile.mkdtemp(prefix=f"mcrcache-{series_name}-")
    env = os.environ.copy()
    env["MCR_CACHE_ROOT"] = mcr_cache
    try:
        returncode = _run_with_heartbeat(
            cmd, log_path, run_id,
            cwd=str(settings.tools3_dir), env=env,
            max_seconds=settings.starrynite_max_seconds,
        )
    finally:
        shutil.rmtree(mcr_cache, ignore_errors=True)
    if returncode == 0:
        _finish_run(run_id, log_path, returncode)
        return

    # Failure: try automatic detection-collapse recovery before finalizing.
    diagnostic = diagnose_starrynite_failure(log_path)
    recovery = _auto_recover(series_name)
    if recovery is not None and recovery.action == "truncate":
        # run_starrynite was re-queued (end_time=K, status back to PENDING);
        # the worker will re-claim and re-run the truncated movie. Do NOT
        # finalize this run — leaving it PENDING is the whole point.
        _append_log(
            log_path,
            f"embryoDB auto-recovery: {recovery.detail}; re-queued run_starrynite.",
        )
        return

    diagnostic, output_summary = _stub_recovery_notes(diagnostic, recovery)
    _finish_run(
        run_id, log_path, returncode,
        diagnostic=diagnostic, output_summary=output_summary,
    )


def _run_starrynite_new(
    series_id: int,
    series_name: str,
    image_loc: Path,
    run_id: int,
    log_path: Path,
) -> None:
    """Run the NEW all-MATLAB StarryNite (release_v1) for one series.

    Selected via the run row's ``params.sn_engine == "new"``. The whole run
    happens under ``settings.starrynite_scratch_root`` (never image_loc); only
    the final lineage lands in ``dats/`` — in the SAME slot the legacy pipeline
    uses (``<series>-edit.zip`` + pristine ``<series>.zip``), so downstream
    steps and AceTree find it identically.

    The curation-effort estimate is NOT computed here — it is its own
    ``compute_difficulty`` step, so it runs for every engine and every route
    (fresh import, rerun, recovery) and is retryable on its own.

    Unlike the legacy path there is no detection-collapse auto-recovery: the
    retrained tracker's robustness is the whole point (it does not wedge), and
    the effort predictor is the triage signal instead.
    """
    from . import starrynite_v1 as snv1

    params_path = image_loc / "matlabParams"
    if not params_path.exists():
        _finish_run(
            run_id, log_path, 1,
            diagnostic=(
                f"new StarryNite: no matlabParams at {params_path}; run "
                "write_matlab_params first"
            ),
        )
        return

    dats_dir = image_loc / "dats"

    scratch = Path(
        tempfile.mkdtemp(
            prefix=f"embryodb-sn-{series_name}-",
            dir=str(ensure_dir(settings.starrynite_scratch_root)),
        )
    )
    scratch_out = scratch / "out"
    try:
        paramfile = snv1.build_newmatlab_paramfile(params_path, scratch / "params.txt")
        image_first = image_loc / "tif" / f"{series_name}-t001-p01.tif"
        cmd, env = snv1.build_matlab_command(paramfile, image_first, scratch_out)
        returncode = _run_with_heartbeat(
            cmd, log_path, run_id,
            cwd=str(scratch), env=env,
            max_seconds=settings.starrynite_max_seconds,
        )
        if returncode != 0:
            _finish_run(
                run_id, log_path, returncode,
                diagnostic="new StarryNite (release_v1) run failed; see log tail.",
            )
            return

        # Land the lineage in the legacy slot.
        try:
            landed = snv1.land_lineage(scratch_out, dats_dir, series_name)
        except FileNotFoundError as exc:
            _finish_run(run_id, log_path, 1, diagnostic=str(exc))
            return

        summary: dict = {"sn_engine": "new", **landed}
        _finish_run(run_id, log_path, 0, output_summary=summary)
        _normalize_dats_perms(series_name)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _run_starrynite_prod(
    series_id: int,
    series_name: str,
    image_loc: Path,
    run_id: int,
    log_path: Path,
    run_params: dict | None = None,
) -> None:
    """Run the PRODUCTION StarryNite (``sn_production_driver.m``) for one series.

    Selected via the run row's ``params.sn_engine == "prod"``. Structurally the
    same shape as the new-SN path — scratch-only run, land into the legacy
    ``dats/`` slot — with two differences: the driver consumes the series'
    ``matlabParams`` directly (optionally with the intensity threshold flattened
    across staging bands), and it emits loose ``tNNN-nuclei`` rather than a zip,
    so landing packs one.

    The driver's code resolves from the ACTIVE StarryNite dev tree, so the
    provenance snapshot recorded on the run is the only durable record of what
    produced this lineage. It is captured before the run, not after, so a run
    that later fails still says what it tried.
    """
    from . import starrynite_prod as snp

    params_path = image_loc / "matlabParams"
    if not params_path.exists():
        _finish_run(
            run_id, log_path, 1,
            diagnostic=(
                f"production StarryNite: no matlabParams at {params_path}; run "
                "write_matlab_params first"
            ),
        )
        return

    try:
        options = snp.resolve_options(run_params)
    except ValueError as exc:
        _finish_run(run_id, log_path, 1, diagnostic=str(exc))
        return

    provenance = snp.capture_provenance(options)
    if provenance.get("dev_dirty"):
        _append_log(
            log_path,
            "embryoDB NOTE: the StarryNite dev tree has uncommitted changes, so "
            "the recorded git HEAD does not fully identify the code used:\n  "
            + "\n  ".join(provenance["dev_dirty"]),
        )

    scratch = Path(
        tempfile.mkdtemp(
            prefix=f"embryodb-snprod-{series_name}-",
            dir=str(ensure_dir(settings.starrynite_scratch_root)),
        )
    )
    scratch_out = scratch / "out"
    try:
        try:
            paramfile = snp.build_prod_paramfile(
                params_path,
                scratch / "matlabParams",
                uniform_threshold=options["uniform_threshold"],
            )
        except ValueError as exc:
            _finish_run(run_id, log_path, 1, diagnostic=str(exc))
            return

        image_first = image_loc / "tif" / f"{series_name}-t001-p01.tif"
        cmd, env = snp.build_matlab_command(
            paramfile, image_first, scratch_out, scratch, options=options
        )
        returncode = _run_with_heartbeat(
            cmd, log_path, run_id,
            cwd=str(scratch), env=env,
            max_seconds=settings.starrynite_max_seconds,
        )
        if returncode != 0:
            _finish_run(
                run_id, log_path, returncode,
                diagnostic=snp.diagnose_prod_failure(log_path, options),
                output_summary={"sn_engine": "prod", "provenance": provenance},
            )
            return

        try:
            landed = snp.land_lineage(
                scratch_out, image_loc / "dats", series_name, paramfile=paramfile
            )
        except FileNotFoundError as exc:
            _finish_run(
                run_id, log_path, 1, diagnostic=str(exc),
                output_summary={"sn_engine": "prod", "provenance": provenance},
            )
            return

        summary: dict = {"sn_engine": "prod", **landed, "provenance": provenance}
        _finish_run(run_id, log_path, 0, output_summary=summary)
        _normalize_dats_perms(series_name)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def step_compute_difficulty(
    series_id: int,
    series_name: str,
    image_loc: Path,
    run_id: int,
) -> None:
    """Predict curation effort for one series and store it in `series_difficulty`.

    Reads the RAW tracker lineage ``dats/<series>.zip`` — never ``-edit.zip``,
    which on a curated series is human-corrected and would predict near-zero
    effort for a movie that in fact took hours to fix.

    This step never FAILs. It sits between run_starrynite and run_red_extract
    (the lineage is its only input), so a FAILED row would strand extraction
    behind an advisory triage number. Anything that prevents a prediction is
    recorded as SKIPPED with the reason, and is retryable on its own via
    ``embryodb pipeline rerun --step compute_difficulty``.

    The model is calibrated on new-SN output. Legacy classic-SN lineages are
    predicted anyway — every series deserves a triage signal — but their rows
    carry ``difficulty.OUT_OF_SCOPE_SUFFIX`` on the model_version so the documented
    negative transfer can never be mistaken for an in-scope number.
    """
    from ..effort_manifest import resolution_for
    from ..queries import difficulty as q_difficulty
    from . import starrynite_v1 as snv1

    log_path = ensure_dir(image_loc / "logs") / f"{series_name}-compute_difficulty.log"

    with session_scope() as s:
        run = s.get(PipelineStepRun, run_id)
        if run is not None:
            run.log_path = str(log_path)
        series = s.get(Series, series_id)
        if series is None:
            _skip_run(run_id, f"series id {series_id} is not in the DB")
            return
        xyres, zres = resolution_for(series)
        annot_loc = series.annot_loc or str(image_loc)
        sn_run = (
            s.query(PipelineStepRun)
            .filter_by(series_id=series_id, step="run_starrynite")
            .one_or_none()
        )
        engine = (sn_run.params or {}).get("sn_engine", "old") if sn_run else "old"

    dats = Path(annot_loc) / "dats"
    lineage = dats / f"{series_name}.zip"
    if not lineage.exists():
        _skip_run(run_id, f"no raw lineage at {lineage}; nothing to predict on")
        return

    result = snv1.run_effort(lineage, xyres, zres, dats, series_name)
    if not result.get("ran"):
        _skip_run(
            run_id,
            f"effort predictor produced no result: {result.get('error') or 'unknown'}",
        )
        return

    model_version = result.get("model_version") or "unknown"
    if engine != "new":
        model_version += q_difficulty.OUT_OF_SCOPE_SUFFIX

    predictions = [
        {
            "stage": st["stage"],
            "effort_predicted": st.get("effort_predicted"),
            "effort_bucket": st.get("effort_bucket"),
            "model_version": model_version,
        }
        for st in result.get("stages") or []
        if st.get("stage")
    ]
    if not predictions:
        _skip_run(run_id, "effort predictor returned no per-stage predictions")
        return

    try:
        with session_scope() as s:
            q_difficulty.upsert_predictions(s, series_name, predictions)
    except Exception as exc:  # noqa: BLE001 — the numbers are on disk either way
        _skip_run(run_id, f"could not store predictions: {type(exc).__name__}: {exc}")
        return

    report = result.get("report_path")
    try:
        log_path.write_text(
            Path(report).read_text(encoding="utf-8") if report else "",
            encoding="utf-8",
        )
        chmod_if_possible(log_path, 0o664)
    except OSError:
        pass

    _finish_run(
        run_id, log_path, 0,
        output_summary={
            "sn_engine": engine,
            "model_version": model_version,
            "triage": result.get("triage"),
            "effort_events": result.get("effort_events"),
            "report_path": report,
            "json_path": result.get("json_path"),
            "n_stages": len(predictions),
        },
    )


def _auto_recover(series_name: str):
    """Run worker-side auto-recovery in its own session. Returns the
    RecoveryResult (a plain dataclass, safe past session close) or None when no
    detection collapse is recognized / anything went wrong."""
    from .sn_recovery import auto_recover

    try:
        with session_scope() as s:
            return auto_recover(s, series_name, spawn=False)
    except Exception:
        return None


def _stub_recovery_notes(
    diagnostic: str | None, recovery
) -> tuple[str | None, dict | None]:
    """Build the FAILED-run note + output_summary for the stub-fallback path.

    A stub was auto-written (recovery.action == "stub"): the run still failed to
    trace, but AceTree can now open the raw images. When recovery is None (no
    collapse — some other failure), pass the original diagnostic through."""
    if recovery is None or recovery.action != "stub":
        return diagnostic, None
    note = (
        f"StarryNite detection collapsed: only timepoints "
        f"1..{recovery.last_usable_timepoint} of {recovery.total_timepoints} are "
        "usable (dim/photobleached signal or the embryo drifting out of frame). "
        "The usable prefix is too short to trace, so embryoDB auto-wrote a stub "
        f"annotation — AceTree can open the raw images. {recovery.detail}"
    )
    diagnostic = f"{diagnostic}\n\n{note}" if diagnostic else note
    summary = {
        "detection_collapse": True,
        "last_usable_timepoint": recovery.last_usable_timepoint,
        "total_timepoints": recovery.total_timepoints,
        "recovery_action": "stub",
    }
    return diagnostic, summary


def _append_log(log_path: Path, message: str) -> None:
    try:
        with open(log_path, "a", encoding="utf-8", errors="replace") as fp:
            fp.write(f"\n*** {message} ***\n")
    except OSError:
        pass


def step_run_red_extract(
    series_id: int,
    series_name: str,
    image_loc: Path,
    run_id: int,
    protocol_channel_map: dict,
) -> None:
    """Wrap acebatch3.jar RedExtractor1 for one series.

    Skipped automatically when the protocol has no reporter channel.

    Command shape:
        nice java -mx500m -cp <tools3_dir>/acebatch3.jar \\
             RedExtractor1 <series_list_file>
    """
    if "reporter" not in protocol_channel_map.values():
        _skip_run(run_id, "no reporter channel in protocol channel_map")
        return

    log_path = ensure_dir(image_loc / "logs") / f"{series_name}-run_red_extract.log"

    with session_scope() as s:
        run = s.get(PipelineStepRun, run_id)
        if run is not None:
            run.log_path = str(log_path)

    with tempfile.TemporaryDirectory() as tmp_dir:
        list_file = _write_series_list(Path(tmp_dir), series_name)
        cmd = [
            "nice",
            settings.java_command,
            f"-mx{settings.java_mx}",
            "-cp",
            str(settings.tools3_dir / "acebatch3.jar"),
            "RedExtractor1",
            str(list_file),
        ]
        returncode = _run_with_heartbeat(cmd, log_path, run_id)

    _finish_run(run_id, log_path, returncode)
    if returncode == 0:
        _normalize_dats_perms(series_name)


def step_run_measure(
    series_id: int,
    series_name: str,
    image_loc: Path,
    run_id: int,
    protocol_channel_map: dict,
) -> None:
    """Wrap acebatch3.jar Measure1 for one series.

    Skipped automatically when the protocol has no reporter channel.

    Command shape:
        nice java -mx500m -cp <tools3_dir>/acebatch3.jar \\
             Measure1 <series_list_file>
    """
    if "reporter" not in protocol_channel_map.values():
        _skip_run(run_id, "no reporter channel in protocol channel_map")
        return

    log_path = ensure_dir(image_loc / "logs") / f"{series_name}-run_measure.log"

    with session_scope() as s:
        run = s.get(PipelineStepRun, run_id)
        if run is not None:
            run.log_path = str(log_path)

    with tempfile.TemporaryDirectory() as tmp_dir:
        list_file = _write_series_list(Path(tmp_dir), series_name)
        cmd = [
            "nice",
            settings.java_command,
            f"-mx{settings.java_mx}",
            "-cp",
            str(settings.tools3_dir / "acebatch3.jar"),
            "Measure1",
            str(list_file),
        ]
        returncode = _run_with_heartbeat(cmd, log_path, run_id)

    _finish_run(run_id, log_path, returncode)
    if returncode == 0:
        _normalize_dats_perms(series_name)


def step_stage_images(series_id: int, run_id: int) -> None:
    """Worker-side staging: copy + LZW-compress raw TIFs into the series'
    image_loc. The orchestrator skips its inline stage_images when the user
    requested a delay; this function is what the worker eventually runs.

    All required inputs (source_dir, protocol, image_loc_root, position) are
    rehydrated from the DB so the worker doesn't need the caller's
    Python-side state.
    """
    from .stage import plan_acquisition, stage_position
    from ..models import Acquisition, Series

    log_path: Path | None = None
    started_at = datetime.now(tz=timezone.utc)

    # Resolve everything we need from the DB up front.
    with session_scope() as s:
        series = s.get(Series, series_id)
        if series is None:
            _fail_run_now(run_id, f"series id={series_id} not found")
            return
        if series.acquisition_id is None:
            _fail_run_now(run_id, f"series {series.series_name!r} has no acquisition; cannot re-stage")
            return
        acq = s.get(Acquisition, series.acquisition_id)
        if acq is None or not acq.source_dir:
            _fail_run_now(run_id, f"acquisition {series.acquisition_id} has no source_dir")
            return
        protocol = acq.protocol
        if protocol is None:
            _fail_run_now(run_id, f"acquisition {acq.name!r} has no protocol")
            return

        # Capture all the scalars we need; ORM objects expire after the
        # session closes.
        series_name = series.series_name
        position = series.position
        if position is None:
            _fail_run_now(run_id, f"series {series_name!r} has no position recorded")
            return
        source_dir = Path(acq.source_dir)
        parser_name = acq.parser_name or "leica_tilescan"
        image_loc = Path(series.image_loc) if series.image_loc else None
        if image_loc is None:
            _fail_run_now(run_id, f"series {series_name!r} has no image_loc")
            return
        # image_loc_root is the parent-of-parent of image_loc:
        # /murrlab3/<user>/images/<series>/   →   /murrlab3/<user>/images/
        image_loc_root = image_loc.parent
        slices_str = (protocol.defaults or {}).get("slices") or ""
        slices = int(slices_str) if slices_str.isdigit() else 67
        # channel_map keys come out of JSON as strings; the staging code
        # expects int keys.
        channel_map = {int(k): v for k, v in (protocol.channel_map or {}).items()}

    # Log location matches the other subprocess steps.
    log_path = ensure_dir(image_loc / "logs") / f"{series_name}-stage_images.log"
    with session_scope() as s:
        run = s.get(PipelineStepRun, run_id)
        if run is not None:
            run.log_path = str(log_path)
            run.started_at = started_at
            run.heartbeat_at = started_at

    try:
        if not source_dir.is_dir():
            raise FileNotFoundError(
                f"source_dir does not exist: {source_dir}  "
                "(was it deleted between scheduling and now?)"
            )
        plan = plan_acquisition(source_dir, _DummyProtocol(channel_map), parser_name=parser_name)
        if position not in plan.positions:
            raise ValueError(
                f"position {position} not found in {source_dir} (plan had positions: "
                f"{sorted(plan.positions)})"
            )
        pos_plan = plan.positions[position]
        # Stage with periodic heartbeat updates. stage_position is blocking
        # and single-threaded; we run it on a background thread so the main
        # thread can heartbeat without waiting for I/O completion.
        outcome_holder: dict[str, object] = {}
        exc_holder: list[BaseException] = []

        def _do_stage():
            try:
                outcome_holder["out"] = stage_position(
                    pos_plan,
                    channel_map,
                    image_loc_root=image_loc_root,
                    slices=slices,
                    compress_with_lzw=True,
                    overwrite=True,  # worker re-runs always overwrite
                )
            except BaseException as exc:
                exc_holder.append(exc)

        import threading
        t = threading.Thread(target=_do_stage, daemon=True)
        t.start()
        while t.is_alive():
            t.join(timeout=HEARTBEAT_INTERVAL)
            try:
                with session_scope() as s:
                    run = s.get(PipelineStepRun, run_id)
                    if run is not None:
                        run.heartbeat_at = datetime.now(tz=timezone.utc)
            except Exception:
                pass

        if exc_holder:
            raise exc_holder[0]

        outcome = outcome_holder.get("out")
        # Write a brief summary into the log.
        try:
            with open(log_path, "w", encoding="utf-8") as fp:
                fp.write(
                    f"stage_images for {series_name}\n"
                    f"  source_dir: {source_dir}\n"
                    f"  position:   {position}\n"
                    f"  image_loc:  {image_loc}\n"
                    f"  raw count:  {plan.raw_count}\n"
                    f"  written:    {getattr(outcome, 'written', 0)}\n"
                    f"  skipped:    {getattr(outcome, 'skipped', 0)}\n"
                    f"  errors:     {len(getattr(outcome, 'errors', []))}\n"
                )
                for name, err in getattr(outcome, "errors", []):
                    fp.write(f"  err: {name}: {err}\n")
        except OSError:
            pass

        # Record success — also update series.timepts from the actual staged
        # file count, mirroring what the inline orchestrator does.
        with session_scope() as s:
            run = s.get(PipelineStepRun, run_id)
            if run is not None:
                run.status = RunStatus.COMPLETE
                run.completed_at = datetime.now(tz=timezone.utc)
                run.output_summary = {
                    "written": getattr(outcome, "written", 0),
                    "skipped": getattr(outcome, "skipped", 0),
                    "errors": len(getattr(outcome, "errors", [])),
                }
            series = s.get(Series, series_id)
            synced_name = None
            if series is not None:
                series.timepts = str(pos_plan.n_timepoints)
                synced_name = series.series_name
        # Mirror the updated timepts into the legacy <series>.xml. Runs after
        # the session commits (sync_legacy_xml opens its own); best-effort so a
        # mirror failure never fails the staging step.
        if synced_name:
            try:
                from .. import legacy_sync

                legacy_sync.sync_legacy_xml(synced_name)
            except Exception:  # noqa: BLE001 — mirror is non-critical
                pass
    except BaseException as exc:  # noqa: BLE001 — we need the full message
        _fail_run_now(run_id, f"{type(exc).__name__}: {exc}", log_path=log_path)


def _fail_run_now(run_id: int, message: str, log_path: Path | None = None) -> None:
    """Mark a step run as FAILED with `message` as the error_excerpt."""
    with session_scope() as s:
        run = s.get(PipelineStepRun, run_id)
        if run is None:
            return
        run.status = RunStatus.FAILED
        run.completed_at = datetime.now(tz=timezone.utc)
        run.error_excerpt = message[:1024]
        if log_path is not None:
            run.log_path = str(log_path)
    if log_path is not None:
        try:
            with open(log_path, "a", encoding="utf-8") as fp:
                fp.write(f"\nFAILED: {message}\n")
        except OSError:
            pass


class _DummyProtocol:
    """Minimal stand-in for the ORM Protocol object so plan_acquisition can
    read .channel_map without needing a live SQLAlchemy session."""
    def __init__(self, channel_map: dict[int, str]) -> None:
        self.channel_map = {str(k): v for k, v in channel_map.items()}


__all__ = [
    "step_run_starrynite",
    "step_compute_difficulty",
    "step_run_red_extract",
    "step_run_measure",
    "step_stage_images",
    "HEARTBEAT_INTERVAL",
]
