"""Snake draft order arithmetic.

Everything in the live tool keys off "how many picks until my next turn", not "what round is it",
because that gap is what actually determines whether Marc can afford to wait on a position.
In a snake it alternates between two values, and at the turn it collapses to 1.
"""

from __future__ import annotations

from dataclasses import dataclass


def overall_pick(teams: int, rnd: int, slot: int) -> int:
    """Overall pick number (1-indexed) for `slot` in round `rnd`.

    Odd rounds run 1..teams, even rounds run teams..1.
    """
    if not 1 <= slot <= teams:
        raise ValueError(f"slot {slot} outside 1..{teams}")
    if rnd < 1:
        raise ValueError(f"round {rnd} must be >= 1")
    if rnd % 2 == 1:
        return (rnd - 1) * teams + slot
    return rnd * teams - slot + 1


def slot_on_clock(teams: int, pick_no: int) -> int:
    """Which draft slot owns overall pick `pick_no` (1-indexed)."""
    if pick_no < 1:
        raise ValueError(f"pick {pick_no} must be >= 1")
    rnd = (pick_no - 1) // teams + 1
    idx = (pick_no - 1) % teams  # 0-indexed position within the round
    return idx + 1 if rnd % 2 == 1 else teams - idx


def round_of(teams: int, pick_no: int) -> int:
    return (pick_no - 1) // teams + 1


def pick_label(teams: int, pick_no: int) -> str:
    """Human draft-board label, e.g. pick 25 in a 12-team league -> '3.01'."""
    rnd = round_of(teams, pick_no)
    within = pick_no - (rnd - 1) * teams
    return f"{rnd}.{within:02d}"


def my_picks(teams: int, slot: int, rounds: int) -> list[int]:
    """Every overall pick number belonging to `slot`, in order."""
    return [overall_pick(teams, r, slot) for r in range(1, rounds + 1)]


def next_pick_for(teams: int, slot: int, rounds: int, after: int) -> int | None:
    """The slot's next overall pick strictly after pick number `after`."""
    for p in my_picks(teams, slot, rounds):
        if p > after:
            return p
    return None


def picks_until_next(teams: int, slot: int, rounds: int, current: int) -> int | None:
    """How many picks elapse between `current` and the slot's next turn.

    0 means the slot is on the clock right now with another pick immediately after.
    None means the slot has no picks left.
    """
    nxt = next_pick_for(teams, slot, rounds, current)
    if nxt is None:
        return None
    return nxt - current


@dataclass(frozen=True)
class TurnContext:
    """What the recommendation engine needs to know about Marc's position in the order.

    `at_the_turn` is the case Marc specifically called out: near the round boundary he picks twice in
    quick succession, which changes what he can afford to let slide. `gap_to_next` of 1 or 2 means the
    board barely moves before he's up again; a gap of 20 means it moves a lot.
    """

    teams: int
    slot: int
    rounds: int
    current_pick: int
    next_pick: int | None
    following_pick: int | None

    @property
    def gap_to_next(self) -> int | None:
        if self.next_pick is None:
            return None
        return self.next_pick - self.current_pick

    @property
    def picks_between_turns(self) -> int | None:
        """Opponent picks that occur between my next pick and the one after it."""
        if self.next_pick is None or self.following_pick is None:
            return None
        return self.following_pick - self.next_pick - 1

    @property
    def at_the_turn(self) -> bool:
        """True when my next two picks are close enough to plan as a pair.

        The snake gap at the turn is 1 (back-to-back across the round boundary); we allow 2 so the
        pair logic engages one pick early rather than one pick late.
        """
        between = self.picks_between_turns
        return between is not None and between <= 2

    @classmethod
    def build(cls, teams: int, slot: int, rounds: int, current_pick: int) -> TurnContext:
        nxt = next_pick_for(teams, slot, rounds, current_pick - 1)
        following = next_pick_for(teams, slot, rounds, nxt) if nxt is not None else None
        return cls(
            teams=teams,
            slot=slot,
            rounds=rounds,
            current_pick=current_pick,
            next_pick=nxt,
            following_pick=following,
        )
