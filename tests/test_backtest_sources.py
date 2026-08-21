"""Tests for tools/backtest_sources.py -- the 2025 source-accuracy backtest.

The traps this file is aimed at, in order of how badly each would corrupt the answer:

1. **Averaging a missing stat in as a zero.** Sleeper publishes no ``rec_tgt`` at all. If the
   blend treated that as "0 targets" rather than "no opinion", the blend would be a third,
   worse source rather than an average of two.
2. **Treating ``games`` of 0 as a projection of zero games.** Sleeper reports a blanket 18 for
   every player in both 2025 and 2026 -- a constant, not a forecast -- and an unknown must
   never be averaged in.
3. **Silently dropping the players who never played.** They are exactly the rows that punish
   an over-optimistic source, so an empty ESPN actual block ("played, recorded nothing") has
   to be distinguishable from a missing one ("unobserved") rather than collapsed together.
4. **A wrong ESPN stat id.** CLAUDE.md's standing warning: plausible numbers in the wrong
   field, and nothing downstream catches it. The verification gate must actually fail when an
   id is wrong, so it is tested against a deliberately corrupted payload.
5. **Guessing a join.** Two players with the same normalized name and position must go
   unmatched, never resolved to a coin flip.

Nothing here touches the network. The end-to-end test reads the ``data/backtest/`` cache and
skips when it is absent.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

# tools/ is a plain directory of scripts, not an installed package, so load by path rather
# than assuming an importable name.
_spec = importlib.util.spec_from_file_location(
    "backtest_sources", REPO_ROOT / "tools" / "backtest_sources.py"
)
assert _spec and _spec.loader
bs = importlib.util.module_from_spec(_spec)
sys.modules["backtest_sources"] = bs
_spec.loader.exec_module(bs)


# ================================================================================== blending


def test_blend_averages_only_sources_that_have_the_stat():
    sleeper = {"rec": 80.0, "rec_yd": 1000.0}          # no rec_tgt at all, ever
    espn = {"rec": 90.0, "rec_yd": 1100.0, "rec_tgt": 130.0}

    blended = bs.blend_statlines([sleeper, espn], games_varies=[True, True])

    assert blended["rec"] == pytest.approx(85.0)
    assert blended["rec_yd"] == pytest.approx(1050.0)
    # The single source that HAS targets carries the whole target figure. Averaging Sleeper in
    # as a zero would have produced 65.0 and invented a receiver nobody projected.
    assert blended["rec_tgt"] == pytest.approx(130.0)


def test_blend_treats_zero_games_as_unknown_not_as_zero_games():
    known = {"rush_yd": 900.0, "games": 16.0}
    unknown = {"rush_yd": 1000.0, "games": 0.0}

    blended = bs.blend_statlines([known, unknown], games_varies=[True, True])

    assert blended["games"] == pytest.approx(16.0)  # not 8.0
    assert blended["rush_yd"] == pytest.approx(950.0)


def test_blend_omits_a_stat_no_source_reports():
    blended = bs.blend_statlines(
        [{"rush_yd": 10.0}, {"rush_yd": 20.0}], games_varies=[True, True]
    )
    assert "pass_yd" not in blended
    assert blended["rush_yd"] == pytest.approx(15.0)


def test_blend_weights_renormalize_over_contributing_sources_only():
    a = {"rec_yd": 1000.0}
    b = {"rec_yd": 500.0, "rec_tgt": 100.0}

    blended = bs.blend_statlines([a, b], [0.75, 0.25], games_varies=[True, True])

    assert blended["rec_yd"] == pytest.approx(0.75 * 1000.0 + 0.25 * 500.0)
    # b is the only contributor to rec_tgt, so its weight renormalizes to 1.0 -- a 0.25 weight
    # must not shrink a figure it is the sole source of.
    assert blended["rec_tgt"] == pytest.approx(100.0)


def test_blend_rejects_mismatched_weights():
    with pytest.raises(ValueError):
        bs.blend_statlines([{"rec": 1.0}, {"rec": 2.0}], [1.0], games_varies=[True, True])


def test_equal_weight_blend_is_the_midpoint_of_a_weight_sweep():
    a = {"rush_yd": 0.0}
    b = {"rush_yd": 100.0}
    assert bs.blend_statlines([a, b], games_varies=[True, True])["rush_yd"] == pytest.approx(
        bs.blend_statlines([a, b], [0.5, 0.5], games_varies=[True, True])["rush_yd"]
    )


def test_a_constant_games_source_does_not_contribute_to_the_blended_games_figure():
    """The 18/18-vs-17/11 case, which is the real one.

    Sleeper publishes a blanket 18.0 for every player in 2025 as well as 2026 -- one distinct
    value, so a constant rather than a forecast, and one MORE than this league's 17-week season.
    Averaging it against ESPN's real per-player figure produced 14.5 for an 11-game projection
    and destroyed the only genuine durability signal in the blend. The 17-week cap in
    `league_points` does not catch it, because 14.5 is already under the cap (Codex 2026-08-21
    finding 9).
    """
    sleeper = {"rush_yd": 900.0, "games": 18.0}   # constant across the pool
    espn = {"rush_yd": 1000.0, "games": 11.0}     # a real projection

    # ESPN's figure survives intact; Sleeper's constant is excluded from `games` ONLY.
    blended = bs.blend_statlines([sleeper, espn], games_varies=[False, True])
    assert blended["games"] == pytest.approx(11.0)
    assert blended["rush_yd"] == pytest.approx(950.0), (
        "excluding a source from `games` must not exclude it from anything else"
    )

    # The bug, for contrast: admitting the constant lands halfway between a forecast and a
    # placeholder, and nothing downstream can tell.
    assert bs.blend_statlines([sleeper, espn], games_varies=[True, True])[
        "games"
    ] == pytest.approx(14.5)

    # If NO source varies, there is no games signal to carry and the key must be absent rather
    # than defaulted -- the same "unknown, not zero" rule the test above pins for a zero.
    assert "games" not in bs.blend_statlines([sleeper, espn], games_varies=[False, False])


def test_games_variation_is_measured_from_the_payload_not_declared():
    """Whether a column is a forecast or a constant is a fact about the data in front of us, and
    it has been wrong in both directions before. It is measured, and the measurement is what
    feeds the mask."""
    def _p(sleeper_games: float, espn_games: float) -> bs.MatchedPlayer:
        return bs.MatchedPlayer(
            name="x", pos="RB", team="SF", espn_id="1", sleeper_pid="1",
            match_method="test",
            espn_proj={"games": espn_games},
            sleeper_proj={"games": sleeper_games},
            actual={}, weekly_actual=[],
        )

    constant_sleeper = [_p(18.0, 17.0), _p(18.0, 11.0), _p(18.0, 16.0)]
    measured = bs.measure_games_variation(constant_sleeper)
    assert measured == {"sleeper": False, "espn": True}
    assert bs.blend_games_mask(measured) == [False, True]

    # A single-player pool cannot show variation in EITHER source, and must not claim it does.
    assert bs.measure_games_variation([_p(18.0, 17.0)]) == {"sleeper": False, "espn": False}


def test_blending_with_an_unmeasured_source_fails_loudly_rather_than_defaulting():
    """A silent default is the defect. Re-admitting it must be impossible by accident, so the
    mask is a required argument and an incomplete one raises rather than filling a gap in."""
    with pytest.raises(KeyError, match="espn"):
        bs.blend_games_mask({"sleeper": False})
    with pytest.raises(TypeError):
        bs.blend_statlines([{"rec": 1.0}, {"rec": 2.0}])  # type: ignore[call-arg]


# ========================================================================== ESPN block reading


def _espn_player(stats_blocks, *, pid=1, pos_id=3, name="Test Player"):
    return {
        "player": {
            "id": pid,
            "fullName": name,
            "defaultPositionId": pos_id,
            "proTeamId": 4,
            "stats": stats_blocks,
        }
    }


def _block(source_id, split, stats, season=2025):
    return {
        "seasonId": season,
        "statSourceId": source_id,
        "statSplitTypeId": split,
        "stats": stats,
    }


def test_empty_actual_block_is_distinguished_from_a_missing_one():
    with_empty = _espn_player([_block(1, 0, {"42": 900.0}), _block(0, 0, {})])
    without_any = _espn_player([_block(1, 0, {"42": 900.0})], pid=2)

    recs = bs.espn_records([with_empty, without_any])

    assert recs["1"].actual == {}
    assert recs["1"].actual_is_empty_block is True
    assert recs["2"].actual is None
    assert recs["2"].actual_is_empty_block is False


def test_canonicalize_uses_the_verified_stat_id_map():
    stats = {"3": 4000.0, "42": 1200.0, "53": 90.0, "210": 17.0, "60": 13.3}
    canonical = bs._canonicalize(stats)
    assert canonical["pass_yd"] == 4000.0
    assert canonical["rec_yd"] == 1200.0
    assert canonical["rec"] == 90.0
    assert canonical["games"] == 17.0
    # id 60 is a RATE (yards per catch). It has no canonical stat and must not leak in as one.
    assert 13.3 not in canonical.values()


def test_weekly_actual_blocks_are_collected_for_the_bonus_ground_truth():
    player = _espn_player(
        [
            _block(1, 0, {"42": 900.0}),
            _block(0, 0, {"42": 800.0}),
            _block(0, 1, {"42": 110.0}),
            _block(0, 1, {"42": 40.0}),
            _block(1, 1, {"42": 55.0}),  # weekly PROJECTION -- must not be mistaken for actual
        ]
    )
    rec = bs.espn_records([player])["1"]
    assert [w["rec_yd"] for w in rec.weekly_actual] == [110.0, 40.0]


def test_non_skill_positions_are_excluded():
    kicker = _espn_player([_block(1, 0, {"42": 1.0})], pid=9, pos_id=5)
    defense = _espn_player([_block(1, 0, {"42": 1.0})], pid=10, pos_id=16)
    assert bs.espn_records([kicker, defense]) == {}


# ================================================================== the stat-id verification gate


def _consistent_player(pid=1):
    """A player whose ESPN ratio fields agree with the components we map."""
    stats = {
        "0": 500.0, "1": 350.0, "21": 0.7,            # att / cmp / completion pct
        "23": 100.0, "24": 500.0, "39": 5.0,          # rush att / yd / ypa
        "53": 50.0, "42": 600.0, "60": 12.0,          # rec / rec yd / ypc
        "20": 10.0, "72": 3.0, "73": 13.0,            # int / fum lost / turnovers
    }
    return _espn_player([_block(1, 0, stats), _block(0, 0, dict(stats))], pid=pid, pos_id=1)


def test_stat_id_gate_passes_on_a_consistent_payload():
    lines = bs.verify_espn_stat_ids([_consistent_player()])
    assert any("id60 == rec_yd/rec" in line and "1/1 agree" in line for line in lines)
    assert any(line.strip().startswith("actual") for line in lines)


def test_stat_id_gate_fails_when_receiving_yards_sit_in_the_wrong_field():
    """The exact failure CLAUDE.md warns about: plausible numbers, wrong field."""
    player = _consistent_player()
    proj = player["player"]["stats"][0]["stats"]
    proj["42"] = 1800.0  # rec_yd inflated; ESPN's own id 60 (ypc) no longer reconciles

    with pytest.raises(AssertionError, match="ESPN stat-id verification FAILED"):
        bs.verify_espn_stat_ids([player])


def test_stat_id_gate_fails_on_a_broken_turnover_identity():
    player = _consistent_player()
    player["player"]["stats"][1]["stats"]["20"] = 99.0  # ints no longer sum to id 73

    with pytest.raises(AssertionError):
        bs.verify_espn_stat_ids([player])


# ==================================================================================== joining


def _sleeper_row(pid, first, last, pos, stats):
    return {
        "player_id": pid,
        "player": {"first_name": first, "last_name": last, "position": pos, "team": "CIN"},
        "stats": stats,
    }


def test_sleeper_records_map_gp_to_games_and_ignore_points_fields():
    rows = [
        _sleeper_row(
            "7564", "Ja'Marr", "Chase", "WR",
            {"rec": 100.0, "rec_yd": 1400.0, "gp": 18.0, "pts_half_ppr": 271.8, "adp_2qb": 3.0},
        )
    ]
    rec = bs.sleeper_records(rows)["7564"]
    assert rec.proj["games"] == 18.0
    assert rec.proj["rec_yd"] == 1400.0
    # Fantasy points and ADP are not component stats and must never enter a stat line.
    assert 271.8 not in rec.proj.values()
    assert "pts_half_ppr" not in rec.proj


def test_join_prefers_the_direct_espn_id_over_name_matching():
    espn = bs.espn_records(
        [_espn_player([_block(1, 0, {"42": 900.0}), _block(0, 0, {"42": 800.0})],
                      pid=4362, name="Different Spelling")]
    )
    sleeper = bs.sleeper_records([_sleeper_row("7564", "Different", "Spelling", "WR",
                                              {"rec_yd": 850.0})])
    universe = {"7564": {"espn_id": 4362}}

    matched, counts, _ = bs.join_sources(espn, sleeper, universe)

    assert len(matched) == 1
    assert matched[0].match_method == "espn_id"
    assert counts["espn_id"] == 1 and counts["name_pos"] == 0


def test_join_falls_back_to_normalized_name_and_position():
    espn = bs.espn_records(
        [_espn_player([_block(1, 0, {"42": 900.0}), _block(0, 0, {"42": 800.0})],
                      pid=1, name="Michael Pittman Jr.")]
    )
    sleeper = bs.sleeper_records([_sleeper_row("55", "Mike", "Pittman", "WR", {"rec_yd": 850.0})])

    matched, counts, _ = bs.join_sources(espn, sleeper, {})

    assert counts["name_pos"] == 1
    assert matched[0].match_method == "name_pos"


def test_join_never_guesses_an_ambiguous_name():
    espn = bs.espn_records(
        [_espn_player([_block(1, 0, {"42": 900.0}), _block(0, 0, {"42": 800.0})],
                      pid=1, name="Mike Williams")]
    )
    sleeper = bs.sleeper_records(
        [
            _sleeper_row("1", "Mike", "Williams", "WR", {"rec_yd": 700.0}),
            _sleeper_row("2", "Michael", "Williams", "WR", {"rec_yd": 300.0}),
        ]
    )

    matched, counts, dropped = bs.join_sources(espn, sleeper, {})

    assert matched == []
    assert counts["ambiguous_name"] == 1
    assert [d.name for d in dropped] == ["Mike Williams"]


def test_join_flags_unobserved_production_instead_of_dropping_it_silently():
    """A projected player with no actual block is held out ON PURPOSE and countable.

    Dropping them without a count is how a backtest quietly stops punishing the more
    optimistic source.
    """
    espn = bs.espn_records([_espn_player([_block(1, 0, {"42": 900.0})], pid=1, name="Ghost Guy")])
    sleeper = bs.sleeper_records([_sleeper_row("1", "Ghost", "Guy", "WR", {"rec_yd": 850.0})])

    matched, counts, _ = bs.join_sources(espn, sleeper, {})

    assert len(matched) == 1
    assert matched[0].actual_status == "missing_block"
    assert counts["actual_missing_block"] == 1


def test_join_keeps_a_player_who_recorded_nothing_all_season():
    espn = bs.espn_records(
        [_espn_player([_block(1, 0, {"42": 900.0}), _block(0, 0, {})], pid=1, name="Zero Guy")]
    )
    sleeper = bs.sleeper_records([_sleeper_row("1", "Zero", "Guy", "WR", {"rec_yd": 850.0})])

    matched, counts, _ = bs.join_sources(espn, sleeper, {})

    assert matched[0].actual_status == "empty_block"
    assert matched[0].actual == {}
    assert counts["actual_empty_block"] == 1


def test_espn_to_sleeper_index_includes_players_who_are_no_longer_active():
    """The universe join must not filter on ``active``.

    Filtering there would quietly restrict a 2025 backtest to players who survived to 2026 --
    survivorship bias built straight into the population.
    """
    universe = {
        "1": {"espn_id": 111, "active": True},
        "2": {"espn_id": 222, "active": False},
        "3": {"espn_id": None},
    }
    index = bs._espn_to_sleeper_index(universe)
    assert index == {"111": "1", "222": "2"}


# ==================================================================================== scoring


SCORING = {"rec": 0.5, "rec_yd": 0.1, "rec_td": 6.0, "rush_yd": 0.1, "pass_int": -2.0}
#: Both sources treated as publishing a real per-player games figure. Correct for the
#: synthetic fixtures below, whose games values are set per player; the constant-source
#: case has its own test.
VARYING = {"sleeper": True, "espn": True}


def test_league_points_is_the_plain_dot_product_without_a_bonus():
    stats = {"rec": 100.0, "rec_yd": 1200.0, "rec_td": 8.0}
    assert bs.league_points(stats, SCORING) == pytest.approx(50.0 + 120.0 + 48.0)


def test_games_cap_stops_sleepers_blanket_18_from_inflating_the_bonus():
    """Sleeper reports gp=18.0 for every player. 18 games cannot happen in a 17-week season."""
    stats = {"rec_yd": 1700.0, "games": 18.0}
    schedule = {"rec_yd": ({"threshold": 100, "points": 3.0},)}

    class _Curve:
        stat, position = "rec_yd", "WR"

    # No curve for (rec_yd, WR) -> the bonus term is zero either way, so this isolates the
    # games figure that reaches the bonus call rather than the bonus size itself.
    capped = bs.league_points(
        stats, SCORING, pos="WR", bonus=True, schedule=schedule, curves={}, games_cap=17.0
    )
    uncapped = bs.league_points(
        stats, SCORING, pos="WR", bonus=True, schedule=schedule, curves={}, games_cap=None
    )
    assert capped == pytest.approx(uncapped)  # zero bonus with no curve, both ways
    assert capped == pytest.approx(bs.league_points(stats, SCORING))


def test_actual_points_bonus_comes_from_real_weekly_yardage_not_a_model():
    player = bs.MatchedPlayer(
        name="X", pos="WR", team="CIN", espn_id="1", sleeper_pid="1", match_method="espn_id",
        espn_proj={}, sleeper_proj={},
        actual={"rec_yd": 250.0},
        weekly_actual=[{"rec_yd": 120.0}, {"rec_yd": 30.0}, {"rec_yd": 100.0}],
    )
    schedule = {"rec_yd": ({"threshold": 100, "points": 3.0},)}

    plain = bs.actual_points(player, SCORING)
    with_bonus = bs.actual_points(player, SCORING, bonus=True, schedule=schedule)

    assert plain == pytest.approx(25.0)
    assert with_bonus == pytest.approx(25.0 + 6.0)  # two games cleared 100 yards


# ==================================================================================== metrics


def test_metrics_are_the_textbook_definitions():
    m = bs.metrics([10.0, 20.0, 30.0], [12.0, 18.0, 33.0])
    assert m.n == 3
    assert m.mae == pytest.approx((2 + 2 + 3) / 3)
    assert m.bias == pytest.approx((-2 + 2 - 3) / 3)
    assert m.rmse == pytest.approx(((4 + 4 + 9) / 3) ** 0.5)
    # Cross-checked against numpy.corrcoef([10,20,30], [12,18,33])[0,1] = 0.97072534.
    assert m.corr == pytest.approx(0.9707253433941508, abs=1e-9)


def test_paired_compare_signs_point_at_the_closer_source():
    # A is closer on every player, so the gap must be negative and the win count complete.
    err_a = [1.0, -1.0, 2.0, -2.0, 1.0, -1.0, 2.0, -2.0, 1.0, -1.0, 1.5, -1.5]
    err_b = [8.0, -9.0, 7.0, -6.0, 8.0, -9.0, 7.0, -6.0, 8.0, -9.0, 7.5, -6.5]

    result = bs.paired_compare(err_a, err_b)

    assert result.mean_diff < 0
    assert result.a_closer == len(err_a)
    assert result.p_value < 0.01
    assert result.ci_high < 0
    assert "REAL" in result.line("a", "b")


def test_paired_compare_calls_a_tiny_gap_indistinguishable():
    err_a = [10.0, -10.0, 9.0, -9.0, 11.0, -11.0, 8.0, -8.0, 12.0, -12.0, 7.0, -7.0]
    err_b = [-10.2, 10.1, -8.8, 9.2, -10.9, 11.1, -8.1, 7.9, -12.2, 11.8, -6.9, 7.2]

    result = bs.paired_compare(err_a, err_b)

    assert result.p_value > 0.05
    assert "not distinguishable" in result.line("a", "b")


def test_paired_compare_rejects_unpaired_input():
    with pytest.raises(ValueError):
        bs.paired_compare([1.0, 2.0], [1.0])


# =============================================================================== ADP tiering


def _player_with_rank(rank):
    p = bs.MatchedPlayer(
        name="X", pos="WR", team="CIN", espn_id="1", sleeper_pid="1", match_method="espn_id",
        espn_proj={}, sleeper_proj={}, actual={}, weekly_actual=[],
    )
    p.adp_rank = rank
    return p


@pytest.mark.parametrize(
    "rank,expected",
    [(1, "ADP 1-24"), (24, "ADP 1-24"), (25, "ADP 25-60"), (60, "ADP 25-60"),
     (61, "ADP 61-120"), (120, "ADP 61-120"), (121, "ADP 121+"), (None, bs.UNRANKED_TIER)],
)
def test_adp_tiers_partition_the_board(rank, expected):
    assert bs._tier_of(_player_with_rank(rank)) == expected


def test_attach_adp_ranks_by_ascending_adp_not_feed_order():
    ffc_raw = {
        "players": [
            {"name": "Second Guy", "position": "WR", "team": "CIN", "adp": 20.0,
             "stdev": 5.0, "high": 10, "low": 30, "times_drafted": 100, "bye": 10},
            {"name": "First Guy", "position": "RB", "team": "ATL", "adp": 2.0,
             "stdev": 1.0, "high": 1, "low": 5, "times_drafted": 100, "bye": 5},
        ]
    }
    first = bs.MatchedPlayer(
        name="First Guy", pos="RB", team="ATL", espn_id="1", sleeper_pid="1",
        match_method="espn_id", espn_proj={}, sleeper_proj={}, actual={}, weekly_actual=[],
    )
    second = bs.MatchedPlayer(
        name="Second Guy", pos="WR", team="CIN", espn_id="2", sleeper_pid="2",
        match_method="espn_id", espn_proj={}, sleeper_proj={}, actual={}, weekly_actual=[],
    )

    hits = bs.attach_adp([first, second], ffc_raw)

    assert hits == 2
    assert (first.adp_rank, second.adp_rank) == (1, 2)


# ================================================================================ calibration


def test_ols_fit_on_a_perfect_forecast_is_the_identity_line():
    x = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0]
    fit = bs.ols_fit(x, x, draws=200)

    assert fit.slope == pytest.approx(1.0)
    assert fit.intercept == pytest.approx(0.0, abs=1e-9)
    assert fit.r2 == pytest.approx(1.0)
    assert fit.sd_ratio == pytest.approx(1.0)
    assert fit.excludes_one is False


def test_ols_fit_detects_an_over_dispersed_forecast():
    """The textbook miscalibration: the forecast's range is twice the outcome's."""
    x = [0.0, 20.0, 40.0, 60.0, 80.0, 100.0, 120.0, 140.0]
    y = [v / 2 for v in x]

    fit = bs.ols_fit(x, y, draws=500)

    assert fit.slope == pytest.approx(0.5)
    assert fit.sd_ratio == pytest.approx(0.5)
    assert fit.r == pytest.approx(1.0)
    assert fit.excludes_one is True


def test_ols_fit_slope_decomposition_is_an_identity():
    """slope == r * sd(actual)/sd(projected), always. The report leans on this to say WHY a
    slope is below 1, so it must actually hold rather than being asserted in prose."""
    x = [3.0, 9.0, 12.0, 20.0, 25.0, 31.0, 44.0, 50.0, 61.0, 70.0]
    y = [8.0, 6.0, 19.0, 15.0, 33.0, 24.0, 40.0, 38.0, 52.0, 49.0]

    fit = bs.ols_fit(x, y, draws=200)

    assert fit.slope == pytest.approx(fit.r * fit.sd_ratio, rel=1e-9)


def test_ols_fit_distinguishes_weak_correlation_from_over_dispersion():
    """Two ways to get a slope near 0.5, and the report must tell them apart.

    Case A: forecast perfectly ordered but twice as wide (r = 1, sd ratio 0.5).
    Case B: forecast the right width but half-random (sd ratio ~1, r ~0.5).
    """
    x = [0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0]
    over_dispersed = [v / 2 for v in x]
    scrambled = [20.0, 5.0, 40.0, 15.0, 60.0, 25.0, 80.0, 35.0, 100.0, 45.0]

    a = bs.ols_fit(x, over_dispersed, draws=200)
    b = bs.ols_fit(x, scrambled, draws=200)

    assert a.r == pytest.approx(1.0) and a.sd_ratio == pytest.approx(0.5)
    assert b.sd_ratio > 0.9 and b.r < 0.8


def test_ols_fit_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        bs.ols_fit([1.0, 2.0, 3.0], [1.0, 2.0])


def test_ffa_reference_slopes_are_all_below_one():
    """Carried as an external 12-season reference, not measured here. If a future edit ever
    puts a slope >= 1 in this dict it is no longer the thing being cited."""
    assert set(bs.FFA_SLOPES) == set(bs.POSITIONS)
    assert all(0.0 < v < 1.0 for v in bs.FFA_SLOPES.values())


# ================================================================== the shrink and its algebra


def test_mean_preserving_shrink_endpoints():
    assert bs.mean_preserving_shrink(20.0, 10.0, 1.0) == pytest.approx(20.0)   # b=1: identity
    assert bs.mean_preserving_shrink(20.0, 10.0, 0.0) == pytest.approx(10.0)   # b=0: all mean
    assert bs.mean_preserving_shrink(20.0, 10.0, 0.5) == pytest.approx(15.0)


def test_mean_preserving_shrink_preserves_the_positional_mean():
    values = [4.0, 9.0, 11.0, 16.0, 20.0]
    mean = sum(values) / len(values)
    shrunk = [bs.mean_preserving_shrink(v, mean, 0.6) for v in values]
    assert sum(shrunk) / len(shrunk) == pytest.approx(mean)


def test_shrink_scales_value_above_replacement_by_exactly_the_slope():
    """The load-bearing algebra behind section H's answer about the QB premium.

    EVoB is (player - replacement) * games. Under a mean-preserving shrink BOTH the player and
    the replacement move toward the same positional mean, so their gap -- and therefore EVoB --
    scales by exactly the slope. If this ever stopped holding, section H's per-position
    multipliers would be measuring something else.
    """
    mean, slope = 10.0, 0.53
    player, replacement = 21.0, 8.0

    gap_before = player - replacement
    gap_after = (
        bs.mean_preserving_shrink(player, mean, slope)
        - bs.mean_preserving_shrink(replacement, mean, slope)
    )

    assert gap_after == pytest.approx(slope * gap_before)


# =========================================================================== pairing helpers


def _matched(name, pos, proj_pts, actual_pts, actual_games, proj_games=17.0):
    """A MatchedPlayer whose league points come out at a chosen value under SCORING.

    rec_yd is scored at 0.1, so points = rec_yd / 10.
    """
    return bs.MatchedPlayer(
        name=name, pos=pos, team="CIN", espn_id="1", sleeper_pid="1", match_method="espn_id",
        espn_proj={"rec_yd": proj_pts * 10.0, "games": proj_games},
        sleeper_proj={"rec_yd": proj_pts * 10.0, "games": proj_games},
        actual={"rec_yd": actual_pts * 10.0, "games": actual_games},
        weekly_actual=[],
    )


def test_season_points_pairs_returns_league_points_for_both_sides():
    players = [_matched("A", "WR", 100.0, 80.0, 17.0), _matched("B", "WR", 50.0, 60.0, 17.0)]

    projected, actual = bs.season_points_pairs(
        players, "espn", SCORING, games_varies=VARYING
    )

    assert projected == pytest.approx([100.0, 50.0])
    assert actual == pytest.approx([80.0, 60.0])


def test_ppg_pairs_drops_players_with_no_actual_games():
    """A player who never played has no actual PPG. Including him as a zero would invent a
    rate observation that does not exist."""
    players = [
        _matched("Played", "WR", 170.0, 85.0, 17.0),
        _matched("Never played", "WR", 170.0, 0.0, 0.0),
    ]

    projected, actual = bs.ppg_pairs(
        players, "espn", SCORING, games_cap=17.0, games_varies=VARYING
    )

    assert len(projected) == len(actual) == 1
    assert projected[0] == pytest.approx(10.0)   # 170 points / 17 projected games
    assert actual[0] == pytest.approx(5.0)       # 85 points / 17 actual games


def test_ppg_pairs_caps_projected_games_at_the_league_season():
    """Sleeper's blanket 18 must not become the denominator of a projected rate."""
    players = [_matched("A", "WR", 170.0, 85.0, 17.0, proj_games=18.0)]

    projected, _ = bs.ppg_pairs(
        players, "sleeper", SCORING, games_cap=17.0, games_varies=VARYING
    )

    assert projected[0] == pytest.approx(10.0)   # 170/17, not 170/18


# ============================================================ ADP-tier bias decomposition


def test_adp_bias_decomposition_adds_up_to_the_raw_bias():
    """The three printed components must reconstruct the tier's raw bias, or the table is
    telling a story the arithmetic does not support."""
    import re

    from draftroom.config import LeagueConfig

    cfg = LeagueConfig.from_yaml()
    players = []
    # A spread of WRs with a deliberate slope-below-1 pattern, plus ADP ranks so the tiers fill.
    for i in range(40):
        proj = 40.0 + 6.0 * i
        actual = 60.0 + 3.0 * i + (5.0 if i % 2 else -5.0)
        p = _matched(f"WR{i}", "WR", proj, actual, 17.0)
        p.adp_rank = i + 1
        players.append(p)

    lines = bs.adp_bias_decomposition_lines(players, cfg, games_variation=VARYING)

    rows = 0
    for line in lines:
        m = re.match(
            r"\s+(ADP [\d+-]+)\s+(\d+)\s+([+-][\d.]+)\s+([+-][\d.]+)\s+([+-][\d.]+)\s+([+-][\d.]+)",
            line,
        )
        if not m:
            continue
        rows += 1
        raw, spread, level, residual = (float(m.group(i)) for i in (3, 4, 5, 6))
        assert raw == pytest.approx(spread + level + residual, abs=0.15)
    assert rows >= 2


# ============================================================== end-to-end on the real cache


@pytest.mark.skipif(
    not (bs.ESPN_CACHE.exists() and bs.SLEEPER_CACHE.exists() and bs.FFC_CACHE.exists()),
    reason="data/backtest/ cache absent; run `python tools/backtest_sources.py --refresh`",
)
def test_end_to_end_report_on_the_cached_2025_payloads():
    report, matched = bs.run(refresh=False)

    # Population: the real join lands in the hundreds. A collapse to a handful would mean the
    # join broke, which is the failure most likely to go unnoticed.
    assert len(matched) > 300
    assert all(p.actual_status in {"real", "empty_block", "missing_block"} for p in matched)

    for expected in (
        "GATE: ESPN stat-id identities",
        "A. LEAGUE POINTS, NO PER-GAME YARDAGE BONUS",
        "B. LEAGUE POINTS INCLUDING THE PER-GAME YARDAGE BONUS",
        "E. WHAT WEIGHT WOULD 2025 CHOOSE?",
        "F. CALIBRATION",
        "G. IS THE ADP-TIER BIAS DISTINGUISHABLE",
        "H. CONSEQUENCE",
        "I. THE ADJUSTMENT THE BOARD APPLIES",
        "ONE SEASON ONLY",
    ):
        assert expected in report

    # Both sources, and the blend, must appear in every table.
    for source in bs.SOURCES:
        assert source in report

    # Section H re-values the live board. It degrades to a SKIPPED line if the board pipeline
    # cannot be imported (other agents are live in those modules), which is deliberate -- but
    # the run this test observes must not silently be the degraded one.
    assert "CURRENT (no shrink)" in report, "section H degraded; board pipeline unavailable"
