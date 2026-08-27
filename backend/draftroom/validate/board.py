"""Build a REAL (not synthetic) draft board from cached prep data, for validation tooling.

Every other tool in this repo that needs a board either uses hand-built synthetic fixtures
(tests) or derives a SYNTHETIC draft value straight from ADP (``tests/test_recommend.py``)
because a real projections -> valuation pipeline isn't wired end to end yet. This module closes
that gap for validation purposes only: it joins the cached Sleeper season projections onto the
cached FFC ADP board via the real crosswalk (:mod:`draftroom.prep.crosswalk`), scores each
player's projected stat line with the league's OWN scoring PLUS the validated per-game yardage
bonus model (:func:`draftroom.prep.scoring.score_statline_with_bonus`, additive on top of the
untouched ``score_statline`` dot product -- see that function's docstring; MAE 1.335, mean bias
0.254 against the 2025 backtest), and runs the result through the real replacement/EVoB
pipeline (:mod:`draftroom.valuation.replacement`, :mod:`draftroom.valuation.evob`). This is the
first (and, as of 2026-08-18, only) production path that actually calls the bonus model --
``score_statline`` itself stays a pure dot product, untouched, exactly as its docstring
requires.

**WHICH projection drives PPG is now a parameter** (plan 2026-08-20, B1). Until that plan, this
module set every player's PPG from **Sleeper's stat line alone** -- ESPN and FantasyPros were
fully resolved and scored, but only fed the ``DISAGREE`` badge, so the point estimate behind
every recommendation ignored two of the (then) three independent source families.
``build_real_board`` now takes ``source=`` (default ``"blend"``, Marc's decision): ``"blend"``
scores the equal-weight component-stat composite (:mod:`draftroom.valuation.composite`), and
``"sleeper"``/``"espn"``/``"fantasypros"``/``"fantasysharks"`` each score that source's statline
**unmodified**, which is what makes the toggle an honest comparison rather than a re-weighting
of the same numbers. Everything else -- the bonus model, the availability-curve cap, the
disagreement measure, the replacement/EVoB chain -- is untouched by the switch; only the
statline feeding ``ppg`` changes.

**FantasySharks is the FOURTH family** (added 2026-08-20 after ``docs/FANTASYSHARKS.md``
established independence against two controls; see ``docs/FANTASYSHARKS.md``). It is resolved
here exactly like ESPN and FantasyPros: best-effort, degrading with a warning to "this source
contributes nothing" rather than failing the board build. It publishes no games column at all,
so it never contributes to the blended games figure -- and it does not need a special case to
be excluded, because :func:`~draftroom.valuation.composite.varying_games_sources` measures that
from the resolved pool itself.

Nothing here invents a number: PPG comes from the ACTIVE source's projected stat line divided by
the games figure that source publishes (see :data:`GAMES_DIVISOR_NOTE` for the two sources
-- FantasyPros and FantasySharks -- that publish none), ``expected_games`` is ``min(the source's games,
the rank-conditional availability curve)`` -- see :func:`_cap_expected_games_by_curve`; the
fitted curve corrects source optimism about durability while a source projecting FEWER games
than the curve is trusted outright -- and ADP/stdev come straight from the cached FFC payload.
The only approximation is the join
itself (name/ID crosswalk) and the well-documented FFC-is-published-at-12-teams caveat
(CLAUDE.md: "One deliberate 12-team exception").

Reads only cached files under ``data/raw/`` (:func:`draftroom.prep.http.load_latest_raw`) -- no
network call, so this is safe to run offline and on draft night's own machine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping

from draftroom.config import LeagueConfig
from draftroom.draft.recommend import BoardPlayer
from draftroom.prep import espn_client, fantasysharks_client, manual_csv
from draftroom.prep.crosswalk import DYNASTYPROCESS_SOURCE, Crosswalk, build_crosswalk
from draftroom.prep.ffc_client import AdpRow, parse_adp_rows
from draftroom.prep.http import load_latest_raw
from draftroom.prep.schema import StatLine, normalize_name
from draftroom.prep.scoring import score_statline_with_bonus
from draftroom.prep.sleeper_client import SKILL_POSITIONS, to_statlines
from draftroom.valuation.bonuses import load_bonus_schedule, load_curves
from draftroom.valuation.composite import (
    COMPOSITE_SOURCES,
    BlendProvenance,
    blend_statlines,
    games_distinct_counts,
    varying_games_sources,
)
from draftroom.valuation.disagreement import (
    DISAGREEMENT_CAVEAT,
    SourceDisagreement,
    compute_disagreement,
    sigma_ppg_from_disagreement,
)
from draftroom.valuation.decisions import Decision, load_decisions, rejected_index
from draftroom.valuation.evob import compute_draft_values
from draftroom.valuation.injury_research import (
    ResearchNote,
    load_research,
    unpriced_notes,
)
from draftroom.valuation.playing_time import (
    Binding,
    PlayingTimeOverride,
    bind as bind_playing_time,
    load_overrides,
    overrides_by_pid,
)
from draftroom.valuation.replacement import PlayerSeason

__all__ = [
    "RealBoard",
    "build_real_board",
    "SEASON",
    "BOARD_SOURCE_KEYS",
    "DEFAULT_BOARD_SOURCE",
]

log = logging.getLogger("draftroom.validate.board")

#: Hardcoded per this repo's convention (prep/fantasypros_client.py, prep/fetch_all.py do the
#: same) -- there is no shared "current season" constant elsewhere in the codebase to import.
SEASON = 2026

#: Which projection the board's PPG comes from. ``"blend"`` is the equal-weight composite over
#: the four independent families (:mod:`draftroom.valuation.composite`); each single-source key
#: uses that source's statline **unmodified**, which is what makes the toggle an honest
#: comparison rather than a re-weighting of the same thing. Derived from ``COMPOSITE_SOURCES``,
#: so adding a family is a one-line change there and every board key follows.
BOARD_SOURCE_KEYS: tuple[str, ...] = ("blend", *COMPOSITE_SOURCES)

#: Marc's decision, 2026-08-20 (docs/archive/PLAN_2026-08-20.md): the default projection is the
#: equal-weight blend of ALL the source families -- four since FantasySharks was verified and
#: wired in the same day. Neither ESPN nor Sleeper is "the source of
#: record" any more -- the composite is, and the active key is explicit in every payload.
DEFAULT_BOARD_SOURCE = "blend"

#: How the season-total -> PPG divisor is chosen, stated once here because it is the one place
#: this module has to take a position on a number no source supplies.
GAMES_DIVISOR_NOTE = (
    "PPG = season points / games, where games is the ACTIVE SOURCE's own projected games "
    "whenever it publishes one (Sleeper: a flat 18.0 for every record; ESPN: a real per-player "
    "figure, 17.0 for 452 of 461; the blend: ESPN's alone, because Sleeper's constant carries "
    "no player-specific information). TWO of the four families publish no games column at all "
    "-- FantasyPros (0 columns across the four CSVs) and FantasySharks (0 games-shaped headers "
    "on all four served tables, 0 distinct positive values across 516 players, re-measured by "
    "its own games_report() rather than asserted) -- so a line from either falls back to the "
    "league's own season length (LeagueConfig.weeks) as the divisor -- i.e. it is read as a "
    "full-season projection -- and its expected_games is left None so the fitted "
    "rank-conditional availability prior supplies the VOLUME. No games figure is ever "
    "fabricated per player, and a 0.0 from a source is never treated as 'projected for zero "
    "games played'."
)


@dataclass(frozen=True)
class RealBoard:
    """The joined, valued board plus a record of what did NOT make it on, for transparency.

    Both ``players`` and ``seasons`` describe the same underlying pool: ``seasons`` is the raw
    valuation INPUT (ppg + expected_games, what :func:`~draftroom.valuation.evob.compute_draft_values`
    itself takes), ``players`` is the OUTPUT joined onto ADP (what the draft engine's
    :class:`~draftroom.draft.recommend.BoardPlayer` needs). Callers doing their own valuation
    sweep (e.g. the sanity-invariant gate re-deriving a baseline at a different config) want
    ``seasons``; callers driving a mock draft want ``players``.
    """

    players: tuple[BoardPlayer, ...]
    seasons: tuple[PlayerSeason, ...]
    #: FFC rows at a skill position that never got a real dv (unresolved, or resolved but with
    #: no/zero-game projection). Never silently dropped -- callers can inspect this.
    excluded: tuple[AdpRow, ...]
    cfg: LeagueConfig
    #: Cross-source (Sleeper/FantasyPros/ESPN/FantasySharks) spread, keyed by player_id. Populated
    #: for whichever players resolved onto at least one of those sources -- a player absent
    #: here simply had no data to compare (never a fabricated zero). See
    #: :mod:`draftroom.valuation.disagreement` and ``disagreement_caveat`` below before reading
    #: any of these numbers as a confidence signal.
    disagreement: Mapping[str, SourceDisagreement]
    #: Marc's adjudicated rejections that actually APPLIED to each player, keyed by
    #: player_id -- so the UI can say WHY a number is gone rather than silently showing a
    #: different value than the sources would imply. Absent = nothing was rejected for him.
    applied_decisions: Mapping[str, tuple[Decision, ...]]
    #: The mandated caveat (verbatim, see draftroom.valuation.disagreement.DISAGREEMENT_CAVEAT):
    #: attached directly to the data, not just to a docstring, so nothing that carries a
    #: RealBoard around loses it.
    disagreement_caveat: str = DISAGREEMENT_CAVEAT
    #: WHICH projection built this board -- one of :data:`BOARD_SOURCE_KEYS`. Never implicit:
    #: the plan requires the active source to be visible in the payload and on screen, and a
    #: post-draft audit needs to know which board a pick was made against.
    source: str = DEFAULT_BOARD_SOURCE
    #: player_id -> {source key -> league-scored SEASON POINTS under that source's own statline}
    #: (same scoring the board itself uses, bonus model included). Carries a ``"blend"`` entry
    #: alongside the four families so the UI can show all five side by side with no refetch.
    #: A source absent for a player simply has no key -- never a fabricated 0.0.
    points_by_source: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    #: player_id -> how that player's blended statline was reached (per-stat contributing source
    #: counts, single-source stats, what was rejected). Populated for every player regardless of
    #: the active source, because the composite is computed either way and hiding how a number
    #: was reached is not allowed.
    blend_provenance: Mapping[str, BlendProvenance] = field(default_factory=dict)
    #: Verbatim :data:`GAMES_DIVISOR_NOTE`, attached to the data for the same reason
    #: ``disagreement_caveat`` is.
    games_divisor_note: str = GAMES_DIVISOR_NOTE
    #: Marc's manual playing-time overrides that actually MOVED a player's expected games,
    #: keyed by player_id (:mod:`draftroom.valuation.playing_time`). Same asymmetry as
    #: ``applied_decisions``: an override the availability curve clamped away, or one that
    #: landed on the figure the pipeline already had, is loaded and logged but NOT recorded
    #: here, because a badge must never claim a change that did not happen.
    #: (Appended at the END of the field list deliberately, like PoolPlayer's later fields:
    #: callers construct RealBoard positionally through the earlier ones.)
    applied_playing_time: Mapping[str, Binding] = field(default_factory=dict)
    #: EVERY override loaded from ``data/playing_time.json``, whether it moved anything or not.
    #: ``applied_playing_time`` answers "what changed for him"; this answers "what judgements
    #: are on file", which is the question an audit asks.
    playing_time_overrides: Mapping[str, PlayingTimeOverride] = field(default_factory=dict)
    #: Externally researched findings that NO number on this board reflects, keyed by player_id
    #: (:mod:`draftroom.valuation.injury_research`). Two kinds: research that carries no games
    #: figure at all (open discipline -- the category with zero sources), and research that
    #: carries one but whose override has not been applied or was clamped away. Rendered as a
    #: badge, because the alternative is a judgement sitting in a JSON file nobody opens in a
    #: live room. Restricted to players actually ON this board: a note on a man who cannot be
    #: drafted here is noise.
    research_notes: Mapping[str, ResearchNote] = field(default_factory=dict)


def build_real_board(
    cfg: LeagueConfig | None = None, *, source: str = DEFAULT_BOARD_SOURCE
) -> RealBoard:
    """Join cached FFC ADP onto cached projections, score with ``cfg``, value with EVoB.

    Args:
        cfg: league config to score and value against. Defaults to
            :meth:`~draftroom.config.LeagueConfig.from_yaml` -- the real, CONFIRMED 10-team
            league (``data/league_manual.yaml``).
        source: which projection drives ``ppg`` -- one of :data:`BOARD_SOURCE_KEYS`.
            ``"blend"`` (the default) is the equal-weight component-stat composite over the
            four independent families; a single-source key uses that source's statline
            unmodified. All four sources are resolved either way (they already were, for the
            disagreement measure), so switching costs nothing extra and no source is fetched or
            resolved twice.

    Raises:
        ValueError: unknown ``source``. Silently falling back to some other projection would
            mean the board on screen is not the board the label claims.
    """
    cfg = cfg or LeagueConfig.from_yaml()
    if source not in BOARD_SOURCE_KEYS:
        raise ValueError(
            f"unknown board source {source!r}; expected one of {list(BOARD_SOURCE_KEYS)}"
        )

    sleeper_raw = load_latest_raw("sleeper")
    ffc_raw = load_latest_raw("ffc")
    ffc_rows = parse_adp_rows(ffc_raw)
    try:
        dp_csv = load_latest_raw(DYNASTYPROCESS_SOURCE)
    except FileNotFoundError:
        dp_csv = None
        log.warning(
            "no cached DynastyProcess crosswalk under data/raw/dynastyprocess/; falling back "
            "to Sleeper's own cross-ID fields only for stage-1 matching"
        )

    cw = build_crosswalk(sleeper_raw, ffc_rows, dynastyprocess_csv_text=dp_csv)
    statlines = to_statlines(load_latest_raw("sleeper_projections"))

    # Bonus model (task: wire the validated additive bonus into production scoring). Best
    # effort: a missing fitted-curves cache degrades to plain score_statline (bonus_schedule/
    # bonus_curves=None makes score_statline_with_bonus byte-for-byte equal to score_statline,
    # per that function's own docstring/tests) rather than failing the whole board build.
    try:
        bonus_schedule = load_bonus_schedule()
        bonus_curves = load_curves()
    except FileNotFoundError:
        bonus_schedule = None
        bonus_curves = None
        log.warning(
            "no cached fitted bonus curves under data/bonus_curves.json; scoring without the "
            "per-game yardage bonus for this build"
        )

    # Cross-source disagreement inputs (Marc's own idea, approved 2026-08-17): ESPN and the
    # manual FantasyPros CSVs, each resolved onto the crosswalk's pid the same way Sleeper
    # already is. Both are best-effort -- a missing/stale/malformed source degrades to "this
    # source contributes nothing" rather than failing the whole board build, because the core
    # ADP x Sleeper valuation pipeline has nothing to do with whether Marc has downloaded a
    # fresh FantasyPros CSV this week.
    espn_by_pid = _resolve_espn_statlines(cw)
    fantasypros_by_pid = _resolve_fantasypros_statlines(cw)
    fantasysharks_by_pid = _resolve_fantasysharks_statlines(cw)

    # Which sources' `games` figure carries real per-player information, measured from the
    # resolved pools themselves rather than hardcoded (see composite.GAMES_NOTE). Sleeper
    # publishes a blanket 18.0 for every record -- averaging that into ESPN's real per-player
    # projection would erase the only genuine durability signal the pipeline has, and would
    # contradict _cap_expected_games_by_curve's own stated policy that a source projecting
    # FEWER games than the curve is trusted outright.
    games_pools = {
        "sleeper": statlines,
        "espn": espn_by_pid,
        "fantasypros": fantasypros_by_pid,
        # Publishes no games column at all, so it drops out of the games blend on the
        # MEASUREMENT (0 distinct positive values), not on a hardcoded exclusion. If
        # FantasySharks ever adds a real per-player games column, the next build admits it.
        "fantasysharks": fantasysharks_by_pid,
    }
    games_sources = varying_games_sources(games_pools)
    log.info(
        "games figures admitted to the blend: %s (distinct positive values per source: %s)",
        sorted(games_sources) or "none", games_distinct_counts(games_pools),
    )

    # Marc's adjudicated rejections (the review queue -- docs/REVIEW_QUEUE.md). Nothing here was
    # decided by the tool: `candidates.py` only ever SURFACES a candidate, and a number is dropped
    # only because he said so.
    #
    # DecisionsFileError is deliberately NOT caught. Every other optional input in this module
    # degrades to "this source contributes nothing" on a bad cache, because a missing FantasyPros
    # CSV has nothing to do with whether the board is sound. A malformed decisions file is the
    # opposite: degrading would silently stop applying rejections Marc made deliberately, and the
    # board would look fine while quietly disagreeing with him. Fail loudly instead.
    rejections = rejected_index(load_decisions())
    if not rejections.is_empty:
        log.info(
            "applying %d adjudicated rejection(s) from the review queue: %s source-wide, "
            "%d player-specific",
            rejections.n_rejections, sorted(rejections.source_wide), len(rejections.by_player),
        )

    # Marc's manual playing-time overrides -- the ONLY thing in this pipeline that can move a
    # player's expected games on human knowledge (docs/PLAYING_TIME.md). Loaded here and applied
    # inside _cap_expected_games_by_curve, because the override and the curve are one decision:
    # `min(override, curve)`. PlayingTimeFileError is deliberately NOT caught, for exactly the
    # reason DecisionsFileError isn't -- degrading would silently un-apply a judgement Marc made
    # about a player he knows something about, and the board would look fine while ignoring him.
    # Fails closed exactly like the overrides and the decisions file: missing means nothing
    # was researched; present-but-broken raises rather than degrading to "no findings".
    research = load_research()
    playing_time = overrides_by_pid(load_overrides())
    if playing_time:
        log.info(
            "%d manual playing-time override(s) on file: %s",
            len(playing_time),
            "; ".join(o.describe() for o in playing_time.values()),
        )

    seasons: list[PlayerSeason] = []
    meta: dict[str, AdpRow] = {}
    excluded: list[AdpRow] = []
    disagreement: dict[str, SourceDisagreement] = {}
    applied_decisions: dict[str, tuple[Decision, ...]] = {}
    points_by_source: dict[str, dict[str, float]] = {}
    blend_provenance: dict[str, BlendProvenance] = {}

    def _score(statline: StatLine, pos: str, games: float) -> float:
        return score_statline_with_bonus(
            statline.as_dict(), cfg.scoring,
            pos=pos, games=games,
            bonus_schedule=bonus_schedule, bonus_curves=bonus_curves,
        )

    for row in ffc_rows:
        pos = (row.pos or "").strip().upper()
        if pos not in SKILL_POSITIONS:
            continue  # DEF/PK: out of this league's scope entirely, not a data gap.
        key = str(row.player_id) if row.player_id is not None else f"{row.name}|{row.team}|{row.pos}"
        pid = cw.resolve("ffc", key)
        if pid is None:
            excluded.append(row)  # unresolved crosswalk -- no source can be attached at all.
            continue
        pid = str(pid)

        # The SAME resolved statlines that already fed the disagreement measure are the
        # composite's inputs. Nothing is fetched or resolved a second time.
        by_source: dict[str, StatLine | None] = {
            "sleeper": statlines.get(pid),
            "espn": espn_by_pid.get(pid),
            "fantasypros": fantasypros_by_pid.get(pid),
            "fantasysharks": fantasysharks_by_pid.get(pid),
        }
        blended, provenance = blend_statlines(
            by_source,
            pos=pos,
            games_sources=games_sources,
            rejected=rejections.for_player(pid),
        )

        active = blended if source == "blend" else by_source[source]
        if active is None or not _has_projection(active):
            # Resolved, but the ACTIVE source has nothing for this player. Recorded, never
            # silently dropped, and never back-filled from a different source -- that would
            # make the single-source boards dishonest comparisons.
            excluded.append(row)
            continue

        # Season points under every source that resolved, plus the blend, on the board's own
        # scale (same scoring, same bonus model) -- what the UI shows side by side. Recorded
        # only for players who actually made THIS board, so `points_by_source` and
        # `blend_provenance` are keyed by exactly the same ids as `players`/`seasons`; an
        # excluded player has no row to show them on.
        per_source_points: dict[str, float] = {}
        for skey, sl in (*by_source.items(), ("blend", blended)):
            if sl is None or not _has_projection(sl):
                continue
            per_source_points[skey] = _score(sl, pos, _games_divisor(sl, cfg))
        points_by_source[pid] = per_source_points
        blend_provenance[pid] = provenance

        divisor = _games_divisor(active, cfg)
        total_points = _score(active, pos, divisor)

        d = compute_disagreement(
            pid,
            {key: (sl.as_dict() if sl is not None else None) for key, sl in by_source.items()},
            cfg.scoring,
        )
        disagreement[pid] = d
        # Record a decision against this player ONLY where it actually removed a contribution.
        # `rejections.decisions_for(pid)` includes every SOURCE-WIDE rejection, which is the
        # right answer to "what rules are in force" and the wrong answer to "what changed for
        # him": rejecting (fantasysharks, pass_td) is in force for all 188 players but alters
        # only the ~36 who have a FantasySharks passing-TD number. Badging all 188 would put a
        # REJ on every row and teach Marc to ignore it. `provenance.rejected_applied` is
        # already filtered to pairs that genuinely removed a contribution here.
        #
        # Only the blend can carry these: a single-source board scores that source's statline
        # UNMODIFIED (see `active` below), which is the entire point of the toggle, so no
        # rejection applies there and no badge should claim one did.
        if source == "blend" and provenance.rejected_applied:
            # Match on `d.stats`, not `d.stat`: a whole-statline decision carries the literal
            # sentinel `"*"`, while `rejected_index` expands it before the composite ever sees
            # it, so `rejected_applied` holds only concrete pairs. Comparing the raw `d.stat`
            # therefore matched nothing for exactly the largest kind of rejection -- a "*"
            # decision on Jordyn Tyson moved his dv by 13.4 and showed NO badge, which is the
            # precise failure this badge exists to prevent.
            applied_here = frozenset(provenance.rejected_applied)
            applied = tuple(
                d
                for d in rejections.decisions_for(pid)
                if any((d.source, s) in applied_here for s in d.stats)
            )
            if applied:
                applied_decisions[pid] = applied
        sigma_ppg = sigma_ppg_from_disagreement(d, divisor)

        seasons.append(
            PlayerSeason(
                player_id=pid,
                pos=pos,
                ppg=total_points / divisor,
                # The source's OWN games figure, capped by the availability curve below. None
                # when the active source publishes none (FantasyPros) -- which makes
                # resolve_players apply the fitted rank-conditional prior instead, exactly as
                # prep/manual_csv.py's 2026-08-18 note requires.
                expected_games=(active.games if active.games > 0 else None),
                sigma_ppg=sigma_ppg,  # None unless >=2 independent sources resolved -- see disagreement.py.
                name=row.name,
            )
        )
        meta[pid] = row

    # Cap each player's expected games at the rank-conditional availability curve (Codex
    # 2026-08-18: passing Sleeper's games straight through made EXPECTED_GAMES_CURVE inert on
    # the real board -- the curve only applied when expected_games was None, which it never
    # was here). Policy: `min(source_games, curve)`. The curve is FITTED actual availability
    # by positional rank (late QBs really average ~11 of 17), so a source projecting MORE
    # games than players at that rank historically play is optimism the fit corrects; a source
    # projecting FEWER is trusted outright -- it knows something player-specific (suspension,
    # a dated return timeline) that a rank curve cannot.
    seasons, playing_time_bindings = _cap_expected_games_by_curve(
        seasons, cfg, overrides=playing_time
    )
    for pid, binding in playing_time_bindings.items():
        # An override that moved nothing is a note that is not doing what Marc thinks it is, so
        # it is said out loud rather than left to be inferred from an absent badge.
        (log.info if binding.moved else log.warning)(
            "playing-time override %s: %s",
            "APPLIED" if binding.moved else "CHANGED NOTHING",
            binding.describe(),
        )
    # A VALID id pointing at the WRONG player is the dangerous case: it applies cleanly, badges
    # cleanly, and moves a player Marc never meant to touch. The loader cannot catch it (it has
    # no board), but here the names are in hand. A warning, not an error -- names legitimately
    # differ on suffixes and punctuation, so refusing the build would make the file brittle.
    name_by_pid = {pid: (row.name or "") for pid, row in meta.items()}
    for pid, override in playing_time.items():
        board_name = name_by_pid.get(pid)
        if not override.player_name or board_name is None:
            continue
        if normalize_name(override.player_name) != normalize_name(board_name):
            log.warning(
                "playing-time override for player_id %s names %r but that id is %r on the "
                "board. The id is what gets applied -- if %r is who you meant, the id is wrong "
                "and this override is moving the wrong player.",
                pid, override.player_name, board_name, override.player_name,
            )

    unmatched = sorted(set(playing_time) - set(playing_time_bindings))
    if unmatched:
        # Not an error: an override may legitimately name a player the ACTIVE source has no
        # projection for, or one who is not on the ADP board at all. But a silent no-op on a
        # hand-written judgement is exactly the failure this whole module exists to prevent.
        log.warning(
            "%d playing-time override(s) matched no player on this board [source=%s] and did "
            "nothing: %s. Check the player_id against the pool -- an id that is not on the "
            "board is usually a typo or a player this source does not project.",
            len(unmatched), source,
            "; ".join(playing_time[pid].describe() for pid in unmatched),
        )

    dv_map = compute_draft_values(seasons, cfg)

    players = tuple(
        BoardPlayer(
            player_id=pid,
            name=meta[pid].name,
            pos=dv.pos,
            team=meta[pid].team,
            bye=meta[pid].bye,
            adp=meta[pid].adp,
            stdev=meta[pid].std_dev,
            dv=dv.dv,
            dv_sd=dv.sigma_season,  # populated wherever cross-source disagreement gave a sigma_ppg.
        )
        for pid, dv in dv_map.items()
    )
    log.info(
        "real board [source=%s]: %d players valued, %d FFC skill rows excluded "
        "(unresolved or no projection from this source)",
        source, len(players), len(excluded),
    )
    # A VALID Sleeper id pointing at the WRONG player is the dangerous case here too: it binds
    # cleanly, badges cleanly, and puts one man's risk on another man's row while leaving the
    # real one unbadged. Same check the playing-time overrides get above, and deliberately the
    # same SEVERITY -- a warning, not a raise (Codex 2026-08-27 asked for a raise).
    #
    # Why a warning: the sibling check on `playing_time` is a warning for a stated reason ("names
    # legitimately differ on suffixes and punctuation, so refusing the build would make the file
    # brittle"), and that reason applies here unchanged. Two things make it MORE tolerable here,
    # not less: a research note moves no number at all, where an override moves a real one, and
    # `player_name` now travels in the payload so a mis-bound note is visible on the row itself
    # rather than only in a log. A file that refuses to build the board on a punctuation
    # difference would get edited around, which is worse than a loud line.
    for pid, finding in ((f.player_id, f) for f in research):
        board_name = name_by_pid.get(pid)
        if not finding.player_name or board_name is None:
            continue
        if normalize_name(finding.player_name) != normalize_name(board_name):
            log.warning(
                "research finding for player_id %s names %r but that id is %r on the board. "
                "The id is what binds -- if %r is who you meant, the id is wrong and this "
                "finding is badging the wrong player (and leaving %r unbadged).",
                pid, finding.player_name, board_name, finding.player_name, finding.player_name,
            )

    # Research the board is NOT already expressing as a number. `priced_pids` is the set whose
    # override actually MOVED something -- for those the NN.NG badge is the stronger statement,
    # so a note would be redundant (the same asymmetry REJ already uses). Restricted to players
    # on this board, because a note on a man with no row has nowhere to render and would only
    # ever be seen in a log.
    on_board = {p.player_id for p in players}
    notes = {
        pid: note
        for pid, note in unpriced_notes(
            research,
            priced_pids={pid for pid, b in playing_time_bindings.items() if b.moved},
        ).items()
        if pid in on_board
    }
    if notes:
        log.info(
            "%d research note(s) carried to the board -- findings no number reflects: %s",
            len(notes),
            "; ".join(
                f"{n.finding.player_name or pid} ({n.finding.status or 'status unstated'})"
                for pid, n in notes.items()
            ),
        )
    off_board = sorted(
        {f.player_id for f in research}
        - on_board
        - {pid for pid, b in playing_time_bindings.items() if b.moved}
    )
    if off_board:
        # Same class of warning as an unmatched override, and it earns its place for the same
        # reason: research about a player with no row is invisible, and silence there is
        # indistinguishable from success.
        log.warning(
            "%d research finding(s) name a player who is not on this board [source=%s], so no "
            "note can be shown for them: %s",
            len(off_board), source, ", ".join(off_board),
        )

    return RealBoard(
        players=players, seasons=tuple(seasons), excluded=tuple(excluded), cfg=cfg,
        disagreement=disagreement,
        applied_decisions=applied_decisions,
        source=source,
        points_by_source=points_by_source,
        blend_provenance=blend_provenance,
        # Only the bindings that MOVED a number get badged (see the field's own comment); the
        # full set on file is carried separately for the audit question.
        applied_playing_time={
            pid: b for pid, b in playing_time_bindings.items() if b.moved
        },
        playing_time_overrides=dict(playing_time),
        research_notes=notes,
    )


def _has_projection(statline: StatLine) -> bool:
    """Does this statline carry a real projection, as opposed to being an empty shell?

    ``games > 0 or has_nonzero_stats()``. Both halves matter, and for different sources:
    Sleeper and ESPN always publish a games figure (so the first half is what admits them, and
    is exactly the ``statline.games <= 0`` gate this module used before the composite landed --
    behaviour on the ``"sleeper"`` board is unchanged); FantasyPros publishes no games column at
    all, so a real FantasyPros row is admitted only by its nonzero component stats.
    """
    return statline.games > 0 or statline.has_nonzero_stats()


def _games_divisor(statline: StatLine, cfg: LeagueConfig) -> float:
    """The season-total -> PPG divisor. See :data:`GAMES_DIVISOR_NOTE` (verbatim, on RealBoard).

    The source's own projected games when it publishes one; otherwise the league's own season
    length, because a season total with no games figure is a full-season projection and
    ``cfg.weeks`` is the league's confirmed setting, not a number invented here. The VOLUME
    side is untouched by this fallback: ``expected_games`` stays ``None`` in that case, so the
    fitted rank-conditional availability prior -- not 17 -- decides how many games the rate is
    credited for.
    """
    return float(statline.games) if statline.games > 0 else float(cfg.weeks)


def _cap_expected_games_by_curve(
    seasons: list[PlayerSeason],
    cfg: LeagueConfig,
    *,
    overrides: Mapping[str, PlayingTimeOverride] | None = None,
) -> tuple[list[PlayerSeason], dict[str, Binding]]:
    """``expected_games = min(the human's figure if any else source_games, curve(pos, rank))``.

    Rank is 1-based by projected PPG within the position -- the same ranking convention
    :func:`draftroom.valuation.replacement.resolve_players` uses for curve lookups, so this cap
    and the valuation pipeline agree on who "rank 25" is. PPG itself is untouched (it is a
    per-game rate; this only changes the games VOLUME that rate is credited for).

    Two inputs, one rule. Without an override the behaviour is unchanged: a source projecting
    MORE games than players at that rank historically play is optimism the fit corrects, and a
    source projecting FEWER is trusted outright. With an override
    (:mod:`draftroom.valuation.playing_time`) Marc's figure REPLACES the source's -- including
    the ``None`` that FantasyPros and FantasySharks leave behind, which is why an override is
    the only thing here that can turn an implicit "let the fitted prior decide" into an
    explicit number -- and the same curve then clamps it. So an override lowers a player freely
    and restores him only as far as the healthy-rank figure. That clamp is what keeps
    :func:`draftroom.validate.invariants.check_expected_games_capped_by_curve` true by
    construction rather than by exemption.

    Returns:
        The seasons (a new list, same order) and, keyed by player_id, a
        :class:`~draftroom.valuation.playing_time.Binding` for every override that matched a
        player here -- including the ones that changed nothing, because the caller reports
        those rather than hiding them. Overrides naming a player not in ``seasons`` are simply
        absent from the mapping, which is how the caller detects them.
    """
    from dataclasses import replace as _dc_replace

    from draftroom.valuation.replacement import expected_games as _curve_games

    overrides = overrides or {}
    by_pos: dict[str, list[PlayerSeason]] = {}
    for s in seasons:
        by_pos.setdefault(s.pos, []).append(s)

    capped: dict[str, PlayerSeason] = {}
    bindings: dict[str, Binding] = {}
    for pos, group in by_pos.items():
        for rank, s in enumerate(sorted(group, key=lambda x: -x.ppg), start=1):
            cap = _curve_games(pos, rank=rank, weeks=cfg.weeks)
            override = overrides.get(s.player_id)
            if override is not None:
                # `bind` needs the ALREADY-CAPPED source figure, not the raw one -- passing the
                # raw number made every override on a player the curve had already capped look
                # like it moved something (Josh Allen: source 17.0, curve 16.6, so an override
                # of 66.6 clamped to 16.6 read as 17.0 -> 16.6 and got badged for a change it
                # did not make).
                #
                # `expected_games=None` means "this source published no games column" and must
                # reach `bind` as None rather than 0.0 -- the two are opposites (no information
                # vs. a projection of zero games played). `bind` then resolves None to the curve,
                # because the fitted prior is what supplies the volume for those players
                # downstream, so the curve IS their no-override figure.
                counterfactual = (
                    None if s.expected_games is None else min(float(s.expected_games), cap)
                )
                binding = bind_playing_time(override, source_games=counterfactual, curve=cap)
                bindings[s.player_id] = binding
                capped[s.player_id] = _dc_replace(s, expected_games=binding.now)
                continue
            source_games = float(s.expected_games or 0.0)
            capped[s.player_id] = (
                _dc_replace(s, expected_games=min(source_games, cap))
                if source_games > cap
                else s
            )
    return [capped[s.player_id] for s in seasons], bindings


def _resolve_espn_statlines(cw: Crosswalk) -> dict[str, StatLine]:
    """ESPN's projected statlines, keyed by the crosswalk's pid (not ESPN's own id).

    Best-effort: no cached ESPN payload is a normal, expected state (this source is optional
    enrichment, not part of the core ADP x Sleeper valuation), so it degrades to "no ESPN data"
    with a warning rather than failing the whole board build.
    """
    try:
        raw = load_latest_raw("espn")
    except FileNotFoundError:
        log.warning("no cached ESPN payload under data/raw/espn/; cross-source disagreement "
                    "will run on Sleeper + FantasyPros only wherever ESPN is missing")
        return {}

    players_raw = raw.get("players", []) if isinstance(raw, dict) else []
    refs = espn_client.to_player_refs(players_raw, SEASON)
    espn_statlines = espn_client.to_statlines(players_raw, SEASON)

    out: dict[str, StatLine] = {}
    for espn_id, ref in refs.items():
        entry = cw.resolve_espn_row(espn_id, ref.name, ref.team, ref.pos, espn_id=espn_id)
        statline = espn_statlines.get(espn_id)
        if entry.pid is not None and statline is not None:
            out[entry.pid] = statline
    return out


def _resolve_fantasysharks_statlines(cw: Crosswalk) -> dict[str, StatLine]:
    """FantasySharks' projected statlines, keyed by the crosswalk's pid.

    Best-effort in the same spirit as the two resolvers around it: no cached payload, or one
    whose served HTML has drifted from ``POSITION_LAYOUTS``, degrades to "no FantasySharks data"
    with a warning rather than failing the whole board build.

    Two details specific to this source, both of them the adapter's own findings (see
    ``docs/FANTASYSHARKS.md``):

    * Its player ids appear in NO id crosswalk, so ``resolve_fantasysharks_row`` takes no
      ``extra_ids`` and every row joins on name+team+pos or fuzzy. Measured 98.8% resolution
      (510 of 516), with the 6 misses all fullbacks Sleeper classifies ``FB`` -- outside
      ``SKILL_POSITIONS`` entirely, so a scope fact rather than a crosswalk defect.
    * It publishes THRESHOLD-CLEARING GAME COUNTS alongside the component stats. Those are
      deliberately NOT read here: they are not canonical component stats and must never be
      blended as if they were. Their consumer is ``tools/validate_bonus_vs_sharks.py``.
    """
    try:
        payload = fantasysharks_client.load_cached()
    except (FileNotFoundError, fantasysharks_client.FantasySharksError) as exc:
        log.warning(
            "no usable cached FantasySharks payload under data/raw/fantasysharks/ (%s: %s); the "
            "blend and the disagreement measure will run on the other three families wherever "
            "FantasySharks is missing", type(exc).__name__, exc,
        )
        return {}

    try:
        rows = fantasysharks_client.parse_all(fantasysharks_client.pages_of(payload))
    except fantasysharks_client.FantasySharksError as exc:
        # A ColumnLayoutError here means the served table drifted. That must be SEEN (the
        # adapter raises rather than absorbing it) but it must not take the board down.
        log.warning(
            "cached FantasySharks payload could not be parsed (%s: %s); treating this source as "
            "absent for this build -- re-run the fetch and re-verify the column layout",
            type(exc).__name__, exc,
        )
        return {}

    out: dict[str, StatLine] = {}
    for row in rows:
        entry = cw.resolve_fantasysharks_row(row.source_key, row.name, row.team, row.pos)
        if entry.pid is not None:
            out[entry.pid] = row.stats
    return out


def _resolve_fantasypros_statlines(cw: Crosswalk) -> dict[str, StatLine]:
    """FantasyPros' (manual CSV) projected statlines, keyed by the crosswalk's pid.

    Best-effort in the same spirit as `_resolve_espn_statlines`: a missing, stale, or malformed
    manual CSV degrades to "no FantasyPros data" for the disagreement measure rather than
    failing the whole board build -- the core valuation pipeline (ADP x Sleeper) has nothing to
    do with whether Marc has downloaded this week's FantasyPros export.
    """
    try:
        results = manual_csv.load_all_positions(season=SEASON)
    except manual_csv.ManualCsvError as exc:
        log.warning(
            "manual FantasyPros CSV(s) unusable for the disagreement measure (%s: %s); "
            "cross-source disagreement will run on Sleeper + ESPN only wherever FantasyPros "
            "is missing", type(exc).__name__, exc,
        )
        return {}

    out: dict[str, StatLine] = {}
    for pos, result in results.items():
        pos_u = pos.upper()
        for name_team_key, statline in result.statlines.items():
            name, _, team = name_team_key.partition("|")
            entry = cw.resolve_fantasypros_row(name_team_key, name, team, pos_u)
            if entry.pid is not None:
                out[entry.pid] = statline
    return out
