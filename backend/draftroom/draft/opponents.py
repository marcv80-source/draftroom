"""Opponent pick model -- softmax over the available pool, calibrated per league (eventually).

Yahoo draft history for THIS league is not available yet (CLAUDE.md: gated by manual OAuth
application). Rather than wait, this module runs on **national ADP priors** today through
:meth:`LeagueCalibration.national_only`, with a calibration hook
(:meth:`LeagueCalibration.from_draft_results`) that raises until real pick-by-pick history
lands. Swapping in real calibration later changes zero call sites -- only which
``LeagueCalibration`` gets constructed.

The model, for manager ``m`` choosing among available players at pick ``t``::

    P(m picks j) ~ exp( (-mu_j_eff + g*need_m(pos_j) + h*run(pos_j)) / tau(t) )

- ``mu_j_eff``: player ``j``'s mean ADP, shifted earlier by any live positional run
  (:class:`~draftroom.draft.survival.PositionalRun`) and by this league's own timing offset
  and this manager's reach profile (both zero under :meth:`~LeagueCalibration.national_only`).
- ``need_m(pos)``: how badly manager ``m`` needs ``pos`` right now -- the fraction of that
  position's starting slots still unfilled, plus a flat bump if a flex slot is open and the
  position is flex-eligible.
- ``run(pos)``: 1.0 while :class:`PositionalRun` says ``pos`` is actively running, else 0.0.
- ``tau(t) = 4 + 0.06*t``: temperature. The board is chalky in round 1 (tight distribution
  around ADP) and chaotic by round 12 (need and randomness dominate).

**Herding is opponent-only, on purpose.** The research CLAUDE.md cites both ways: managers
demonstrably herd off recent picks at a position, AND herding does not correlate with winning.
Those two facts together mean the *correct* model has opponents herd (because that's what they
actually do, and the survival/VONA numbers need to reflect real opponent behavior) while
``draftroom.draft.recommend`` must never add a symmetrical herding bonus to Marc's own ranking
-- copying a crowd that isn't rewarded for copying itself would just be modeling our way into
their mistake. If you are tempted to add an "everyone's taking RBs, you should too" term to
recommend.py, don't: that signal already lives here, on the *opponent* side of the boundary,
and nowhere else.

**Hard constraint.** However chatty the softmax gets, a manager who is down to as many picks as
they have unfilled starter slots cannot spend one more pick on a luxury. Their choice set is
restricted to positions where they still have a starter hole (or an open flex) before the
softmax runs at all -- this is a constraint on the support, not a soft penalty.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from draftroom.config import LeagueConfig
from draftroom.draft.survival import PositionalRun, _mu_sd, _pos_of

__all__ = [
    "LeagueCalibration",
    "temperature",
    "unfilled_starters_from_counts",
    "flex_deficit_from_counts",
    "total_filled_from_counts",
    "picks_remaining_from_counts",
    "manager_need",
    "hard_constraint_positions",
    "opponent_scores",
    "opponent_pick_probabilities",
    "G_NEED",
    "H_HERD",
]

#: Coefficient on `need_m(pos)`, in "picks" of ADP-equivalent pull. Per the spec: 8.
G_NEED = 8.0

#: Coefficient on the herding indicator. Per the spec: 3.
H_HERD = 3.0


def temperature(pick_no: float) -> float:
    """``tau(t) = 4 + 0.06*t`` -- chalky early, chaotic late."""
    return 4.0 + 0.06 * float(pick_no)


# --------------------------------------------------------------------------- calibration


@dataclass(frozen=True)
class LeagueCalibration:
    """Per-league timing offsets and per-manager reach, learned from THIS league's history.

    ``position_timing_offset``: picks earlier than national ADP this room has historically
    taken a position, keyed by canonical position code. Positive = sooner than the market.

    ``manager_reach``: picks earlier than ADP a specific manager (by draft slot) tends to
    reach, independent of position -- some managers just pull the trigger early. Positive =
    reaches early.

    Both are additive adjustments to ``mu_j_eff`` (subtracted, since a smaller mu means
    sooner). Empty mappings are a no-op: :meth:`national_only` is exactly that.
    """

    position_timing_offset: Mapping[str, float] = field(default_factory=dict)
    manager_reach: Mapping[int, float] = field(default_factory=dict)

    @classmethod
    def national_only(cls) -> "LeagueCalibration":
        """Pure national-ADP calibration: zero timing offset, zero reach.

        This is what the model runs on TODAY -- Yahoo pick-by-pick history for this league
        does not exist yet (CLAUDE.md). Every ``mu_j_eff`` reduces to national ADP plus
        whatever a live positional run is doing.
        """
        return cls(position_timing_offset={}, manager_reach={})

    @classmethod
    def from_draft_results(cls, results: Any) -> "LeagueCalibration":
        """Fit timing offsets and reach profiles from this league's actual pick history.

        STUB. Requires Yahoo OAuth `fspt-r` access to the league's prior-season pick-by-pick
        results (CLAUDE.md's data-sources table), which is gated behind manual application and
        not available on this machine yet. When it lands, this should:

          1. For each position, compare this league's mean pick number to the same season's
             national ADP mean for players at that ADP, giving ``position_timing_offset``.
          2. For each manager (draft slot), compare their picks to national ADP at time of
             pick, averaged, giving ``manager_reach``.

        Raises:
            NotImplementedError: always, until real draft history is wired in.
        """
        raise NotImplementedError(
            "LeagueCalibration.from_draft_results needs Yahoo pick-by-pick history, which "
            "is not available yet (OAuth access gated by manual application -- see "
            "CLAUDE.md's data-sources table). Use LeagueCalibration.national_only() until "
            "then."
        )


# --------------------------------------------------------------------------- roster counts
#
# The opponent model and the Monte-Carlo simulator both need "how many players does team X
# have at position P" many thousands of times over a draft-night session, and the simulator
# needs it once per opponent pick per Monte-Carlo trial. Neither can afford to replay the full
# event log or touch DraftState's dataclasses that often, so both work off a plain
# `Mapping[str, int]` snapshot (`have`) instead. `initial roster counts for every team come
# from DraftState.roster_positions(...) once per recommend() / simulate_forward() call; after
# that, everything below is dict arithmetic.


def unfilled_starters_from_counts(have: Mapping[str, int], cfg: LeagueConfig) -> dict[str, int]:
    """Mirrors ``DraftState.unfilled_starters`` but off a plain counts dict, not a replay."""
    return {
        pos: max(0, need - have.get(pos, 0))
        for pos, need in cfg.starters.items()
        if max(0, need - have.get(pos, 0)) > 0
    }


def flex_deficit_from_counts(have: Mapping[str, int], cfg: LeagueConfig) -> int:
    """How many flex slots are still open, given aggregate counts at flex-eligible positions.

    APPROXIMATION: this does not track which specific rostered player is assigned to which
    slot (starter vs. flex) -- it only compares the aggregate count of players at
    flex-eligible positions against the starters required there. That is fine because flex is
    fungible by construction: the roster-construction question "is there an open flex slot"
    only depends on the total supply of flex-eligible players versus the total demand for
    them (dedicated starters + flex), not on which specific player is "in" which slot.
    """
    if cfg.flex_slots <= 0:
        return 0
    have_eligible = sum(have.get(p, 0) for p in cfg.flex_eligible)
    dedicated_demand = sum(cfg.starters.get(p, 0) for p in cfg.flex_eligible)
    surplus = have_eligible - dedicated_demand
    # `surplus` can be arbitrarily negative early in a draft (a team with zero RB/WR/TE is
    # nowhere near even covering its DEDICATED starters yet, long before flex is the concern).
    # The number of flex slots actually FILLED can never exceed flex_slots itself or be
    # negative, so it must be clamped before subtracting -- otherwise a very negative surplus
    # inflates the deficit past the real number of flex slots that exist (a team with nothing
    # drafted would otherwise show a deficit of 6+ against a league with only 1 flex slot).
    filled = min(cfg.flex_slots, max(0, surplus))
    return cfg.flex_slots - filled


def total_filled_from_counts(have: Mapping[str, int]) -> int:
    return sum(have.values())


def picks_remaining_from_counts(have: Mapping[str, int], cfg: LeagueConfig) -> int:
    """Roster spots (of any kind) this team has left to fill, assuming they draft to a full roster."""
    return max(0, cfg.roster_size - total_filled_from_counts(have))


def manager_need(have: Mapping[str, int], cfg: LeagueConfig) -> dict[str, float]:
    """``need_m(pos)`` for every position this league has lineup demand for.

    ``unfilled_starter_slots(pos) / total_slots(pos) + 0.25`` if a flex slot is open and
    ``pos`` is flex-eligible, per the spec. Positions with zero dedicated starter slots (pure
    flex-only positions, if a league ever has one) get just the flex bump.
    """
    unfilled = unfilled_starters_from_counts(have, cfg)
    flex_open = flex_deficit_from_counts(have, cfg) > 0

    need: dict[str, float] = {}
    for pos, total in cfg.starters.items():
        if total <= 0:
            continue
        ratio = unfilled.get(pos, 0) / total
        bump = 0.25 if (flex_open and pos in cfg.flex_eligible) else 0.0
        need[pos] = ratio + bump
    for pos in cfg.flex_eligible:
        if pos not in need:
            need[pos] = 0.25 if flex_open else 0.0
    return need


def hard_constraint_positions(
    have: Mapping[str, int], cfg: LeagueConfig
) -> frozenset[str] | None:
    """The HARD CONSTRAINT: if remaining picks <= unfilled starter slots, only needed positions
    are legal. Returns ``None`` when there is no restriction (plenty of picks left to fill holes
    later), or the restricted set of positions when the constraint binds.
    """
    unfilled = unfilled_starters_from_counts(have, cfg)
    flex_deficit = flex_deficit_from_counts(have, cfg)
    total_needed = sum(unfilled.values()) + flex_deficit
    if total_needed <= 0:
        return None
    remaining = picks_remaining_from_counts(have, cfg)
    if remaining > total_needed:
        return None
    allowed = set(unfilled.keys())
    if flex_deficit > 0:
        allowed |= set(cfg.flex_eligible)
    return frozenset(allowed)


# --------------------------------------------------------------------------- softmax model


def _adjusted_mu(
    mu: float,
    pos: str,
    *,
    team_slot: int,
    calibration: LeagueCalibration,
    run: PositionalRun | None,
) -> float:
    if run is not None:
        mu = run.adjusted_mu(mu, pos)
    mu -= calibration.position_timing_offset.get(pos, 0.0)
    mu -= calibration.manager_reach.get(team_slot, 0.0)
    return mu


def opponent_scores(
    available: Sequence[Any],
    *,
    team_slot: int,
    pick_no: float,
    have: Mapping[str, int],
    cfg: LeagueConfig,
    calibration: LeagueCalibration | None = None,
    run: PositionalRun | None = None,
    g: float = G_NEED,
    h: float = H_HERD,
    resolved: Mapping[str, tuple[float, float | None, str]] | None = None,
) -> dict[str, float]:
    """Unnormalized softmax LOGITS (score / tau already applied) per available player.

    Positions outside the hard-constraint set (when it binds) are simply absent from the
    result -- excluded from the support, not down-weighted.

    Args:
        resolved: optional ``player_id -> (adp, stdev, pos)`` cache. ``draftroom.draft.survival``'s
            duck-typed ``_mu_sd``/``_pos_of`` helpers are correct but resolve a player's shape
            (mapping? dataclass? tuple?) via ``isinstance`` checks against ``typing`` generics on
            every call -- fine occasionally, measurably slow when called for every available
            player on every one of thousands of simulated opponent picks
            (:mod:`draftroom.draft.simulate` is exactly that hot loop). Passing a pre-resolved
            cache, built once per player rather than once per pick, is the fix; omitting it just
            falls back to the general (slower) duck-typed path, which is fine for one-off calls.
    """
    calibration = calibration or LeagueCalibration.national_only()
    allowed = hard_constraint_positions(have, cfg)
    need = manager_need(have, cfg)
    tau = temperature(pick_no)

    scores: dict[str, float] = {}
    for p in available:
        if resolved is not None:
            pid0 = str(getattr(p, "player_id", None) or (p.get("player_id") if isinstance(p, Mapping) else None))
            mu, _sd, pos = resolved[pid0]
        else:
            pos = _pos_of(p)
            mu, _sd = _mu_sd(p)
        if allowed is not None and pos not in allowed:
            continue
        mu_eff = _adjusted_mu(mu, pos, team_slot=team_slot, calibration=calibration, run=run)
        # Herd term: whether `pos` has *live residual momentum* right now. `PositionalRun.shift`
        # is already exactly that signal -- nonzero only while a run fired recently and hasn't
        # decayed away (see PositionalRun.observe/stale_decay) -- so reusing it here means the
        # hot per-pick scoring loop never has to re-sort the remaining pool to answer "is this
        # position running" (that O(n log n) work already happened once, in `observe`, when the
        # shift was armed).
        herd = h * (1.0 if (run is not None and run.shift(pos) > 0.0) else 0.0)
        util = -mu_eff + g * need.get(pos, 0.0) + herd
        if resolved is not None:
            pid = pid0
        else:
            pid = getattr(p, "player_id", None)
            if pid is None and isinstance(p, Mapping):
                pid = p.get("player_id")
        scores[str(pid)] = util / tau
    return scores


def opponent_pick_probabilities(
    available: Sequence[Any],
    *,
    team_slot: int,
    pick_no: float,
    have: Mapping[str, int],
    cfg: LeagueConfig,
    calibration: LeagueCalibration | None = None,
    run: PositionalRun | None = None,
    g: float = G_NEED,
    h: float = H_HERD,
    resolved: Mapping[str, tuple[float, float | None, str]] | None = None,
) -> dict[str, float]:
    """Normalized ``P(m picks j)`` over ``available``, respecting the hard constraint."""
    scores = opponent_scores(
        available,
        team_slot=team_slot,
        pick_no=pick_no,
        have=have,
        cfg=cfg,
        calibration=calibration,
        run=run,
        g=g,
        h=h,
        resolved=resolved,
    )
    if not scores:
        return {}
    top = max(scores.values())
    exps = {pid: math.exp(s - top) for pid, s in scores.items()}
    z = sum(exps.values())
    return {pid: v / z for pid, v in exps.items()}
