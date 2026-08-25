"""Monte Carlo opponent simulation from the current pick through Marc's next two turns.

``recommend.py`` needs an honest answer to "what does the board look like when I pick again",
and the analytic survival math in :mod:`draftroom.draft.survival` only answers that per-player,
independent of what any *specific* opponent needs (it is pure ADP-plus-run). The opponent model
in :mod:`draftroom.draft.opponents` is need-aware and constraint-aware (a team out of picks and
short a starter MUST take that position), and that behavior only shows up in aggregate once you
actually roll it forward pick by pick. This module is that roll-forward, repeated ``n_sims``
times so the answer comes with a spread, not just a mean.

One simulation trial:

1. Start at ``state.current_pick`` with the real board (whatever is undrafted right now).
2. If ``my_pick_at_next`` is given, Marc's own next pick (``TurnContext.next_pick``) is *not*
   sampled -- it is fixed to that player, because the whole point of calling this from
   ``recommend.py`` is "what does the board look like if I take X now".
3. Every pick that belongs to an opponent between now and ``TurnContext.following_pick`` is
   drawn from :func:`~draftroom.draft.opponents.opponent_pick_probabilities`, using a
   **per-simulation** :class:`~draftroom.draft.survival.PositionalRun` so each trial's herd
   dynamics are its own (trial 3 might catch a real QB run; trial 4 might not).
4. The set of players still on the board at ``next_pick`` and at ``following_pick`` is recorded.

Performance: the per-pick work is O(available players), and the horizon between two of Marc's
own picks in a 12-team snake is at most ~2*teams-2 opponent picks (worse near the turn, much
better at it) -- a few dozen at most. 500 trials of a few dozen picks each over a ~200-player
pool is small enough that a plain Python loop over trials, with each pick's scoring vectorized
in numpy, comfortably clears the "well under 2 seconds" bar (measured, not assumed -- see
``tests/test_recommend.py`` for the real number on this
machine).
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from draftroom.config import LeagueConfig
from draftroom.draft import opponents as opp
from draftroom.draft import snake
from draftroom.draft.state import DraftState
from draftroom.draft.survival import PositionalRun, _mu_sd, _pos_of

__all__ = ["SimResult", "SimulationSummary", "simulate_forward"]


@dataclass(frozen=True)
class SimResult:
    """One trial's outcome: who was still on the board at each of Marc's next two picks."""

    survived_at_next: frozenset[str]
    survived_at_following: frozenset[str] | None


@dataclass(frozen=True)
class SimulationSummary:
    """``n_sims`` trials plus the timing, so a caller can report both the answer and its cost."""

    n_sims: int
    current_pick: int
    next_pick: int | None
    following_pick: int | None
    results: tuple[SimResult, ...]
    elapsed_seconds: float

    def survival_rate(self, player_id: str, *, at: str = "following") -> float:
        """Fraction of trials where ``player_id`` was still available at ``next``/``following``."""
        if not self.results:
            return 0.0
        if at == "next":
            hits = sum(1 for r in self.results if player_id in r.survived_at_next)
        elif at == "following":
            hits = sum(
                1
                for r in self.results
                if r.survived_at_following is not None and player_id in r.survived_at_following
            )
        else:
            raise ValueError(f"at must be 'next' or 'following', got {at!r}")
        return hits / len(self.results)

    def best_value_distribution(
        self, dv_of: Mapping[str, float], *, at: str = "following", exclude: str | None = None
    ) -> np.ndarray:
        """Per-trial max draft value among survivors -- the input to E[value]/SD[value]."""
        out = np.zeros(len(self.results), dtype=float)
        for i, r in enumerate(self.results):
            pool = r.survived_at_following if at == "following" else r.survived_at_next
            if pool is None:
                out[i] = 0.0
                continue
            vals = [dv_of[pid] for pid in pool if pid != exclude and pid in dv_of]
            out[i] = max(vals) if vals else 0.0
        return out


@dataclass
class _RunPlayer:
    """The minimal shape `PositionalRun` needs: `.pos` and `.adp` (see survival._pos_of/_mu_sd)."""

    pos: str
    adp: float


#: `PositionalRun.share()` (survival.py, not modified here) only ever looks at the top
#: `top_n=30` remaining players by ADP. Handing it a top-40 slice instead of the full ~200-player
#: pool gives an IDENTICAL result (40 > 30, so the true top-30 is always inside it) for a
#: fraction of the `_mu_sd` calls its internal sort would otherwise make.
_RUN_REMAINING_CAP = 40


def _top_by_adp(order_by_adp: Sequence[str], pool: Mapping[str, Any], cap: int) -> list[str]:
    """The first `cap` ids from the static ADP order that are still in `pool`."""
    out: list[str] = []
    for pid in order_by_adp:
        if pid in pool:
            out.append(pid)
            if len(out) >= cap:
                break
    return out


def _seed_have(state: DraftState, cfg: LeagueConfig, pos_of: Mapping[str, str]) -> dict[int, dict[str, int]]:
    return {t: dict(state.roster_positions(t, pos_of)) for t in range(1, state.teams + 1)}


def simulate_forward(
    state: DraftState,
    cfg: LeagueConfig,
    players: Sequence[Any],
    *,
    n_sims: int = 500,
    horizon_picks: int | None = None,
    my_pick_at_next: str | None = None,
    calibration: opp.LeagueCalibration | None = None,
    run_seed: PositionalRun | None = None,
    seed: int | None = None,
) -> SimulationSummary:
    """Roll the opponent model forward ``n_sims`` times from ``state.current_pick``.

    Args:
        state: the live draft state. Only read, never mutated.
        cfg: league rules -- every need/constraint computation comes from here.
        players: the full player pool (drafted and undrafted); ADP/dv/pos are read off
            whatever attributes :mod:`draftroom.draft.survival` / :mod:`draftroom.draft.vona`
            already know how to read (``.adp``/``.stdev``/``.pos``/``.dv``/``.player_id``).
        n_sims: number of independent trials.
        horizon_picks: optional cap on how many picks forward to simulate, counted from
            ``state.current_pick``. Defaults to whatever it takes to reach
            ``TurnContext.following_pick`` (Marc's next TWO turns, inclusive of "now").
        my_pick_at_next: if given, Marc's own pick at ``TurnContext.next_pick`` is fixed to
            this player_id rather than contributing to the opponent sampling (used by
            ``recommend.py`` to ask "what happens if I take X now"). Only meaningful when
            ``next_pick == state.current_pick``, i.e. Marc is on the clock right now.
        calibration: opponent calibration; defaults to
            :meth:`~draftroom.draft.opponents.LeagueCalibration.national_only`.
        run_seed: a :class:`PositionalRun` already populated with the real picks made so far
            (so trial 1's herd dynamics start from the actual board history, not a blank
            slate). Each trial gets its own deep copy. Omit for a fresh detector per trial.
        seed: RNG seed, for reproducible tests.

    Returns:
        A :class:`SimulationSummary` with one :class:`SimResult` per trial and the wall-clock
        time actually spent, so callers can report real numbers rather than assumed ones.
    """
    calibration = calibration or opp.LeagueCalibration.national_only()
    rng = np.random.default_rng(seed)

    ctx = state.turn_context()
    if ctx.next_pick is None:
        raise ValueError(f"team slot {state.my_slot} has no remaining picks; nothing to simulate")
    next_pick = ctx.next_pick
    following_pick = ctx.following_pick

    natural_stop = following_pick if following_pick is not None else next_pick
    stop_pick = natural_stop
    if horizon_picks is not None:
        capped = state.current_pick + int(horizon_picks) - 1
        stop_pick = min(stop_pick, capped) if stop_pick is not None else capped

    # Resolve each player's (adp, stdev, pos) exactly ONCE for the whole call, not once per pick
    # per trial. `draftroom.draft.survival._mu_sd`/`_pos_of` are correct but duck-type their
    # argument via `isinstance` checks against `typing` generics on every call -- fine once per
    # player, measurably slow if repeated for every available player on every simulated pick
    # across hundreds of trials. See the comment on `opponents.opponent_scores(resolved=...)`.
    pos_of: dict[str, str] = {}
    dv_of: dict[str, float] = {}
    adp_of: dict[str, float] = {}
    resolved: dict[str, tuple[float, float | None, str]] = {}
    for p in players:
        pid = str(getattr(p, "player_id"))
        mu, sd = _mu_sd(p)
        pos = _pos_of(p)
        pos_of[pid] = pos
        adp_of[pid] = mu
        resolved[pid] = (mu, sd, pos)
        dv_of[pid] = getattr(p, "dv", None) if getattr(p, "dv", None) is not None else getattr(
            p, "draft_value", 0.0
        )
    # Static ADP order never changes trial to trial -- used to hand `PositionalRun.observe` a
    # cheap, already-mostly-sorted TOP-N-by-ADP slice instead of the full remaining pool (its
    # internal `share()` only ever looks at the top `top_n` anyway; see `_RUN_REMAINING_CAP`).
    order_by_adp = sorted(pos_of, key=lambda pid: adp_of[pid])

    drafted_now = state.drafted_player_ids
    base_pool = [p for p in players if str(getattr(p, "player_id")) not in drafted_now]
    base_have = _seed_have(state, cfg, pos_of)

    results: list[SimResult] = []
    t0 = time.perf_counter()

    for _ in range(n_sims):
        pool: dict[str, Any] = {str(getattr(p, "player_id")): p for p in base_pool}
        have = {t: dict(counts) for t, counts in base_have.items()}
        run = copy.deepcopy(run_seed) if run_seed is not None else PositionalRun()

        survived_at_next: frozenset[str] | None = None
        survived_at_following: frozenset[str] | None = None

        pick = state.current_pick
        while pick <= stop_pick:
            if pick == next_pick:
                survived_at_next = frozenset(pool.keys())
                if my_pick_at_next is not None:
                    taken = pool.pop(my_pick_at_next, None)
                    if taken is not None:
                        slot = state.my_slot
                        pos = pos_of.get(my_pick_at_next, _pos_of(taken))
                        have.setdefault(slot, {})[pos] = have.get(slot, {}).get(pos, 0) + 1
                    pick += 1
                    continue
            if pick == following_pick:
                survived_at_following = frozenset(pool.keys())
                break

            slot = snake.slot_on_clock(cfg.teams, pick)
            if slot == state.my_slot:
                # Marc's own pick within the horizon that isn't `next_pick` (shouldn't occur
                # given TurnContext's definition of next/following as his own consecutive
                # turns) -- nothing to model, so the pick is a no-op and the board doesn't move.
                pick += 1
                continue

            available = list(pool.values())
            if not available:
                break
            slot_have = have.setdefault(slot, {})
            probs = opp.opponent_pick_probabilities(
                available,
                team_slot=slot,
                pick_no=pick,
                have=slot_have,
                cfg=cfg,
                calibration=calibration,
                run=run,
                resolved=resolved,
            )
            if not probs:
                # Hard-constrained to a position with nobody left at it -- vanishingly rare,
                # but the pick still has to happen. Fall back to unconstrained ADP-only choice
                # among everyone available rather than deadlocking the simulation.
                probs = opp.opponent_pick_probabilities(
                    available,
                    team_slot=slot,
                    pick_no=pick,
                    have={},
                    cfg=cfg,
                    calibration=calibration,
                    run=run,
                    resolved=resolved,
                )
            pids = list(probs.keys())
            p_arr = np.array([probs[pid] for pid in pids], dtype=float)
            p_arr = p_arr / p_arr.sum()
            chosen = pids[int(rng.choice(len(pids), p=p_arr))]

            pool.pop(chosen)
            pos = pos_of[chosen]
            slot_have[pos] = slot_have.get(pos, 0) + 1

            # Feed PositionalRun.observe only the top-N-by-ADP still on the board (its own
            # `share()` only ever consults the top `top_n=30` after sorting), not the full
            # remaining pool -- same result, far fewer `_mu_sd` calls in this hot loop.
            top_ids = _top_by_adp(order_by_adp, pool, _RUN_REMAINING_CAP)
            remaining_for_run = [_RunPlayer(pos=pos_of[pid], adp=adp_of[pid]) for pid in top_ids]
            run.observe(pos, remaining=remaining_for_run)

            pick += 1

        if survived_at_next is None:
            survived_at_next = frozenset(pool.keys())
        results.append(
            SimResult(survived_at_next=survived_at_next, survived_at_following=survived_at_following)
        )

    elapsed = time.perf_counter() - t0
    return SimulationSummary(
        n_sims=n_sims,
        current_pick=state.current_pick,
        next_pick=next_pick,
        following_pick=following_pick,
        results=tuple(results),
        elapsed_seconds=elapsed,
    )
