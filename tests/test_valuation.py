"""Tests for the scoring and valuation core.

The property under test throughout is that a wrong number here is *loud*, not silent. Every
test below is either an economic invariant from CLAUDE.md's sanity-invariant gate, or a case
where the naive implementation gives a plausible-looking answer that is backwards.

Fixtures are built locally with plain dicts and synthetic pools: nothing here touches the
network, a cache, or another agent's in-progress module.
"""

from __future__ import annotations

import math

import pytest

from draftroom.config import DEFAULT_MANUAL_LEAGUE_PATH, LeagueConfig
from draftroom.prep.scoring import ScoringKeyError, score_all, score_statline
from draftroom.prep.stat_map import (
    YAHOO_STAT_IDS,
    MissingStatIdError,
    UnsupportedStatIdError,
    resolve,
    resolve_modifier,
)
from draftroom.valuation.evob import compute_draft_values, evob
from draftroom.valuation.replacement import (
    PlayerSeason,
    expected_games,
    man_games_demand,
    man_games_demand_detail,
    replacement_levels,
)

# --------------------------------------------------------------------------- fixtures

HALF_PPR = {
    "pass_yd": 0.04,
    "pass_td": 4.0,
    "pass_int": -1.0,
    "rush_yd": 0.1,
    "rush_td": 6.0,
    "rec": 0.5,
    "rec_yd": 0.1,
    "rec_td": 6.0,
    "fum_lost": -2.0,
}


def make_cfg(
    *,
    teams: int = 12,
    qb: int = 2,
    rb: int = 2,
    wr: int = 3,
    te: int = 1,
    flex_slots: int = 1,
    bench: int = 5,
    weeks: int = 17,
) -> LeagueConfig:
    return LeagueConfig(
        teams=teams,
        starters={"QB": qb, "RB": rb, "WR": wr, "TE": te},
        flex_slots=flex_slots,
        flex_eligible=frozenset({"RB", "WR", "TE"}),
        bench=bench,
        weeks=weeks,
        scoring=HALF_PPR,
    )


def linear_pool(pos: str, n: int, hi: float, lo: float) -> list[PlayerSeason]:
    """A straight-line PPG curve. Crude, but it makes rank arithmetic checkable by hand."""
    step = 0.0 if n <= 1 else (hi - lo) / (n - 1)
    return [
        PlayerSeason(player_id=f"{pos}{i + 1}", pos=pos, ppg=hi - step * i, name=f"{pos}{i + 1}")
        for i in range(n)
    ]


def convex_pool(pos: str, n: int, hi: float, lo: float, curve: float = 1.5) -> list[PlayerSeason]:
    """A convex decline: steep at the top, flat in the middle. Closer to a real position."""
    return [
        PlayerSeason(
            player_id=f"{pos}{i + 1}",
            pos=pos,
            ppg=lo + (hi - lo) * (1.0 - i / (n - 1)) ** curve,
            name=f"{pos}{i + 1}",
        )
        for i in range(n)
    ]


def realistic_pool() -> list[PlayerSeason]:
    """The pool shape used by tools/show_replacement.py, so tests and the tool agree."""
    return (
        linear_pool("QB", 32, 22.0, 10.0)
        + linear_pool("RB", 60, 20.0, 5.0)
        + linear_pool("WR", 80, 19.0, 5.0)
        + linear_pool("TE", 30, 14.0, 3.0)
    )


def deep_pool() -> list[PlayerSeason]:
    """Deep enough that a 16-team, 3-QB league never runs a position dry."""
    return (
        linear_pool("QB", 80, 22.0, 6.0)
        + linear_pool("RB", 120, 20.0, 3.0)
        + linear_pool("WR", 150, 19.0, 3.0)
        + linear_pool("TE", 80, 14.0, 2.0)
    )


# =========================================================================== 1. Harstad
# The single most important test in the file.


def test_harstad_per_game_fixture_beats_season_total_vbd():
    """83.2 pts in 7 games must outrank 84.2 pts in 16.

    Season-total VBD ranks them by the totals (84.2 > 83.2) and therefore backwards: the
    7-game player beat replacement by ~6 points every week he played, while the 16-game player
    was *below* replacement all season. The weeks player A missed get covered off a bench that
    is replacement-level anyway, which is exactly what the baseline already assumes.
    """
    a_points, a_games = 83.2, 7
    b_points, b_games = 84.2, 16
    a_ppg, b_ppg = a_points / a_games, b_points / b_games

    assert a_ppg == pytest.approx(11.8857, abs=1e-4)
    assert b_ppg == pytest.approx(5.2625, abs=1e-4)

    # Document the inversion this test exists to prevent.
    assert b_points > a_points, "season totals rank B first -- that is the bug"

    # A plausible TE baseline, computed rather than asserted: run the real replacement model
    # over a realistic TE pool under our provisional league.
    cfg = make_cfg()
    te_baseline = replacement_levels(realistic_pool(), cfg)["TE"].baseline_ppg
    assert 5.0 < te_baseline < 11.0, f"TE baseline {te_baseline} is not plausible"

    evob_a = evob(a_ppg, te_baseline, a_games)
    evob_b = evob(b_ppg, te_baseline, b_games)

    assert evob_a > evob_b
    assert evob_a > 0 > evob_b, (
        f"A should be clearly above replacement and B clearly below "
        f"(baseline {te_baseline:.2f}: A={evob_a:.1f}, B={evob_b:.1f})"
    )


@pytest.mark.parametrize("baseline", [3.0, 5.0, 6.5, 8.0, 9.5, 11.0])
def test_harstad_ordering_holds_at_every_plausible_baseline(baseline: float):
    """The ordering is not an artifact of one baseline choice.

    Algebraically A - B = 9*baseline - 1, so A wins for any baseline above 0.11 PPG. Anything
    in TE range is far past that.
    """
    evob_a = evob(83.2 / 7, baseline, 7)
    evob_b = evob(84.2 / 16, baseline, 16)
    assert evob_a > evob_b
    assert (evob_a - evob_b) == pytest.approx(9.0 * baseline - 1.0, abs=1e-6)


def test_harstad_ordering_survives_the_full_draft_value_pipeline():
    """Same result end to end, not just through the bare evob() helper."""
    cfg = make_cfg()
    players = realistic_pool() + [
        PlayerSeason(player_id="A", pos="TE", ppg=83.2 / 7, expected_games=7, name="Harstad A"),
        PlayerSeason(player_id="B", pos="TE", ppg=84.2 / 16, expected_games=16, name="Harstad B"),
    ]
    values = compute_draft_values(players, cfg)
    assert values["A"].evob > values["B"].evob
    # The decomposition the UI explains a pick with must be internally consistent.
    a = values["A"]
    assert a.evob == pytest.approx((a.ppg - a.baseline_ppg) * a.expected_games)
    assert a.dv == pytest.approx(a.evob - a.lam * a.sigma_season)


# ================================================================ 2. Parameterization


@pytest.mark.parametrize("pos", ["QB", "RB", "WR", "TE"])
def test_replacement_rank_is_monotonic_in_team_count(pos: str):
    """More teams means more man-games of demand, so replacement must get deeper.

    CLAUDE.md sanity invariant: "baselines move monotonically with team count and starter
    slots."
    """
    players = deep_pool()
    ranks = []
    for teams in (8, 10, 12, 14, 16):
        info = replacement_levels(players, make_cfg(teams=teams))[pos]
        assert not info.pool_exhausted, f"{pos} pool ran dry at {teams} teams"
        ranks.append(info.baseline_rank)

    assert ranks == sorted(ranks), f"{pos} ranks not non-decreasing: {ranks}"
    # Strict across the span: base demand doubles from 8 to 16 teams, which no plausible
    # re-shuffling of the flex blocks can offset.
    assert ranks[-1] > ranks[0], f"{pos} rank did not deepen from 8 to 16 teams: {ranks}"


@pytest.mark.parametrize("pos", ["QB", "RB", "WR", "TE"])
def test_replacement_rank_is_monotonic_in_starter_slots(pos: str):
    """More starting slots at a position means its replacement level gets deeper."""
    players = deep_pool()
    ranks = []
    for count in (1, 2, 3):
        cfg = make_cfg(**{pos.lower(): count})
        info = replacement_levels(players, cfg)[pos]
        assert not info.pool_exhausted
        ranks.append(info.baseline_rank)

    assert ranks == sorted(ranks), f"{pos} ranks not non-decreasing in starters: {ranks}"
    assert ranks[-1] > ranks[0], f"{pos} rank did not deepen from 1 to 3 starters: {ranks}"


def test_baseline_ppg_falls_as_replacement_gets_deeper():
    """The rank invariant is only useful if the PPG behind it moves the same way."""
    players = deep_pool()
    baselines = [
        replacement_levels(players, make_cfg(teams=teams))["RB"].baseline_ppg
        for teams in (8, 10, 12, 14, 16)
    ]
    assert baselines == sorted(baselines, reverse=True), baselines


def test_nothing_is_hardcoded_to_twelve_teams():
    """A 10-team, 1-QB, no-flex league must produce its own textbook numbers."""
    cfg = make_cfg(teams=10, qb=1, rb=2, wr=2, te=1, flex_slots=0)
    demand = man_games_demand(cfg)
    assert demand["QB"] == 10 * 1 * 17
    assert demand["WR"] == 10 * 2 * 17
    assert cfg.roster_size == 1 + 2 + 2 + 1 + 0 + 5


# ======================================================================== 3. The 2QB shift


def test_two_qb_league_roughly_doubles_qb_replacement_rank(capsys):
    """The whole edge, in one assertion.

    12 x 1 x 17 = 204 QB-games vs 12 x 2 x 17 = 408. At ~15.6 expected games per QB that is
    a crossing near QB14 vs near QB27 -- so an elite QB is being compared against a materially
    worse alternative, and his value jumps.
    """
    qbs = convex_pool("QB", 60, 24.0, 8.0)
    # Flex is RB/WR/TE only, so the QB baseline is untouched by flex allocation; still, give
    # the model the other positions so the config is exercised whole.
    players = qbs + linear_pool("RB", 60, 20.0, 5.0) + linear_pool("WR", 80, 19.0, 5.0) + linear_pool("TE", 30, 14.0, 3.0)

    cfg_1qb = make_cfg(qb=1)
    cfg_2qb = make_cfg(qb=2)

    info_1 = replacement_levels(players, cfg_1qb)["QB"]
    info_2 = replacement_levels(players, cfg_2qb)["QB"]

    elite_1 = compute_draft_values(players, cfg_1qb)["QB1"]
    elite_2 = compute_draft_values(players, cfg_2qb)["QB1"]

    print()
    print("  2QB SHIFT -------------------------------------------------------")
    print(f"  1-QB league : demand {info_1.man_games_demand:7.1f} man-games -> "
          f"replacement QB{info_1.baseline_rank}, baseline {info_1.baseline_ppg:5.2f} PPG")
    print(f"  2-QB league : demand {info_2.man_games_demand:7.1f} man-games -> "
          f"replacement QB{info_2.baseline_rank}, baseline {info_2.baseline_ppg:5.2f} PPG")
    print(f"  QB1 ({elite_1.ppg:.2f} PPG, {elite_1.expected_games:.1f} exp games) "
          f"EVoB: {elite_1.evob:6.1f} (1QB) -> {elite_2.evob:6.1f} (2QB)  "
          f"= +{elite_2.evob - elite_1.evob:.1f} pts, {elite_2.evob / elite_1.evob:.2f}x")
    print("  -----------------------------------------------------------------")

    ratio = info_2.baseline_rank / info_1.baseline_rank
    assert 1.8 <= ratio <= 2.2, (
        f"2QB replacement rank QB{info_2.baseline_rank} is {ratio:.2f}x the 1QB "
        f"QB{info_1.baseline_rank}; expected roughly double"
    )
    assert info_2.baseline_ppg < info_1.baseline_ppg - 2.0
    assert elite_2.evob > elite_1.evob * 1.4, (
        f"elite QB EVoB only moved {elite_1.evob:.1f} -> {elite_2.evob:.1f}"
    )
    assert elite_2.evob - elite_1.evob > 40.0


def test_two_qb_demand_is_exactly_the_arithmetic_in_claude_md():
    cfg = make_cfg(qb=2)
    assert man_games_demand(cfg, realistic_pool())["QB"] == 12 * 2 * 17 == 408


# ==================================================================== 4. Flex allocation


def test_flex_blocks_go_where_the_marginal_player_is_best():
    """A flex slot is filled by whoever is best *in that slot*, so the greedy compares raw
    PPG at each position's next-past-baseline player. Here RB falls off a cliff right after
    its starters and WR does not, so every block must go to WR."""
    cfg = make_cfg()
    players = (
        linear_pool("QB", 40, 22.0, 10.0)
        + [
            PlayerSeason(player_id=f"RB{i + 1}", pos="RB", ppg=20.0 - 0.2 * i if i < 30 else 4.0)
            for i in range(80)
        ]
        + linear_pool("WR", 90, 19.0, 8.0)
        + linear_pool("TE", 40, 12.0, 2.0)
    )

    detail = man_games_demand_detail(cfg, players)
    assert sum(detail.flex_blocks.values()) == cfg.teams * cfg.flex_slots == 12
    assert detail.flex_blocks["WR"] == 12
    assert detail.flex_blocks["RB"] == 0
    assert detail.flex_blocks["TE"] == 0
    assert detail.demand["WR"] == pytest.approx(12 * 3 * 17 + 12 * 17)
    assert detail.demand["RB"] == pytest.approx(12 * 2 * 17)
    assert not detail.warnings


def test_flex_blocks_follow_the_cliff_to_the_other_position():
    """Mirror image: put the cliff at WR and the blocks must move to RB."""
    cfg = make_cfg()
    players = (
        linear_pool("QB", 40, 22.0, 10.0)
        + linear_pool("RB", 90, 20.0, 9.0)
        + [
            PlayerSeason(player_id=f"WR{i + 1}", pos="WR", ppg=19.0 - 0.15 * i if i < 45 else 3.0)
            for i in range(90)
        ]
        + linear_pool("TE", 40, 12.0, 2.0)
    )
    detail = man_games_demand_detail(cfg, players)
    assert sum(detail.flex_blocks.values()) == 12
    assert detail.flex_blocks["RB"] == 12


def test_flex_allocation_splits_when_positions_are_close():
    """With a smooth realistic pool the blocks split, and the totals still reconcile."""
    cfg = make_cfg()
    detail = man_games_demand_detail(cfg, realistic_pool())

    assert sum(detail.flex_blocks.values()) == 12
    assert len(detail.trace) == 12
    # Every block's demand is `weeks` man-games and lands on a flex-eligible position.
    total_flex = sum(detail.demand[p] - detail.base[p] for p in detail.demand)
    assert total_flex == pytest.approx(cfg.teams * cfg.flex_slots * cfg.weeks)
    assert set(pos for pos, _ in detail.trace) <= cfg.flex_eligible
    assert detail.flex_blocks["QB"] == 0, "QB is not flex-eligible and must get nothing"
    # Greedy means the winning marginal PPG is non-increasing as blocks are consumed.
    marginals = [ppg for _, ppg in detail.trace]
    assert marginals == sorted(marginals, reverse=True), marginals


def test_flex_allocation_requires_a_player_pool():
    """Splitting the flex without a pool would mean inventing an allocation."""
    with pytest.raises(ValueError, match="needs a player pool"):
        man_games_demand(make_cfg(flex_slots=1))
    # No flex, no pool needed.
    assert man_games_demand(make_cfg(flex_slots=0))["TE"] == 12 * 1 * 17


def test_man_games_demand_is_base_plus_allocated_flex():
    cfg = make_cfg()
    detail = man_games_demand_detail(cfg, realistic_pool())
    for pos, base in detail.base.items():
        assert detail.demand[pos] == pytest.approx(
            base + detail.flex_blocks[pos] * cfg.weeks
        )


# =========================================================================== 5. Scoring


def test_score_statline_matches_a_hand_computed_value():
    stats = {
        "pass_yd": 4000,
        "pass_td": 30,
        "pass_int": 10,
        "rush_yd": 300,
        "rush_td": 3,
        "rec_tgt": 5,  # canonical but unscored -> contributes nothing
        "games": 16,
    }
    scoring = {"pass_yd": 0.04, "pass_td": 4.0, "pass_int": -1.0, "rush_yd": 0.1, "rush_td": 6.0}
    # 4000*0.04=160, 30*4=120, 10*-1=-10, 300*0.1=30, 3*6=18  ->  318.0
    assert score_statline(stats, scoring) == pytest.approx(318.0)


def test_unknown_key_in_the_stat_line_is_ignored():
    """Sources emit stats nobody scores. Dropping them is correct, not data loss."""
    scoring = {"rec": 0.5, "rec_yd": 0.1}
    assert score_statline({"rec": 100, "rec_yd": 1200, "snap_pct": 0.9}, scoring) == pytest.approx(
        170.0
    )


def test_non_canonical_scoring_key_is_an_error():
    """A scoring key comes from the league's own modifiers: a bad one means points are
    silently going missing upstream."""
    with pytest.raises(ScoringKeyError):
        score_statline({"rec": 10}, {"receptions": 0.5})
    with pytest.raises(ValueError):  # ScoringKeyError is a ValueError
        score_statline({"rec": 10}, {"rec": 0.5, "kick_ret_yd": 0.04})


def test_the_same_function_scores_projections_and_actuals():
    """Projections and actuals are the same shape, so the reconciliation gate is meaningful."""
    scoring = {"rush_yd": 0.1, "rush_td": 6.0, "rec": 0.5}
    projection = {"rush_yd": 1200.0, "rush_td": 9.0, "rec": 45.0}
    actual = {"rush_yd": 1200.0, "rush_td": 9.0, "rec": 45.0}
    assert score_statline(projection, scoring) == score_statline(actual, scoring) == 196.5


def test_score_all_handles_both_record_shapes():
    scoring = {"rec": 0.5, "rec_td": 6.0}
    as_mapping = {"p1": {"rec": 100, "rec_td": 10}, "p2": {"rec": 20, "rec_td": 1}}
    as_records = [
        {"player_id": "p1", "stats": {"rec": 100, "rec_td": 10}},
        {"player_id": "p2", "stats": {"rec": 20, "rec_td": 1}},
    ]
    expected = {"p1": 110.0, "p2": 16.0}
    assert score_all(as_mapping, scoring) == pytest.approx(expected)
    assert score_all(as_records, scoring) == pytest.approx(expected)


def test_missing_yahoo_stat_id_raises_rather_than_dropping():
    """CLAUDE.md: a stat_id in the modifiers but not the map is a hard pipeline failure."""
    assert resolve(4) == "pass_yd"
    assert resolve(99999) is None
    with pytest.raises(MissingStatIdError):
        resolve_modifier(99999)

    settings = _yahoo_settings_normalized()
    settings["stat_modifiers"].append({"stat_id": 57, "value": 1.0})  # not in the map
    with pytest.raises(MissingStatIdError):
        LeagueConfig.from_yahoo_settings(settings, dict(YAHOO_STAT_IDS))


def test_return_td_is_unsupported_not_silently_dropped():
    """stat_id 15 has no canonical stat. It raises, and can only be dropped deliberately."""
    with pytest.raises(UnsupportedStatIdError):
        resolve_modifier(15)

    settings = _yahoo_settings_normalized()
    settings["stat_modifiers"].append({"stat_id": 15, "value": 6.0})
    with pytest.raises(UnsupportedStatIdError):
        LeagueConfig.from_yahoo_settings(settings, dict(YAHOO_STAT_IDS))

    cfg = LeagueConfig.from_yahoo_settings(
        settings, dict(YAHOO_STAT_IDS), ignore_stat_ids=[15]
    )
    assert "ret_td" not in cfg.scoring
    assert any("15" in note for note in cfg.provenance)


def test_two_point_conversions_fan_out_to_all_three_canonical_stats():
    """Yahoo scores 2-pointers with one combined modifier; the canonical vocabulary splits
    them three ways. Mapping the id to any single one would undercount."""
    assert resolve_modifier(16) == ("pass_2pt", "rush_2pt", "rec_2pt")
    cfg = LeagueConfig.from_yahoo_settings(_yahoo_settings_normalized(), dict(YAHOO_STAT_IDS))
    assert cfg.scoring["pass_2pt"] == 2.0
    assert cfg.scoring["rush_2pt"] == 2.0
    assert cfg.scoring["rec_2pt"] == 2.0


# ================================================================ 6. LeagueConfig loaders


def _yahoo_settings_normalized() -> dict:
    """The documented (already-normalized) Yahoo settings shape. UNVERIFIED."""
    return {
        "num_teams": 12,
        "start_week": 1,
        "playoff_start_week": 15,
        "roster_positions": [
            {"position": "QB", "count": 2},
            {"position": "RB", "count": 2},
            {"position": "WR", "count": 3},
            {"position": "TE", "count": 1},
            {"position": "W/R/T", "count": 1},
            {"position": "BN", "count": 5},
            {"position": "IR", "count": 2},
        ],
        "stat_modifiers": [
            {"stat_id": 4, "value": 0.04},
            {"stat_id": 5, "value": 4},
            {"stat_id": 6, "value": -1},
            {"stat_id": 9, "value": 0.1},
            {"stat_id": 10, "value": 6},
            {"stat_id": 11, "value": 0.5},
            {"stat_id": 12, "value": 0.1},
            {"stat_id": 13, "value": 6},
            {"stat_id": 16, "value": 2},
            {"stat_id": 18, "value": -2},
        ],
    }


def _yahoo_settings_raw() -> dict:
    """Yahoo's actual XML-transliterated JSON: collections as {"0": {...}, "count": n},
    each element wrapped in a singleton dict, numbers as strings."""
    positions = _yahoo_settings_normalized()["roster_positions"]
    modifiers = _yahoo_settings_normalized()["stat_modifiers"]
    return {
        "num_teams": "12",
        "start_week": "1",
        "playoff_start_week": "15",
        "roster_positions": {
            **{str(i): {"roster_position": {**p, "count": str(p["count"])}} for i, p in enumerate(positions)},
            "count": len(positions),
        },
        "stat_modifiers": {
            "stats": {
                **{
                    str(i): {"stat": {"stat_id": str(m["stat_id"]), "value": str(m["value"])}}
                    for i, m in enumerate(modifiers)
                },
                "count": len(modifiers),
            }
        },
    }


@pytest.mark.parametrize(
    "settings_factory", [_yahoo_settings_normalized, _yahoo_settings_raw], ids=["normalized", "raw"]
)
def test_from_yahoo_settings_builds_the_right_league(settings_factory):
    cfg = LeagueConfig.from_yahoo_settings(
        settings_factory(), dict(YAHOO_STAT_IDS), draft_slot=7
    )

    assert cfg.teams == 12
    assert dict(cfg.starters) == {"QB": 2, "RB": 2, "WR": 3, "TE": 1}
    assert cfg.flex_slots == 1
    assert cfg.flex_eligible == frozenset({"RB", "WR", "TE"})
    assert cfg.bench == 5  # IR slots are not bench: they carry no lineup demand
    assert cfg.weeks == 14  # playoff_start_week 15 - start_week 1
    assert cfg.draft_slot == 7
    assert cfg.roster_size == 2 + 2 + 3 + 1 + 1 + 5 == 14

    assert cfg.scoring["pass_yd"] == pytest.approx(0.04)
    assert cfg.scoring["pass_td"] == 4.0
    assert cfg.scoring["pass_int"] == -1.0
    assert cfg.scoring["rec"] == 0.5
    assert cfg.scoring["fum_lost"] == -2.0
    assert "games" not in cfg.scoring


def test_from_yahoo_settings_rejects_a_miscounted_collection():
    settings = _yahoo_settings_raw()
    settings["roster_positions"]["count"] = 99
    with pytest.raises(ValueError, match="count"):
        LeagueConfig.from_yahoo_settings(settings, dict(YAHOO_STAT_IDS))


def test_from_yahoo_settings_handles_superflex():
    settings = _yahoo_settings_normalized()
    settings["roster_positions"] = [
        {"position": "QB", "count": 1},
        {"position": "RB", "count": 2},
        {"position": "WR", "count": 3},
        {"position": "TE", "count": 1},
        {"position": "Q/W/R/T", "count": 1},
        {"position": "BN", "count": 6},
    ]
    cfg = LeagueConfig.from_yahoo_settings(settings, dict(YAHOO_STAT_IDS))
    assert cfg.flex_eligible == frozenset({"QB", "RB", "WR", "TE"})
    assert dict(cfg.starters)["QB"] == 1


def test_from_yaml_reads_the_real_allendale_dad_league_settings():
    """Pins the CONFIRMED league configuration, read off Yahoo on 2026-08-17.

    These were provisional guesses until the settings page was actually read, and three of them were
    wrong: teams was 12 (really 10), bench was 5 (really 6), and the interception penalty was -1
    (the league overrode Yahoo's default to -2). Each silently moved every replacement level and
    therefore every ranking, so they are pinned here rather than left to drift.
    """
    cfg = LeagueConfig.from_yaml(DEFAULT_MANUAL_LEAGUE_PATH)
    assert cfg.teams == 10
    assert dict(cfg.starters) == {"QB": 2, "RB": 2, "WR": 3, "TE": 1}
    assert cfg.flex_slots == 1
    assert cfg.flex_eligible == frozenset({"RB", "WR", "TE"})
    assert cfg.bench == 6
    assert cfg.weeks == 17
    # 9 starters + 6 bench. The 2 IR slots cannot hold a healthy player, so they add no roster demand.
    assert cfg.roster_size == 15
    assert cfg.draft_slot is None, "2026 slot is not drawn until draft night"

    assert cfg.scoring["rec"] == 0.5, "half PPR"
    assert cfg.scoring["pass_td"] == 4.0
    assert cfg.scoring["pass_int"] == -2.0, (
        "the league overrode Yahoo's default of -1; doubling the penalty in a league that starts "
        "two QBs lands twice on every roster"
    )
    assert cfg.scoring["pass_yd"] == 0.04
    assert cfg.scoring["rush_yd"] == 0.1
    assert cfg.scoring["rec_yd"] == 0.1
    assert cfg.scoring["fum_lost"] == -2.0


def test_the_league_has_twenty_starting_qb_slots():
    """The single number the whole model hangs on.

    10 teams x 2 mandatory QB slots. It is why replacement-level QB sits around QB22 here rather than
    QB11, and why every publicly available ranking is wrong for this league.
    """
    cfg = LeagueConfig.from_yaml(DEFAULT_MANUAL_LEAGUE_PATH)
    assert cfg.teams * cfg.starters["QB"] == 20


def test_config_rejects_a_non_canonical_scoring_key():
    with pytest.raises(ScoringKeyError):
        make_cfg().replace(scoring={"receptions": 0.5})


def test_config_rejects_an_impossible_draft_slot():
    with pytest.raises(ValueError, match="draft_slot"):
        make_cfg(teams=12).replace(draft_slot=13)


# ============================================================ expected games + plumbing


def test_expected_games_uses_the_positional_prior_and_honours_overrides():
    assert expected_games("QB") == pytest.approx(15.6)
    assert expected_games("RB") == pytest.approx(13.9)
    assert expected_games("WR") == pytest.approx(14.5)
    assert expected_games("TE") == pytest.approx(14.2)
    assert expected_games("RB", 4.0) == 4.0
    # The prior is a rate, not a count: a shorter season scales it.
    assert expected_games("QB", weeks=14) == pytest.approx(15.6 * 14 / 17)
    # And it can never exceed the season.
    assert expected_games("QB", 25.0, weeks=17) == 17.0
    with pytest.raises(ValueError, match="no expected-games prior"):
        expected_games("K")


def test_baseline_ppg_averages_three_ranks_for_stability():
    cfg = make_cfg(flex_slots=0)
    players = linear_pool("TE", 40, 14.0, 3.0) + linear_pool("QB", 40, 22.0, 10.0) + linear_pool(
        "RB", 40, 20.0, 5.0
    ) + linear_pool("WR", 60, 19.0, 5.0)
    info = replacement_levels(players, cfg)["TE"]
    pool = sorted((p for p in players if p.pos == "TE"), key=lambda p: -p.ppg)
    ranks = info.baseline_ranks_averaged
    assert len(ranks) == 3
    assert info.baseline_ppg == pytest.approx(sum(pool[r - 1].ppg for r in ranks) / 3)


def test_shallow_pool_is_flagged_not_silently_accepted():
    """A pool too thin to cover demand yields a baseline that overstates replacement
    quality. It must announce itself."""
    cfg = make_cfg(flex_slots=0)
    players = linear_pool("QB", 5, 22.0, 18.0) + linear_pool("RB", 40, 20.0, 5.0) + linear_pool(
        "WR", 60, 19.0, 5.0
    ) + linear_pool("TE", 30, 14.0, 3.0)
    info = replacement_levels(players, cfg)["QB"]
    assert info.pool_exhausted is True
    assert info.baseline_rank == 5


def test_draft_value_risk_penalty_is_inert_until_lambda_is_set():
    cfg = make_cfg()
    players = realistic_pool()
    neutral = compute_draft_values(players, cfg, lam=0.0)
    assert all(v.dv == pytest.approx(v.evob) for v in neutral.values())

    risky = [
        PlayerSeason(player_id="RB1", pos="RB", ppg=20.0, sigma_ppg=4.0),
        PlayerSeason(player_id="RB2", pos="RB", ppg=20.0, sigma_ppg=1.0),
    ] + [p for p in players if p.player_id not in {"RB1", "RB2"}]
    averse = compute_draft_values(risky, cfg, lam=0.5)
    assert averse["RB1"].evob == pytest.approx(averse["RB2"].evob)
    assert averse["RB1"].dv < averse["RB2"].dv, "risk aversion must penalise the volatile one"
    assert averse["RB1"].sigma_source == "from_sigma_ppg"
    assert averse["RB1"].sigma_season == pytest.approx(4.0 * averse["RB1"].expected_games)


def test_valuing_a_position_the_league_does_not_roster_raises():
    cfg = make_cfg()
    players = realistic_pool() + [PlayerSeason(player_id="K1", pos="K", ppg=8.0, expected_games=17)]
    with pytest.raises(KeyError, match="no replacement level"):
        compute_draft_values(players, cfg)


def test_top_qb_lands_in_the_top_eight_overall_in_this_league(capsys):
    """CLAUDE.md sanity invariant: with two mandatory QB slots the best QB is a top-8 overall
    asset and 10-14 QBs belong in the top 30."""
    cfg = make_cfg()
    values = compute_draft_values(realistic_pool(), cfg)
    ordered = sorted(values.values(), key=lambda v: -v.evob)

    qb_rank_overall = next(i for i, v in enumerate(ordered, 1) if v.pos == "QB")
    qbs_in_top_30 = sum(1 for v in ordered[:30] if v.pos == "QB")
    print(f"\n  top QB is overall #{qb_rank_overall}; {qbs_in_top_30} QBs in the top 30")

    assert qb_rank_overall <= 8, f"best QB is only overall #{qb_rank_overall}"
    assert 10 <= qbs_in_top_30 <= 14, f"{qbs_in_top_30} QBs in the top 30"


def test_the_evob_submodule_is_not_shadowed_by_the_evob_function():
    """Regression guard, and it caught a real bug.

    ``valuation/__init__.py`` originally re-exported the ``evob`` function, which rebinds the
    package attribute ``draftroom.valuation.evob`` from the submodule to the function. Every
    ``import draftroom.valuation.evob as m; m.DraftValue`` then fails, and -- how this was
    found -- monkeypatching the module in a test harness silently does nothing.
    """
    import types

    import draftroom.valuation
    import draftroom.valuation.evob as module

    assert isinstance(module, types.ModuleType)
    assert isinstance(draftroom.valuation.evob, types.ModuleType)
    assert callable(module.evob)
    assert module.evob(10.0, 6.0, 15.0) == pytest.approx(60.0)


def test_math_is_finite_everywhere():
    values = compute_draft_values(realistic_pool(), make_cfg())
    assert all(math.isfinite(v.evob) and math.isfinite(v.dv) for v in values.values())
