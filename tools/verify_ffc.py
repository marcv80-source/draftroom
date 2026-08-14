"""Independent check that the ADP feed really is 2QB format.

This matters because the survival model is built entirely on this feed. Standard-league ADP would
misprice every quarterback in a two-QB league, and the failure would be silent: the numbers would
still look plausible, just wrong in the one direction that costs the most.

The tell is unmistakable. In a 1-QB league roughly one or two QBs go in the top 20 overall. In a
real 2-QB league, 24 quarterbacks have to start every week, so the top of the board is full of them.
"""

from __future__ import annotations

import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    files: list[str] = []
    for sub in ("ffc", "ffc_adp"):
        files.extend(glob.glob(os.path.join(ROOT, "data", "raw", sub, "*.json")))
    files.sort()
    if not files:
        print("FAIL: no cached FFC payload found under data/raw/{ffc,ffc_adp}")
        return 1
    print(f"cached FFC payloads: {len(files)}  (using {os.path.basename(files[-1])})")

    with open(files[-1], encoding="utf-8") as fh:
        raw = json.load(fh)
    players = raw.get("players", raw) if isinstance(raw, dict) else raw
    print(f"rows: {len(players)}")
    print(f"fields: {sorted(players[0].keys())}")
    if isinstance(raw, dict):
        meta = {k: v for k, v in raw.items() if k != "players"}
        print(f"payload meta: {meta}")

    rows = sorted(players, key=lambda p: float(p["adp"]))

    print("\n-- top 20 overall by ADP --")
    qb_top20 = 0
    for i, p in enumerate(rows[:20], 1):
        if p.get("position") == "QB":
            qb_top20 += 1
        print(
            f"{i:2d}. {p.get('name',''):<26} {p.get('position',''):<3} "
            f"{p.get('team',''):<4} adp={p.get('adp'):<7} stdev={p.get('stdev')}"
        )

    qbs = [p for p in rows if p.get("position") == "QB"]
    missing_stdev = [p for p in players if p.get("stdev") in (None, "")]

    print(f"\nQBs in top 20 overall : {qb_top20}")
    print(f"total QBs in feed     : {len(qbs)}")
    if qbs:
        print(f"QB1 ADP               : {qbs[0]['adp']}  ({qbs[0]['name']})")
    if len(qbs) >= 24:
        # 12 teams x 2 starting QBs = 24 QBs that must be rostered as starters.
        print(f"QB24 ADP              : {qbs[23]['adp']}  ({qbs[23]['name']})")
    print(f"rows missing stdev    : {len(missing_stdev)}")

    checks: list[tuple[str, bool, str]] = [
        ("2QB format (>=6 QBs in top 20)", qb_top20 >= 6, f"{qb_top20} QBs"),
        ("std dev present on every row", not missing_stdev, f"{len(missing_stdev)} missing"),
        ("enough QB depth for 24 starters", len(qbs) >= 24, f"{len(qbs)} QBs"),
    ]
    print("\n-- checks --")
    ok = True
    for label, passed, detail in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}  ({detail})")
        ok &= passed
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
