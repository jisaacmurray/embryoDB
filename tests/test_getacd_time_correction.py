"""Tests for the ACD time-correction estimator.

The factor scales *leaf* branch lengths into the reference timebase (internal
branches take their span from the model directly), so overestimating it
stretches every terminal branch. GetACD.pl and get_acd.R both average over
every cell, which lets cycles truncated by the end of the movie -- full model
length, fragment of an observed span -- inflate the estimate without bound.
"""

from __future__ import annotations

import pytest

from embryodb.getacd import DivTimes, compute_time_correction

CYCLE = 10  # model cell-cycle length, reference units
OBSERVED = 5  # timepoints each cell is seen for => true correction is 2.0


def _lineage(n_complete: int, n_truncated: int, truncated_cycle: int = 200):
    """A chain of observed cells, plus leaves whose division is never seen."""
    birth, division, parent, daughter = {}, {}, {}, {}
    rows = []

    for i in range(n_complete + 2):  # +2: the chain's first and last are not complete
        cell = f"c{i}"
        birth[cell] = i * CYCLE
        division[cell] = i * CYCLE + CYCLE
        if i:
            parent[cell] = f"c{i - 1}"
            daughter[f"c{i - 1}"] = cell
        rows += [{"cell": cell, "time": i * OBSERVED + t} for t in range(OBSERVED + 1)]

    # Leaves: born from an observed parent, but the movie ends before they
    # divide, so `observed` is 1 timepoint against a whole model cycle.
    for j in range(n_truncated):
        cell = f"t{j}"
        birth[cell] = 500
        division[cell] = 500 + truncated_cycle
        parent[cell] = "c0"
        rows += [{"cell": cell, "time": 900}, {"cell": cell, "time": 901}]

    return rows, DivTimes(birth=birth, division=division, parent=parent, daughter=daughter)


def test_truncated_cycles_do_not_inflate_the_estimate():
    """The regression that stretched terminal branches ~5x on
    20230328_SYS674_tab-1_L3: a mean over every cell reached 8.1 where the
    fully-observed cells said ~1.6."""
    rows, div = _lineage(n_complete=40, n_truncated=40)
    assert compute_time_correction(rows, div) == pytest.approx(CYCLE / OBSERVED)


def test_estimate_is_one_when_the_movie_is_already_on_the_reference_timebase():
    """ReferenceModel must round-trip to exactly 1.0 -- it *is* the timebase."""
    rows, div = _lineage(n_complete=40, n_truncated=0)
    for r in rows:  # observe each cell for a full model cycle
        r["time"] = int(r["time"]) * (CYCLE // OBSERVED)
    assert compute_time_correction(rows, div) == pytest.approx(1.0)


def test_falls_back_to_the_median_when_too_few_complete_cycles(capsys):
    """Degenerate lineages (e.g. an unlineaged 15k-'cell' CD) have no complete
    cycles at all, so the estimator must still return something and say so."""
    rows, div = _lineage(n_complete=2, n_truncated=30, truncated_cycle=200)
    correction = compute_time_correction(rows, div)
    err = capsys.readouterr().err
    assert "fully-observed cell cycles" in err
    # median over all ratios, dominated by the truncated leaves
    assert correction == pytest.approx(200.0)
    assert "outside the expected range" in err


def test_no_usable_ratios_returns_one(capsys):
    div = DivTimes(birth={}, division={}, parent={}, daughter={})
    assert compute_time_correction([{"cell": "X", "time": 1}], div) == 1.0
    assert "no valid time ratios" in capsys.readouterr().err
