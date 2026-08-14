"""Print replacement levels for our league next to a conventional 1-QB league.

Run:
    C:\\dev\\draftroom\\.venv\\Scripts\\python.exe tools\\show_replacement.py

The pool here is SYNTHETIC -- a plausible declining PPG curve per position, not projections.
It exists to show the *shape* of the answer (how much deeper two mandatory QB slots push
replacement) before real projection data lands. Do not read the baseline PPG values as
forecasts; read the ranks.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from draftroom.config import LeagueConfig  # noqa: E402
from draftroom.valuation.evob import compute_draft_values  # noqa: E402
from draftroom.valuation.replacement import (  # noqa: E402
    PlayerSeason,
    man_games_demand_detail,
    replacement_levels,
)

# Synthetic pool: (position, count, best PPG, worst PPG), linear in between.
POOL_SHAPE = [
    ("QB", 32, 22.0, 10.0),
    ("RB", 60, 20.0, 5.0),
    ("WR", 80, 19.0, 5.0),
    ("TE", 30, 14.0, 3.0),
]

POSITION_ORDER = ["QB", "RB", "WR", "TE"]


def build_pool() -> list[PlayerSeason]:
    players: list[PlayerSeason] = []
    for pos, n, hi, lo in POOL_SHAPE:
        step = (hi - lo) / (n - 1)
        for i in range(n):
            players.append(
                PlayerSeason(
                    player_id=f"{pos}{i + 1}",
                    pos=pos,
                    ppg=round(hi - step * i, 3),
                    name=f"{pos}{i + 1}",
                )
            )
    return players


def base_config(qb_starters: int) -> LeagueConfig:
    """Our provisional league (see data/league_manual.yaml), with QB slots swapped."""
    cfg = LeagueConfig.from_yaml()
    return cfg.replace(starters={**dict(cfg.starters), "QB": qb_starters})


def print_table(title: str, cfg: LeagueConfig, players: list[PlayerSeason]) -> None:
    detail = man_games_demand_detail(cfg, players)
    levels = replacement_levels(players, cfg)

    print()
    print(title)
    print(
        f"  {cfg.teams} teams | starters "
        + ", ".join(f"{p}{cfg.starters[p]}" for p in POSITION_ORDER)
        + f" | flex {cfg.flex_slots} ({'/'.join(sorted(cfg.flex_eligible))})"
        + f" | bench {cfg.bench} | {cfg.weeks} weeks | roster {cfg.roster_size}"
    )
    print(
        "  Pos  Pool   Base MG  Flex blk  Total MG   Repl rank   Baseline PPG   "
        "Ranks averaged"
    )
    print("  " + "-" * 84)
    for pos in POSITION_ORDER:
        info = levels[pos]
        ranks = ",".join(str(r) for r in info.baseline_ranks_averaged)
        flag = "  POOL EXHAUSTED" if info.pool_exhausted else ""
        print(
            f"  {pos:<4} {info.pool_size:>4}   {info.base_man_games:>7.0f}  "
            f"{info.flex_blocks:>8}  {info.man_games_demand:>8.0f}   "
            f"{pos}{info.baseline_rank:<9}  {info.baseline_ppg:>10.2f}   {ranks:>14}{flag}"
        )
    if detail.warnings:
        for w in detail.warnings:
            print(f"  WARNING: {w}")


def print_top_board(cfg: LeagueConfig, players: list[PlayerSeason], n: int = 15) -> None:
    values = compute_draft_values(players, cfg)
    ordered = sorted(values.values(), key=lambda v: -v.evob)[:n]
    print()
    print(f"  Top {n} by EVoB in the 2-QB league (synthetic pool):")
    print("   #  Player   Pos     PPG   Baseline   ExpG      EVoB")
    print("  " + "-" * 56)
    for i, v in enumerate(ordered, 1):
        print(
            f"  {i:>2}  {v.player_id:<8} {v.pos:<4} {v.ppg:>6.2f}   {v.baseline_ppg:>7.2f}   "
            f"{v.expected_games:>4.1f}  {v.evob:>8.1f}"
        )


def main() -> None:
    players = build_pool()
    cfg_2qb = base_config(2)
    cfg_1qb = base_config(1)

    print("=" * 88)
    print("REPLACEMENT LEVELS -- synthetic pool, PROVISIONAL league settings")
    print("Pool: " + ", ".join(f"{n} {pos} ({hi}->{lo} PPG)" for pos, n, hi, lo in POOL_SHAPE))
    print("Expected games priors (UNVERIFIED): QB 15.6, RB 13.9, WR 14.5, TE 14.2 of 17")
    print("=" * 88)

    print_table("OUR LEAGUE (2 mandatory QB starters)", cfg_2qb, players)
    print_table("CONVENTIONAL LEAGUE (1 QB starter, otherwise identical)", cfg_1qb, players)

    qb2 = replacement_levels(players, cfg_2qb)["QB"]
    qb1 = replacement_levels(players, cfg_1qb)["QB"]
    print()
    print(
        f"  QB replacement moves QB{qb1.baseline_rank} -> QB{qb2.baseline_rank} "
        f"({qb2.baseline_rank / qb1.baseline_rank:.2f}x deeper), baseline PPG "
        f"{qb1.baseline_ppg:.2f} -> {qb2.baseline_ppg:.2f}"
    )
    expected_lo, expected_hi = 26, 28
    verdict = (
        "MATCHES" if expected_lo <= qb2.baseline_rank <= expected_hi else "DISAGREES WITH"
    )
    print(
        f"  CLAUDE.md expects QB{expected_lo}-{expected_hi}: computed QB{qb2.baseline_rank} "
        f"{verdict} that expectation."
    )

    print_top_board(cfg_2qb, players)


if __name__ == "__main__":
    main()
