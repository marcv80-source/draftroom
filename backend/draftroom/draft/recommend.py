"""The recommendation engine -- turns a board + draft state into a ranked, explained pick.

This is where every other live-draft module gets consumed at once: survival
(:mod:`draftroom.draft.survival`) for "is he still there", VONA (:mod:`draftroom.draft.vona`)
for "what does waiting on this position cost", tiers (:mod:`draftroom.tiers.dynamic`) for
"is this a cliff", the opponent model (:mod:`draftroom.draft.opponents`) and its Monte Carlo
roll-forward (:mod:`draftroom.draft.simulate`) for "what will actually be left", and the
explain layer (:mod:`draftroom.explain`) to turn all of that into bullets Marc can read in
four seconds.

**Draft values are synthetic wherever this module is exercised without real projections**
(tests) -- see ``tests/test_recommend.py``
for exactly how, and every synthetic figure is labeled as such at the point it's produced. This
module itself does not care where ``BoardPlayer.dv`` came from; it only consumes it.

Two design choices worth stating up front:

1. **Guardrails exclude, they do not penalize.** A candidate that would leave a starter slot
   unfillable, or bust a roster cap, never reaches the ranked list -- it is removed from the
   candidate pool before utility is even computed. A "penalty" large enough to always sink such
   a candidate is just an exclusion with extra steps and a bug waiting for the day the penalty
   isn't large enough.
2. **One shared Monte Carlo roll-forward, not one per candidate.** Every candidate's expected
   continuation value is read off the SAME set of simulated trials
   (:func:`draftroom.draft.simulate.simulate_forward`, called once), filtering out whichever
   player the candidate would have taken. The alternative -- resimulating from scratch for each
   of a dozen-plus candidates -- would multiply the Monte Carlo cost for a correction (how much
   one specific player's absence changes ~11 opponents' aggregate behavior across ~200 other
   players) that is second-order. This is a documented approximation, not exact conditioning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from draftroom.config import LeagueConfig
from draftroom.draft import opponents as opp
from draftroom.draft import scarcity
from draftroom.draft import snake
from draftroom.draft.simulate import SimulationSummary, _RunPlayer, simulate_forward
from draftroom.draft.state import DraftState
from draftroom.draft.survival import (
    PositionalRun,
    SdFit,
    _mu_sd,
    _pos_of,
    fit_sd_model,
    p_available,
    tier_exhaustion_pick,
)
from draftroom.draft.vona import VonaResult, vona_all_positions
from draftroom.explain import primitives as prim
from draftroom.explain.render import render
from draftroom.tiers.dynamic import MIN_N_FOR_GMM, TierEngine, TierInfo, largest_gap_tiers

__all__ = [
    "BoardPlayer",
    "recommend",
    "DEFAULT_LAM",
    "SHUTOUT_PROB_THRESHOLD",
    "ELITE_QB_RANK_CUTOFF",
]

#: Default risk-aversion coefficient in `U = E[value] - lam*SD[value]` (spec default).
DEFAULT_LAM = 0.25

#: Guardrail 2 threshold: fire the CRITICAL shut-out warning once P(startable < demand) exceeds
#: this, over the shared Monte Carlo trials. Spec value: 0.30.
SHUTOUT_PROB_THRESHOLD = 0.30

#: Fix "C" (b), the opportunistic elite grab: with zero QBs rostered, a QB ranked at or above
#: this AMONG QBS on the full preseason board (by dv) is ranked first when available -- and a
#: QB below it is never reached for by this rule. This is the "visible knob" from the approved
#: spec (default top-3); `recommend(elite_qb_rank_cutoff=...)` overrides it per call, and 0
#: disables the rule outright. Validated in the 2025 backtest at +16-18 points (t=3.7-4.1)
#: (the strategy tournament (retired 2026-08-25), `qb_one_elite_one_cheap`).
ELITE_QB_RANK_CUTOFF = 3

#: At-the-turn pair optimizer: only pair-partners with at least this much survival odds are
#: considered (spec value). Below it, the pair math would be dominated by a coin flip rather
#: than a real plan.
PAIR_SURVIVAL_FLOOR = 0.85


@dataclass(frozen=True)
class BoardPlayer:
    """One player as the recommendation engine needs to see them: ADP + a draft value.

    ``dv`` (and ``dv_sd``) are expected to already be risk-adjusted draft values -- typically
    :class:`draftroom.valuation.evob.DraftValue.dv` -- or, until real projections are wired up,
    a clearly-labeled SYNTHETIC stand-in derived from ADP (see ``tests/test_recommend.py``).
    This dataclass is intentionally duck-type-compatible with what
    :mod:`draftroom.draft.survival`, :mod:`draftroom.draft.vona`, and
    :mod:`draftroom.tiers.dynamic` already know how to read (``.adp``/``.stdev``, ``.pos``,
    ``.dv``, ``.player_id``) -- nothing here is a new protocol, it is the union of the fields
    those modules already expect on "anything player-ish".
    """

    player_id: str
    name: str
    pos: str
    team: str
    bye: int | None
    adp: float
    stdev: float | None
    dv: float
    dv_sd: float = 0.0
    #: False for roster-only players carried for BOOKKEEPING (write-in targets with no
    #: projection). They stay in the pool so drafted write-ins keep their position in the
    #: roster/need math, but they must never enter candidate generation, tiering, VONA, or
    #: startable-supply counts -- a `dv` of 0.0 on them is "no projection", not an evaluation.
    is_ranked: bool = True

    @property
    def floor(self) -> float:
        return self.dv - self.dv_sd

    @property
    def ceiling(self) -> float:
        return self.dv + self.dv_sd


# --------------------------------------------------------------------------- guardrails


def _feasible_after_pick(
    state: DraftState, cfg: LeagueConfig, pos_of: Mapping[str, str], taken_pos: str
) -> bool:
    """Guardrail 1: after hypothetically taking one more player at `taken_pos`, can Marc still
    fill every mandatory starter (+flex) slot with the picks he'd have left? EXCLUDES, never
    just down-ranks, a candidate that fails this."""
    have = dict(state.roster_positions(state.my_slot, pos_of))
    have[taken_pos] = have.get(taken_pos, 0) + 1
    unfilled = opp.unfilled_starters_from_counts(have, cfg)
    flex_deficit = opp.flex_deficit_from_counts(have, cfg)
    total_needed = sum(unfilled.values()) + flex_deficit
    remaining_after = state.picks_remaining_for(state.my_slot) - 1
    return remaining_after >= total_needed


#: UNVERIFIED TUNING CONSTANT: LeagueConfig has no explicit "max at position" field, so this
#: encodes the spec's own example ("never a 4th QB in a 2-QB league") as a rule: one bench
#: backup beyond the required starters at a non-flex position (2 + 1 = 3, so a 4th is capped
#: out); flex-eligible positions get a little more room to stash (flex_slots plus two bench
#: spots) because a RB/WR/TE surplus is a normal, useful hedge in a way a 3rd/4th QB isn't.
_BENCH_BACKUP_NON_FLEX = 1
_BENCH_STASH_FLEX = 2


def _max_roster_at_position(cfg: LeagueConfig, pos: str) -> int:
    starters_here = cfg.starters.get(pos, 0)
    if pos in cfg.flex_eligible:
        return starters_here + cfg.flex_slots + _BENCH_STASH_FLEX
    return starters_here + _BENCH_BACKUP_NON_FLEX


def _roster_cap_ok(
    state: DraftState, cfg: LeagueConfig, pos_of: Mapping[str, str], pos: str
) -> bool:
    """Guardrail 3: roster caps from config. EXCLUDES a candidate that would bust the cap."""
    have = state.roster_positions(state.my_slot, pos_of).get(pos, 0)
    return have < _max_roster_at_position(cfg, pos)


# --------------------------------------------------------------------------- run history


def _build_run_detector(
    state: DraftState, by_id: Mapping[str, Any], pos_of: Mapping[str, str]
) -> PositionalRun:
    """Replay the picks already made so the run detector's history matches the real board,
    rather than starting a live recommendation from a blank slate."""
    run = PositionalRun()
    ordered = sorted((p for p in state.picks.values() if p.is_filled), key=lambda p: p.pick_no)
    pool = dict(by_id)
    for pk in ordered:
        pos = pk.stub_pos if pk.player_id is None else pos_of.get(pk.player_id)
        if pk.player_id is not None:
            pool.pop(pk.player_id, None)
        if not pos:
            continue
        remaining = [_RunPlayer(pos=pos_of.get(pid, ""), adp=_mu_sd(obj)[0]) for pid, obj in pool.items()]
        run.observe(pos, remaining=remaining)
    return run


# --------------------------------------------------------------------------- explanation primitives


def _fit_tiers_for_pos(pos_pool: Sequence[BoardPlayer]) -> list[TierInfo]:
    """One GMM (or largest-gap) fit per position, NOT per candidate.

    A GMM fit is the single most expensive thing this module does (sklearn's k-means init
    alone runs ~10-20ms per k searched). ``recommend()`` builds up to
    ``n_candidates_per_pos`` candidates at EVERY position, and every one of them needs to know
    its own tier -- calling this once per candidate would refit the exact same position's pool
    redundantly (4 QB candidates -> the same QB pool fit 4 times). Call it once per position
    and hand every candidate at that position the same tier list.
    """
    if len(pos_pool) >= MIN_N_FOR_GMM:
        return TierEngine().update("_", pos_pool)  # fresh engine: no cross-pick hysteresis here
    return largest_gap_tiers(pos_pool)


def _tier_cliff_from(
    tiers: Sequence[TierInfo],
    player_id: str,
    current_pick: int,
    teams: int,
    fit: SdFit | None,
    run: PositionalRun | None,
) -> prim.TierCliff:
    for i, t in enumerate(tiers):
        ids = {getattr(m, "player_id") for m in t.members}
        if player_id in ids:
            exhaustion = tier_exhaustion_pick(t.members, current_pick, fit=fit, run=run)
            label = snake.pick_label(teams, exhaustion) if exhaustion is not None else None
            return prim.TierCliff(
                tier_index=i,
                tier_size_remaining=t.size,
                points_to_next_tier=(t.cliff if t.cliff is not None else 0.0),
                exhaustion_pick=exhaustion,
                exhaustion_label=label,
            )
    # Defensive fallback -- every candidate is built from `pos_pool`, so this should not happen.
    return prim.TierCliff(tier_index=0, tier_size_remaining=1, points_to_next_tier=0.0, exhaustion_pick=None)


def _position_depth(
    pos: str,
    pos_pool: Sequence[BoardPlayer],
    cfg: LeagueConfig,
    state: DraftState,
    pos_of: Mapping[str, str],
    current_pick: int,
    fit: SdFit | None,
    run: PositionalRun | None,
) -> prim.PositionDepth:
    startable_remaining = sum(1 for p in pos_pool if p.dv > 0)
    league_demand = sum(
        state.unfilled_starters(t, cfg.starters, pos_of).get(pos, 0) for t in range(1, state.teams + 1)
    )
    cushion: float | None = None
    if pos_pool:
        exhaustion = tier_exhaustion_pick(
            pos_pool, current_pick, threshold=max(1.0, float(league_demand)), fit=fit, run=run
        )
        cushion = float(exhaustion - current_pick) if exhaustion is not None else float(len(pos_pool))
    return prim.PositionDepth(
        position=pos,
        startable_remaining=startable_remaining,
        league_demand_remaining=league_demand,
        picks_of_cushion=cushion,
    )


def _opponent_pressure(
    pos: str,
    ctx,
    state: DraftState,
    cfg: LeagueConfig,
    pos_of: Mapping[str, str],
    calibration: opp.LeagueCalibration,
    run: PositionalRun | None,
) -> prim.OpponentPressure | None:
    picks_between = ctx.picks_between_turns
    if picks_between is None:
        return None

    # Count DISTINCT teams, not picks. Two different things: in a snake, the team at the turn picks
    # twice in a row, so iterating picks double-counts that manager. It also produced nonsense like
    # "16 of the 16 teams" in a 12-team league, which is the kind of visibly-wrong line that makes
    # you stop believing the rest of the panel.
    slots_before: list[int] = []
    for pick in range(state.current_pick + 1, state.current_pick + 1 + picks_between):
        slot = snake.slot_on_clock(state.teams, pick)
        if slot not in slots_before:
            slots_before.append(slot)

    teams_before = len(slots_before)
    needing = sum(
        1
        for slot in slots_before
        if pos in state.unfilled_starters(slot, cfg.starters, pos_of)
    )
    offset = calibration.position_timing_offset.get(pos) or None
    run_detected = bool(run is not None and run.shift(pos) > 0.0)
    return prim.OpponentPressure(
        position=pos,
        teams_before_next_turn=teams_before,
        teams_needing_position=needing,
        league_timing_offset=offset,
        run_detected=run_detected,
    )


def _fallbacks(
    pos: str,
    pos_pool: Sequence[BoardPlayer],
    exclude_id: str,
    exclude_dv: float,
    current_pick: int,
    next_pick: int | None,
    fit: SdFit | None,
    run: PositionalRun | None,
    *,
    n: int = 2,
) -> tuple[prim.Fallback, ...]:
    others = sorted((p for p in pos_pool if p.player_id != exclude_id), key=lambda p: -p.dv)[:n]
    out: list[prim.Fallback] = []
    for alt in others:
        if next_pick is None:
            p_surv = 1.0
        else:
            mu, sd = _mu_sd(alt)
            if run is not None:
                mu = run.adjusted_mu(mu, alt.pos)
            p_surv = p_available(mu, sd, next_pick, current_pick, fit=fit)
        out.append(
            prim.Fallback(
                player_id=alt.player_id,
                name=alt.name,
                pos=pos,
                points_behind=exclude_dv - alt.dv,
                p_survive_next=p_surv,
            )
        )
    return tuple(out)


def _survival_info(
    X: BoardPlayer,
    ctx,
    state: DraftState,
    cfg: LeagueConfig,
    fit: SdFit | None,
    run: PositionalRun | None,
) -> prim.SurvivalInfo:
    teams = cfg.teams
    if ctx.following_pick is None:
        # No further turn to worry about -- he's being drafted right now.
        return prim.SurvivalInfo(
            next_pick=state.current_pick,
            next_pick_label=snake.pick_label(teams, state.current_pick),
            p_survive_next=1.0,
        )
    mu, sd = _mu_sd(X)
    if run is not None:
        mu = run.adjusted_mu(mu, X.pos)
    # Conditioned from current_pick + 1: the current pick is Marc's own, so no opponent can
    # consume a player "at" it (Codex 2026-08-18 off-by-one; exactly 1.0 at a back-to-back turn).
    p_next = p_available(mu, sd, ctx.following_pick, state.current_pick + 1, fit=fit)

    third_pick = snake.next_pick_for(teams, state.my_slot, state.rounds, ctx.following_pick)
    p_following = None
    if third_pick is not None:
        p_following = p_available(mu, sd, third_pick, state.current_pick + 1, fit=fit)

    return prim.SurvivalInfo(
        next_pick=ctx.following_pick,
        next_pick_label=snake.pick_label(teams, ctx.following_pick),
        p_survive_next=p_next,
        following_pick=third_pick,
        following_pick_label=(snake.pick_label(teams, third_pick) if third_pick is not None else None),
        p_survive_following=p_following,
    )


def _bye_flags(X: BoardPlayer, state: DraftState, by_id: Mapping[str, BoardPlayer]) -> tuple[str, ...]:
    if X.bye is None:
        return ()
    my_ids = {pk.player_id for pk in state.roster(state.my_slot) if pk.player_id is not None}
    my_byes = {by_id[pid].bye for pid in my_ids if pid in by_id and by_id[pid].bye is not None}
    return ("BYE_COLLISION",) if X.bye in my_byes else ()


# --------------------------------------------------------------------------- main entry point


def recommend(
    state: DraftState,
    cfg: LeagueConfig,
    players: Sequence[BoardPlayer],
    *,
    lam: float = DEFAULT_LAM,
    calibration: opp.LeagueCalibration | None = None,
    n_sims: int = 500,
    n_candidates_per_pos: int = 4,
    seed: int | None = None,
    elite_qb_rank_cutoff: int = ELITE_QB_RANK_CUTOFF,
) -> prim.Recommendation:
    """Build the full, explained recommendation for whoever is on the clock right now.

    Fix "C" (approved 2026-08-17, built 2026-08-18) lives here, in three parts that MOVE THE
    RANKING rather than merely append warnings (the old warn-only guardrail was the proven
    mechanism behind losing to plain-ADP bots from all 10 slots):

    (a) **Reactive scarcity floor.** For any dedicated (non-flex) position where Marc still
        has an unfilled starter slot, when `startable supply remaining - leaguewide unfilled
        slots <= opponent picks before his next turn`, candidates at that position are ranked
        FIRST. Deterministic counting, not a Monte Carlo probability -- the same trigger the
        2025 backtest priced at +28.2 points (t=6.6) as `qb_never_below_line`.
    (b) **Opportunistic elite grab.** With zero QBs rostered, a top-`elite_qb_rank_cutoff` QB
        (by dv, ranked among QBs on the FULL board) still available is ranked first; a QB
        below the cutoff is never reached for by this rule. Backtest: +16-18 (t=3.7-4.1).
    (c) **Opportunity cost in the ranking.** The off-turn utility adds the position's VONA
        (``vona.py``, previously computed and displayed but decision-inert): the Monte Carlo
        continuation term is position-agnostic (the best player left at the next turn barely
        depends on which candidate was taken), so VONA is exactly the missing positional-decay
        term. Fallbacks stay on every candidate, so the tone contract holds: the floor and
        the grab re-rank, the human still sees what waiting would have kept.

    Args:
        state: live draft state (read-only here).
        cfg: league rules.
        players: the whole player pool, drafted and undrafted, as :class:`BoardPlayer`.
        lam: risk-aversion coefficient, ``U = E[value] - lam*SD[value]``. Only used off the
            turn; the at-the-turn branch optimizes an exact pair value instead (see module
            docstring and CLAUDE.md: "our recommendations never herd", and separately, never
            substitute a variance penalty for the exact joint optimum when one is computable).
        calibration: opponent calibration; defaults to
            :meth:`~draftroom.draft.opponents.LeagueCalibration.national_only`.
        n_sims: Monte Carlo trials for the shared roll-forward (see module docstring: one
            shared simulation, not one per candidate).
        n_candidates_per_pos: how many top-by-draft-value players per position enter the
            candidate set before guardrails are applied.
        seed: RNG seed for the Monte Carlo roll-forward, for reproducible tests/demos.
        elite_qb_rank_cutoff: the fix-"C"(b) knob -- see :data:`ELITE_QB_RANK_CUTOFF`. 0
            disables the elite grab.
    """
    calibration = calibration or opp.LeagueCalibration.national_only()
    # Identity/roster maps cover EVERYONE (a drafted unranked write-in must still count toward
    # its team's positional needs); everything valuation-shaped runs on ranked players only --
    # an unranked player's dv of 0.0 is "no projection", never an evaluation (CLAUDE.md).
    by_id: dict[str, BoardPlayer] = {p.player_id: p for p in players}
    pos_of: dict[str, str] = {p.player_id: p.pos for p in players}
    dv_of: dict[str, float] = {p.player_id: p.dv for p in players}
    ranked_players = [p for p in players if p.is_ranked]
    ranked_by_id: dict[str, BoardPlayer] = {p.player_id: p for p in ranked_players}

    pick_label = snake.pick_label(cfg.teams, state.current_pick)
    if not state.is_my_pick:
        return prim.Recommendation(
            pick_no=state.current_pick,
            pick_label=pick_label,
            on_the_clock=state.slot_on_clock,
            is_my_pick=False,
            candidates=(),
            warnings=("Not on the clock -- no recommendation generated.",),
        )

    drafted = state.drafted_player_ids
    available = [p for p in ranked_players if p.player_id not in drafted]
    ctx = state.turn_context()

    try:
        fit: SdFit | None = fit_sd_model(ranked_players)
    except ValueError:
        fit = None

    run = _build_run_detector(state, ranked_by_id, pos_of)
    warnings: list[str] = []

    # ---- candidate generation: top-N by draft value, per position ----
    full_pos_pool: dict[str, list[BoardPlayer]] = {
        pos: [p for p in available if p.pos == pos] for pos in cfg.positions
    }
    candidates_by_pos: dict[str, list[BoardPlayer]] = {
        pos: sorted(pool, key=lambda p: -p.dv)[:n_candidates_per_pos]
        for pos, pool in full_pos_pool.items()
    }

    # ---- one shared Monte Carlo roll-forward (see module docstring) ----
    sims: SimulationSummary | None = None
    if ctx.next_pick is not None:
        sims = simulate_forward(
            state, cfg, ranked_players, n_sims=n_sims, calibration=calibration, run_seed=run, seed=seed
        )

    # ---- guardrail 2: positional shut-out risk, checked for every position with demand ----
    if sims is not None and sims.following_pick is not None and sims.results:
        for pos in sorted(cfg.positions):
            league_demand = sum(
                state.unfilled_starters(t, cfg.starters, pos_of).get(pos, 0)
                for t in range(1, state.teams + 1)
            )
            if league_demand <= 0:
                continue
            hits = 0
            for r in sims.results:
                if r.survived_at_following is None:
                    continue
                startable = sum(
                    1
                    for pid in r.survived_at_following
                    if pos_of.get(pid) == pos and dv_of.get(pid, 0.0) > 0
                )
                if startable < league_demand:
                    hits += 1
            frac = hits / len(sims.results)
            if frac > SHUTOUT_PROB_THRESHOLD:
                warnings.append(
                    f"CRITICAL: {frac:.0%} chance {pos} startable supply drops below the "
                    f"{league_demand} unfilled league slot(s) before your next turn. "
                    f"Forcing {pos} into the candidate set."
                )
                if not candidates_by_pos.get(pos):
                    candidates_by_pos[pos] = sorted(full_pos_pool.get(pos, []), key=lambda p: -p.dv)[
                        :n_candidates_per_pos
                    ]

    # ---- fix "C" (a): reactive scarcity floor (deterministic, moves the ranking) ----
    # SHARED implementation with the strategy tournament (retired 2026-08-25) via draftroom.draft.scarcity --
    # "startable" means the same man-games rank cutoff the tournament validated, and the trigger
    # bounds opponent consumption by their own need rather than assuming every intervening pick
    # eats supply (Codex 2026-08-18: the old form fired with 21 startable vs 20 open slots).
    my_have = state.roster_positions(state.my_slot, pos_of)
    my_unfilled = opp.unfilled_starters_from_counts(my_have, cfg)
    gap_pick_slots = (
        [
            snake.slot_on_clock(cfg.teams, pk)
            for pk in range(state.current_pick + 1, ctx.following_pick)
        ]
        if ctx.following_pick is not None
        else []
    )
    floor_positions: set[str] = set()
    for pos in sorted(my_unfilled):
        if pos in cfg.flex_eligible:
            # Flex-eligible supply is fungible across RB/WR/TE, so the per-position count
            # below would misfire there; the floor is validated (and needed) for dedicated
            # positions -- in this league, QB.
            continue
        pos_pool_all = full_pos_pool.get(pos, [])
        cutoff = scarcity.startable_rank_cutoff(cfg, pos)
        pos_rank_full = {
            p.player_id: i + 1
            for i, p in enumerate(
                sorted((q for q in ranked_players if q.pos == pos), key=lambda p: -p.dv)
            )
        }
        startable_remaining = sum(
            1 for p in pos_pool_all if pos_rank_full.get(p.player_id, 10**9) <= cutoff
        )
        unfilled_by_slot = {
            t: state.unfilled_starters(t, cfg.starters, pos_of).get(pos, 0)
            for t in range(1, state.teams + 1)
            if t != state.my_slot
        }
        consumption = scarcity.opponent_consumption_bound(gap_pick_slots, unfilled_by_slot)
        if scarcity.scarcity_trigger_fires(
            startable_remaining=startable_remaining,
            opponent_consumption_bound=consumption,
            my_unfilled=my_unfilled[pos],
        ):
            floor_positions.add(pos)
            warnings.append(
                f"SCARCITY FLOOR: {startable_remaining} startable {pos}s left (top-{cutoff} by "
                f"man-games demand), opponents can need-consume up to {consumption} before your "
                f"next turn, and you still need {my_unfilled[pos]} -- {pos} ranked first. "
                f"Fallback shown below."
            )
            if not candidates_by_pos.get(pos):
                candidates_by_pos[pos] = sorted(pos_pool_all, key=lambda p: -p.dv)[
                    :n_candidates_per_pos
                ]

    # ---- fix "C" (b): opportunistic elite QB grab (never reaches below the cutoff) ----
    elite_ids: set[str] = set()
    if (
        elite_qb_rank_cutoff > 0
        and cfg.starters.get("QB", 0) > 0
        and my_have.get("QB", 0) == 0
    ):
        qb_rank_full = {
            p.player_id: i + 1
            for i, p in enumerate(
                sorted((q for q in ranked_players if q.pos == "QB"), key=lambda p: -p.dv)
            )
        }
        elite_available = [
            p
            for p in full_pos_pool.get("QB", [])
            if qb_rank_full.get(p.player_id, 10**9) <= elite_qb_rank_cutoff
        ]
        if elite_available:
            elite_ids = {p.player_id for p in elite_available}
            best_elite = max(elite_available, key=lambda p: p.dv)
            warnings.append(
                f"ELITE QB AVAILABLE: {best_elite.name} is a top-{elite_qb_rank_cutoff} board "
                f"QB and you have 0 of {cfg.starters['QB']} -- ranked first. Fallback shown below."
            )
            already = {p.player_id for p in candidates_by_pos.get("QB", [])}
            for p in elite_available:
                if p.player_id not in already:
                    candidates_by_pos.setdefault("QB", []).append(p)

    flat = [p for pool in candidates_by_pos.values() for p in pool]

    # ---- guardrails 1 & 3: exclude infeasible / cap-busting candidates outright ----
    feasible = [
        p
        for p in flat
        if _feasible_after_pick(state, cfg, pos_of, p.pos) and _roster_cap_ok(state, cfg, pos_of, p.pos)
    ]

    vona_map: Mapping[str, VonaResult] = {}
    if ctx.following_pick is not None:
        # Condition survival from current_pick + 1, not current_pick: Marc himself consumes the
        # current pick, so opponents can only take players from pick current+1 onward. At a
        # back-to-back turn (following == current + 1) every other player survives with
        # probability exactly 1.0 (Codex 2026-08-18: the old form priced an impossible draft
        # opportunity, and VONA now moves rankings, so the off-by-one moved decisions).
        vona_map = vona_all_positions(
            available, state.current_pick + 1, ctx.following_pick, fit=fit, run=run
        )

    is_turn = bool(ctx.at_the_turn and ctx.following_pick is not None)
    utilities: dict[str, float] = {}
    pair_partner: dict[str, tuple[BoardPlayer | None, float]] = {}

    if is_turn:
        # Optimise the PAIR jointly: this pick, plus whichever survivor at the very next turn
        # maximises DV_X + E[DV_Y], restricted to Y with p_survive >= PAIR_SURVIVAL_FLOOR (spec).
        for X in feasible:
            best_y: BoardPlayer | None = None
            best_p = 0.0
            best_val = dv_of[X.player_id]
            for Y in available:
                if Y.player_id == X.player_id:
                    continue
                mu, sd = _mu_sd(Y)
                if run is not None:
                    mu = run.adjusted_mu(mu, Y.pos)
                # From current_pick + 1: taking X consumes the current pick, so Y cannot be
                # drafted "at" it -- at a true back-to-back turn this is exactly 1.0.
                p_surv = p_available(mu, sd, ctx.following_pick, state.current_pick + 1, fit=fit)
                if p_surv < PAIR_SURVIVAL_FLOOR:
                    continue
                val = dv_of[X.player_id] + Y.dv * p_surv
                if val > best_val:
                    best_val, best_y, best_p = val, Y, p_surv
            utilities[X.player_id] = best_val
            pair_partner[X.player_id] = (best_y, best_p)
        if utilities:
            top_id = max(utilities, key=lambda pid: utilities[pid])
            y, p = pair_partner.get(top_id, (None, 0.0))
            if y is not None:
                warnings.append(
                    f"AT THE TURN -- best pair: {by_id[top_id].name} now, then {y.name} "
                    f"({p:.0%} survives) at {snake.pick_label(cfg.teams, ctx.following_pick)}."
                )
    else:
        # Mid-round: rank by U = E[value] - lam*SD[value]. SD combines two independent sources
        # of spread, added in quadrature: X's OWN outcome uncertainty (`dv_sd` -- bust risk /
        # boom-bust range on this specific player, a high-ceiling boom-bust WR versus a
        # high-floor safe one) and the CONTINUATION uncertainty (what the board looks like at
        # Marc's next turn, from the shared Monte Carlo roll-forward). Both read off the same
        # shared simulation (see module docstring for why it's shared rather than per-candidate).
        for X in feasible:
            if sims is not None and sims.following_pick is not None:
                dist = sims.best_value_distribution(dv_of, at="following", exclude=X.player_id)
                e_val = dv_of[X.player_id] + float(np.mean(dist))
                continuation_sd = float(np.std(dist))
            else:
                e_val = dv_of[X.player_id]
                continuation_sd = 0.0
            sd_val = float(np.sqrt(X.dv_sd**2 + continuation_sd**2))
            # Fix "C" (c): the continuation term above is position-agnostic (the best player
            # on the board at the next turn is nearly the same set whichever candidate is
            # taken), so the positional cost of WAITING -- VONA -- is added explicitly.
            vona_term = vona_map[X.pos].vona if X.pos in vona_map else 0.0
            utilities[X.player_id] = e_val - lam * sd_val + vona_term

    # One tier fit per POSITION (not per candidate) -- see `_fit_tiers_for_pos`'s docstring.
    tiers_by_pos: dict[str, list[TierInfo]] = {
        pos: _fit_tiers_for_pos(pool) for pos, pool in full_pos_pool.items() if pool
    }

    candidates: list[prim.Candidate] = []
    for X in feasible:
        pos_pool = full_pos_pool[X.pos]
        tier = _tier_cliff_from(tiers_by_pos[X.pos], X.player_id, state.current_pick, cfg.teams, fit, run)
        survival_info = _survival_info(X, ctx, state, cfg, fit, run)
        depth = _position_depth(X.pos, pos_pool, cfg, state, pos_of, state.current_pick, fit, run)
        pressure = _opponent_pressure(X.pos, ctx, state, cfg, pos_of, calibration, run)
        vona_val = vona_map[X.pos].vona if X.pos in vona_map else 0.0

        alt_candidates = [c for c in flat if c.pos != X.pos]
        counterfactual = None
        if alt_candidates and vona_map:
            z = max(alt_candidates, key=lambda p: p.dv)
            if z.pos in vona_map and X.pos in vona_map:
                counterfactual = prim.Counterfactual(
                    position_given_up=z.pos,
                    points_given_up=vona_map[z.pos].vona,
                    position_gained=X.pos,
                    points_gained=vona_map[X.pos].vona,
                )

        fallbacks = _fallbacks(
            X.pos,
            pos_pool,
            X.player_id,
            X.dv,
            state.current_pick + 1,  # survival conditioned past Marc's own pick (see vona note)
            ctx.following_pick,
            fit,
            run,
        )
        flags = _bye_flags(X, state, by_id)

        candidates.append(
            prim.Candidate(
                player_id=X.player_id,
                name=X.name,
                pos=X.pos,
                team=X.team,
                bye=X.bye,
                draft_value=X.dv,
                projected_points=X.dv,  # SYNTHETIC proxy until real per-player projections land
                floor=X.floor,
                ceiling=X.ceiling,
                utility=utilities.get(X.player_id, X.dv),
                tier=tier,
                survival=survival_info,
                depth=depth,
                vona=vona_val,
                opponent_pressure=pressure,
                counterfactual=counterfactual,
                fallbacks=fallbacks,
                flags=flags,
            )
        )

    # Fix "C" ranking: the scarcity floor outranks everything (it is the catastrophe
    # avoider, +28.2 in the backtest), the elite grab outranks ordinary value (+16-18),
    # utility settles everything else -- including which player leads a forced position.
    def _priority(c: prim.Candidate) -> int:
        if c.pos in floor_positions:
            return 2
        if c.player_id in elite_ids:
            return 1
        return 0

    candidates.sort(key=lambda c: (-_priority(c), -c.utility))

    rec = prim.Recommendation(
        pick_no=state.current_pick,
        pick_label=pick_label,
        on_the_clock=state.slot_on_clock,
        is_my_pick=True,
        candidates=tuple(candidates),
        warnings=tuple(warnings),
        at_the_turn=is_turn,
        picks_until_next=ctx.picks_between_turns,
    )
    return render(rec)
