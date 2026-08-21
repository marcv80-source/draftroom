"""Tests for snake arithmetic, the event log, and crash recovery.

The recovery tests matter more than they look: the failure they guard against happens once, on the
one night of the year when it cannot be debugged.
"""

from __future__ import annotations

import pytest

from draftroom.draft import snake
from draftroom.draft.events import CorruptEventLog, EventLog
from draftroom.draft.state import DraftSession, DraftState

TEAMS = 12
ROUNDS = 14


# --------------------------------------------------------------------------- snake math


def test_overall_pick_snake_reverses_on_even_rounds():
    assert snake.overall_pick(TEAMS, 1, 1) == 1
    assert snake.overall_pick(TEAMS, 1, 12) == 12
    # Round 2 runs backwards: slot 12 picks first.
    assert snake.overall_pick(TEAMS, 2, 12) == 13
    assert snake.overall_pick(TEAMS, 2, 1) == 24
    assert snake.overall_pick(TEAMS, 3, 1) == 25


def test_slot_on_clock_is_the_inverse_of_overall_pick():
    for rnd in range(1, ROUNDS + 1):
        for slot in range(1, TEAMS + 1):
            n = snake.overall_pick(TEAMS, rnd, slot)
            assert snake.slot_on_clock(TEAMS, n) == slot
            assert snake.round_of(TEAMS, n) == rnd


def test_pick_label_matches_draft_board_convention():
    assert snake.pick_label(TEAMS, 1) == "1.01"
    assert snake.pick_label(TEAMS, 13) == "2.01"
    assert snake.pick_label(TEAMS, 25) == "3.01"


def test_slot_9_gap_alternates_and_collapses_at_the_turn():
    """Marc's stated scenario: picking 9th, how long until he's up again.

    From slot 9 in a 12-team draft the gap alternates 7 / 17 -- he waits half a round, then a round
    and a half. That asymmetry is exactly why 'can I wait on QB?' has a different answer depending on
    which side of the turn he's on.
    """
    picks = snake.my_picks(TEAMS, 9, 6)
    assert picks == [9, 16, 33, 40, 57, 64]
    gaps = [b - a for a, b in zip(picks, picks[1:])]
    assert gaps == [7, 17, 7, 17, 7]


def test_turn_context_flags_back_to_back_picks():
    # At pick 16 (slot 9's second pick), the next one is 33 -- 16 picks of board movement between.
    ctx = snake.TurnContext.build(TEAMS, 9, ROUNDS, 16)
    assert ctx.next_pick == 16
    assert ctx.following_pick == 33
    assert ctx.picks_between_turns == 16
    assert not ctx.at_the_turn

    # Slot 12 at pick 12 is the true turn: picks 12 and 13 back to back, nothing in between.
    ctx = snake.TurnContext.build(TEAMS, 12, ROUNDS, 12)
    assert ctx.next_pick == 12
    assert ctx.following_pick == 13
    assert ctx.picks_between_turns == 0
    assert ctx.at_the_turn


def test_overall_pick_rejects_out_of_range_slot():
    with pytest.raises(ValueError):
        snake.overall_pick(TEAMS, 1, 13)


# --------------------------------------------------------------------------- event log


def test_append_then_read_round_trips(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    log.append("pick", pick_no=1, player_id="p1")
    log.append("pick", pick_no=2, player_id="p2")
    evs = log.events()
    assert [e.seq for e in evs] == [1, 2]
    assert evs[1].payload["player_id"] == "p2"


def test_sequence_continues_after_reopening_the_log(tmp_path):
    path = tmp_path / "events.jsonl"
    EventLog(path).append("pick", pick_no=1, player_id="p1")
    reopened = EventLog(path)  # simulates a relaunch
    ev = reopened.append("pick", pick_no=2, player_id="p2")
    assert ev.seq == 2


def test_torn_final_line_is_reported_not_silently_dropped(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    log.append("pick", pick_no=1, player_id="p1")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"seq": 2, "type": "pi')  # power cut mid-write
    with pytest.raises(CorruptEventLog):
        EventLog(path).events()


# --------------------------------------------------------------------------- state replay


def _session(tmp_path, slot=9):
    return DraftSession(
        EventLog(tmp_path / "draft.jsonl"), teams=TEAMS, rounds=ROUNDS, my_slot=slot
    )


def test_one_keystroke_pick_assigns_to_team_on_clock_and_advances(tmp_path):
    """The core loop: Marc types a name, hits Enter, and the pick lands on the right team.

    This is what buys full opponent rosters for one keystroke per pick.
    """
    s = _session(tmp_path)
    assert s.state.slot_on_clock == 1
    s.record_pick("chase")
    assert s.state.picks[1].team_slot == 1
    assert s.state.current_pick == 2
    assert s.state.slot_on_clock == 2
    s.record_pick("gibbs")
    assert s.state.picks[2].team_slot == 2
    assert s.state.drafted_player_ids == {"chase", "gibbs"}


def test_pick_assignment_follows_the_snake_into_round_two(tmp_path):
    s = _session(tmp_path)
    for i in range(13):
        s.record_pick(f"p{i}")
    # Pick 13 is the first of round 2, which belongs to slot 12 again.
    assert s.state.picks[12].team_slot == 12
    assert s.state.picks[13].team_slot == 12


def test_undo_returns_the_player_to_the_pool(tmp_path):
    s = _session(tmp_path)
    s.record_pick("chase")
    s.record_pick("gibbs")
    s.undo_last()
    assert s.state.drafted_player_ids == {"chase"}
    assert s.state.current_pick == 2


def test_undo_is_repeatable_lifo(tmp_path):
    s = _session(tmp_path)
    s.record_pick("a")
    s.record_pick("b")
    s.record_pick("c")
    s.undo_last()
    s.undo_last()
    assert s.state.drafted_player_ids == {"a"}


def test_correcting_a_past_pick_frees_the_wrong_player(tmp_path):
    """Wrong name entered three rounds ago. The fix must put that player back on the board."""
    s = _session(tmp_path)
    s.record_pick("chase")
    s.record_pick("gibbs")
    s.record_pick("nabers")
    s.correct_pick(2, player_id="achane")
    assert "gibbs" not in s.state.drafted_player_ids
    assert "achane" in s.state.drafted_player_ids
    assert s.state.picks[2].team_slot == 2  # ownership unchanged


def test_out_of_order_means_not_the_team_on_the_clock_not_merely_an_explicit_slot(tmp_path):
    """`out_of_order` is a COMPUTED fact about the pick, not a note about how it was requested.

    The old rule was `out_of_order = team_slot is not None`, which was harmless while the only
    way to name a slot was the explicit out-of-turn command. Click-anywhere drafting (plan A2)
    always sends a slot -- the picker defaults to whoever is on the clock -- so every ordinary
    pick started rendering an OOO badge in the Draft Results tab (Codex 2026-08-21 finding 7). On
    a tool whose first job is bookkeeping, a flag that fires on everything is worse than no flag.
    """
    s = _session(tmp_path)

    # Naming the slot that IS on the clock: ordinary pick, no flag. This is the click-anywhere
    # default path, and it is the case the old rule got wrong.
    assert s.state.slot_on_clock == 1
    s.record_pick("chase", team_slot=1)
    assert s.state.picks[1].team_slot == 1
    assert not s.state.picks[1].out_of_order

    # Pick 7 belongs to slot 7 in round 1, so recording it there is in order even though both
    # the pick number and the slot were supplied explicitly.
    s.record_pick("gibbs", pick_no=7, team_slot=7)
    assert s.state.picks[7].team_slot == 7
    assert not s.state.picks[7].out_of_order

    # Genuinely out of order: pick 8 went to a team that was not on the clock for it.
    s.record_pick("bijan", pick_no=8, team_slot=3)
    assert s.state.picks[8].team_slot == 3
    assert s.state.picks[8].out_of_order


def test_out_of_order_is_recomputed_on_replay_so_a_stale_payload_flag_cannot_win(tmp_path):
    """Old logs still carry `out_of_order` in the payload. Replay must ignore it and recompute,
    or every pick recorded before this fix would keep its wrong badge forever."""
    log = EventLog(tmp_path / "draft.jsonl")
    # Hand-write the pre-fix shape: an in-order pick that the old command layer flagged OOO.
    log.append("pick", pick_no=1, team_slot=1, player_id="chase", out_of_order=True)
    st = DraftState.replay(log.events(), teams=TEAMS, rounds=ROUNDS, my_slot=9)
    assert not st.picks[1].out_of_order


def test_missed_picks_are_reported_as_gaps(tmp_path):
    """Marc looks up and the board is three stickers ahead of him."""
    s = _session(tmp_path)
    s.record_pick("chase")
    s.set_clock(5)
    assert s.state.gaps() == [2, 3, 4]
    # Backfilling closes the gap without dragging the clock backwards.
    s.record_pick("gibbs", pick_no=3)
    assert s.state.gaps() == [2, 4]
    assert s.state.current_pick == 5


def test_stub_player_still_consumes_a_roster_spot_at_its_position(tmp_path):
    """A player we've never heard of still tells the opponent model that a QB came off the board."""
    s = _session(tmp_path)
    s.record_stub("Some Rookie", "QB")
    counts = s.state.roster_positions(1, pos_of={})
    assert counts == {"QB": 1}


def test_state_survives_a_crash_and_relaunch(tmp_path):
    """The scenario that actually matters: the app dies in round 4 and comes back."""
    path = tmp_path / "draft.jsonl"
    s = DraftSession(EventLog(path), teams=TEAMS, rounds=ROUNDS, my_slot=9)
    for i in range(40):
        s.record_pick(f"p{i}")
    s.undo_last()
    s.correct_pick(5, player_id="corrected")
    before = (
        s.state.current_pick,
        sorted(s.state.drafted_player_ids),
        {n: p.team_slot for n, p in s.state.picks.items()},
    )

    del s  # process dies

    recovered = DraftSession(EventLog(path), teams=TEAMS, rounds=ROUNDS, my_slot=9)
    after = (
        recovered.state.current_pick,
        sorted(recovered.state.drafted_player_ids),
        {n: p.team_slot for n, p in recovered.state.picks.items()},
    )
    assert before == after


def test_unfilled_starters_drives_opponent_need(tmp_path):
    """Two mandatory QB slots means a team with one QB still has a hole. That's the run predictor."""
    s = _session(tmp_path)
    s.record_pick("qb1")  # slot 1
    pos_of = {"qb1": "QB"}
    starters = {"QB": 2, "RB": 2, "WR": 3, "TE": 1}
    holes = s.state.unfilled_starters(1, starters, pos_of)
    assert holes["QB"] == 1
    assert holes["RB"] == 2
    # A team that hasn't picked at all is short both QBs.
    assert s.state.unfilled_starters(5, starters, pos_of)["QB"] == 2


def test_team_named_sets_and_clears_name_with_last_event_winning(tmp_path):
    """Last event wins on replay (plan A1): renaming a slot twice keeps only the final name,
    and an empty-string name clears it back to the default rather than storing ''."""
    s = _session(tmp_path, slot=1)
    s.set_team_name(3, "Country Club Boys")
    assert s.state.team_names[3] == "Country Club Boys"
    s.set_team_name(3, "Renamed Later")
    assert s.state.team_names == {3: "Renamed Later"}, "the later event must win"

    s.set_team_name(3, "")
    assert 3 not in s.state.team_names, "an empty name clears the slot rather than storing ''"


def test_team_label_precedence_name_wins_then_you_then_team_n(tmp_path):
    s = _session(tmp_path, slot=9)
    # No name set anywhere: my own slot is YOU, everyone else is Team N.
    assert s.state.team_label(9) == "YOU"
    assert s.state.team_label(2) == "Team 2"

    # A name set for MY OWN slot outranks the "YOU" default.
    s.set_team_name(9, "Country Club Boys")
    assert s.state.team_label(9) == "Country Club Boys"

    # A name set for another slot outranks "Team N".
    s.set_team_name(2, "Jaxson Fart")
    assert s.state.team_label(2) == "Jaxson Fart"

    # Clearing slot 9's name falls back to YOU again, not to "Team 9".
    s.set_team_name(9, "  ")  # whitespace-only also clears, since it's stripped
    assert s.state.team_label(9) == "YOU"


def test_team_names_survive_replay(tmp_path):
    path = tmp_path / "draft.jsonl"
    s = DraftSession(EventLog(path), teams=TEAMS, rounds=ROUNDS, my_slot=9)
    s.set_team_name(1, "Country Club Boys")
    s.set_team_name(2, "Jaxson Fart")
    s.set_team_name(1, "Renamed")

    rebuilt = DraftState.replay(EventLog(path).events(), teams=TEAMS, rounds=ROUNDS, my_slot=9)
    assert rebuilt.team_names == {1: "Renamed", 2: "Jaxson Fart"}
    assert rebuilt == s.state


def test_pick_corrected_with_team_slot_reassigns_ownership(tmp_path):
    """Reassign-to-team (plan A3): a correction that carries team_slot moves ownership."""
    s = _session(tmp_path, slot=1)
    s.record_pick("chase")  # pick 1 -> slot 1
    assert s.state.picks[1].team_slot == 1

    s.correct_pick(1, player_id="chase", team_slot=5)
    assert s.state.picks[1].team_slot == 5
    assert s.state.picks[1].player_id == "chase", "the player itself is untouched"


def test_pick_corrected_without_team_slot_leaves_ownership_unchanged(tmp_path):
    """Byte-for-byte regression: a correction that never mentions team_slot must behave exactly
    as it did before reassign-to-team existed."""
    s = _session(tmp_path, slot=1)
    s.record_pick("chase")  # pick 1 -> slot 1
    s.correct_pick(1, player_id="nabers")
    assert s.state.picks[1].team_slot == 1
    assert s.state.picks[1].player_id == "nabers"


def test_reassigned_team_slot_survives_replay(tmp_path):
    path = tmp_path / "draft.jsonl"
    s = DraftSession(EventLog(path), teams=TEAMS, rounds=ROUNDS, my_slot=9)
    s.record_pick("chase")
    s.correct_pick(1, player_id="chase", team_slot=7)

    rebuilt = DraftState.replay(EventLog(path).events(), teams=TEAMS, rounds=ROUNDS, my_slot=9)
    assert rebuilt.picks[1].team_slot == 7
    assert rebuilt == s.state


def test_replay_is_the_only_source_of_truth(tmp_path):
    """Rebuilding from the log must equal the live object, or crash recovery is a lie."""
    path = tmp_path / "draft.jsonl"
    s = DraftSession(EventLog(path), teams=TEAMS, rounds=ROUNDS, my_slot=9)
    s.record_pick("a")
    s.record_stub("Mystery Guy", "RB")
    s.record_pick("c", pick_no=9, team_slot=9)
    s.undo_last()
    s.set_clock(4)

    rebuilt = DraftState.replay(
        EventLog(path).events(), teams=TEAMS, rounds=ROUNDS, my_slot=9
    )
    assert rebuilt == s.state
