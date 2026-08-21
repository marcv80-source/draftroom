"""Multi-source projection composite: blend COMPONENT STATS, then score once.

Plan 2026-08-20 B1. Until this module existed, ``validate/board.py`` set every player's PPG
from **Sleeper's stat line alone** -- ESPN and FantasyPros were fully resolved and scored, but
only fed the ``DISAGREE`` badge. The point estimate driving every recommendation ignored two of
the (then) three independent source families. A FOURTH family, **FantasySharks**, was verified
independent and wired in on 2026-08-20 (``docs/FANTASYSHARKS.md``): it disagrees with Sleeper,
ESPN and FantasyPros at 23-27% of stat level, the same magnitude those three disagree with each
other (21.3%), and nowhere near the 99.8%-identical re-publication signature the ESPN/Clay
positive control produces.

WHY COMPONENT STATS AND NOT POINTS. CLAUDE.md is explicit: "Every source adapter emits
component stats, never fantasy points. Points are computed only by applying the league's own
Yahoo ``stat_modifiers``." Averaging the sources' fantasy-point totals would also break the
per-game yardage bonus model, which needs YARDAGE (and a games figure) to compute a hit rate --
not a points total. So: blend the stat lines, then score the blended line once.

THE CORRECTNESS TRAP THIS MODULE EXISTS TO AVOID
------------------------------------------------
``StatLine`` is a plain dataclass whose every field defaults to ``0.0`` and which has no
``None``. A genuinely projected zero and a stat the source never published are therefore
**indistinguishable at the field level**. Averaging naively over "all the sources" turns
ESPN's 172 projected targets into ``172 / 3 = 57`` the moment Sleeper and FantasyPros (neither
of which publishes targets at all) contribute their structural zeros. Nothing downstream would
catch that: 57 targets is a perfectly plausible-looking number.

The resolution is **per SOURCE, not per value**: each adapter knows, from its own mapping
table, which canonical stats it actually publishes. :data:`SOURCE_PUBLISHES` is that knowledge,
made explicit and verified against the real cached payloads (see below). A stat a source does
not publish never enters that stat's average, and :class:`BlendProvenance` records how many
sources actually contributed to every single stat, so the denominator is never hidden.

HOW EVERY ENTRY IN THE TABLE WAS VERIFIED (2026-08-20, against the real cached payloads under
``data/raw/`` and ``data/manual/`` -- not read off a docstring)
--------------------------------------------------------------------------------------------
**Sleeper** (``data/raw/sleeper_projections``, 3,111 records). Counted, per canonical stat, how
many records carry the mapped raw key and how many carry a NONZERO value. Those two counts were
**identical for all 16 mapped stats** (e.g. ``pass_att`` present in 78 records, nonzero in 78;
``rec`` 474/474; ``games`` 3111/3111). Sleeper therefore OMITS zero-valued keys entirely, which
means an absent key from Sleeper is the source asserting zero, not withholding a number -- so
Sleeper's ``0.0`` is a real zero and belongs in the average. ``rec_tgt``: present in **0 of
3,111** records, and a scan for any target-shaped raw key (``*tg*``/``*targ*``/``*trg*``)
across the whole payload returned **nothing**. Confirms ``prep/sleeper_client.py``'s note and
CLAUDE.md. ``games``: present and nonzero on all 3,111 -- and, separately worth knowing, it is
the flat constant **18.0 for every single player** (see the note on ``games`` below).

**ESPN** (``data/raw/espn``, 461 skill players with a 2026 season-projection block). Same
presence-vs-nonzero count over the ``statSourceId == 1, statSplitTypeId == 0`` blocks: again
**identical for all 17 stats**, so ESPN also omits zeros and an absent id is an asserted zero.
Every canonical stat including ``rec_tgt`` (360 of 461 blocks -- receivers and backs) and
``games`` (461 of 461) is published. Presence is position-shaped exactly as expected: QBs carry
no receiving ids at all, WR/TE/RB carry no passing ids.

**FantasyPros** (``data/manual/FantasyPros_Fantasy_Football_Projections_<POS>.csv``, parsed:
82 QB / 132 RB / 190 WR / 120 TE rows). Here absence is STRUCTURAL, not an omitted zero: the
COLUMN does not exist. ``prep/manual_csv.POSITION_LAYOUTS`` is the ground truth and was
re-derived from the real files -- QB publishes passing + rushing + FL, RB rushing + receiving +
FL, WR receiving + rushing + FL, TE receiving + FL only. No position publishes ``rec_tgt``, any
2-point conversion, or ``games``. It is one of the TWO sources whose published set is
POSITION-DEPENDENT (FantasySharks, below, is the other), which is why
:data:`SOURCE_PUBLISHES_BY_POS` exists and why :func:`blend_statlines` takes an optional
``pos``: without it, a tight end's FantasyPros structural-zero ``rush_yd`` would divide
Sleeper's and ESPN's real rushing yards by an inflated denominator.

**FantasySharks** (``data/raw/fantasysharks``, 516 rows: 78 QB / 136 RB / 187 WR / 115 TE).
Absence here is STRUCTURAL like FantasyPros -- the served table has a fixed COLUMN layout per
position (``fantasysharks_client.POSITION_LAYOUTS``, read positionally because the RB table
repeats the header text ``">= 50 yd"`` for both rushing and receiving), so a stat with no column
is a number this source never published, not an omitted zero. Verified by the same
key-presence-versus-nonzero-count method used above, run over the real cached payload
(2026-08-20): for all four positions, **every declared-published stat is nonzero on at least one
row** and **no undeclared stat is ever nonzero on any row** -- i.e. the declared set is neither
too wide nor too narrow. The decisive numbers are ``rush_att``: nonzero on **78 of 78** QB rows
and 135 of 136 RB rows, and nonzero on **0 of 187** WR and **0 of 115** TE rows while those same
WR/TE rows carry ``rush_yd`` on 130 and 36 of them respectively. That is a column that does not
exist, not a projection of zero carries, which is why this source is position-keyed in
:data:`SOURCE_PUBLISHES_BY_POS`: without it, a tight end's structural-zero ``rush_att`` would
divide two real numbers by three. Also measured to zero across all 516 rows: every 2-point
conversion (no column exists anywhere in this source) and ``games`` (see the note below).

KNOWN LIMIT OF THIS RESOLUTION, STATED RATHER THAN HIDDEN
---------------------------------------------------------
Resolution is per (source, position), not per (source, player, stat). The adapters return a
``StatLine`` and discard which raw keys were present, so this module cannot see that ESPN
published no ``rush_att`` id for a specific deep-bench receiver. That case is harmless
*because* the counts above prove both API sources omit only zeros: the field they left absent
is a zero they are asserting, and averaging it in is correct. The case that would NOT be
harmless -- a structurally absent column -- is exactly the FantasyPros and FantasySharks
case, and both of those are handled by position.

WEIGHTS AND REJECTION
---------------------
``weights=None`` means equal weight, which is the correct default with no measured track
record to justify anything else (see the plan's "Backtest: verify before promising" -- 2025
preseason projections per source are not retrievable, so nobody has earned a higher weight).
A fourth family changes the denominator, not the weighting rule.
``rejected`` is the hook for the *amended* composite (plan B6): a ``(source, stat)`` pair
listed there is dropped from that stat's average. Rejection is per source AND per stat, not
per player, because a source is usually wrong about one thing (a receiver's targets) while
fine about the rest. Nothing populates ``rejected`` yet -- the plumbing and its tests exist so
B3/B4's challenge signals have somewhere to land.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Container, Iterable, Mapping

from draftroom.prep import espn_client, fantasysharks_client, manual_csv, sleeper_client
from draftroom.prep.schema import CANONICAL_STATS, StatLine

__all__ = [
    "COMPOSITE_SOURCES",
    "SOURCE_PUBLISHES",
    "SOURCE_PUBLISHES_BY_POS",
    "BlendProvenance",
    "blend_statlines",
    "blend_many",
    "published_stats",
    "games_distinct_counts",
    "varying_games_sources",
    "GAMES_NOTE",
]

#: The four INDEPENDENT source families (CLAUDE.md: ESPN's API and the Mike Clay PDF are ONE
#: source, verified 411/411 identical -- never both). FantasySharks was added 2026-08-20 after
#: its independence was verified against BOTH controls -- the ESPN/Clay re-publication pair
#: (99.8% of players identical within rounding) and the Sleeper/ESPN independent pair (0.0%) --
#: rather than against an invented correlation threshold; see ``docs/FANTASYSHARKS.md``. Same
#: tuple CONTENT as :data:`draftroom.valuation.disagreement.INDEPENDENT_SOURCES` (a test pins
#: them together); kept as its own name here because this module's meaning is "sources that feed
#: the point estimate", which is a different question from "sources that feed the spread
#: measure", even where they coincide.
COMPOSITE_SOURCES: tuple[str, ...] = ("sleeper", "espn", "fantasypros", "fantasysharks")

GAMES_NOTE = (
    "`games` is blended over the sources that report a POSITIVE figure AND whose figure "
    "actually VARIES from player to player. Two separate exclusions, for two different reasons. "
    "(1) A source that publishes no games column contributes nothing -- FantasyPros has none at "
    "all (measured: 0.0 on all 524 rows parsed from the four CSVs), and neither does "
    "FantasySharks (measured by its own games_report() over the served pages: 0 games-shaped "
    "headers on all four position tables, 0 distinct positive values across 516 parsed "
    "players). (2) A source publishing a "
    "single CONSTANT for everyone contributes nothing either, because a constant carries no "
    "player-specific durability information and averaging it in DESTROYS the information a "
    "varying source has. Measured 2026-08-20 on the adapters' own statlines: Sleeper = exactly "
    "ONE distinct value (18.0) across all 3,111 records, and 18 exceeds this league's own 17 "
    "weeks; ESPN = SEVEN distinct values across 461 statlines (17.0 x452, 15.0 x2, 11.0 x2, "
    "4.0 x2, 13.0, 10.0, 6.0), the low ones being ESPN flagging specific players as missing "
    "real time. Blending ESPN's 11-game projection with Sleeper's blanket 18 would give "
    "14.5, and min(14.5, curve) then throws away the only genuine per-player durability signal "
    "in the pipeline -- which directly contradicts validate/board.py's stated policy that a "
    "source projecting FEWER games than the curve is trusted outright because it knows "
    "something player-specific a rank curve cannot. The variance test is computed from each "
    "source's own resolved statlines at build time, never hardcoded, so a source that starts "
    "publishing real per-player games is picked up automatically. If no source qualifies the "
    "blend emits 0.0, which downstream reads as PlayerSeason.expected_games=None -> apply the "
    "fitted rank-conditional availability prior. A 0.0 is NEVER averaged in as a real "
    "projection of zero games played."
)

# --------------------------------------------------------------------------- publish tables

#: Canonical stats each source publishes, as the UNION over positions. Derived from the
#: adapters' own mapping tables and verified against the real cached payloads -- see the module
#: docstring for exactly which count confirmed which entry. A stat NOT in a source's set never
#: enters that stat's average from that source, at any weight.
SOURCE_PUBLISHES: Mapping[str, frozenset[str]] = {
    # Every canonical stat the Sleeper adapter maps. Notably NOT rec_tgt: 0 of 3,111 records
    # carry any target-shaped key.
    "sleeper": frozenset(sleeper_client.SLEEPER_STAT_MAP.values()),
    # All 17 canonical stats, rec_tgt included -- the reason ESPN is the only source that can
    # answer the receiver-targets question at all.
    "espn": frozenset(espn_client.ESPN_STAT_ID_MAP.values()),
    # Union over the four per-position CSV layouts. No 2pt columns, no targets, no games.
    "fantasypros": frozenset(
        canonical
        for layout in manual_csv.POSITION_LAYOUTS.values()
        for _header, canonical in layout
        if canonical is not None
    ),
    # Union over the four served position tables. Publishes rec_tgt -- which is why targets
    # stopped being a single-source stat -- and no 2pt column and no games column anywhere.
    # The union is deliberately the COARSE answer here: it claims `rush_att`, which is true for
    # QB and RB and structurally false for WR and TE. Callers that know the position get the
    # narrowed set from SOURCE_PUBLISHES_BY_POS below.
    "fantasysharks": fantasysharks_client.PUBLISHED_STATS,
}

#: The position-dependent refinement, for the two sources whose absences are STRUCTURAL. Both
#: have a different COLUMN SET per position -- FantasyPros' TE export has no rushing columns at
#: all; FantasySharks' WR and TE tables carry rushing YARDS and TDs with no attempts column
#: (verified: ``rush_att`` nonzero on 78 of 78 QB rows and 0 of 187 WR rows) -- so their
#: structural zeros are position-specific. Sleeper and ESPN publish the same schema for every
#: position and omit only genuine zeros (verified -- see module docstring), so they are absent
#: from this table and fall back to :data:`SOURCE_PUBLISHES`.
SOURCE_PUBLISHES_BY_POS: Mapping[str, Mapping[str, frozenset[str]]] = {
    "fantasypros": {
        pos.upper(): frozenset(
            canonical for _header, canonical in layout if canonical is not None
        )
        for pos, layout in manual_csv.POSITION_LAYOUTS.items()
    },
    # Straight from the adapter's own table, which is itself derived from POSITION_LAYOUTS --
    # not hand-copied, so it cannot drift from the parser that actually reads the columns.
    "fantasysharks": {
        pos.upper(): stats
        for pos, stats in fantasysharks_client.PUBLISHED_STATS_BY_POS.items()
    },
}


def published_stats(source: str, pos: str | None = None) -> frozenset[str]:
    """Canonical stats ``source`` publishes, narrowed to ``pos`` where that matters.

    Raises:
        ValueError: unknown source. A typo'd source name that silently published nothing would
            look exactly like a source with no data, and would quietly halve a denominator.
    """
    if source not in SOURCE_PUBLISHES:
        raise ValueError(
            f"unknown projection source {source!r}; known sources are "
            f"{sorted(SOURCE_PUBLISHES)}. Refusing to treat an unrecognised source as "
            "'publishes nothing' -- that would silently change every blend denominator."
        )
    if pos:
        by_pos = SOURCE_PUBLISHES_BY_POS.get(source)
        if by_pos is not None:
            narrowed = by_pos.get(pos.strip().upper())
            if narrowed is not None:
                return narrowed
    return SOURCE_PUBLISHES[source]


# -------------------------------------------------------------- does a source's games vary?


def _statlines_of(lines: Iterable[StatLine | None] | Mapping[object, StatLine | None]):
    """Accept either a sequence of statlines or a ``{key: statline}`` mapping."""
    if isinstance(lines, Mapping):
        return lines.values()
    return lines


def games_distinct_counts(
    by_source: Mapping[str, Iterable[StatLine | None] | Mapping[object, StatLine | None]],
) -> dict[str, int]:
    """How many DISTINCT positive ``games`` values each source publishes across its own pool.

    The diagnostic behind :func:`varying_games_sources`, exposed separately because the number
    itself is the evidence: 1 means the source is publishing a blanket constant, 0 means it
    publishes no games figure at all, and anything higher means real per-player variation.
    Values are rounded to 6 decimals before counting, so float noise cannot fake variation.
    """
    out: dict[str, int] = {}
    for source, lines in by_source.items():
        published_stats(source)  # validates the source name
        if "games" not in SOURCE_PUBLISHES[source]:
            out[source] = 0
            continue
        values = {
            round(float(sl.games), 6)
            for sl in _statlines_of(lines)
            if sl is not None and sl.games > 0
        }
        out[source] = len(values)
    return out


def varying_games_sources(
    by_source: Mapping[str, Iterable[StatLine | None] | Mapping[object, StatLine | None]],
) -> frozenset[str]:
    """The sources whose ``games`` figure carries real per-player information.

    A source is admitted only if its positive games values take **more than one distinct
    value** across its own resolved pool. See :data:`GAMES_NOTE` for the measured numbers and
    for why a constant must be excluded rather than averaged: blending ESPN's 11-game flag with
    Sleeper's blanket 18.0 yields 14.5, and the availability cap then discards the only genuine
    per-player durability signal the pipeline has.

    Computed from the data every time, never hardcoded -- a source that starts publishing real
    per-player games is picked up on the next build with no code change. Excluding a source here
    affects ``games`` ONLY; it still contributes every other stat normally.
    """
    counts = games_distinct_counts(by_source)
    return frozenset(source for source, n in counts.items() if n >= 2)


# ------------------------------------------------------------------------------ provenance


@dataclass(frozen=True)
class BlendProvenance:
    """How a blended stat line was reached. Every denominator visible, nothing implied.

    ``sources_by_stat`` is the load-bearing field: it is the answer to "who is this number
    actually from", per stat. ``rec_tgt`` on a real board reads ``("espn",)`` -- one source,
    ESPN's value passed through unchanged, NOT ``espn / 3``.
    """

    #: Sources that contributed to at least one stat, sorted. A source present in the input but
    #: fully rejected (or zero-weighted) does not appear here -- it contributed nothing.
    sources_present: tuple[str, ...]
    #: Sources that had a statline at all, sorted -- before rejection/weighting. Kept separate
    #: from ``sources_present`` so "the source was missing" and "the source was rejected" are
    #: distinguishable after the fact.
    sources_offered: tuple[str, ...]
    #: canonical stat -> the sources that contributed to its average, in sorted order.
    sources_by_stat: Mapping[str, tuple[str, ...]]
    #: canonical stat -> how many sources contributed. 0 means the blended value is 0.0 because
    #: NOBODY published it, which is a different fact from "the sources agree it is zero".
    n_by_stat: Mapping[str, int]
    #: The ``(source, stat)`` pairs that actually removed a contribution here. Only pairs that
    #: would otherwise have counted are listed -- ``rejected`` is typed as a ``Container``, so
    #: the input itself cannot be enumerated, and echoing back a rejection that never applied
    #: would misstate what happened to this player.
    rejected_applied: tuple[tuple[str, str], ...]
    #: The effective weight per offered source (equal weights when none were supplied).
    weights: Mapping[str, float]
    #: The position the publish tables were narrowed to, or None if not supplied. None means
    #: FantasyPros was treated with its UNION column set -- see SOURCE_PUBLISHES_BY_POS.
    pos: str | None
    #: True when at least one source CONTRIBUTED a games figure. False means the blended
    #: ``games`` is 0.0 = "unknown, apply the availability prior" (see :data:`GAMES_NOTE`).
    games_known: bool
    #: Sources that reported a positive games figure for this player but were NOT admitted,
    #: because their games figure is a blanket constant across their whole pool and so carries
    #: no player-specific information (see :func:`varying_games_sources`). Recorded because
    #: "Sleeper said 18 and we deliberately ignored it" must not be invisible.
    games_excluded_as_constant: tuple[str, ...] = ()

    @property
    def n_sources(self) -> int:
        return len(self.sources_present)

    def describe(self) -> str:
        """One line, for a log or a UI tooltip. Names the single-source stats explicitly --
        those are the ones a reader is most likely to mistake for a three-source consensus."""
        singles = sorted(s for s, n in self.n_by_stat.items() if n == 1)
        missing = sorted(s for s, n in self.n_by_stat.items() if n == 0)
        parts = [f"blended from {', '.join(self.sources_present) or 'nothing'}"]
        if singles:
            parts.append(f"single-source stats: {', '.join(singles)}")
        if missing:
            parts.append(f"no source published: {', '.join(missing)}")
        if self.rejected_applied:
            parts.append(
                "rejected: " + ", ".join(f"{s}/{st}" for s, st in self.rejected_applied)
            )
        if not self.games_known:
            parts.append("games unknown (availability prior applies)")
        return "; ".join(parts)


# --------------------------------------------------------------------------------- the blend


def blend_statlines(
    by_source: Mapping[str, StatLine | None],
    *,
    weights: Mapping[str, float] | None = None,
    rejected: Container[tuple[str, str]] = (),
    pos: str | None = None,
    games_sources: Container[str] | None = None,
) -> tuple[StatLine, BlendProvenance]:
    """Average each canonical stat over only the sources that PUBLISH it.

    Args:
        by_source: source name -> that source's statline, or ``None`` when the source has no
            data for this player. Keys must be known sources (see :data:`SOURCE_PUBLISHES`);
            an unknown key raises rather than being silently ignored.
        weights: source -> weight. ``None`` (the default) means equal weight, which is the
            right answer with no measured accuracy history to justify anything else. Weights
            need not sum to 1 -- each stat's average is normalised over whoever contributed to
            THAT stat, which is the whole point of this function.
        rejected: ``(source, stat)`` pairs to exclude from that stat's average -- the hook for
            the amended composite (plan B6). Any container supporting ``in``.
        pos: the player's canonical position (``QB``/``RB``/``WR``/``TE``). Supplying it
            narrows FantasyPros to the columns its export actually has for that position;
            omitting it uses FantasyPros' union column set, which OVERSTATES what FantasyPros
            publishes for a tight end (no rushing columns) and would dilute another source's
            real rushing number with a structural zero. Callers that know the position should
            always pass it.
        games_sources: the sources whose ``games`` figure is admitted -- pass
            ``varying_games_sources(...)`` computed over the whole resolved pool. This is a
            SOURCE-level fact ("does this source's games figure vary at all?") that a
            single-player call cannot see for itself, which is why it is an argument. ``None``
            means "admit any source reporting a positive figure", which is only safe when the
            caller has already established that every source it passes publishes real
            per-player games; a board caller must always pass the computed set, or Sleeper's
            blanket 18.0 will average away ESPN's real per-player projection (see
            :data:`GAMES_NOTE`). Affects ``games`` only.

    Returns:
        ``(blended_statline, provenance)``. A stat no source published comes back ``0.0`` with
        ``n_by_stat == 0`` -- read the provenance, not the value, to tell that apart from a
        genuine consensus zero.

    Raises:
        ValueError: an unknown source key in ``by_source`` or ``weights``, or a negative weight.
    """
    for key in by_source:
        published_stats(key, pos)  # validates the source name, raises on a typo
    if weights is not None:
        for key, w in weights.items():
            published_stats(key, pos)
            if w < 0:
                raise ValueError(
                    f"negative weight {w!r} for source {key!r}; a negative weight would "
                    "subtract one source's projection from another's, which is not a blend"
                )

    offered = tuple(sorted(k for k, v in by_source.items() if v is not None))
    effective_weights: dict[str, float] = {
        s: (1.0 if weights is None else float(weights.get(s, 0.0))) for s in offered
    }

    values: dict[str, float] = {}
    sources_by_stat: dict[str, tuple[str, ...]] = {}
    n_by_stat: dict[str, int] = {}
    rejected_applied: list[tuple[str, str]] = []
    contributed: set[str] = set()
    games_dropped: list[str] = []

    for stat in CANONICAL_STATS:
        num = 0.0
        den = 0.0
        used: list[str] = []
        for source in offered:
            statline = by_source[source]
            assert statline is not None  # guaranteed by `offered`
            if stat not in published_stats(source, pos):
                continue  # the source does not publish this stat: never a zero contribution
            if stat == "games":
                if float(statline.games) <= 0.0:
                    # An absent/zero games figure is "unknown", never "zero games played".
                    continue
                if games_sources is not None and source not in games_sources:
                    # The source publishes a blanket constant: no player-specific information,
                    # and averaging it in would destroy what a varying source knows.
                    games_dropped.append(source)
                    continue
            if (source, stat) in rejected:
                rejected_applied.append((source, stat))
                continue
            w = effective_weights[source]
            if w <= 0.0:
                continue
            num += w * float(getattr(statline, stat))
            den += w
            used.append(source)
        values[stat] = (num / den) if den > 0 else 0.0
        sources_by_stat[stat] = tuple(used)
        n_by_stat[stat] = len(used)
        contributed.update(used)

    blended = StatLine(**values)
    provenance = BlendProvenance(
        sources_present=tuple(sorted(contributed)),
        sources_offered=offered,
        sources_by_stat=sources_by_stat,
        n_by_stat=n_by_stat,
        rejected_applied=tuple(rejected_applied),
        weights=dict(effective_weights),
        pos=(pos.strip().upper() if pos else None),
        games_known=n_by_stat["games"] > 0,
        games_excluded_as_constant=tuple(sorted(games_dropped)),
    )
    return blended, provenance


def blend_many(
    by_player: Mapping[str, Mapping[str, StatLine | None]],
    *,
    pos_of: Mapping[str, str] | None = None,
    weights: Mapping[str, float] | None = None,
    rejected: Container[tuple[str, str]] = (),
) -> dict[str, tuple[StatLine, BlendProvenance]]:
    """:func:`blend_statlines` over a whole board, keyed by player id.

    Convenience only -- identical semantics, one player at a time, with ``pos_of`` supplying
    each player's position so the FantasyPros column narrowing actually applies. Because it
    sees the whole pool, it derives ``games_sources`` itself via
    :func:`varying_games_sources` rather than making the caller remember to.
    """
    pooled: dict[str, list[StatLine | None]] = {}
    for sources in by_player.values():
        for source, sl in sources.items():
            pooled.setdefault(source, []).append(sl)
    games_sources = varying_games_sources(pooled)

    out: dict[str, tuple[StatLine, BlendProvenance]] = {}
    for pid, sources in by_player.items():
        out[pid] = blend_statlines(
            sources,
            weights=weights,
            rejected=rejected,
            pos=(pos_of or {}).get(pid),
            games_sources=games_sources,
        )
    return out
