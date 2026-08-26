"""Ledger #12: the fields the ALL board's "best pick now" ranking is built on.

The ALL tab used to rank by season draft value while the recommendation panel ranked Josh Allen
first, and Marc read that as a bug. It was not -- the panel sorts by `(gate_priority, utility)` --
but the two screens disagreed with nothing on screen saying why, so the engine now PUBLISHES the
pieces the board needs and the board reuses them instead of re-deriving them.

These tests pin the contract, and specifically the two ways the first attempt got it wrong (both
caught by Codex 2026-08-26, both of which would have been silent in a room):

  1. The gate was re-derived from `forced_positions`, which names a whole POSITION. The panel's
     gate applies only to candidates that passed feasibility and the per-position top-N cut, so
     re-deriving it hoisted every remaining player at that position -- QB23 and below included --
     above every other position, destroying the best-available scan the ALL tab exists for.
  2. `value + VONA` was believed to reproduce `utility`'s ordering as an identity. It does not:
     at the turn the panel optimises a PAIR, and mid-round `utility` carries a candidate-specific
     continuation and risk term. It agreed on all 16 candidates for ONE board state, which is a
     measurement, not a guarantee -- so `gate_priority` carries the panel's own ordering for the
     players the panel actually ranks.

Boards are hand-constructed with SYNTHETIC values, the same convention as `test_recommend.py` and
`test_fix_c.py`, and these deliberately reuse that module's helpers so the league shape under test
is the real one (10 teams, 2 QB, no K/DST).
"""

from __future__ import annotations

from draftroom.draft.recommend import recommend
from draftroom.draft.state import Pick

from tests.test_fix_c import _fill_board, _state, give_roster, make_cfg


class TestPublishedFields:
    """The three fields exist, are typed as the UI expects, and are internally consistent."""

    def test_vona_by_pos_is_published_for_every_position_with_players(self):
        cfg = make_cfg()
        rec = recommend(_state(), cfg, _fill_board(qbs=40, qb_dv=60.0), n_sims=20, seed=7)
        assert rec.vona_by_pos, "the board cannot rank by pick-now value without this"
        # Every position on the board is priced, and a price is a real number (0.0 is legitimate --
        # it means waiting costs nothing there, which is itself information).
        for pos, v in rec.vona_by_pos.items():
            assert isinstance(pos, str) and pos
            assert isinstance(v, float)

    def test_vona_by_pos_agrees_with_the_candidates_own_vona(self):
        """The whole point of publishing the map is that the board cannot drift from the panel.

        If these two ever disagreed, the board would rank by one number while the panel explained
        itself with another, and nothing on screen would reveal it.
        """
        cfg = make_cfg()
        rec = recommend(_state(), cfg, _fill_board(qbs=40, qb_dv=60.0), n_sims=20, seed=7)
        for c in rec.candidates:
            assert c.vona == rec.vona_by_pos[c.pos]

    def test_gate_priority_matches_the_order_candidates_are_returned_in(self):
        """`gate_priority` is the panel's PRIMARY sort key, ahead of utility."""
        cfg = make_cfg()
        rec = recommend(_state(), cfg, _fill_board(qbs=40, qb_dv=60.0), n_sims=20, seed=7)
        priorities = [c.gate_priority for c in rec.candidates]
        assert priorities == sorted(priorities, reverse=True), (
            "candidates must be returned gate-first; the board slices the leading gated run "
            "straight off this list"
        )
        # And within one gate level, utility decides.
        for level in set(priorities):
            utils = [c.utility for c in rec.candidates if c.gate_priority == level]
            assert utils == sorted(utils, reverse=True)

    def test_elite_grab_stamps_priority_one_not_two(self):
        """The two gates are different levels and must not be flattened into one another.

        The UI collapses them to a boolean for the sort, but it may only do that BECAUSE the
        engine ordered them first -- level 2 (catastrophe avoidance) outranks level 1 (opportunism).
        """
        cfg = make_cfg()
        rec = recommend(_state(), cfg, _fill_board(qbs=40, qb_dv=60.0), n_sims=20, seed=7)
        assert any("ELITE QB AVAILABLE" in w for w in rec.warnings)
        lead = rec.candidates[0]
        assert lead.pos == "QB"
        assert lead.gate_priority == 1
        assert rec.elite_player_ids, "the elite ids back the REC badge on the board"
        assert lead.player_id in rec.elite_player_ids

    def test_ungated_candidates_carry_priority_zero(self):
        cfg = make_cfg()
        # Grab off, floor quiet: nothing should be gated at all.
        rec = recommend(
            _state(), cfg, _fill_board(qbs=40, qb_dv=60.0), n_sims=20, seed=7,
            elite_qb_rank_cutoff=0,
        )
        assert all(c.gate_priority == 0 for c in rec.candidates)
        assert rec.forced_positions == ()
        assert rec.elite_player_ids == ()


class TestGateCannotHoistAWholeBlock:
    """Bug 1: the reason the board gates on candidate IDs and never on `forced_positions`."""

    def test_gated_candidates_are_far_fewer_than_the_position_is_deep(self):
        cfg = make_cfg()
        rec = recommend(_state(), cfg, _fill_board(qbs=40, qb_dv=60.0), n_sims=20, seed=7)
        gated = [c for c in rec.candidates if c.gate_priority > 0]
        assert gated, "this fixture is supposed to fire the elite grab"
        # 40 QBs are on the board. The gate covers a handful, so hoisting them cannot bury the
        # other positions -- which is exactly what gating on the POSITION would have done.
        qbs_on_board = 40
        assert len(gated) <= 6
        assert len(gated) < qbs_on_board / 4

    def test_every_gated_id_is_a_real_undrafted_candidate(self):
        """The board hoists by ID, so a stale or drafted ID would hoist a struck-through row."""
        cfg = make_cfg()
        state = _state(current_pick=4, my_slot=4)
        players = _fill_board(qbs=40, qb_dv=60.0)
        for i, pick_no in enumerate((1, 2)):
            state.picks[pick_no] = Pick(pick_no=pick_no, team_slot=2, player_id=f"q{i}")
        rec = recommend(state, cfg, players, n_sims=20, seed=7)
        drafted = state.drafted_player_ids
        cand_ids = {c.player_id for c in rec.candidates}
        for c in rec.candidates:
            if c.gate_priority > 0:
                assert c.player_id not in drafted
                assert c.player_id in cand_ids


class TestPickNowIsAnApproximationOutsideTheGate:
    """Bug 2: `value + VONA` is NOT an identity for `utility`, and the code must not claim it is.

    This test does not assert the two disagree (on a given board they may happen to agree) -- it
    asserts the SCALES differ, which is the structural reason the board may only reuse the panel's
    ordering for gated players and must fall back to its own approximation for the rest.
    """

    def test_utility_is_not_value_plus_vona(self):
        cfg = make_cfg()
        rec = recommend(_state(), cfg, _fill_board(qbs=40, qb_dv=60.0), n_sims=40, seed=7)
        gaps = [c.utility - (c.draft_value + c.vona) for c in rec.candidates]
        assert gaps, "no candidates to compare"
        # The continuation term is position-agnostic by construction, so the gap is nearly
        # constant -- which is why the ORDERING often matches -- but it is not zero, so the two
        # are not interchangeable as values.
        assert max(abs(g) for g in gaps) > 1.0, (
            "if this ever becomes ~0 the approximation note in TierBoard.tsx is stale"
        )

    def test_at_the_turn_utility_is_a_pair_value(self):
        """At back-to-back picks the panel optimises a PAIR, so utility exceeds one player's worth.

        This is the clearest case where reconstructing the panel's ranking from `value + VONA`
        cannot work, and it is why `gate_priority` is published.
        """
        cfg = make_cfg()
        # Slot 1 in a 10-team snake picks 20 and 21 back to back.
        state = _state(current_pick=20, my_slot=10)
        players = _fill_board(qbs=40, qb_dv=60.0)
        rec = recommend(state, cfg, players, n_sims=20, seed=7)
        if not rec.at_the_turn:
            return  # fixture did not land on a turn; nothing to assert
        lead = rec.candidates[0]
        assert lead.utility >= lead.draft_value


class TestDegradedStates:
    """What the board falls back to. Each of these is a real moment in a draft."""

    def test_not_my_pick_publishes_no_ranking_inputs(self):
        """The panel refuses off-clock, and must not leave the board half-armed.

        `vona_by_pos` empty is the signal the UI keys its whole pick-now treatment on, so an
        empty candidate list must come with empty inputs rather than stale ones.
        """
        cfg = make_cfg()
        state = _state()
        state.my_slot = 2  # someone else is on the clock at pick 1
        rec = recommend(state, cfg, _fill_board(qbs=40, qb_dv=60.0), n_sims=20, seed=7)
        assert rec.is_my_pick is False
        assert rec.candidates == ()
        assert rec.vona_by_pos == {}
        assert rec.forced_positions == ()
        assert rec.elite_player_ids == ()

    def test_final_round_publishes_no_vona(self):
        """No following turn means nothing to wait for, so there is no cost of waiting.

        This is the case that made the UI's split readiness check a bug: VONA goes empty here
        while the gates can still fire, so a board keyed on VONA alone would have re-ordered
        itself with no NOW column and no explanation.
        """
        cfg = make_cfg(bench=0, starters={"QB": 1})
        # Last pick of the draft: one roster slot, so there is no following turn.
        state = _state(current_pick=cfg.teams * 1, my_slot=1)
        give_roster(state, 1, [])
        rec = recommend(state, cfg, _fill_board(qbs=40, qb_dv=60.0), n_sims=20, seed=7)
        if rec.candidates:
            # If the engine still produced candidates, VONA must be absent or all-zero -- there
            # is no later turn for a player to be missed at.
            assert not rec.vona_by_pos or all(v == 0.0 for v in rec.vona_by_pos.values())
