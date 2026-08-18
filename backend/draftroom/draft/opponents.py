"""Opponent pick model -- softmax over the available pool, calibrated per league.

Yahoo's OAuth-gated pick-by-pick API is still not available (CLAUDE.md), but real history for
THIS league now exists anyway: ``data/draft_2025.csv``, hand-transcribed from Yahoo's own
"Draft Results" page, 150 real picks with 6 transcription checks passing
(``tools/analyze_2025_draft.py``). :meth:`LeagueCalibration.from_draft_results` fits
:func:`fit_position_timing_offset` (and, optionally, :func:`fit_manager_reach`) from that
history against a cached national 2QB ADP payload -- see ``tools/calibrate_opponents.py``,
which is the one place that turns the CSV + ADP into :class:`PickObservation` rows and calls
this method.

**That tool's own out-of-sample validation (leave-one-manager-out, scored against the real
softmax this module runs) found the naive flat per-position offset does not reliably beat
plain national ADP** -- the room's actual QB pace is front-loaded (an early run, roughly what
2QB ADP itself already predicts), then falls BEHIND the scaled national pace for two rounds,
then scrambles to fill all 20 starting slots by pick 60. A single constant nets those opposing
effects to something that helps nowhere well. Per the project's own rule ("if it does not beat
plain ADP, we ship plain ADP and say so"), the shipped ``data/opponent_calibration_2025.json``
therefore carries **empty** offsets -- :meth:`LeagueCalibration.from_calibration_file` loading
it is deliberately equivalent to :meth:`national_only`. The tool still computes and records the
raw measured numbers (for the next person who wants to try a round-aware version, or has more
than one season to fit on) under a `measured_*` key, clearly separated from what is actually
applied. Swapping in a real, validated calibration later changes zero call sites in this
module -- only which ``LeagueCalibration`` gets constructed.

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

import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from draftroom.config import REPO_ROOT, LeagueConfig
from draftroom.draft.survival import PositionalRun, _mu_sd, _pos_of

__all__ = [
    "LeagueCalibration",
    "PickObservation",
    "ManagerReachFit",
    "scale_adp_to_league",
    "fit_position_timing_offset",
    "fit_manager_reach",
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
    "DEFAULT_CALIBRATION_PATH",
]

#: Where :meth:`LeagueCalibration.from_calibration_file` looks by default, and where
#: ``tools/calibrate_opponents.py`` writes its output. See that tool and the module docstring
#: for why this file's shipped ``position_timing_offset``/``manager_reach`` are empty.
DEFAULT_CALIBRATION_PATH = REPO_ROOT / "data" / "opponent_calibration_2025.json"

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

    Two GATED experiments (2026-08-18), both defaulting to a no-op and both shipped ON only
    if they beat plain ADP through the leave-one-manager-out gate (``tools/fit_room_prior.py``):

    ``qb_mu_curve``: a monotone piecewise-linear remap of a QB's scaled-national mean ADP onto
    this room's own observed QB pick timing (rank-matched from ``data/draft_2025.csv`` -- the
    room takes 7 QBs by pick 20 where the scaled feed expects 14, then avalanches picks 44-60).
    A curve, not a flat offset, because the flat offset already failed the gate: the room's
    pace error changes SIGN across the draft, which a constant nets to nothing.

    ``satiation_damper``: picks of utility subtracted from a position a manager has already
    filled every dedicated starter slot at (non-flex positions only). The observed room took
    1 luxury QB in 21 QB picks before pick 85, while the un-damped softmax still feels the
    full ``-mu`` pull toward elite QBs on QB-complete teams.
    """

    position_timing_offset: Mapping[str, float] = field(default_factory=dict)
    manager_reach: Mapping[int, float] = field(default_factory=dict)
    qb_mu_curve: tuple[tuple[float, float], ...] = ()
    satiation_damper: float = 0.0

    def remap_qb_mu(self, mu: float) -> float:
        """Apply ``qb_mu_curve`` (identity when empty). Linear between knots; beyond either
        end, the endpoint's additive offset carries on (the tail keeps its shape)."""
        curve = self.qb_mu_curve
        if not curve:
            return mu
        if mu <= curve[0][0]:
            return mu + (curve[0][1] - curve[0][0])
        if mu >= curve[-1][0]:
            return mu + (curve[-1][1] - curve[-1][0])
        for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
            if x0 <= mu <= x1:
                if x1 == x0:
                    return y0
                t = (mu - x0) / (x1 - x0)
                return y0 + t * (y1 - y0)
        return mu  # unreachable given the bounds checks

    @classmethod
    def national_only(cls) -> "LeagueCalibration":
        """Pure national-ADP calibration: zero timing offset, zero reach.

        This is what the model runs on TODAY -- Yahoo pick-by-pick history for this league
        does not exist yet (CLAUDE.md). Every ``mu_j_eff`` reduces to national ADP plus
        whatever a live positional run is doing.
        """
        return cls(position_timing_offset={}, manager_reach={})

    @classmethod
    def from_draft_results(
        cls,
        observations: Sequence["PickObservation"],
        *,
        include_manager_reach: bool = False,
    ) -> "LeagueCalibration":
        """Fit timing offsets (always) and, optionally, shrunk manager reach, from real picks.

        ``observations`` are :class:`PickObservation` rows -- this league's own actual picks,
        each carrying the player's national ADP already rescaled onto this league's own pick
        numbering (:func:`scale_adp_to_league`). ``tools/calibrate_opponents.py`` is the one
        place that builds those rows (from ``data/draft_2025.csv`` plus a cached FFC payload)
        and calls this method; see that tool for the out-of-sample validation that justifies
        (or, currently, does not justify -- see the module docstring) shipping the result.

        Args:
            include_manager_reach: default **False**. Draft SLOT is not a persistent manager
                identity -- Yahoo redraws it every season (``data/draft_2025.csv``'s own header:
                "the 2026 slot is not drawn yet") -- so a ``manager_reach`` fit keyed by last
                season's slot numbers would apply last year's specific people's tendencies to
                whoever a random draw seats in that chair this year. Leave-one-manager-out
                validation in the tool shows it does not even earn its keep in-season (15 picks
                per manager shrinks it to nearly nothing). Kept as an opt-in for completeness
                and for whenever real, persistent per-manager identity (Yahoo's ``team_key``)
                is available.
        """
        position_timing_offset = fit_position_timing_offset(observations)
        manager_reach: Mapping[int, float] = {}
        if include_manager_reach:
            manager_reach = fit_manager_reach(observations, position_timing_offset).shrunk
        return cls(position_timing_offset=position_timing_offset, manager_reach=manager_reach)

    @classmethod
    def from_calibration_file(cls, path: str | Path | None = None) -> "LeagueCalibration":
        """Load a previously-fit calibration from the params file :func:`to_json` writes.

        Defaults to :data:`DEFAULT_CALIBRATION_PATH` (``data/opponent_calibration_2025.json``),
        the file ``tools/calibrate_opponents.py`` produces. Only the top-level
        ``position_timing_offset``/``manager_reach`` keys are read -- a params file may also
        carry ``measured_*`` diagnostic blocks (the raw, unshipped numbers), which this
        deliberately ignores so "what's measured" and "what's applied" can never be confused.
        """
        target = Path(path) if path is not None else DEFAULT_CALIBRATION_PATH
        with open(target, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        offset = {str(k): float(v) for k, v in payload.get("position_timing_offset", {}).items()}
        reach = {int(k): float(v) for k, v in payload.get("manager_reach", {}).items()}
        curve = tuple(
            (float(x), float(y)) for x, y in payload.get("qb_mu_curve", [])
        )
        damper = float(payload.get("satiation_damper", 0.0))
        return cls(
            position_timing_offset=offset,
            manager_reach=reach,
            qb_mu_curve=curve,
            satiation_damper=damper,
        )

    def to_json(self, path: str | Path, *, extra: Mapping[str, Any] | None = None) -> None:
        """Persist ``{position_timing_offset, manager_reach}`` (plus any ``extra`` diagnostic
        keys, e.g. the measured-but-not-shipped numbers) as the params file
        :meth:`from_calibration_file` reads back.
        """
        payload: dict[str, Any] = dict(extra or {})
        payload["position_timing_offset"] = dict(self.position_timing_offset)
        payload["manager_reach"] = {str(k): v for k, v in self.manager_reach.items()}
        if self.qb_mu_curve:
            payload["qb_mu_curve"] = [[x, y] for x, y in self.qb_mu_curve]
        if self.satiation_damper:
            payload["satiation_damper"] = self.satiation_damper
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")


# --------------------------------------------------------------------------- calibration fitting


@dataclass(frozen=True)
class PickObservation:
    """One real draft pick, reduced to what calibration fitting needs.

    ``scaled_adp`` is the player's national ADP already rescaled onto THIS league's own pick
    numbering by :func:`scale_adp_to_league` -- never a raw ADP number from a feed measured on
    a different team count.
    """

    pick_no: int
    team_slot: int
    pos: str
    scaled_adp: float


def scale_adp_to_league(adp_national: float, *, teams_national: int, teams_league: int) -> float:
    """Rescale a national mean-ADP pick number onto this league's own pick numbering.

    FFC's 2QB feed exists only at ``teams_national=12`` (CLAUDE.md); this room has
    ``teams_league=10``. A snake draft's pick numbers are just "how many teams times how many
    rounds have gone by", so the auditable correction for "this many fewer teams turn a round
    over" is a straight linear rescale by the team-count ratio: pick 12 on a 12-team board and
    (this function's output) pick 10 on a 10-team board both sit at the same *proportional*
    point in round 1. This is a PROXY, not a description (CLAUDE.md) -- it assumes the
    national player-value ORDER survives the team-count change, which is the best available
    assumption without a native 10-team 2QB feed (FFC does not publish one). Never "fix" this
    by fetching ``teams=10`` -- the endpoint returns nothing there.
    """
    return float(adp_national) * (float(teams_league) / float(teams_national))


def fit_position_timing_offset(observations: Sequence[PickObservation]) -> dict[str, float]:
    """League-wide earliness/lateness by position, in picks. Positive = sooner than market.

    ``offset[pos] = mean(scaled_adp - pick_no)`` over every observation at that position: if a
    position is consistently taken several picks before its scaled ADP says it should go, the
    mean residual is positive, and subtracting a positive offset from ``mu`` (see
    ``_adjusted_mu``) is exactly what makes the model's own effective ADP sooner too.

    Positions with zero observations are simply absent from the result (nothing to average).
    """
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for obs in observations:
        sums[obs.pos] = sums.get(obs.pos, 0.0) + (obs.scaled_adp - obs.pick_no)
        counts[obs.pos] = counts.get(obs.pos, 0) + 1
    return {pos: sums[pos] / counts[pos] for pos in sums}


@dataclass(frozen=True)
class ManagerReachFit:
    """Per-manager reach, with the empirical-Bayes shrinkage that produced it kept visible.

    ``raw`` is each manager's own mean residual (scaled ADP minus pick, with the league's
    position offset already removed -- so ``raw`` is measuring reach INDEPENDENT of position,
    per the spec). ``shrunk`` pulls each ``raw`` value toward the grand mean (which is exactly
    0.0 by construction, since position offsets are themselves per-position means) by
    ``lambda = tau2 / (tau2 + sigma2/n)`` -- the classic one-way random-effects estimator:
    ``tau2`` is the (method-of-moments) variance BETWEEN managers, ``sigma2`` the pooled
    variance WITHIN a manager's own picks. With 15 picks per manager (CLAUDE.md: "15 picks per
    manager is thin"), ``sigma2`` is large relative to any real between-manager signal, so
    ``lambda`` comes out small and ``shrunk`` sits close to 0 for everyone -- which is the
    correct, honest behavior for a noisy per-manager estimate, not a bug.
    """

    raw: Mapping[int, float]
    shrunk: Mapping[int, float]
    lam: float
    between_manager_variance: float
    within_manager_variance: float
    n_per_manager: Mapping[int, int]


def fit_manager_reach(
    observations: Sequence[PickObservation],
    position_timing_offset: Mapping[str, float],
) -> ManagerReachFit:
    """Fit :class:`ManagerReachFit` from real picks, given an already-fit position offset.

    Residual per pick = ``scaled_adp - position_timing_offset[pos] - pick_no``; per-manager
    reach is the mean residual over that manager's own picks (independent of position, since
    the position offset has already been subtracted out).
    """
    by_slot: dict[int, list[float]] = {}
    for obs in observations:
        resid = obs.scaled_adp - position_timing_offset.get(obs.pos, 0.0) - obs.pick_no
        by_slot.setdefault(obs.team_slot, []).append(resid)

    n_per_manager = {slot: len(v) for slot, v in by_slot.items()}
    raw = {slot: statistics.mean(v) for slot, v in by_slot.items()}

    n_groups = len(by_slot)
    grand_n = sum(n_per_manager.values())
    if n_groups < 2 or grand_n <= n_groups:
        # Not enough structure to estimate a between-manager variance at all; shrink everyone
        # all the way to the grand mean rather than report a meaningless number.
        grand_mean = statistics.mean(v for vals in by_slot.values() for v in vals) if grand_n else 0.0
        shrunk = {slot: grand_mean for slot in by_slot}
        return ManagerReachFit(
            raw=raw, shrunk=shrunk, lam=0.0,
            between_manager_variance=0.0, within_manager_variance=0.0,
            n_per_manager=n_per_manager,
        )

    grand_mean = sum(sum(v) for v in by_slot.values()) / grand_n
    ss_within = sum(sum((x - raw[slot]) ** 2 for x in v) for slot, v in by_slot.items())
    df_within = grand_n - n_groups
    within_var = ss_within / df_within if df_within > 0 else 0.0
    ss_between = sum(n_per_manager[slot] * (raw[slot] - grand_mean) ** 2 for slot in by_slot)
    df_between = n_groups - 1
    between_ms = ss_between / df_between if df_between > 0 else 0.0
    avg_n = grand_n / n_groups
    # Method-of-moments between-group variance: subtract off the within-group noise that
    # inflates the raw between-group mean square, floored at 0 (a negative estimate just means
    # "no detectable between-manager signal at all").
    between_var = max(0.0, (between_ms - within_var) / avg_n)
    lam = between_var / (between_var + within_var / avg_n) if (between_var + within_var) > 0 else 0.0
    shrunk = {slot: lam * raw[slot] for slot in raw}
    return ManagerReachFit(
        raw=raw, shrunk=shrunk, lam=lam,
        between_manager_variance=between_var, within_manager_variance=within_var,
        n_per_manager=n_per_manager,
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
    if pos == "QB":
        mu = calibration.remap_qb_mu(mu)  # identity unless a gated qb_mu_curve shipped
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
        # Gated satiation damper (see LeagueCalibration): a manager whose dedicated starter
        # slots at a NON-flex position are already full feels less of the raw ADP pull there.
        # 0.0 (the default everywhere) is an exact no-op.
        sat = 0.0
        if calibration.satiation_damper and pos not in cfg.flex_eligible:
            starters_here = cfg.starters.get(pos, 0)
            if starters_here > 0 and have.get(pos, 0) >= starters_here:
                sat = calibration.satiation_damper
        util = -mu_eff + g * need.get(pos, 0.0) + herd - sat
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
