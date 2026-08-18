"""Fantasy points from component stats.

One function scores everything. Projections and historical actuals are the same shape (a
mapping of canonical stat name -> value), so they go through the same dot product against the
league's own modifiers. That is what makes the scoring reconciliation gate meaningful: if
re-scoring 2025 actuals with last season's modifiers reproduces Yahoo's recorded totals, then
the identical code path is also scoring the projections correctly.

Two asymmetric rules, on purpose:

* An **unknown key in the stat line** is fine and scores nothing. Sources emit stats nobody
  scores (targets, attempts); dropping them is correct, not a data loss.
* An **unknown key in the scoring dict** is an error. Scoring keys come from the league
  settings, so a non-canonical one means a modifier was mistranslated upstream and points are
  silently going missing.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

__all__ = [
    "CANONICAL_STATS",
    "ScoringKeyError",
    "score_statline",
    "score_all",
    "score_statline_with_bonus",
]


try:  # The prep.schema module is owned by another agent and may not exist yet.
    from draftroom.prep.schema import CANONICAL_STATS as _SCHEMA_CANONICAL_STATS

    CANONICAL_STATS: frozenset[str] = frozenset(_SCHEMA_CANONICAL_STATS)
except ImportError:  # pragma: no cover - exercised only before prep/schema.py lands
    # Fallback mirror of the canonical vocabulary in CLAUDE.md, used until
    # draftroom.prep.schema exists. If the two ever disagree, schema.py wins -- this literal
    # is here so the valuation core is testable in isolation, not to be a second source of
    # truth.
    CANONICAL_STATS = frozenset(
        {
            "pass_att", "pass_cmp", "pass_yd", "pass_td", "pass_int", "pass_2pt",
            "rush_att", "rush_yd", "rush_td", "rush_2pt",
            "rec", "rec_tgt", "rec_yd", "rec_td", "rec_2pt",
            "fum_lost",
            "games",
        }
    )


class ScoringKeyError(ValueError):
    """A scoring dict contains a key that is not a canonical stat name."""

    def __init__(self, bad_keys: Iterable[str]) -> None:
        self.bad_keys = tuple(sorted(bad_keys))
        super().__init__(
            "scoring contains non-canonical stat name(s): "
            + ", ".join(repr(k) for k in self.bad_keys)
            + ". Scoring keys come from the league's Yahoo modifiers, so a non-canonical key "
            "means a stat_id was mistranslated and points are going missing. Canonical names: "
            + ", ".join(sorted(CANONICAL_STATS))
        )


def validate_scoring(scoring: Mapping[str, float]) -> None:
    """Raise :class:`ScoringKeyError` if any scoring key is not canonical."""
    bad = [k for k in scoring if k not in CANONICAL_STATS]
    if bad:
        raise ScoringKeyError(bad)


def score_statline(stats: Mapping[str, float], scoring: Mapping[str, float]) -> float:
    """Fantasy points for one stat line: a pure dot product over canonical stat names.

    Args:
        stats: canonical stat name -> value. Keys absent from ``scoring`` contribute 0.
        scoring: canonical stat name -> points per unit (the league's Yahoo modifiers).

    Raises:
        ScoringKeyError: a key in ``scoring`` is not a canonical stat name.
    """
    validate_scoring(scoring)
    return _dot(stats, scoring)


def _dot(stats: Mapping[str, float] | Any, scoring: Mapping[str, float]) -> float:
    stats = _as_stats_mapping(stats)
    total = 0.0
    # Iterate the scoring dict: it is the short one, and it defines what counts.
    for name, per_unit in scoring.items():
        value = stats.get(name)
        if value is None:
            continue
        total += float(value) * float(per_unit)
    return total


def _as_stats_mapping(stats: Any) -> Mapping[str, float]:
    """Accept a plain mapping or a StatLine-like object (``prep.schema.StatLine``)."""
    if isinstance(stats, Mapping):
        return stats
    as_dict = getattr(stats, "as_dict", None)
    if callable(as_dict):
        return as_dict()
    return {
        name: getattr(stats, name)
        for name in CANONICAL_STATS
        if getattr(stats, name, None) is not None
    }


def score_all(
    statlines: Mapping[str, Mapping[str, float]] | Iterable[Any],
    scoring: Mapping[str, float],
) -> dict[str, float]:
    """Score many stat lines at once.

    ``statlines`` may be either a mapping of ``player_id -> stat mapping``, or an iterable of
    StatLine-like records exposing ``player_id`` plus either a ``stats`` mapping or the
    canonical fields themselves (so it works with ``prep.schema.StatLine`` once that lands).
    """
    validate_scoring(scoring)  # Validate once, not per player.

    out: dict[str, float] = {}
    if isinstance(statlines, Mapping):
        for player_id, stats in statlines.items():
            out[str(player_id)] = _dot(stats, scoring)
        return out

    for record in statlines:
        player_id, stats = _split_record(record)
        out[player_id] = _dot(stats, scoring)
    return out


def score_statline_with_bonus(
    stats: Mapping[str, float],
    scoring: Mapping[str, float],
    *,
    pos: str | None = None,
    games: float | None = None,
    bonus_schedule: Mapping[str, Any] | None = None,
    bonus_curves: Mapping[Any, Any] | None = None,
    include_bonus: bool = True,
) -> float:
    """The linear dot product, plus -- deliberately AFTER it, never inside it -- an additive
    expected per-game yardage bonus (see ``valuation.bonuses``, ``docs/BONUS_SCORING.md``).

    ``score_statline`` itself is untouched by this function and stays a pure dot product over
    canonical stat names: that purity is what makes the scoring reconciliation gate meaningful
    (CLAUDE.md gate #1 re-scores actuals with the engine and checks against Yahoo's recorded
    totals). Bonuses are bolted on here, as a second, separate term:

        total = score_statline(stats, scoring) + expected_bonus(...).total

    Bonuses are switchable off two ways -- pass ``include_bonus=False``, or simply omit
    ``bonus_schedule``/``bonus_curves`` (both default to ``None``, which turns this back into
    plain ``score_statline``). Either path reproduces ``score_statline`` exactly, byte for
    byte, which is itself covered by a test.

    The bonus term only ever adds to the **mean** (expected season points). It has no opinion
    about dispersion of outcomes, which is what the engine's risk-aversion knob (``lam`` in
    ``valuation.evob``) penalises -- the two operate on different moments of the same
    distribution and are computed independently. Nothing here reaches into or reads that
    penalty, on purpose.

    Args:
        pos: the player's canonical position (``QB``/``RB``/``WR``/``TE``). Required (via this
            argument or a ``pos``/``position`` key already in ``stats``) whenever a bonus is
            requested, since curves are looked up per position.
        games: games played (actual) or expected (projection) this season. Required whenever
            a bonus is requested -- the bonus formula is per-game probability times games.
        bonus_schedule: the league's per-game bonus schedule (stat -> thresholds/points), e.g.
            ``valuation.bonuses.load_bonus_schedule()``.
        bonus_curves: Tier 1 hit-rate curves, e.g. ``valuation.bonuses.load_curves()``.
    """
    base = score_statline(stats, scoring)
    if not include_bonus or bonus_schedule is None or bonus_curves is None:
        return base

    # Deferred import: draftroom.config (and therefore draftroom.valuation.replacement/evob)
    # imports draftroom.prep.scoring at module load time, so importing valuation.bonuses up
    # top here would run the valuation package's __init__ mid-way through this module's own
    # import and risk a circular import. valuation.bonuses itself has no dependency on
    # draftroom.config or draftroom.prep, so this is safe to resolve lazily, on first call.
    from draftroom.valuation.bonuses import expected_bonus

    stat_line: dict[str, Any] = dict(_as_stats_mapping(stats))
    if pos is not None:
        stat_line["pos"] = pos
    if games is not None:
        stat_line["games"] = games

    estimate = expected_bonus(stat_line, bonus_schedule, bonus_curves)
    return base + estimate.total


def _split_record(record: Any) -> tuple[str, Mapping[str, float]]:
    """Pull ``(player_id, stats)`` out of a StatLine-like record or a plain dict."""
    if isinstance(record, Mapping):
        if "player_id" not in record:
            raise KeyError("stat line dict has no 'player_id' key")
        player_id = str(record["player_id"])
        stats = record.get("stats")
        if stats is None:
            stats = {k: v for k, v in record.items() if k in CANONICAL_STATS}
        return player_id, stats

    player_id = getattr(record, "player_id", None)
    if player_id is None:
        raise AttributeError(f"stat line {record!r} has no player_id")
    stats = getattr(record, "stats", None)
    if stats is None:
        stats = {
            name: getattr(record, name)
            for name in CANONICAL_STATS
            if getattr(record, name, None) is not None
        }
    return str(player_id), stats
