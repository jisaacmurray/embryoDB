"""Archive existing lineage zips before a pipeline step overwrites them.

``dats/<series>-edit.zip`` is the file AceTree saves curation into, and
``dats/<series>.zip`` is the pristine tracker lineage. Several pipeline steps
legitimately rewrite both -- re-running StarryNite, re-writing a detection stub
-- and until now they did so with no way back. A re-pipeline on a curated series
therefore destroyed the curation silently.

The zips are small (~1 MB for a 240-timepoint movie) relative to the image data
beside them, so keeping copies is cheap insurance. Before any step overwrites
them, :func:`archive_annotations` copies whatever is already there into
``dats/archived/<UTC timestamp>/`` with a manifest recording why.

COPY, not move: the caller's overwrite semantics stay exactly as they were, and
steps whose writer is an external process (the legacy ``matlab_SN_cluster.pl``,
which writes the zips itself) cannot be confused by a file vanishing underneath
them. That makes this safe to call unconditionally before a step runs.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from ..config import settings
from ..fsutil import ensure_dir, safe_copy

ARCHIVE_DIRNAME = "archived"


def annotation_zips(dats_dir: Path | str, series_name: str) -> list[Path]:
    """The lineage zips a pipeline step may overwrite, newest-slot first."""
    dats = Path(dats_dir)
    return [
        dats / f"{series_name}-edit.zip",
        dats / f"{series_name}.zip",
    ]


def archive_annotations(
    dats_dir: Path | str,
    series_name: str,
    *,
    reason: str,
    enabled: bool | None = None,
) -> list[str]:
    """Copy existing lineage zips into ``dats/archived/<timestamp>/``.

    Returns the archived destination paths (empty when nothing existed, or when
    archiving is disabled). Never raises: an archive failure must not fail an
    otherwise good pipeline step, so problems are swallowed and reported by the
    empty return. `reason` is recorded in the manifest so a later reader can tell
    a StarryNite rerun from a stub rewrite.
    """
    if enabled is None:
        enabled = settings.archive_annotations
    if not enabled:
        return []

    present = [p for p in annotation_zips(dats_dir, series_name) if p.is_file()]
    if not present:
        return []

    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_dir = Path(dats_dir) / ARCHIVE_DIRNAME / stamp
    archived: list[str] = []
    try:
        ensure_dir(dest_dir)
        lines = [f"archived: {stamp}", f"reason: {reason}", ""]
        for src in present:
            dst = safe_copy(src, dest_dir / src.name)
            mtime = _dt.datetime.fromtimestamp(
                src.stat().st_mtime, _dt.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            lines.append(f"{src.name}  size={src.stat().st_size}  mtime={mtime}")
            archived.append(str(dst))
        (dest_dir / "MANIFEST.txt").write_text("\n".join(lines) + "\n")
    except OSError:
        return archived

    _prune(Path(dats_dir), settings.archive_annotations_keep)
    return archived


def _prune(dats_dir: Path, keep: int) -> None:
    """Drop all but the newest `keep` archive generations. 0 keeps everything."""
    if keep <= 0:
        return
    root = dats_dir / ARCHIVE_DIRNAME
    if not root.is_dir():
        return
    try:
        gens = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name)
    except OSError:
        return
    for stale in gens[:-keep]:
        for f in stale.iterdir():
            try:
                f.unlink()
            except OSError:
                return
        try:
            stale.rmdir()
        except OSError:
            return


__all__ = ["ARCHIVE_DIRNAME", "annotation_zips", "archive_annotations"]
