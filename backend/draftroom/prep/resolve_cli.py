"""CLI: run the player-identity crosswalk cascade over cached raw data.

    python -m draftroom.prep.resolve_cli

Reads only cached raw data (data/raw/sleeper, data/raw/ffc, and, if present,
data/raw/dynastyprocess) plus data/overrides.csv -- no network calls, so this
is safe to run offline / in CI. Run `fetch_all` and
`crosswalk.fetch_dynastyprocess_csv()` at least once first to populate the
cache.

Prints:
  - a table of resolve_method -> count
  - the completeness gate (CLAUDE.md gate #2): every FFC player with ADP rank
    <= 200 must resolve. PASS/FAIL, with every unresolved top-200 player and
    its best-guess candidates on FAIL.
  - writes data/unresolved_report.csv for manual triage into overrides.csv.

Exit code 1 on gate FAIL, so this can gate the prep pipeline.
"""

from __future__ import annotations

import csv
import sys

from draftroom.prep.crosswalk import DYNASTYPROCESS_SOURCE, REPO_ROOT, build_crosswalk
from draftroom.prep.ffc_client import parse_adp_rows
from draftroom.prep.http import load_latest_raw
from draftroom.prep.sleeper_client import SKILL_POSITIONS

TOP_N_GATE = 200


def main() -> int:
    sleeper_raw = load_latest_raw("sleeper")
    ffc_raw = load_latest_raw("ffc")
    ffc_rows = parse_adp_rows(ffc_raw)

    try:
        dynastyprocess_csv_text = load_latest_raw(DYNASTYPROCESS_SOURCE)
    except FileNotFoundError:
        dynastyprocess_csv_text = None
        print(
            "WARNING: no cached DynastyProcess crosswalk under data/raw/dynastyprocess/. "
            "Stage 1 direct-ID matching will only use Sleeper's own cross-ID fields. Run "
            "`python -c \"from draftroom.prep.crosswalk import fetch_dynastyprocess_csv as f; f()\"` "
            "once (needs network) to populate it.\n"
        )

    cw = build_crosswalk(sleeper_raw, ffc_rows, dynastyprocess_csv_text=dynastyprocess_csv_text)

    print("=== Resolve method counts ===")
    stats = cw.stats()
    total = sum(stats.values())
    for method, count in sorted(stats.items(), key=lambda kv: -kv[1]):
        print(f"  {method:24s} {count:4d}")
    print(f"  {'TOTAL':24s} {total:4d}")

    # This league drafts no K/DST (CLAUDE.md: "No kickers. No defenses."), so
    # "top 200 by ADP" for the completeness gate means top 200 among the
    # positions Sleeper's spine even carries (QB/RB/WR/TE) -- a DEF/PK row is
    # out of league scope, not a crosswalk miss, and is never counted here.
    relevant_rows = [r for r in ffc_rows if (r.pos or "").strip().upper() in SKILL_POSITIONS]
    ranked = sorted(relevant_rows, key=lambda r: r.adp)[:TOP_N_GATE]
    failures = []
    for rank, row in enumerate(ranked, start=1):
        key = str(row.player_id) if row.player_id is not None else f"{row.name}|{row.team}|{row.pos}"
        pid = cw.resolve("ffc", key)
        if pid is None:
            entry = cw.entries.get(("ffc", key))
            failures.append((rank, row, entry))

    print(f"\n=== Completeness gate: top {TOP_N_GATE} FFC ADP players must all resolve ===")
    if not failures:
        print(f"PASS -- all {len(ranked)} top-{TOP_N_GATE} FFC ADP players resolved.")
    else:
        print(f"FAIL -- {len(failures)} of {len(ranked)} top-{TOP_N_GATE} FFC ADP players unresolved:")
        for rank, row, entry in failures:
            detail = entry.detail if entry else "no entry"
            print(f"  rank={rank:3d} adp={row.adp:6.1f} {row.name} ({row.team} {row.pos}) -- {detail}")

    report_path = REPO_ROOT / "data" / "unresolved_report.csv"
    rows = cw.unresolved_report()
    with report_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["source", "name", "team", "pos", "adp", "detail"])
        for r in rows:
            writer.writerow([r["source"], r["name"], r["team"], r["pos"], r["adp"], r["detail"]])
    print(f"\nWrote {len(rows)} unresolved rows to {report_path}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
