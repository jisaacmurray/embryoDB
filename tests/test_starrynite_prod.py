"""Tests for the production StarryNite engine (sn_engine=prod)."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

from embryodb.config import settings
from embryodb.pipeline import starrynite_prod as snp

SHIPPED_PARAMS = """\
% matlabParams for a test series
xyres=0.16;
zres=1.0;
parameters.staging=[102,181,251,351,500];
% parameters.intensitythreshold=[0.004,0.02,0.008,0.008,0.008,0.008];
parameters.intensitythreshold=[0.004,0.0075,0.008,0.008,0.008,0.008];
parameters.rangethreshold=[0.6,0.6,0.6,0.6,0.6,0.6];
end_time=240;
"""


@pytest.fixture
def params_file(tmp_path: Path) -> Path:
    p = tmp_path / "matlabParams"
    p.write_text(SHIPPED_PARAMS, encoding="utf-8")
    return p


def _threshold_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if "intensitythreshold" in ln]


def test_flattens_all_bands(params_file, tmp_path):
    out = snp.build_prod_paramfile(
        params_file, tmp_path / "out", uniform_threshold=0.004
    )
    text = out.read_text()
    assert "parameters.intensitythreshold=[0.004,0.004,0.004,0.004,0.004,0.004];" in text


def test_leaves_commented_history_alone(params_file, tmp_path):
    """A '%'-prefixed prior setting is history, not configuration."""
    out = snp.build_prod_paramfile(
        params_file, tmp_path / "out", uniform_threshold=0.004
    )
    lines = _threshold_lines(out.read_text())
    commented = [ln for ln in lines if ln.strip().startswith("%")]
    assert commented == ["% parameters.intensitythreshold=[0.004,0.02,0.008,0.008,0.008,0.008];"]


def test_band_count_follows_the_file(tmp_path):
    """Band count is read from the file's own vector -- the corpus uses more
    than one staging vector, so a hardcoded 6 would silently truncate."""
    p = tmp_path / "matlabParams"
    p.write_text("parameters.intensitythreshold=[0.004,0.0075,0.008];\n", encoding="utf-8")
    out = snp.build_prod_paramfile(p, tmp_path / "out", uniform_threshold=0.004)
    assert "parameters.intensitythreshold=[0.004,0.004,0.004];" in out.read_text()


def test_none_keeps_original_values(params_file, tmp_path):
    out = snp.build_prod_paramfile(params_file, tmp_path / "out", uniform_threshold=None)
    assert out.read_text() == SHIPPED_PARAMS


def test_refuses_when_no_active_threshold_line(tmp_path):
    """Silently running unflattened would yield a lineage that looks like the
    variant but isn't -- exactly the kind of confusion the A/B can't survive."""
    p = tmp_path / "matlabParams"
    p.write_text("% parameters.intensitythreshold=[0.004,0.0075];\nxyres=0.16;\n")
    with pytest.raises(ValueError, match="expected exactly 1"):
        snp.build_prod_paramfile(p, tmp_path / "out", uniform_threshold=0.004)


def test_default_threshold_comes_from_settings(params_file, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "starrynite_prod_uniform_threshold", 0.006)
    out = snp.build_prod_paramfile(params_file, tmp_path / "out")
    assert "parameters.intensitythreshold=[0.006,0.006,0.006,0.006,0.006,0.006];" in out.read_text()


def test_matlab_command_matches_the_validated_ad_hoc_invocation(tmp_path, monkeypatch):
    """The pilot runs were launched by hand; the pipeline must issue the same
    call or it is not the same engine."""
    monkeypatch.setattr(settings, "starrynite_prod_dir", Path("/opt/sn/release_prod"))
    monkeypatch.setattr(settings, "starrynite_prod_assets", None)
    monkeypatch.setattr(settings, "starrynite_prod_iscale", 1.0)
    monkeypatch.setattr(settings, "starrynite_prod_stagelo", 3)
    monkeypatch.setattr(settings, "matlab_command", Path("/usr/bin/matlab"))

    cmd, env = snp.build_matlab_command(
        "/scratch/matlabParams", "/img/s/tif/s-t001-p01.tif", "/scratch/out", "/scratch"
    )
    expr = cmd[-1]
    assert cmd[0] == "/usr/bin/matlab"
    assert "-batch" in cmd
    assert "addpath('/opt/sn/release_prod')" in expr
    assert "sn_production_driver('/scratch/matlabParams', '/img/s/tif/s-t001-p01.tif', '/scratch/out'" in expr
    assert "'assetdir', '/opt/sn/release_prod/assets'" in expr
    assert "'stagelo', 3" in expr
    assert "'iscale', 1.0" in expr
    assert "'workdir', '/scratch'" in expr
    # A -v7.3 save fails on GPFS/NFS without this.
    assert env["HDF5_USE_FILE_LOCKING"] == "FALSE"


def test_assets_dir_override(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "starrynite_prod_dir", tmp_path / "drv")
    monkeypatch.setattr(settings, "starrynite_prod_assets", tmp_path / "elsewhere")
    assert snp.assets_dir() == tmp_path / "elsewhere"


def test_dev_repo_parsed_from_driver(tmp_path, monkeypatch):
    """The repo path is baked into the .m as a literal, so parsing it is the
    only way to record what a run loaded without editing the SN agent's file."""
    monkeypatch.setattr(settings, "starrynite_prod_dir", tmp_path)
    (tmp_path / "sn_production_driver.m").write_text(
        "thisdir=fileparts(mfilename('fullpath'));\n"
        "repo='/some/where/StarryNite';\n"
        "addpath(thisdir);\n"
    )
    assert snp.dev_repo() == Path("/some/where/StarryNite")


def test_dev_repo_missing_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "starrynite_prod_dir", tmp_path)
    assert snp.dev_repo() is None


def test_capture_provenance_never_raises_on_missing_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "starrynite_prod_dir", tmp_path / "nope")
    monkeypatch.setattr(settings, "starrynite_prod_assets", None)
    prov = snp.capture_provenance()
    assert prov["dev_repo"] == "<not found in driver>"
    assert "unavailable" in prov["driver_sha256"]
    assert set(prov["assets"]) == {
        "nucleus_filter.pkl", "tracking_model.mat", "tracking_template.mat"
    }


def test_capture_provenance_records_git_state(tmp_path, monkeypatch):
    import subprocess

    repo = tmp_path / "StarryNite"
    (repo / "distribution_lineaging").mkdir(parents=True)
    tracked = repo / "distribution_lineaging" / "tracker.m"
    tracked.write_text("v1\n")
    for args in (
        ["init", "-q"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "seed"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True)
    tracked.write_text("v2 -- uncommitted tracker edit\n")

    drv = tmp_path / "drv"
    drv.mkdir()
    (drv / "sn_production_driver.m").write_text(f"repo='{repo}';\n")
    monkeypatch.setattr(settings, "starrynite_prod_dir", drv)
    monkeypatch.setattr(settings, "starrynite_prod_assets", None)

    prov = snp.capture_provenance()
    assert prov["dev_repo"] == str(repo)
    assert len(prov["dev_head"]) == 40
    assert prov["dev_head_subject"] == "seed"
    # The dirty list is the whole point: a HEAD alone would misidentify this run.
    assert any("tracker.m" in entry for entry in prov["dev_dirty"])


def test_build_lineage_zip_packs_sorted_timepoints(tmp_path):
    ndir = tmp_path / "nuclei"
    ndir.mkdir()
    for i in (1, 2, 10):
        (ndir / f"t{i:03d}-nuclei").write_text(f"tp{i}\n")
    params = tmp_path / "matlabParams"
    params.write_text("xyres=0.16;\n")

    n = snp.build_lineage_zip(ndir, tmp_path / "s.zip", params)
    assert n == 3
    with zipfile.ZipFile(tmp_path / "s.zip") as z:
        names = z.namelist()
    assert names[:3] == ["nuclei/t001-nuclei", "nuclei/t002-nuclei", "nuclei/t010-nuclei"]
    assert "parameters/matlabParams" in names
    assert not list(tmp_path.glob("*.tmp"))


def test_build_lineage_zip_requires_nuclei(tmp_path):
    (tmp_path / "nuclei").mkdir()
    with pytest.raises(FileNotFoundError):
        snp.build_lineage_zip(tmp_path / "nuclei", tmp_path / "s.zip")


def test_land_lineage_fills_both_slots(tmp_path):
    out = tmp_path / "out" / "nuclei"
    out.mkdir(parents=True)
    (out / "t001-nuclei").write_text("a\n")
    dats = tmp_path / "dats"

    landed = snp.land_lineage(tmp_path / "out", dats, "SER")
    assert landed["timepoints"] == 1
    assert (dats / "SER-edit.zip").is_file()
    assert (dats / "SER.zip").is_file()


def test_land_lineage_archives_existing_curation(tmp_path):
    """-edit.zip is where AceTree saves curation; rebuilding it from tracker
    output destroyed a curation pass once already."""
    out = tmp_path / "out" / "nuclei"
    out.mkdir(parents=True)
    (out / "t001-nuclei").write_text("a\n")
    dats = tmp_path / "dats"
    dats.mkdir()
    (dats / "SER-edit.zip").write_bytes(b"PRECIOUS CURATION")

    landed = snp.land_lineage(tmp_path / "out", dats, "SER")
    assert landed["archived"]
    saved = list((dats / "archived").rglob("SER-edit.zip"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == b"PRECIOUS CURATION"


def test_land_lineage_without_nuclei_raises(tmp_path):
    (tmp_path / "out").mkdir()
    with pytest.raises(FileNotFoundError, match="no t\\*-nuclei"):
        snp.land_lineage(tmp_path / "out", tmp_path / "dats", "SER")


def test_prod_is_an_accepted_cli_engine():
    from embryodb.cli import SN_ENGINES, _validate_sn_engine

    assert "prod" in SN_ENGINES
    assert _validate_sn_engine("prod") == "prod"


def test_resolve_options_defaults_to_settings(monkeypatch):
    monkeypatch.setattr(settings, "starrynite_prod_uniform_threshold", 0.004)
    monkeypatch.setattr(settings, "starrynite_prod_iscale", 1.0)
    monkeypatch.setattr(settings, "starrynite_prod_stagelo", 3)
    assert snp.resolve_options(None) == {
        "uniform_threshold": 0.004, "iscale": 1.0, "stagelo": 3
    }
    assert snp.resolve_options({"sn_engine": "prod"})["uniform_threshold"] == 0.004


def test_resolve_options_applies_per_run_overrides(monkeypatch):
    monkeypatch.setattr(settings, "starrynite_prod_uniform_threshold", 0.004)
    monkeypatch.setattr(settings, "starrynite_prod_iscale", 1.0)
    monkeypatch.setattr(settings, "starrynite_prod_stagelo", 3)
    opts = snp.resolve_options(
        {"sn_uniform_threshold": "0.0025", "sn_iscale": "0.5", "sn_stagelo": 2}
    )
    assert opts == {"uniform_threshold": 0.0025, "iscale": 0.5, "stagelo": 2}


@pytest.mark.parametrize("token", ["none", "None", "native", "keep", "paramfile", ""])
def test_resolve_options_native_tokens_keep_paramfile_bands(token, monkeypatch):
    """A hand-tuned per-band vector must survive: flattening it would undo the
    tuning that makes a dim movie work."""
    monkeypatch.setattr(settings, "starrynite_prod_uniform_threshold", 0.004)
    assert snp.resolve_options(
        {"sn_uniform_threshold": token}
    )["uniform_threshold"] is None
    assert snp.resolve_options(
        {"sn_uniform_threshold": None}
    )["uniform_threshold"] is None


@pytest.mark.parametrize(
    "params",
    [
        {"sn_uniform_threshold": "very dim"},
        {"sn_iscale": "half"},
        {"sn_stagelo": "2.5x"},
    ],
)
def test_resolve_options_rejects_garbage(params):
    """Silently falling back to the default would produce a lineage whose
    recorded threshold is not the one you asked for."""
    with pytest.raises(ValueError):
        snp.resolve_options(params)


def test_matlab_command_honours_overridden_options(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "starrynite_prod_dir", Path("/opt/sn/release_prod"))
    monkeypatch.setattr(settings, "starrynite_prod_assets", None)
    monkeypatch.setattr(settings, "starrynite_prod_iscale", 1.0)
    monkeypatch.setattr(settings, "starrynite_prod_stagelo", 3)
    monkeypatch.setattr(settings, "matlab_command", Path("/usr/bin/matlab"))

    cmd, _ = snp.build_matlab_command(
        "/scratch/matlabParams", "/img/t001-p01.tif", "/scratch/out", "/scratch",
        options={"uniform_threshold": None, "iscale": 0.5, "stagelo": 2},
    )
    expr = cmd[-1]
    assert "'stagelo', 2" in expr
    assert "'iscale', 0.5" in expr


def test_capture_provenance_records_effective_options(tmp_path, monkeypatch):
    """Two runs that differed only in threshold must not look identical in the
    provenance record."""
    monkeypatch.setattr(settings, "starrynite_prod_dir", tmp_path / "nope")
    monkeypatch.setattr(settings, "starrynite_prod_assets", None)
    monkeypatch.setattr(settings, "starrynite_prod_uniform_threshold", 0.004)
    prov = snp.capture_provenance(
        {"uniform_threshold": 0.0025, "iscale": 0.5, "stagelo": 2}
    )
    assert prov["uniform_threshold"] == 0.0025
    assert prov["iscale"] == 0.5
    assert prov["stagelo"] == 2


def test_diagnose_prod_failure_explains_the_zlevel_crash(tmp_path):
    log = tmp_path / "run.log"
    log.write_text(
        "Detect stage...\n"
        "Unrecognized function or variable 'zlevel'.\n"
        "Error in processVolume (line 113)\n"
    )
    msg = snp.diagnose_prod_failure(log, {"uniform_threshold": 0.004})
    assert "0.004" in msg
    assert "too high for this movie" in msg
    # Points at the knob that actually fixes it.
    assert "sn_uniform_threshold" in msg

    native = snp.diagnose_prod_failure(log, {"uniform_threshold": None})
    assert "per-band" in native


def test_diagnose_prod_failure_is_generic_otherwise(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("Error using imread\nsomething else entirely\n")
    assert "see log tail" in snp.diagnose_prod_failure(log)
    assert "see log tail" in snp.diagnose_prod_failure(tmp_path / "missing.log")


def test_real_driver_still_matches_our_expectations():
    """The driver lives outside this repo and is edited by the StarryNite work.
    If its signature drifts, the pipeline would issue a call it no longer
    accepts -- fail loudly here rather than at 3am in a queued run."""
    drv = snp.driver_path()
    if not drv.is_file():
        pytest.skip(f"production driver not installed at {drv}")
    text = drv.read_text(errors="replace")
    assert re.search(r"function\s+sn_production_driver\s*\(", text)
    for knob in ("assetdir", "iscale", "stagelo", "workdir", "pyexe"):
        assert f"'{knob}'" in text, f"driver no longer accepts {knob!r}"
    assert snp.dev_repo() is not None
