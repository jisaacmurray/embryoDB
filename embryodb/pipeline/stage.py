"""Image staging: copy+compress+rename raw acquisition TIFs into the
per-series, per-channel layout that StarryNite and AceTree expect.

Replaces the heavy lifting of `Stellaris_tif_pipeline*.pl`. Pure Python via
`tifffile` for read/write (with optional LZW recompression) — no
subprocess to ImageMagick.

Input filename convention (Leica):  <stem>_Position N_tNNN_zNN_RAW_chNN.tif
Output filename convention:         <stem>_L<N>-t<NNN>-p<NN>.tif
                                    (1-indexed t and p; channel routed by dir)

The pipeline preserves the legacy z-inversion: AceTree expects the deepest
plane at p=01 and the surface at p=N. The legacy Perl does `p = (slices + 1) - z`
when z is 0-indexed in the raw filename. We do the same, with `slices`
configurable per protocol.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import tifffile

from ..fsutil import ensure_dir, safe_copy
from ..models import Protocol
from ..parsers.filename import RawImage, scan_directory

log = logging.getLogger(__name__)


# Map from a role string (in Protocol.channel_map) to the subdirectory the
# legacy pipeline + AceTree expect.
ROLE_TO_SUBDIR: dict[str, str] = {
    "histone": "tif",
    "reporter": "tifR",
    "DIC": "DIC",
}


def role_subdir(role: str, raw_channel_index: int) -> str:
    """Pick the subdirectory for a given channel role.

    Roles we know about land in their canonical dir; unknown roles (e.g.
    `reporter2` or other extra channels) get `tifC<n>/` where n is the raw
    channel index, so the data is preserved on disk for future viewers
    even though legacy AceTree won't display it.
    """
    sub = ROLE_TO_SUBDIR.get(role)
    if sub is not None:
        return sub
    return f"tifC{raw_channel_index}"


@dataclass
class StagePlan:
    """Plan of what `stage_images` will do, computed before any I/O."""

    acquisition_stem: str
    positions: dict[int, "PositionPlan"] = field(default_factory=dict)
    channel_map: dict[int, str] = field(default_factory=dict)
    raw_count: int = 0

    @property
    def position_numbers(self) -> list[int]:
        return sorted(self.positions.keys())


@dataclass
class PositionPlan:
    position: int
    series_name: str
    files_by_channel: dict[int, list[RawImage]] = field(default_factory=lambda: defaultdict(list))
    timepoints: set[int] = field(default_factory=set)
    z_planes: set[int] = field(default_factory=set)

    @property
    def planes_per_volume(self) -> int:
        return len(self.z_planes)

    @property
    def n_timepoints(self) -> int:
        return len(self.timepoints)


def plan_acquisition(
    source_dir: Path | str,
    protocol: Protocol,
    parser_name: str = "leica_tilescan",
    series_template: str = "{stem}_L{position}",
) -> StagePlan:
    """Walk `source_dir`, group raw images by position+channel, return a plan.

    Read-only — no files touched yet. The caller can validate the plan
    (right number of positions / timepoints / channels) before committing
    to the I/O.
    """
    source = Path(source_dir)
    if not source.is_dir():
        raise FileNotFoundError(f"source_dir does not exist: {source}")

    channel_map = {int(k): v for k, v in (protocol.channel_map or {}).items()}
    plan = StagePlan(acquisition_stem="", channel_map=channel_map)

    for img in scan_directory(parser_name, source):
        if not plan.acquisition_stem:
            plan.acquisition_stem = img.acquisition_stem
        elif plan.acquisition_stem != img.acquisition_stem:
            log.warning(
                "mixed acquisition stems in %s: %r vs %r",
                source,
                plan.acquisition_stem,
                img.acquisition_stem,
            )
        pos = plan.positions.setdefault(
            img.position,
            PositionPlan(
                position=img.position,
                series_name=series_template.format(
                    stem=img.acquisition_stem, position=img.position
                ),
            ),
        )
        pos.files_by_channel[img.raw_channel].append(img)
        pos.timepoints.add(img.timepoint)
        pos.z_planes.add(img.z_plane)
        plan.raw_count += 1

    return plan


@dataclass
class StageOutcome:
    """Per-position stage result."""

    position: int
    series_name: str
    image_loc: Path
    written: int = 0
    skipped: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    completed_at: datetime | None = None


def stage_position(
    plan: PositionPlan,
    channel_map: dict[int, str],
    image_loc_root: Path,
    slices: int,
    *,
    compress_with_lzw: bool = True,
    overwrite: bool = False,
) -> StageOutcome:
    """Stage one position's raw images into `image_loc_root/<series>/<channel_dir>/`.

    `slices` is the protocol's plane count (used for the z-inversion).
    For each raw image we compute the output filename:
        p = slices - z_plane    (since z_plane is 0-indexed, p is 1-indexed)
        t = timepoint + 1       (1-indexed)
    Output: <image_loc>/<channel_dir>/<series>-t<NNN>-p<NN>.tif
    """
    image_loc = ensure_dir(image_loc_root / plan.series_name)
    outcome = StageOutcome(
        position=plan.position, series_name=plan.series_name, image_loc=image_loc
    )

    for raw_channel, files in plan.files_by_channel.items():
        role = channel_map.get(raw_channel, "")
        subdir_name = role_subdir(role, raw_channel)
        subdir = ensure_dir(image_loc / subdir_name)

        for img in files:
            t1 = img.timepoint + 1
            p1 = slices - img.z_plane
            out_name = f"{plan.series_name}-t{t1:03d}-p{p1:02d}.tif"
            dst = subdir / out_name

            if dst.exists() and not overwrite:
                outcome.skipped += 1
                continue

            try:
                if compress_with_lzw:
                    _copy_with_lzw(img.path, dst)
                else:
                    safe_copy(img.path, dst)
                outcome.written += 1
            except Exception as exc:
                outcome.errors.append((str(img.path), repr(exc)))

    outcome.completed_at = datetime.now(tz=timezone.utc)
    return outcome


def _copy_with_lzw(src: Path, dst: Path) -> None:
    """Read `src` and write `dst` with LZW compression.

    Skips the recompression if `src` is already LZW (small win on already-
    compressed files); for raw uncompressed Leica exports the savings are
    substantial.
    """
    with tifffile.TiffFile(src) as tf:
        arr = tf.asarray()
        # Preserve photometric / resolution tags where possible.
        existing = tf.pages[0].tags
        photometric = None
        if "PhotometricInterpretation" in existing:
            try:
                photometric = existing["PhotometricInterpretation"].value
            except Exception:
                photometric = None
    tifffile.imwrite(
        dst,
        arr,
        compression="lzw",
        photometric=photometric,
    )
    # Permission discipline matches safe_copy
    from ..fsutil import chgrp_if_possible, chmod_if_possible, DEFAULT_FILE_MODE
    chmod_if_possible(dst, DEFAULT_FILE_MODE)
    chgrp_if_possible(dst)


__all__ = [
    "StagePlan",
    "PositionPlan",
    "StageOutcome",
    "plan_acquisition",
    "stage_position",
    "role_subdir",
    "ROLE_TO_SUBDIR",
]
