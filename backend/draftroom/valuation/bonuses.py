"""Per-game yardage bonus valuation -- Tier 1 (empirical hit-rate curves) only.

The league pays milestone bonuses inside a *single game* (see ``docs/BONUS_SCORING.md``):

    passing    +3 at 300 yds, +1 more at 400, +1 more at 500
    rushing    +3 at 100 yds, +1 more at 150, +1 more at 200
    receiving  +3 at 100 yds, +1 more at 150, +1 more at 200

A season total cannot tell you how those yards were distributed across weeks, so a bonus is an
expectation over the *distribution* of single-game yardage, not a function of the season mean:

    E[season bonus] = games x Sum_k bonus_k x P(single-game yards >= threshold_k)

Tier 1 estimates ``P(single-game yards >= threshold)`` by counting: bin player-seasons by
position and yards-per-game, then compute the empirical rate at which the *individual games*
of players in that bin cleared each threshold. It is simply counting, and it is exactly
reality within the bins it has data for -- the tradeoff (documented in the plan) is that it
cannot distinguish two players at the same yards-per-game who reached it in different shapes
(a bell-cow's ten 100-yard games vs. a committee back's seventeen 59-yard games). That
differentiation is Tier 3's job and is explicitly out of scope here.

Two moments, kept separate on purpose: the bonus estimated here goes into the **mean** season
points (``expected_bonus``). The engine's risk knob (``lam`` in ``valuation.evob``) penalises
**dispersion of outcomes** and is untouched by anything in this module -- turning bonuses on
must never change a sigma/lambda computation elsewhere. They are different moments of the same
distribution and must not be assumed to net out against each other.

Ground truth for validation comes from ``actual_bonus``, which needs no model at all: given a
player's real per-game yardage, it just adds up whichever thresholds each game actually
cleared. That is what makes the bell-cow/committee difference and the 2025 backtest testable
against reality rather than another model.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "BONUS_STATS",
    "DEFAULT_BONUS_SCHEDULE",
    "DEFAULT_CURVES_PATH",
    "DEFAULT_POSITIONS",
    "BinRate",
    "BonusCurve",
    "BonusEstimate",
    "actual_bonus",
    "curve_from_dict",
    "curve_to_dict",
    "expected_bonus",
    "fit_empirical_curves",
    "load_bonus_schedule",
    "load_curves",
    "save_curves",
]

# backend/draftroom/valuation/bonuses.py -> valuation -> draftroom -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CURVES_PATH = REPO_ROOT / "data" / "bonus_curves.json"
DEFAULT_LEAGUE_YAML = REPO_ROOT / "data" / "league_manual.yaml"

#: The three canonical stats this league pays a per-game bonus on.
BONUS_STATS: tuple[str, ...] = ("pass_yd", "rush_yd", "rec_yd")

#: Fantasy positions we fit curves for. A position/stat combo with too little data (e.g. TE
#: passing yards) simply produces no curve; ``expected_bonus`` treats that as zero, never a
#: crash -- see the module-level note on missing curves below.
DEFAULT_POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE")

#: Literal mirror of ``data/league_manual.yaml``'s ``scoring_bonuses`` block, used as the
#: fallback when the yaml file is unavailable (e.g. a unit test run in isolation, or a
#: draft-night snapshot with no repo checkout). ``load_bonus_schedule`` reads the real file
#: first; this is not a second source of truth for anything that ships.
DEFAULT_BONUS_SCHEDULE: Mapping[str, tuple[dict[str, float], ...]] = {
    "pass_yd": (
        {"threshold": 300.0, "points": 3.0},
        {"threshold": 400.0, "points": 1.0},
        {"threshold": 500.0, "points": 1.0},
    ),
    "rush_yd": (
        {"threshold": 100.0, "points": 3.0},
        {"threshold": 150.0, "points": 1.0},
        {"threshold": 200.0, "points": 1.0},
    ),
    "rec_yd": (
        {"threshold": 100.0, "points": 3.0},
        {"threshold": 150.0, "points": 1.0},
        {"threshold": 200.0, "points": 1.0},
    ),
}

#: Bin width in yards used when grouping player-seasons by yards-per-game, per stat. Passing
#: yardage runs roughly 3x the scale of rushing/receiving yardage, so it gets a wider bin.
DEFAULT_BIN_WIDTH: Mapping[str, float] = {"pass_yd": 25.0, "rush_yd": 10.0, "rec_yd": 10.0}

#: A player-season needs at least this many games before it is trusted to place a bin -- a
#: 1-game "season" (e.g. a Week 17 call-up) would otherwise plant a spurious 100% or 0% rate.
DEFAULT_MIN_SEASON_GAMES = 4

#: Minimum individual games backing a bin before its rate is trusted on its own; sparser bins
#: are folded into a neighbor rather than reported (or dropped).
DEFAULT_MIN_GAMES_PER_BIN = 50


def load_bonus_schedule(path: str | Path | None = None) -> dict[str, tuple[dict[str, float], ...]]:
    """The league's per-game yardage bonus schedule, read from ``league_manual.yaml``.

    Reads the ``scoring_bonuses`` block directly (see ``data/league_manual.yaml``) rather than
    going through :class:`draftroom.config.LeagueConfig`, which has no field for it -- the
    league settings loader does not yet model per-game bonuses at all. Falls back to
    :data:`DEFAULT_BONUS_SCHEDULE` if the file or the key is unavailable.
    """
    target = Path(path) if path is not None else DEFAULT_LEAGUE_YAML
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {k: tuple(v) for k, v in DEFAULT_BONUS_SCHEDULE.items()}

    import yaml  # imported lazily, same convention as draftroom.config.LeagueConfig.from_yaml

    raw = yaml.safe_load(text) or {}
    block = raw.get("scoring_bonuses")
    if not block:
        return {k: tuple(v) for k, v in DEFAULT_BONUS_SCHEDULE.items()}
    return {
        str(stat): tuple(
            {"threshold": float(e["threshold"]), "points": float(e["points"])} for e in entries
        )
        for stat, entries in block.items()
    }


# ============================================================================== Tier 1 curves


@dataclass(frozen=True)
class BinRate:
    """One yards-per-game bin's empirical hit rate at each of a curve's thresholds.

    ``hit_rate`` is positional, aligned with the parent :class:`BonusCurve`'s ``thresholds``
    tuple (not a dict keyed by threshold) so it round-trips through JSON without stringifying
    float keys.
    """

    ypg_lo: float
    ypg_hi: float  # `float("inf")` for the open-ended top bin
    n_games: int
    hit_rate: tuple[float, ...]


@dataclass(frozen=True)
class BonusCurve:
    """Tier 1 lookup table for one (stat, position): yards-per-game bin -> hit rate.

    ``bins`` is sorted ascending by ``ypg_lo`` and covers the whole observed range; looking up
    a yards-per-game outside that range extrapolates flat from the nearest edge bin (Tier 1's
    known limitation -- it cannot extrapolate past what it counted; Tier 2's smooth fit exists
    to fix that).
    """

    stat: str
    position: str
    bin_width: float
    thresholds: tuple[float, ...]
    bins: tuple[BinRate, ...]
    n_games: int
    n_player_seasons: int


@dataclass(frozen=True)
class BonusEstimate:
    """Expected (or actual) season bonus points, itemised by stat.

    ``by_stat`` lets the UI say "+18 of his projection is yardage bonuses" and break that down
    further into "12 from passing, 6 from rushing," etc.
    """

    total: float
    by_stat: Mapping[str, float]


def _as_rows(weekly_df: Any) -> list[dict[str, Any]]:
    """Coerce a polars DataFrame, pandas DataFrame, or plain list of dicts into row dicts."""
    to_dicts = getattr(weekly_df, "to_dicts", None)
    if callable(to_dicts):  # polars.DataFrame
        return to_dicts()
    to_records = getattr(weekly_df, "to_dict", None)
    if callable(to_records):  # pandas.DataFrame
        return weekly_df.to_dict(orient="records")
    return list(weekly_df)


def _bin_index(ypg: float, width: float) -> int:
    return int(math.floor(max(0.0, ypg) / width))


def _merge_sparse_bins(
    raw_bins: Sequence[dict[str, Any]], n_thresholds: int, min_games: int
) -> list[dict[str, Any]]:
    """Walk bins ascending by ``bin_lo``, accumulating into a merged bin until it has enough
    games to trust, then starting a new one. A sparse tail is folded into the previous merged
    bin rather than dropped -- every game counted stays represented in the curve."""
    ordered = sorted(raw_bins, key=lambda r: r["bin_lo"])
    merged: list[dict[str, Any]] = []
    acc: dict[str, Any] | None = None
    for row in ordered:
        if acc is None:
            acc = {"bin_lo": row["bin_lo"], "n_games": row["n_games"], "hits": list(row["hits"])}
        else:
            acc["n_games"] += row["n_games"]
            acc["hits"] = [a + b for a, b in zip(acc["hits"], row["hits"])]
        if acc["n_games"] >= min_games:
            merged.append(acc)
            acc = None
    if acc is not None:
        if merged:
            last = merged[-1]
            last["n_games"] += acc["n_games"]
            last["hits"] = [a + b for a, b in zip(last["hits"], acc["hits"])]
        else:
            merged.append(acc)  # never enough data anywhere -- report what we counted, once
    return merged


def _pava_nondecreasing(values: Sequence[float], weights: Sequence[float]) -> list[float]:
    """Weighted isotonic regression (pool adjacent violators): the closest non-decreasing
    sequence to ``values`` under weighted squared error, weights = ``weights``.

    Bins are ordered by yards-per-game, and a hit rate at a fixed threshold has no business
    going *down* as average yards go *up* -- any dip is sampling noise, not signal. This is a
    standard, well-known smoothing technique (not a distributional assumption): it only
    resolves inversions between adjacent bins, in the direction reality has to go, weighted by
    how many games actually back each bin. It cannot manufacture a trend that is not already
    there, and where the raw counts are already monotonic it is a no-op.
    """
    # Each "segment" is [weighted_sum, weight_sum, n_original_bins_absorbed].
    segments: list[list[float]] = []
    for v, w in zip(values, weights):
        segments.append([v * w, w, 1])
        while len(segments) >= 2 and _seg_mean(segments[-2]) > _seg_mean(segments[-1]):
            b = segments.pop()
            a = segments.pop()
            segments.append([a[0] + b[0], a[1] + b[1], a[2] + b[2]])
    out: list[float] = []
    for seg in segments:
        out.extend([_seg_mean(seg)] * int(seg[2]))
    return out


def _seg_mean(seg: Sequence[float]) -> float:
    return seg[0] / seg[1] if seg[1] else 0.0


def _build_curve(
    merged_bins: Sequence[dict[str, Any]],
    *,
    stat: str,
    position: str,
    bin_width: float,
    thresholds: tuple[float, ...],
    n_games: int,
    n_player_seasons: int,
) -> BonusCurve:
    n_bins = len(merged_bins)
    weights = [float(m["n_games"]) for m in merged_bins]
    # Isotonic-smooth each threshold column independently across the bin sequence.
    smoothed_by_threshold: list[list[float]] = []
    for j in range(len(thresholds)):
        raw = [
            (m["hits"][j] / m["n_games"]) if m["n_games"] else 0.0 for m in merged_bins
        ]
        smoothed_by_threshold.append(_pava_nondecreasing(raw, weights) if n_bins else [])

    bins: list[BinRate] = []
    for i, m in enumerate(merged_bins):
        lo = float(m["bin_lo"])
        hi = float(merged_bins[i + 1]["bin_lo"]) if i + 1 < len(merged_bins) else float("inf")
        n = int(m["n_games"])
        rate = tuple(smoothed_by_threshold[j][i] for j in range(len(thresholds)))
        bins.append(BinRate(ypg_lo=lo, ypg_hi=hi, n_games=n, hit_rate=rate))
    return BonusCurve(
        stat=stat,
        position=position,
        bin_width=bin_width,
        thresholds=thresholds,
        bins=tuple(bins),
        n_games=n_games,
        n_player_seasons=n_player_seasons,
    )


def fit_empirical_curves(
    weekly_df: Any,
    *,
    schedule: Mapping[str, Sequence[Mapping[str, float]]] | None = None,
    positions: Sequence[str] = DEFAULT_POSITIONS,
    bin_width: Mapping[str, float] | None = None,
    min_season_games: int = DEFAULT_MIN_SEASON_GAMES,
    min_games_per_bin: int = DEFAULT_MIN_GAMES_PER_BIN,
    cache_path: str | Path | None = DEFAULT_CURVES_PATH,
) -> dict[tuple[str, str], BonusCurve]:
    """Tier 1 empirical hit-rate curves, one per (stat, position) with enough data.

    Args:
        weekly_df: one row per player per game, with columns/keys ``season``, ``player_id``,
            ``position``, and the canonical yardage stats in :data:`BONUS_STATS` (``pass_yd``,
            ``rush_yd``, ``rec_yd``). Accepts a polars or pandas DataFrame, or a plain list of
            row dicts. Produced by ``tools/fetch_weekly_history.py`` from nflreadpy; this
            function itself never touches the network.
        schedule: stat -> tuple of ``{"threshold": ..., "points": ...}``. Defaults to the
            league's real bonus schedule (:func:`load_bonus_schedule`).
        cache_path: where to write the fitted curves as JSON. Pass ``None`` to skip writing
            (e.g. for a backtest fit that must not clobber the production cache).

    Method: for each (stat, position), group player-weeks by ``(season, player_id)``, keep
    only seasons with ``>= min_season_games`` games played, and bin each such player-season by
    its season yards-per-game. Then, for every *individual game* played by a player in a bin,
    check which thresholds that single game's yards actually cleared, and aggregate: the bin's
    hit rate at a threshold is (games clearing it) / (total games in the bin). Bins with fewer
    than ``min_games_per_bin`` games are merged with a neighbor so no reported rate rests on a
    tiny sample.
    """
    schedule = schedule if schedule is not None else load_bonus_schedule()
    widths = {**DEFAULT_BIN_WIDTH, **(bin_width or {})}
    rows = _as_rows(weekly_df)

    curves: dict[tuple[str, str], BonusCurve] = {}
    for stat, entries in schedule.items():
        if stat not in BONUS_STATS:
            continue
        thresholds = tuple(float(e["threshold"]) for e in entries)
        width = float(widths.get(stat, 10.0))

        for pos in positions:
            pos_rows = [r for r in rows if str(r.get("position", "")).upper() == str(pos).upper()]
            if not pos_rows:
                continue

            # (season, player_id) -> {games, total, [game rows]}
            seasons: dict[tuple[Any, Any], dict[str, Any]] = {}
            for r in pos_rows:
                key = (r.get("season"), r.get("player_id"))
                s = seasons.setdefault(key, {"games": 0, "total": 0.0, "rows": []})
                s["games"] += 1
                s["total"] += float(r.get(stat, 0.0) or 0.0)
                s["rows"].append(r)

            raw_bins: dict[int, dict[str, Any]] = {}
            n_player_seasons = 0
            n_games = 0
            for s in seasons.values():
                if s["games"] < min_season_games:
                    continue
                n_player_seasons += 1
                ypg = s["total"] / s["games"]
                idx = _bin_index(ypg, width)
                bin_row = raw_bins.setdefault(
                    idx, {"bin_lo": idx * width, "n_games": 0, "hits": [0] * len(thresholds)}
                )
                for game in s["rows"]:
                    n_games += 1
                    bin_row["n_games"] += 1
                    value = float(game.get(stat, 0.0) or 0.0)
                    for j, t in enumerate(thresholds):
                        if value >= t:
                            bin_row["hits"][j] += 1

            if not raw_bins:
                continue

            merged = _merge_sparse_bins(list(raw_bins.values()), len(thresholds), min_games_per_bin)
            curves[(stat, pos)] = _build_curve(
                merged,
                stat=stat,
                position=pos,
                bin_width=width,
                thresholds=thresholds,
                n_games=n_games,
                n_player_seasons=n_player_seasons,
            )

    if cache_path is not None:
        save_curves(curves, cache_path)
    return curves


# ------------------------------------------------------------------------- JSON persistence


def curve_to_dict(curve: BonusCurve) -> dict[str, Any]:
    return {
        "stat": curve.stat,
        "position": curve.position,
        "bin_width": curve.bin_width,
        "thresholds": list(curve.thresholds),
        "n_games": curve.n_games,
        "n_player_seasons": curve.n_player_seasons,
        "bins": [
            {
                "ypg_lo": b.ypg_lo,
                "ypg_hi": None if math.isinf(b.ypg_hi) else b.ypg_hi,
                "n_games": b.n_games,
                "hit_rate": list(b.hit_rate),
            }
            for b in curve.bins
        ],
    }


def curve_from_dict(d: Mapping[str, Any]) -> BonusCurve:
    bins = tuple(
        BinRate(
            ypg_lo=float(b["ypg_lo"]),
            ypg_hi=float("inf") if b["ypg_hi"] is None else float(b["ypg_hi"]),
            n_games=int(b["n_games"]),
            hit_rate=tuple(float(x) for x in b["hit_rate"]),
        )
        for b in d["bins"]
    )
    return BonusCurve(
        stat=str(d["stat"]),
        position=str(d["position"]),
        bin_width=float(d["bin_width"]),
        thresholds=tuple(float(x) for x in d["thresholds"]),
        bins=bins,
        n_games=int(d["n_games"]),
        n_player_seasons=int(d["n_player_seasons"]),
    )


def save_curves(curves: Mapping[tuple[str, str], BonusCurve], path: str | Path | None = None) -> Path:
    """Write fitted curves to JSON. ``data/bonus_curves.json`` by default."""
    target = Path(path) if path is not None else DEFAULT_CURVES_PATH
    payload = {
        "fitted_at": datetime.now(timezone.utc).isoformat(),
        "curves": [curve_to_dict(c) for c in curves.values()],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


def load_curves(path: str | Path | None = None) -> dict[tuple[str, str], BonusCurve]:
    """Read back fitted curves. This is the only Tier 1 entry point draft-night code should
    ever call -- it touches disk, never the network, and never imports nflreadpy."""
    target = Path(path) if path is not None else DEFAULT_CURVES_PATH
    payload = json.loads(target.read_text(encoding="utf-8"))
    out: dict[tuple[str, str], BonusCurve] = {}
    for cd in payload.get("curves", []):
        c = curve_from_dict(cd)
        out[(c.stat, c.position)] = c
    return out


# ==================================================================== expected & actual bonus


def _find_bin(bins: Sequence[BinRate], ypg: float) -> BinRate | None:
    if not bins:
        return None
    if ypg < bins[0].ypg_lo:
        return bins[0]  # flat extrapolation below the observed range
    for b in bins:
        if b.ypg_lo <= ypg < b.ypg_hi:
            return b
    return bins[-1]  # flat extrapolation above the observed range (last bin's hi is +inf)


def _hit_rate(curve: BonusCurve | None, ypg: float, threshold: float) -> float:
    if curve is None:
        return 0.0
    try:
        idx = curve.thresholds.index(float(threshold))
    except ValueError:
        return 0.0  # curve was never fit at this exact threshold -- conservative default
    b = _find_bin(curve.bins, ypg)
    return b.hit_rate[idx] if b is not None else 0.0


def _stat_line_field(stat_line: Any, name: str, default: Any = 0.0) -> Any:
    if isinstance(stat_line, Mapping):
        if name in stat_line:
            return stat_line[name]
        return default
    return getattr(stat_line, name, default)


def _resolve_pos(stat_line: Any) -> str:
    pos = _stat_line_field(stat_line, "pos", None)
    if pos is None:
        pos = _stat_line_field(stat_line, "position", None)
    if pos is None:
        raise KeyError(
            "stat_line has no 'pos' (or 'position') field -- expected_bonus needs the "
            "player's position to pick the right curve"
        )
    return str(pos).upper()


def expected_bonus(
    stat_line: Any,
    cfg: Mapping[str, Sequence[Mapping[str, float]]] | None = None,
    curves: Mapping[tuple[str, str], BonusCurve] | None = None,
) -> BonusEstimate:
    """Expected season bonus points, itemised by stat: ``games x Sum_k bonus_k x P(yards >= k)``.

    Args:
        stat_line: a mapping or object with a ``pos``/``position``, a ``games`` count, and
            season-total values for whichever of ``pass_yd``/``rush_yd``/``rec_yd`` apply.
            Missing stats default to 0 (correct: no receiving yards means no receiving bonus).
        cfg: the bonus schedule (stat -> thresholds/points). Defaults to the league's real
            schedule via :func:`load_bonus_schedule`.
        curves: Tier 1 curves from :func:`fit_empirical_curves` / :func:`load_curves`. A
            missing (stat, position) curve -- e.g. TE passing yards -- contributes zero rather
            than raising: it is a genuine "not applicable," not a data error.

    The marginal bonuses at each threshold are independent tail probabilities (the schedule
    stacks: +3 at the first threshold, +1 more at the second, +1 more at the third), so they
    are simply summed -- no special handling needed, per the plan.
    """
    cfg = cfg if cfg is not None else load_bonus_schedule()
    curves = curves or {}

    games = float(_stat_line_field(stat_line, "games", 0.0) or 0.0)
    by_stat: dict[str, float] = {}
    if games <= 0:
        return BonusEstimate(total=0.0, by_stat={stat: 0.0 for stat in cfg})

    pos = _resolve_pos(stat_line)
    for stat, entries in cfg.items():
        total_yd = float(_stat_line_field(stat_line, stat, 0.0) or 0.0)
        ypg = total_yd / games
        curve = curves.get((stat, pos))
        per_game_points = 0.0
        for entry in entries:
            p = _hit_rate(curve, ypg, float(entry["threshold"]))
            per_game_points += float(entry["points"]) * p
        by_stat[stat] = per_game_points * games

    return BonusEstimate(total=sum(by_stat.values()), by_stat=by_stat)


def actual_bonus(
    weekly_games: Iterable[Any],
    cfg: Mapping[str, Sequence[Mapping[str, float]]] | None = None,
) -> BonusEstimate:
    """Real bonus points a player actually earned, computed directly from real per-game yards.

    No model, no probability: for every game, add whichever thresholds that game's yards
    actually cleared, per stat, then sum. This is the ground truth ``expected_bonus`` is
    validated against -- it is what makes the bell-cow/committee distinction and the 2025
    backtest testable against reality rather than against another model.

    Args:
        weekly_games: one entry per game played, each a mapping or object exposing whichever
            of ``pass_yd``/``rush_yd``/``rec_yd`` apply (missing/absent stats count as 0).
        cfg: the bonus schedule. Defaults to the league's real schedule.
    """
    cfg = cfg if cfg is not None else load_bonus_schedule()
    totals: dict[str, float] = {stat: 0.0 for stat in cfg}
    for game in weekly_games:
        for stat, entries in cfg.items():
            yards = float(_stat_line_field(game, stat, 0.0) or 0.0)
            for entry in entries:
                if yards >= float(entry["threshold"]):
                    totals[stat] += float(entry["points"])
    return BonusEstimate(total=sum(totals.values()), by_stat=totals)
