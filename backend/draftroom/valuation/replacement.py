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
    "EXPECTED_GAMES_CURVE",
    "PRIOR_BASE_WEEKS",
    "AvailabilityBin",
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
# FITTED 2026-08-18 from nflreadpy weekly player-game logs, 2019-2025 regular season only
# (data/raw/nflreadpy_weekly/*.csv; CRITICAL: nflreadpy's weekly loader includes POSTSEASON
# rows by default -- tools/fetch_weekly_history.py already filters to season_type == "REG"
# before caching, confirmed by re-checking the cached file here: only 1 of 4,191 player-seasons
# shows >17 games, and that one is a mid-season trade landing on both teams' non-overlapping
# byes, not postseason leakage).
#
# REPLACES the old flat EXPECTED_GAMES_PRIOR (QB 15.6 / RB 13.9 / WR 14.5 / TE 14.2,
# UNVERIFIED). That flat prior was a reasonable description of the TOP of a position (see the
# rank 1-20 rows below, which land close to it) but was wrong applied uniformly all the way
# down a draft board: QBs ranked 25-40 play far fewer games than QBs ranked 1-15, and this
# league's replacement level sits at ~QB22-30 (CLAUDE.md), squarely in the range where the old
# flat number was most wrong.
#
# METHOD: for each of the 7 seasons and each position, rank every player-season by total
# position-relevant yardage (QB: pass_yd; RB: rush_yd + rec_yd; WR/TE: rec_yd) -- the best
# proxy available from this cached extract, which carries only pass_yd/rush_yd/rec_yd (no
# TDs/INTs/receptions -- tools/fetch_weekly_history.py's _KEEP_COLUMNS -- so it is NOT the
# player's real fantasy-point finish, just a yardage-based stand-in for it). Bin by rank into
# 5-rank buckets (35 player-seasons per bucket across the 7 seasons; the ranks >60 tail pools
# everything below into one open-ended bucket), average games played within a bucket, and
# smooth with the same weighted pool-adjacent-violators isotonic regression
# valuation/bonuses.py already uses for its hit-rate curves (rates cannot legitimately go UP
# as rank gets worse; any bump is sampling noise). This ranks players by END-OF-SEASON
# production, which is deliberately what you want here: the question this curve answers is
# "of the players who turn out to finish around rank N, how many games do they average," which
# is survivorship-inclusive by design (the reason a rank-25 QB often finishes there IS that he
# missed time) -- exactly the durability signal a draft-time rank estimate needs baked in.
#
# CROSS-CHECK against Mike Clay's published durability haircut (~2 games off QB/WR/TE, ~3 off
# RB, i.e. ~15 games for QB/WR/TE and ~14 for RB): this curve's rank 1-20 average is QB 15.85,
# WR 15.75, TE 15.27, RB 15.58 -- QB/WR/TE land within ~1 game of Clay's number (this fit is
# slightly LESS pessimistic, plausibly because Clay's population includes deeper backups this
# yardage-only proxy ranks lower down); RB is off by more (15.58 vs Clay's ~14) since a
# yardage-only proxy underweights receiving-back value at the very top less than Clay's own
# ranking does. Both fits agree on the qualitative shape (QB/WR/TE hold up better than RB at
# the top); see tools/fit_games_availability.py to regenerate against a fresh nflreadpy pull.
#
# Recompute periodically (new seasons of history become available every year) by re-running
# tools/fit_games_availability.py against a freshly-cached data/raw/nflreadpy_weekly/*.csv and
# pasting the printed literal back in here -- deliberately, with the real fit numbers in the
# commit, never guessed.
# ---------------------------------------------------------------------------------------

#: One bucket of a rank-conditional availability curve: (rank_lo, rank_hi_inclusive,
#: expected_games_out_of_17). ``rank_hi`` is ``None`` for the open-ended tail bucket.
AvailabilityBin = tuple[int, int | None, float]

EXPECTED_GAMES_CURVE: Mapping[str, tuple[AvailabilityBin, ...]] = {
    "QB": (
        (1, 5, 16.60),
        (6, 10, 16.20),
        (11, 15, 15.80),
        (16, 20, 14.80),
        (21, 25, 12.91),
        (26, 30, 11.06),
        (31, 35, 8.80),
        (36, 40, 6.60),
        (41, 45, 5.11),
        (46, 50, 4.03),
        (51, 55, 3.40),
        (56, 60, 3.26),
        (61, None, 2.48),
    ),
    "RB": (
        (1, 5, 16.26),
        (6, 10, 15.60),
        (11, 15, 15.31),
        (16, 20, 15.14),
        (21, 25, 14.77),
        (26, 30, 14.77),
        (31, 35, 14.29),
        (36, 40, 13.96),
        (41, 45, 13.96),
        (46, 50, 13.77),
        (51, 55, 12.97),
        (56, 60, 12.54),
        (61, None, 6.95),
    ),
    "WR": (
        (1, 5, 16.34),
        (6, 10, 16.00),
        (11, 15, 15.74),
        (16, 20, 15.50),
        (21, 25, 15.50),
        (26, 30, 15.50),
        (31, 35, 15.23),
        (36, 40, 15.06),
        (41, 45, 14.50),
        (46, 50, 14.50),
        (51, 55, 14.50),
        (56, 60, 13.63),
        (61, None, 8.46),
    ),
    "TE": (
        (1, 5, 15.94),
        (6, 10, 15.21),
        (11, 15, 15.21),
        (16, 20, 14.66),
        (21, 25, 13.96),
        (26, 30, 13.96),
        (31, 35, 13.24),
        (36, 40, 13.24),
        (41, 45, 12.20),
        (46, 50, 11.51),
        (51, 55, 11.51),
        (56, 60, 10.86),
        (61, None, 5.66),
    ),
}

#: The curves above are expressed in games out of a 17-game season; they are rescaled if the
#: league's `weeks` differs, so they stay an availability *rate* rather than a raw count.
PRIOR_BASE_WEEKS = 17


def _games_for_rank(pos: str, rank: int, curves: Mapping[str, tuple[AvailabilityBin, ...]]) -> float:
    """Look up the fitted games-out-of-17 figure for ``pos`` at positional rank ``rank``.

    Buckets are contiguous starting at rank 1 with an open-ended (``rank_hi=None``) tail, so
    every rank >= 1 matches exactly one bucket; flat extrapolation past the fitted range lives
    in that tail bucket, the same convention ``valuation/bonuses.py`` uses for its curves.
    """
    for rank_lo, rank_hi, games in curves[pos]:
        if rank >= rank_lo and (rank_hi is None or rank <= rank_hi):
            return games
    raise AssertionError(  # pragma: no cover - buckets are contiguous/open-ended by construction
        f"rank {rank} matched no bucket in the {pos} availability curve -- the curve table is malformed"
    )


def expected_games(
    pos: str,
    override: float | None = None,
    *,
    rank: int | None = None,
    curves: Mapping[str, tuple[AvailabilityBin, ...]] = EXPECTED_GAMES_CURVE,
    weeks: int = PRIOR_BASE_WEEKS,
) -> float:
    """Expected games played for a player at ``pos``, conditioned on positional rank.

    Args:
        pos: position code (``QB``/``RB``/``WR``/``TE``).
        override: per-player expected games, e.g. a known suspension or a player already
            ruled out for part of the season. Takes precedence over the curve, and does not
            need ``rank`` (bypasses the lookup entirely).
        rank: 1-based rank within ``pos`` (best = 1), e.g. by projected PPG. Required whenever
            ``override`` is not given -- there is no more flat per-position number to fall back
            to (see the curve's fitting note above: a QB1 and a QB35 do not share a durability
            outlook, and pretending they do is exactly the bug this replaced).
        curves: position -> rank-conditional availability buckets, see
            :data:`EXPECTED_GAMES_CURVE`.
        weeks: the league's season length; the curve is rescaled to it.

    Raises:
        ValueError: unknown position with no override, or no ``rank`` given when one is
            needed. Guessing a durability curve (or a rank) for a position/player we have not
            actually placed would silently distort its baseline.
    """
    if override is not None:
        value = float(override)
    else:
        key = str(pos).upper()
        if key not in curves:
            raise ValueError(
                f"no expected-games curve for position {pos!r} (have "
                f"{sorted(curves)}); pass an override rather than guessing"
            )
        if rank is None:
            raise ValueError(
                "expected_games needs `rank` to look up the rank-conditional availability "
                "curve (no flat per-position prior exists anymore -- see "
                "EXPECTED_GAMES_CURVE's fitting note). Pass an override instead if this "
                "player's games truly cannot be tied to a rank."
            )
        if int(rank) < 1:
            raise ValueError(f"rank must be >= 1, got {rank}")
        games_at_full_season = _games_for_rank(key, int(rank), curves)
        value = float(games_at_full_season) * (float(weeks) / float(PRIOR_BASE_WEEKS))
    if value < 0:
        raise ValueError(f"expected games must be >= 0, got {value}")
    return min(value, float(weeks))


@dataclass(frozen=True)
class PlayerSeason:
    """One player's projected season, reduced to what valuation needs.

    ``expected_games`` is optional: ``None`` means "look up the rank-conditional availability
    curve" (see :func:`resolve_players`/:data:`EXPECTED_GAMES_CURVE`) -- the player's rank is
    derived from ``ppg`` within their position, not supplied here. Anything supplied here
    overrides that lookup entirely.
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
    curves: Mapping[str, tuple[AvailabilityBin, ...]] = EXPECTED_GAMES_CURVE,
) -> tuple[PlayerSeason, ...]:
    """Normalize any player-ish records into :class:`PlayerSeason` with games filled in.

    Accepts :class:`PlayerSeason` instances, plain dicts, or any object exposing
    ``player_id`` / ``pos`` / ``ppg``.

    Games are rank-conditional now (see :data:`EXPECTED_GAMES_CURVE`), so a player with no
    explicit ``expected_games`` override needs a rank first: this groups by position and sorts
    by PPG descending (best = rank 1) -- the same "how good do we think this player is right
    now" signal the rest of the pipeline already ranks on -- before looking up each player's
    games in the curve. A player already known to be out part of the season should carry an
    explicit ``expected_games`` override instead, which skips the rank lookup entirely.
    """
    coerced = [_coerce_player(raw) for raw in players]
    by_pos: dict[str, list[PlayerSeason]] = {}
    for p in coerced:
        by_pos.setdefault(str(p.pos).upper(), []).append(p)

    out: list[PlayerSeason] = []
    for pos, group in by_pos.items():
        ranked = sorted(group, key=lambda p: (-p.ppg, p.player_id))
        for rank, p in enumerate(ranked, start=1):
            games = expected_games(pos, p.expected_games, rank=rank, curves=curves, weeks=cfg.weeks)
            out.append(_dc_replace(p, pos=pos, expected_games=games))
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
    curves: Mapping[str, tuple[AvailabilityBin, ...]] = EXPECTED_GAMES_CURVE,
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

    resolved = resolve_players(players, cfg, curves=curves)
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
    curves: Mapping[str, tuple[AvailabilityBin, ...]] = EXPECTED_GAMES_CURVE,
) -> dict[str, float]:
    """Total man-games each position must supply, base starters plus allocated flex."""
    return dict(man_games_demand_detail(cfg, players, curves=curves).demand)


# ------------------------------------------------------------------------- replacement


def replacement_levels(
    players: Iterable[Any],
    cfg: LeagueConfig,
    *,
    curves: Mapping[str, tuple[AvailabilityBin, ...]] = EXPECTED_GAMES_CURVE,
) -> dict[str, ReplacementInfo]:
    """Replacement level per position: baseline rank and baseline PPG.

    Walk each position's ranking by PPG, accumulating expected games until the cumulative
    supply reaches that position's man-games demand. The rank where it crosses is the
    baseline rank. Baseline PPG is the mean of ranks ``(B-1, B, B+1)`` rather than rank ``B``
    alone: a single player's projection sitting exactly on the crossing would otherwise drive
    every EVoB at the position, and projections at that depth are noisy.
    """
    resolved = resolve_players(players, cfg, curves=curves)
    pools = _by_position(resolved)
    breakdown = man_games_demand_detail(cfg, resolved, curves=curves)

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
