"""What the Allendale Dad League actually did in 2025.

The whole reason to want last year's board is that national ADP describes a market, not a room.
This room has ten specific people in it, most of whom will be back, and they showed their hand.

Validates the transcription first, then reports the patterns that should change how Marc drafts.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "data" / "draft_2025.csv"

TEAMS = 10
ROUNDS = 15
STARTING_QBS = 20  # 10 teams x 2 mandatory QB slots


def load() -> list[dict]:
    rows: list[dict] = []
    with CSV_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            rows = list(csv.DictReader([line] + fh.readlines()))
            break
    for r in rows:
        r["pick_no"] = int(r["pick_no"])
        r["round"] = int(r["round"])
        r["slot"] = int(r["slot"])
    return rows


def validate(rows: list[dict]) -> bool:
    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"[{'PASS' if passed else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))

    check("150 picks", len(rows) == TEAMS * ROUNDS, f"{len(rows)}")
    check("pick numbers 1..150 with no gaps",
          [r["pick_no"] for r in rows] == list(range(1, TEAMS * ROUNDS + 1)))

    per_slot = Counter(r["slot"] for r in rows)
    check("every slot has exactly 15 picks",
          all(per_slot[s] == ROUNDS for s in range(1, TEAMS + 1)),
          str(dict(sorted(per_slot.items()))))

    # Snake integrity: odd rounds ascend by slot, even rounds descend.
    snake_ok = True
    for r in rows:
        pos_in_round = r["pick_no"] - (r["round"] - 1) * TEAMS
        expected = pos_in_round if r["round"] % 2 == 1 else TEAMS - pos_in_round + 1
        if r["slot"] != expected:
            snake_ok = False
            print(f"       snake mismatch at pick {r['pick_no']}: slot {r['slot']} != {expected}")
    check("snake order is internally consistent", snake_ok)

    names = [r["player"] for r in rows]
    dupes = [n for n, c in Counter(names).items() if c > 1]
    check("no player drafted twice", not dupes, ", ".join(dupes) if dupes else "")

    slot_team = defaultdict(set)
    for r in rows:
        slot_team[r["slot"]].add(r["team"])
    check("each slot maps to exactly one team",
          all(len(v) == 1 for v in slot_team.values()))
    return ok


def main() -> int:
    rows = load()
    print("=" * 88)
    print("TRANSCRIPTION CHECKS")
    print("=" * 88)
    if not validate(rows):
        print("\nTranscription is not trustworthy. Fix before using for calibration.")
        return 1

    print()
    print("=" * 88)
    print("HOW THIS ROOM DRAFTS QUARTERBACKS")
    print("=" * 88)
    qbs = [r for r in rows if r["pos"] == "QB"]
    by_round: Counter = Counter(r["round"] for r in qbs)
    running = 0
    print(f"{'Rd':>3}  {'QBs':>3}  {'cum':>4}   picks")
    for rnd in range(1, ROUNDS + 1):
        n = by_round.get(rnd, 0)
        running += n
        if n:
            who = ", ".join(f"{r['player']} ({r['pick_no']})" for r in qbs if r["round"] == rnd)
        else:
            who = "-"
        print(f"{rnd:>3}  {n:>3}  {running:>4}   {who}")

    qb20 = qbs[STARTING_QBS - 1] if len(qbs) >= STARTING_QBS else None
    print()
    print(f"total QBs drafted        : {len(qbs)} of 150 picks ({len(qbs)/len(rows)*100:.0f}%)")
    print(f"QBs in round 1           : {by_round.get(1, 0)} of {TEAMS}")
    if qb20:
        print(f"the {STARTING_QBS}th QB (last starter): {qb20['player']} at pick "
              f"{qb20['pick_no']} (round {qb20['round']})")
        print(f"  -> after pick {qb20['pick_no']}, every remaining QB is someone's backup")

    print()
    print("=" * 88)
    print("POSITION MIX BY ROUND")
    print("=" * 88)
    print(f"{'Rd':>3}  {'QB':>3} {'RB':>3} {'WR':>3} {'TE':>3}")
    for rnd in range(1, ROUNDS + 1):
        c = Counter(r["pos"] for r in rows if r["round"] == rnd)
        print(f"{rnd:>3}  {c.get('QB',0):>3} {c.get('RB',0):>3} {c.get('WR',0):>3} {c.get('TE',0):>3}")
    total = Counter(r["pos"] for r in rows)
    print(f"{'ALL':>3}  {total['QB']:>3} {total['RB']:>3} {total['WR']:>3} {total['TE']:>3}")

    print()
    print("=" * 88)
    print("MANAGER TENDENCIES -- when each team took its two starting QBs")
    print("=" * 88)
    print(f"{'slot':>4}  {'team':<26} {'QB1 pick':>9} {'QB2 pick':>9}  first 6 picks")
    for slot in range(1, TEAMS + 1):
        team_rows = sorted((r for r in rows if r["slot"] == slot), key=lambda r: r["pick_no"])
        team = team_rows[0]["team"]
        team_qbs = [r for r in team_rows if r["pos"] == "QB"]
        qb1 = team_qbs[0]["pick_no"] if team_qbs else None
        qb2 = team_qbs[1]["pick_no"] if len(team_qbs) > 1 else None
        shape = "".join(r["pos"][0] if r["pos"] != "TE" else "T" for r in team_rows[:6])
        me = "  <-- MARC" if "Country Club" in team else ""
        print(f"{slot:>4}  {team:<26} {str(qb1):>9} {str(qb2):>9}  {shape}{me}")

    print()
    print("  (first-6 shape: Q=QB R=RB W=WR T=TE, in pick order)")

    print()
    print("=" * 88)
    print("MARC'S 2025 DRAFT")
    print("=" * 88)
    mine = sorted((r for r in rows if "Country Club" in r["team"]), key=lambda r: r["pick_no"])
    for r in mine:
        print(f"  {r['round']:>2}.{(r['pick_no'] - (r['round']-1)*TEAMS):02d}  pick {r['pick_no']:>3}  "
              f"{r['player']:<24} {r['pos']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
