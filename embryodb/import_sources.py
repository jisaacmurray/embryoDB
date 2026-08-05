"""Decide whether an import source is safe to delete.

The originals in ``/murrlab3/Images`` (a per-acquisition directory of raw TIFs,
or a ``.lif``) are redundant once a movie is staged, but they are the only
copy — so this module answers "did the staging actually capture everything?"
rather than "is there a DB row?".

The predecessor, ``tools3/CheckImages.pl``, asked only the latter: it stripped
``_L<n>`` off the legacy XML filenames and deleted the whole source directory if
*any* position had produced an XML. An acquisition where L1 staged and L2 failed
lost its L2 source. Everything here is per-series and the acquisition is
deletable only when every one of its series passes.

Nothing in this module deletes; it reports. The caller decides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Acquisition, PipelineStepRun, RunStatus, Series

#: Only sources under one of these roots may be proposed for deletion. A staged
#: tree or a hand-curated directory that happens to be in the DB is not an
#: import source, and this is the guard that keeps it from being treated as one.
DEFAULT_ROOTS: tuple[Path, ...] = (Path("/murrlab3/Images"),)

#: The nuclear channel. Its absence is always a failure; other channels may be
#: legitimately absent (single-colour movies, or a channel given role `skip`).
CORE_SUBDIR = "tif"


@dataclass
class SeriesVerdict:
    name: str
    problems: list[str] = field(default_factory=list)
    planes_found: int = 0
    planes_expected: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass
class SourceVerdict:
    """One deletable (or not) import source."""

    source: Path
    kind: str  # "dir" | "lif"
    acquisitions: list[str] = field(default_factory=list)
    series: list[SeriesVerdict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    bytes_reclaimed: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems and bool(self.series) and all(s.ok for s in self.series)

    @property
    def blockers(self) -> list[str]:
        out = list(self.problems)
        for s in self.series:
            out.extend(f"{s.name}: {p}" for p in s.problems)
        return out


def _lif_path(source_dir: str) -> Path | None:
    """The LIF backing an acquisition, or None if this is a directory import.

    LIF imports record ``<lif>::<series>`` in ``Acquisition.source_dir`` — a
    virtual path naming the LIF *and* the TileScan series inside it. One file
    can therefore back several acquisitions, which is why deletion is decided
    per file rather than per acquisition.
    """
    head = source_dir.split("::", 1)[0]
    return Path(head) if head.lower().endswith(".lif") else None


def _under_roots(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.resolve().relative_to(Path(root).resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _spot_check(staged: Path, series_name: str, n_t: int, n_p: int) -> list[str]:
    """Confirm the staged tree is still there, by name — never by listing.

    Staging already counted what it wrote, so the only thing the filesystem can
    add is "has someone removed it since?". Plane names are deterministic
    (``<series>-t%03d-p%02d.tif``), so the first and last answer that in two
    stats. Listing ``tif/`` instead would cache one dentry per plane — tens of
    thousands per series, which is the pattern that OOM-killed penticton.
    """
    problems = []
    for label, t, p in (("first", 1, 1), ("last", n_t, n_p)):
        f = staged / CORE_SUBDIR / f"{series_name}-t{t:03d}-p{p:02d}.tif"
        try:
            if f.stat().st_size == 0:
                problems.append(f"{label} plane is zero-byte ({f.name})")
        except OSError:
            problems.append(f"{label} plane missing ({f.name})")
    return problems


def verify_series(series: Series, source: Path) -> SeriesVerdict:
    """Everything that must hold before this series' source can be dropped."""
    v = SeriesVerdict(name=series.series_name)

    if not series.image_loc:
        v.problems.append("no image_loc")
        return v
    staged = Path(series.image_loc)
    if not staged.is_dir():
        v.problems.append(f"image_loc missing on disk ({staged})")
        return v
    # If the staged tree lives inside the source, deleting the source deletes
    # the only copy. Legacy series backfilled in place look exactly like this.
    if _under_roots(staged, (source,)) or staged.resolve() == source.resolve():
        v.problems.append("staged tree is inside the source — nothing was copied out")
        return v

    md = series.microscopy
    if md is None:
        v.problems.append("no microscopy metadata parsed")
        return v
    missing = [
        f for f in ("n_timepoints", "planes_per_volume", "voxel_xy_um", "voxel_z_um")
        if getattr(md, f) in (None, 0)
    ]
    if missing:
        v.problems.append(f"metadata incomplete: {', '.join(missing)}")

    # SKIPPED is what `pipeline mark-legacy` writes for series that predate the
    # pipeline — it means staging never ran, not that it succeeded.
    done = [
        r for r in series.runs
        if r.step == "stage_images" and r.status == RunStatus.COMPLETE
    ]
    if not done:
        seen = {str(r.status) for r in series.runs if r.step == "stage_images"}
        v.problems.append(
            f"stage_images not complete (status: {', '.join(sorted(seen))})"
            if seen else "no stage_images run recorded"
        )
        return v

    summary = done[-1].output_summary or {}
    # `planes_skipped` means the destination already existed and was left
    # alone, so a resumed staging run legitimately reports 0 written and a full
    # complement skipped. Only `planes_omitted` (a channel given role `skip`)
    # is genuinely not on disk.
    staged_planes = int(summary.get("planes_written") or 0) + int(
        summary.get("planes_skipped") or 0
    )
    if not staged_planes:
        v.problems.append("stage_images recorded no planes")
        return v
    v.planes_found = staged_planes

    # A movie split across LIF objects is staged as one continuous time course,
    # so the real length is the metadata's count plus whatever was appended.
    # Checking against the metadata alone flags every segmented acquisition.
    if md.n_timepoints and md.planes_per_volume:
        n_t = int(md.n_timepoints) + int(summary.get("appended_timepoints") or 0)
        per_channel = n_t * int(md.planes_per_volume)
        v.planes_expected = per_channel
        # Channels aren't fixed — a single-colour movie has one, and a channel
        # given role `skip` is deliberately absent — so the count is derived
        # rather than assumed, and only its consistency is asserted.
        if staged_planes % per_channel:
            v.problems.append(
                f"staged {staged_planes} planes, not a whole multiple of {per_channel} "
                f"({n_t}t x {md.planes_per_volume}p) — extraction looks truncated"
            )
        else:
            n_ch = staged_planes // per_channel
            if md.channels_per_plane and n_ch > int(md.channels_per_plane):
                v.problems.append(
                    f"staged {n_ch} channels but the source has "
                    f"{md.channels_per_plane}"
                )
            v.problems.extend(
                _spot_check(staged, series.series_name, n_t, int(md.planes_per_volume))
            )
    return v


def _dir_bytes(d: Path) -> int:
    """Size of one import source.

    Only ever called for a source that already passed every check, i.e. one we
    are about to offer to delete. Sizing every candidate would mean walking
    thousands of large TIF trees over NFS, which is the traversal pattern that
    OOM-killed penticton.
    """
    total = 0
    for root, _dirs, files in os.walk(d):
        for f in files:
            try:
                total += os.stat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def plan_source_gc(
    session: Session,
    *,
    older_than: int = 30,
    roots: tuple[Path, ...] = DEFAULT_ROOTS,
    include_lif: bool = True,
) -> list[SourceVerdict]:
    """Group every acquisition by its on-disk source and verify each group.

    A LIF is only proposed once every acquisition extracted from it passes, and
    only when the file's own TileScan list is fully represented in the DB —
    a position that was never imported leaves no DB trace at all, so the file
    has to be asked directly.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=older_than)
    acqs = list(session.execute(select(Acquisition)).scalars())

    groups: dict[Path, list[Acquisition]] = {}
    for acq in acqs:
        if not acq.source_dir:
            continue
        lif = _lif_path(acq.source_dir)
        if lif is not None and not include_lif:
            continue
        key = lif if lif is not None else Path(acq.source_dir)
        groups.setdefault(key, []).append(acq)

    out: list[SourceVerdict] = []
    for source, members in sorted(groups.items()):
        kind = "lif" if source.suffix.lower() == ".lif" else "dir"
        v = SourceVerdict(
            source=source, kind=kind, acquisitions=sorted(a.name for a in members)
        )
        if not source.exists():
            continue  # already reclaimed
        if not _under_roots(source, roots):
            continue  # outside the managed roots — not ours to delete
        for acq in members:
            if acq.staged_at is None:
                v.problems.append(f"{acq.name}: never staged")
            elif acq.staged_at > cutoff:
                v.problems.append(
                    f"{acq.name}: staged {(datetime.now(tz=timezone.utc) - acq.staged_at).days}d "
                    f"ago, under the {older_than}d grace period"
                )
            if not acq.series_list:
                v.problems.append(f"{acq.name}: no series rows")
            for s in acq.series_list:
                v.series.append(verify_series(s, source))
        if kind == "lif":
            v.problems.extend(_lif_coverage_problems(source, members))
        if v.ok:
            try:
                v.bytes_reclaimed = (
                    source.stat().st_size if kind == "lif" else _dir_bytes(source)
                )
            except OSError:
                v.bytes_reclaimed = 0
        out.append(v)
    return out


def _lif_coverage_problems(lif: Path, members: list[Acquisition]) -> list[str]:
    """TileScans present in the file but never imported.

    A position that was never imported leaves no DB trace whatsoever, so the
    container has to be asked directly — this is the check that stops a LIF
    holding six TileScans from being deleted because one of them was staged.

    Staged series are named ``<stem>_L<n>`` and carry no record of the TileScan
    they came from; the LIF-internal name survives only in the ``::`` suffix of
    ``Acquisition.source_dir``, so that is what gets compared.
    """
    try:
        from .lif.extractor import inspect_lif

        infos = inspect_lif(lif)
    except Exception as exc:  # noqa: BLE001 — unreadable file must block, not crash
        return [f"could not read {lif.name} to confirm coverage: {exc}"]

    from .lif.import_flow import appendable_candidates

    imported = {
        acq.source_dir.split("::", 1)[1]
        for acq in members
        if "::" in (acq.source_dir or "")
    }
    # A LIF holds more than the movies: single-timepoint overview snaps, and
    # the `<main>_t<N>` objects holding a late extra volume, which staging
    # already folded into the main time course (hence `appended_timepoints`).
    # Neither is unimported data, and flagging them blocks every LIF.
    consumed = set(imported)
    for name in imported:
        consumed.update(appendable_candidates(infos, name))

    problems: list[str] = []
    missing = [
        i.name
        for i in infos
        if i.name not in consumed
        and any(p.n_timepoints > 1 for p in i.positions)
    ]
    if missing:
        head = ", ".join(missing[:5])
        more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
        problems.append(
            f"{len(missing)} of {len(infos)} TileScan(s) hold un-imported movies: "
            f"{head}{more}"
        )

    # Within the TileScans that were imported, every position must have landed.
    n_positions = sum(len(i.positions) for i in infos if i.name in imported)
    n_staged = sum(len(acq.series_list) for acq in members)
    if n_positions and n_staged != n_positions:
        problems.append(
            f"imported TileScan(s) hold {n_positions} position(s) but only "
            f"{n_staged} series row(s) exist"
        )
    return problems
