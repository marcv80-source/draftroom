"""VONA -- value of next available: the live cost of waiting one turn.

"Can I afford to wait on this position?" is the single most common question at the table, and
the honest answer needs an *expectation*, not the naive "take the ADP-best guy who'll be there
next time". :func:`expected_best_available` is an order-statistic expectation over the
remaining pool: the expected draft value of whichever player turns out to be the best one still
standing at a future pick, given every player's own conditional survival probability
(:mod:`draftroom.draft.survival`).

:func:`vona` is then just today's best at a position minus that expectation one turn later. A
position with a hard cliff (one great player, then a canyon) has high VONA -- waiting is
expensive because the expectation collapses once that one player is gone. A deep position (WR
in most leagues) has low VONA -- the expected best-available barely moves, because there is
always another very-similar player behind the one you'd take now.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from draftroom.draft.survival import PositionalRun, SdFit, _mu_sd, _pos_of, p_available

__all__ = ["VonaResult", "expected_best_available", "vona", "vona_all_positions"]


def _dv_of(player: Any) -> float:
    """Pull a scalar draft value out of anything player-ish (mirrors ``tiers.dynamic``)."""
    if isinstance(player, Mapping):
        raw = player.get("dv", player.get("draft_value"))
    else:
        raw = getattr(player, "dv", None)
        if raw is None:
            raw = getattr(player, "draft_value", None)
    if raw is None:
        raise KeyError(f"no draft-value field (dv/draft_value) on {player!r}")
    return float(raw)


def expected_best_available(
    players: Sequence[Any],
    target_pick: float,
    current_pick: float,
    *,
    fit: SdFit | None = None,
    run: PositionalRun | None = None,
) -> float:
    """``E[max draft value among survivors at target_pick]``.

    An order-statistic expectation, built from each player's own conditional survival
    probability (:func:`~draftroom.draft.survival.p_available`): sort by draft value
    descending, then the "best-available" event for player ``i`` is "``i`` survives to
    ``target_pick`` AND every player with strictly higher draft value does not". Treating
    survival as independent across players (the same approximation
    :func:`~draftroom.draft.survival.expected_survivors` makes, and for the same reason: only
    the spread of the estimate is affected, not its mean) turns that into a running product:

    ``E = sum_i DV_i * P(i alive) * prod_{j better than i} (1 - P(j alive))``

    The missing probability mass (every ranked player already gone) contributes an implicit
    zero -- "best available" is then whatever replacement-level flex fill-in the roster
    algorithm would find, which is out of scope for this function.
    """
    ranked = sorted(players, key=lambda p: -_dv_of(p))
    total = 0.0
    prob_all_better_gone = 1.0
    for player in ranked:
        mu, sd = _mu_sd(player)
        if run is not None:
            mu = run.adjusted_mu(mu, _pos_of(player))
        p_alive = p_available(mu, sd, target_pick, current_pick, fit=fit)
        total += _dv_of(player) * p_alive * prob_all_better_gone
        prob_all_better_gone *= 1.0 - p_alive
    return total


@dataclass(frozen=True)
class VonaResult:
    """One position's live wait cost, with the two numbers that produced it kept visible."""

    position: str
    best_now: float
    expected_next: float

    @property
    def vona(self) -> float:
        return self.best_now - self.expected_next

    def describe(self) -> str:
        return (
            f"{self.position}: best now {self.best_now:.1f}, expected next turn "
            f"{self.expected_next:.1f} -> VONA {self.vona:.1f}"
        )


def vona(
    position: str,
    players: Sequence[Any],
    current_pick: float,
    next_pick: float,
    *,
    fit: SdFit | None = None,
    run: PositionalRun | None = None,
) -> VonaResult:
    """``DV(best available now at pos) - E[DV(best available at pos at next_pick)]``.

    ``players`` should be the currently-remaining pool (any positions; this filters). The
    "best available now" term needs no survival math at all -- the pool given IS who is on the
    board right now, so it is simply the max draft value in it.
    """
    pos = str(position).upper()
    pool = [p for p in players if _pos_of(p) == pos]
    if not pool:
        return VonaResult(position=pos, best_now=0.0, expected_next=0.0)

    best_now = max(_dv_of(p) for p in pool)
    expected_next = expected_best_available(pool, next_pick, current_pick, fit=fit, run=run)
    return VonaResult(position=pos, best_now=best_now, expected_next=expected_next)


def vona_all_positions(
    players: Sequence[Any],
    current_pick: float,
    next_pick: float,
    *,
    fit: SdFit | None = None,
    run: PositionalRun | None = None,
) -> dict[str, VonaResult]:
    """:func:`vona` for every position present in ``players`` -- the UI's cheat-sheet row."""
    positions = sorted({_pos_of(p) for p in players})
    return {
        pos: vona(pos, players, current_pick, next_pick, fit=fit, run=run) for pos in positions
    }
