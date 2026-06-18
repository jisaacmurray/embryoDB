"""Recognize StarryNite *detection collapse* and recover from it.

The hybrid StarryNite (compiled-MATLAB nuclear detection → C tracer) fails hard
when the input movie degrades partway through: photobleaching / a dim reporter,
or the embryo drifting laterally out of frame at later timepoints. Detection
then finds ~0–3 "nuclei" per timepoint where there should be hundreds, the C
tracer cannot build a lineage from that, and it wedges without ever printing
"End time" — the run is marked FAILED (see
:func:`subprocess_steps.diagnose_starrynite_failure`).

This module turns that dead end into two recoverable outcomes:

  1. **Truncated retry** — if a long, healthy *prefix* of timepoints exists
     (the common drift / late-bleach case: good early, bad late), re-run the
     pipeline on just ``1..K`` by setting ``end_time=K`` in ``matlabParams`` and
     re-queuing ``run_starrynite``. The truncated movie traces cleanly and you
     get a real (partial) lineage.
  2. **Stub fallback** — if the usable prefix is too short to be worth tracing
     (this movie collapsed almost immediately), drop a placeholder annotation
     (:mod:`embryodb.pipeline.stub_annotation`) so AceTree can at least open the
     raw images for inspection.

The collapse detector is deliberately conservative so it does *not* fire on the
legitimate few-cell early embryo: collapse is only declared once detection has
reached a substantial peak and then falls and *stays* low.

This is a stopgap for the current tracer's brittleness; a rewritten tracer that
degrades gracefully would make it unnecessary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..parsers.matlab_params import load as load_params

# Collapse-detection tuning.
COLLAPSE_FRACTION = 0.25  # "low" = below this fraction of the running peak
COLLAPSE_FLOOR = 4        # ...but always treat < this many nuclei as low
COLLAPSE_RUN = 4          # this many consecutive low timepoints => collapsed
MIN_PEAK = 20             # don't declare collapse until detection peaked here
MIN_TRACEABLE = 10        # > this many nuclei is always traceable (never "low"),
                          # even when below COLLAPSE_FRACTION of the peak
MIN_USABLE_PREFIX = 30    # fewer usable timepoints than this => prefer the stub
MIN_TRUNCATE_GAIN = 5     # only truncate if it shortens the run by at least this


@dataclass
class CollapseReport:
    counts: dict[int, int]          # timepoint -> detected nucleus count
    total_timepoints: int
    peak: int                       # max nuclei at any single timepoint
    collapsed: bool                 # did detection collapse mid-movie?
    last_usable_timepoint: int      # K: end of the healthy prefix (1-based)


def matlabnuclei_dir(image_loc: Path, series_name: str) -> Path:
    """Location of the per-timepoint detection output for a series."""
    return Path(image_loc) / "tif" / f"{series_name}_matlabnuclei"


def count_detected_nuclei(nuclei_dir: Path) -> dict[int, int]:
    """Per-timepoint detected-nucleus counts from ``matlabnuclei<N>`` files.

    Each file holds one nucleus per line, so the count is the line count.
    Missing/unreadable files are treated as zero. Diameter files
    (``matlabdiams<N>``) are ignored.
    """
    counts: dict[int, int] = {}
    if not nuclei_dir.is_dir():
        return counts
    for child in nuclei_dir.iterdir():
        name = child.name
        if not name.startswith("matlabnuclei"):
            continue
        suffix = name[len("matlabnuclei"):]
        if not suffix.isdigit():
            continue
        tp = int(suffix)
        try:
            text = child.read_text(errors="replace")
        except OSError:
            counts[tp] = 0
            continue
        counts[tp] = sum(1 for line in text.splitlines() if line.strip())
    return counts


def analyze_counts(counts: dict[int, int]) -> CollapseReport:
    """Decide whether detection collapsed and where the healthy prefix ends.

    Walks timepoints in order tracking the running peak. A timepoint is "low"
    when its count is below ``max(COLLAPSE_FLOOR, COLLAPSE_FRACTION*peak)`` *and*
    not above ``MIN_TRACEABLE`` — i.e. a timepoint with more than
    ``MIN_TRACEABLE`` nuclei is always considered traceable and keeps the prefix
    alive, even if it dipped below the fraction. Collapse is declared at the
    first run of ``COLLAPSE_RUN`` consecutive low timepoints *after* the peak
    has reached ``MIN_PEAK``; the last usable timepoint K is the one just before
    that run. With no collapse, K is the last timepoint (the failure was
    something else — caller won't truncate).
    """
    if not counts:
        return CollapseReport(
            counts={}, total_timepoints=0, peak=0, collapsed=False,
            last_usable_timepoint=0,
        )
    tps = sorted(counts)
    total = tps[-1]
    peak = max(counts.values())

    running_peak = 0
    low_run = 0
    low_run_start: int | None = None
    for tp in tps:
        c = counts[tp]
        running_peak = max(running_peak, c)
        threshold = max(COLLAPSE_FLOOR, COLLAPSE_FRACTION * running_peak)
        if running_peak >= MIN_PEAK and c < threshold and c <= MIN_TRACEABLE:
            if low_run == 0:
                low_run_start = tp
            low_run += 1
            if low_run >= COLLAPSE_RUN:
                k = max(0, (low_run_start or tp) - 1)
                return CollapseReport(
                    counts=counts, total_timepoints=total, peak=peak,
                    collapsed=True, last_usable_timepoint=k,
                )
        else:
            low_run = 0
            low_run_start = None
    return CollapseReport(
        counts=counts, total_timepoints=total, peak=peak, collapsed=False,
        last_usable_timepoint=total,
    )


def analyze_series_collapse(image_loc: Path, series_name: str) -> CollapseReport:
    """Convenience: count detection output for a series and analyze it."""
    counts = count_detected_nuclei(matlabnuclei_dir(image_loc, series_name))
    return analyze_counts(counts)


@dataclass
class RecoveryResult:
    series_name: str
    action: str                     # "truncate" | "stub" | "none"
    last_usable_timepoint: int
    total_timepoints: int
    detail: str


def recover_series(
    session,
    series_name: str,
    *,
    action: str = "auto",
    min_usable_prefix: int = MIN_USABLE_PREFIX,
    spawn: bool = False,
) -> RecoveryResult:
    """Recover one collapsed StarryNite run (CLI/GUI shared core).

    ``action``:
      * ``"auto"`` — truncate when a usable prefix of at least
        ``min_usable_prefix`` timepoints exists *and* truncating shortens the
        run meaningfully; otherwise write the stub.
      * ``"truncate"`` — force the truncated retry (errors if no usable prefix).
      * ``"stub"`` — force the stub regardless of the analysis.

    Truncation sets ``end_time=K`` in ``matlabParams`` and re-queues
    ``run_starrynite`` (via the shared :func:`rerun.requeue_series`); the worker
    is spawned only when ``spawn=True``. Stub generation writes the placeholder
    annotation immediately. Caller owns the session/commit.
    """
    from ..queries.series import get_by_name

    series = get_by_name(session, series_name)
    if series is None:
        raise ValueError(f"no such series: {series_name!r}")
    if not series.image_loc:
        raise ValueError(f"series {series_name!r} has no image_loc")

    report = analyze_series_collapse(Path(series.image_loc), series_name)
    k = report.last_usable_timepoint

    if action == "stub":
        return _do_stub(session, series, report, reason="forced")

    if action == "truncate":
        if k <= 0:
            raise ValueError(
                f"{series_name}: no usable timepoints detected; cannot truncate "
                "(use the stub instead)"
            )
        return _do_truncate(session, series_name, report, spawn=spawn)

    # action == "auto"
    ceiling = _current_end_time(Path(series.image_loc)) or report.total_timepoints
    can_truncate = (
        report.collapsed
        and k >= min_usable_prefix
        and k <= ceiling - MIN_TRUNCATE_GAIN
    )
    if can_truncate:
        return _do_truncate(session, series_name, report, spawn=spawn)
    reason = (
        f"usable prefix {k}/{report.total_timepoints} too short "
        f"(< {min_usable_prefix})"
        if report.collapsed
        else "no detection collapse recognized"
    )
    return _do_stub(session, series, report, reason=reason)


def _current_end_time(image_loc: Path) -> int | None:
    """The ``end_time`` currently set in the series' ``matlabParams``.

    Used as the truncation ceiling so we compare the usable prefix K against the
    range StarryNite was *told* to process, not the raw count of detection files
    on disk. This matters because a prior (longer) run leaves stale
    ``matlabnuclei`` files: after truncating to ``end_time=K`` those stale low
    counts at K+1.. still look like a "collapse", and guarding on
    ``total_timepoints`` would re-truncate forever. Guarding on ``end_time``
    bounds recovery to a single truncate (the healthy 1..K prefix re-runs
    cleanly). Returns None when unreadable.
    """
    pp = Path(image_loc) / "matlabParams"
    if not pp.exists():
        return None
    try:
        return load_params(pp).get_int("end_time")
    except OSError:
        return None


def auto_recover(session, series_name: str, *, spawn: bool = False) -> RecoveryResult | None:
    """Worker-side automatic recovery after a ``run_starrynite`` FAILED.

    Called by :func:`subprocess_steps.step_run_starrynite` (not a user action).
    Returns ``None`` when no detection collapse is recognized — the failure was
    something else (e.g. the cell_ct_limit tracer overflow), so the caller
    leaves the run FAILED with its existing diagnostic. On a recognized
    collapse it either:

      * **truncate** — a long healthy prefix exists *and* K shortens the current
        ``end_time`` by at least ``MIN_TRUNCATE_GAIN``: re-queue ``run_starrynite``
        with ``end_time=K`` (the caller must then NOT finalize the run — it is
        back to PENDING for the worker to re-claim); or
      * **stub** — the usable prefix is too short to trace: write the detection
        stub so AceTree can open the images. The caller finalizes FAILED with a
        note pointing at the stub.

    The ``end_time`` ceiling guard (see :func:`_current_end_time`) bounds this to
    a single truncate even with stale detection files on disk.
    """
    from ..queries.series import get_by_name

    series = get_by_name(session, series_name)
    if series is None or not series.image_loc:
        return None
    report = analyze_series_collapse(Path(series.image_loc), series_name)
    if not report.collapsed:
        return None

    k = report.last_usable_timepoint
    ceiling = _current_end_time(Path(series.image_loc)) or report.total_timepoints
    if k >= MIN_USABLE_PREFIX and k <= ceiling - MIN_TRUNCATE_GAIN:
        return _do_truncate(session, series_name, report, spawn=spawn)
    reason = f"usable prefix {k}/{report.total_timepoints} too short to trace"
    return _do_stub(session, series, report, reason=reason)


def _do_truncate(session, series_name, report, *, spawn: bool) -> RecoveryResult:
    from .rerun import requeue_series

    k = report.last_usable_timepoint
    requeue_series(
        session,
        [series_name],
        steps=["run_starrynite"],
        overrides={"end_time": str(k)},
    )
    if spawn:
        from .worker import spawn_worker

        spawn_worker()
    return RecoveryResult(
        series_name=series_name,
        action="truncate",
        last_usable_timepoint=k,
        total_timepoints=report.total_timepoints,
        detail=(
            f"re-queued run_starrynite with end_time={k} "
            f"(was {report.total_timepoints})"
        ),
    )


def _do_stub(session, series, report, *, reason: str) -> RecoveryResult:
    from .stub_annotation import write_stub_for_series

    stub = write_stub_for_series(series)
    if stub.mode == "detection":
        what = f"wrote detection annotation ({stub.n_nuclei} nuclei across 1..{stub.n_timepoints})"
    else:
        what = "wrote placeholder annotation"
    return RecoveryResult(
        series_name=series.series_name,
        action="stub",
        last_usable_timepoint=report.last_usable_timepoint,
        total_timepoints=report.total_timepoints,
        detail=f"{what} ({reason}); AceTree can open the images",
    )


__all__ = [
    "CollapseReport",
    "RecoveryResult",
    "matlabnuclei_dir",
    "count_detected_nuclei",
    "analyze_counts",
    "analyze_series_collapse",
    "recover_series",
    "auto_recover",
]
