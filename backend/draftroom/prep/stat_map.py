"""Yahoo ``stat_id`` -> canonical stat name.

Yahoo's scoring settings arrive as a list of ``stat_modifiers`` keyed by integer ``stat_id``.
Nothing downstream is allowed to see a raw ``stat_id``: this module is the only place the
translation happens, and per CLAUDE.md an id that appears in the league's modifiers but not
here is a **hard pipeline failure**, never a silent skip. A silently-dropped modifier is
exactly the failure mode that poisons every ranking downstream while looking fine.

===============================================================================
UNVERIFIED
===============================================================================
Every id in ``YAHOO_STAT_IDS`` below is seeded from the commonly documented Yahoo Fantasy
stat-id table and has **NOT** been confirmed against the live Yahoo API, because OAuth access
is not yet granted. Before the first real snapshot, fetch
``/fantasy/v2/game/nfl/stat_categories`` and diff it against this table. The scoring
reconciliation gate (CLAUDE.md gate #1: re-score 12 players' 2025 actuals within 1.0 pt of
Yahoo's own totals) is what will actually catch a wrong id here -- a mis-mapped id shifts a
whole position's points and will blow that gate loudly.
===============================================================================

Two ids in the documented seed set do not have a clean 1:1 canonical target, and the handling
is deliberate rather than convenient:

* **16 (2-Point Conversions)** -- Yahoo scores 2-pointers with a single combined modifier, but
  the canonical vocabulary splits them into ``pass_2pt`` / ``rush_2pt`` / ``rec_2pt``. Mapping
  it to any one of the three would undercount. It is therefore a *composite* id: one modifier
  fans out to all three canonical stats at the same per-unit value, which is arithmetically
  identical to Yahoo's combined stat.
* **15 (Return TD)** -- there is no canonical stat for return touchdowns at all, and no source
  in the pipeline projects them. It is listed as *unsupported*: it raises by default and can
  only be dropped by an explicit, conscious ``ignore_stat_ids`` on the caller.
"""

from __future__ import annotations

from typing import Mapping

__all__ = [
    "MissingStatIdError",
    "UnsupportedStatIdError",
    "YAHOO_STAT_IDS",
    "COMPOSITE_STAT_IDS",
    "UNSUPPORTED_STAT_IDS",
    "resolve",
    "resolve_modifier",
]


class MissingStatIdError(KeyError):
    """A Yahoo ``stat_id`` reached the pipeline with no entry in the stat map.

    Raised instead of dropping the modifier. See CLAUDE.md: "A Yahoo ``stat_id`` present in
    the league's modifiers but missing from the stat map is a hard pipeline failure, never a
    silent skip."
    """

    def __init__(self, stat_id: int, detail: str = "") -> None:
        self.stat_id = stat_id
        msg = (
            f"Yahoo stat_id {stat_id} is not in the stat map. Add it to "
            f"draftroom.prep.stat_map.YAHOO_STAT_IDS (verified against the live Yahoo "
            f"stat_categories endpoint) rather than dropping the modifier."
        )
        if detail:
            msg = f"{msg} {detail}"
        super().__init__(msg)

    def __str__(self) -> str:  # KeyError repr-quotes its arg; make messages readable.
        return self.args[0]


class UnsupportedStatIdError(MissingStatIdError):
    """A known Yahoo ``stat_id`` that has no representation in the canonical vocabulary.

    Distinct from :class:`MissingStatIdError` because the id is *recognised* -- we simply
    cannot score it honestly. Callers may opt out explicitly (``ignore_stat_ids``); they may
    not do so by accident.
    """

    def __init__(self, stat_id: int, reason: str) -> None:
        self.stat_id = stat_id
        self.reason = reason
        KeyError.__init__(
            self,
            f"Yahoo stat_id {stat_id} is recognised but has no canonical stat: {reason} "
            f"Pass it in ignore_stat_ids to drop it deliberately.",
        )


# --------------------------------------------------------------------------------------
# The seed map. ALL IDS UNVERIFIED -- see module docstring.
# Offensive stats only; the league has no kickers and no defenses (CLAUDE.md), so K/DST ids
# are intentionally absent and would raise if they ever showed up in the modifiers.
# --------------------------------------------------------------------------------------
YAHOO_STAT_IDS: Mapping[int, str] = {
    1: "pass_att",  # UNVERIFIED - Passing Attempts
    2: "pass_cmp",  # UNVERIFIED - Completions
    4: "pass_yd",  # UNVERIFIED - Passing Yards
    5: "pass_td",  # UNVERIFIED - Passing Touchdowns
    6: "pass_int",  # UNVERIFIED - Interceptions thrown
    8: "rush_att",  # UNVERIFIED - Rushing Attempts
    9: "rush_yd",  # UNVERIFIED - Rushing Yards
    10: "rush_td",  # UNVERIFIED - Rushing Touchdowns
    11: "rec",  # UNVERIFIED - Receptions
    12: "rec_yd",  # UNVERIFIED - Receiving Yards
    13: "rec_td",  # UNVERIFIED - Receiving Touchdowns
    18: "fum_lost",  # UNVERIFIED - Fumbles Lost
    # Targets are not part of the documented seed set and are not scored by any league we
    # know of; rec_tgt exists in the canonical vocabulary for modeling, not for scoring.
}

#: Ids whose single Yahoo modifier fans out to several canonical stats at the same per-unit
#: value. UNVERIFIED, same caveat as above.
COMPOSITE_STAT_IDS: Mapping[int, tuple[str, ...]] = {
    16: ("pass_2pt", "rush_2pt", "rec_2pt"),  # UNVERIFIED - Yahoo's combined "2-Point Conversions"
}

#: Ids we recognise but cannot express in the canonical vocabulary. Value is the reason.
UNSUPPORTED_STAT_IDS: Mapping[int, str] = {
    15: (  # UNVERIFIED - Return Touchdowns
        "return touchdowns have no canonical stat and no source in this pipeline projects "
        "them, so they cannot be scored."
    ),
}


def resolve(stat_id: int) -> str | None:
    """Return the single canonical stat name for ``stat_id``, else ``None``.

    ``None`` means "no single canonical name": the id is unknown, composite (16), or
    unsupported (15). Use :func:`resolve_modifier` when building scoring, because it
    distinguishes those cases and raises instead of returning a falsy value.
    """
    return YAHOO_STAT_IDS.get(int(stat_id))


def resolve_modifier(stat_id: int, stat_map: Mapping[int, str] | None = None) -> tuple[str, ...]:
    """Return every canonical stat a Yahoo modifier applies to. Raises rather than dropping.

    Args:
        stat_id: the Yahoo id from ``stat_modifiers``.
        stat_map: optional override of the 1:1 table (the league loader passes its own so the
            map stays an injectable input rather than a hidden global).

    Raises:
        UnsupportedStatIdError: recognised id with no canonical representation.
        MissingStatIdError: id absent from every table.
    """
    sid = int(stat_id)
    table = YAHOO_STAT_IDS if stat_map is None else stat_map
    name = table.get(sid)
    if name is not None:
        return (name,)
    composite = COMPOSITE_STAT_IDS.get(sid)
    if composite is not None:
        return composite
    reason = UNSUPPORTED_STAT_IDS.get(sid)
    if reason is not None:
        raise UnsupportedStatIdError(sid, reason)
    raise MissingStatIdError(sid)
