"""Helpers for the PRODUCTION StarryNite (``sn_production_driver.m``).

Selected per pipeline run via ``params.sn_engine == "prod"``. This is a THIRD
engine, not a revision of ``"new"`` — the two share almost nothing:

===================  ==========================  ================================
                     ``new`` (release_v1)        ``prod``
===================  ==========================  ================================
entry point          ``run_starrynite.m``        ``sn_production_driver.m``
stages               detect + track              detect -> nucleus filter -> track
nucleus filter       none                        sklearn ``nucleus_filter.pkl``
tracking model       ``MurrayTrackingModel*``    ``assets/tracking_model.mat``
tracking parameters  from the param file         from a frozen template
output               AceTree lineage ``.zip``    loose ``nuclei/tNNN-nuclei``
code tree            frozen release              **active dev tree**
===================  ==========================  ================================

**The dev tree is deliberately live** (jmurr, 2026-07-29): the driver resolves
its ``.m`` code from ``new_tools/StarryNite``, so tracker work reaches production
without a release step. The price is that a lineage's behaviour depends on the
state of that tree at run time — a mutable dependency the ``new`` engine
deliberately avoids. :func:`capture_provenance` is what makes that tractable:
every run records the tree's git HEAD, its dirty files, and digests of the
driver + assets, so two prod lineages can be compared on what actually produced
them instead of on when they ran. When a prod result surprises you, read the
run's ``output_summary["provenance"]`` first.

Nothing here writes into ``image_loc`` except the final ``dats/`` products; the
run itself happens entirely under ``settings.starrynite_scratch_root``.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import zipfile
from pathlib import Path

from ..config import settings
from ..fsutil import ensure_dir, safe_copy
from .annotation_archive import archive_annotations

# The one ACTIVE assignment in a matlabParams file. Commented history lines
# start with '%', so anchoring at the start of the line skips them.
_THRESHOLD_RE = re.compile(
    r"^parameters\.intensitythreshold\s*=\s*\[([^\]]*)\]\s*;", re.MULTILINE
)

# Files big enough that hashing them over NFS costs more than it's worth
# (tracking_model.mat is ~2 GB); recorded by size+mtime instead.
_HASH_SIZE_LIMIT = 64 * 1024 * 1024


def assets_dir() -> Path:
    return Path(settings.starrynite_prod_assets or settings.starrynite_prod_dir / "assets")


def driver_path() -> Path:
    return Path(settings.starrynite_prod_dir) / "sn_production_driver.m"


def dev_repo() -> Path | None:
    """The StarryNite tree the driver ``addpath``s, read out of the driver itself.

    The path is baked into ``sn_production_driver.m`` as a ``repo=`` literal
    rather than passed in, so parsing is the only way to record what a run
    actually loaded without editing the StarryNite agent's file.
    """
    try:
        text = driver_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"^\s*repo\s*=\s*'([^']+)'", text, re.MULTILINE)
    return Path(m.group(1)) if m else None


def build_prod_paramfile(
    matlab_params_path: Path | str,
    out_path: Path | str,
    *,
    uniform_threshold: float | None = -1.0,
) -> Path:
    """Write the series' ``matlabParams`` to `out_path`, optionally flattening
    ``parameters.intensitythreshold`` to a single value across all staging bands.

    The driver consumes a series' existing matlabParams directly — it already
    carries the movie-tuned detection block, resolutions, downsampling and ROI —
    so this copies rather than regenerates. The band count is taken from the
    file's own vector, not assumed, since the corpus uses more than one staging
    vector.

    `uniform_threshold` defaults to the sentinel ``-1.0`` meaning "use
    ``settings.starrynite_prod_uniform_threshold``"; pass ``None`` to keep the
    file's own values. Raises ValueError if the file has no active threshold
    line to rewrite — silently running unflattened would produce a lineage that
    looks like the variant but isn't.
    """
    if uniform_threshold == -1.0:
        uniform_threshold = settings.starrynite_prod_uniform_threshold

    text = Path(matlab_params_path).read_text(encoding="utf-8")
    if uniform_threshold is not None:
        matches = _THRESHOLD_RE.findall(text)
        if len(matches) != 1:
            raise ValueError(
                f"{matlab_params_path}: expected exactly 1 active "
                f"parameters.intensitythreshold line, found {len(matches)}"
            )
        nbands = len([p for p in matches[0].split(",") if p.strip()])
        vector = ",".join([f"{uniform_threshold:g}"] * nbands)
        text = _THRESHOLD_RE.sub(
            f"parameters.intensitythreshold=[{vector}];", text, count=1
        )

    out = Path(out_path)
    ensure_dir(out.parent)
    out.write_text(text, encoding="utf-8")
    return out


def build_matlab_command(
    paramfile: Path | str,
    image_first_plane: Path | str,
    out_dir: Path | str,
    workdir: Path | str,
) -> tuple[list[str], dict[str, str]]:
    """Build the headless MATLAB command + env that runs the production driver.

    `out_dir` receives the ``nuclei/`` lineage; `workdir` holds the driver's
    intermediates (``eseq_prod.mat``, the rich feature table, the keep mask).
    Both are scratch.
    """
    matlab_expr = (
        f"addpath('{Path(settings.starrynite_prod_dir)}'); "
        f"sn_production_driver('{paramfile}', '{image_first_plane}', '{out_dir}', "
        f"'workdir', '{workdir}', "
        f"'assetdir', '{assets_dir()}', "
        f"'stagelo', {int(settings.starrynite_prod_stagelo)}, "
        f"'iscale', {float(settings.starrynite_prod_iscale)!r}, "
        f"'pyexe', '{settings.effort_python}')"
    )
    cmd = [
        str(settings.matlab_command),
        "-nodisplay",
        "-nosplash",
        "-batch",
        matlab_expr,
    ]
    env = os.environ.copy()
    # A -v7.3 save fails on GPFS/NFS without this.
    env["HDF5_USE_FILE_LOCKING"] = "FALSE"
    return cmd, env


def _digest(path: Path) -> str:
    try:
        st = path.stat()
    except OSError as exc:
        return f"<unavailable: {exc.strerror}>"
    if st.st_size > _HASH_SIZE_LIMIT:
        return f"size={st.st_size} mtime={int(st.st_mtime)}"
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError as exc:
        return f"<unreadable: {exc.strerror}>"
    return h.hexdigest()


def _git(repo: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:  # noqa: BLE001 — provenance must never fail a run
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def capture_provenance() -> dict:
    """Snapshot what this run is about to execute.

    The dev tree is intentionally mutable, so this is the only durable record of
    which code produced a given lineage. ``dirty`` listing anything means the
    tree carried uncommitted edits at run time and the git HEAD alone does NOT
    identify the code — that combination bit us once already, when a tracker
    file changed overnight between two halves of an A/B.

    Never raises; an unavailable field is recorded as such.
    """
    prov: dict = {
        "driver": str(driver_path()),
        "driver_sha256": _digest(driver_path()),
        "assets": {},
        "iscale": float(settings.starrynite_prod_iscale),
        "stagelo": int(settings.starrynite_prod_stagelo),
        "uniform_threshold": settings.starrynite_prod_uniform_threshold,
    }
    adir = assets_dir()
    for name in ("nucleus_filter.pkl", "tracking_model.mat", "tracking_template.mat"):
        prov["assets"][name] = _digest(adir / name)

    repo = dev_repo()
    if repo is None:
        prov["dev_repo"] = "<not found in driver>"
        return prov
    prov["dev_repo"] = str(repo)
    head = _git(repo, "rev-parse", "HEAD")
    if head:
        prov["dev_head"] = head
        prov["dev_head_subject"] = _git(repo, "log", "-1", "--format=%s") or ""
    # --untracked-files defaults to `normal`, which collapses an untracked dir
    # to one entry. Never pass -uall here: the tree sits on NFS.
    status = _git(repo, "status", "--porcelain")
    if status is not None:
        prov["dev_dirty"] = [ln.strip() for ln in status.splitlines() if ln.strip()]
    return prov


def find_nuclei_dir(out_dir: Path | str) -> Path | None:
    """Locate the ``tNNN-nuclei`` dir the production driver wrote."""
    out = Path(out_dir)
    for cand in (out / "nuclei", out):
        if cand.is_dir() and any(cand.glob("t*-nuclei")):
            return cand
    return None


def _nuclei_files(nuclei_dir: Path) -> list[Path]:
    return sorted(
        nuclei_dir.glob("t*-nuclei"),
        key=lambda p: int(re.sub(r"\D", "", p.name) or 0),
    )


def build_lineage_zip(
    nuclei_dir: Path | str,
    zip_path: Path | str,
    paramfile: Path | str | None = None,
) -> int:
    """Pack loose ``tNNN-nuclei`` files into an AceTree-openable lineage zip.

    Unlike the ``new`` engine the production driver emits no zip, so we build
    one. The parameter file actually used is stored alongside the nuclei so a
    landed lineage carries the detection settings that produced it.

    Returns the number of timepoints packed.
    """
    ndir = Path(nuclei_dir)
    files = _nuclei_files(ndir)
    if not files:
        raise FileNotFoundError(f"no t*-nuclei files in {ndir}")
    out = Path(zip_path)
    ensure_dir(out.parent)
    tmp = out.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f"nuclei/{f.name}")
        if paramfile is not None and Path(paramfile).exists():
            z.write(paramfile, "parameters/matlabParams")
    os.replace(tmp, out)
    return len(files)


def land_lineage(
    out_dir: Path | str,
    dats_dir: Path | str,
    series_name: str,
    *,
    paramfile: Path | str | None = None,
) -> dict:
    """Build the lineage zip from the driver's nuclei output and copy it into
    the series' ``dats/`` in the SAME slot every other engine uses:
    ``<series>-edit.zip`` (AceTree's curation target) plus a pristine
    ``<series>.zip``.

    Any existing zips are archived first — ``-edit.zip`` is user data, and
    rebuilding it from tracker output destroys curation silently.
    """
    nuclei = find_nuclei_dir(out_dir)
    if nuclei is None:
        raise FileNotFoundError(
            f"production StarryNite produced no t*-nuclei under {out_dir}"
        )
    staged = Path(out_dir) / f"{series_name}.zip"
    ntp = build_lineage_zip(nuclei, staged, paramfile)

    dats = ensure_dir(dats_dir)
    archived = archive_annotations(
        dats, series_name, reason="run_starrynite (sn_engine=prod) landing lineage"
    )
    edit_zip = dats / f"{series_name}-edit.zip"
    pristine = dats / f"{series_name}.zip"
    safe_copy(staged, edit_zip)
    safe_copy(staged, pristine)
    out = {
        "edit_zip": str(edit_zip),
        "pristine_zip": str(pristine),
        "timepoints": ntp,
    }
    if archived:
        out["archived"] = archived
    return out


__all__ = [
    "assets_dir",
    "driver_path",
    "dev_repo",
    "build_prod_paramfile",
    "build_matlab_command",
    "capture_provenance",
    "find_nuclei_dir",
    "build_lineage_zip",
    "land_lineage",
]
