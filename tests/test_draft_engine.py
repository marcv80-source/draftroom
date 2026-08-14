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


def test_out_of_order_pick_assigns_to_named_team(tmp_path):
    s = _session(tmp_path)
    s.record_pick("chase")
    s.record_pick("gibbs", pick_no=7, team_slot=7)
    assert s.state.picks[7].team_slot == 7
    assert s.state.picks[7].out_of_order


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
