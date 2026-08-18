"""Behavioral tests for fix "C" (scarcity floor, elite grab, VONA-in-ranking) and the fixed
sim objective (unfilled mandatory slots are charged, not free).

Boards are hand-constructed with SYNTHETIC values, built to make one behavior provably true or
false -- same convention as `tests/test_recommend.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from mock_draft_sim import starting_lineup_value, waiver_fill_values  # noqa: E402

from draftroom.config import LeagueConfig
from draftroom.draft import opponents as opp
from draftroom.draft.recommend import BoardPlayer, recommend
from draftroom.draft.state import DraftState, Pick

# The REAL league shape (10 teams, 2 QB, no K/DST) -- fix "C" exists for exactly this shape.
TEAMS = 10
STARTERS = {"QB": 2, "RB": 2, "WR": 3, "TE": 1}
FLEX = frozenset({"RB", "WR", "TE"})
SCORING = {"pass_yd": 0.04, "pass_td": 4.0, "rush_yd": 0.1, "rec": 0.5}


def make_cfg(**overrides) -> LeagueConfig:
    payload: dict = dict(
        teams=TEAMS,
        starters=dict(STARTERS),
        flex_slots=1,
        flex_eligible=FLEX,
        bench=6,
        weeks=17,
        scoring=dict(SCORING),
    )
    payload.update(overrides)
    return LeagueConfig(**payload)


def player(pid, pos, adp, dv, *, stdev=3.0, dv_sd=0.0) -> BoardPlayer:
    return BoardPlayer(
        player_id=pid, name=pid, pos=pos, team="FA", bye=None, adp=adp, stdev=stdev,
        dv=dv, dv_sd=dv_sd,
    )


def _fill_board(qbs=6, rbs=8, wrs=10, tes=4, *, qb_dv=20.0):
    """A board with enough bodies at every position for feasibility; RBs carry the top value."""
    players = []
    for i in range(qbs):
        players.append(player(f"q{i}", "QB", 30 + i, qb_dv - i))
    for i in range(rbs):
        players.append(player(f"r{i}", "RB", 1 + i, 100.0 - 5 * i))
    for i in range(wrs):
        players.append(player(f"w{i}", "WR", 15 + i, 60.0 - 3 * i))
    for i in range(tes):
        players.append(player(f"t{i}", "TE", 40 + i, 30.0 - 4 * i))
    return players


def _state(my_slot=1, current_pick=1, rounds=15) -> DraftState:
    return DraftState(teams=TEAMS, rounds=rounds, my_slot=my_slot, current_pick=current_pick)


_seq = 80000


def give_roster(state: DraftState, team_slot: int, positions: list[str]) -> None:
    global _seq
    for pos in positions:
        _seq += 1
        state.picks[_seq] = Pick(
            pick_no=_seq, team_slot=team_slot, stub_name=f"filler{_seq}", stub_pos=pos
        )


# ------------------------------------------------------------------ sim objective (lever b)


class TestUnfilledSlotCharge:
    def test_waiver_fill_capped_at_zero(self):
        cfg = make_cfg()
        undrafted = [player(f"q{i}", "QB", 100 + i, 50.0 - i) for i in range(12)]
        fill = waiver_fill_values(undrafted, cfg)
        # A breakout sitting on the wire must not be creditable: hard cap at 0.
        assert fill["QB"] == 0.0

    def test_waiver_fill_is_contested_band_median(self):
        cfg = make_cfg()
        undrafted = [player(f"q{i}", "QB", 100 + i, -10.0 * (i + 1)) for i in range(12)]
        fill = waiver_fill_values(undrafted, cfg)
        # top-10 band is -10..-100; upper median of the band, not the best leftover (-10).
        assert fill["QB"] < -10.0

    def test_zero_qb_roster_charged(self):
        cfg = make_cfg()
        qbs = [player(f"q{i}", "QB", 30 + i, 10.0 - i) for i in range(2)]
        others = (
            [player(f"r{i}", "RB", 1 + i, 50.0) for i in range(4)]
            + [player(f"w{i}", "WR", 10 + i, 40.0) for i in range(4)]
            + [player(f"t{i}", "TE", 40 + i, 20.0) for i in range(2)]
        )
        by_id = {p.player_id: p for p in qbs + others}
        with_qbs = [p.player_id for p in qbs + others]  # identical apart from the 2 QBs
        without_qbs = [p.player_id for p in others]
        fill = {"QB": -40.0, "RB": 0.0, "WR": 0.0, "TE": 0.0}
        v_with = starting_lineup_value(with_qbs, by_id, cfg, waiver_fill=fill)
        v_without = starting_lineup_value(without_qbs, by_id, cfg, waiver_fill=fill)
        # 2 QBs at dv 10+9 = +19 vs two charged slots at -40 each = -80: the no-QB roster
        # must lose by exactly the swing.
        assert v_with - v_without == (10.0 + 9.0) - 2 * (-40.0)

    def test_legacy_none_preserves_old_behavior(self):
        cfg = make_cfg()
        others = [player(f"r{i}", "RB", 1 + i, 50.0) for i in range(4)]
        by_id = {p.player_id: p for p in others}
        ids = [p.player_id for p in others]
        assert starting_lineup_value(ids, by_id, cfg) == starting_lineup_value(
            ids, by_id, cfg, waiver_fill=None
        )


# ------------------------------------------------------------------ fix "C" (a): the floor


class TestScarcityFloor:
    def test_floor_forces_qb_to_the_top(self):
        cfg = make_cfg()
        state = _state()
        # 6 startable QBs vs 20 unfilled league QB slots and an 18-pick gap: fires hard.
        players = _fill_board(qbs=6)
        rec = recommend(state, cfg, players, n_sims=20, seed=7, elite_qb_rank_cutoff=0)
        assert rec.candidates[0].pos == "QB"
        assert any("SCARCITY FLOOR" in w for w in rec.warnings)

    def test_floor_releases_when_my_need_met(self):
        cfg = make_cfg()
        state = _state(current_pick=21)
        give_roster(state, 1, ["QB", "QB"])
        players = _fill_board(qbs=6)
        rec = recommend(state, cfg, players, n_sims=20, seed=7, elite_qb_rank_cutoff=0)
        # My 2 QB slots are full: no force, best value (RB) leads.
        assert rec.candidates[0].pos == "RB"
        assert not any("SCARCITY FLOOR" in w for w in rec.warnings)

    def test_floor_never_fires_for_flex_positions(self):
        cfg = make_cfg()
        state = _state()
        # RB supply of 2 vs 20+ league demand would "fire" numerically, but RB is
        # flex-eligible so the floor must skip it.
        players = _fill_board(qbs=40, rbs=2)
        rec = recommend(state, cfg, players, n_sims=20, seed=7, elite_qb_rank_cutoff=0)
        assert not any("SCARCITY FLOOR: " + "2 startable RB" in w for w in rec.warnings)
        assert not any("RB ranked first" in w for w in rec.warnings)


# ------------------------------------------------------------------ fix "C" (b): elite grab


class TestEliteGrab:
    def test_elite_qb_ranked_first_despite_lower_dv(self):
        cfg = make_cfg()
        state = _state()
        # 40 startable QBs (dv 60..21, all > 0): the floor stays quiet; q0 is the board's #1
        # QB at dv 60 while r0 carries dv 100. The grab must still put q0 first.
        players = _fill_board(qbs=40, qb_dv=60.0)
        rec = recommend(state, cfg, players, n_sims=20, seed=7)
        # Which of the three elites leads is a near-tie (the exclude-X continuation cancels
        # their dv gaps); the behavior under test is that an ELITE QB outranks the dv-100 RB.
        assert rec.candidates[0].player_id in {"q0", "q1", "q2"}
        assert any("ELITE QB AVAILABLE" in w for w in rec.warnings)

    def test_never_reaches_when_elites_are_gone(self):
        cfg = make_cfg()
        state = _state(current_pick=4)
        # The top-3 board QBs were drafted by others: the rule must NOT reach for q3.
        # qb_dv=60 keeps all 40 QBs startable so the scarcity floor stays quiet too.
        players = _fill_board(qbs=40, qb_dv=60.0)
        for i, pick_no in enumerate((1, 2, 3)):
            state.picks[pick_no] = Pick(pick_no=pick_no, team_slot=2, player_id=f"q{i}")
        state.my_slot = 4
        state.current_pick = 4
        rec = recommend(state, cfg, players, n_sims=20, seed=7)
        assert rec.candidates[0].pos == "RB"
        assert not any("ELITE QB AVAILABLE" in w for w in rec.warnings)

    def test_grab_releases_after_first_qb(self):
        cfg = make_cfg()
        state = _state(current_pick=21)
        give_roster(state, 1, ["QB"])
        players = _fill_board(qbs=40, qb_dv=60.0)
        rec = recommend(state, cfg, players, n_sims=20, seed=7)
        assert rec.candidates[0].pos == "RB"

    def test_knob_widens_and_disables(self):
        cfg = make_cfg()
        state = _state(current_pick=4, my_slot=4)
        players = _fill_board(qbs=40, qb_dv=60.0)
        for i, pick_no in enumerate((1, 2, 3)):
            state.picks[pick_no] = Pick(pick_no=pick_no, team_slot=2, player_id=f"q{i}")
        # cutoff 5: q3 (board QB rank 4) now qualifies.
        rec = recommend(state, cfg, players, n_sims=20, seed=7, elite_qb_rank_cutoff=5)
        assert rec.candidates[0].pos == "QB"
        # cutoff 0: rule off entirely, even with q0 on the board.
        state2 = _state()
        rec2 = recommend(state2, cfg, players, n_sims=20, seed=7, elite_qb_rank_cutoff=0)
        assert rec2.candidates[0].pos == "RB"


# ------------------------------------------------------------------ fix "C" (c): VONA


class TestVonaInRanking:
    def test_cliff_position_outranks_deep_position_at_equal_dv(self):
        cfg = make_cfg()
        state = _state(current_pick=21)
        give_roster(state, 1, ["QB", "QB"])  # silence floor and grab
        players = [
            # RB cliff: r0 then a canyon, and r0's ADP says he's gone well before pick 40.
            player("r0", "RB", 22.0, 50.0, stdev=2.0),
            player("r1", "RB", 60.0, 5.0, stdev=2.0),
            player("r2", "RB", 61.0, 4.0, stdev=2.0),
            # WR depth: w0's twin survives, and w0 himself isn't leaving soon.
            player("w0", "WR", 80.0, 50.0, stdev=2.0),
            player("w1", "WR", 81.0, 49.0, stdev=2.0),
            player("w2", "WR", 82.0, 48.0, stdev=2.0),
            # bodies so every position exists
            player("q0", "QB", 90.0, 5.0),
            player("q1", "QB", 91.0, 4.0),
            player("t0", "TE", 95.0, 5.0),
            player("t1", "TE", 96.0, 4.0),
        ]
        rec = recommend(state, cfg, players, n_sims=30, seed=11, elite_qb_rank_cutoff=0)
        order = [c.player_id for c in rec.candidates]
        assert order.index("r0") < order.index("w0")


# ------------------------------------------------------------------ gated opponent knobs


class TestGatedOpponentKnobs:
    def test_qb_mu_curve_identity_when_empty(self):
        calib = opp.LeagueCalibration.national_only()
        assert calib.remap_qb_mu(37.5) == 37.5

    def test_qb_mu_curve_interpolates_and_extends(self):
        calib = opp.LeagueCalibration(qb_mu_curve=((10.0, 20.0), (30.0, 60.0)))
        assert calib.remap_qb_mu(20.0) == 40.0  # midpoint
        assert calib.remap_qb_mu(5.0) == 15.0  # left tail keeps the +10 offset
        assert calib.remap_qb_mu(40.0) == 70.0  # right tail keeps the +30 offset

    def test_satiation_damps_only_complete_nonflex_positions(self):
        cfg = make_cfg()
        pool = [player("qX", "QB", 10.0, 5.0), player("rX", "RB", 11.0, 5.0)]
        have = {"QB": 2, "RB": 0}
        base = opp.opponent_scores(
            pool, team_slot=2, pick_no=30, have=have, cfg=cfg,
            calibration=opp.LeagueCalibration.national_only(),
        )
        damped = opp.opponent_scores(
            pool, team_slot=2, pick_no=30, have=have, cfg=cfg,
            calibration=opp.LeagueCalibration(satiation_damper=8.0),
        )
        assert damped["qX"] < base["qX"]  # QB-complete team feels less QB pull
        assert damped["rX"] == base["rX"]  # incomplete flex position untouched


# ------------------------------------------------- shared scarcity module (Codex 2026-08-18)


class TestSharedScarcity:
    def test_startable_rank_cutoff_reproduces_qb22_at_real_settings(self):
        from draftroom.draft.scarcity import startable_rank_cutoff

        assert startable_rank_cutoff(make_cfg(), "QB") == 22

    def test_codex_example_no_longer_over_fires(self):
        """21 startable QBs, 20 open slots leaguewide, 18-pick gap: the OLD trigger
        (supply - leaguewide_unfilled <= gap) fired immediately; need-bounded consumption must
        not, because every need-driven opponent pick removes supply AND demand together."""
        from draftroom.draft.scarcity import opponent_consumption_bound, scarcity_trigger_fires

        # 9 opponents each picking twice in the gap, each still needing both QB slots.
        gap_slots = [slot for slot in range(2, 11) for _ in range(2)]
        unfilled = {slot: 2 for slot in range(2, 11)}
        consumption = opponent_consumption_bound(gap_slots, unfilled)
        assert consumption == 18
        assert not scarcity_trigger_fires(
            startable_remaining=21, opponent_consumption_bound=consumption, my_unfilled=2
        ), "cushion of exactly my_unfilled survives worst-case need consumption -- no force"

    def test_fires_when_worst_case_consumption_starves_my_need(self):
        from draftroom.draft.scarcity import scarcity_trigger_fires

        assert scarcity_trigger_fires(
            startable_remaining=3, opponent_consumption_bound=2, my_unfilled=2
        )

    def test_never_fires_with_no_unfilled_need(self):
        from draftroom.draft.scarcity import scarcity_trigger_fires

        assert not scarcity_trigger_fires(
            startable_remaining=0, opponent_consumption_bound=5, my_unfilled=0
        )

    def test_consumption_bound_respects_both_pick_count_and_need(self):
        from draftroom.draft.scarcity import opponent_consumption_bound

        # slot 2 picks twice but needs one; slot 3 picks once but needs two; slot 4 needs none.
        assert opponent_consumption_bound([2, 2, 3, 4], {2: 1, 3: 2, 4: 0}) == 2


# ------------------------------------------------ back-to-back survival is exactly 1.0 (Codex)


class TestBackToBackSurvival:
    def test_pair_partner_survives_100pct_at_the_turn(self):
        """Slot 10 at pick 10 picks again at 11: once the current pick is Marc's, no opponent
        can consume anyone before his following pick, so the pair partner's survival must be
        exactly 100% (the old code conditioned from current_pick and priced an impossible
        draft opportunity)."""
        cfg = make_cfg()
        players = _fill_board()
        state = _state(my_slot=10, current_pick=10)
        rec = recommend(state, cfg, players, n_sims=10, seed=1)
        assert rec.at_the_turn
        turn_warnings = [w for w in rec.warnings if "best pair" in w]
        assert turn_warnings, f"expected an at-the-turn pair plan, warnings={rec.warnings}"
        assert "100% survives" in turn_warnings[0]


# --------------------------------------------------- unranked players are bookkeeping-only


class TestUnrankedNeverRecommended:
    def test_unranked_player_never_becomes_a_candidate_even_with_absurd_dv(self):
        """A roster-only write-in must never enter candidate generation no matter what number
        sits in its dv field -- dv on an unranked player is not an evaluation (CLAUDE.md).
        Before the is_ranked filter, draft night handed all 980 pool players (789 of them
        unranked) straight into recommend()."""
        cfg = make_cfg()
        players = _fill_board()
        players.append(
            BoardPlayer(
                player_id="writein", name="writein", pos="RB", team="FA", bye=None,
                adp=999.0, stdev=50.0, dv=10_000.0, is_ranked=False,
            )
        )
        rec = recommend(state := _state(my_slot=1, current_pick=1), cfg, players, n_sims=10, seed=1)
        assert state.is_my_pick
        ids = {c.player_id for c in rec.candidates}
        assert "writein" not in ids
        fallback_ids = {f.player_id for c in rec.candidates for f in c.fallbacks}
        assert "writein" not in fallback_ids

    def test_drafted_unranked_writein_still_counts_toward_opponent_need_math(self):
        """The other half of the contract: an unranked player DRAFTED by an opponent must still
        fill that team's positional need (bookkeeping), even though he is never recommended."""
        cfg = make_cfg()
        players = _fill_board()
        players.append(
            BoardPlayer(
                player_id="writein-qb", name="writein-qb", pos="QB", team="FA", bye=None,
                adp=999.0, stdev=50.0, dv=0.0, is_ranked=False,
            )
        )
        state = _state(my_slot=1, current_pick=1)
        # Opponent slot 2 drafted the write-in QB: their QB need must drop from 2 to 1.
        state.picks[70001] = Pick(pick_no=70001, team_slot=2, player_id="writein-qb")
        pos_of = {p.player_id: p.pos for p in players}
        unfilled = state.unfilled_starters(2, dict(cfg.starters), pos_of)
        assert unfilled.get("QB") == 1
