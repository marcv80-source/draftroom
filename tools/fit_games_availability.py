"""Fit rank-conditional expected-games-played curves from cached nflreadpy weekly history.

Regenerates the literal in `valuation/replacement.py`'s ``EXPECTED_GAMES_CURVE`` -- run this,
paste the printed dict values back in by hand (deliberately manual: this is a hard-coded
constant, not a config file loaded at runtime, matching this codebase's convention -- see
CLAUDE.md's "Calculation-engine conventions"). Never call this from anything under
`backend/draftroom/valuation` at runtime; it is prep-only tooling.

Reads ONLY the cached CSV under data/raw/nflreadpy_weekly/ (see tools/fetch_weekly_history.py)
-- no network call. That cache is already regular-season-only (fetch_weekly_history.py filters
`season_type == "REG"` before writing it), which matters: nflreadpy's weekly loader includes
POSTSEASON rows by default, and postseason games would inflate high-rank players' game counts.

METHOD (see valuation/replacement.py's EXPECTED_GAMES_CURVE docstring for the full rationale):
  1. Per season, per position, rank every player-season by total position-relevant yardage
     (QB: pass_yd; RB: rush_yd + rec_yd; WR/TE: rec_yd) -- the best proxy available from this
     extract, which carries no TDs/INTs/receptions.
  2. Bin ranks into fixed-width buckets (default width 5, up to MAX_RANK, with an open-ended
     tail bucket beyond that).
  3. Average games played within each bucket across all seasons.
  4. Smooth with weighted isotonic regression (pool adjacent violators, non-increasing) --
     the same technique valuation/bonuses.py already uses for its hit-rate curves: a rate has
     no business going UP as rank gets worse, so any bump between adjacent buckets is sampling
     noise, not signal.

Usage:
    python -m tools.fit_games_availability
"""

from __future__ import annotations

import csv
import glob
from collections import defaultdict
from pathlib import Path
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw" / "nflreadpy_weekly"

POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE")
BIN_WIDTH = 5
MAX_RANK = 60


def _latest_weekly_csv() -> Path:
    files = sorted(glob.glob(str(RAW_DIR / "*.csv")))
    if not files:
        raise FileNotFoundError(
            f"no cached weekly history under {RAW_DIR}; run tools/fetch_weekly_history.py "
            "first (that step hits the network -- this fitting script never does)"
        )
    return Path(files[-1])


def _rank_metric(pos: str, totals: Mapping[str, float]) -> float:
    """The yardage-only proxy for end-of-season fantasy relevance (see module docstring)."""
    if pos == "QB":
        return totals["pass_yd"]
    if pos == "RB":
        return totals["rush_yd"] + totals["rec_yd"]
    if pos in ("WR", "TE"):
        return totals["rec_yd"]
    raise ValueError(f"unexpected position {pos!r}")


def _load_player_season_totals(path: Path) -> dict[tuple[str, str], dict[str, float | str | int]]:
    agg: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"games": 0, "pass_yd": 0.0, "rush_yd": 0.0, "rec_yd": 0.0, "pos": None}
    )
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (row["season"], row["player_id"])
            a = agg[key]
            a["games"] += 1
            a["pass_yd"] += float(row["pass_yd"] or 0)
            a["rush_yd"] += float(row["rush_yd"] or 0)
            a["rec_yd"] += float(row["rec_yd"] or 0)
            a["pos"] = row["position"]
    return agg


def _rank_games_by_position(
    agg: Mapping[tuple[str, str], Mapping[str, object]],
) -> dict[tuple[str, int], list[int]]:
    by_season_pos: dict[tuple[str, str], list[tuple[float, int]]] = defaultdict(list)
    for (season, _pid), a in agg.items():
        pos = a["pos"]
        if pos not in POSITIONS:
            continue
        by_season_pos[(season, pos)].append((_rank_metric(pos, a), a["games"]))  # type: ignore[arg-type]

    rank_games: dict[tuple[str, int], list[int]] = defaultdict(list)
    for (_season, pos), rows in by_season_pos.items():
        rows.sort(key=lambda t: -t[0])
        for rank, (_metric, games) in enumerate(rows, start=1):
            rank_games[(pos, rank)].append(games)
    return rank_games


def _pava_nonincreasing(values: Sequence[float], weights: Sequence[float]) -> list[float]:
    """Weighted isotonic regression, forced non-increasing (mirrors bonuses.py's
    non-decreasing version -- games-played can only go down as rank gets worse)."""
    segments: list[list[float]] = []
    for v, w in zip(values, weights):
        segments.append([v * w, w, 1])
        while len(segments) >= 2 and _seg_mean(segments[-2]) < _seg_mean(segments[-1]):
            b = segments.pop()
            a = segments.pop()
            segments.append([a[0] + b[0], a[1] + b[1], a[2] + b[2]])
    out: list[float] = []
    for seg in segments:
        out.extend([_seg_mean(seg)] * int(seg[2]))
    return out


def _seg_mean(seg: Sequence[float]) -> float:
    return seg[0] / seg[1] if seg[1] else 0.0


def fit_curve(pos: str, rank_games: Mapping[tuple[str, int], list[int]]) -> list[tuple[int, int | None, float, int]]:
    """Returns a list of (rank_lo, rank_hi_or_None, smoothed_games, n_games_backing_bucket)."""
    buckets: list[tuple[int, int | None, list[int]]] = []
    lo = 1
    while lo <= MAX_RANK:
        hi = min(lo + BIN_WIDTH - 1, MAX_RANK)
        vals: list[int] = []
        for rank in range(lo, hi + 1):
            vals.extend(rank_games.get((pos, rank), []))
        buckets.append((lo, hi, vals))
        lo = hi + 1
    tail_vals: list[int] = []
    for (p, rank), vals in rank_games.items():
        if p == pos and rank > MAX_RANK:
            tail_vals.extend(vals)
    buckets.append((MAX_RANK + 1, None, tail_vals))

    raw_means = [(sum(v) / len(v) if v else 0.0) for _, _, v in buckets]
    weights = [max(len(v), 1) for _, _, v in buckets]
    smoothed = _pava_nonincreasing(raw_means, weights)

    return [
        (lo, hi, sm, len(v))
        for (lo, hi, v), sm in zip(buckets, smoothed)
    ]


def main() -> int:
    path = _latest_weekly_csv()
    print(f"using {path}")
    agg = _load_player_season_totals(path)
    seasons = sorted({s for s, _ in agg})
    print(f"seasons covered: {seasons} (n={len(seasons)})")
    rank_games = _rank_games_by_position(agg)

    print("\nEXPECTED_GAMES_CURVE = {")
    for pos in POSITIONS:
        curve = fit_curve(pos, rank_games)
        print(f'    "{pos}": (')
        for lo, hi, games, n in curve:
            hi_repr = "None" if hi is None else hi
            print(f"        ({lo}, {hi_repr}, {games:.2f}),  # n={n}")
        print("    ),")
    print("}")

    # Cross-check block: top-20 average per position vs Mike Clay's published haircut
    # (~2 games off QB/WR/TE, ~3 off RB out of a 17-game season).
    print("\ncross-check (rank 1-20 average games, vs Clay's ~2-game QB/WR/TE / ~3-game RB haircut):")
    for pos in POSITIONS:
        vals: list[int] = []
        for rank in range(1, 21):
            vals.extend(rank_games.get((pos, rank), []))
        avg = sum(vals) / len(vals) if vals else float("nan")
        print(f"  {pos}: rank 1-20 avg = {avg:.2f} games (haircut {17 - avg:.2f})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
