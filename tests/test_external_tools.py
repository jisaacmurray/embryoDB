"""Tests for the detached subprocess launchers for legacy analysis tools.

We never actually spawn java/perl here — `subprocess.Popen` is monkeypatched
to capture the constructed shell command so we can assert on its shape.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from embryodb import external_tools


class _FakePopen:
    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = 12345

    def poll(self):
        return 0


@pytest.fixture
def captured_popen(monkeypatch, tmp_path):
    """Replace subprocess.Popen and route runs dir into tmp_path."""
    monkeypatch.setattr(external_tools.subprocess, "Popen", _FakePopen)
    monkeypatch.setenv("EMBRYODB_RUNS_DIR", str(tmp_path))
    return tmp_path


def test_run_extract_builds_correct_shell(captured_popen, tmp_path):
    result = external_tools.run_extract(
        ["seriesA", "seriesB"],
        ["red_extractor", "partial", "align"],
        tools3_dir=Path("/fake/tools3"),
    )
    assert isinstance(result.proc, _FakePopen)
    assert result.proc.args[0] == "bash"
    assert result.proc.args[1] == "-c"
    cmd = result.proc.args[2]
    # The extract chain no longer inlines java/perl per step. It delegates to the
    # per-series tracked runner, which isolates failures and records
    # PipelineStepRun rows (see embryodb.pipeline.extract_run).
    assert "-m embryodb.cli run-extract-batch" in cmd
    # Chosen steps are passed through in canonical order (comma-joined).
    assert "--steps 'red_extractor,partial,align'" in cmd
    # The list file the runner reads is the one written under EMBRYODB_RUNS_DIR.
    assert f"--list '{result.series_list}'" in cmd
    # detached subprocess kwargs
    assert result.proc.kwargs["start_new_session"] is True
    # series list file was written under EMBRYODB_RUNS_DIR
    assert result.series_list.parent == tmp_path
    assert result.series_list.read_text() == "seriesA\nseriesB\n"
    # log path lives alongside the list file
    assert result.log_path.parent == tmp_path
    assert result.log_path.suffix == ".log"


def test_run_extract_rejects_empty_series(captured_popen):
    with pytest.raises(ValueError, match="series_names"):
        external_tools.run_extract([], ["red_extractor"])


def test_run_extract_rejects_empty_steps(captured_popen):
    with pytest.raises(ValueError, match="step_keys"):
        external_tools.run_extract(["s"], [])


def test_run_extract_rejects_unknown_step(captured_popen):
    with pytest.raises(ValueError, match="unknown step keys"):
        external_tools.run_extract(["s"], ["bogus"])


def test_run_print_trees_minimal(captured_popen, tmp_path):
    result = external_tools.run_print_trees(
        ["seriesA"],
        renderer="java",
        tools3_dir=Path("/fake/tools3"),
    )
    cmd = result.proc.args[2]
    assert "acexpress_CL2.jar" in cmd
    assert "Tree1" in cmd
    # Without explicit params, only the series list arg goes after Tree1.
    after = cmd.split("Tree1")[1]
    assert "rainbow" not in after  # color_scheme not added unless min_expr given
    assert result.series_list.read_text() == "seriesA\n"


def test_run_print_trees_with_params(captured_popen, tmp_path):
    # Tree1 parses min/max/linewidth with Integer.parseInt — passing a float
    # string (e.g. "100.0") triggers NumberFormatException. Even when the
    # caller passes a float, the wrapper must serialize as an int.
    result = external_tools.run_print_trees(
        ["seriesA"],
        min_expr=100.0,
        max_expr=5000.0,
        color_scheme="ABal",
        linewidth=5,
        renderer="java",
        tools3_dir=Path("/fake/tools3"),
    )
    cmd = result.proc.args[2]
    # Tree1's positional args: <list> <min> <max> <colorOrRoot> <linewidth>
    after = cmd.split("Tree1")[1]
    assert " 100 " in after
    assert " 5000 " in after
    assert "100.0" not in after
    assert "5000.0" not in after
    assert "'ABal'" in after
    assert " 5" in after


def test_run_print_trees_defaults_to_headless(captured_popen):
    result = external_tools.run_print_trees(
        ["seriesA"], renderer="java", tools3_dir=Path("/fake/tools3")
    )
    assert "xvfb-run -a" in result.proc.args[2]


def test_run_print_trees_on_screen_skips_xvfb(captured_popen, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    result = external_tools.run_print_trees(
        ["seriesA"], on_screen=True, renderer="java", tools3_dir=Path("/fake/tools3")
    )
    assert "xvfb-run" not in result.proc.args[2]


def test_run_print_trees_on_screen_requires_display(captured_popen, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    with pytest.raises(external_tools.LaunchError, match="DISPLAY"):
        external_tools.run_print_trees(["seriesA"], on_screen=True, renderer="java")


def test_run_print_trees_on_screen_rejected_in_remote_mode(captured_popen, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(external_tools.settings, "remote", True)
    with pytest.raises(external_tools.LaunchError, match="remote mode"):
        external_tools.run_print_trees(["seriesA"], on_screen=True, renderer="java")


def test_print_trees_defaults_to_livetools(captured_popen):
    """The default renderer is LIVEtools -- no JVM in the shell at all."""
    cmd = external_tools.run_print_trees(["seriesA"]).proc.args[2]
    assert "render-trees-batch" in cmd
    assert "acexpress_CL2.jar" not in cmd


def test_livetools_renderer_passes_scale_and_prefix(captured_popen):
    cmd = external_tools.run_print_trees(
        ["seriesA"],
        min_expr=0,
        max_expr=43750,
        color_scheme="blueyellow",
        cd_prefix="ACD",
        renderer="livetools",
    ).proc.args[2]
    assert "--cd-prefix 'ACD'" in cmd
    assert "--color-scheme 'blueyellow'" in cmd
    assert "--min-expr 0" in cmd
    assert "--max-expr 43750" in cmd


def test_livetools_renderer_rejects_on_screen(captured_popen, monkeypatch):
    """--on-screen shows Tree1's JFrame; LIVEtools only writes PNGs."""
    monkeypatch.setenv("DISPLAY", ":0")
    with pytest.raises(external_tools.LaunchError, match="Tree1 feature"):
        external_tools.run_print_trees(["seriesA"], on_screen=True, renderer="livetools")


def test_print_trees_rejects_unknown_renderer(captured_popen):
    with pytest.raises(external_tools.LaunchError, match="unknown tree renderer"):
        external_tools.run_print_trees(["seriesA"], renderer="graphviz")


def test_queued_print_trees_job_keeps_its_renderer(tmp_path):
    """A job queued in remote mode must render the same way on the worker."""
    shell = external_tools.build_command_for_kind(
        "print_trees",
        {"series_names": ["seriesA"], "renderer": "java"},
        tmp_path / "list.txt",
        Path("/fake/tools3"),
    )
    assert "acexpress_CL2.jar" in shell


def test_run_getacd_builds_correct_shell(captured_popen, tmp_path):
    result = external_tools.run_getacd(
        ["seriesA", "seriesB"],
        tools3_dir=Path("/fake/tools3"),
    )
    cmd = result.proc.args[2]
    assert "GetACD STOPGAP" in cmd
    # Invokes the legacy Perl script from tools3 on the list file.
    assert "perl '/fake/tools3/GetACD.pl'" in cmd
    assert f"'{result.series_list}'" in cmd
    # Pre-creates the scratch dirs GetACD.pl copies into.
    assert "mkdir -p CDs AuxInfos" in cmd
    assert result.series_list.read_text() == "seriesA\nseriesB\n"
    assert result.proc.kwargs["start_new_session"] is True


def test_run_getacd_rejects_empty_series(captured_popen):
    with pytest.raises(ValueError, match="series_names"):
        external_tools.run_getacd([])


def test_shell_quote_handles_single_quotes():
    # Bash single-quote-escape uses '\''.
    assert external_tools.shell_quote("ab'cd") == "'ab'\\''cd'"
