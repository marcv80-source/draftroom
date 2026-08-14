"""Valuation core: man-games replacement levels and expected value over baseline.

* :mod:`draftroom.valuation.replacement` -- expected games, man-games demand with greedy flex
  allocation, and per-position replacement baselines.
* :mod:`draftroom.valuation.evob` -- EVoB and the :class:`~draftroom.valuation.evob.DraftValue`
  decomposition the UI explains a pick with.

The ``evob`` *function* is deliberately NOT re-exported here. Re-exporting it would rebind the
package attribute ``draftroom.valuation.evob`` from the submodule to the function, so
``import draftroom.valuation.evob as m`` would silently hand back a function and every
``m.DraftValue`` would fail. Import it from the submodule:
``from draftroom.valuation.evob import evob``.
"""

from draftroom.valuation.evob import DraftValue, compute_draft_values
from draftroom.valuation.replacement import (
    EXPECTED_GAMES_PRIOR,
    DemandBreakdown,
    PlayerSeason,
    ReplacementInfo,
    expected_games,
    man_games_demand,
    man_games_demand_detail,
    replacement_levels,
    resolve_players,
)

__all__ = [
    "DemandBreakdown",
    "DraftValue",
    "EXPECTED_GAMES_PRIOR",
    "PlayerSeason",
    "ReplacementInfo",
    "compute_draft_values",
    "expected_games",
    "man_games_demand",
    "man_games_demand_detail",
    "replacement_levels",
    "resolve_players",
]
