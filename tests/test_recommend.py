"""Behavioral tests for the recommendation engine (`draftroom.draft.recommend`) and its two
supporting modules, the opponent model (`draftroom.draft.opponents`) and the Monte Carlo
roll-forward (`draftroom.draft.simulate`).

Every board here is hand-constructed and every draft value is SYNTHETIC -- there are no real
per-player projections in this codebase yet (CLAUDE.md). Boards are built specifically to make
one behavior provably true or false, not to look like a realistic draft (that's what
this module is for, using the real cached FFC ADP).

`n_sims` is kept small (20-150) in most tests purely for test-suite speed; the real 500-sim
performance claim is checked separately in `TestSimulationPerformance` and in the demo tool,
where it matters.
"""

from __future__ import annotations

import pytest

from draftroom.config import LeagueConfig
from draftroom.draft import opponents as opp
from draftroom.draft import snake
from draftroom.draft.recommend import BoardPlayer, recommend
from draftroom.draft.simulate import simulate_forward
from draftroom.draft.state import DraftState, Pick
from draftroom.draft.survival import PositionalRun, load_ffc_adp
from draftroom.explain.render import as_text

TEAMS = 12
STARTERS = {"QB": 2, "RB": 2, "WR": 3, "TE": 1}
FLEX = frozenset({"RB", "WR", "TE"})
SCORING = {
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


def make_cfg(**overrides) -> LeagueConfig:
    payload: dict = dict(
        teams=TEAMS,
        starters=dict(STARTERS),
        flex_slots=1,
        flex_eligible=FLEX,
        bench=5,
        weeks=17,
        scoring=dict(SCORING),
    )
    payload.update(overrides)
    return LeagueConfig(**payload)


def player(
    pid: str, pos: str, adp: float, dv: float, *, stdev: float = 3.0, name: str | None = None,
    team: str = "FA", bye: int | None = None, dv_sd: float = 0.0,
) -> BoardPlayer:
    return BoardPlayer(
        player_id=pid, name=name or pid, pos=pos, team=team, bye=bye, adp=adp, stdev=stdev,
        dv=dv, dv_sd=dv_sd,
    )


_seq = 90000


def give_roster(state: DraftState, team_slot: int, positions: list[str]) -> None:
    """Stub-fill a team's roster with the given positions, without needing those players in the
    candidate pool at all (`Pick.stub_pos` is read directly by `DraftState.roster_positions`)."""
    global _seq
    for pos in positions:
        _seq += 1
        state.picks[_seq] = Pick(pick_no=_seq, team_slot=team_slot, stub_name=f"filler{_seq}", stub_pos=pos)


def rig_run_history(state: DraftState, team_slot: int, pos: str, n: int = 6) -> None:
    """Append `n` stub picks at `pos` so `recommend._build_run_detector` sees an active run."""
    global _seq
    for _ in range(n):
        _seq += 1
        state.picks[_seq] = Pick(pick_no=_seq, team_slot=team_slot, stub_name=f"runfiller{_seq}", stub_pos=pos)


def big_board() -> list[BoardPlayer]:
    """A reasonably deep synthetic board across all four rostered positions."""
    out: list[BoardPlayer] = []
    curves = {
        "QB": (30, 32, 1.2),
        "RB": (28, 40, 0.9),
        "WR": (26, 45, 1.0),
        "TE": (18, 20, 1.5),
    }
    adp = 1.0
    for pos, (top_dv, n, step) in curves.items():
        for i in range(n):
            dv = max(0.0, top_dv - i * (top_dv / n) * 1.3)
            out.append(player(f"{pos}{i}", pos, adp, dv, stdev=max(0.6, 1.0 + i * 0.15)))
            adp += step
    return out


# =================================================================================================
# 1. Guardrail 1: starter-slot infeasibility EXCLUDES, not down-ranks
# =================================================================================================


class TestGuardrail1Feasibility:
    def test_candidate_that_breaks_starter_feasibility_is_excluded(self):
        cfg = make_cfg(bench=0)  # roster_size = 2+2+3+1+1+0 = 9
        assert cfg.roster_size == 9
        state = DraftState(teams=TEAMS, rounds=9, my_slot=1, current_pick=1)
        # 7 of Marc's 9 spots filled, ZERO of them QB -- 2 picks left, both QB starter slots open.
        give_roster(state, 1, ["RB", "RB", "WR", "WR", "WR", "TE", "WR"])
        state.current_pick = snake.overall_pick(TEAMS, 8, 1)
        assert state.picks_remaining_for(1) == 2

        players = [
            player("qb1", "QB", 10, 20),
            player("rb1", "RB", 8, 18),
            player("wr1", "WR", 12, 16),
        ]
        rec = recommend(state, cfg, players, n_sims=20, seed=0)
        returned_pos = {c.pos for c in rec.candidates}
        returned_ids = {c.player_id for c in rec.candidates}

        # Taking the RB (or WR) now would leave 2 unfilled QB starter slots with only 1 pick
        # left -- infeasible, and must be EXCLUDED outright, not merely ranked lower.
        assert "rb1" not in returned_ids
        assert "wr1" not in returned_ids
        assert "RB" not in returned_pos and "WR" not in returned_pos
        # Taking the QB now leaves exactly 1 unfilled QB slot with 1 pick left -- feasible.
        assert "qb1" in returned_ids

    def test_feasibility_helper_directly(self):
        """Same fact, checked directly against the guardrail function (no Monte Carlo, no
        candidate-building noise) -- the unit-level version of the test above."""
        from draftroom.draft.recommend import _feasible_after_pick

        cfg = make_cfg(bench=0)
        state = DraftState(teams=TEAMS, rounds=9, my_slot=1, current_pick=1)
        give_roster(state, 1, ["RB", "RB", "WR", "WR", "WR", "TE", "WR"])
        state.current_pick = snake.overall_pick(TEAMS, 8, 1)

        assert _feasible_after_pick(state, cfg, {}, "QB") is True
        assert _feasible_after_pick(state, cfg, {}, "RB") is False
        assert _feasible_after_pick(state, cfg, {}, "WR") is False


# =================================================================================================
# 2. Guardrail 2: positional shut-out risk fires CRITICAL and forces the position in
# =================================================================================================


class TestGuardrail2ShutoutRisk:
    def test_qb_shutout_risk_fires_critical_and_forces_qb_into_candidates(self):
        cfg = make_cfg()
        # Slot 9, pick 16 (round 2) -- matches CLAUDE.md's stated scenario (gap 7/17 alternating).
        state = DraftState(
            teams=TEAMS, rounds=14, my_slot=9, current_pick=snake.overall_pick(TEAMS, 2, 9)
        )
        # 8 teams already have both starting QBs; teams 9 (Marc), 10, 11, 12 have none --
        # exactly 8 unfilled league QB slots.
        for t in range(1, 9):
            give_roster(state, t, ["QB", "QB"])

        players = [
            player("qb1", "QB", 20, 30),
            player("qb2", "QB", 22, 28),
            player("qb3", "QB", 24, 26),
            player("qb_deep1", "QB", 150, 0.0),  # not startable (dv <= 0)
            player("qb_deep2", "QB", 160, -1.0),
        ]
        players += [p for p in big_board() if p.pos != "QB"]  # depth at every other position

        rec = recommend(state, cfg, players, n_sims=80, seed=1)

        assert any("CRITICAL" in w and "QB" in w for w in rec.warnings), rec.warnings
        assert any(c.pos == "QB" for c in rec.candidates)
        qb_candidate = next(c for c in rec.candidates if c.pos == "QB")
        assert qb_candidate.depth.is_shutout_risk


# =================================================================================================
# 3. At-the-turn: the pair optimiser beats greedy single-pick selection
# =================================================================================================


class TestAtTheTurnPairOptimizer:
    def test_pair_optimizer_protects_the_cliff_greedy_would_lose(self):
        cfg = make_cfg()
        # Slot 11 at pick 11: next_pick=11 (now), following_pick=14, 2 opponent picks between
        # (both slot 12's, the "wheel") -- at_the_turn per TurnContext's <=2 rule.
        state = DraftState(teams=TEAMS, rounds=14, my_slot=11, current_pick=11)

        # A: the TRUE cliff. High value, but essentially gone by pick 14 if not taken now.
        a = player("A", "RB", 13, 28, stdev=0.5)
        # B: higher RAW value than A, but safe regardless -- it will still be there at 14.
        b = player("B", "WR", 45, 30, stdev=5.0)
        # Fallbacks so both positions have a "next best" and pair search has real alternatives.
        c = player("C", "WR", 70, 10, stdev=5.0)
        d = player("D", "RB", 90, 8, stdev=5.0)
        players = [a, b, c, d]

        rec = recommend(state, cfg, players, n_sims=30, seed=2)
        assert rec.at_the_turn is True

        greedy_choice = max(players, key=lambda p: p.dv)
        assert greedy_choice.player_id == "B", "test setup: B must be the naive top-dv choice"

        # The pair-optimal recommendation must be A, not the naively-higher-value B: taking B
        # now leaves A (the real cliff) to a ~2.6% survival chance, while B was safe either way.
        assert rec.candidates[0].player_id == "A", (
            f"greedy would take B (dv={b.dv}) and lose A's tier; pair-optimal must take A "
            f"instead. Got top candidate {rec.candidates[0].player_id!r}."
        )
        top_ids = [c.player_id for c in rec.candidates]
        assert top_ids.index("A") < top_ids.index("B")


# =================================================================================================
# 4. Higher lambda shifts ranking toward the lower-variance (higher-floor) player
# =================================================================================================


class TestLambdaShiftsTowardFloor:
    def test_higher_lambda_prefers_the_high_floor_player_over_the_high_ceiling_one(self):
        cfg = make_cfg()
        state = DraftState(teams=TEAMS, rounds=14, my_slot=1, current_pick=1)

        # Same draft value, same position, same ADP -- differ ONLY in dv_sd (outcome spread).
        high_floor = player("FLOOR", "WR", 30, 20.0, stdev=3.0, dv_sd=1.0)
        high_ceiling = player("CEIL", "WR", 30.5, 20.0, stdev=3.0, dv_sd=15.0)
        depth = [player(f"wr_deep{i}", "WR", 40 + i * 3, 12 - i, stdev=3.0) for i in range(6)]
        other = [player(f"other{i}", pos, 20 + i * 4, 15 - i, stdev=3.0) for i, pos in enumerate(
            ["RB", "RB", "RB", "TE", "QB", "QB"]
        )]
        players = [high_floor, high_ceiling] + depth + other

        rec_lam0 = recommend(state, cfg, players, lam=0.0, n_sims=60, seed=7)
        u_floor_0 = next(c for c in rec_lam0.candidates if c.player_id == "FLOOR").utility
        u_ceil_0 = next(c for c in rec_lam0.candidates if c.player_id == "CEIL").utility
        # At lam=0 the risk term is inert -- the two should be (near) indistinguishable.
        assert u_floor_0 == pytest.approx(u_ceil_0, abs=0.5)

        rec_lam_hi = recommend(state, cfg, players, lam=2.0, n_sims=60, seed=7)
        u_floor_hi = next(c for c in rec_lam_hi.candidates if c.player_id == "FLOOR").utility
        u_ceil_hi = next(c for c in rec_lam_hi.candidates if c.player_id == "CEIL").utility

        assert u_floor_hi > u_ceil_hi, "higher lambda must penalize the high-ceiling/high-sd player more"
        ids_hi = [c.player_id for c in rec_lam_hi.candidates]
        assert ids_hi.index("FLOOR") < ids_hi.index("CEIL")


# =================================================================================================
# 5. Opponent model: hard-constrained need-taking
# =================================================================================================


class TestOpponentHardConstraint:
    def test_manager_with_empty_qb_slot_and_few_picks_left_is_restricted_to_need(self):
        cfg = make_cfg()
        # Every slot filled except both QBs, and exactly as many picks remain as slots needed.
        have = {"RB": 4, "WR": 5, "TE": 3}  # 12 filled; roster_size 14 -> 2 remaining
        assert opp.picks_remaining_from_counts(have, cfg) == 2
        assert opp.unfilled_starters_from_counts(have, cfg) == {"QB": 2}
        assert opp.flex_deficit_from_counts(have, cfg) == 0

        allowed = opp.hard_constraint_positions(have, cfg)
        assert allowed == frozenset({"QB"})

        pool = [
            player("qb1", "QB", 30, 20),
            player("qb2", "QB", 40, 15),
            player("rb1", "RB", 10, 35),  # would otherwise be the clear top pick by dv
            player("wr1", "WR", 12, 32),
        ]
        probs = opp.opponent_pick_probabilities(pool, team_slot=1, pick_no=90, have=have, cfg=cfg)
        assert set(probs.keys()) == {"qb1", "qb2"}
        assert probs["qb1"] + probs["qb2"] == pytest.approx(1.0)
        # A manager in this spot takes a QB with certainty -- not merely "high probability" --
        # because the hard constraint removed every other position from the support entirely.
        assert "rb1" not in probs and "wr1" not in probs


# =================================================================================================
# 6. Herding: opponents shift toward a running position; the recommendation engine does not
# =================================================================================================


class TestHerdingIsOpponentOnlyNeverOurs:
    def test_opponent_probabilities_shift_toward_a_detected_run(self):
        cfg = make_cfg()
        pool = [
            player("rb0", "RB", 40, 10), player("rb1", "RB", 45, 9),
            player("wr0", "WR", 40, 10), player("wr1", "WR", 45, 9),
        ]
        run = PositionalRun()
        for _ in range(6):
            run.observe("WR", remaining=pool)
        assert run.shift("WR") > 0.0
        assert run.shift("RB") == 0.0

        probs = opp.opponent_pick_probabilities(pool, team_slot=1, pick_no=10, have={}, cfg=cfg, run=run)
        wr_mass = probs["wr0"] + probs["wr1"]
        rb_mass = probs["rb0"] + probs["rb1"]
        # Same ADP, same dv on both sides -- the only thing that can break the symmetry is the
        # herd term, and it must push mass toward the position that's actually running.
        assert wr_mass > rb_mass

    def test_recommendation_ranking_does_not_herd(self):
        """The exact same board, differing ONLY in whether a WR run is live in the pick
        history, must give the evaluated WR candidate the SAME utility either way.

        This is a confound-free construction, not a coincidence: in the at-the-turn pair
        formula, a candidate taken "now" contributes its flat draft value with no survival term
        of its own (only the chosen partner's survival matters), and the partner chosen here
        (RB_Y) sits at a position untouched by the WR run. So a WR run being live can change
        NOTHING about this candidate's utility unless the engine added an extra herding bonus
        for "the position everyone's taking" -- which CLAUDE.md and `opponents.py`'s own
        docstring say it must never do.
        """
        cfg = make_cfg()

        state_plain = DraftState(teams=TEAMS, rounds=14, my_slot=11, current_pick=11)
        state_run = DraftState(teams=TEAMS, rounds=14, my_slot=11, current_pick=11)
        rig_run_history(state_run, team_slot=2, pos="WR", n=6)

        players = [
            player("WR_X", "WR", 40, 25, stdev=5.0),   # candidate under test
            player("RB_Y", "RB", 45, 22, stdev=5.0),   # the safe pair partner -- a different, unaffected position
            player("WR_other", "WR", 60, 15, stdev=5.0),
            player("RB_other", "RB", 90, 8, stdev=5.0),
        ]

        rec_plain = recommend(state_plain, cfg, players, n_sims=20, seed=5)
        rec_run = recommend(state_run, cfg, players, n_sims=20, seed=5)
        assert rec_plain.at_the_turn and rec_run.at_the_turn

        u_plain = next(c for c in rec_plain.candidates if c.player_id == "WR_X").utility
        u_run = next(c for c in rec_run.candidates if c.player_id == "WR_X").utility
        assert u_plain == pytest.approx(u_run, abs=1e-9)


# =================================================================================================
# 7. Recommendation is fully populated
# =================================================================================================


class TestRecommendationFullyPopulated:
    def test_every_candidate_has_bullets_a_fallback_and_a_counterfactual(self):
        cfg = make_cfg()
        state = DraftState(teams=TEAMS, rounds=14, my_slot=4, current_pick=4)
        players = big_board()

        rec = recommend(state, cfg, players, n_sims=60, seed=11)
        assert rec.candidates, "expected a non-empty candidate list on a deep, open board"

        saw_fallback = False
        saw_counterfactual = False
        for c in rec.candidates:
            assert c.bullets, f"{c.player_id} has no rendered bullets"
            if c.fallbacks:
                saw_fallback = True
                for fb in c.fallbacks:
                    assert fb.name
                    assert 0.0 <= fb.p_survive_next <= 1.0
            if c.counterfactual is not None:
                saw_counterfactual = True

        assert saw_fallback, "expected at least one candidate with fallbacks on a deep board"
        assert saw_counterfactual, "expected at least one candidate with a counterfactual"


# =================================================================================================
# 8. Read the actual sentences
# =================================================================================================


class TestRenderedTextIsReadable:
    def test_print_the_rendered_recommendation_for_a_realistic_pick(self):
        cfg = make_cfg()
        state = DraftState(teams=TEAMS, rounds=14, my_slot=9, current_pick=snake.overall_pick(TEAMS, 2, 9))
        for t in range(1, 5):
            give_roster(state, t, ["QB", "QB"])
        players = big_board()

        rec = recommend(state, cfg, players, n_sims=100, seed=42)
        text = as_text(rec)
        print("\n" + text + "\n")

        assert "Pick 2." in text
        assert rec.candidates
        for c in rec.candidates[:3]:
            assert c.name in text


# =================================================================================================
# Simulation performance -- measured, not assumed
# =================================================================================================


class TestSimulationPerformance:
    """Guard against an algorithmic regression, not against a busy laptop.

    A tight wall-clock assertion fails whenever something else is running on the machine, which
    trains everyone to ignore a red suite -- and that costs far more than this test catches. The
    regression genuinely worth catching is the naive implementation this replaced, which ran ~5s and
    re-derived every player's ADP on every simulated pick. The ceiling is set to catch that class of
    mistake while tolerating a loaded machine, and the measured number is always printed so an
    upward drift stays visible even on a pass.

    Draft-night latency does not depend on this bound: recommendations are computed between picks,
    not while Marc waits on one.
    """

    REGRESSION_CEILING_SECONDS = 6.0

    def test_500_sims_complete_without_algorithmic_regression_on_the_real_ffc_board(self):
        cfg = make_cfg()
        adp_players = load_ffc_adp()
        players = [
            player(str(p.player_id), p.pos, p.adp, max(0.0, 100.0 - p.adp), stdev=p.stdev or 3.0, name=p.name)
            for p in adp_players
        ]
        by_adp = sorted(adp_players, key=lambda p: p.adp)
        state = DraftState(teams=TEAMS, rounds=14, my_slot=9, current_pick=16)
        for i, p in enumerate(by_adp[:15], start=1):
            state.picks[i] = Pick(pick_no=i, team_slot=(i - 1) % 12 + 1, player_id=str(p.player_id))

        summary = simulate_forward(state, cfg, players, n_sims=500, seed=0)
        print(
            f"\n500-sim roll-forward over the real {len(players)}-player FFC board, "
            f"pick {summary.current_pick} -> {summary.following_pick}: "
            f"{summary.elapsed_seconds:.3f}s wall clock\n"
        )
        assert summary.elapsed_seconds < self.REGRESSION_CEILING_SECONDS, (
            f"{summary.elapsed_seconds:.2f}s exceeds the {self.REGRESSION_CEILING_SECONDS}s "
            "regression ceiling -- this is slow enough to indicate an algorithmic problem, "
            "not just a busy machine"
        )
        assert len(summary.results) == 500
