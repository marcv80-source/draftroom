"""Shared scarcity-floor math -- ONE implementation for the live engine AND the simulators.

Exists because of a 2026-08-18 Codex review finding: the live engine's scarcity trigger and the
strategy tournament's validated trigger were two separate implementations that disagreed both on
what counts as "startable" (the live one used ``dv > 0`` over a placeholder valuation, the
tournament used a man-games rank cutoff) and on the trigger arithmetic. A trigger that was
validated in one form and deployed in another isn't validated at all. Everything here is plain
counting on inputs both sides can supply, so ``draftroom.draft.recommend`` and
the strategy tournament (retired 2026-08-25) literally share this code.

The trigger arithmetic also fixes a real over-fire in the old form. The old trigger,
``startable_remaining - leaguewide_unfilled <= gap``, held demand constant while assuming every
intervening opponent pick consumes supply -- but an opponent filling their own starter slot
consumes one unit of supply AND one unit of demand, leaving the cushion unchanged. (Codex's
example: 21 startable QBs, 20 open slots leaguewide, an 18-pick gap -- the old form fires
immediately even though need-driven QB picks preserve the one-player cushion all the way down.)
What actually shuts Marc out is supply falling below HIS OWN unfilled need before his next
turn, and the most an opponent can need-consume in the gap is bounded by their own unfilled
slots and how many times they pick. Room evidence backs need-bounded consumption: 20 of 21 QB
picks before pick 85 in the real 2025 draft were made by teams with an open QB starter slot.
"""

from __future__ import annotations

from collections import Counter
from typing import Mapping, Sequence

from draftroom.config import LeagueConfig

__all__ = [
    "startable_rank_cutoff",
    "opponent_consumption_bound",
    "scarcity_trigger_fires",
]


def startable_rank_cutoff(cfg: LeagueConfig, pos: str = "QB") -> int:
    """How many players at ``pos`` (best-to-worst) this league's man-games demand can call
    'startable', from PRESEASON durability priors alone.

    Moved here (generalized) from the strategy tournament (retired 2026-08-25)'s ``qb_startable_rank`` so
    the live engine uses the exact cutoff the tournament validated. No real outcome, no ADP:
    just ``teams``, ``starters[pos]``, ``weeks``, and the repo's rank-conditional availability
    curve. Demand = ``teams * starters[pos] * weeks`` man-games, covered by accumulating each
    rank's own expected games until demand is met. At this league's real settings (10 teams,
    2 QB, 17 weeks) the QB cutoff lands at 22 -- matching CLAUDE.md's own "replacement level
    QB is QB22" line.

    Only meaningful for a DEDICATED (non-flex) position: flex-eligible supply is fungible
    across RB/WR/TE, so a per-position man-games walk understates their effective demand.
    Callers guard on that (both existing callers only use QB in this league).
    """
    from draftroom.valuation.replacement import expected_games

    demand = float(cfg.teams) * float(cfg.starters.get(pos, 0)) * float(cfg.weeks)
    covered = 0.0
    rank = 0
    while covered < demand and rank < 10_000:
        rank += 1
        covered += expected_games(pos, rank=rank, weeks=cfg.weeks)
    return max(1, rank)


def opponent_consumption_bound(
    gap_pick_slots: Sequence[int], unfilled_by_slot: Mapping[int, int]
) -> int:
    """Upper bound on how many startable players AT ONE POSITION the opponents picking in the
    gap can need-consume before my next turn.

    ``gap_pick_slots``: the team slot on the clock for each pick strictly between my current
    pick and my next one (a slot appears twice at the turn). ``unfilled_by_slot``: each slot's
    unfilled starter count at the position in question. A team can consume at most
    ``min(times it picks in the gap, its own unfilled slots)`` -- it cannot need two QBs it
    doesn't have room to start, and it cannot take two players with one pick.
    """
    counts = Counter(gap_pick_slots)
    return sum(
        min(n_picks, max(0, int(unfilled_by_slot.get(slot, 0))))
        for slot, n_picks in counts.items()
    )


def scarcity_trigger_fires(
    *, startable_remaining: int, opponent_consumption_bound: int, my_unfilled: int
) -> bool:
    """Force the position when worst-case need-driven consumption leaves less supply than MY
    unfilled starters need. ``supply - consumption < my_need`` -- see the module docstring for
    why demand-neutral opponent fills must not fire this."""
    if my_unfilled <= 0:
        return False
    return startable_remaining - opponent_consumption_bound < my_unfilled
