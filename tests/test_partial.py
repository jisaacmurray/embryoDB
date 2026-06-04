"""Tests for the Python replacement for Partial.pl."""

from __future__ import annotations

import pytest

from embryodb.partial import (
    PartialEditRule,
    canonicalize,
    parse_editing_code,
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
