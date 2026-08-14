"""Man-games replacement level -- the number every other number leans on.

The idea in one line: a league does not need "24 starting RBs", it needs **RB man-games**.
12 teams x 2 RB slots x 17 weeks = 408 RB-games that somebody has to play. Real players miss
time, so covering 408 RB-games takes more than 24 RBs; you have to walk down the ranking
adding each player's *expected games* until the cumulative supply covers the demand. Wherever
that crosses is replacement level.

That framing is what produces the league's actual edge. With two mandatory QB slots the demand
is 12 x 2 x 17 = 408 QB-games, and at ~15.6 expected games per QB the crossing lands near
QB27 -- not the QB13-14 that every public ranking is built around (CLAUDE.md).

Nothing here knows the league is 12 teams or starts 2 QBs. Every quantity comes out of a
:class:`~draftroom.config.LeagueConfig`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace as _dc_replace
from typing import Any, Iterable, Mapping, Sequence

from draftroom.config import LeagueConfig

__all__ = [
    "EXPECTED_GAMES_PRIOR",
    "PRIOR_BASE_WEEKS",
    "PlayerSeason",
    "ReplacementInfo",
    "DemandBreakdown",
    "expected_games",
    "man_games_demand",
    "man_games_demand_detail",
    "replacement_levels",
    "resolve_players",
]


# ---------------------------------------------------------------------------------------
# UNVERIFIED. Positional expected-games priors, in games out of a 17-game season.
#
# These are the historical availability rates commonly cited for fantasy-relevant starters
# (QB most durable, RB least). They have NOT been recomputed from nflreadpy data on this
# machine. They are load-bearing: expected games multiplies straight into EVoB, and it also
# sets how fast the man-games walk consumes demand, so a 1-game error at RB moves the RB
# baseline by roughly two ranks. Recompute from actual games played once historical stats are
# in the pipeline, per position, ideally as a function of ADP rank rather than a flat prior.
# ---------------------------------------------------------------------------------------
EXPECTED_GAMES_PRIOR: Mapping[str, float] = {
    "QB": 15.6,  # UNVERIFIED
    "RB": 13.9,  # UNVERIFIED
    "WR": 14.5,  # UNVERIFIED
    "TE": 14.2,  # UNVERIFIED
}

#: The priors above are expressed out of a 17-game season; they are rescaled if the league's
#: `weeks` differs, so the prior stays an availability *rate* rather than a raw count.
PRIOR_BASE_WEEKS = 17


def expected_games(
    pos: str,
    override: float | None = None,
    *,
    priors: Mapping[str, float] = EXPECTED_GAMES_PRIOR,
    weeks: int = PRIOR_BASE_WEEKS,
) -> float:
    """Expected games played for a player at ``pos``.

    Args:
        pos: position code (``QB``/``RB``/``WR``/``TE``).
        override: per-player expected games, e.g. a known suspension or a player already
            ruled out for part of the season. Takes precedence over the prior.
        priors: position -> expected games out of :data:`PRIOR_BASE_WEEKS`.
        weeks: the league's season length; the prior is rescaled to it.

    Raises:
        ValueError: unknown position with no override. Guessing a durability prior for a
            position we have never modeled would silently distort its baseline.
    """
    if override is not None:
        value = float(override)
    else:
        key = str(pos).upper()
        if key not in priors:
            raise ValueError(
                f"no expected-games prior for position {pos!r} (have "
                f"{sorted(priors)}); pass an override rather than guessing"
            )
        value = float(priors[key]) * (float(weeks) / float(PRIOR_BASE_WEEKS))
    if value < 0:
        raise ValueError(f"expected games must be >= 0, got {value}")
    return min(value, float(weeks))


@dataclass(frozen=True)
class PlayerSeason:
    """One player's projected season, reduced to what valuation needs.

    ``expected_games`` is optional: ``None`` means "use the positional prior". Anything
    supplied here overrides it.
    """

    player_id: str
    pos: str
    ppg: float
    expected_games: float | None = None
    #: Standard deviation of the player's *season total* fantasy points, if known.
    sigma_season: float | None = None
    #: Standard deviation of PPG, if that is what the projection source gives.
    sigma_ppg: float | None = None
    name: str = ""


@dataclass(frozen=True)
class ReplacementInfo:
    """Replacement level at one position, with the inputs that produced it."""

    pos: str
    baseline_rank: int
    baseline_ppg: float
    man_games_demand: float
    base_man_games: float
    flex_man_games: float
    flex_blocks: int
    pool_size: int
    #: True when the pool is too shallow to cover demand -- the baseline is then the bottom
    #: of the pool and is an *overestimate* of replacement quality. Never silently ignored.
    pool_exhausted: bool = False

    @property
    def baseline_ranks_averaged(self) -> tuple[int, ...]:
        """The 1-based ranks whose PPG was averaged into ``baseline_ppg``."""
        return tuple(
            r for r in (self.baseline_rank - 1, self.baseline_rank, self.baseline_rank + 1)
            if 1 <= r <= self.pool_size
        )


@dataclass(frozen=True)
class DemandBreakdown:
    """Man-games demand per position, plus how the flex blocks were allocated."""

    base: Mapping[str, float]
    flex_blocks: Mapping[str, int]
    demand: Mapping[str, float]
    #: One entry per allocated block: ``(position, marginal PPG that won it)``.
    trace: tuple[tuple[str, float], ...] = ()
    #: Positions where the pool ran out during allocation.
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------- coercion


def resolve_players(
    players: Iterable[Any],
    cfg: LeagueConfig,
    *,
    priors: Mapping[str, float] = EXPECTED_GAMES_PRIOR,
) -> tuple[PlayerSeason, ...]:
    """Normalize any player-ish records into :class:`PlayerSeason` with games filled in.

    Accepts :class:`PlayerSeason` instances, plain dicts, or any object exposing
    ``player_id`` / ``pos`` / ``ppg``.
    """
    out: list[PlayerSeason] = []
    for raw in players:
        p = _coerce_player(raw)
        games = expected_games(p.pos, p.expected_games, priors=priors, weeks=cfg.weeks)
        out.append(_dc_replace(p, pos=str(p.pos).upper(), expected_games=games))
    return tuple(out)


def _coerce_player(raw: Any) -> PlayerSeason:
    if isinstance(raw, PlayerSeason):
        return raw
    if isinstance(raw, Mapping):
        try:
            return PlayerSeason(
                player_id=str(raw["player_id"]),
                pos=str(raw["pos"]),
                ppg=float(raw["ppg"]),
                expected_games=_opt_float(raw.get("expected_games")),
                sigma_season=_opt_float(raw.get("sigma_season")),
                sigma_ppg=_opt_float(raw.get("sigma_ppg")),
                name=str(raw.get("name", "")),
            )
        except KeyError as exc:  # pragma: no cover - defensive
            raise KeyError(f"player record missing {exc} : {raw!r}") from exc
    return PlayerSeason(
        player_id=str(getattr(raw, "player_id")),
        pos=str(getattr(raw, "pos")),
        ppg=float(getattr(raw, "ppg")),
        expected_games=_opt_float(getattr(raw, "expected_games", None)),
        sigma_season=_opt_float(getattr(raw, "sigma_season", None)),
        sigma_ppg=_opt_float(getattr(raw, "sigma_ppg", None)),
        name=str(getattr(raw, "name", "")),
    )


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _by_position(players: Sequence[PlayerSeason]) -> dict[str, list[PlayerSeason]]:
    """Group players by position, each group sorted by PPG descending (stable on ties)."""
    groups: dict[str, list[PlayerSeason]] = {}
    for p in players:
        groups.setdefault(p.pos, []).append(p)
    for pos in groups:
        groups[pos].sort(key=lambda p: (-p.ppg, p.player_id))
    return groups


# ------------------------------------------------------------------------------ demand


def _crossing_index(pool: Sequence[PlayerSeason], demand: float) -> int | None:
    """0-based index of the player whose games push cumulative supply past ``demand``.

    Returns ``-1`` when demand is zero or negative (nobody is consumed), and ``None`` when
    the pool cannot cover the demand at all.
    """
    if demand <= 0:
        return -1
    cumulative = 0.0
    for i, p in enumerate(pool):
        cumulative += float(p.expected_games or 0.0)
        if cumulative >= demand:
            return i
    return None


def _marginal_ppg(pool: Sequence[PlayerSeason], demand: float) -> float:
    """PPG of the next player past the current baseline -- who a new slot would press into
    starting duty. ``-inf`` when the pool cannot supply one, so the greedy never allocates
    into a position that has already run dry."""
    crossing = _crossing_index(pool, demand)
    if crossing is None:
        return float("-inf")
    nxt = crossing + 1
    if nxt >= len(pool):
        return float("-inf")
    return float(pool[nxt].ppg)


def man_games_demand_detail(
    cfg: LeagueConfig,
    players: Iterable[Any] | None = None,
    *,
    priors: Mapping[str, float] = EXPECTED_GAMES_PRIOR,
) -> DemandBreakdown:
    """Man-games demand per position, with the flex allocation shown.

    Base demand is ``teams * starters[pos] * weeks``. The flex adds
    ``teams * flex_slots * weeks`` more man-games, allocated by **greedy marginal
    allocation**: one roster-slot-season block (``weeks`` man-games) at a time, each block
    going to whichever flex-eligible position currently has the best next-past-baseline
    player, recomputing after every block. A flex slot is filled by whoever is best *at that
    slot*, so raw PPG is the right comparison across positions.
    """
    base = {
        pos: float(cfg.teams) * float(count) * float(cfg.weeks)
        for pos, count in cfg.starters.items()
    }
    for pos in cfg.flex_eligible:
        base.setdefault(pos, 0.0)

    demand = dict(base)
    blocks_per_pos = {pos: 0 for pos in demand}
    total_blocks = cfg.teams * cfg.flex_slots
    trace: list[tuple[str, float]] = []
    warnings: list[str] = []

    if total_blocks == 0:
        return DemandBreakdown(base=base, flex_blocks=blocks_per_pos, demand=demand)

    if players is None:
        raise ValueError(
            "flex allocation needs a player pool: greedy marginal allocation compares the "
            "next-past-baseline player at each flex-eligible position. Pass `players`, or "
            "use a config with flex_slots == 0."
        )

    resolved = resolve_players(players, cfg, priors=priors)
    pools = _by_position(resolved)
    eligible = sorted(cfg.flex_eligible)
    missing = [pos for pos in eligible if not pools.get(pos)]
    if missing:
        warnings.append(
            f"no players at flex-eligible position(s) {missing}; they cannot receive blocks"
        )

    block = float(cfg.weeks)
    for _ in range(total_blocks):
        scored = [(pos, _marginal_ppg(pools.get(pos, ()), demand[pos])) for pos in eligible]
        best_pos, best_ppg = max(scored, key=lambda item: (item[1], -eligible.index(item[0])))
        if best_ppg == float("-inf"):
            # Every eligible pool is exhausted. The slot still has to be filled, so the
            # demand is real -- put it where there is the most remaining supply and say so
            # rather than dropping man-games on the floor.
            best_pos = max(
                eligible,
                key=lambda pos: (
                    sum(float(p.expected_games or 0.0) for p in pools.get(pos, ()))
                    - demand[pos],
                    -eligible.index(pos),
                ),
            )
            warnings.append(
                f"flex block allocated to {best_pos} with no marginal player available "
                f"(pool exhausted); baseline there is an overestimate"
            )
        demand[best_pos] += block
        blocks_per_pos[best_pos] += 1
        trace.append((best_pos, best_ppg))

    allocated = sum(blocks_per_pos.values())
    if allocated != total_blocks:  # pragma: no cover - guard against a future edit
        raise AssertionError(f"allocated {allocated} flex blocks, expected {total_blocks}")

    return DemandBreakdown(
        base=base,
        flex_blocks=blocks_per_pos,
        demand=demand,
        trace=tuple(trace),
        warnings=tuple(warnings),
    )


def man_games_demand(
    cfg: LeagueConfig,
    players: Iterable[Any] | None = None,
    *,
    priors: Mapping[str, float] = EXPECTED_GAMES_PRIOR,
) -> dict[str, float]:
    """Total man-games each position must supply, base starters plus allocated flex."""
    return dict(man_games_demand_detail(cfg, players, priors=priors).demand)


# ------------------------------------------------------------------------- replacement


def replacement_levels(
    players: Iterable[Any],
    cfg: LeagueConfig,
    *,
    priors: Mapping[str, float] = EXPECTED_GAMES_PRIOR,
) -> dict[str, ReplacementInfo]:
    """Replacement level per position: baseline rank and baseline PPG.

    Walk each position's ranking by PPG, accumulating expected games until the cumulative
    supply reaches that position's man-games demand. The rank where it crosses is the
    baseline rank. Baseline PPG is the mean of ranks ``(B-1, B, B+1)`` rather than rank ``B``
    alone: a single player's projection sitting exactly on the crossing would otherwise drive
    every EVoB at the position, and projections at that depth are noisy.
    """
    resolved = resolve_players(players, cfg, priors=priors)
    pools = _by_position(resolved)
    breakdown = man_games_demand_detail(cfg, resolved, priors=priors)

    out: dict[str, ReplacementInfo] = {}
    for pos, demand in breakdown.demand.items():
        pool = pools.get(pos, [])
        if not pool:
            continue
        crossing = _crossing_index(pool, demand)
        exhausted = crossing is None
        if exhausted:
            baseline_rank = len(pool)
        else:
            baseline_rank = max(1, crossing + 1)

        idx = [baseline_rank - 2, baseline_rank - 1, baseline_rank]  # 0-based B-1, B, B+1
        window = [pool[i].ppg for i in idx if 0 <= i < len(pool)]
        baseline_ppg = sum(window) / len(window)

        base = float(breakdown.base.get(pos, 0.0))
        out[pos] = ReplacementInfo(
            pos=pos,
            baseline_rank=baseline_rank,
            baseline_ppg=baseline_ppg,
            man_games_demand=demand,
            base_man_games=base,
            flex_man_games=demand - base,
            flex_blocks=int(breakdown.flex_blocks.get(pos, 0)),
            pool_size=len(pool),
            pool_exhausted=exhausted,
        )
    return out
