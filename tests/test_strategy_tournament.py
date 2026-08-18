"""Tests for `tools/strategy_tournament.py` -- the QB-timing strategy tournament.

Everything here is OFFLINE and synthetic (hand-built `BoardPlayer`s, a small hand-built
`LeagueConfig`) -- no `nflreadpy` call, no network, per this repo's own rule that a live fetch
must never run inside a test (CLAUDE.md: it also has a side effect on `data/raw/` cache
resolution). `build_historical_board`/`build_projection_board` are exercised only via mocked/no
network paths implicitly -- they're not called here at all; what's tested is the strategy logic
and the draft-loop mechanics, which are the parts this module actually owns.
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
for p in (str(BACKEND_DIR), str(TOOLS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402

from draftroom.config import LeagueConfig  # noqa: E402
from draftroom.draft.recommend import BoardPlayer  # noqa: E402

import strategy_tournament as st  # noqa: E402

TEAMS = 10
STARTERS = {"QB": 2, "RB": 2, "WR": 3, "TE": 1}
FLEX = frozenset({"RB", "WR", "TE"})
SCORING = {
    "pass_yd": 0.04, "pass_td": 4.0, "pass_int": -2.0,
    "rush_yd": 0.1, "rush_td": 6.0,
    "rec": 0.5, "rec_yd": 0.1, "rec_td": 6.0,
    "fum_lost": -2.0,
}


def make_cfg(**overrides) -> LeagueConfig:
    payload: dict = dict(
        teams=TEAMS, starters=dict(STARTERS), flex_slots=1, flex_eligible=FLEX,
        bench=6, weeks=17, scoring=dict(SCORING),
    )
    payload.update(overrides)
    return LeagueConfig(**payload)


def player(pid: str, pos: str, dv: float, *, adp: float | None = None, stdev: float = 3.0) -> BoardPlayer:
    return BoardPlayer(
        player_id=pid, name=pid, pos=pos, team="FA", bye=None,
        adp=adp if adp is not None else (200.0 - dv), stdev=stdev, dv=dv, dv_sd=0.0,
    )


# ============================================================================ qb_startable_rank


def test_qb_startable_rank_matches_documented_value():
    """CLAUDE.md's own claim: at this league's real settings (10 teams, 2 QB, 17 weeks), the
    real-world man-games crossing is QB22 (before durability nudges it a bit further in a real
    backtest) -- the preseason-prior-only formula should reproduce that exact number."""
    cfg = make_cfg()
    assert st.qb_startable_rank(cfg) == 22


def test_qb_startable_rank_scales_with_demand():
    """Updated 2026-08-18 for the rank-conditional availability curve: doubling the man-games
    demand must deepen the startable rank by MORE than double, not exactly double.

    "Exactly double" was only true under the old FLAT per-position prior (every QB rank
    contributed the same ~15.6 games, so man-games supply was linear in rank and doubling
    demand doubled the crossing rank arithmetically). The real, fitted curve's games-per-rank
    shrinks fast past the top of the position (QB rank 25-40 averages far fewer games than
    rank 1-15 -- see replacement.py's EXPECTED_GAMES_CURVE), so each additional rank past the
    original crossing supplies fewer man-games than the ranks before it. Reaching double the
    demand therefore needs MORE than double the ranks -- 96, not 44, at 20 teams -- which is
    exactly the shape the games-played fix exists to capture, not a bug in this test.
    """
    cfg10 = make_cfg()
    cfg20 = make_cfg(teams=20)
    rank_10 = st.qb_startable_rank(cfg10)
    rank_20 = st.qb_startable_rank(cfg20)
    assert rank_10 == 22
    assert rank_20 == 96
    assert rank_20 > 2 * rank_10, (
        f"a realistic (non-flat) availability curve must need MORE than double the rank to "
        f"cover double the demand: {rank_10} -> {rank_20}"
    )
    # Fewer weeks -> less demand AND smaller per-rank availability (both scale by weeks/17,
    # uniformly across every rank) -> the crossing rank itself is unchanged.
    cfg2 = make_cfg(weeks=8)
    assert st.qb_startable_rank(cfg2) == st.qb_startable_rank(make_cfg())


# ==================================================================================== build_qb_rank


def test_build_qb_rank_orders_by_dv_descending_and_excludes_other_positions():
    players = [
        player("qb_best", "QB", 150.0),
        player("rb1", "RB", 300.0),  # highest dv overall, but not a QB -- must not appear
        player("qb_mid", "QB", 90.0),
        player("qb_worst", "QB", 10.0),
    ]
    ranks = st.build_qb_rank(players)
    assert ranks == {"qb_best": 1, "qb_mid": 2, "qb_worst": 3}
    assert "rb1" not in ranks


# ==================================================================================== strategy_pick


def _have_all(cfg: LeagueConfig, my_slot: int, my_qb: int = 0) -> dict[int, dict[str, int]]:
    have = {t: {} for t in range(1, cfg.teams + 1)}
    have[my_slot] = {"QB": my_qb}
    return have


def test_deadline_strategy_takes_best_value_before_deadline_even_if_qb_is_worse():
    """Before the deadline round, QB competes on dv like anything else -- a strategy with
    qb_deadline_round=6 must NOT force QB in round 3 just because a QB exists."""
    cfg = make_cfg()
    strat = st.Strategy("qb_early", 6)
    available = [player("qb1", "QB", 50.0), player("rb1", "RB", 120.0)]
    have = _have_all(cfg, my_slot=1, my_qb=0)
    pick_no = 3 * cfg.teams - (cfg.teams - 1)  # round 3, slot 1's pick
    chosen = st.strategy_pick(
        strat, available, have, my_slot=1, pick_no=pick_no, cfg=cfg,
        qb_rank={"qb1": 1}, qb_startable_rank_cutoff=22,
    )
    assert chosen == "rb1"  # higher dv wins -- deadline hasn't bound yet


def test_deadline_strategy_forces_qb_at_deadline_even_if_worse():
    """At/after the deadline round, with QB need unmet, the ONLY legal-by-rule pick is the best
    available QB -- even though a non-QB has higher dv."""
    cfg = make_cfg()
    strat = st.Strategy("qb_early", 6)
    available = [player("qb1", "QB", 50.0), player("rb1", "RB", 120.0)]
    have = _have_all(cfg, my_slot=1, my_qb=0)
    pick_no = st.snake.overall_pick(cfg.teams, 6, 1)  # exactly round 6, slot 1
    chosen = st.strategy_pick(
        strat, available, have, my_slot=1, pick_no=pick_no, cfg=cfg,
        qb_rank={"qb1": 1}, qb_startable_rank_cutoff=22,
    )
    assert chosen == "qb1"


def test_qb_need_already_met_ignores_deadline_and_reactive_logic():
    """Once both starting QB slots are filled, EVERY strategy (fixed-round or reactive)
    collapses to pure best-value -- this is the guarantee that a strategy never drafts a 3rd/4th
    QB out of leftover positional logic."""
    cfg = make_cfg()
    available = [player("qb1", "QB", 500.0), player("rb1", "RB", 10.0)]
    have = _have_all(cfg, my_slot=1, my_qb=2)  # need already met
    for strat in (st.Strategy("qb_elite", 4), st.Strategy("qb_never_below_line", None, reactive=True)):
        chosen = st.strategy_pick(
            strat, available, have, my_slot=1, pick_no=1, cfg=cfg,
            qb_rank={"qb1": 1}, qb_startable_rank_cutoff=22,
        )
        assert chosen == "qb1"  # best value overall, not "avoid QB because need is met"


def test_best_value_never_forces_qb_regardless_of_scarcity_or_round():
    """The control: qb_deadline_round=None, reactive=False must NEVER force a QB, even in the
    most extreme scarcity (every other team has already used all its QB slots, one startable QB
    left, my pick is right before a long gap) -- the only lever that exists is dv."""
    cfg = make_cfg()
    strat = st.Strategy("best_value", None, reactive=False)
    have = {t: {"QB": 2} for t in range(1, cfg.teams + 1)}
    have[1] = {"QB": 0}  # I'm the only one who still needs QBs, and only 1 remains
    available = [player("qb1", "QB", 400.0), player("wr1", "WR", 10.0)]
    chosen = st.strategy_pick(
        strat, available, have, my_slot=1, pick_no=1, cfg=cfg,
        qb_rank={"qb1": 1}, qb_startable_rank_cutoff=22,
    )
    assert chosen == "qb1"  # only because it also has the highest dv here
    # Now flip the dv so the QB is NOT the best value -- best_value must still skip it.
    available2 = [player("qb1", "QB", 5.0), player("wr1", "WR", 400.0)]
    chosen2 = st.strategy_pick(
        strat, available2, have, my_slot=1, pick_no=1, cfg=cfg,
        qb_rank={"qb1": 1}, qb_startable_rank_cutoff=22,
    )
    assert chosen2 == "wr1"


def test_reactive_trigger_fires_when_startable_supply_meets_leaguewide_demand():
    cfg = make_cfg()
    strat = st.Strategy("qb_never_below_line", None, reactive=True)
    # All 10 teams (including mine) still need both QB slots: leaguewide unfilled = 10*2 = 20.
    have = {t: {"QB": 0} for t in range(1, cfg.teams + 1)}
    # Only 18 startable QBs remain -- supply already BELOW demand -- must trigger regardless of gap.
    available = [player(f"qb{i}", "QB", 100.0 - i) for i in range(18)] + [player("wr1", "WR", 500.0)]
    qb_rank = {p.player_id: i + 1 for i, p in enumerate(available) if p.pos == "QB"}
    pick_no = st.snake.overall_pick(cfg.teams, 1, 1)  # round 1, slot 1 -- gap to next turn is large
    chosen = st.strategy_pick(
        strat, available, have, my_slot=1, pick_no=pick_no, cfg=cfg,
        qb_rank=qb_rank, qb_startable_rank_cutoff=22,
    )
    assert chosen.startswith("qb")  # forced, even though wr1 has far higher dv


def test_reactive_trigger_does_not_fire_with_ample_startable_supply():
    cfg = make_cfg()
    strat = st.Strategy("qb_never_below_line", None, reactive=True)
    have = {t: {"QB": 0} for t in range(1, cfg.teams + 1)}  # leaguewide unfilled = 10*2 = 20
    # Round 1, slot 1: next turn is pick 20 (round 2, snake), so gap = 20 - 1 - 1 = 18.
    # 45 startable QBs remain -- comfortably more than unfilled(20) + gap(18) = 38 -- so the
    # line is nowhere close and best-value should win.
    available = [player(f"qb{i}", "QB", 50.0 - i) for i in range(45)] + [player("wr1", "WR", 500.0)]
    qb_rank = {p.player_id: i + 1 for i, p in enumerate(available) if p.pos == "QB"}
    pick_no = st.snake.overall_pick(cfg.teams, 1, 1)
    chosen = st.strategy_pick(
        strat, available, have, my_slot=1, pick_no=pick_no, cfg=cfg,
        qb_rank=qb_rank, qb_startable_rank_cutoff=44,
    )
    assert chosen == "wr1"


def test_reactive_trigger_ignores_qbs_below_the_startable_cutoff():
    """A remaining QB ranked worse than `qb_startable_rank_cutoff` (a token QB4/QB5 nobody would
    ever start) must NOT count as supply -- this is the exact bug caught in dry-run testing
    (an earlier version counted every leftover QB regardless of quality and the trigger never
    fired)."""
    cfg = make_cfg()
    strat = st.Strategy("qb_never_below_line", None, reactive=True)
    have = {t: {"QB": 0} for t in range(1, cfg.teams + 1)}
    # Only 3 QBs remain in the pool, but their qb_rank (60, 61, 62) is far below the cutoff (22)
    # -- none of them count as "startable" supply, so the trigger must still fire.
    available = [
        player("qb_deep1", "QB", 5.0), player("qb_deep2", "QB", 4.0), player("qb_deep3", "QB", 3.0),
        player("wr1", "WR", 500.0),
    ]
    qb_rank = {"qb_deep1": 60, "qb_deep2": 61, "qb_deep3": 62}
    pick_no = st.snake.overall_pick(cfg.teams, 1, 1)
    chosen = st.strategy_pick(
        strat, available, have, my_slot=1, pick_no=pick_no, cfg=cfg,
        qb_rank=qb_rank, qb_startable_rank_cutoff=22,
    )
    assert chosen.startswith("qb_deep")  # forced despite low dv -- 0 startable QBs is the point


# ==================================================================================== qb_one_elite_one_cheap


def test_elite_one_cheap_deadline_is_round_one_and_stays_forced_in_later_rounds_too():
    """ELITE_QB_DEADLINE_ROUND == 1 (see the constant's docstring: elite QBs are gone from the
    WHOLE draft, not just this team's board, well before round 4) -- the rule engages on a
    strategy's very first pick, and the `>=` comparison means it stays engaged at any later
    round too, as long as my_qb is still 0 and an elite QB happens to still be sitting there."""
    cfg = make_cfg()
    strat = st.Strategy("qb_one_elite_one_cheap", elite_one_cheap=True)
    have = _have_all(cfg, my_slot=1, my_qb=0)
    available = [player("qb_elite", "QB", 50.0), player("rb1", "RB", 120.0)]
    for rnd in (1, 3):
        pick_no = st.snake.overall_pick(cfg.teams, rnd, 1)
        chosen = st.strategy_pick(
            strat, available, have, my_slot=1, pick_no=pick_no, cfg=cfg,
            qb_rank={"qb_elite": 1}, qb_startable_rank_cutoff=22,
        )
        assert chosen == "qb_elite", f"round {rnd}"


def test_elite_one_cheap_forces_qb_at_deadline_only_if_elite_tier_present():
    cfg = make_cfg()
    strat = st.Strategy("qb_one_elite_one_cheap", elite_one_cheap=True)
    have = _have_all(cfg, my_slot=1, my_qb=0)
    pick_no = st.snake.overall_pick(cfg.teams, st.ELITE_QB_DEADLINE_ROUND, 1)

    # Elite tier (rank <= ELITE_QB_RANK_CUTOFF) still on the board -- forced, despite lower dv.
    available = [player("qb_elite", "QB", 50.0), player("rb1", "RB", 120.0)]
    chosen = st.strategy_pick(
        strat, available, have, my_slot=1, pick_no=pick_no, cfg=cfg,
        qb_rank={"qb_elite": st.ELITE_QB_RANK_CUTOFF}, qb_startable_rank_cutoff=22,
    )
    assert chosen == "qb_elite"


def test_elite_one_cheap_does_not_reach_for_a_mediocre_qb_once_elite_tier_is_gone():
    """The whole point of the compound rule: it never substitutes a below-elite QB just to hit
    the deadline -- if rank 1-3 is gone, it falls through to best value like the control."""
    cfg = make_cfg()
    strat = st.Strategy("qb_one_elite_one_cheap", elite_one_cheap=True)
    have = _have_all(cfg, my_slot=1, my_qb=0)
    pick_no = st.snake.overall_pick(cfg.teams, st.ELITE_QB_DEADLINE_ROUND, 1)
    available = [player("qb_mediocre", "QB", 20.0), player("rb1", "RB", 50.0)]
    chosen = st.strategy_pick(
        strat, available, have, my_slot=1, pick_no=pick_no, cfg=cfg,
        qb_rank={"qb_mediocre": st.ELITE_QB_RANK_CUTOFF + 5}, qb_startable_rank_cutoff=22,
    )
    assert chosen == "rb1"  # NOT qb_mediocre -- it would win on raw dv if this rule "reached"


def test_elite_one_cheap_second_qb_uses_the_reactive_scarcity_trigger():
    """Once the elite QB1 is rostered (my_qb == 1), the second QB slot must behave exactly like
    `qb_never_below_line` -- forced only when startable supply meets leaguewide demand."""
    cfg = make_cfg()
    strat = st.Strategy("qb_one_elite_one_cheap", elite_one_cheap=True)
    have = {t: {"QB": 0} for t in range(1, cfg.teams + 1)}
    have[1] = {"QB": 1}  # elite QB1 already rostered
    # Ample supply -- 45 startable QBs vs. 19 leaguewide unfilled (9 other teams x2 + my 1) plus
    # a large gap at round 1 -- should NOT trigger.
    available = [player(f"qb{i}", "QB", 50.0 - i) for i in range(45)] + [player("wr1", "WR", 500.0)]
    qb_rank = {p.player_id: i + 1 for i, p in enumerate(available) if p.pos == "QB"}
    pick_no = st.snake.overall_pick(cfg.teams, 1, 1)
    chosen = st.strategy_pick(
        strat, available, have, my_slot=1, pick_no=pick_no, cfg=cfg,
        qb_rank=qb_rank, qb_startable_rank_cutoff=44,
    )
    assert chosen == "wr1"

    # Now starve supply down to right at the line -- must trigger.
    available_scarce = [player("qb_last", "QB", 5.0), player("wr1", "WR", 500.0)]
    chosen2 = st.strategy_pick(
        strat, available_scarce, have, my_slot=1, pick_no=pick_no, cfg=cfg,
        qb_rank={"qb_last": 1}, qb_startable_rank_cutoff=22,
    )
    assert chosen2 == "qb_last"


# ==================================================================================== run_one_draft integration


def _small_universe(cfg: LeagueConfig) -> list[BoardPlayer]:
    """Enough players to complete a full draft at this (small) league's roster_size, with QB
    plentiful enough that a deadline strategy can always find one."""
    players: list[BoardPlayer] = []
    for i in range(30):
        players.append(player(f"qb{i}", "QB", 200.0 - i))
    for i in range(60):
        players.append(player(f"rb{i}", "RB", 195.0 - i))
    for i in range(80):
        players.append(player(f"wr{i}", "WR", 190.0 - i))
    for i in range(30):
        players.append(player(f"te{i}", "TE", 150.0 - i))
    return players


def test_run_one_draft_fills_every_roster_slot_and_deadline_strategy_completes_qb2_on_time():
    cfg = make_cfg()
    players = _small_universe(cfg)
    draft_players_by_id = {p.player_id: p for p in players}
    resolved = {p.player_id: (p.adp, p.stdev, p.pos) for p in players}
    qb_rank = st.build_qb_rank(players)
    cutoff = st.qb_startable_rank(cfg)

    strat = st.Strategy("qb_elite", 4)
    rosters = st.run_one_draft(
        seed=1, strategy=strat, strategy_slot=3, draft_players_by_id=draft_players_by_id,
        resolved=resolved, cfg=cfg, qb_rank=qb_rank, qb_startable_rank_cutoff=cutoff,
    )
    for slot in range(1, cfg.teams + 1):
        assert len(rosters[slot]) == cfg.roster_size

    my_ids = rosters[3]
    qb_seen = 0
    round_of_2nd_qb = None
    for rnd, pid in enumerate(my_ids, start=1):
        if draft_players_by_id[pid].pos == "QB":
            qb_seen += 1
            if qb_seen == 2:
                round_of_2nd_qb = rnd
                break
    assert round_of_2nd_qb is not None
    assert round_of_2nd_qb <= 4  # qb_elite's deadline


def test_run_one_draft_baseline_bot_only_has_no_strategy_seat():
    cfg = make_cfg()
    players = _small_universe(cfg)
    draft_players_by_id = {p.player_id: p for p in players}
    resolved = {p.player_id: (p.adp, p.stdev, p.pos) for p in players}
    qb_rank = st.build_qb_rank(players)
    cutoff = st.qb_startable_rank(cfg)

    rosters = st.run_one_draft(
        seed=2, strategy=None, strategy_slot=None, draft_players_by_id=draft_players_by_id,
        resolved=resolved, cfg=cfg, qb_rank=qb_rank, qb_startable_rank_cutoff=cutoff,
    )
    assert len(rosters) == cfg.teams
    for slot in range(1, cfg.teams + 1):
        assert len(rosters[slot]) == cfg.roster_size
    # No duplicate players across the whole draft.
    all_ids = [pid for ids in rosters.values() for pid in ids]
    assert len(all_ids) == len(set(all_ids))


# ==================================================================================== scoring integration


def test_starting_lineup_value_reused_unmodified_scores_a_full_roster():
    """Smoke test that `mock_draft_sim.starting_lineup_value` (imported, never reimplemented)
    works against a `scoring_players_by_id` map built the way `main()` builds it: same
    BoardPlayer objects with `dv` swapped for a separate scoring value."""
    from dataclasses import replace

    cfg = make_cfg()
    draft_board = {
        "qb1": player("qb1", "QB", 10.0), "qb2": player("qb2", "QB", 5.0),
        "rb1": player("rb1", "RB", 10.0), "rb2": player("rb2", "RB", 8.0),
        "wr1": player("wr1", "WR", 10.0), "wr2": player("wr2", "WR", 8.0), "wr3": player("wr3", "WR", 6.0),
        "te1": player("te1", "TE", 10.0),
        "flex_rb": player("flex_rb", "RB", 20.0),
    }
    scoring_dv = {"qb1": 100.0, "qb2": 50.0, "rb1": 30.0, "rb2": 20.0, "wr1": 40.0, "wr2": 25.0,
                  "wr3": 15.0, "te1": 12.0, "flex_rb": 60.0}
    scoring_board = {pid: replace(p, dv=scoring_dv[pid]) for pid, p in draft_board.items()}

    roster_ids = list(draft_board.keys())
    value = st.starting_lineup_value(roster_ids, scoring_board, cfg)
    # Optimal lineup: QB x2 (100+50), RB x2 best-of-{rb1,rb2,flex_rb}=flex_rb(60)+rb1(30),
    # WR x3 (40+25+15), TE x1 (12), flex takes the next best RB/WR/TE leftover = rb2 (20).
    expected = 100 + 50 + 60 + 30 + 40 + 25 + 15 + 12 + 20
    assert value == pytest.approx(expected)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
