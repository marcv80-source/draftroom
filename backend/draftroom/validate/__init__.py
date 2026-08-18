"""Validation suite: the runnable sanity-invariant gate (CLAUDE.md's "Non-negotiable gates" #3)
and the tooling that builds a real (not synthetic) board to run it against.

* :mod:`draftroom.validate.invariants` -- the six sanity checks, each returning a
  :class:`~draftroom.validate.invariants.CheckResult` with the real numbers behind it.
* :mod:`draftroom.validate.board` -- joins cached FFC ADP onto cached Sleeper projections
  through the real crosswalk and real EVoB pipeline, for validation tooling that wants a real
  board instead of a synthetic one.

``tools/run_invariants.py`` is the CLI: it builds the real board, runs every check, prints the
real numbers, and exits 0 only if every check passed.
"""

from draftroom.validate.board import RealBoard, build_real_board
from draftroom.validate.invariants import (
    CheckResult,
    check_baseline_monotonic_starter_slots,
    check_baseline_monotonic_team_count,
    check_per_game_fixture,
    check_qb_count_in_top30,
    check_survival_monotone_and_normalized,
    check_top_qb_top8,
    deep_synthetic_pool,
    run_all,
)

__all__ = [
    "CheckResult",
    "RealBoard",
    "build_real_board",
    "check_baseline_monotonic_starter_slots",
    "check_baseline_monotonic_team_count",
    "check_per_game_fixture",
    "check_qb_count_in_top30",
    "check_survival_monotone_and_normalized",
    "check_top_qb_top8",
    "deep_synthetic_pool",
    "run_all",
]
