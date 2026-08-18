"""Cross-source projection disagreement -- Marc's own idea, approved 2026-08-18, unbuilt until
this session.

At snapshot/composite time, compute per-player spread across the three INDEPENDENT source
families this pipeline has: **Sleeper, FantasyPros (the manual CSVs), and ESPN**. CLAUDE.md is
explicit that ESPN's API and the Mike Clay PDF are the SAME source (verified 411/411 identical
field-for-field) -- never resolve both into a disagreement measure, or Clay agreeing with
himself would make disagreement look artificially small.

Two flavours, both computed here:
  (a) POINTS spread -- each source's projected stat line scored under the league's own scoring
      (:func:`draftroom.prep.scoring.score_statline`), then the spread of those season-total
      point figures across whichever source families actually have data for the player.
  (b) COMPONENT spread -- the same idea, but broken into passing/rushing/receiving/fumbles
      groups, because two sources can agree on a running back's total while disagreeing sharply
      on carries vs. catches -- different players entirely in a half-PPR league, and a spread
      computed only on the final total would hide exactly that disagreement.

THE MANDATED CAVEAT (attach wherever this measure is surfaced -- data, docstrings, UI copy):
with only three notionally-independent families, LOW disagreement is not evidence of an
accurate projection -- all three can share the same beat-reporter depth chart, the same
offseason narrative, and be wrong together. HIGH disagreement is the real signal here; its
absence is not a safety signal and must never be read as one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from draftroom.prep.scoring import score_statline

__all__ = [
    "INDEPENDENT_SOURCES",
    "STAT_GROUPS",
    "DISAGREEMENT_CAVEAT",
    "SourceDisagreement",
    "compute_disagreement",
    "sigma_ppg_from_disagreement",
]

#: The three source families CLAUDE.md confirms are independent. ESPN and the Mike Clay PDF
#: are the SAME source and must never both appear as separate entries feeding this measure.
INDEPENDENT_SOURCES: tuple[str, ...] = ("sleeper", "fantasypros", "espn")

#: Canonical stats grouped by play type, for the component-spread flavour. A stat absent from
#: the league's scoring dict simply contributes 0 to its group -- no special-casing needed.
STAT_GROUPS: Mapping[str, tuple[str, ...]] = {
    "passing": ("pass_att", "pass_cmp", "pass_yd", "pass_td", "pass_int", "pass_2pt"),
    "rushing": ("rush_att", "rush_yd", "rush_td", "rush_2pt"),
    "receiving": ("rec", "rec_tgt", "rec_yd", "rec_td", "rec_2pt"),
    "fumbles": ("fum_lost",),
}

DISAGREEMENT_CAVEAT = (
    "Three notionally independent source families feed this measure (Sleeper, FantasyPros, "
    "ESPN) -- but they are not three independent looks at reality: all three can lean on the "
    "same beat-reporter depth chart or the same offseason narrative and be wrong in the same "
    "direction together. HIGH disagreement across them is a real danger signal. LOW "
    "disagreement is NOT a safety signal and must never be read as 'this projection is "
    "accurate' -- it may just mean all three sources made the same assumption. (CLAUDE.md: "
    "ESPN's API and the Mike Clay PDF are ONE source, verified 411/411 identical -- never "
    "counted as two of the three families here.)"
)


@dataclass(frozen=True)
class SourceDisagreement:
    """One player's cross-source spread, both flavours, with every input kept visible."""

    player_id: str
    #: Which of INDEPENDENT_SOURCES actually had a statline for this player, sorted.
    sources_present: tuple[str, ...]
    #: source -> season-total fantasy points under the league's own scoring.
    points_by_source: Mapping[str, float]
    points_mean: float
    #: Population stdev of points_by_source's values. 0.0 (not missing) when < 2 sources.
    points_stdev: float
    #: max - min across points_by_source's values. 0.0 when < 2 sources.
    points_range: float
    #: group name (see STAT_GROUPS) -> population stdev of that group's points across sources.
    component_stdev: Mapping[str, float]
    n_sources: int

    @property
    def has_disagreement_signal(self) -> bool:
        """False when fewer than 2 independent sources had data -- there is nothing to
        compare, and points_stdev/component_stdev being 0.0 in that case must not be read as
        'sources agree.' Callers should check this before trusting a 0.0 spread."""
        return self.n_sources >= 2


def _population_stdev(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return variance**0.5


def compute_disagreement(
    player_id: str,
    stats_by_source: Mapping[str, Mapping[str, float]],
    scoring: Mapping[str, float],
) -> SourceDisagreement:
    """Build one player's :class:`SourceDisagreement` from whichever source statlines exist.

    Args:
        player_id: the resolved (crosswalk) player id these statlines share.
        stats_by_source: source name -> canonical stat mapping (e.g. ``StatLine.as_dict()``).
            Only keys in :data:`INDEPENDENT_SOURCES` are considered; a source absent from this
            mapping (or explicitly ``None``) simply does not contribute -- never fabricated.
        scoring: the league's own scoring (``LeagueConfig.scoring``).
    """
    sources_present = tuple(
        sorted(s for s in INDEPENDENT_SOURCES if stats_by_source.get(s) is not None)
    )
    points_by_source = {s: score_statline(stats_by_source[s], scoring) for s in sources_present}
    values = list(points_by_source.values())
    n = len(values)
    points_mean = sum(values) / n if n else 0.0
    points_stdev = _population_stdev(values)
    points_range = (max(values) - min(values)) if n >= 2 else 0.0

    component_stdev: dict[str, float] = {}
    for group, stat_names in STAT_GROUPS.items():
        group_scoring = {k: v for k, v in scoring.items() if k in stat_names}
        if not group_scoring:
            continue  # league doesn't score anything in this group -- nothing to disagree on
        group_values = [score_statline(stats_by_source[s], group_scoring) for s in sources_present]
        component_stdev[group] = _population_stdev(group_values)

    return SourceDisagreement(
        player_id=player_id,
        sources_present=sources_present,
        points_by_source=points_by_source,
        points_mean=points_mean,
        points_stdev=points_stdev,
        points_range=points_range,
        component_stdev=component_stdev,
        n_sources=n,
    )


def sigma_ppg_from_disagreement(d: SourceDisagreement, expected_games: float) -> float | None:
    """Map the points-spread into a PPG sigma :class:`~draftroom.valuation.evob.DraftValue` can
    consume, so ``sigma_source`` stops reading "absent" for players with real cross-source data.

    UNVERIFIED MODELING ASSUMPTION, documented once here rather than at each call site: this
    divides the season-POINTS stdev by ``expected_games`` to land on a PPG-scale figure. Each
    source may implicitly assume a different games count (Sleeper and ESPN both publish their
    own per-player figure; FantasyPros publishes none at all -- see prep/manual_csv.py's
    2026-08-18 fix), so part of what this measures is disagreement about DURABILITY, not just
    about weekly rate. That conflation is a known limitation, not a hidden one.

    Returns ``None`` (never a fabricated 0.0) when there are fewer than 2 independent sources
    or no games to divide by -- see :attr:`SourceDisagreement.has_disagreement_signal`.

    CAVEAT (see module-level ``DISAGREEMENT_CAVEAT``, same wording, attached here again because
    a sigma value on its own is exactly the kind of number someone reads out of context): a low
    sigma here is NOT evidence the projection is safe -- with only three correlated source
    families, it may just mean they all made the same assumption. High disagreement is the real
    signal; its absence is not a safety signal.
    """
    if not d.has_disagreement_signal or expected_games <= 0:
        return None
    return d.points_stdev / float(expected_games)
