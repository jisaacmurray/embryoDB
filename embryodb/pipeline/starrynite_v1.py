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
  ``effort/predict_effort.py``); see the calibration scope note there.

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
    # The legacy driver builds every output path by string-concatenating
    # `outputdirectory` with a filename, so it MUST end in a separator; without
    # one the lineage zip lands as `<scratch>/out<series>_run.zip` (sibling of
    # `out/`) instead of `<scratch>/out/<series>_run.zip`, where land_lineage /
    # find_lineage_zip look — and the run is wrongly reported as producing none.
    scratch_out_slash = os.path.join(str(scratch_out), "")
    matlab_expr = (
        f"addpath('{release}'); "
        f"run_starrynite('{paramfile}', '{image_first_plane}', '{scratch_out_slash}')"
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
    # The release driver writes tracked nuclei to `<out>/<suffix><series>/nuclei/`
    # (e.g. `out/run<series>/nuclei/`) — one level deeper than the checks above.
    for nested in sorted(glob.glob(os.path.join(str(out), "*", "nuclei"))):
        if glob.glob(os.path.join(nested, "t*-nuclei")):
            return Path(nested)
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


def _render_report(payload: dict) -> str:
    """Human-readable effort report, rendered from the predictor's JSON.

    Mirrors the layout ``predict_effort.py`` prints without ``--json``, so the
    file in ``dats/`` reads the same whether a human or the pipeline produced
    it. Machine consumers should read the sibling ``_sn_effort.json``.
    """
    lines = ["=== predicted curation effort (GT-free, retrained-new-SN) ===",
             f"{'stage':>6} {'N_at':>8} {'t_at':>8} {'effort_pred':>12}"]
    for st in payload.get("stages") or []:
        lines.append(
            f"{st.get('stage',''):>6} {st.get('n_at',0):>8.1f} "
            f"{st.get('t_at',0):>8.1f} {st.get('effort_predicted',0):>12.1f}"
        )
    events = payload.get("effort_events")
    events_txt = "n/a" if events is None else f"{float(events):.0f}"
    lines += [
        "",
        f"full-movie predicted effort = {events_txt} events   ->  TRIAGE: "
        f"{payload.get('triage','n/a')}",
        f"(triage tertiles from corpus: q1={payload.get('triage_q1',0):.0f} "
        f"q2={payload.get('triage_q2',0):.0f} effort-events)",
        f"model: {payload.get('model_version','?')} "
        f"(md5 {str(payload.get('model_md5',''))[:8]})",
    ]
    return "\n".join(lines) + "\n"


def run_effort(
    nuclei_source: Path | str,
    xyres: float,
    zres: float,
    dats_dir: Path | str,
    series_name: str,
) -> dict:
    """Run the GT-free curation-effort predictor on NEW-SN output.

    `nuclei_source` is either a loose tracked-nuclei dir (``tNNN-nuclei`` files)
    or an AceTree lineage ``.zip`` — the release ``predict_effort.py`` accepts
    both and, for a zip, extracts its internal ``nuclei/tNNN-nuclei`` entries to
    a tempdir before running the same featurizer/model. We prefer the landed
    ``dats/<series>-edit.zip`` because it is durable (survives scratch cleanup,
    so it is also backfillable) and decoupled from the driver's scratch layout.

    Runs the predictor in ``--json`` mode so the PER-STAGE predictions survive
    (the printed table alone only yields the full-movie number). Writes the raw
    JSON to ``dats/<series>_sn_effort.json`` and a human report to
    ``dats/<series>_sn_effort.txt``, and returns a structured summary carrying
    ``stages`` for :func:`embryodb.queries.difficulty.upsert_predictions`.
    Failures are reported in the returned dict rather than raised — a missing
    effort estimate must not fail an otherwise good run.

    SCOPE: calibrated on new-SN output. The model exhibits documented negative
    transfer on legacy classic-SN lineages; it is still run on them so every
    series gets a triage signal, but the caller tags those rows with a distinct
    ``model_version`` (``queries.difficulty.OUT_OF_SCOPE_SUFFIX``) so biased
    numbers can never be mistaken for in-scope ones.
    """
    import json
    import subprocess

    script = settings.starrynite_v1_dir / "effort" / "predict_effort.py"
    cmd = [
        str(settings.effort_python),
        str(script),
        str(nuclei_source),
        str(xyres),
        str(zres),
        "--json",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600
        )
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the run
        return {"ran": False, "error": f"{type(exc).__name__}: {exc}"}

    stdout = proc.stdout or ""
    dats = ensure_dir(dats_dir)
    summary: dict = {"ran": False}
    if proc.returncode != 0:
        summary["error"] = (proc.stderr or stdout or "").strip()[-500:]
        return summary
    try:
        payload = json.loads(stdout)
    except ValueError as exc:
        summary["error"] = f"could not parse predictor JSON: {exc}"
        return summary

    for name, text in (
        (f"{series_name}_sn_effort.json", stdout),
        (f"{series_name}_sn_effort.txt", _render_report(payload)),
    ):
        try:
            path = dats / name
            path.write_text(text, encoding="utf-8")
            chmod_if_possible(path, 0o664)
            chgrp_if_possible(path)
        except OSError:
            pass

    summary.update(
        ran=True,
        report_path=str(dats / f"{series_name}_sn_effort.txt"),
        json_path=str(dats / f"{series_name}_sn_effort.json"),
        model_version=payload.get("model_version"),
        effort_events=payload.get("effort_events"),
        triage=payload.get("triage"),
        stages=payload.get("stages") or [],
    )
    return summary


__all__ = [
    "build_newmatlab_paramfile",
    "build_matlab_command",
    "find_lineage_zip",
    "find_nuclei_dir",
    "land_lineage",
    "run_effort",
]
