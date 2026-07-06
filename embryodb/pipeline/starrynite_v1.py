"""Helpers for the NEW all-MATLAB StarryNite (StarryNite/release_v1).

This is the *optional* alternative to the legacy compiled-MATLAB+C StarryNite
that ``matlab_SN_cluster.pl`` drives. It is selected per pipeline run via the
``run_starrynite`` step's ``params.sn_engine == "new"`` (default stays "old", so
this is an opt-in, not a switchover).

These functions are pure/side-effect-scoped so ``subprocess_steps`` owns the
heartbeat/log/DB machinery:

- :func:`build_newmatlab_paramfile` — turn a series' existing ``matlabParams``
  (which already carries the movie-tuned DETECTION block + resolution) into a
  ``newmatlab`` parameter file by appending the release's FIXED tracking block
  (the retrained-model ``load`` + ``trackingparameters.*``). Written to scratch.
- :func:`build_matlab_command` — the headless MATLAB invocation + env.
- :func:`land_lineage` — copy the scratch lineage zip into the series' ``dats/``
  in the SAME slot the legacy pipeline uses (``<series>-edit.zip`` + pristine
  ``<series>.zip``), so downstream steps / AceTree find it identically.
- :func:`run_effort` — the GT-free curation-effort predictor (release
  ``effort/predict_effort.py``). NEW-SN output only; see the scope note there.

Nothing here writes into ``image_loc`` except the final ``dats/`` products; the
run itself happens entirely under ``settings.starrynite_scratch_root``.
"""

from __future__ import annotations

import glob
import os
import re
from pathlib import Path

from ..config import settings
from ..fsutil import chmod_if_possible, chgrp_if_possible, ensure_dir, safe_copy
from ..parsers.matlab_params import load as load_params

# Marker in params_template_newmatlab.txt where the FIXED tracking block begins.
# Everything from here to EOF is the release-managed tracking config (the model
# `load` + all `trackingparameters.*`). We take it verbatim so the block stays
# in sync if the StarryNite agent revises the template.
_TRACKING_MARKER = "% ============ TRACKING"

# Routing flags the newmatlab template sets that our legacy matlabParams omits
# (legacy detection defaults them implicitly). key -> full statement.
_EXTRA_FLAGS: dict[str, str] = {
    "splitstack": "splitstack=false;",
    "rednuclei": "rednuclei=false;",
    "flipstack": "flipstack=false;",
    "zeropadding": "zeropadding=true;",
}


def _template_tracking_block(model_name: str) -> str:
    """Read the release template and return its FIXED tracking block, with the
    model filename swapped to `model_name`."""
    tpl = (settings.starrynite_v1_dir / "params_template_newmatlab.txt").read_text(
        encoding="utf-8"
    )
    idx = tpl.find(_TRACKING_MARKER)
    if idx == -1:
        raise RuntimeError(
            f"tracking-block marker {_TRACKING_MARKER!r} not found in the "
            "release_v1 param template; cannot build a new-SN parameter file"
        )
    block = tpl[idx:]
    # Swap `load 'MurrayTrackingModel_XXXX.mat'` -> the configured model name.
    block = re.sub(
        r"load\s+'MurrayTrackingModel[^']*'",
        f"load '{model_name}'",
        block,
        count=1,
    )
    return block


def build_newmatlab_paramfile(
    matlab_params_path: Path | str,
    out_path: Path | str,
    *,
    model_name: str | None = None,
) -> Path:
    """Write a ``newmatlab`` parameter file to `out_path` from the series'
    existing legacy ``matlabParams``.

    The legacy file already defines resolution + the full DETECTION block
    (``parameters.staging/intensitythreshold/rangethreshold/...``), which the
    new all-MATLAB detector reuses unchanged. We only (1) add the few routing
    flags the newmatlab template carries that legacy omits and (2) append the
    fixed retrained-tracking block. `out_path` is scratch — it is a plain write
    (not a series file), so it does not route through ``safe_write``.
    """
    model_name = model_name or settings.starrynite_tracking_model
    base = Path(matlab_params_path).read_text(encoding="utf-8")
    present = load_params(Path(matlab_params_path)).values
    extra = "\n".join(
        stmt for key, stmt in _EXTRA_FLAGS.items() if key not in present
    )
    tracking = _template_tracking_block(model_name)

    parts = [base.rstrip()]
    if extra:
        parts.append("% --- new-SN routing flags (added by embryoDB) ---\n" + extra)
    parts.append(tracking.rstrip() + "\n")
    text = "\n\n".join(parts)
    out = Path(out_path)
    ensure_dir(out.parent)
    out.write_text(text, encoding="utf-8")
    return out


def build_matlab_command(
    paramfile: Path | str,
    image_first_plane: Path | str,
    scratch_out: Path | str,
) -> tuple[list[str], dict[str, str]]:
    """Build the headless MATLAB command + env that runs ``run_starrynite.m``
    on one series. `image_first_plane` is ``<image_loc>/tif/<series>-t001-p01.tif``;
    `scratch_out` is the (scratch) output dir the lineage lands in."""
    release = settings.starrynite_v1_dir
    matlab_expr = (
        f"addpath('{release}'); "
        f"run_starrynite('{paramfile}', '{image_first_plane}', '{scratch_out}')"
    )
    cmd = [
        str(settings.matlab_command),
        "-nodisplay",
        "-nosplash",
        "-batch",
        matlab_expr,
    ]
    env = os.environ.copy()
    # A -v7.3 save fails on GPFS/NFS without this (release README requirement).
    env["HDF5_USE_FILE_LOCKING"] = "FALSE"
    return cmd, env


def find_lineage_zip(scratch_out: Path | str) -> Path | None:
    """Locate the AceTree lineage zip the new SN produced in `scratch_out`."""
    hits = sorted(glob.glob(os.path.join(str(scratch_out), "*.zip")))
    return Path(hits[0]) if hits else None


def find_nuclei_dir(scratch_out: Path | str) -> Path | None:
    """Locate the tracked-nuclei dir (``nuclei/`` of tNNN-nuclei files) that the
    effort predictor consumes. Falls back to the run dir if the loose
    ``tNNN-nuclei`` files sit directly in `scratch_out`."""
    out = Path(scratch_out)
    cand = out / "nuclei"
    if cand.is_dir() and glob.glob(str(cand / "t*-nuclei")):
        return cand
    if glob.glob(str(out / "t*-nuclei")):
        return out
    return None


def land_lineage(scratch_out: Path | str, dats_dir: Path | str, series_name: str) -> dict:
    """Copy the new-SN lineage zip into the series' ``dats/`` in the SAME slot
    the legacy pipeline writes: ``<series>-edit.zip`` (the AceTree curation
    target the config points at) plus a pristine ``<series>.zip``.

    Returns a small summary dict. Raises FileNotFoundError if no zip was
    produced (the caller turns that into a FAILED run)."""
    zip_path = find_lineage_zip(scratch_out)
    if zip_path is None:
        raise FileNotFoundError(
            f"new StarryNite produced no lineage .zip in {scratch_out}"
        )
    dats = ensure_dir(dats_dir)
    edit_zip = dats / f"{series_name}-edit.zip"
    pristine = dats / f"{series_name}.zip"
    safe_copy(zip_path, edit_zip)
    safe_copy(zip_path, pristine)
    return {"edit_zip": str(edit_zip), "pristine_zip": str(pristine)}


_TRIAGE_RE = re.compile(
    r"full-movie predicted effort\s*=\s*([\d.]+)\s*events.*?TRIAGE:\s*(\S+)",
    re.IGNORECASE | re.DOTALL,
)


def run_effort(
    nuclei_dir: Path | str,
    xyres: float,
    zres: float,
    dats_dir: Path | str,
    series_name: str,
) -> dict:
    """Run the GT-free curation-effort predictor on a NEW-SN nuclei dir.

    Writes the full predictor stdout to ``dats/<series>_sn_effort.txt`` and
    returns a structured summary (triage bucket + full-movie effort +
    ``report_path``). Failures are reported in the returned dict rather than
    raised — a missing effort estimate must not fail an otherwise good run.

    SCOPE: valid ONLY on new-SN output. The model exhibits documented negative
    transfer on legacy classic-SN lineages, so callers gate this on
    ``sn_engine == "new"``. A future generalized estimator for legacy movies is
    a planned extension (see the caller's placeholder).
    """
    import subprocess

    script = settings.starrynite_v1_dir / "effort" / "predict_effort.py"
    cmd = [
        str(settings.effort_python),
        str(script),
        str(nuclei_dir),
        str(xyres),
        str(zres),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600
        )
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the run
        return {"ran": False, "error": f"{type(exc).__name__}: {exc}"}

    stdout = proc.stdout or ""
    report_path = ensure_dir(dats_dir) / f"{series_name}_sn_effort.txt"
    try:
        report_path.write_text(
            (stdout or "") + ("\n[stderr]\n" + proc.stderr if proc.stderr else ""),
            encoding="utf-8",
        )
        chmod_if_possible(report_path, 0o664)
        chgrp_if_possible(report_path)
    except OSError:
        pass

    summary: dict = {"ran": proc.returncode == 0, "report_path": str(report_path)}
    if proc.returncode != 0:
        summary["error"] = (proc.stderr or stdout or "").strip()[-500:]
        return summary
    m = _TRIAGE_RE.search(stdout)
    if m:
        summary["effort_events"] = float(m.group(1))
        summary["triage"] = m.group(2)
    return summary


__all__ = [
    "build_newmatlab_paramfile",
    "build_matlab_command",
    "find_lineage_zip",
    "find_nuclei_dir",
    "land_lineage",
    "run_effort",
]
