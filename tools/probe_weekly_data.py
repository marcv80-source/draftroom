"""Can we actually get per-game yardage history? This is the gate for the bonus model.

Two things must both be true, and neither can be assumed:
  1. nflreadpy can reach its data through this machine's TLS-inspecting corporate proxy.
  2. The weekly data actually carries per-game passing / rushing / receiving yards, plus an id we
     can join to our crosswalk.

If either fails, the bonus plan needs a different data source, and it is far cheaper to find that
out now than halfway through building it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Point every HTTP stack at the corporate CA bundle before importing anything that opens a socket.
CA = r"C:\Users\mvaldes\.claude\corp-ca-bundle.pem"
if Path(CA).exists():
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "HTTPX_CA_BUNDLE"):
        os.environ.setdefault(var, CA)
    print(f"CA bundle wired in: {CA}")
else:
    print("WARNING: corporate CA bundle not found; TLS verification will probably fail")

import nflreadpy as nfl  # noqa: E402


def main() -> int:
    print(f"nflreadpy {nfl.__version__}\n")

    try:
        df = nfl.load_player_stats(seasons=[2025], summary_level="week")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: could not load weekly player stats: {type(exc).__name__}: {exc}")
        return 1

    print(f"loaded weekly player stats for 2025: {df.shape[0]} rows x {df.shape[1]} cols")

    cols = list(df.columns)
    wanted = {
        "passing yards": ["passing_yards"],
        "rushing yards": ["rushing_yards"],
        "receiving yards": ["receiving_yards"],
        "week": ["week"],
        "season": ["season"],
        "position": ["position"],
        "player name": ["player_display_name", "player_name"],
        "join id (gsis)": ["player_id", "gsis_id"],
    }
    print("\nrequired fields:")
    missing = []
    for label, candidates in wanted.items():
        hit = next((c for c in candidates if c in cols), None)
        print(f"  [{'OK  ' if hit else 'MISS'}] {label:<16} {hit or candidates}")
        if not hit:
            missing.append(label)

    # The whole point: can we count 100-yard games?
    try:
        import polars as pl

        rb = df.filter(
            (pl.col("position") == "RB") & (pl.col("rushing_yards").is_not_null())
        )
        games = rb.height
        hundred = rb.filter(pl.col("rushing_yards") >= 100).height
        print(f"\nRB game-weeks in 2025: {games}")
        print(f"  100+ rushing yard games: {hundred} ({hundred / games * 100:.1f}% of RB games)")

        wr = df.filter(
            (pl.col("position") == "WR") & (pl.col("receiving_yards").is_not_null())
        )
        wr_games = wr.height
        wr_hundred = wr.filter(pl.col("receiving_yards") >= 100).height
        print(f"WR game-weeks in 2025: {wr_games}")
        print(f"  100+ receiving yard games: {wr_hundred} ({wr_hundred / wr_games * 100:.1f}%)")

        qb = df.filter(
            (pl.col("position") == "QB") & (pl.col("passing_yards").is_not_null())
        )
        qb_games = qb.height
        qb_300 = qb.filter(pl.col("passing_yards") >= 300).height
        print(f"QB game-weeks in 2025: {qb_games}")
        print(f"  300+ passing yard games: {qb_300} ({qb_300 / qb_games * 100:.1f}%)")

        # The distinction the whole model exists to capture: same season total, different shape.
        print("\nsame-total, different-shape check (2025 RBs, 8+ games):")
        agg = (
            rb.group_by("player_display_name")
            .agg(
                pl.col("rushing_yards").sum().alias("total"),
                pl.col("rushing_yards").count().alias("g"),
                (pl.col("rushing_yards") >= 100).sum().alias("hundreds"),
            )
            .filter((pl.col("g") >= 8) & (pl.col("total") >= 600))
            .sort("total", descending=True)
        )
        rows = agg.head(12).rows(named=True)
        print(f"  {'player':<24} {'yds':>5} {'g':>3} {'100+':>5} {'bonus':>6}")
        for r in rows:
            print(
                f"  {r['player_display_name']:<24} {r['total']:>5.0f} {r['g']:>3} "
                f"{r['hundreds']:>5} {r['hundreds'] * 3:>6}"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"\ncould not compute hit rates: {type(exc).__name__}: {exc}")
        return 1

    # How many seasons can we get? More history = better curves.
    try:
        multi = nfl.load_player_stats(seasons=[2022, 2023, 2024, 2025], summary_level="week")
        print(f"\nmulti-season pull OK: {multi.shape[0]} rows across 2022-2025")
    except Exception as exc:  # noqa: BLE001
        print(f"\nmulti-season pull FAILED: {type(exc).__name__}: {exc}")
        return 1

    print("\nGATE: PASS" if not missing else f"\nGATE: FAIL -- missing {missing}")
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
