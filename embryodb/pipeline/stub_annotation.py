"""Write a StarryNite annotation so AceTree can open a series that has no real
lineage.

AceTree refuses to open an image series without an annotation file. When a
movie is too dim, photobleaches, or drifts out of frame and StarryNite fails
(see :mod:`embryodb.pipeline.sn_recovery`), we still want to *look* at the raw
images. The legacy pipeline handled this by dropping a one-timepoint stub zip
into the series' ``dats/``; this module is the faithful port, with one useful
upgrade: when the MATLAB *detection* output survives (it usually does — only
the C tracer wedges), we build the annotation from the **real per-timepoint
detected nuclei** instead of a single fake one, so you can see where the
55 / 39 / 23 / 13 … detected nuclei actually are before adjusting parameters.

Two modes (chosen automatically by :func:`write_stub_for_series`):

  * **detection** — convert ``tif/<series>_matlabnuclei/matlabnuclei<N>`` (+
    ``matlabdiams<N>``) into one ``nuclei/t<NNN>-nuclei`` per timepoint, with no
    lineage links (each nucleus is an independent dot). The coordinate mapping
    is a verified pass-through against a successfully-traced series:
    ``matlabnuclei`` ``x y z`` → AceTree ``x, y, z.0``; ``matlabdiams`` →
    diameter; name ``Nuc<index>``.
  * **placeholder** — a single fake nucleus (the legacy stub), used only when
    no detection output exists.

Either way it is written as both ``<series>.zip`` and ``<series>-edit.zip``,
with ``dats/<series>.xml`` produced by the shared
:func:`orchestrate.step_write_acetree_config` so the config can't drift from a
normal run. The eventual real fix is to make AceTree (the napari
``acetree_py`` rewrite) not require an annotation at all.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..fsutil import chmod_if_possible, ensure_dir
from ..models import Series
from .annotation_archive import archive_annotations
from .orchestrate import (
    FALLBACK_VOXEL_XY_UM,
    FALLBACK_VOXEL_Z_UM,
    step_write_acetree_config,
)

# AceTree nucleus row field layout (comma-separated):
#   index, valid, predecessor, successor1, successor2, x, y, z, diameter, name,
#   then ten weight/intensity fields. For an un-extracted detection stub the
#   trailing fields are placeholders AceTree ignores for display; field 15 is
#   empty and field 17 is the constant 25000, mirroring real tracer output.
_PLACEHOLDER_TAIL = "561242, 9363, 19774, 2112, , 9363, 25000, 9450, 9450, 0,"
_DETECTION_TAIL = "0, 0, 0, 0, , 0, 25000, 0, 0, 0,"
_DEFAULT_PLANES = 67
_DEFAULT_DIAM = "30"


@dataclass
class StubReport:
    series_name: str
    zip_path: Path
    edit_zip_path: Path
    config_path: Path
    n_timepoints: int
    mode: str = "placeholder"   # "detection" | "placeholder"
    n_nuclei: int = 0           # total detected nuclei written (detection mode)


def _nucleus_row(*, x: int, y: int, z: float, diameter: int, name: str) -> str:
    return (
        f"1, 1, -1, -1, -1, {x}, {y}, {z}, {diameter}, {name}, {_PLACEHOLDER_TAIL}\n"
    )


def _detection_row(idx: int, x: str, y: str, z: str, diameter: str) -> str:
    return (
        f"{idx}, 1, -1, -1, -1, {x}, {y}, {float(z):.1f}, {diameter}, "
        f"Nuc{idx}, {_DETECTION_TAIL} \n"
    )


def write_stub_nuclei_zip(
    zip_path: Path,
    *,
    x: int = 376,
    y: int = 120,
    z: float = 30.0,
    diameter: int = 28,
    name: str = "polar1",
) -> Path:
    """Write a one-timepoint placeholder nuclei zip (``nuclei/t001-nuclei``).

    Standalone (no DB) so it is trivially testable. Returns ``zip_path``.
    """
    ensure_dir(zip_path.parent)
    row = _nucleus_row(x=x, y=y, z=z, diameter=diameter, name=name)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("nuclei/t001-nuclei", row)
    chmod_if_possible(zip_path, 0o664)
    return zip_path


def build_detection_nuclei_zip(
    zip_path: Path, matlabnuclei_dir: Path, n_timepoints: int
) -> int:
    """Write a nuclei zip from real per-timepoint StarryNite detection output.

    One ``nuclei/t<NNN>-nuclei`` per timepoint ``1..n_timepoints``; each detected
    nucleus becomes an unlineaged AceTree row from the paired
    ``matlabnuclei<N>`` (``x y z``) and ``matlabdiams<N>`` (diameter) lines.
    Missing timepoint files are written empty. Returns the total nuclei written.
    """
    ensure_dir(zip_path.parent)
    total = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for tp in range(1, n_timepoints + 1):
            nuc_file = matlabnuclei_dir / f"matlabnuclei{tp}"
            diam_file = matlabnuclei_dir / f"matlabdiams{tp}"
            rows: list[str] = []
            if nuc_file.exists():
                nuc_lines = [
                    ln for ln in nuc_file.read_text(errors="replace").splitlines()
                    if ln.strip()
                ]
                diam_lines = (
                    diam_file.read_text(errors="replace").splitlines()
                    if diam_file.exists() else []
                )
                for i, ln in enumerate(nuc_lines):
                    parts = ln.split()
                    if len(parts) < 3:
                        continue
                    diam = (
                        diam_lines[i].strip()
                        if i < len(diam_lines) and diam_lines[i].strip()
                        else _DEFAULT_DIAM
                    )
                    rows.append(_detection_row(i + 1, parts[0], parts[1], parts[2], diam))
                    total += 1
            zf.writestr(f"nuclei/t{tp:03d}-nuclei", "".join(rows))
    chmod_if_possible(zip_path, 0o664)
    return total


def write_stub_for_series(
    series: Series,
    *,
    n_timepoints: int | None = None,
    planes_per_volume: int | None = None,
    voxel_xy_um: float | None = None,
    voxel_z_um: float | None = None,
    from_detection: bool = True,
) -> StubReport:
    """Write the stub zip(s) + AceTree config for a Series.

    Resolves image_loc / voxel size / plane count / timepoint count from the
    Series and its microscopy metadata, falling back to project defaults when
    absent. When ``from_detection`` and StarryNite detection output exists, the
    zip holds the real per-timepoint detected nuclei; otherwise a single
    placeholder nucleus. Sets ``series.acetree_config`` (via the shared config
    writer). Caller owns the session/commit.
    """
    if not series.image_loc:
        raise ValueError(f"series {series.series_name!r} has no image_loc")
    image_loc = Path(series.image_loc)
    dats = ensure_dir(image_loc / "dats")

    micro = series.microscopy
    if voxel_xy_um is None:
        voxel_xy_um = micro.voxel_xy_um if micro and micro.voxel_xy_um else FALLBACK_VOXEL_XY_UM
    if voxel_z_um is None:
        voxel_z_um = micro.voxel_z_um if micro and micro.voxel_z_um else FALLBACK_VOXEL_Z_UM
    if planes_per_volume is None:
        planes_per_volume = (
            (micro.planes_per_volume if micro and micro.planes_per_volume else None)
            or _DEFAULT_PLANES
        )
    if n_timepoints is None:
        n_timepoints = _infer_timepoints(series, image_loc)

    zip_path = dats / f"{series.series_name}.zip"
    edit_zip_path = dats / f"{series.series_name}-edit.zip"

    # A stub rewrite on a series that already has a real (possibly curated)
    # lineage would otherwise replace it with detections or a single placeholder.
    archive_annotations(
        dats, series.series_name, reason="write_stub_for_series overwriting lineage"
    )

    nuclei_dir = image_loc / "tif" / f"{series.series_name}_matlabnuclei"
    use_detection = (
        from_detection
        and nuclei_dir.is_dir()
        and any(nuclei_dir.glob("matlabnuclei*"))
    )
    if use_detection:
        n_nuclei = build_detection_nuclei_zip(zip_path, nuclei_dir, n_timepoints)
        build_detection_nuclei_zip(edit_zip_path, nuclei_dir, n_timepoints)
        mode = "detection"
    else:
        write_stub_nuclei_zip(zip_path)
        write_stub_nuclei_zip(edit_zip_path)
        mode, n_nuclei = "placeholder", 1

    config_path = step_write_acetree_config(
        series,
        image_loc,
        n_timepoints,
        planes_per_volume,
        voxel_xy_um,
        voxel_z_um,
    )

    return StubReport(
        series_name=series.series_name,
        zip_path=zip_path,
        edit_zip_path=edit_zip_path,
        config_path=config_path,
        n_timepoints=n_timepoints,
        mode=mode,
        n_nuclei=n_nuclei,
    )


def _infer_timepoints(series: Series, image_loc: Path) -> int:
    """Best-effort timepoint count: series.timepts, else count staged TIFs."""
    if series.timepts:
        try:
            n = int(series.timepts)
            if n > 0:
                return n
        except (TypeError, ValueError):
            pass
    tif_dir = image_loc / "tif"
    if tif_dir.is_dir():
        n = sum(1 for _ in tif_dir.glob(f"{series.series_name}-t*-p01.tif"))
        if n > 0:
            return n
    return 1


__all__ = [
    "StubReport",
    "write_stub_nuclei_zip",
    "build_detection_nuclei_zip",
    "write_stub_for_series",
]
