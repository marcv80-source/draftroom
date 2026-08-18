"""Runnable sanity-invariant gate -- CLAUDE.md's "Non-negotiable gates" #3.

Builds the REAL draft board (cached FFC ADP joined onto cached Sleeper season projections
through the real crosswalk, scored with the real league's own scoring, valued through the real
replacement/EVoB pipeline -- see ``draftroom.validate.board``) against the REAL, CONFIRMED
10-team league config (``data/league_manual.yaml``), then runs every sanity invariant from
CLAUDE.md's gate list and prints the real numbers behind each one.

Exit code 0 if every check passes, 1 otherwise -- a snapshot that fails this gate is unloadable
per CLAUDE.md ("Never present a number that hasn't passed these").

Reads only cached files under data/raw/ -- no network call.

Run:
    C:\\dev\\draftroom\\.venv\\Scripts\\python.exe tools\\run_invariants.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from draftroom.validate import board as board_mod  # noqa: E402
from draftroom.validate import invariants  # noqa: E402


def main() -> int:
    # The crosswalk logs a WARNING per colliding DynastyProcess ID (expected, harmless, and
    # noisy at ~20 lines) -- suppress below WARNING so the gate's own PASS/FAIL table isn't
    # buried under it.
    logging.getLogger("draftroom.prep.crosswalk").setLevel(logging.ERROR)

    print("=" * 92)
    print("SANITY INVARIANT GATE  (CLAUDE.md: 'Non-negotiable gates' #3)")
    print("=" * 92)

    real = board_mod.build_real_board()
    cfg = real.cfg
    print(
        f"\nreal board: {len(real.players)} players valued (real Sleeper season projections x "
        f"real FFC ADP, joined via the real crosswalk), {len(real.excluded)} FFC skill-position "
        f"rows excluded (unresolved or no game projection)"
    )
    print(
        f"league: {cfg.teams} teams, starters={dict(cfg.starters)}, "
        f"flex={cfg.flex_slots}x{sorted(cfg.flex_eligible)}, bench={cfg.bench}, weeks={cfg.weeks} "
        f"(source: data/league_manual.yaml, CONFIRMED 2026-08-17)"
    )
    if real.excluded:
        sample = ", ".join(f"{r.name}({r.pos})" for r in real.excluded[:8])
        print(f"  excluded sample: {sample}{' ...' if len(real.excluded) > 8 else ''}")

    results = invariants.run_all(list(real.seasons), cfg)

    print(f"\n{len(results)} checks ran:")
    for r in results:
        print("  " + r.describe())

    n_pass = sum(1 for r in results if r.passed)
    print(f"\n{n_pass}/{len(results)} checks passed.")

    if n_pass == len(results):
        print("GATE: PASS")
        return 0
    print("GATE: FAIL -- see the FAIL line(s) above. Do not present numbers from this run.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
