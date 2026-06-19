"""Tests for embryodb.external launchers (legacy Java AceTree + AceTree-Py).

The spawned processes are monkeypatched so nothing real is launched; the
focus is config-path resolution and the LaunchError guards.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from embryodb import external
from embryodb.models import Series


def _series_with_config(tmp_path: Path, name: str = "demo_L1") -> Series:
    dats = tmp_path / name / "dats"
    dats.mkdir(parents=True)
    (dats / f"{name}.xml").write_text("<embryo/>")
    return Series(
        series_name=name,
        annot_loc=str(tmp_path / name),
        acetree_config=f"{name}.xml",
    )


def test_acetree_py_missing_config_raises(tmp_path):
    row = Series(series_name="x_L1", annot_loc=str(tmp_path), acetree_config="x_L1.xml")
    with pytest.raises(external.LaunchError, match="config not found"):
        external.launch_acetree_py(row)


def test_acetree_py_missing_interpreter_raises(tmp_path):
    row = _series_with_config(tmp_path)
    missing = tmp_path / "no_such_venv" / "bin" / "python"
    with pytest.raises(external.LaunchError, match="interpreter not found"):
        external.launch_acetree_py(row, python=missing)


def test_acetree_py_spawns_with_config(tmp_path, monkeypatch):
    row = _series_with_config(tmp_path)
    fake_py = tmp_path / "venv" / "bin" / "python"
    fake_py.parent.mkdir(parents=True)
    fake_py.write_text("#!/bin/sh\n")

    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            captured["cmd"] = cmd
            self.pid = 4321

    monkeypatch.setattr(external.subprocess, "Popen", FakePopen)
    proc = external.launch_acetree_py(row, python=fake_py)
    assert proc.pid == 4321
    assert captured["cmd"][:4] == [str(fake_py), "-m", "acetree_py", "gui"]
    assert captured["cmd"][4].endswith("demo_L1.xml")
