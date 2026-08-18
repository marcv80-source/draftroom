"""EVoB -- expected value over baseline, the per-game valuation this model drafts on.

``EVoB = (PPG - baseline_PPG) * expected_games``

The alternative (season-total VBD, "projected points minus the baseline's projected points")
gets the ordering wrong whenever games played differ, which is most of the time. Harstad's
example, kept as the first test in the suite: a player who scores 83.2 points in 7 games is
worth more than one who scores 84.2 in 16, because in the ten weeks he plays he is beating
replacement by six points a game, and the weeks he misses are covered by a bench player who is
roughly replacement anyway. Season totals rank them backwards.

:class:`DraftValue` keeps the components rather than collapsing to one number, because the UI
has to explain a pick in two or three bullets each backed by a computed number (CLAUDE.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from draftroom.config import LeagueConfig
from draftroom.valuation.replacement import (
    EXPECTED_GAMES_CURVE,
    AvailabilityBin,
    ReplacementInfo,
    replacement_levels,
    resolve_players,
)

__all__ = ["DraftValue", "evob", "compute_draft_values"]


def evob(ppg: float, baseline_ppg: float, expected_games: float) -> float:
    """Expected points above replacement over the season the player is expected to play."""
    return (float(ppg) - float(baseline_ppg)) * float(expected_games)


@dataclass(frozen=True)
class DraftValue:
    """One player's draft value with every input kept visible.

    ``dv = evob - lam * sigma_season`` is the risk-adjusted number. ``lam`` is a *preference*,
    not an estimate: at 0 the model is risk-neutral and ``dv == evob``.
    """

    player_id: str
    pos: str
    ppg: float
    baseline_ppg: float
    expected_games: float
    evob: float
    sigma_season: float
    lam: float
    dv: float
    name: str = ""
    #: How ``sigma_season`` was obtained -- "given", "from_sigma_ppg", or "absent".
    sigma_source: str = "absent"

    @property
    def points_above_baseline_per_game(self) -> float:
        return self.ppg - self.baseline_ppg


def _sigma_season_for(player: Any, games: float) -> tuple[float, str]:
    """Resolve a player's season-total sigma.

    UNVERIFIED MODELING ASSUMPTION: when only a PPG sigma is available, season sigma is taken
    as ``sigma_ppg * expected_games`` (perfectly correlated weeks) rather than
    ``sigma_ppg * sqrt(expected_games)`` (independent weeks). The dominant uncertainty in a
    season projection is talent/role/usage, which persists week to week, so the linear form is
    the conservative one -- but this has not been fit to data. Supplying ``sigma_season``
    directly bypasses it entirely, and the default of 0.0 means the risk penalty stays inert
    until someone deliberately turns it on.
    """
    if getattr(player, "sigma_season", None) is not None:
        return float(player.sigma_season), "given"
    sigma_ppg = getattr(player, "sigma_ppg", None)
    if sigma_ppg is not None:
        return float(sigma_ppg) * float(games), "from_sigma_ppg"
    return 0.0, "absent"


def compute_draft_values(
    players: Iterable[Any],
    cfg: LeagueConfig,
    lam: float = 0.0,
    *,
    curves: Mapping[str, tuple[AvailabilityBin, ...]] = EXPECTED_GAMES_CURVE,
    replacement: Mapping[str, ReplacementInfo] | None = None,
) -> dict[str, DraftValue]:
    """Draft value for every player, against replacement levels derived from ``cfg``.

    Args:
        players: player-ish records (see
            :func:`~draftroom.valuation.replacement.resolve_players`).
        cfg: the league. Every baseline comes from it; nothing is hardcoded.
        lam: risk aversion. ``dv = evob - lam * sigma_season``. 0 = risk-neutral.
        curves: rank-conditional expected-games curves, see
            :data:`~draftroom.valuation.replacement.EXPECTED_GAMES_CURVE`.
        replacement: precomputed baselines, to avoid recomputing during a live draft when the
            pool has not changed. Computed from ``players`` when omitted.

    Raises:
        KeyError: a player sits at a position with no replacement level -- meaning the league
            has no lineup demand for it. Scoring such a player against an invented baseline
            would be a silently wrong number, so it raises instead.
    """
    resolved = resolve_players(players, cfg, curves=curves)
    levels = (
        replacement_levels(resolved, cfg, curves=curves) if replacement is None else replacement
    )

    out: dict[str, DraftValue] = {}
    for p in resolved:
        info = levels.get(p.pos)
        if info is None:
            raise KeyError(
                f"player {p.player_id!r} is at position {p.pos!r}, which has no replacement "
                f"level in this league (positions with lineup demand: {sorted(levels)}). "
                f"Filter the pool to rostered positions before valuing it."
            )
        games = float(p.expected_games or 0.0)
        sigma_season, sigma_source = _sigma_season_for(p, games)
        value = evob(p.ppg, info.baseline_ppg, games)
        out[p.player_id] = DraftValue(
            player_id=p.player_id,
            pos=p.pos,
            ppg=p.ppg,
            baseline_ppg=info.baseline_ppg,
            expected_games=games,
            evob=value,
            sigma_season=sigma_season,
            lam=float(lam),
            dv=value - float(lam) * sigma_season,
            name=p.name,
            sigma_source=sigma_source,
        )
    return out
