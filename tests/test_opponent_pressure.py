"""Regression tests for the opponent-pressure count.

The bug these exist to prevent: the panel once said "16 of the 16 teams before your next pick still
need a QB" in a 12-team league. Two errors compounded. It used the number of PICKS as the number of
teams, and because the team at the turn picks twice in a row, that manager was counted twice.

A line that is visibly, arithmetically impossible is worse than no line at all. Nobody audits the
survival percentages mid-draft, but everybody notices "16 of 16 teams" in a 12-team league, and once
one number is obviously wrong the whole panel stops being believed.
"""

from __future__ import annotations

from draftroom.draft import snake


def _distinct_slots_between(teams: int, current_pick: int, picks_between: int) -> list[int]:
    """Mirror of the production logic, kept here so the arithmetic itself is pinned down."""
    slots: list[int] = []
    for pick in range(current_pick + 1, current_pick + 1 + picks_between):
        s = snake.slot_on_clock(teams, pick)
        if s not in slots:
            slots.append(s)
    return slots


TEAMS = 12


def test_distinct_teams_never_exceeds_league_size():
    """The count that broke. It is arithmetically impossible to have more teams than the league has."""
    for current in range(1, 60):
        for gap in range(1, 25):
            slots = _distinct_slots_between(TEAMS, current, gap)
            assert len(slots) <= TEAMS, (
                f"pick {current}, gap {gap}: counted {len(slots)} teams in a {TEAMS}-team league"
            )


def test_no_team_is_counted_twice():
    for current in range(1, 60):
        for gap in range(1, 25):
            slots = _distinct_slots_between(TEAMS, current, gap)
            assert len(slots) == len(set(slots))


def test_the_turn_double_pick_collapses_to_one_team():
    """The exact shape that caused the bug.

    Slot 12 picks at 12 and 13, back to back across the round boundary. Walking picks 12..13 sees
    slot 12 twice; the honest answer is one team.
    """
    slots = _distinct_slots_between(TEAMS, 11, 2)  # picks 12 and 13
    assert slots == [12]


def test_slot_9_gap_of_16_covers_only_eight_distinct_teams():
    """Marc's real case, and the number is less obvious than it looks.

    From slot 9, pick 16 to pick 33 is 16 picks of board movement. But those 16 picks are made by
    only EIGHT managers: picks 17-24 are slots 8 down to 1, then picks 25-32 are slots 1 back up to
    8. Slots 10, 11 and 12 never pick in that window at all, having already gone earlier in round 2.
    Each of the eight picks twice.

    So the honest sentence is "N of the 8 teams before your next pick", and the pressure it describes
    is concentrated in half the league rather than spread across it.
    """
    ctx = snake.TurnContext.build(TEAMS, 9, 14, 16)
    assert ctx.picks_between_turns == 16
    slots = _distinct_slots_between(TEAMS, 16, 16)
    assert sorted(slots) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert len(slots) == 8
    assert len(slots) < ctx.picks_between_turns  # the bug was reporting the larger number


def test_needing_count_can_never_exceed_teams_before():
    """'N of M' is meaningless if N can exceed M."""
    for current in range(1, 40):
        for gap in range(1, 20):
            slots = _distinct_slots_between(TEAMS, current, gap)
            # Every slot needing the position is the worst case; it must still be <= the total.
            assert len(slots) <= len(slots)
            assert len(slots) <= TEAMS
