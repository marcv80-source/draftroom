"""Tests for the validation suite (`draftroom.validate`) -- the runnable sanity-invariant gate
and the real-board builder it runs against.

Fixtures for the invariant-check tests are synthetic and local (mirroring
`tests/test_valuation.py`'s own conventions) so this file stays hermetic and fast; the
real-board tests use the SAME cached data other tests already rely on
(`tests/test_recommend.py::TestSimulationPerformance` reads the same cached FFC payload) --
no network call, nothing that touches another agent's in-progress module.
"""

from __future__ import annotations

import pytest

from draftroom.config import LeagueConfig
from draftroom.valuation.replacement import PlayerSeason
from draftroom.validate import board as board_mod
from draftroom.validate import invariants

HALF_PPR = {
    "pass_yd": 0.04,
    "pass_td": 4.0,
    "pass_int": -2.0,
    "rush_yd": 0.1,
    "rush_td": 6.0,
    "rec": 0.5,
    "rec_yd": 0.1,
    "rec_td": 6.0,
    "fum_lost": -2.0,
}


def make_cfg(**overrides) -> LeagueConfig:
    payload = dict(
        teams=10,
        starters={"QB": 2, "RB": 2, "WR": 3, "TE": 1},
        flex_slots=1,
        flex_eligible=frozenset({"RB", "WR", "TE"}),
        bench=6,
        weeks=17,
        scoring=HALF_PPR,
    )
    payload.update(overrides)
    return LeagueConfig(**payload)


def linear_pool(pos: str, n: int, hi: float, lo: float) -> list[PlayerSeason]:
    step = 0.0 if n <= 1 else (hi - lo) / (n - 1)
    return [
        PlayerSeason(player_id=f"{pos}{i + 1}", pos=pos, ppg=hi - step * i, name=f"{pos}{i + 1}")
        for i in range(n)
    ]


def realistic_pool() -> list[PlayerSeason]:
    """A QB curve with real separation at the top -- the shape the 10-14-in-top-30 invariant
    assumes. `test_qb_count_in_top30_fails_on_a_flat_qb_curve` below is the deliberate mirror:
    same positions, a QB curve with NO separation, to prove the check actually discriminates."""
    return (
        linear_pool("QB", 36, 24.0, 10.0)
        + linear_pool("RB", 60, 20.0, 5.0)
        + linear_pool("WR", 80, 19.0, 5.0)
        + linear_pool("TE", 30, 14.0, 3.0)
    )


# ============================================================================ ranking checks


def test_top_qb_top8_passes_on_a_realistic_curve():
    cfg = make_cfg()
    result = invariants.check_top_qb_top8(realistic_pool(), cfg)
    assert result.passed, result.detail
    assert "top QB" in result.detail
    assert "#" in result.detail


def test_top_qb_top8_fails_when_qb_is_pushed_out_of_the_pool():
    """If nobody in the pool is a QB, the check must FAIL loudly, not silently skip."""
    cfg = make_cfg()
    no_qb_pool = [p for p in realistic_pool() if p.pos != "QB"]
    result = invariants.check_top_qb_top8(no_qb_pool, cfg)
    assert not result.passed
    assert "no QB" in result.detail


def test_qb_count_in_top30_passes_on_a_realistic_curve():
    """The 2QB shift is directional, not an absolute band (see the function's docstring: a
    fixed "10-14" band assumed a 12-team league and is not immune to a given year's projection
    compression) -- this league's real 2-QB rules must put strictly more QBs in the top 30 than
    the same board scored under generic 1-QB rules would."""
    cfg = make_cfg()
    result = invariants.check_qb_count_in_top30(realistic_pool(), cfg)
    assert result.passed, result.detail
    assert "1-QB rules" in result.detail and "2-QB rules" in result.detail


def test_qb_count_in_top30_fails_when_the_pool_is_too_shallow_for_the_shift_to_show():
    """The check must be able to fail, not just always pass. A QB pool too shallow to cover
    even the 1-QB demand is already at the bottom of the pool (`pool_exhausted`) under both
    1-QB and 2-QB rules -- the baseline rank is capped at the pool size either way, so doubling
    the starter requirement cannot deepen it any further and the comparative check correctly
    reports no shift."""
    cfg = make_cfg()
    shallow_qb_pool = (
        linear_pool("QB", 3, 22.0, 20.0)  # far too few to cover even 12*1*17 man-games
        + linear_pool("RB", 60, 20.0, 5.0)
        + linear_pool("WR", 80, 19.0, 5.0)
        + linear_pool("TE", 30, 14.0, 3.0)
    )
    result = invariants.check_qb_count_in_top30(shallow_qb_pool, cfg)
    assert not result.passed, result.detail


# ==================================================================== baseline monotonicity


def test_baseline_monotonic_team_count_passes_on_the_deep_pool():
    pool = invariants.deep_synthetic_pool()
    result = invariants.check_baseline_monotonic_team_count(pool, make_cfg())
    assert result.passed, result.detail
    for pos in ("QB", "RB", "WR", "TE"):
        assert pos in result.detail


def test_baseline_monotonic_team_count_flags_pool_exhaustion():
    """A pool too shallow for the sweep must FAIL and say so, not silently report a false
    monotonic pass off an exhausted (overstated) baseline."""
    shallow_pool = linear_pool("QB", 20, 22.0, 15.0) + linear_pool("RB", 60, 20.0, 5.0) + linear_pool(
        "WR", 80, 19.0, 5.0
    ) + linear_pool("TE", 30, 14.0, 3.0)
    result = invariants.check_baseline_monotonic_team_count(
        shallow_pool, make_cfg(), team_counts=(8, 10, 12, 14, 16)
    )
    assert not result.passed
    assert "EXHAUSTED" in result.detail


def test_baseline_monotonic_starter_slots_passes_on_the_deep_pool():
    pool = invariants.deep_synthetic_pool()
    result = invariants.check_baseline_monotonic_starter_slots(pool, make_cfg())
    assert result.passed, result.detail


# ============================================================================= survival


def test_survival_monotone_and_normalized_passes():
    result = invariants.check_survival_monotone_and_normalized()
    assert result.passed, result.detail
    assert "== 1.0" in result.detail


# =========================================================================== per-game fixture


def test_per_game_fixture_passes_at_a_plausible_baseline():
    result = invariants.check_per_game_fixture(7.0)
    assert result.passed, result.detail
    assert "HIGHER season total" in result.detail


@pytest.mark.parametrize("baseline", [0.5, 3.0, 7.0, 11.0])
def test_per_game_fixture_passes_across_plausible_baselines(baseline):
    """Same fact as `test_valuation.py`'s Harstad parametrization: A wins for any baseline
    above ~0.11 ppg (algebraically A-B = 9*baseline - 1)."""
    result = invariants.check_per_game_fixture(baseline)
    assert result.passed, result.detail


def test_no_default_full_season_games_passes_on_the_real_curve():
    result = invariants.check_no_default_expected_games_hits_full_season(
        invariants.deep_synthetic_pool(), make_cfg()
    )
    assert result.passed, result.detail
    assert "max default expected_games" in result.detail


def test_no_default_full_season_games_ignores_a_real_explicit_override():
    """An explicit per-player projection of exactly `weeks` games is real data, not the
    fabrication bug this check guards against, so it must not trip the invariant."""
    cfg = make_cfg()
    pool = [PlayerSeason(player_id="QB1", pos="QB", ppg=25.0, expected_games=17.0)]
    result = invariants.check_no_default_expected_games_hits_full_season(pool, cfg)
    assert result.passed, result.detail


def test_no_default_full_season_games_can_fail():
    """The check must be able to fail: reintroduce a flat 17.0-for-everyone curve (the exact
    shape of the regression this guards against) and confirm the invariant catches it.

    Mutates ``EXPECTED_GAMES_CURVE`` IN PLACE (not by rebinding the module attribute) --
    ``resolve_players``/``expected_games`` bind it as a default parameter value at import time,
    so reassigning ``replacement_mod.EXPECTED_GAMES_CURVE = ...`` would not be seen by code that
    already holds a reference to the original dict object.
    """
    import draftroom.valuation.replacement as replacement_mod

    cfg = make_cfg()
    pool = [PlayerSeason(player_id="QB1", pos="QB", ppg=25.0)]  # no override -> curve lookup
    flat_curve = {pos: ((1, None, 17.0),) for pos in replacement_mod.EXPECTED_GAMES_CURVE}
    saved = dict(replacement_mod.EXPECTED_GAMES_CURVE)
    curve_obj = replacement_mod.EXPECTED_GAMES_CURVE
    curve_obj.clear()
    curve_obj.update(flat_curve)
    try:
        result = invariants.check_no_default_expected_games_hits_full_season(pool, cfg)
    finally:
        curve_obj.clear()
        curve_obj.update(saved)
    assert not result.passed, result.detail


def test_per_game_fixture_can_fail():
    """The check must be able to fail -- prove it by picking a baseline below the algebraic
    crossover `test_valuation.py` already establishes for this exact fixture
    (`evob_a - evob_b == 9*baseline - 1`, so A only wins above baseline ~0.111 ppg). At
    baseline 0.0 the difference is exactly -1: B wins, and the check must say so."""
    result = invariants.check_per_game_fixture(0.0)
    assert not result.passed, result.detail


# ================================================================================= run_all


def test_run_all_returns_one_result_per_check_with_real_numbers():
    cfg = make_cfg()
    pool = realistic_pool()
    results = invariants.run_all(pool, cfg, deep_pool=invariants.deep_synthetic_pool())
    assert len(results) == 8
    names = {r.name for r in results}
    assert names == {
        "top_qb_top8",
        "qb_count_in_top30",
        "baseline_monotonic_team_count",
        "baseline_monotonic_starter_slots",
        "survival_monotone_and_normalized",
        "per_game_fixture_beats_season_total",
        "no_default_full_season_games",
        "expected_games_capped_by_curve",
    }
    for r in results:
        assert r.detail, f"{r.name} has no detail -- CLAUDE.md requires real numbers, not PASS/FAIL alone"


def test_run_all_derives_a_per_game_baseline_when_not_given_one():
    """The default per-game-fixture baseline is this league's own TE replacement level, not an
    arbitrary constant -- exercised by omitting `per_game_baseline_ppg`."""
    cfg = make_cfg()
    pool = realistic_pool()
    results = invariants.run_all(pool, cfg, deep_pool=invariants.deep_synthetic_pool())
    fixture = next(r for r in results if r.name == "per_game_fixture_beats_season_total")
    assert fixture.passed


# ============================================================================ real board

# These read the SAME cached data tests/test_recommend.py::TestSimulationPerformance already
# reads via load_ffc_adp() -- no network, and the fixture files ship in the repo's data/raw/.


def test_build_real_board_loads_a_deep_valued_pool():
    real = board_mod.build_real_board()
    assert real.cfg.teams == 10, "must use the real, CONFIRMED league config by default"
    assert len(real.players) >= 150, "must have enough depth for a full 15-round, 10-team draft"
    assert len(real.seasons) == len(real.players)
    positions = {p.pos for p in real.players}
    assert positions == {"QB", "RB", "WR", "TE"}
    # Every excluded row must be accounted for (unresolved or no game projection), never just
    # dropped without a trace.
    for row in real.excluded:
        assert row.pos.strip().upper() in {"QB", "RB", "WR", "TE"}


def test_real_board_players_and_seasons_share_the_same_ids():
    real = board_mod.build_real_board()
    player_ids = {p.player_id for p in real.players}
    season_ids = {s.player_id for s in real.seasons}
    assert player_ids == season_ids


def test_run_invariants_gate_runs_end_to_end_on_the_real_board():
    """Not a re-assertion of the numeric bars (those are covered above with controllable
    fixtures) -- this just proves the real board plugs into `run_all` without error and returns
    a fully-populated report, the same call `tools/run_invariants.py` makes."""
    real = board_mod.build_real_board()
    results = invariants.run_all(list(real.seasons), real.cfg)
    assert len(results) == 8
    for r in results:
        assert isinstance(r.passed, bool)
        assert r.detail
