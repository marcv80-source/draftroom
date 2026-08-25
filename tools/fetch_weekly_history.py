"""Prep-only: pull and cache nflreadpy weekly player game logs, 2019-2025.

This is what ``valuation/bonuses.py``'s Tier 1 curves are fit on: per-game passing/rushing/
receiving yards, needed to count how often a player at a given yards-per-game clears a
milestone in any *single* game.

CLAUDE.md: "No live network call may exist on any draft-phase code path." This module hits the
network (through nflreadpy) and is therefore **prep phase only**. Nothing under
``backend/draftroom/valuation`` or a draft-night entry point may import it; they read the
already-fitted ``data/bonus_curves.json`` (via ``valuation.bonuses.load_curves``) instead.

Usage:
    python -m tools.fetch_weekly_history                       # 2019-2025
    python -m tools.fetch_weekly_history --seasons 2022 2023 2024 2025

Reuses the weekly-data proxy probe (retired 2026-08-25)'s proxy fix rather than re-solving it: this machine sits
behind a TLS-inspecting corporate proxy, and every HTTP stack needs pointing at the exported CA
bundle before nflreadpy opens its first socket.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

CA = r"C:\Users\mvaldes\.claude\corp-ca-bundle.pem"
if Path(CA).exists():
    for _var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "HTTPX_CA_BUNDLE"):
        os.environ.setdefault(_var, CA)

import polars as pl  # noqa: E402

# tools/fetch_weekly_history.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw" / "nflreadpy_weekly"

DEFAULT_SEASONS: tuple[int, ...] = tuple(range(2019, 2026))

#: nflreadpy's raw column name -> our canonical stat name. Mapped at ingest, per CLAUDE.md's
#: "Canonical stat vocabulary": nothing downstream of a source adapter should see a
#: source-specific field name.
_COLUMN_RENAME: dict[str, str] = {
    "passing_yards": "pass_yd",
    "rushing_yards": "rush_yd",
    "receiving_yards": "rec_yd",
}

_KEEP_COLUMNS: tuple[str, ...] = (
    "season",
    "week",
    "player_id",
    "player_display_name",
    "position",
    "pass_yd",
    "rush_yd",
    "rec_yd",
)

#: nflreadpy's weekly loader includes NFL postseason games by default (season_type == "POST"),
#: which can push a single player's game count past 17 (up to 21 for a Super Bowl run). This
#: league's fantasy season is the NFL regular season only (league_manual.yaml: "weeks: 17"),
#: so postseason rows must never enter the curves -- they are real games, but not games this
#: league ever pays a bonus on. Caught during the 2025 backtest: several WRs showed 18-21
#: "games" in a nominally 17-week season before this filter was added.
_REGULAR_SEASON_ONLY = "REG"

_RELEVANT_POSITIONS = ("QB", "RB", "WR", "TE")


def fetch_weekly_history(seasons: tuple[int, ...] = DEFAULT_SEASONS) -> pl.DataFrame:
    """Pull weekly player game logs for ``seasons`` from nflreadpy. Hits the network."""
    import nflreadpy as nfl  # deferred: this whole module is prep-only, never draft-night

    df = nfl.load_player_stats(seasons=list(seasons), summary_level="week")
    if not isinstance(df, pl.DataFrame):
        df = pl.DataFrame(df)  # defensive: pin the shape bonuses.py expects

    rename = {k: v for k, v in _COLUMN_RENAME.items() if k in df.columns}
    df = df.rename(rename)

    missing = [c for c in _KEEP_COLUMNS if c not in df.columns]
    if missing:
        raise KeyError(
            f"nflreadpy weekly data is missing expected column(s) {missing}; the gate in "
            "the weekly-data proxy probe (retired 2026-08-25) should be re-run before trusting this pipeline"
        )
    if "season_type" not in df.columns:
        raise KeyError(
            "nflreadpy weekly data has no 'season_type' column; cannot exclude postseason "
            "games (see the comment on _REGULAR_SEASON_ONLY above)"
        )

    return (
        df.filter(pl.col("season_type") == _REGULAR_SEASON_ONLY)
        .select(list(_KEEP_COLUMNS))
        .filter(pl.col("position").is_in(list(_RELEVANT_POSITIONS)))
        .filter(pl.col("player_id").is_not_null())
    )


def cache_weekly_history(seasons: tuple[int, ...] = DEFAULT_SEASONS) -> Path:
    """Fetch and write to ``data/raw/nflreadpy_weekly/<UTC timestamp>.csv``. Returns the path."""
    df = fetch_weekly_history(seasons)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S.%fZ")
    path = RAW_DIR / f"{ts}.csv"
    df.write_csv(path)
    return path


def load_latest_weekly_history() -> pl.DataFrame:
    """Read back the newest cached weekly history. Never touches the network."""
    if not RAW_DIR.exists():
        raise FileNotFoundError(f"no cached weekly history at {RAW_DIR}; run this tool first")
    files = sorted(p for p in RAW_DIR.iterdir() if p.suffix == ".csv")
    if not files:
        raise FileNotFoundError(f"no cached weekly history csv files in {RAW_DIR}")
    return pl.read_csv(files[-1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seasons", type=int, nargs="+", default=list(DEFAULT_SEASONS), help="seasons to fetch"
    )
    args = parser.parse_args(argv)

    print(f"fetching nflreadpy weekly player stats for seasons {args.seasons} ...")
    path = cache_weekly_history(tuple(args.seasons))
    df = pl.read_csv(path)

    print(f"wrote {df.height} rows x {df.width} cols -> {path}")
    counts = df.group_by("position").agg(pl.len().alias("rows")).sort("position")
    for row in counts.to_dicts():
        print(f"  {row['position']:<3} {row['rows']:>6} game-rows")
    seasons_present = sorted(df.get_column("season").unique().to_list())
    print(f"seasons present: {seasons_present}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
