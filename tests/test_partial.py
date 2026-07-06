"""Tests for the Python replacement for Partial.pl."""

from __future__ import annotations

from pathlib import Path

import pytest

from embryodb import partial as partial_mod
from embryodb.models import Series
from embryodb.partial import (
    EXIT_OK,
    PartialEditRule,
    canonicalize,
    parse_editing_code,
    run_partial_for_series,
)


# --- parse_editing_code ----------------------------------------------------


def test_parse_canonical_form():
    rules = parse_editing_code("ABala:100,ABplp:200")
    assert rules == [
        PartialEditRule("ABala", 100),
        PartialEditRule("ABplp", 200),
    ]


def test_parse_single_cell_rule():
    assert parse_editing_code("P0:240") == [PartialEditRule("P0", 240)]


def test_parse_bare_single_time_gets_p0_prefix():
    # PartialCSV.java prepends "P0:" when the input starts with a digit.
    assert parse_editing_code("240") == [PartialEditRule("P0", 240)]


def test_parse_bare_multi_time_rejected():
    # After P0: prepend, the second entry has no colon — PartialCSV.java
    # throws ArrayIndexOutOfBoundsException; we return None instead.
    assert parse_editing_code("100,200") is None


def test_parse_whitespace_tolerated():
    rules = parse_editing_code("  ABala : 100 ,  ABplp:200 ")
    assert rules == [
        PartialEditRule("ABala", 100),
        PartialEditRule("ABplp", 200),
    ]


@pytest.mark.parametrize(
    "sentinel", ["", "   ", "n/a", "N/A", "none", "None", "na", "-"]
)
def test_parse_sentinel_means_no_work(sentinel):
    assert parse_editing_code(sentinel) is None


def test_parse_none_input():
    assert parse_editing_code(None) is None


@pytest.mark.parametrize(
    "bad",
    [
        "ABala",                # no colon, not bare time
        "ABala:",               # empty time
        ":100",                 # empty cell
        "ABala:notanumber",     # non-int time
        "ABala:-50",            # negative time
        "ABala:100,",           # trailing comma → empty entry
        "ABala:100,,ABplp:200", # empty entry in middle
        "ABala:100,ABplp",      # second entry missing colon
    ],
)
def test_parse_rejects_malformed(bad):
    assert parse_editing_code(bad) is None


# --- canonicalize ----------------------------------------------------------


def test_canonicalize_round_trip():
    code = "ABala:100,ABplp:200"
    rules = parse_editing_code(code)
    assert canonicalize(rules) == code


def test_canonicalize_normalizes_whitespace():
    rules = parse_editing_code("  ABala:100  ,  ABplp:200")
    assert canonicalize(rules) == "ABala:100,ABplp:200"


# --- run_partial_for_series write-back -------------------------------------


def _fake_jar_run(scratch_dir: Path, series: str, names: list[str]):
    """Return a subprocess.run stand-in that writes the jar's outputs into
    its cwd (a scratch tempdir), mimicking partialCSV.jar."""

    class _Proc:
        returncode = 0

    def _run(cmd, cwd=None, stdin=None):
        for prefix in names:
            (Path(cwd) / f"{prefix}{series}.csv").write_bytes(
                f"{prefix} trimmed\n".encode()
            )
        return _Proc()

    return _run


def test_run_partial_writes_outputs_into_dats(db_session, tmp_path, monkeypatch):
    series = "20130113_RW11388_L2"
    annot = tmp_path / "annot"
    dats = annot / "dats"
    dats.mkdir(parents=True)
    db_session.add(
        Series(
            series_name=series,
            annot_loc=str(annot),
            partial_editing_code="110,ABar:140",
        )
    )
    db_session.commit()

    jar = tmp_path / "tools4" / "partialCSV.jar"
    jar.parent.mkdir(parents=True)
    jar.write_bytes(b"jar")

    monkeypatch.setattr(
        partial_mod.subprocess,
        "run",
        _fake_jar_run(tmp_path, series, ["CD", "CA", "SCD", "SCA"]),
    )

    rc = run_partial_for_series(series, tools4_dir=jar.parent)
    assert rc == EXIT_OK
    for prefix in ["CD", "CA", "SCD", "SCA"]:
        out = dats / f"{prefix}{series}.csv"
        assert out.read_text() == f"{prefix} trimmed\n"


def test_run_partial_overwrites_preexisting_dats_file(
    db_session, tmp_path, monkeypatch
):
    """Regression: the jar stages into /tmp, then we write into GPFS dats/.
    A cross-device move degraded to copy2->copystat->utime, which raised
    EPERM when the dest file was owned by another user. safe_write_bytes
    overwrites in place (group-write is enough), so a pre-existing output
    file must be replaced without error."""
    series = "20170501_lin-39_GFP_L1"
    annot = tmp_path / "annot"
    dats = annot / "dats"
    dats.mkdir(parents=True)
    stale = dats / f"CD{series}.csv"
    stale.write_text("stale untrimmed content\n")

    db_session.add(
        Series(
            series_name=series,
            annot_loc=str(annot),
            partial_editing_code="90,ABp:120",
        )
    )
    db_session.commit()

    jar = tmp_path / "tools4" / "partialCSV.jar"
    jar.parent.mkdir(parents=True)
    jar.write_bytes(b"jar")

    monkeypatch.setattr(
        partial_mod.subprocess,
        "run",
        _fake_jar_run(tmp_path, series, ["CD"]),
    )

    rc = run_partial_for_series(series, tools4_dir=jar.parent)
    assert rc == EXIT_OK
    assert stale.read_text() == f"CD trimmed\n"
