"""FantasySharks adapter: a FOURTH projection family, plus the two things it uniquely gives.

Free, no auth, no key, no rate limit published. Verified live 2026-08-20.

    GET https://www.fantasysharks.com/apps/bert/forecasts/projections.php
        ?League=-1&Position=<N>&scoring=18&Segment=<S>&uid=4

WHY THIS SOURCE EXISTS IN THE REPO AT ALL (plan docs/archive/PLAN_2026-08-20.md, "FantasySharks: add
it"). Not for a fourth vote on an average -- CLAUDE.md is blunt that a fourth *correlated*
source adds very little, and the ESPN/Clay episode (411 of 411 players identical, one source
counted as two) is the exact mistake a fourth source invites. Two specific gains justify it:

  1. **Targets.** ``rec_tgt`` came from ESPN alone, so the composite could not average it and
     the team-envelope validator could not cross-check it. FantasySharks publishes a ``Tgt``
     column for RB, WR and TE. Taking a stat from one source to two is a categorically
     different move from taking an average from three to four.
  2. **Projected counts of games clearing each yardage threshold** -- exactly the quantity
     ``valuation/bonuses.py`` models, which has no external reference of any kind. These are
     NOT canonical component stats, so they get their own typed output
     (:class:`ThresholdProjection`) and never enter a :class:`~draftroom.prep.schema.StatLine`.

WHAT IT DOES *NOT* GIVE -- read this before wiring it into anything
------------------------------------------------------------------
**There is no games column. On any of the four position tables.** Measured, not assumed: see
:func:`games_report`, which re-derives the answer from the parsed pages every time it runs
rather than trusting this docstring. So ``StatLine.games`` stays ``0.0`` here, which downstream
must read as "unknown, apply the positional availability prior" -- never as "projected for zero
games", exactly the FantasyPros situation (``prep/manual_csv.py``). It is worth being precise
about why this matters: Sleeper *does* publish a games column and it is the flat constant 18.0
for all 3,111 records (verified 2026-08-20) -- one more than this league's 17 weeks, and a
constant carries no player-specific durability information at all. A source with NO games
column and a source with a CONSTANT games column are equally uninformative about durability;
the difference is only that the constant looks like a forecast. FantasySharks at least does not
pretend.

Also absent, everywhere: any two-point-conversion column (so ``pass_2pt``/``rush_2pt``/
``rec_2pt`` are structurally unpublished, not zero), and rushing ATTEMPTS for WR/TE (they
publish ``Rsh Yds``/``Rsh TDs`` with no attempt count). Those absences are STRUCTURAL -- the
column does not exist -- which is the dangerous kind, because a structural zero averaged in as
a real number silently divides another source's real figure. :data:`PUBLISHED_STATS_BY_POS` is
this module's declaration of what it actually publishes, in the same shape
``valuation/composite.py`` already consumes for FantasyPros via ``SOURCE_PUBLISHES_BY_POS``.

THE `Segment` PARAMETER CHANGES EVERY YEAR AND IS NEVER HARDCODED
----------------------------------------------------------------
``Segment`` is FantasySharks' internal period id. 874 was the 2026 NFL season on 2026-08-20;
877 was "2026 Rest of Year", 878 "2026 Playoffs", 883-901 the individual weeks, 906 the *2027*
season. Nothing in the number says which is which, so a hardcoded constant would, one year
later, quietly serve a different season's projections -- plausible numbers, wrong year, and
nothing downstream could catch it. :func:`discover_segment` therefore reads the page's own
``<select name="Segment">`` and matches the option labelled exactly ``"<season> NFL Season"``,
raising :class:`SegmentNotFoundError` (with the full option list) if no such option exists.
Fail loudly beats silently pulling a prior year.

`Position=4` IS WIDE RECEIVER
-----------------------------
The position ids are not 1/2/3/4. Verified against the returned rows, not read off a table:
1 -> "Quarterback" (Josh Allen, Lamar Jackson at the top), 2 -> "Running Back" (Jahmyr Gibbs,
Bijan Robinson), **4 -> "Wide Receiver"** (Ja'Marr Chase, Puka Nacua), 5 -> "Tight End" (Trey
McBride, Brock Bowers). Id 3 is not a skill position in their numbering; using it for WR is the
trap this comment exists to prevent. Every fetch re-checks the `<select name="Position">`
option that comes back marked ``selected`` against the position we asked for
(:func:`_assert_position_label`), so a renumbering upstream fails the fetch instead of
mislabelling 187 receivers as something else.

COLUMN LAYOUT, AND THE DUPLICATE-HEADER TRAP
--------------------------------------------
Same trap ``prep/manual_csv.py`` documents for FantasyPros, in a worse form: the RB table
carries the header text ``">= 50 yd"`` and ``">= 100 yd"`` **twice each** -- once for rushing,
once for receiving. Header text is therefore useless for disambiguation, so every row is read
POSITIONALLY against the hardcoded per-position layout in :data:`POSITION_LAYOUTS`. The header
row is still parsed, and every one of the twelve-odd repeated header rows the page interleaves
into the table is compared against that layout -- not to derive the mapping, but to raise
:class:`ColumnLayoutError` the moment FantasySharks' real columns drift from what this module
assumes.

Other real artifacts in the served HTML, all handled rather than guessed at:
  - the header row repeats every 16 data rows,
  - a ``"Points Awarded"`` row follows every header repeat, carrying the SCORING preset's
    points-per-unit. It is skipped and never ingested: this adapter emits component stats, and
    a points row is the one thing CLAUDE.md forbids an adapter from passing on,
  - rookies are flagged with a ``<sup>R</sup>`` **tag** appended to the player link, not a
    letter in the name. Naive tag-stripping concatenates it and turns Fernando Mendoza into
    "MendozaR, Fernando"'s cousin "Mendoza, FernandoR" -- 55 of 516 rows on 2026-08-20. The
    parser reads the ``<sup>`` separately and exposes it as :attr:`FantasySharksRow.rookie`,
  - names are ``"Last, First"`` and are reversed here (``"Washington Jr., Mike"`` ->
    ``"Mike Washington Jr."``); ``prep/schema.normalize_name`` strips the suffix on the join,
  - team codes are FantasySharks' own three-letter set (GBP/JAC/KCC/LVR/NEP/NOS/SFO/TBB) and
    are mapped to the Sleeper spine's codes by :data:`FS_TEAM_MAP`; an unmapped code raises
    rather than silently producing a player on team ``""`` that can never join.

COLUMNS DELIBERATELY NOT MAPPED (a decision, not a silent drop)
--------------------------------------------------------------
Every column is accounted for in :data:`POSITION_LAYOUTS`; anything not mapped to a canonical
stat or a threshold carries an explicit ``note`` saying why. The list, with reasons:
  - the six ``0-9 / 10-19 / ... / 50+ <Pass|Rsh|Rec> TDs`` distance buckets -- they SUM to the
    total TD column already mapped (checked: Gibbs 7.8+2.9+0.4+0.4+1.4+1.3 = 14.2 vs Rsh TDs
    14.3), so mapping both would double-count,
  - ``RZ Tgt`` (red-zone targets) -- a subset of ``Tgt``; no canonical stat,
  - ``Sck`` (times sacked, QB) -- no canonical stat, and this league does not score it,
  - ``Kick Ret Yds`` -- return production has no canonical stat in this league (no K, no DST,
    no return scoring), the same treatment ``prep/espn_client.py`` gives ids 101-119. Worth
    recording that the column's VALUES also look wrong for its label: it is 0 for every
    high-usage receiver and rises monotonically as the projection falls (Chase 0, Iosivas 17,
    Tinsley 217, Colbie Young 601, Myles Price 1116), which is rank-shaped, not yardage-shaped.
    Unmapped either way, so the ambiguity costs nothing,
  - ``Opp`` -- a numeric column whose meaning the page never states. Measured what could be
    measured: it scales with the number of weeks in the segment (Chase 1.4 for Week 1 vs 24.2
    for the full season), it is bigger for QBs than for receivers, and it is **not** part of
    the ``Pts`` computation (reconstructing RB points from the mapped columns and the scoring
    row lands on 323.5 vs the published 323.4 for Gibbs without it). Unnamed and unverifiable
    means unmapped,
  - ``Pts`` -- FANTASY POINTS, discarded on every row. Points are computed only by applying
    this league's own modifiers to component stats (CLAUDE.md). Ingesting a foreign scoring
    preset's total would be a second, wrong scoring engine hiding inside a projection.

INDEPENDENCE IS NOT ASSUMED HERE
--------------------------------
Nothing in this module claims FantasySharks is independent of Sleeper, ESPN or FantasyPros.
``docs/FANTASYSHARKS.md`` measures it field by field against all three and prints the
verdict, and that check is a precondition for using this source in any variance, consensus or
disagreement measure -- see CLAUDE.md's ESPN/Clay warning. This adapter only fetches, parses
and maps.

NO NETWORK ON THE DRAFT PATH. Like every other prep adapter, the fetch functions here are
PREP-phase only. Draft night reads a frozen snapshot with the wifi off.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Iterable, Mapping, Sequence

from draftroom.prep.http import cache_raw, load_latest_raw, make_client, request_with_retry
from draftroom.prep.schema import CANONICAL_STATS, PlayerRef, StatLine

log = logging.getLogger("draftroom.prep.fantasysharks")

__all__ = [
    "SOURCE",
    "BASE_URL",
    "SCORING_PRESET",
    "POSITION_IDS",
    "POSITION_LABELS",
    "FS_TEAM_MAP",
    "POSITION_LAYOUTS",
    "PUBLISHED_STATS",
    "PUBLISHED_STATS_BY_POS",
    "THRESHOLDS_BY_POS",
    "GAMES_NOTE",
    "ColumnSpec",
    "FantasySharksError",
    "SegmentNotFoundError",
    "ColumnLayoutError",
    "FantasySharksRow",
    "ThresholdCount",
    "ThresholdProjection",
    "discover_segment",
    "fetch_projections",
    "load_cached",
    "pages_of",
    "parse_page",
    "parse_all",
    "to_statlines",
    "to_player_refs",
    "to_threshold_projections",
    "games_report",
    "bonus_tier_coverage",
]

SOURCE = "fantasysharks"

BASE_URL = (
    "https://www.fantasysharks.com/apps/bert/forecasts/projections.php"
    "?League=-1&Position={position}&scoring={scoring}&uid=4"
)

#: FantasySharks' scoring preset id. It only changes which points-per-unit values appear in the
#: page's "Points Awarded" row and in the ``Pts`` column -- BOTH of which this module discards.
#: It does not change a single component-stat projection (verified: the mapped columns are
#: identical across presets, only ``Pts`` moves). Kept explicit so the URL is reproducible.
SCORING_PRESET = 18

#: Position -> FantasySharks ``Position`` id. **4 is WR, not 3.** Verified against the returned
#: player names and the ``selected`` option label on every fetch -- see the module docstring.
POSITION_IDS: Mapping[str, int] = {"QB": 1, "RB": 2, "WR": 4, "TE": 5}

#: The label FantasySharks puts on each of those ids, checked on every fetch so a renumbering
#: upstream fails loudly instead of mislabelling a whole position group.
POSITION_LABELS: Mapping[str, str] = {
    "QB": "Quarterback",
    "RB": "Running Back",
    "WR": "Wide Receiver",
    "TE": "Tight End",
}

#: FantasySharks team code -> the Sleeper spine's code. All 32 codes observed live 2026-08-20
#: across the four tables (no free-agent/"FA" rows appeared). Nine differ from Sleeper's:
#: GBP/JAC/KCC/LVR/NEP/NOS/SFO/TBB and nothing else. An unmapped code raises rather than
#: yielding a blank team, because a blank team downgrades the crosswalk join from
#: name+team+pos to name+pos and can silently pick the wrong player of a shared name.
FS_TEAM_MAP: Mapping[str, str] = {
    "ARI": "ARI", "ATL": "ATL", "BAL": "BAL", "BUF": "BUF", "CAR": "CAR", "CHI": "CHI",
    "CIN": "CIN", "CLE": "CLE", "DAL": "DAL", "DEN": "DEN", "DET": "DET",
    "GBP": "GB",   # <- differs
    "HOU": "HOU", "IND": "IND",
    "JAC": "JAX",  # <- differs
    "KCC": "KC",   # <- differs
    "LAC": "LAC", "LAR": "LAR",
    "LVR": "LV",   # <- differs
    "MIA": "MIA", "MIN": "MIN",
    "NEP": "NE",   # <- differs
    "NOS": "NO",   # <- differs
    "NYG": "NYG", "NYJ": "NYJ", "PHI": "PHI", "PIT": "PIT", "SEA": "SEA",
    "SFO": "SF",   # <- differs
    "TBB": "TB",   # <- differs
    "TEN": "TEN", "WAS": "WAS",
}

GAMES_NOTE = (
    "FantasySharks publishes NO games column on any of its four position tables (QB/RB/WR/TE) "
    "-- re-measured from the parsed pages by games_report(), not asserted. StatLine.games is "
    "therefore 0.0, meaning UNKNOWN: downstream must apply the positional availability prior, "
    "never read it as a projection of zero games. For contrast, and because the distinction is "
    "the whole point: Sleeper DOES publish a games column and it is the flat constant 18.0 for "
    "all 3,111 records -- one distinct value, and one MORE than this league's 17 weeks. A "
    "constant is not a durability forecast. So FantasySharks contributes nothing to a games "
    "blend, which is the same contribution Sleeper's constant should make."
)

_PLAYER_ID_RE = re.compile(r"playerpage\.php\?id=(\d+)")
_GAMES_HEADER_RE = re.compile(r"\b(games?|gp|g)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FantasySharksError(RuntimeError):
    """Base class for every failure this adapter raises. Never a silent degrade."""


class SegmentNotFoundError(FantasySharksError):
    """The page's own ``<select name="Segment">`` has no option for the requested season.

    Deliberately fatal. The alternative -- fall back to whatever segment the site happens to
    have selected -- serves a different season's projections that look completely plausible.
    """


class ColumnLayoutError(FantasySharksError):
    """The served table's header does not match this module's hardcoded per-position layout.

    Real column drift, as distinct from the artifact rows (repeated headers, the "Points
    Awarded" row, footnotes) which are skipped. Mirrors ``manual_csv.UnmappedColumnError``:
    a layout change must be seen and re-verified by a human, never absorbed.
    """


# ---------------------------------------------------------------------------
# Column layouts -- positional, because the header TEXT repeats within a row
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnSpec:
    """One column of one position's table.

    Exactly one of ``canonical`` / ``threshold`` is set, or neither -- and when it is neither,
    ``note`` says why the column is not mapped. That invariant is asserted at import time, so
    a column can never be dropped without a written reason.
    """

    header: str
    canonical: str | None = None
    #: ``(canonical yardage stat, threshold in yards)`` for a per-game threshold-count column.
    threshold: tuple[str, float] | None = None
    note: str = ""
    #: Parsed (so a non-numeric value still fails the row's validation) but never retained --
    #: reserved for the fantasy-points column. Everything else unmapped lands in
    #: :attr:`FantasySharksRow.extras` for audit.
    discard: bool = False


_C = ColumnSpec

# Reason strings, written once so the same decision reads identically on every position.
_N_RANK = "row number within this position's table; not a projection"
_N_NAME = "player identity, read from the <a> link (also carries the FantasySharks player id)"
_N_TEAM = "NFL team; mapped through FS_TEAM_MAP, not a stat"
_N_TD_BUCKET = "TD-by-distance bucket; these SUM to the total TD column already mapped, so mapping both would double-count"
_N_RZ_TGT = "red-zone targets: a subset of Tgt, no canonical stat"
_N_SACK = "times sacked: no canonical stat, and this league does not score it"
_N_KRET = (
    "return yardage has no canonical stat in this league (no K, no DST, no return scoring). "
    "The values also look rank-shaped rather than yardage-shaped -- 0 for every high-usage "
    "receiver, rising as the projection falls -- so the label itself is doubtful. Unmapped "
    "either way"
)
_N_OPP = (
    "undocumented numeric column. Measured: scales with the segment's week count, larger for "
    "QBs than receivers, and NOT part of the published Pts total. Unnamed and unverifiable "
    "means unmapped"
)
_N_PTS = (
    "FANTASY POINTS under FantasySharks' own scoring preset. Discarded on every row: points "
    "are computed only by applying this league's modifiers to component stats (CLAUDE.md). "
    "Ingesting a foreign preset's total would be a second, wrong scoring engine"
)

# WR's and TE's served tables are column-for-column identical (verified 2026-08-20), so the
# layout is defined once and both keys point at it -- they cannot drift apart in this file.
_RECEIVER_LAYOUT: tuple[ColumnSpec, ...] = (
    _C("#", note=_N_RANK),
    _C("Player", note=_N_NAME),
    _C("Tm", note=_N_TEAM),
    _C("Tgt", canonical="rec_tgt"),
    _C("RZ Tgt", note=_N_RZ_TGT),
    _C("Rec", canonical="rec"),
    _C("Rec Yds", canonical="rec_yd"),
    _C("Rec TDs", canonical="rec_td"),
    _C("0-9 Rec TDs", note=_N_TD_BUCKET),
    _C("10-19 Rec TDs", note=_N_TD_BUCKET),
    _C("20-29 Rec TDs", note=_N_TD_BUCKET),
    _C("30-39 Rec TDs", note=_N_TD_BUCKET),
    _C("40-49 Rec TDs", note=_N_TD_BUCKET),
    _C("50+ Rec TDs", note=_N_TD_BUCKET),
    _C(">= 50 yd", threshold=("rec_yd", 50.0)),
    _C(">= 100 yd", threshold=("rec_yd", 100.0)),
    _C(">= 150 yd", threshold=("rec_yd", 150.0)),
    _C(">= 200 yd", threshold=("rec_yd", 200.0)),
    # No rushing ATTEMPTS column for WR/TE -- yards and TDs only. rush_att is therefore
    # structurally unpublished at these positions, which is why PUBLISHED_STATS_BY_POS exists.
    _C("Rsh Yds", canonical="rush_yd"),
    _C("Rsh TDs", canonical="rush_td"),
    _C("Kick Ret Yds", note=_N_KRET),
    _C("Fum", canonical="fum_lost"),
    _C("Opp", note=_N_OPP),
    _C("Pts", note=_N_PTS, discard=True),
)

#: Verified live 2026-08-20 against the real served tables. Order is the served column order and
#: is load-bearing: rows are read positionally, never by header text (the RB table repeats
#: ">= 50 yd" and ">= 100 yd" for both rushing and receiving).
POSITION_LAYOUTS: Mapping[str, tuple[ColumnSpec, ...]] = {
    "QB": (
        _C("#", note=_N_RANK),
        _C("Player", note=_N_NAME),
        _C("Tm", note=_N_TEAM),
        _C("Att", canonical="pass_att"),
        _C("Comp", canonical="pass_cmp"),
        _C("Pass Yds", canonical="pass_yd"),
        _C("Pass TDs", canonical="pass_td"),
        _C("0-9 Pass TDs", note=_N_TD_BUCKET),
        _C("10-19 Pass TDs", note=_N_TD_BUCKET),
        _C("20-29 Pass TDs", note=_N_TD_BUCKET),
        _C("30-39 Pass TDs", note=_N_TD_BUCKET),
        _C("40-49 Pass TDs", note=_N_TD_BUCKET),
        _C("50+ Pass TDs", note=_N_TD_BUCKET),
        _C("Int", canonical="pass_int"),
        _C("Sck", note=_N_SACK),
        _C(">= 250 yd", threshold=("pass_yd", 250.0)),
        _C(">= 300 yd", threshold=("pass_yd", 300.0)),
        _C(">= 350 yd", threshold=("pass_yd", 350.0)),
        _C("Rush", canonical="rush_att"),
        _C("Rsh Yds", canonical="rush_yd"),
        _C("Rsh TDs", canonical="rush_td"),
        _C("Fum", canonical="fum_lost"),
        _C("Opp", note=_N_OPP),
        _C("Pts", note=_N_PTS, discard=True),
    ),
    "RB": (
        _C("#", note=_N_RANK),
        _C("Player", note=_N_NAME),
        _C("Tm", note=_N_TEAM),
        _C("Rush", canonical="rush_att"),
        _C("Rsh Yds", canonical="rush_yd"),
        _C("Rsh TDs", canonical="rush_td"),
        _C("0-9 Rsh TDs", note=_N_TD_BUCKET),
        _C("10-19 Rsh TDs", note=_N_TD_BUCKET),
        _C("20-29 Rsh TDs", note=_N_TD_BUCKET),
        _C("30-39 Rsh TDs", note=_N_TD_BUCKET),
        _C("40-49 Rsh TDs", note=_N_TD_BUCKET),
        _C("50+ Rsh TDs", note=_N_TD_BUCKET),
        _C(">= 50 yd", threshold=("rush_yd", 50.0)),
        _C(">= 100 yd", threshold=("rush_yd", 100.0)),
        _C("Tgt", canonical="rec_tgt"),
        _C("RZ Tgt", note=_N_RZ_TGT),
        _C("Rec", canonical="rec"),
        _C("Rec Yds", canonical="rec_yd"),
        _C("Rec TDs", canonical="rec_td"),
        # SAME header text as columns 12/13 above. This is the duplicate-header trap; the only
        # thing distinguishing rushing from receiving here is the position in the row.
        _C(">= 50 yd", threshold=("rec_yd", 50.0)),
        _C(">= 100 yd", threshold=("rec_yd", 100.0)),
        _C("Kick Ret Yds", note=_N_KRET),
        _C("Fum", canonical="fum_lost"),
        _C("Opp", note=_N_OPP),
        _C("Pts", note=_N_PTS, discard=True),
    ),
    "WR": _RECEIVER_LAYOUT,
    "TE": _RECEIVER_LAYOUT,
}

# Import-time invariant: no column may be silently dropped. A spec with neither a canonical
# stat nor a threshold MUST carry a written reason, and a spec may never claim both.
for _pos, _layout in POSITION_LAYOUTS.items():
    for _idx, _spec in enumerate(_layout):
        if _spec.canonical is not None and _spec.threshold is not None:
            raise AssertionError(
                f"{_pos} column {_idx} ({_spec.header!r}) claims both a canonical stat and a "
                "threshold; it can only be one."
            )
        if _spec.canonical is None and _spec.threshold is None and not _spec.note:
            raise AssertionError(
                f"{_pos} column {_idx} ({_spec.header!r}) is unmapped with no reason. An "
                "unmapped column is a documented decision, never a silent drop (CLAUDE.md)."
            )
        if _spec.canonical is not None and _spec.canonical not in CANONICAL_STATS:
            raise AssertionError(
                f"{_pos} column {_idx} ({_spec.header!r}) maps to {_spec.canonical!r}, which is "
                f"not in CANONICAL_STATS {CANONICAL_STATS}"
            )
        if _spec.threshold is not None and _spec.threshold[0] not in CANONICAL_STATS:
            raise AssertionError(
                f"{_pos} column {_idx} ({_spec.header!r}) thresholds on "
                f"{_spec.threshold[0]!r}, which is not in CANONICAL_STATS"
            )

#: Canonical stats this source publishes, per position. Same shape ``valuation/composite.py``
#: consumes for FantasyPros (``SOURCE_PUBLISHES_BY_POS``), so wiring this in later is a table
#: entry rather than new logic. ``games`` is absent from every position -- there is no column.
PUBLISHED_STATS_BY_POS: Mapping[str, frozenset[str]] = {
    pos: frozenset(spec.canonical for spec in layout if spec.canonical is not None)
    for pos, layout in POSITION_LAYOUTS.items()
}

#: Union over positions. Useful as the coarse answer; prefer the per-position table, because
#: the union claims ``rush_att`` (true for QB and RB, structurally false for WR and TE).
PUBLISHED_STATS: frozenset[str] = frozenset().union(*PUBLISHED_STATS_BY_POS.values())

#: ``(canonical stat, threshold yards)`` pairs each position's table publishes a game count for.
THRESHOLDS_BY_POS: Mapping[str, tuple[tuple[str, float], ...]] = {
    pos: tuple(spec.threshold for spec in layout if spec.threshold is not None)
    for pos, layout in POSITION_LAYOUTS.items()
}


# ---------------------------------------------------------------------------
# Typed outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThresholdCount:
    """Projected number of GAMES in which a player clears ``threshold`` yards of ``stat``.

    Not a canonical component stat, which is exactly why this lives outside ``StatLine``:
    ``StatLine`` is the vocabulary CLAUDE.md fixes and ``prep/schema.py`` asserts, and a game
    count is not in it. This is the external reference ``valuation/bonuses.py`` has never had.
    """

    stat: str
    threshold: float
    games: float


@dataclass(frozen=True)
class ThresholdProjection:
    """Every threshold-count one player's row publishes, kept beside their identity."""

    source_key: str
    name: str
    pos: str
    team: str
    counts: tuple[ThresholdCount, ...] = ()

    def get(self, stat: str, threshold: float) -> float | None:
        """Games clearing ``threshold`` yards of ``stat``, or ``None`` if not published.

        ``None``, never 0.0. FantasySharks publishes no rushing 150/200 or passing 400/500
        column at all, and returning 0.0 for those would assert that the player never clears
        them -- which is a projection this source never made.
        """
        for c in self.counts:
            if c.stat == stat and c.threshold == threshold:
                return c.games
        return None


@dataclass(frozen=True)
class FantasySharksRow:
    """One parsed player row: identity, canonical statline, thresholds, and the audit trail."""

    source_key: str
    name: str
    pos: str
    team: str
    fs_team: str
    rank: int
    rookie: bool
    stats: StatLine
    thresholds: tuple[ThresholdCount, ...] = ()
    #: Header -> value for every numeric column deliberately NOT mapped (see the ``note`` on
    #: each :class:`ColumnSpec`). ``Pts`` is the one exclusion: fantasy points never leave the
    #: parser. Kept so an unmapped column can be inspected later without a re-fetch.
    extras: Mapping[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# HTML parsing (stdlib only -- this repo has no bs4/lxml, and does not need one here)
# ---------------------------------------------------------------------------


@dataclass
class _RawRow:
    cells: list[str]
    player_id: str | None = None
    rookie: bool = False


class _TableRowParser(HTMLParser):
    """Extract the rows of ``<table id="toolData">`` as plain cell text.

    Written against the real served markup rather than a general HTML model. Two details it
    exists to get right: the ``<sup>R</sup>`` rookie marker must not be concatenated into the
    player's name, and the player's FantasySharks id must be recovered from the row's
    ``playerpage.php?id=`` link (it is the only stable per-player key this source offers --
    FantasySharks ids appear in no ID crosswalk, so this is a source_key, not a join key).
    """

    def __init__(self, table_id: str = "toolData") -> None:
        super().__init__(convert_charrefs=True)
        self._table_id = table_id
        self._depth = 0
        self.rows: list[_RawRow] = []
        self._row: _RawRow | None = None
        self._cell: list[str] | None = None
        self._sup: list[str] | None = None

    # -- structure ------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "table":
            if self._depth or a.get("id") == self._table_id:
                self._depth += 1
            return
        if not self._depth:
            return
        if tag == "tr":
            self._row = _RawRow(cells=[])
        elif tag in ("td", "th"):
            if self._row is None:  # a stray cell outside any row -- ignore it
                return
            self._cell = []
        elif tag == "sup" and self._cell is not None:
            self._sup = []
        elif tag == "a" and self._row is not None and self._row.player_id is None:
            m = _PLAYER_ID_RE.search(a.get("href") or "")
            if m:
                self._row.player_id = m.group(1)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._depth:
                self._depth -= 1
            return
        if not self._depth:
            return
        if tag == "sup" and self._sup is not None:
            if "".join(self._sup).strip().upper() == "R":
                if self._row is not None:
                    self._row.rookie = True
            self._sup = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.cells.append(_clean_cell("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row.cells:
                self.rows.append(self._row)
            self._row = None
            self._cell = None

    def handle_data(self, data: str) -> None:
        if not self._depth:
            return
        if self._sup is not None:
            self._sup.append(data)
        elif self._cell is not None:
            self._cell.append(data)


class _SegmentSelectParser(HTMLParser):
    """Extract ``[(value, label)]`` from ``<select name="Segment">``."""

    def __init__(self, select_name: str = "Segment") -> None:
        super().__init__(convert_charrefs=True)
        self._name = select_name
        self._in_select = False
        self._value: str | None = None
        self._buf: list[str] | None = None
        self.options: list[tuple[str, str]] = []
        self.selected: str | None = None
        self._selected_flag = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "select":
            self._in_select = (a.get("name") or "").strip() == self._name
        elif tag == "option" and self._in_select:
            self._value = (a.get("value") or "").strip()
            self._buf = []
            self._selected_flag = "selected" in a

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._in_select = False
        elif tag == "option" and self._in_select and self._buf is not None:
            label = _clean_cell("".join(self._buf))
            self.options.append((self._value or "", label))
            if self._selected_flag:
                self.selected = self._value or ""
            self._value, self._buf, self._selected_flag = None, None, False

    def handle_data(self, data: str) -> None:
        if self._buf is not None:
            self._buf.append(data)


def _clean_cell(text: str) -> str:
    """Collapse whitespace, including the non-breaking spaces the page pads labels with."""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _to_float(value: str) -> float:
    """Parse a served cell into a float. Empty means 0.0; anything else non-numeric raises."""
    s = value.replace(",", "").replace("%", "").strip()
    if not s or s == "-":
        return 0.0
    return float(s)


def _display_name(last_first: str) -> str:
    """``"Washington Jr., Mike"`` -> ``"Mike Washington Jr."``.

    Splits on the FIRST comma only, so a generational suffix that lives on the last-name side
    ("Gore Jr., Frank") survives into the right place. A row with no comma is returned as-is
    rather than guessed at.
    """
    last, sep, first = last_first.partition(",")
    if not sep:
        return last_first.strip()
    return f"{first.strip()} {last.strip()}".strip()


# ---------------------------------------------------------------------------
# Segment discovery -- never hardcoded
# ---------------------------------------------------------------------------


def discover_segment(html: str, season: int) -> tuple[int, str]:
    """``(segment_id, label)`` for ``"<season> NFL Season"``, read off the page's own select.

    The match is on the LABEL, exactly, because the id carries no meaning. "2026 Rest of Year",
    "2026 Playoffs" and the 19 individual weeks all mention the season too, so a substring
    match would happily pick a partial-season projection.

    Raises:
        SegmentNotFoundError: no option is labelled for that season. Lists every option it did
            find, so the fix is one look rather than an investigation.
    """
    parser = _SegmentSelectParser()
    parser.feed(html)
    if not parser.options:
        raise SegmentNotFoundError(
            "FantasySharks page carried no <select name=\"Segment\"> at all. The page shape "
            "changed -- inspect the real response before guessing a segment id. Never fall "
            "back to a hardcoded number; it would serve a different season silently."
        )

    want = f"{season} NFL Season"
    for value, label in parser.options:
        if label == want:
            try:
                return int(value), label
            except ValueError as exc:
                raise SegmentNotFoundError(
                    f"FantasySharks segment option {label!r} has a non-numeric value {value!r}"
                ) from exc

    raise SegmentNotFoundError(
        f"no FantasySharks segment labelled {want!r}. Options served were: "
        + ", ".join(f"{v}={l!r}" for v, l in parser.options)
        + ". Refusing to fall back to the selected option -- that is how a prior season's "
        "projections get served as this season's."
    )


def _assert_position_label(html: str, position: str) -> None:
    """Confirm the ``selected`` option of ``<select name="Position">`` is the position we asked
    for. Cheap insurance against the ``Position=4`` trap: if FantasySharks ever renumbers, the
    fetch fails instead of returning 187 receivers labelled as something else."""
    parser = _SegmentSelectParser(select_name="Position")
    parser.feed(html)
    if not parser.options:
        raise ColumnLayoutError(
            f"FantasySharks {position} page carried no <select name=\"Position\">; cannot "
            "confirm the position id still means what this module thinks it means."
        )
    want_id = str(POSITION_IDS[position])
    want_label = POSITION_LABELS[position]
    labels = dict(parser.options)
    got = labels.get(want_id)
    if got != want_label:
        raise ColumnLayoutError(
            f"FantasySharks Position={want_id} is now labelled {got!r}, not {want_label!r}. "
            "The position ids have been renumbered -- re-derive POSITION_IDS against the real "
            "returned player names (id 4 was WR, not 3) before trusting any of this data."
        )
    if parser.selected is not None and parser.selected != want_id:
        raise ColumnLayoutError(
            f"asked FantasySharks for Position={want_id} ({want_label}) but the page came back "
            f"with Position={parser.selected} selected."
        )


# ---------------------------------------------------------------------------
# Fetch + cache
# ---------------------------------------------------------------------------


def fetch_projections(
    season: int,
    *,
    positions: Sequence[str] | None = None,
    scoring: int = SCORING_PRESET,
    segment: int | None = None,
) -> dict:
    """Fetch every position table for ``season`` and cache one combined raw payload.

    Flow, and the reason for it: the ``Segment`` id is not knowable a priori, so the first
    request is made WITHOUT a Segment parameter (the site then serves whatever it currently
    defaults to) purely to read the segment ``<select>``. The discovered id is compared with the
    one that page actually served; if they match, that HTML is reused rather than re-fetched.
    Four position pages, so four or five requests total.

    ``segment`` may be passed to pin a specific period (a caller reproducing an old pull), but
    it is still validated against the select, so a stale pin fails rather than silently working.

    Caches ONE json blob to ``data/raw/fantasysharks/<UTC timestamp>.json`` holding the season,
    the resolved segment and its label, the scoring preset, each URL used, and each page's full
    HTML -- so a later parse never needs the network and nothing about the response is lost.
    ``data/raw/fantasysharks/`` is a NEW directory, so it cannot move what ``load_latest_raw()``
    resolves to for any existing source.

    Raises:
        SegmentNotFoundError: the requested season has no segment option.
        ColumnLayoutError: a position id no longer means what this module thinks.
        httpx.HTTPStatusError: a non-2xx response, after the shared client's retries.
    """
    wanted = list(positions) if positions is not None else list(POSITION_IDS)
    unknown = [p for p in wanted if p not in POSITION_IDS]
    if unknown:
        raise ValueError(
            f"unknown FantasySharks position(s) {unknown}; known: {sorted(POSITION_IDS)}"
        )

    pages: dict[str, dict[str, str]] = {}
    with make_client() as client:
        bootstrap_pos = wanted[0]
        bootstrap_url = BASE_URL.format(position=POSITION_IDS[bootstrap_pos], scoring=scoring)
        resp = request_with_retry(client, "GET", bootstrap_url)
        resp.raise_for_status()
        bootstrap_html = resp.text

        resolved_segment, segment_label = discover_segment(bootstrap_html, season)
        if segment is not None and segment != resolved_segment:
            raise SegmentNotFoundError(
                f"caller pinned Segment={segment} but FantasySharks now labels "
                f"{resolved_segment} as {segment_label!r} for season {season}. Segment ids "
                "change every year; do not pin one across seasons."
            )

        bootstrap_selected = _SegmentSelectParser()
        bootstrap_selected.feed(bootstrap_html)
        served_segment = bootstrap_selected.selected

        for pos in wanted:
            url = (
                BASE_URL.format(position=POSITION_IDS[pos], scoring=scoring)
                + f"&Segment={resolved_segment}"
            )
            if pos == bootstrap_pos and served_segment == str(resolved_segment):
                html = bootstrap_html  # the bootstrap page already IS this page
            else:
                r = request_with_retry(client, "GET", url)
                r.raise_for_status()
                html = r.text
            _assert_position_label(html, pos)
            pages[pos] = {"url": url, "html": html}

    payload = {
        "source": SOURCE,
        "season": season,
        "segment": resolved_segment,
        "segment_label": segment_label,
        "scoring": scoring,
        "fetched_utc": datetime.now(timezone.utc).isoformat(),
        "positions": pages,
    }
    cache_raw(SOURCE, payload, suffix="json")
    return payload


def load_cached() -> dict:
    """Read back the newest cached FantasySharks payload. Raises FileNotFoundError if none."""
    raw = load_latest_raw(SOURCE)
    if not isinstance(raw, dict) or "positions" not in raw:
        raise FantasySharksError(
            "cached FantasySharks payload is not the expected shape (a dict with a 'positions' "
            f"map); got {type(raw).__name__}. Do not guess -- inspect the cached file."
        )
    return raw


def pages_of(payload: Mapping) -> dict[str, str]:
    """``{position: html}`` out of a cached payload."""
    out: dict[str, str] = {}
    for pos, entry in (payload.get("positions") or {}).items():
        html = entry.get("html") if isinstance(entry, Mapping) else entry
        if isinstance(html, str):
            out[str(pos).upper()] = html
    return out


# ---------------------------------------------------------------------------
# Parsing one page into typed rows
# ---------------------------------------------------------------------------


def parse_page(html: str, position: str) -> list[FantasySharksRow]:
    """Parse one position's served table into rows, mapped into CANONICAL_STATS.

    Every row is read POSITIONALLY against ``POSITION_LAYOUTS[position]``. The header row (and
    each of its ~12 mid-table repeats) is checked against that layout and raises
    :class:`ColumnLayoutError` on any mismatch -- the header is a drift detector here, never
    the source of the mapping, because the RB table repeats header text within a single row.

    Artifact rows that are skipped, with a log line, rather than failing the load:
      - the repeated header rows (verified identical to the layout first),
      - the ``"Points Awarded"`` scoring row after each header repeat -- never ingested,
      - any row whose cell count differs from the layout, or whose first cell is not a rank
        number, or which carries non-numeric text in a stat column.

    Raises:
        ColumnLayoutError: the table is missing, or a header row does not match the layout.
    """
    pos = position.strip().upper()
    layout = POSITION_LAYOUTS.get(pos)
    if layout is None:
        raise ValueError(f"unknown FantasySharks position {position!r}")

    parser = _TableRowParser()
    parser.feed(html)
    if not parser.rows:
        raise ColumnLayoutError(
            f"FantasySharks {pos} page contained no <table id=\"toolData\"> rows. The page "
            "shape changed, or the request was rejected -- inspect the real response."
        )

    expected = [spec.header for spec in layout]
    saw_header = False
    out: list[FantasySharksRow] = []
    skipped = 0

    for row_no, raw in enumerate(parser.rows, start=1):
        cells = raw.cells

        # A header row: identified by its first cell, not by position (they repeat mid-table).
        if cells and cells[0] == "#":
            if cells != expected:
                raise ColumnLayoutError(
                    f"FantasySharks {pos} table header drifted from the layout this module "
                    f"assumes.\n  served:   {cells}\n  expected: {expected}\n"
                    "Re-derive POSITION_LAYOUTS against the real page (and re-check the "
                    "threshold columns against data/league_manual.yaml's scoring_bonuses) "
                    "before using any of this data. Do not guess the new order."
                )
            saw_header = True
            continue

        # The scoring row. Deliberately never ingested -- it is points-per-unit under
        # FantasySharks' own preset, and this adapter emits component stats only.
        if cells and cells[0] == "Points Awarded":
            continue

        if not cells or not cells[0].isdigit():
            skipped += 1
            log.debug("%s row %d: not a player row, skipping: %r", pos, row_no, cells[:4])
            continue

        if len(cells) != len(layout):
            skipped += 1
            log.warning(
                "FantasySharks %s row %d has %d cells, layout expects %d -- skipping as a "
                "page artifact (the header already matched, so this is not column drift): %r",
                pos, row_no, len(cells), len(layout), cells[:6],
            )
            continue

        fs_team = cells[2].strip().upper()
        team = FS_TEAM_MAP.get(fs_team)
        if team is None:
            raise ColumnLayoutError(
                f"FantasySharks {pos} row {row_no} carries unmapped team code {fs_team!r} "
                f"(player {cells[1]!r}). Add it to FS_TEAM_MAP after confirming what it means "
                "-- a blank team silently downgrades the crosswalk join to name+position and "
                "can pick the wrong player of a shared name."
            )

        try:
            kwargs: dict[str, float] = {}
            thresholds: list[ThresholdCount] = []
            extras: dict[str, float] = {}
            for spec, value in zip(layout[3:], cells[3:]):
                if spec.canonical is not None:
                    kwargs[spec.canonical] = _to_float(value)
                elif spec.threshold is not None:
                    stat, thr = spec.threshold
                    thresholds.append(
                        ThresholdCount(stat=stat, threshold=thr, games=_to_float(value))
                    )
                elif spec.discard:
                    _to_float(value)  # validated, then dropped: points never leave the parser
                else:
                    extras[spec.header] = _to_float(value)
        except ValueError:
            skipped += 1
            log.warning(
                "FantasySharks %s row %d has a non-numeric stat value (footnote row at the "
                "right column count?) -- skipping: %r", pos, row_no, cells[:6],
            )
            continue

        name = _display_name(cells[1])
        if not name:
            skipped += 1
            continue

        source_key = raw.player_id or f"{name}|{team}|{pos}"
        out.append(
            FantasySharksRow(
                source_key=source_key,
                name=name,
                pos=pos,
                team=team,
                fs_team=fs_team,
                rank=int(cells[0]),
                rookie=raw.rookie,
                stats=StatLine(**kwargs),
                thresholds=tuple(thresholds),
                extras=extras,
            )
        )

    if not saw_header:
        raise ColumnLayoutError(
            f"FantasySharks {pos} table had rows but no header row to validate the layout "
            "against. Refusing to read a positional layout off an unverified table."
        )
    if skipped:
        log.info("FantasySharks %s: parsed %d players, skipped %d non-player rows",
                 pos, len(out), skipped)
    return out


def parse_all(pages: Mapping[str, str]) -> list[FantasySharksRow]:
    """Parse every position page. Order is QB, RB, WR, TE, then anything else, then by rank."""
    order = {p: i for i, p in enumerate(POSITION_IDS)}
    rows: list[FantasySharksRow] = []
    for pos in sorted(pages, key=lambda p: (order.get(p.upper(), 99), p)):
        rows.extend(parse_page(pages[pos], pos))
    return rows


# ---------------------------------------------------------------------------
# Adapter outputs
# ---------------------------------------------------------------------------


def to_statlines(rows: Iterable[FantasySharksRow]) -> dict[str, StatLine]:
    """``{source_key: StatLine}``. Component stats only -- ``games`` is 0.0 (see GAMES_NOTE)."""
    return {r.source_key: r.stats for r in rows}


def to_player_refs(rows: Iterable[FantasySharksRow]) -> dict[str, PlayerRef]:
    """``{source_key: PlayerRef}``, keyed identically to :func:`to_statlines`.

    Mirrors ``espn_client.to_player_refs``: the crosswalk's resolver hooks need name/team/pos,
    which a ``StatLine`` deliberately discards.
    """
    return {
        r.source_key: PlayerRef(
            name=r.name, pos=r.pos, team=r.team, source_id=r.source_key, source=SOURCE
        )
        for r in rows
    }


def to_threshold_projections(
    rows: Iterable[FantasySharksRow],
) -> dict[str, ThresholdProjection]:
    """``{source_key: ThresholdProjection}`` -- the per-game yardage-threshold game counts.

    Separate from :func:`to_statlines` on purpose: these are not canonical component stats and
    must never be blended as if they were. They are the first external reference for what
    ``valuation/bonuses.py`` estimates from fitted hit-rate curves.
    """
    return {
        r.source_key: ThresholdProjection(
            source_key=r.source_key, name=r.name, pos=r.pos, team=r.team, counts=r.thresholds
        )
        for r in rows
    }


# ---------------------------------------------------------------------------
# Measurements this source has to be honest about
# ---------------------------------------------------------------------------


def games_report(pages: Mapping[str, str], rows: Iterable[FantasySharksRow] | None = None) -> dict:
    """Measure -- do not assert -- what FantasySharks publishes for ``games``.

    Re-derives the answer from the served pages every run, because "does this source have a
    games column" is exactly the kind of fact that rots silently. Returns:

      ``positions``            per position: the parsed header list and any header matching a
                               games-shaped word (``games``/``gp``/``g``),
      ``games_columns``        every (position, header) hit. Empty means no column exists,
      ``distinct_values``      how many DISTINCT positive ``games`` values the parsed statlines
                               take. 0 = the source says nothing; 1 = a blanket constant
                               masquerading as a forecast (Sleeper's 18.0); >1 = real
                               per-player variation,
      ``values``              those distinct values, sorted,
      ``note``                :data:`GAMES_NOTE`.
    """
    parsed_rows = list(rows) if rows is not None else parse_all(pages)

    per_pos: dict[str, dict] = {}
    hits: list[tuple[str, str]] = []
    for pos, html in pages.items():
        pos_u = pos.strip().upper()
        parser = _TableRowParser()
        parser.feed(html)
        header: list[str] = []
        for raw in parser.rows:
            if raw.cells and raw.cells[0] == "#":
                header = raw.cells
                break
        matches = [h for h in header if _GAMES_HEADER_RE.fullmatch(h.strip())]
        per_pos[pos_u] = {"header": header, "games_headers": matches}
        hits.extend((pos_u, h) for h in matches)

    values = sorted({round(float(r.stats.games), 6) for r in parsed_rows if r.stats.games > 0})
    return {
        "positions": per_pos,
        "games_columns": hits,
        "distinct_values": len(values),
        "values": values,
        "players_parsed": len(parsed_rows),
        "note": GAMES_NOTE,
    }


def bonus_tier_coverage(
    schedule: Mapping[str, Sequence[Mapping[str, float]]],
) -> dict[str, dict]:
    """Which of the LEAGUE's bonus tiers this source publishes a game count for.

    ``schedule`` is the league's own ``scoring_bonuses`` block, passed IN rather than imported:
    a prep adapter has no business reaching into ``valuation``, and the league's schedule is
    read from ``data/league_manual.yaml`` by ``valuation/bonuses.load_bonus_schedule``, which
    is the single source of truth for it.

    Returns, per canonical bonus stat: the league's tiers, which positions publish a count for
    each tier, which tiers nothing covers, and the extra thresholds this source publishes that
    the league does not pay (250/350 passing, 50 rushing/receiving) -- those are not waste,
    they constrain the shape of the same per-game distribution the bonus model is estimating.
    """
    out: dict[str, dict] = {}
    published: dict[str, dict[float, list[str]]] = {}
    for pos, pairs in THRESHOLDS_BY_POS.items():
        for stat, thr in pairs:
            published.setdefault(stat, {}).setdefault(thr, []).append(pos)

    for stat, tiers in schedule.items():
        tier_values = [float(t["threshold"]) for t in tiers]
        covered: dict[float, list[str]] = {}
        missing: list[float] = []
        for thr in tier_values:
            positions = sorted(set(published.get(stat, {}).get(thr, [])))
            if positions:
                covered[thr] = positions
            else:
                missing.append(thr)
        extra = sorted(t for t in published.get(stat, {}) if t not in set(tier_values))
        out[stat] = {
            "league_tiers": tier_values,
            "covered": covered,
            "missing": missing,
            "extra_thresholds": {t: sorted(set(published[stat][t])) for t in extra},
        }
    return out
