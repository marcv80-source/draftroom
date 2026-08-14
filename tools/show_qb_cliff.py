"""Print the QB cliff for draft slot 9 -- the single most decision-relevant number in this
league (CLAUDE.md: 12 teams x 2 QB x 17 weeks means the QB run is not optional to plan for).

For each pick belonging to slot 9 (9, 16, 33, 40, 57, 64 in a 12-team snake), shows:
  - expected number of STARTABLE QBs (top 24 by ADP -- 12 teams x 2 starters) still on the
    board, conditioned on the full pool being available right now (pick 1);
  - individual conditional survival probability for each of the top 12 QBs.

Run:
    C:\\dev\\draftroom\\.venv\\Scripts\\python.exe tools\\show_qb_cliff.py

Reads only the newest cached FFC payload under data/raw/ffc/ -- no network call, matching the
draft-night constraint this whole tool is built around.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from draftroom.draft import snake  # noqa: E402
from draftroom.draft.survival import (  # noqa: E402
    fit_sd_model,
    load_ffc_adp,
    p_available,
    survival_curve,
)

TEAMS = 12
DRAFT_SLOT = 9
ROUNDS_TO_SHOW = 6
CURRENT_PICK = 1  # the whole board is available -- the reference point for conditioning
STARTABLE_QB_CUT = 24  # 12 teams x 2 mandatory QB starters


def main() -> None:
    players = load_ffc_adp()
    qbs = sorted((p for p in players if p.pos == "QB"), key=lambda p: p.adp)
    fit = fit_sd_model(players)

    my_picks = snake.my_picks(TEAMS, DRAFT_SLOT, ROUNDS_TO_SHOW)

    print("=" * 88)
    print(f"QB CLIFF -- draft slot {DRAFT_SLOT}, {TEAMS}-team 2QB league")
    print(f"cached FFC payload: {len(players)} players, {len(qbs)} QBs")
    print(f"fitted sd ~ adp: {fit.describe()}")
    print(f"slot {DRAFT_SLOT}'s next {ROUNDS_TO_SHOW} picks: {my_picks}")
    print("=" * 88)

    top24 = qbs[:STARTABLE_QB_CUT]
    curve_all = survival_curve(qbs, my_picks, CURRENT_PICK, fit=fit)
    curve_startable = survival_curve(top24, my_picks, CURRENT_PICK, fit=fit)

    print()
    print("Expected QBs remaining at each of slot 9's picks:")
    print(f"  {'pick':>6}  {'label':>6}  {'all 36 QBs':>11}  {'startable (top 24)':>19}")
    for pk in my_picks:
        label = snake.pick_label(TEAMS, pk)
        print(f"  {pk:>6}  {label:>6}  {curve_all[pk]:>11.2f}  {curve_startable[pk]:>19.2f}")

    print()
    print("Survival probability for each of the top-12 QBs, conditioned on the full pool")
    print(f"being available now (pick {CURRENT_PICK}):")
    header = "  Rank  Player                ADP    sd  " + "  ".join(
        f"{snake.pick_label(TEAMS, pk):>7}" for pk in my_picks
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for i, p in enumerate(qbs[:12], 1):
        probs = [p_available(p.adp, p.stdev, pk, CURRENT_PICK, fit=fit) for pk in my_picks]
        row = "  ".join(f"{pr:>7.3f}" for pr in probs)
        print(f"  QB{i:<3} {p.name:<20}  {p.adp:>5.1f}  {p.stdev:>4.1f}  {row}")


if __name__ == "__main__":
    main()
