"""Tests for the bonus-model-vs-FantasySharks validation report (``tools/validate_bonus_vs_sharks``).

The report itself is a REPORT -- it changes nothing, so there is no behaviour to pin. What does
need pinning is its **coverage classification**, because that is where the report could quietly
lie: a cell wrongly marked COMPARED would be comparing our number against a threshold
FantasySharks never published (or against a curve we never fitted), and the resulting bias would
look exactly like a real disagreement. docs/FANTASYSHARKS.md is explicit that the gaps must not
be blurred -- in particular that receiving covers all three tiers **only for WR and TE**, since
the RB table's receiving thresholds stop at 100 -- so those claims are asserted here rather than
trusted.

No network. The threshold coverage comes from the adapter's own layout tables and the league's
own yaml; only the fitted-curve half touches disk (``data/bonus_curves.json``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import validate_bonus_vs_sharks as tool  # noqa: E402

from draftroom.valuation.bonuses import load_bonus_schedule, load_curves  # noqa: E402


@pytest.fixture(scope="module")
def cells():
    return tool.coverage(load_bonus_schedule(), load_curves())


def test_passing_and_rushing_cover_only_the_plus_three_tier(cells):
    """The documented gap: FantasySharks publishes passing counts at 250/300/350 and rushing at
    50/100, so of this league's tiers only the first (300 passing, 100 rushing) has any external
    reference at all."""
    assert cells[("QB", "pass_yd", 300.0)] == tool.COMPARED
    assert cells[("QB", "pass_yd", 400.0)] == tool.NO_SOURCE
    assert cells[("QB", "pass_yd", 500.0)] == tool.NO_SOURCE
    assert cells[("RB", "rush_yd", 100.0)] == tool.COMPARED
    assert cells[("RB", "rush_yd", 150.0)] == tool.NO_SOURCE
    assert cells[("RB", "rush_yd", 200.0)] == tool.NO_SOURCE


def test_receiving_covers_all_three_tiers_for_wr_and_te_but_not_for_rb(cells):
    """The qualification docs/FANTASYSHARKS.md says the report must not blur. The RB table's
    receiving threshold columns stop at 100, so an RB's receiving 150/200 tiers have NO external
    reference -- a small gap (a back clearing 150 receiving yards in a game is rare) but a gap,
    and it must be reported as absent rather than interpolated from the WR table."""
    for pos in ("WR", "TE"):
        for thr in (100.0, 150.0, 200.0):
            assert cells[(pos, "rec_yd", thr)] == tool.COMPARED, (pos, thr)
    assert cells[("RB", "rec_yd", 100.0)] == tool.COMPARED
    assert cells[("RB", "rec_yd", 150.0)] == tool.NO_SOURCE
    assert cells[("RB", "rec_yd", 200.0)] == tool.NO_SOURCE


def test_a_position_that_does_not_play_a_stat_is_reported_absent_not_compared(cells):
    """A quarterback has no receiving threshold column and a receiver has no passing one. Those
    cells must read NO SOURCE REFERENCE -- comparing our 0.0 against a number that was never
    published would manufacture perfect agreement about a non-event."""
    for thr in (100.0, 150.0, 200.0):
        assert cells[("QB", "rec_yd", thr)] == tool.NO_SOURCE
    for pos in ("WR", "TE"):
        assert cells[(pos, "pass_yd", 300.0)] == tool.NO_SOURCE
        assert cells[(pos, "rush_yd", 100.0)] == tool.NO_SOURCE


def test_the_unpaid_thresholds_are_listed_and_never_compared(cells):
    """FantasySharks publishes four thresholds this league does not pay. They are real external
    information about the same distribution, but our curves are fitted only at the league's own
    tiers, so there is nothing to compare them to -- and the report must say that rather than
    fit something new (which would be a change to the bonus model)."""
    extra = tool.unpaid_thresholds(load_bonus_schedule())
    assert ("pass_yd", 250.0) in extra
    assert ("pass_yd", 350.0) in extra
    assert ("rush_yd", 50.0) in extra
    assert ("rec_yd", 50.0) in extra
    # ...and none of them appears as a comparable cell, at any position.
    league_tiers = {
        (stat, float(t["threshold"]))
        for stat, tiers in load_bonus_schedule().items()
        for t in tiers
    }
    for (_pos, stat, thr), verdict in cells.items():
        if verdict == tool.COMPARED:
            assert (stat, thr) in league_tiers


def test_every_cell_carries_exactly_one_of_the_three_verdicts(cells):
    assert cells, "no coverage cells classified"
    assert set(cells.values()) <= {tool.COMPARED, tool.NO_SOURCE, tool.NO_CURVE}
    assert sum(1 for v in cells.values() if v == tool.COMPARED) == 9, (
        "expected exactly nine comparable cells: pass 300 (QB), rush 100 (RB), rec 100 "
        "(RB/WR/TE), rec 150 and 200 (WR/TE)"
    )


def test_a_constant_column_reports_no_correlation_rather_than_zero():
    """A 0.0 correlation is a measurement; an undefined one is not. If every player's count is
    the same, there is no correlation to report and the table must print a dash."""
    assert tool._pearson([1.0, 1.0, 1.0], [2.0, 3.0, 4.0]) is None
    assert tool._pearson([1.0, 2.0], [1.0, 2.0]) is None  # too few points to fit anything
    assert tool._pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)
