r"""The outlier REVIEW QUEUE: surface projection candidates, never reject one.

Plan ``docs/PLAN_2026-08-20.md``, "Marc's decisions, round 2". Marc: *"I'd like to have outliers
brought to me and highlighted and then we make decisions around whether to boot it or not."*
This module is the detection and aggregation half of that. It finds nothing new -- every detector
it draws on already exists and was already written up -- and its whole job is to normalise their
very different outputs into ONE candidate type, attach the number Marc actually needs to judge
each one, and rank the result so the list is readable.

**Nothing here rejects anything, at any threshold, ever.** The thresholds in this module select
what gets LOOKED AT; :mod:`draftroom.valuation.decisions` is the only thing that can remove a
number from the composite, and it does so only from a line a human wrote. That separation is
load-bearing: the scouting sweep found distance-based auto-rejection is not statistically sound
at a small number of correlated sources, and two proposed automatic corrections (the per-position
calibration shrink, identity renormalization) have already been declined in this repo for failing
to beat a dumb null of the same magnitude. A human adjudicating case by case has neither problem.

WHAT FIRES, AND HOW MUCH WEIGHT EACH ONE CARRIES
------------------------------------------------
The detectors are NOT equals, and the queue says so out loud via ``severity``. Ordered by how
strong a claim each has on Marc's attention:

``defect`` -- **contamination.** A source publishing something that is not a projection at all.
    Three kinds are implemented, all measured on cached data:
    (1) a CONSTANT masquerading as a projection -- Sleeper's ``games`` is 18.0 for all 3,111
        records, which is also one more than this league's 17 weeks;
    (2) an ALL-ZERO component statline carried with a positive games figure -- Sleeper does this
        for Ricky Pearsall, an ADP-118 receiver, and it is the reason he is the one FFC skill row
        the blend board excludes entirely;
    (3) a CROSSWALK JOIN FAILURE inside the top 200 by ADP -- either an FFC row that resolves to
        nothing (so the player cannot appear on the board at all) or a ranked player a source
        with a full-board pool has no row for.
    This class is the strongest, because it is defect rather than distance: no judgment about
    football is needed to say a flat 18.0 is not a durability projection.

``distance`` -- **one source far from the others on one stat.** The existing cross-source
    disagreement measure (:mod:`draftroom.valuation.disagreement`), localised from "this player
    is contested" down to "THIS source's number for THIS stat is the odd one out", which is the
    grain a decision has to be made at. Read the mandated caveat in that module: high
    disagreement is a real signal, low disagreement is NOT a safety signal.

``hygiene`` -- **the team accounting identity** (:mod:`draftroom.valuation.envelope`). Every
    completed pass is exactly one reception, so summed over a team ``rec == pass_cmp`` exactly.
    Two of the sources break that by up to 21%. It is real arithmetic and it stays in the queue,
    but ``docs/PLAN_2026-08-20.md``'s VERDICT section settled what may be done about it:
    **nothing automatic.** One-sided renormalization improved 2025 error and then failed to beat
    a flat haircut of identical magnitude (p=0.128 overall; the flat cut AHEAD on the top 60 by
    ADP), ordering got very slightly worse (Spearman 0.7777 -> 0.7765), and the honest per-team
    signal turned out to be **passer count, not catcher count** -- Sleeper's overage runs a
    median +18.7% on teams listing fewer than 2 projected quarterbacks versus +5.9% elsewhere.
    So every identity candidate carries its team's projected passer count, and its reason
    sentence says explicitly that no correction is warranted. It is a hygiene flag.

``badge`` -- **the TD-regression flag** (:mod:`draftroom.valuation.td_regression`). Its own
    write-up is the reason it sits last: R^2 around 0.5 outside QB passing yards, 9 flags across
    1,529 statlines when it was measured over three sources, and the only player flagged by all
    three of them is Josh Allen's rushing touchdowns, who genuinely does score 12 of them. Keep
    it visible; do not let it move a number. Its AGGREGATE sibling, ``td_source_bias``, is a
    different and much stronger thing -- see that detector's docstring.

THE COLUMN THAT DECIDES THE ORDERING
------------------------------------
Every candidate carries **board impact**: what happens to that player's draft value if the
flagged source were dropped for that stat. It is computed, not estimated -- the player's blended
statline is rebuilt with ``blend_statlines(rejected=...)``, re-scored through the board's own
scoring (bonus model included), re-capped by the availability curve and re-valued through
``compute_draft_values``, exactly as ``validate/board.py`` does it. The baseline is checked
against the board's own numbers, so a mismatch would be a test failure rather than a silent
drift.

**The queue is ranked by that, not by statistical extremity.** A 40% disagreement on the 180th
player is not worth Marc's attention; a 6% disagreement on a third-rounder is. Two deliberate
exceptions to a pure impact sort, both documented on screen:

* a defect whose impact CANNOT be computed sorts first -- an unresolved crosswalk row means the
  player has no value at all, which is the largest possible board impact and is not expressible
  as a delta;
* a candidate whose impact is exactly 0.0 still appears (a source-wide constant the pipeline
  already excludes, say) with its note explaining why nothing would change. Silence there would
  look like the check had not run.

PREP TIME ONLY
--------------
This is a PREP-phase module. Draft night opens a frozen snapshot read-only, asserts no outbound
network, and must never depend on anything here. Reads only cached files under ``data/raw/`` and
``data/manual/``; never fetches. Never run ``prep/fetch_all.py`` to "refresh" for it -- CLAUDE.md
documents that it moves what ``load_latest_raw()`` resolves to and breaks unrelated tests.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Mapping, MutableMapping, Sequence

from draftroom.config import LeagueConfig
from draftroom.prep.ffc_client import AdpRow
from draftroom.prep.schema import CANONICAL_STATS, StatLine
from draftroom.prep.scoring import score_statline_with_bonus
from draftroom.valuation.composite import (
    COMPOSITE_SOURCES,
    blend_statlines,
    games_distinct_counts,
    published_stats,
    varying_games_sources,
)
from draftroom.valuation.decisions import (
    ALL_STATS,
    RejectedIndex,
    load_decisions,
    rejected_index,
)
from draftroom.valuation.evob import compute_draft_values
from draftroom.valuation.replacement import PlayerSeason

__all__ = [
    "ALL_STATS",
    "DETECTORS",
    "DETECTOR_GROUPS",
    "DEFAULT_DISTANCE_REL_MIN",
    "DEFAULT_TD_BIAS_Z_MIN",
    "DEFAULT_TOP_ADP",
    "FLOOD_THRESHOLD",
    "SHORT_TERM_DESIGNATIONS",
    "NO_EMPIRICAL_DESIGNATION_FIT",
    "PLAYING_TIME_PSEUDO_SOURCE",
    "LONG_TERM_DESIGNATIONS",
    "SEVERITIES",
    "SEV_BADGE",
    "SEV_DEFECT",
    "SEV_DISTANCE",
    "SEV_HYGIENE",
    "Candidate",
    "Impact",
    "ImpactEngine",
    "ReviewInputs",
    "ReviewQueue",
    "collect_candidates",
    "detect_band_hygiene",
    "detect_constant_projections",
    "detect_crosswalk_failures",
    "detect_distance",
    "detect_injury_vs_expected_games",
    "detect_identity_hygiene",
    "detect_td_flags",
    "detect_td_source_bias",
    "detect_zero_statlines",
    "effective_games_by_pid",
    "is_long_term_designation",
    "load_review_inputs",
    "normalized_designation",
    "suppresses_missing_data",
    "queue_sort_key",
]

log = logging.getLogger("draftroom.valuation.candidates")

#: Season the projections are for. Same convention (and same value) as ``validate/board.py``.
SEASON = 2026
#: The one season of ACTUALS available offline, for the envelope and TD fits.
FIT_SEASON = 2025

SEV_DEFECT = "defect"
SEV_DISTANCE = "distance"
SEV_HYGIENE = "hygiene"
SEV_BADGE = "badge"
#: Strongest claim on attention first. Does NOT order the queue -- board impact does.
SEVERITIES: tuple[str, ...] = (SEV_DEFECT, SEV_DISTANCE, SEV_HYGIENE, SEV_BADGE)

#: Every value ``Candidate.detector`` can take -- the fine grain, one per distinct reason a row
#: exists. This is what the queue's counts are keyed by.
DETECTORS: tuple[str, ...] = (
    "contamination_constant",
    "contamination_zero_statline",
    "crosswalk_unresolved",
    "crosswalk_missing_source",
    "distance",
    "identity_hygiene",
    "band_hygiene",
    "td_source_bias",
    "td_regression",
    "injury_vs_expected_games",
)

#: What ``collect_candidates(include=...)`` selects on, and what ``ReviewQueue.skipped`` is
#: keyed by. Coarser than :data:`DETECTORS` because two pairs share one expensive fit: the two
#: crosswalk shapes come out of one pass over the resolution table, and the identity and band
#: checks come out of ONE aggregation of the 37 MB ESPN payload.
DETECTOR_GROUPS: Mapping[str, tuple[str, ...]] = {
    "contamination_constant": ("contamination_constant",),
    "contamination_zero_statline": ("contamination_zero_statline",),
    "crosswalk": ("crosswalk_unresolved", "crosswalk_missing_source"),
    "distance": ("distance",),
    "envelope": ("identity_hygiene", "band_hygiene"),
    "td_regression": ("td_source_bias", "td_regression"),
    "injury": ("injury_vs_expected_games",),
}

#: |aggregate z| a source's whole-board touchdown level must clear to be shown. Two sigma, a
#: display convention rather than a fitted constant -- and, like every threshold in this module,
#: it decides what gets LOOKED AT and never what gets dropped.
DEFAULT_TD_BIAS_Z_MIN = 2.0

#: A source's value must sit at least this far from the other sources' median, as a share of the
#: larger of the two figures, before the pair is put in front of Marc. A DISPLAY threshold: it
#: decides what is looked at, never what is rejected. 0.30 was chosen to keep the distance
#: detector from flooding the page and is a knob on the tool, not a fitted constant.
DEFAULT_DISTANCE_REL_MIN = 0.30

#: Sleeper's own ``injury_status`` vocabulary, read off the cached universe rather than assumed
#: (measured 2026-08-20: the only non-empty values anywhere in the skill-position universe are
#: ``DNR``, ``IR``, ``NA``, ``Out``, ``PUP``, ``Questionable`` and ``Sus``). Split into two sets
#: because they mean completely different things to a season projection.
#:
#: LONG TERM -- a will-not-play or reduced-play designation that a SEASON projection has to price
#: in. ``Sus`` is Sleeper's abbreviation for suspended; both spellings are accepted.
LONG_TERM_DESIGNATIONS: frozenset[str] = frozenset(
    {"IR", "PUP", "NA", "DNR", "OUT", "SUS", "SUSPENDED"}
)

#: SHORT TERM -- a weekly game-status tag. Deliberately NEVER flagged: 28 of the 33 designated
#: players in the ranked pool carry ``Questionable`` in August, including Puka Nacua and Christian
#: McCaffrey, and a detector that fires on all of them is noise with a severity label on it.
SHORT_TERM_DESIGNATIONS: frozenset[str] = frozenset({"QUESTIONABLE", "DOUBTFUL", "PROBABLE"})

NO_EMPIRICAL_DESIGNATION_FIT = (
    "No empirical games-missed figure is fitted for any designation, and none is asserted. It is "
    "not fittable from this repo's cache: Sleeper's `injury_status` is the CURRENT (2026 "
    "preseason) designation, the only cached per-player games history is 2025 actuals out of the "
    "ESPN payload, and matching a 2026 designation to a 2025 games count measures nothing about "
    "either. The cached nflreadpy weekly CSVs carry no injury column at all (season, week, "
    "player_id, player_display_name, position, pass_yd, rush_yd, rec_yd). So this detector never "
    "says how many games a PUP designation costs -- it says only that the pipeline applied NO "
    "player-specific discount at all, which is a comparison between two numbers the pipeline "
    "already produces and needs no rule about the NFL's roster mechanics."
)


def normalized_designation(status: str | None) -> str | None:
    """Upper-cased, stripped ``injury_status``, or ``None`` when there is no designation."""
    if not status:
        return None
    cleaned = str(status).strip().upper()
    return cleaned or None


def is_long_term_designation(status: str | None) -> bool:
    """A designation a SEASON projection has to price in.

    An UNRECOGNISED non-empty designation counts as long term, deliberately. Sleeper can add a
    code at any time, and the safe direction for a detector that only ever surfaces a row is to
    show it; the row carries ``designation_recognised: False`` so the reason says so. The reverse
    default -- treating an unknown code as "nothing to see" -- would hide the very case a new
    code exists to describe.
    """
    d = normalized_designation(status)
    if d is None:
        return False
    return d not in SHORT_TERM_DESIGNATIONS


def suppresses_missing_data(status: str | None) -> bool:
    """Does this designation EXPLAIN a source publishing nothing (or all zeros) for a player?

    Stricter than :func:`is_long_term_designation` on purpose: this one gates other detectors,
    so an unrecognised code must NOT silence a real contamination finding. Only a designation
    this module recognises as long term can excuse missing data.
    """
    d = normalized_designation(status)
    return d in LONG_TERM_DESIGNATIONS

#: The ADP window CLAUDE.md's crosswalk-completeness gate is stated over.
DEFAULT_TOP_ADP = 200

#: Above this many rows from one detector, the queue records that detector as FLOODED. A queue
#: nobody can read is the same as no queue, so the count is reported rather than hidden behind
#: a truncated list.
FLOOD_THRESHOLD = 100

#: The ``source`` on a row where no source is at fault. ``crosswalk`` is the other one. Neither
#: is a real projection source, which is itself what makes the row un-rejectable: a decisions-file
#: entry naming it fails validation in ``decisions.parse_decisions``.
PLAYING_TIME_PSEUDO_SOURCE = "playing_time"

_UNIT_SEASON_POINTS = "season pts (league-scored, bonus incl.)"


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Impact:
    """What dropping one source's number would do to the board. Computed, never estimated.

    ``dv`` is the board's own draft value (``EVoB`` at ``lam=0``), so ``dv_delta`` is in real
    expected-points-above-replacement, the unit every recommendation is already denominated in.
    """

    #: "player" -- one player's value moves. "source" -- the aggregate over the whole board.
    scope: str
    #: False when there is nothing to recompute (the player is not on the board, or the number
    #: is missing rather than wrong). ``note`` then says why, and the row still appears.
    computable: bool
    note: str
    dv_before: float | None = None
    dv_after: float | None = None
    dv_delta: float = 0.0
    ppg_before: float | None = None
    ppg_after: float | None = None
    points_before: float | None = None
    points_after: float | None = None
    #: Rank by ``dv`` over the whole valued board, 1-based. ``None`` after a drop-off.
    rank_before: int | None = None
    rank_after: int | None = None
    #: True when rejecting this number leaves the player with no projection at all, so he falls
    #: off the board. A different and much larger event than a value moving.
    drops_from_board: bool = False
    #: Players whose ``dv`` moved by more than a rounding error. 0 or 1 at player scope.
    n_players_moved: int = 0
    #: At source scope, the single player who moves most (name), for the one-line summary.
    worst_player: str = ""

    @property
    def magnitude(self) -> float:
        """The scalar the queue is ranked by: absolute movement in draft value."""
        return abs(self.dv_delta)

    @property
    def rank_delta(self) -> int | None:
        if self.rank_before is None or self.rank_after is None:
            return None
        return self.rank_after - self.rank_before

    def describe(self) -> str:
        if not self.computable:
            return self.note
        if self.drops_from_board:
            return (
                f"drops off the board entirely (was dv {self.dv_before:.1f}, "
                f"rank {self.rank_before})"
            )
        if self.scope == "source":
            return (
                f"dv moves on {self.n_players_moved} player(s); largest single move "
                f"{self.dv_delta:+.1f} ({self.worst_player})"
            )
        rank = f", rank {self.rank_before} -> {self.rank_after}" if self.rank_after else ""
        return f"dv {self.dv_before:.1f} -> {self.dv_after:.1f} ({self.dv_delta:+.1f}){rank}"


@dataclass(frozen=True)
class Candidate:
    """One ``(source, stat, player)`` put in front of Marc, with everything needed to judge it.

    ``player_id is None`` widens the grain to the whole ``(source, stat)`` -- the shape
    ``blend_statlines`` accepts natively, used for a source-wide defect. ``stat == "*"`` widens
    it to every canonical stat for that player, used when no single stat is the problem.
    """

    source: str
    stat: str
    player_id: str | None
    player_name: str
    pos: str
    team: str
    adp: float | None
    #: Every source's number for this stat. ``None`` = that source has no row for this player.
    #: A source that structurally publishes no such column is in ``unpublished_by`` instead --
    #: "no column" and "no row" are different facts and must not render the same.
    values_by_source: Mapping[str, float | None]
    unpublished_by: tuple[str, ...]
    #: What the numbers in ``values_by_source`` are (the stat name, or season points for "*").
    value_label: str
    #: The detector with the strongest claim on attention among those that fired for this key.
    detector: str
    severity: str
    #: Why it fired, one plain sentence per detector that fired.
    reason: str
    impact: Impact | None = None
    #: ``detector name -> that detector's computed numbers`` (z, passer count, deviation...).
    #: Always nested by detector, even for a single one, so a row that grows a second detector
    #: does not change the shape of the field.
    detail: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    #: Every detector that fired for this ``(source, stat, player)``, strongest severity first.
    #: More than one is common and is not a bug: a FantasySharks passing-touchdown figure can be
    #: both the odd one out across sources AND outside its own yardage's fitted dispersion. The
    #: queue carries ONE row per decision key, because a decision is per key -- two rows would
    #: mean two contradictory decisions about the same number.
    detectors: tuple[str, ...] = ()
    #: False when keep/reject is not the right response, so the page shows the row without a
    #: control. A crosswalk join failure is the case: the number is MISSING, not wrong, so
    #: rejecting it is a no-op and the fix is a ``data/overrides.csv`` entry. Recording a
    #: rejection that provably changes nothing would put noise in the audit trail.
    actionable: bool = True

    @property
    def key(self) -> tuple[str, str, str | None]:
        """The decision key. Same tuple :class:`draftroom.valuation.decisions.Decision` uses."""
        return (self.source, self.stat, self.player_id)

    @property
    def scope(self) -> str:
        return "source" if self.player_id is None else "player"

    @property
    def magnitude(self) -> float:
        return self.impact.magnitude if self.impact else 0.0

    def row_id(self) -> str:
        """A stable id for one page row. Equals the decision key for actionable rows.

        Non-actionable rows can share a decision key (every unresolved FFC row is
        ``("crosswalk", "*", None)``), so the player name is folded in to keep page ids unique.
        Those rows never become decisions, so the shared key never materialises in the file.
        """
        base = f"{self.source}|{self.stat}|{self.player_id or '*'}"
        return base if self.player_id else f"{base}|{self.player_name}"


@dataclass(frozen=True)
class ReviewQueue:
    """The whole queue, plus enough about how it was built to read it honestly."""

    candidates: tuple[Candidate, ...]
    #: Detector findings BEFORE merging by decision key. ``len(candidates)`` is the number of
    #: pending decisions; this is the number of times a detector fired.
    n_findings: int
    counts_by_detector: Mapping[str, int]
    counts_by_severity: Mapping[str, int]
    #: Detectors that produced more than :data:`FLOOD_THRESHOLD` rows. Named, never hidden.
    flooded: tuple[str, ...]
    #: Detectors that could not run at all (a missing cache, usually) and why.
    skipped: Mapping[str, str]
    board_source: str
    n_board_players: int
    sources: tuple[str, ...]
    notes: tuple[str, ...] = ()
    #: ``detector -> rows a will-not-play designation explained away``. An all-zero statline for
    #: an IR player is a correct projection, not contamination, and a source declining to publish
    #: him is a source being right. Counted rather than silent, because a suppression Marc cannot
    #: see is indistinguishable from a detector that stopped working.
    suppressed_by_injury: Mapping[str, int] = field(default_factory=dict)
    #: ``player_id -> the override's own description``, for players who carried an
    #: ``injury_vs_expected_games`` row until Marc set their games by hand
    #: (:mod:`draftroom.valuation.playing_time`). Reported for the same reason
    #: ``suppressed_by_injury`` is: the row is gone because the question was answered, and that
    #: is a different thing from a detector going quiet.
    settled_by_override: Mapping[str, str] = field(default_factory=dict)

    def top(self, n: int) -> tuple[Candidate, ...]:
        return self.candidates[:n]

    def by_detector(self, detector: str) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.detector == detector)


@dataclass(frozen=True)
class ReviewInputs:
    """Everything the detectors read, resolved once. Cached files only -- no network."""

    cfg: LeagueConfig
    #: The blend board (the default board, so the one whose values a decision would move).
    board: object
    #: source -> pid -> that source's resolved statline.
    statlines_by_source: Mapping[str, Mapping[str, StatLine]]
    pos_of: Mapping[str, str]
    name_of: Mapping[str, str]
    team_of: Mapping[str, str]
    adp_of: Mapping[str, float]
    games_sources: frozenset[str]
    #: Unresolved crosswalk rows, as ``prep.crosswalk.Crosswalk.unresolved_report()`` returns.
    unresolved: tuple[Mapping[str, object], ...]
    #: FFC skill rows that never resolved to a pid -- players the board cannot see at all.
    unresolved_ffc: tuple[AdpRow, ...]
    #: The raw cached ESPN payload, kept so the envelope and TD fits do not re-read 37 MB twice.
    espn_raw: Mapping[str, object] | None
    bonus_schedule: Mapping[str, object] | None
    bonus_curves: Mapping[object, object] | None
    #: Per-source distinct-positive-``games`` counts, the evidence for the constant detector.
    games_distinct: Mapping[str, int]
    #: source -> how many of its statlines are all-zero with a positive games figure, over the
    #: source's WHOLE pool (not just the ranked players the queue reports individually).
    zero_statline_totals: Mapping[str, int] = field(default_factory=dict)
    #: Sleeper's own injury/practice-report fields, keyed by the crosswalk pid -- the SAME cached
    #: universe file ``live_data.load_player_pool`` reads them from, so the queue and the board UI
    #: can never disagree about a player's designation. Empty when the cache is missing.
    injury_status: Mapping[str, str | None] = field(default_factory=dict)
    practice_participation: Mapping[str, str | None] = field(default_factory=dict)
    depth_chart_order: Mapping[str, int | None] = field(default_factory=dict)
    #: The decisions ALREADY in force (``data/projection_decisions.json``), as the board was
    #: built with. The impact engine must union these into every hypothetical rejection: without
    #: them it rebuilt each statline from raw sources, silently RESTORING earlier rejections, so
    #: reviewing a second candidate for an already-adjudicated player showed the first decision
    #: being undone and blamed the movement on the new one (Codex 2026-08-21 finding 8).
    rejections: RejectedIndex = field(default_factory=RejectedIndex.empty)

    def designation(self, pid: str) -> str | None:
        return normalized_designation(self.injury_status.get(pid))

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(s for s in COMPOSITE_SOURCES if s in self.statlines_by_source)

    def by_source_for(self, pid: str) -> dict[str, StatLine | None]:
        return {s: self.statlines_by_source[s].get(pid) for s in self.sources}


# ---------------------------------------------------------------------------
# Loading (cached files only)
# ---------------------------------------------------------------------------


def load_review_inputs(
    cfg: LeagueConfig | None = None, *, season: int = SEASON
) -> ReviewInputs:
    """Resolve every source onto the crosswalk and build the blend board, from cache only.

    The four sources are joined by ``validate/board.py``'s OWN resolvers rather than by a second
    copy of that logic, so the queue can never disagree with the board about which pid a source
    row belongs to. FantasySharks is looked up through ``getattr`` because that resolver landed
    the same day this module did: if it is absent, the source is simply skipped with a note,
    which is a smaller failure than a crash.
    """
    from draftroom.prep.crosswalk import DYNASTYPROCESS_SOURCE, build_crosswalk
    from draftroom.prep.ffc_client import parse_adp_rows
    from draftroom.prep.http import load_latest_raw
    from draftroom.prep.sleeper_client import (
        SKILL_POSITIONS,
        filter_active_skill_players,
        to_statlines as sleeper_to_statlines,
    )
    from draftroom.valuation.bonuses import load_bonus_schedule, load_curves
    from draftroom.validate import board as board_mod

    cfg = cfg or LeagueConfig.from_yaml()

    sleeper_raw = load_latest_raw("sleeper")
    ffc_rows = list(parse_adp_rows(load_latest_raw("ffc")))
    try:
        dp_csv = load_latest_raw(DYNASTYPROCESS_SOURCE)
    except FileNotFoundError:
        dp_csv = None
        log.warning("no cached DynastyProcess crosswalk; stage-1 ID matching is Sleeper-only")
    cw = build_crosswalk(sleeper_raw, ffc_rows, dynastyprocess_csv_text=dp_csv)

    universe = filter_active_skill_players(sleeper_raw)
    pos_of = {pid: ref.pos.upper() for pid, ref in universe.items()}
    name_of = {pid: ref.name for pid, ref in universe.items()}
    team_of = {pid: (ref.team or "").upper() for pid, ref in universe.items()}

    statlines_by_source: dict[str, dict[str, StatLine]] = {
        "sleeper": dict(sleeper_to_statlines(load_latest_raw("sleeper_projections"))),
        "espn": dict(board_mod._resolve_espn_statlines(cw)),
        "fantasypros": dict(board_mod._resolve_fantasypros_statlines(cw)),
    }
    fs_resolver = getattr(board_mod, "_resolve_fantasysharks_statlines", None)
    if fs_resolver is not None:
        statlines_by_source["fantasysharks"] = dict(fs_resolver(cw))
    else:  # pragma: no cover - only while the board's fourth-source resolver is mid-build
        log.warning(
            "validate/board.py has no _resolve_fantasysharks_statlines; the review queue will "
            "run over the sources it does expose"
        )
    statlines_by_source = {
        s: lines for s, lines in statlines_by_source.items() if s in COMPOSITE_SOURCES
    }

    # ADP per pid, straight off the FFC entries the crosswalk just resolved (same derivation as
    # tools/verify_fantasysharks.py). Lowest ADP wins if a player somehow resolved twice.
    adp_of: dict[str, float] = {}
    for (source, _key), entry in cw.entries.items():
        if source == "ffc" and entry.pid is not None and entry.adp is not None:
            adp_of[entry.pid] = min(entry.adp, adp_of.get(entry.pid, entry.adp))

    unresolved_ffc = tuple(
        row
        for row in ffc_rows
        if (row.pos or "").strip().upper() in SKILL_POSITIONS
        and cw.resolve(
            "ffc",
            str(row.player_id) if row.player_id is not None else f"{row.name}|{row.team}|{row.pos}",
        )
        is None
    )

    try:
        bonus_schedule = load_bonus_schedule()
        bonus_curves = load_curves()
    except FileNotFoundError:
        bonus_schedule = None
        bonus_curves = None
        log.warning("no fitted bonus curves cached; impact is computed without the bonus term")

    try:
        espn_raw = load_latest_raw("espn")
    except FileNotFoundError:
        espn_raw = None

    board = board_mod.build_real_board(cfg, source="blend")
    # The SAME decisions the board was just built with. Read here rather than pulled off the
    # board so the impact engine's rejection set and the board's are provably one call apart.
    standing = rejected_index(load_decisions())

    return ReviewInputs(
        cfg=cfg,
        board=board,
        statlines_by_source=statlines_by_source,
        pos_of=pos_of,
        name_of=name_of,
        team_of=team_of,
        adp_of=adp_of,
        games_sources=varying_games_sources(statlines_by_source),
        unresolved=tuple(cw.unresolved_report()),
        unresolved_ffc=unresolved_ffc,
        espn_raw=espn_raw,
        bonus_schedule=bonus_schedule,
        bonus_curves=bonus_curves,
        rejections=standing,
        games_distinct=games_distinct_counts(statlines_by_source),
        zero_statline_totals={
            s: sum(
                1 for line in lines.values() if line.games > 0 and not line.has_nonzero_stats()
            )
            for s, lines in statlines_by_source.items()
        },
        injury_status={pid: (sleeper_raw.get(pid) or {}).get("injury_status") for pid in pos_of},
        practice_participation={
            pid: (sleeper_raw.get(pid) or {}).get("practice_participation") for pid in pos_of
        },
        depth_chart_order={pid: _int_or_none(sleeper_raw, pid) for pid in pos_of},
    )


def _int_or_none(sleeper_raw: Mapping[str, object], pid: str) -> int | None:
    dco = (sleeper_raw.get(pid) or {}).get("depth_chart_order")
    return int(dco) if isinstance(dco, (int, float)) else None


# ---------------------------------------------------------------------------
# Board impact
# ---------------------------------------------------------------------------


def _stats_covered(stat: str) -> tuple[str, ...]:
    return CANONICAL_STATS if stat == ALL_STATS else (stat,)


class ImpactEngine:
    """Recompute the board with one ``(source, stat[, player])`` rejected, and diff it.

    The recomputation walks the SAME path ``validate/board.py`` walks -- ``blend_statlines`` ->
    ``score_statline_with_bonus`` -> ``_games_divisor`` -> ``_cap_expected_games_by_curve`` ->
    ``compute_draft_values``, calling that module's own helpers rather than reimplementing them.
    The baseline is derived the same way and is asserted equal to the board's own ``dv`` in the
    test suite, so a drift between this and the board is a red test rather than a wrong column.

    ``sigma_ppg`` is carried over unchanged from the baseline season. Dropping a source really
    would change the cross-source spread, but the board runs at ``lam=0``, where
    ``dv == evob`` and sigma cannot move a value at all -- so recomputing it would add a number
    to the page that provably changes nothing.
    """

    def __init__(self, inputs: ReviewInputs) -> None:
        from draftroom.validate import board as board_mod

        self._board_mod = board_mod
        self.inputs = inputs
        self.cfg = inputs.cfg
        board = inputs.board
        self._seasons: list[PlayerSeason] = list(board.seasons)
        self._index = {s.player_id: i for i, s in enumerate(self._seasons)}
        # Marc's playing-time overrides have to be carried through BOTH the baseline and every
        # counterfactual, or the impact column would lie for an overridden player: the baseline
        # seasons already have his figure applied (re-applying it is idempotent, since the
        # override is already curve-clamped), but `_rebuild_season` derives expected_games from
        # the statline and would silently hand that player back his source's games figure.
        self._overrides = dict(getattr(board, "playing_time_overrides", {}) or {})
        self._baseline_dv = self._value(self._seasons)
        self._baseline_rank = self._ranks(self._baseline_dv)

    # -- internals ---------------------------------------------------------

    def _value(self, seasons: Sequence[PlayerSeason]):
        capped, _ = self._board_mod._cap_expected_games_by_curve(
            list(seasons), self.cfg, overrides=self._overrides
        )
        return compute_draft_values(capped, self.cfg)

    @staticmethod
    def _ranks(dv_map) -> dict[str, int]:
        ordered = sorted(dv_map.values(), key=lambda d: -d.dv)
        return {d.player_id: i for i, d in enumerate(ordered, start=1)}

    def _rebuild_season(self, pid: str, statline: StatLine) -> PlayerSeason:
        base = self._seasons[self._index[pid]]
        divisor = self._board_mod._games_divisor(statline, self.cfg)
        points = score_statline_with_bonus(
            statline.as_dict(),
            self.cfg.scoring,
            pos=base.pos,
            games=divisor,
            bonus_schedule=self.inputs.bonus_schedule,
            bonus_curves=self.inputs.bonus_curves,
        )
        return PlayerSeason(
            player_id=pid,
            pos=base.pos,
            ppg=points / divisor,
            expected_games=(statline.games if statline.games > 0 else None),
            sigma_ppg=base.sigma_ppg,
            name=base.name,
        )

    def _blend(self, pid: str, rejected) -> tuple[StatLine, bool]:
        """Blend this player, with ``rejected`` applied ON TOP of the standing decisions.

        The union is the whole point. `rejected=()` here means "the board as it is today",
        not "the board with nothing ever rejected" -- the latter made the before/after columns
        describe undoing Marc's earlier decisions (Codex 2026-08-21 finding 8).
        """
        pos = self.inputs.pos_of.get(pid) or self._seasons[self._index[pid]].pos
        standing = self.inputs.rejections.for_player(pid)
        line, _prov = blend_statlines(
            self.inputs.by_source_for(pid),
            pos=pos,
            games_sources=self.inputs.games_sources,
            rejected=frozenset(standing) | frozenset(rejected),
        )
        return line, self._board_mod._has_projection(line)

    def points_of(self, pid: str, source: str, stat: str) -> tuple[float, float] | None:
        """``(season points before, after)`` for one candidate. The cheap pre-rank.

        Costs one blend and one scoring call, against ~1.1 ms for a full board revaluation, so
        it is what a few thousand candidates get screened with before the expensive column is
        computed for the ones that survive.
        """
        if pid not in self._index:
            return None
        rejected = frozenset((source, s) for s in _stats_covered(stat))
        before, _ = self._blend(pid, ())
        after, ok = self._blend(pid, rejected)
        base = self._seasons[self._index[pid]]
        p_before = score_statline_with_bonus(
            before.as_dict(), self.cfg.scoring, pos=base.pos,
            games=self._board_mod._games_divisor(before, self.cfg),
            bonus_schedule=self.inputs.bonus_schedule, bonus_curves=self.inputs.bonus_curves,
        )
        if not ok:
            return (p_before, 0.0)
        p_after = score_statline_with_bonus(
            after.as_dict(), self.cfg.scoring, pos=base.pos,
            games=self._board_mod._games_divisor(after, self.cfg),
            bonus_schedule=self.inputs.bonus_schedule, bonus_curves=self.inputs.bonus_curves,
        )
        return (p_before, p_after)

    # -- the public column -------------------------------------------------

    def for_player(self, pid: str, source: str, stat: str) -> Impact:
        """Board impact of rejecting ``(source, stat)`` for this one player."""
        if pid not in self._index:
            return Impact(
                scope="player",
                computable=False,
                note=(
                    "not on the valued board, so there is no value to move -- which is itself "
                    "the finding: a player the board cannot see cannot be drafted against it"
                ),
            )
        base = self._seasons[self._index[pid]]
        rejected = frozenset((source, s) for s in _stats_covered(stat))
        after_line, still_projected = self._blend(pid, rejected)

        dv_before = self._baseline_dv[pid].dv
        rank_before = self._baseline_rank[pid]
        pair = self.points_of(pid, source, stat)
        points_before = pair[0] if pair else None

        if not still_projected:
            return Impact(
                scope="player",
                computable=True,
                note=(
                    "rejecting this leaves the player with no projection from any source, so he "
                    "falls off the board entirely rather than being revalued"
                ),
                dv_before=dv_before,
                dv_after=None,
                dv_delta=-dv_before,
                ppg_before=base.ppg,
                ppg_after=None,
                points_before=points_before,
                points_after=None,
                rank_before=rank_before,
                rank_after=None,
                drops_from_board=True,
                n_players_moved=1,
            )

        new_season = self._rebuild_season(pid, after_line)
        seasons = list(self._seasons)
        seasons[self._index[pid]] = new_season
        dv_map = self._value(seasons)
        ranks = self._ranks(dv_map)
        dv_after = dv_map[pid].dv
        return Impact(
            scope="player",
            computable=True,
            note="",
            dv_before=dv_before,
            dv_after=dv_after,
            dv_delta=dv_after - dv_before,
            ppg_before=base.ppg,
            ppg_after=new_season.ppg,
            points_before=points_before,
            points_after=(pair[1] if pair else None),
            rank_before=rank_before,
            rank_after=ranks[pid],
            n_players_moved=1 if abs(dv_after - dv_before) > 1e-9 else 0,
        )

    def for_source(self, source: str, stat: str) -> Impact:
        """Board impact of rejecting ``(source, stat)`` for EVERY player -- the aggregate."""
        rejected = frozenset((source, s) for s in _stats_covered(stat))
        seasons: list[PlayerSeason] = []
        dropped: list[str] = []
        for base in self._seasons:
            line, ok = self._blend(base.player_id, rejected)
            if not ok:
                dropped.append(base.name or base.player_id)
                continue
            seasons.append(self._rebuild_season(base.player_id, line))
        dv_map = self._value(seasons)

        moved: list[tuple[float, str]] = []
        for base in self._seasons:
            before = self._baseline_dv[base.player_id].dv
            after = dv_map[base.player_id].dv if base.player_id in dv_map else 0.0
            if abs(after - before) > 1e-9:
                moved.append((after - before, base.name or base.player_id))
        moved.sort(key=lambda t: -abs(t[0]))
        worst_delta, worst_name = moved[0] if moved else (0.0, "")
        note = ""
        if not moved:
            note = (
                "no value on the board moves: the pipeline already excludes this number, so the "
                "row is a hygiene record rather than a pending change"
            )
        if dropped:
            note = (note + " " if note else "") + (
                f"{len(dropped)} player(s) would lose every projection: {', '.join(dropped[:4])}"
            )
        return Impact(
            scope="source",
            computable=True,
            note=note,
            dv_delta=worst_delta,
            n_players_moved=len(moved),
            worst_player=worst_name,
            drops_from_board=bool(dropped),
        )


# ---------------------------------------------------------------------------
# Shared helpers for building a candidate
# ---------------------------------------------------------------------------


def _values_for_stat(
    inputs: ReviewInputs, pid: str, stat: str, pos: str
) -> tuple[dict[str, float | None], tuple[str, ...], str]:
    """``(source -> value, sources with no such column, label)`` for one player and stat.

    ``stat == "*"`` reports each source's league-scored SEASON POINTS instead, because "the
    source's number" for a whole statline is its point total, and that is also what the board's
    own ``points_by_source`` shows.
    """
    if stat == ALL_STATS:
        per_source = dict(getattr(inputs.board, "points_by_source", {}).get(pid, {}))
        values: dict[str, float | None] = {
            s: per_source.get(s) for s in inputs.sources
        }
        return values, (), _UNIT_SEASON_POINTS

    values = {}
    unpublished: list[str] = []
    for s in inputs.sources:
        line = inputs.statlines_by_source[s].get(pid)
        if stat not in published_stats(s, pos):
            unpublished.append(s)
            values[s] = None
            continue
        values[s] = float(getattr(line, stat)) if line is not None else None
    return values, tuple(unpublished), stat


def _meta(inputs: ReviewInputs, pid: str) -> tuple[str, str, str, float | None]:
    """``(name, pos, team, adp)`` for a pid, from the crosswalk spine."""
    return (
        inputs.name_of.get(pid, pid),
        inputs.pos_of.get(pid, ""),
        inputs.team_of.get(pid, ""),
        inputs.adp_of.get(pid),
    )


def _candidate(
    inputs: ReviewInputs,
    *,
    source: str,
    stat: str,
    pid: str | None,
    detector: str,
    severity: str,
    reason: str,
    detail: Mapping[str, object] | None = None,
    values: Mapping[str, float | None] | None = None,
    value_label: str | None = None,
) -> Candidate:
    nested = {detector: dict(detail or {})}
    if pid is None:
        return Candidate(
            source=source,
            stat=stat,
            player_id=None,
            player_name="(every player)",
            pos="",
            team="",
            adp=None,
            values_by_source=dict(values or {}),
            unpublished_by=(),
            value_label=value_label or stat,
            detector=detector,
            severity=severity,
            reason=reason,
            detail=nested,
            detectors=(detector,),
        )
    name, pos, team, adp = _meta(inputs, pid)
    derived, unpublished, label = _values_for_stat(inputs, pid, stat, pos)
    if values is not None:
        derived, unpublished = dict(values), ()
    values = derived
    label = value_label or label
    return Candidate(
        source=source,
        stat=stat,
        player_id=pid,
        player_name=name,
        pos=pos,
        team=team,
        adp=adp,
        values_by_source=values,
        unpublished_by=unpublished,
        value_label=label,
        detector=detector,
        severity=severity,
        reason=reason,
        detail=nested,
        detectors=(detector,),
    )


# ---------------------------------------------------------------------------
# Detector 1: a constant masquerading as a projection
# ---------------------------------------------------------------------------


def detect_constant_projections(inputs: ReviewInputs) -> list[Candidate]:
    """A source publishing ONE value for every player is not publishing a projection.

    Measured, not asserted: :func:`composite.games_distinct_counts` counts distinct positive
    values per source over that source's own resolved pool. Exactly one distinct value is the
    signature. Sleeper's ``games`` is 18.0 on all 3,111 records -- and 18 is also one more than
    this league's 17 weeks, which is a second, independent reason to distrust it as a durability
    number.

    Emitted at ``player_id = None`` (source-wide), because a constant is a property of the
    source's whole file and not of any one player. The composite already refuses to blend it
    (:func:`composite.varying_games_sources`), so the impact column will honestly read "nothing
    moves" -- the row exists so the exclusion is visible and Marc can confirm it, not to propose
    a change.
    """
    out: list[Candidate] = []
    for source, n_distinct in sorted(inputs.games_distinct.items()):
        if n_distinct != 1:
            continue
        lines = inputs.statlines_by_source[source]
        values = {round(float(sl.games), 6) for sl in lines.values() if sl.games > 0}
        constant = next(iter(values))
        n_records = sum(1 for sl in lines.values() if sl.games > 0)
        weeks = float(inputs.cfg.weeks)
        excluded = source not in inputs.games_sources
        reason = (
            f"{source} publishes the same games figure ({constant:g}) for all {n_records} of its "
            f"players, so it carries no player-specific durability information"
            + (f", and {constant:g} exceeds this league's own {weeks:g} weeks" if constant > weeks else "")
            + (
                "; the composite already excludes it, so this row is a hygiene confirmation"
                if excluded
                else "; it is currently being blended"
            )
            + "."
        )
        out.append(
            _candidate(
                inputs,
                source=source,
                stat="games",
                pid=None,
                detector="contamination_constant",
                severity=SEV_DEFECT,
                reason=reason,
                values={
                    s: (float(n) if n is not None else None)
                    for s, n in inputs.games_distinct.items()
                },
                value_label=(
                    "distinct positive `games` values published (1 = a blanket constant, "
                    "0 = no games column at all)"
                ),
                detail={
                    "constant": constant,
                    "n_records": n_records,
                    "league_weeks": weeks,
                    "already_excluded_by_composite": excluded,
                    "distinct_positive_values": n_distinct,
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Detector 2: an all-zero statline carried with a positive games figure
# ---------------------------------------------------------------------------


def detect_zero_statlines(
    inputs: ReviewInputs, *, suppressed: MutableMapping[str, int] | None = None
) -> list[Candidate]:
    """A row that says "this player exists and will play, and will do nothing".

    A source claiming a positive games figure while every component stat is zero is not
    projecting a zero season -- it is carrying a placeholder.

    GATED ON INJURY STATUS, and this is the point of the gate: an all-zero statline for a healthy
    starter is contamination, and for a player on IR it is **the truth**. Sleeper's Ricky Pearsall
    row (ADP 118, ``games`` 18.0 with every component stat 0.0) was the top finding in this queue
    until Marc supplied what the data could not -- Pearsall is out for the season -- at which
    point it stopped being a defect and became a correct projection carrying a wrong games figure.
    Without the gate every IR player generates a top-ranked false alarm here and three more in
    ``crosswalk_missing_source``, which is four rows of noise per injured player crowding out real
    findings. Suppressions are COUNTED and reported on the queue, never silent.

    Reported per player for players in the ADP feed only. The whole-pool count travels in the
    detail -- Sleeper carries thousands, because every unprojected player in its universe looks
    like this, and 3,000 rows of "not projected" is not a review queue.
    """
    out: list[Candidate] = []
    for source in inputs.sources:
        total = inputs.zero_statline_totals.get(source, 0)
        for pid, line in inputs.statlines_by_source[source].items():
            if pid not in inputs.adp_of:
                continue
            if line.games <= 0 or line.has_nonzero_stats():
                continue
            if suppresses_missing_data(inputs.injury_status.get(pid)):
                # A will-not-play designation EXPLAINS an all-zero projection. The playing-time
                # detector picks the player up instead, where the finding actually lives.
                if suppressed is not None:
                    suppressed["contamination_zero_statline"] = (
                        suppressed.get("contamination_zero_statline", 0) + 1
                    )
                continue
            name, _pos, _team, adp = _meta(inputs, pid)
            out.append(
                _candidate(
                    inputs,
                    source=source,
                    stat=ALL_STATS,
                    pid=pid,
                    detector="contamination_zero_statline",
                    severity=SEV_DEFECT,
                    reason=(
                        f"{source} carries {name} with games={line.games:g} and every component "
                        f"stat 0.0, which is a placeholder rather than a projection for a player "
                        f"drafted at ADP {adp:.1f}." if adp is not None else
                        f"{source} carries {name} with games={line.games:g} and every component "
                        f"stat 0.0, which is a placeholder rather than a projection."
                    ),
                    detail={
                        "games": float(line.games),
                        "source_total_zero_statlines": total,
                    },
                )
            )
    return out


# ---------------------------------------------------------------------------
# Detector 3: crosswalk join failures inside the top N by ADP
# ---------------------------------------------------------------------------


def detect_crosswalk_failures(
    inputs: ReviewInputs,
    *,
    top_adp: int = DEFAULT_TOP_ADP,
    suppressed: MutableMapping[str, int] | None = None,
) -> list[Candidate]:
    """Join failures the board cannot recover from on its own.

    Two shapes, and they are not the same problem:

    * **an FFC row that resolves to nothing** -- the player cannot appear on the board at all,
      which is CLAUDE.md gate #2 ("zero unresolved players inside the top 200 by ADP") failing.
    * **a ranked player a source has no row for** -- the composite silently averages over one
      fewer source. Sometimes legitimate (the source simply does not project that deep), so the
      candidate carries that source's nearest unresolved row by fuzzy name score, which is what
      turns "missing" into "misjoined": the real find of 2026-08-20 was Kenny Gainwell at ADP
      132.8, published by FantasySharks under a fuller first name that scored 86.7 against the
      crosswalk's 90.0 floor.

    Neither is fixed by rejecting a number -- the number is absent, not wrong. The fix is an
    entry in ``data/overrides.csv``, and the reason sentence says so.

    GATED ON INJURY STATUS for the second shape: a source declining to publish a player who will
    not play is a source being CORRECT, not a join failure. Ricky Pearsall (IR) produced one such
    row per source and all three were false positives. Suppressions are counted and reported.
    """
    from draftroom.prep.crosswalk import FUZZY_THRESHOLD
    from draftroom.prep.schema import normalize_name

    out: list[Candidate] = []
    ranked = sorted(inputs.adp_of.items(), key=lambda kv: kv[1])[:top_adp]
    ranked_pids = {pid for pid, _ in ranked}

    for row in inputs.unresolved_ffc:
        if row.adp is not None and row.adp > top_adp:
            continue
        out.append(
            Candidate(
                source="crosswalk",
                stat=ALL_STATS,
                player_id=None,
                player_name=row.name,
                pos=(row.pos or "").upper(),
                team=(row.team or "").upper(),
                adp=row.adp,
                values_by_source={},
                unpublished_by=(),
                value_label=_UNIT_SEASON_POINTS,
                detector="crosswalk_unresolved",
                severity=SEV_DEFECT,
                reason=(
                    f"{row.name} is in the ADP feed at {row.adp:.1f} but resolves to no player "
                    f"id, so he cannot appear on the board at all -- fix with a "
                    f"data/overrides.csv entry, not by rejecting a number."
                ),
                detail={"crosswalk_unresolved": {"kind": "ffc_row_unresolved"}},
                detectors=("crosswalk_unresolved",),
                actionable=False,
            )
        )

    # Per source: which of its own rows failed to resolve, indexed by normalized name, so a
    # "missing" ranked player can be matched against what the source actually published.
    unresolved_by_source: dict[str, list[Mapping[str, object]]] = {}
    for entry in inputs.unresolved:
        unresolved_by_source.setdefault(str(entry.get("source")), []).append(entry)

    scorer = _fuzzy_scorer()
    for pid in ranked_pids:
        name, pos, team, adp = _meta(inputs, pid)
        if suppresses_missing_data(inputs.injury_status.get(pid)):
            # Every source that skipped him is right to have skipped him. Counted, not silent.
            if suppressed is not None:
                missing = sum(
                    1
                    for s in inputs.sources
                    if inputs.statlines_by_source[s].get(pid) is None
                )
                if missing:
                    suppressed["crosswalk_missing_source"] = (
                        suppressed.get("crosswalk_missing_source", 0) + missing
                    )
            continue
        for source in inputs.sources:
            if inputs.statlines_by_source[source].get(pid) is not None:
                continue
            near = _nearest_unresolved(
                scorer, normalize_name(name), pos, unresolved_by_source.get(source, ())
            )
            near_note = ""
            if near is not None:
                cand_name, score = near
                near_note = (
                    f" {source} did publish {cand_name!r}, which scored {score:.1f} against the "
                    f"crosswalk's {FUZZY_THRESHOLD:.0f} floor"
                )
            out.append(
                replace(
                    _candidate(
                        inputs,
                        source=source,
                        stat=ALL_STATS,
                        pid=pid,
                        detector="crosswalk_missing_source",
                        severity=SEV_DEFECT,
                        reason=(
                            f"{name} (ADP {adp:.1f}) has no {source} row, so the composite "
                            f"averages him over one fewer source than the rest of the board."
                            + (near_note + " -- a name mismatch." if near_note else "")
                            + " Nothing here is rejectable: the number is missing, not wrong, so"
                            " the fix is a data/overrides.csv entry."
                        ),
                        detail={
                            "kind": "source_has_no_row",
                            "nearest_unresolved_row": (near[0] if near else None),
                            "nearest_unresolved_score": (near[1] if near else None),
                            "fuzzy_threshold": float(FUZZY_THRESHOLD),
                        },
                    ),
                    actionable=False,
                )
            )
    return out


def _fuzzy_scorer() -> Callable[[str, str], float] | None:
    """The same fuzzy scorer the crosswalk uses, or ``None`` if rapidfuzz is unavailable."""
    try:
        from rapidfuzz import fuzz

        return lambda a, b: float(fuzz.token_sort_ratio(a, b))
    except Exception:  # noqa: BLE001 - the near-miss note is a nicety, not a requirement
        return None


def _nearest_unresolved(
    scorer: Callable[[str, str], float] | None,
    norm_name: str,
    pos: str,
    rows: Iterable[Mapping[str, object]],
) -> tuple[str, float] | None:
    if scorer is None:
        return None
    from draftroom.prep.schema import normalize_name

    best: tuple[str, float] | None = None
    for row in rows:
        if pos and str(row.get("pos", "")).upper() not in ("", pos):
            continue
        other = str(row.get("name", ""))
        score = scorer(norm_name, normalize_name(other))
        if best is None or score > best[1]:
            best = (other, score)
    if best is None or best[1] < 70.0:
        return None
    return best


# ---------------------------------------------------------------------------
# Detector 4: an injury designation the valuation never read
# ---------------------------------------------------------------------------


def effective_games_by_pid(inputs: ReviewInputs) -> dict[str, tuple[float, float, int]]:
    """``pid -> (games the board actually credited, the healthy-rank curve figure, rank)``.

    The second number is the load-bearing one. ``EXPECTED_GAMES_CURVE`` is fitted on availability
    by POSITIONAL RANK over everybody at that rank, so it is precisely the figure for a player
    about whom the pipeline knows nothing player-specific. ``validate/board.py`` then applies
    ``min(source games, curve)``, and leaves ``expected_games`` at ``None`` when no source
    published a games figure -- in which case ``resolve_players`` fills in that same curve value.

    Either way, **credited == curve means no player-specific discount was applied**. That is the
    whole comparison this detector makes, and both sides of it are numbers the pipeline already
    produced. Rank is 1-based by projected PPG within position, the same convention
    ``_cap_expected_games_by_curve`` and ``resolve_players`` use, so all three agree on who
    "rank 30" is.
    """
    from draftroom.valuation.replacement import expected_games as _curve

    by_pos: dict[str, list] = {}
    for season in getattr(inputs.board, "seasons", ()):
        by_pos.setdefault(season.pos, []).append(season)

    out: dict[str, tuple[float, float, int]] = {}
    for pos, group in by_pos.items():
        for rank, season in enumerate(sorted(group, key=lambda s: -s.ppg), start=1):
            curve = float(_curve(pos, rank=rank, weeks=inputs.cfg.weeks))
            credited = float(season.expected_games) if season.expected_games is not None else curve
            out[season.player_id] = (credited, curve, rank)
    return out


def _games_text(inputs: ReviewInputs, pid: str) -> str:
    """Each source's own games figure for this player, in words, for the reason sentence."""
    parts = []
    for s in inputs.sources:
        line = inputs.statlines_by_source[s].get(pid)
        if line is None:
            parts.append(f"{s} no row")
        elif line.games <= 0:
            parts.append(f"{s} no games column")
        else:
            parts.append(f"{s} {line.games:.1f}")
    return ", ".join(parts)


def detect_injury_vs_expected_games(
    inputs: ReviewInputs, *, ranked_only: bool = True
) -> list[Candidate]:
    """A will-not-play designation sitting next to a healthy player's playing time.

    THE GAP THIS CLOSES. ``injury_status`` is carried on ``live_data.PoolPlayer`` and emitted in
    the server payload, and it **never touches the valuation** -- grep it and it appears in
    ``live_data.py`` (carrying it) and ``server.py`` (the payload) and nowhere else. So whether a
    designation reaches ``expected_games`` depends entirely on whether ESPN happened to price it
    in, which is accidental rather than principled. Measured on the ranked pool 2026-08-20: Alec
    Pierce is on PUP at ADP 70.3 and the board credits him with 15.50 of 17 games, which is
    exactly the rank-conditional availability curve for WR30 -- the figure for a player nothing is
    known about. Ricky Pearsall (IR) came out right only by luck: he is off the board because
    Sleeper happened to zero his stat line, not because anything read his status.

    WHY THIS IS NOT ACTIONABLE. Every other detector here says "this source's number may be
    wrong", which ``blend_statlines(rejected=...)`` can express. This one says "the playing-time
    assumption for this player looks wrong", which it cannot express at all: no source is at fault
    (ESPN's 17.0 is an ordinary if-healthy projection) and dropping a source would not change an
    availability figure. So ``actionable`` is ``False``, exactly as for
    ``crosswalk_missing_source``, and the reason says the fix is a playing-time override.
    Recording a rejection that provably changes nothing would put noise in the audit trail.

    NO THRESHOLD IS INVENTED. Nothing here asserts what a PUP designation costs in games -- see
    :data:`NO_EMPIRICAL_DESIGNATION_FIT` for why that is not fittable from this repo's cache and
    is therefore not asserted. The check is a CONSISTENCY comparison between the games the board
    credited and the healthy-rank curve figure, both printed in the reason so Marc judges two
    numbers rather than a verdict. Severity splits on that comparison alone: ``defect`` when the
    discount is exactly zero, ``hygiene`` when some source did price something in and the only
    open question is whether it priced in enough -- which is the question this module has no
    number for.

    A player already OFF the board is not surfaced: his designation explains that rather than
    contradicting it, and there is no playing-time assumption left to be wrong. Short-term
    game-status tags never fire (see :data:`SHORT_TERM_DESIGNATIONS`).

    SETTLED PLAYERS DROP OUT, BUT ONLY FOR THE DESIGNATION THEY ANSWER. Once Marc has written a
    playing-time override for a player (:mod:`draftroom.valuation.playing_time`), the gap this
    detector exists to surface is closed FOR HIM: the board's games figure is now a human
    judgement rather than an unexamined healthy-rank default, which is the only thing the check
    was ever complaining about. Handing his own decision back as a fresh candidate would be
    noise, and the ``hygiene`` wording below ("a source did price in N games") would be actively
    wrong -- no source did; he did. Those players go to
    :attr:`ReviewQueue.settled_by_override` instead, because a suppression nobody can see is
    indistinguishable from a detector that stopped working.

    The suppression is DESIGNATION-SCOPED, and that is the whole difficulty. An override records
    which designation it was answering; if the player's CURRENT designation is a different one,
    the override predates the news and the row must fire again. Suppressing on the mere existence
    of an override let a 12-game judgement written for a suspension silently absorb a later IR
    designation -- the single most expensive thing this detector could get wrong, since the
    player then sits on the board at a stale figure with a badge implying somebody looked
    (Codex 2026-08-24 finding 3). An override that recorded NO designation is treated as
    answering none of them, so it never suppresses; the row fires and says an override is in
    force. This repo does not get to guess what a human meant.
    """
    games = effective_games_by_pid(inputs)
    weeks = float(inputs.cfg.weeks)
    applied = getattr(inputs.board, "applied_playing_time", {}) or {}
    settled = {
        pid
        for pid, binding in applied.items()
        if normalized_designation(binding.override.designation) is not None
        and normalized_designation(binding.override.designation)
        == normalized_designation(inputs.injury_status.get(pid))
    }

    designated = [
        pid
        for pid in (inputs.adp_of if ranked_only else inputs.pos_of)
        if is_long_term_designation(inputs.injury_status.get(pid))
    ]
    n_discounted = sum(
        1 for pid in designated if pid in games and games[pid][0] < games[pid][1] - 1e-9
    )
    n_off_board = sum(1 for pid in designated if pid not in games)

    out: list[Candidate] = []
    for pid in sorted(designated, key=lambda p: inputs.adp_of.get(p, 1e9)):
        if pid not in games:
            continue
        if pid in settled:
            continue  # Marc already set this player's games by hand -- see the docstring.
        designation = inputs.designation(pid)
        recognised = suppresses_missing_data(designation)
        credited, curve, rank = games[pid]
        discount = curve - credited
        no_discount = discount <= 1e-9
        name, pos, _team, adp = _meta(inputs, pid)
        adp_text = f"ADP {adp:.1f}" if adp is not None else "unranked"
        per_source = _games_text(inputs, pid)

        if no_discount:
            severity = SEV_DEFECT
            reason = (
                f"{name} is listed {designation} at {adp_text}, and the board credits him with "
                f"{credited:.2f} of {weeks:.0f} games -- exactly the rank-conditional "
                f"availability curve for {pos}{rank}, which is the figure for a player nothing "
                f"player-specific is known about, so no part of the pipeline discounted him for "
                f"the designation ({per_source}). No source's number is wrong here, so this is "
                f"not a rejection: the fix is a playing-time override. No games-missed figure is "
                f"asserted for {designation} -- see NO_EMPIRICAL_DESIGNATION_FIT."
            )
        else:
            severity = SEV_HYGIENE
            reason = (
                f"{name} is listed {designation} at {adp_text}, and the board credits him with "
                f"{credited:.2f} of {weeks:.0f} games against {curve:.2f} from the healthy-rank "
                f"curve for {pos}{rank}, so a source did price in {discount:.2f} games "
                f"({per_source}). Whether that is ENOUGH for a {designation} designation is the "
                f"question, and this module has no number for it -- see "
                f"NO_EMPIRICAL_DESIGNATION_FIT -- so it is shown rather than judged. Not a "
                f"rejection either way: the fix would be a playing-time override."
            )
        if not recognised:
            reason += (
                f" NOTE: {designation!r} is not in this module's designation vocabulary "
                f"{sorted(LONG_TERM_DESIGNATIONS)}, so it is surfaced on the safe side and is "
                f"NOT allowed to excuse missing data anywhere else."
            )
        stale = applied.get(pid)
        if stale is not None:
            # He has an override, and it did NOT settle this row -- otherwise `settled` would
            # have skipped him above. Say which designation it was written for, because the
            # likely reading of an unbadged-but-overridden player is "somebody looked at this".
            answered = normalized_designation(stale.override.designation)
            reason += (
                f" NOTE: a playing-time override IS in force for him ({stale.describe()}), but "
                + (
                    f"it was written for a {answered} designation and he is now listed "
                    f"{designation}"
                    if answered
                    else "it records no designation at all"
                )
                + ", so it is not treated as answering this and the row stands. Re-decide it if "
                "the new designation changes your figure."
            )

        candidate = _candidate(
            inputs,
            source=PLAYING_TIME_PSEUDO_SOURCE,
            stat="games",
            pid=pid,
            detector="injury_vs_expected_games",
            severity=severity,
            reason=reason,
            detail={
                "designation": designation,
                "designation_recognised": recognised,
                "practice_participation": inputs.practice_participation.get(pid),
                "depth_chart_order": inputs.depth_chart_order.get(pid),
                "games_credited_by_board": credited,
                "healthy_rank_curve_games": curve,
                "discount_applied": discount,
                "no_discount_applied": no_discount,
                "positional_rank": rank,
                "league_weeks": weeks,
                "n_designated_in_ranked_pool": len(designated),
                "n_designated_with_any_source_discount": n_discounted,
                "n_designated_off_board": n_off_board,
                "n_designated_settled_by_override": len(settled & set(designated)),
                "empirical_fit": NO_EMPIRICAL_DESIGNATION_FIT,
            },
        )
        shortfall = (
            "the curve value itself" if no_discount else f"{discount:.2f} below the curve"
        )
        out.append(
            replace(
                candidate,
                actionable=False,
                impact=Impact(
                    scope="player",
                    computable=False,
                    note=(
                        "no dv delta: nothing is being dropped. Rejecting a source cannot change "
                        f"an availability figure, and {credited:.2f} of {weeks:.0f} games is "
                        f"{shortfall}. Ranked by ADP among the rows whose impact is not "
                        "expressible as a delta."
                    ),
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Detector 5: cross-source distance, localised to (source, stat, player)
# ---------------------------------------------------------------------------


def detect_distance(
    inputs: ReviewInputs,
    *,
    rel_min: float = DEFAULT_DISTANCE_REL_MIN,
    ranked_only: bool = True,
) -> list[Candidate]:
    """The odd one out: one source's number far from the median of the others, on one stat.

    ``valuation/disagreement.py`` measures spread at the PLAYER level (points stdev, and per
    play-type component stdev). A decision has to be made at ``(source, stat, player)``, so this
    walks down to that grain: for every stat at least three sources publish for that player,
    take the median of the other sources and flag whichever source sits furthest from it.

    ``rel_min`` is the deviation, as a share of the larger of (the source's value, the others'
    median), above which the row is shown. It is a DISPLAY threshold on a page, not a rule -- a
    number a threshold surfaces is still only removed by a human decision. Three sources is the
    minimum because with two there is no "others' median" and therefore no odd one out, only a
    disagreement with no direction.

    The mandated caveat from ``disagreement.py`` applies unchanged and is reproduced on the page:
    high disagreement here is a real danger signal, its absence is NOT a safety signal, and four
    correlated families can be wrong together.
    """
    out: list[Candidate] = []
    pids = inputs.adp_of.keys() if ranked_only else _all_pids(inputs)
    for pid in pids:
        pos = inputs.pos_of.get(pid, "")
        lines = {s: inputs.statlines_by_source[s].get(pid) for s in inputs.sources}
        for stat in CANONICAL_STATS:
            if stat == "games":
                continue  # a games figure is the constant detector's and games_sources' business
            contributing = {
                s: float(getattr(line, stat))
                for s, line in lines.items()
                if line is not None and stat in published_stats(s, pos)
            }
            if len(contributing) < 3:
                continue
            if not any(v for v in contributing.values()):
                continue  # every source says zero: structural agreement, not disagreement
            flagged = _odd_one_out(contributing)
            if flagged is None:
                continue
            source, value, others_median, dev_rel = flagged
            if dev_rel < rel_min:
                continue
            others = {s: v for s, v in contributing.items() if s != source}
            direction = "above" if value > others_median else "below"
            out.append(
                _candidate(
                    inputs,
                    source=source,
                    stat=stat,
                    pid=pid,
                    detector="distance",
                    severity=SEV_DISTANCE,
                    reason=(
                        f"{source}'s {stat} of {value:,.1f} sits {dev_rel:.0%} {direction} the "
                        f"{others_median:,.1f} median of the other {len(others)} sources "
                        f"({', '.join(f'{s} {v:,.1f}' for s, v in sorted(others.items()))})."
                    ),
                    detail={
                        "others_median": others_median,
                        "deviation": value - others_median,
                        "deviation_rel": dev_rel,
                        "n_contributing": len(contributing),
                    },
                )
            )
    return out


def _all_pids(inputs: ReviewInputs) -> set[str]:
    pids: set[str] = set()
    for lines in inputs.statlines_by_source.values():
        pids.update(lines)
    return pids


def _odd_one_out(
    values: Mapping[str, float]
) -> tuple[str, float, float, float] | None:
    """``(source, its value, the others' median, relative deviation)`` for the furthest source.

    The deviation denominator is ``max(|value|, |median|)``, so the figure is bounded in [0, 1]
    and reads as "this share of the larger of the two numbers". A source at 0 where the others
    say 320 comes out at 1.00, which is the honest reading of that gap; dividing by the median
    alone would make a source-published zero look like any other large miss.
    """
    best: tuple[str, float, float, float] | None = None
    for source, value in values.items():
        others = [v for s, v in values.items() if s != source]
        if len(others) < 2:
            return None
        median = statistics.median(others)
        denom = max(abs(value), abs(median))
        if denom <= 1e-9:
            continue
        dev_rel = abs(value - median) / denom
        if best is None or dev_rel > best[3]:
            best = (source, value, median, dev_rel)
    return best


# ---------------------------------------------------------------------------
# Detector 5 and 6: the team accounting identity, and the fitted band
# ---------------------------------------------------------------------------


def _envelope_reports(inputs: ReviewInputs, *, top_n: int = 3):
    """``(reports per source, bandset, tolerances)`` or ``None`` when the fit is unavailable."""
    from draftroom.valuation import envelope as env

    if inputs.espn_raw is None:
        return None
    actuals = env.team_season_actuals(inputs.espn_raw, FIT_SEASON)
    tolerances = env.fit_identity_tolerances(actuals)
    _path, weekly_rows = env.load_weekly_history_rows()
    bandset = env.fit_bands(
        team_actuals=actuals,
        yardage_means=env.league_yardage_means(weekly_rows),
        fit_season=FIT_SEASON,
    )
    reports = {}
    for source, lines in inputs.statlines_by_source.items():
        usable = {pid: line for pid, line in lines.items() if line.has_nonzero_stats()}
        reports[source] = env.build_report(
            source, usable, inputs.team_of, bandset, tolerances, name_of=inputs.name_of
        )
    return reports, bandset, tolerances


def detect_identity_hygiene(
    inputs: ReviewInputs, *, reports=None, top_n: int = 3
) -> list[Candidate]:
    """Team accounting-identity overages, as HYGIENE FLAGS. No correction is implied.

    A team cannot catch more passes than it threw: summed over a team, ``rec == pass_cmp``,
    ``rec_yd == pass_yd``, ``rec_td == pass_td``, exactly. Two of the sources break that by up to
    21%, and the equal-weight blend inherits most of it.

    What was NOT allowed to follow from that, per ``docs/PLAN_2026-08-20.md``'s VERDICT section:
    a remedy. One-sided renormalization improves 2025 MAE (37.14 -> 36.04) and then loses to a
    flat haircut of identical magnitude (36.42, p=0.128, and the flat cut ahead on the top 60 by
    ADP); ordering, the only thing a board consumes, got slightly worse. The durable rule that
    came out of it -- every proposed correction must beat a dumb null of equal magnitude -- is
    why this detector only ever flags.

    Each candidate therefore carries the team's **projected passer count**, because that is the
    honest per-team signal the arbitration found: Sleeper's overage runs a median +18.7% on teams
    listing fewer than 2 projected quarterbacks against +5.9% elsewhere. The violation localises
    to a team, so the row names the ``top_n`` players contributing most of that team's total for
    the stat -- those are the numbers a decision could act on, not the team.
    """
    if reports is None:
        bundle = _envelope_reports(inputs)
        if bundle is None:
            return []
        reports = bundle[0]

    out: list[Candidate] = []
    for source, report in sorted(reports.items()):
        for check in report.identity_violations:
            sums = report.team_sums.get(check.team)
            if sums is None:
                continue
            passers = sums.count("pass_cmp")
            contributors = sums.contributors.get(check.recv_stat, ())[:top_n]
            for pid, name, value in contributors:
                out.append(
                    _candidate(
                        inputs,
                        source=source,
                        stat=check.recv_stat,
                        pid=pid,
                        detector="identity_hygiene",
                        severity=SEV_HYGIENE,
                        reason=(
                            f"{check.team}'s {source} {check.recv_stat} sums to "
                            f"{check.recv_side:,.0f} against its own {check.pass_stat} of "
                            f"{check.pass_side:,.0f} ({check.delta_pct:+.1%}, tolerance "
                            f"{check.tolerance_pct:.2%}), and {name} is {value:,.0f} of it; the "
                            f"team lists {passers} projected passer(s), which is the honest "
                            f"per-team signal -- HYGIENE FLAG ONLY, no correction is warranted "
                            f"(the measured remedy failed to beat a flat haircut of the same "
                            f"size)."
                        ),
                        detail={
                            "team": check.team,
                            "rule": check.rule,
                            "pass_stat": check.pass_stat,
                            "pass_side": check.pass_side,
                            "recv_side": check.recv_side,
                            "delta": check.delta,
                            "delta_pct": check.delta_pct,
                            "tolerance_pct": check.tolerance_pct,
                            "projected_passers": passers,
                            "player_share_of_team": (
                                value / check.recv_side if check.recv_side else None
                            ),
                            "no_correction_warranted": True,
                        },
                    )
                )
    return out


def detect_band_hygiene(
    inputs: ReviewInputs, *, reports=None, top_n: int = 3
) -> list[Candidate]:
    """Team sums above the fitted plausible band -- the other envelope check, also hygiene only.

    Honestly widened by measured league drift, this fires about once in 96 team-stat checks (the
    write-up's number: one FantasyPros team, +1.5%). Kept because a near-inert check that does
    fire is worth a look, and because its absence would be indistinguishable from not running it.
    """
    if reports is None:
        bundle = _envelope_reports(inputs)
        if bundle is None:
            return []
        reports = bundle[0]

    out: list[Candidate] = []
    for source, report in sorted(reports.items()):
        for violation in report.band_violations:
            for pid, name, value in violation.top_contributors[:top_n]:
                out.append(
                    _candidate(
                        inputs,
                        source=source,
                        stat=violation.stat,
                        pid=pid,
                        detector="band_hygiene",
                        severity=SEV_HYGIENE,
                        reason=(
                            f"{violation.team}'s {source} {violation.stat} sums to "
                            f"{violation.value:,.0f} against a fitted band high of "
                            f"{violation.band.high:,.0f} ({violation.excess_pct:+.1%}), and "
                            f"{name} is {value:,.0f} of it -- hygiene flag; the band is fitted "
                            f"on {violation.band.n_team_seasons} team-seasons and fires roughly "
                            f"once in 96 checks."
                        ),
                        detail={
                            "team": violation.team,
                            "team_value": violation.value,
                            "band_high": violation.band.high,
                            "band_low": violation.band.low,
                            "excess_pct": violation.excess_pct,
                            "drift_measured": violation.band.drift_measured,
                        },
                    )
                )
    return out


# ---------------------------------------------------------------------------
# Detector 7: the TD-regression badge
# ---------------------------------------------------------------------------


def detect_td_source_bias(
    inputs: ReviewInputs, *, z_min: float = DEFAULT_TD_BIAS_Z_MIN, modelset=None
) -> list[Candidate]:
    """A source's AGGREGATE touchdown level against what its own yardage implies.

    ``docs/PROJECTION_CHALLENGES.md`` is blunt that this is the better of the two TD mechanisms
    and "a different and better mechanism than the plan asked for": the per-player z-score asks
    a question an R^2 of 0.5 can barely answer, but summed over a hundred players the noise
    cancels and what is left is the source's RATE -- how many touchdowns it hands out per yard
    against what the league actually produced. It is also the only TD finding shaped like the
    rejection the composite can express, which is per ``(source, stat)``.

    Emitted at ``player_id = None``, because that is what the finding is about. One honest limit,
    stated in the reason: the fit is per ``(position, td_stat)`` while the rejection grain is
    ``(source, stat)`` across positions, so a source-wide reject on ``rec_td`` acts on WR, RB and
    TE together even when only one of them is biased.

    ``z_min`` is a display threshold at two sigma. It selects what is shown, not what is dropped,
    and it is a round number rather than a fitted one -- said plainly here so nobody mistakes it
    for a measurement.
    """
    from draftroom.valuation import td_regression as tdr

    if modelset is None:
        if inputs.espn_raw is None:
            return []
        modelset = tdr.fit_td_models(
            tdr.player_season_actuals(inputs.espn_raw, FIT_SEASON), seasons=[FIT_SEASON]
        )

    # Every source's aggregate for every group first, so a flagged row can show what the OTHER
    # sources project for the same group -- the whole point of a cross-source page.
    by_group: dict[tuple[str, str], dict[str, object]] = {}
    for source in inputs.sources:
        pool = {
            pid: line
            for pid, line in inputs.statlines_by_source[source].items()
            if line.has_nonzero_stats()
        }
        for bias in tdr.source_bias(source, pool, inputs.pos_of, modelset):
            by_group.setdefault((bias.pos, bias.td_stat), {})[source] = bias

    out: list[Candidate] = []
    for (pos, td_stat), biases in sorted(by_group.items()):
        totals: dict[str, float | None] = {
            s: (biases[s].projected_total if s in biases else None) for s in inputs.sources
        }
        for source, bias in sorted(biases.items()):
            if abs(bias.z) < z_min:
                continue
            direction = "more" if bias.ratio > 1 else "fewer"
            out.append(
                _candidate(
                    inputs,
                    source=source,
                    stat=bias.td_stat,
                    pid=None,
                    detector="td_source_bias",
                    severity=SEV_DISTANCE,
                    values=totals,
                    value_label=(
                        f"{bias.td_stat} summed over {pos}s above the model's usage floor "
                        f"(fitted expectation {bias.expected_total:,.0f})"
                    ),
                    reason=(
                        f"across {bias.n_players} {bias.pos}s, {source} projects "
                        f"{bias.projected_total:,.0f} {bias.td_stat} against "
                        f"{bias.expected_total:,.0f} implied by its own {bias.model.predictor} at "
                        f"the fitted {FIT_SEASON} rate -- {abs(bias.ratio - 1):.1%} {direction} "
                        f"(aggregate z {bias.z:+.2f}); a level bias in one source's whole board, "
                        f"which is the one TD finding shaped like the rejection the composite can "
                        f"express, though that rejection would act on every position at once."
                    ),
                    detail={
                        "pos": bias.pos,
                        "n_players": bias.n_players,
                        "projected_total": bias.projected_total,
                        "expected_total": bias.expected_total,
                        "ratio": bias.ratio,
                        "z": bias.z,
                        "predictor": bias.model.predictor,
                        "model_r2": bias.model.r2,
                        "z_min_is_a_display_threshold": z_min,
                    },
                )
            )
    return out


def detect_td_flags(
    inputs: ReviewInputs, *, quantile: float = 0.95, ranked_only: bool = True, modelset=None
) -> list[Candidate]:
    """Projected touchdowns outside the fitted historical dispersion. A BADGE, nothing more.

    Its own write-up settles what it is worth: R^2 0.40-0.61 for everything except QB passing
    yards, 9 flags across 1,529 statlines, and the most consistent flag (Josh Allen's rushing
    touchdowns) is a genuine outlier rather than a projection error. The threshold is a fitted
    quantile of |z| among real 2025 player-seasons, so "outlier" means "further from its own
    yardage than 95% of real seasons were" -- and a projection is an expectation, which should be
    LESS dispersed than reality, so this under-flags by construction.
    """
    from draftroom.valuation import td_regression as tdr

    if modelset is None:
        if inputs.espn_raw is None:
            return []
        modelset = tdr.fit_td_models(
            tdr.player_season_actuals(inputs.espn_raw, FIT_SEASON), seasons=[FIT_SEASON]
        )

    out: list[Candidate] = []
    for source, lines in sorted(inputs.statlines_by_source.items()):
        pool = {
            pid: line
            for pid, line in lines.items()
            if (not ranked_only or pid in inputs.adp_of) and line.has_nonzero_stats()
        }
        for flag in tdr.flag_statlines(
            pool, inputs.pos_of, modelset, name_of=inputs.name_of, quantile=quantile
        ):
            direction = "above" if flag.z > 0 else "below"
            out.append(
                _candidate(
                    inputs,
                    source=source,
                    stat=flag.td_stat,
                    pid=flag.player_id,
                    detector="td_regression",
                    severity=SEV_BADGE,
                    reason=(
                        f"{source} projects {flag.name} for {flag.projected_td:.1f} "
                        f"{flag.td_stat} against {flag.expected_td:.1f} implied by his own "
                        f"{flag.predictor} of {flag.predictor_value:,.0f} at the fitted 2025 "
                        f"rate (z {flag.z:+.2f} vs a {flag.threshold:.2f} threshold, "
                        f"{direction}) -- a badge, not a correction."
                    ),
                    detail={
                        "z": flag.z,
                        "threshold": flag.threshold,
                        "expected_td": flag.expected_td,
                        "predictor": flag.predictor,
                        "predictor_value": flag.predictor_value,
                        "model_r2": flag.model.r2,
                        "quantile": quantile,
                    },
                )
            )
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def queue_sort_key(c: Candidate) -> tuple:
    """Board impact first, in the two documented exceptions' shadow.

    A defect whose impact cannot be expressed as a delta (an unresolved crosswalk row: the
    player has no value at all) sorts ahead of everything, because "this player is invisible to
    the board" is a larger fact than any value movement. Everything else sorts by absolute
    movement in draft value, descending, then by ADP so the earlier pick wins a tie.
    """
    unmeasurable_defect = (
        c.severity == SEV_DEFECT and c.impact is not None and not c.impact.computable
    )
    return (
        0 if unmeasurable_defect else 1,
        -c.magnitude,
        c.adp if c.adp is not None else 1e9,
        c.player_name,
        c.source,
        c.stat,
    )


def _merge_by_key(findings: Sequence[Candidate]) -> list[Candidate]:
    """One row per ``(source, stat, player_id)``, carrying every detector that fired on it.

    Multiple detectors landing on the same number is the normal case, not an anomaly: a
    FantasySharks passing-touchdown figure is both the odd one out across four sources AND
    outside what its own passing yardage buys at the fitted 2025 rate. Those are two independent
    reasons to look, and exactly ONE decision. Emitting two rows would let Marc keep a number on
    one row and reject it on another.

    The surviving row takes the strongest severity's identity (see :data:`SEVERITIES`), keeps
    every reason sentence, and nests each detector's numbers under its own name.
    """
    grouped: dict[tuple[str, str, str | None, str], list[Candidate]] = {}
    for c in findings:
        # Non-actionable rows share a decision key by design (see Candidate.row_id), so they are
        # grouped by row id instead -- merging every unresolved FFC row into one would erase
        # the players.
        grouped.setdefault((*c.key, "" if c.actionable else c.player_name), []).append(c)

    out: list[Candidate] = []
    for group in grouped.values():
        group.sort(key=lambda c: (SEVERITIES.index(c.severity), c.detector))
        primary = group[0]
        if len(group) == 1:
            out.append(primary)
            continue
        detectors = tuple(dict.fromkeys(c.detector for c in group))
        reasons = list(dict.fromkeys(c.reason for c in group))
        detail: dict[str, Mapping[str, object]] = {}
        for c in group:
            detail.update(c.detail)
        out.append(
            replace(
                primary,
                detectors=detectors,
                reason=" ".join(reasons),
                detail=detail,
                actionable=all(c.actionable for c in group),
            )
        )
    return out


def collect_candidates(
    inputs: ReviewInputs,
    *,
    engine: ImpactEngine | None = None,
    distance_rel_min: float = DEFAULT_DISTANCE_REL_MIN,
    top_adp: int = DEFAULT_TOP_ADP,
    td_quantile: float = 0.95,
    impact_budget: int | None = None,
    include: Sequence[str] | None = None,
) -> ReviewQueue:
    """Run every detector, attach board impact, rank, and report what happened.

    ``impact_budget=None`` (the default) computes the real board impact for **every** candidate.
    That is affordable because one full revaluation costs about 1.5 ms -- measured, not assumed:
    ``compute_draft_values`` over the 188-player board is ~1.1 ms, and the whole queue of ~1,200
    candidates lands near two seconds. A cap is available anyway for a much wider run: with one
    set, candidates are screened on a CHEAP proxy first (the change in league-scored season
    points for that one player, one blend and one scoring call) and only the top
    ``impact_budget`` are revalued. The proxy and the real column move in the same direction but
    are not identical -- the real one subtracts a positional baseline and multiplies by expected
    games -- so any capped run says so on the page instead of leaving it an implementation
    detail. ``impact_budget=0`` skips the expensive column entirely.
    """
    engine = engine or ImpactEngine(inputs)
    skipped: dict[str, str] = {}
    notes: list[str] = []

    suppressed: dict[str, int] = {}
    groups: dict[str, Callable[[], list[Candidate]]] = {
        "contamination_constant": lambda: detect_constant_projections(inputs),
        "contamination_zero_statline": lambda: detect_zero_statlines(
            inputs, suppressed=suppressed
        ),
        "crosswalk": lambda: detect_crosswalk_failures(
            inputs, top_adp=top_adp, suppressed=suppressed
        ),
        "distance": lambda: detect_distance(inputs, rel_min=distance_rel_min),
        "envelope": lambda: _envelope_candidates(inputs),
        "td_regression": lambda: _td_candidates(inputs, quantile=td_quantile),
        "injury": lambda: detect_injury_vs_expected_games(inputs),
    }
    assert set(groups) == set(DETECTOR_GROUPS), "DETECTOR_GROUPS is out of step with the code"

    findings: list[Candidate] = []
    for name, fn in groups.items():
        if include is not None and name not in include:
            skipped[name] = "not requested"
            continue
        try:
            findings.extend(fn())
        except FileNotFoundError as exc:
            skipped[name] = f"missing cached input: {exc}"
            log.warning("detector %s skipped: %s", name, exc)
        except Exception as exc:  # noqa: BLE001 - one broken detector must not empty the queue
            skipped[name] = f"{type(exc).__name__}: {exc}"
            log.warning("detector %s failed: %s: %s", name, type(exc).__name__, exc)

    # Detector counts are counted BEFORE merging, because "how many rows did this detector
    # produce" is the flood question, and it is not the same question as "how many decisions are
    # pending" (which is the merged row count).
    counts_by_detector: dict[str, int] = {}
    for f in findings:
        counts_by_detector[f.detector] = counts_by_detector.get(f.detector, 0) + 1
    found = _merge_by_key(findings)

    # With no budget, every candidate gets the real column and the cheap proxy is not needed.
    # With one, the proxy decides who gets it.
    scored: list[tuple[float, Candidate]] = []
    if impact_budget is None:
        scored = [(0.0, c) for c in found]
    else:
        for c in found:
            proxy = 0.0
            if c.player_id is not None and c.source in inputs.sources:
                pair = engine.points_of(c.player_id, c.source, c.stat)
                if pair is not None:
                    proxy = abs(pair[1] - pair[0])
            elif c.player_id is None and c.source in inputs.sources:
                proxy = float("inf")  # source-wide: always worth the full computation
            scored.append((proxy, c))
        scored.sort(key=lambda t: -t[0])

    out: list[Candidate] = []
    budget = float("inf") if impact_budget is None else impact_budget
    for proxy, c in scored:
        if c.impact is not None:
            # A detector that already knows its impact cannot be recomputed -- the playing-time
            # detector is not about a source's number at all, so there is no rejection to price.
            out.append(c)
            continue
        if c.source not in inputs.sources:
            out.append(
                replace(
                    c,
                    impact=Impact(
                        scope="player" if c.player_id else "source",
                        computable=False,
                        note=(
                            "nothing to recompute: this is a JOIN failure, so the number is "
                            "missing rather than wrong. The fix is a data/overrides.csv entry, "
                            "and until then the player is absent from the board entirely."
                        ),
                    ),
                )
            )
            continue
        if budget <= 0:
            out.append(
                replace(
                    c,
                    impact=Impact(
                        scope="player" if c.player_id else "source",
                        computable=False,
                        note=(
                            f"not revalued: outside the {impact_budget}-row impact budget "
                            f"(cheap proxy {proxy:.1f} season pts). Raise --impact-budget to "
                            "compute it."
                        ),
                    ),
                )
            )
            continue
        budget -= 1
        if c.player_id is None:
            impact = engine.for_source(c.source, c.stat)
        else:
            impact = engine.for_player(c.player_id, c.source, c.stat)
        out.append(replace(c, impact=impact))

    out.sort(key=queue_sort_key)

    counts_by_severity: dict[str, int] = {}
    for c in out:
        counts_by_severity[c.severity] = counts_by_severity.get(c.severity, 0) + 1
    flooded = tuple(
        d for d, n in sorted(counts_by_detector.items()) if n > FLOOD_THRESHOLD
    )
    if flooded:
        notes.append(
            "FLOODED detector(s): "
            + ", ".join(f"{d} ({counts_by_detector[d]} rows)" for d in flooded)
            + ". The queue is ranked by board impact, so the rows that cannot move a pick sink "
            "to the bottom -- but a detector producing this many rows is itself the finding."
        )
    if impact_budget is not None and len(out) > impact_budget:
        notes.append(
            f"{len(out) - impact_budget} row(s) beyond the {impact_budget}-row impact budget "
            "carry no computed impact and are marked as such. They were ranked into that budget "
            "on a cheap season-points proxy, not on the board-impact column itself."
        )

    if len(findings) != len(out):
        notes.append(
            f"{len(findings)} detector findings merged into {len(out)} review rows: more than "
            "one detector fired on the same (source, stat, player). One row per decision key is "
            "deliberate -- two rows would invite two contradictory decisions about one number."
        )

    on_board = {s.player_id for s in getattr(inputs.board, "seasons", ())}
    off_board_designated = [
        f"{inputs.name_of.get(pid, pid)} ({inputs.designation(pid)}, ADP {inputs.adp_of[pid]:.1f})"
        for pid in sorted(inputs.adp_of, key=lambda p: inputs.adp_of[p])
        if pid not in on_board and suppresses_missing_data(inputs.injury_status.get(pid))
    ]
    if off_board_designated:
        notes.append(
            "designated and OFF the valued board entirely, so carrying no row here at all: "
            + ", ".join(off_board_designated)
            + ". Nothing needs deciding for them -- a source publishing nothing for a player who "
            "will not play is a source being right, and their exclusion from the board is now "
            "EXPLAINED rather than lucky."
        )

    if suppressed:
        notes.append(
            "suppressed by injury status: "
            + ", ".join(f"{d} ({n} row(s))" for d, n in sorted(suppressed.items()))
            + ". An all-zero statline for a player on IR is a correct projection and a source "
            "declining to publish him is a source being right, so those rows are not "
            "contamination. Each suppressed player is picked up by injury_vs_expected_games "
            "instead, where the finding actually lives."
        )

    board = inputs.board
    # Must match `detect_injury_vs_expected_games`'s own suppression exactly: only overrides
    # that ANSWER the player's current designation. Reporting every applied override here
    # described healthy players as vanished injury rows, and would have described a stale
    # override as having settled a designation it never saw (Codex 2026-08-24 finding 3).
    settled_by_override = {
        pid: binding.describe()
        for pid, binding in (getattr(board, "applied_playing_time", {}) or {}).items()
        if normalized_designation(binding.override.designation) is not None
        and normalized_designation(binding.override.designation)
        == normalized_designation(inputs.injury_status.get(pid))
    }
    if settled_by_override:
        notes.append(
            "settled by a manual playing-time override written for the designation the player "
            "currently carries, so carrying no injury row here: "
            + "; ".join(sorted(settled_by_override.values()))
            + ". The board's games figure for these players is now Marc's own judgement rather "
            "than the healthy-rank default, which is the only thing "
            "injury_vs_expected_games was ever complaining about. An override whose recorded "
            "designation does NOT match the current one is not listed here -- that player keeps "
            "his row, because the override predates the news."
        )

    return ReviewQueue(
        candidates=tuple(out),
        n_findings=len(findings),
        suppressed_by_injury=suppressed,
        settled_by_override=settled_by_override,
        counts_by_detector=counts_by_detector,
        counts_by_severity=counts_by_severity,
        flooded=flooded,
        skipped=skipped,
        board_source=getattr(board, "source", "blend"),
        n_board_players=len(getattr(board, "players", ())),
        sources=inputs.sources,
        notes=tuple(notes),
    )


def _td_candidates(
    inputs: ReviewInputs,
    *,
    quantile: float = 0.95,
    z_min: float = DEFAULT_TD_BIAS_Z_MIN,
) -> list[Candidate]:
    """Both TD mechanisms off ONE fit -- the per-player badge and the aggregate source level."""
    from draftroom.valuation import td_regression as tdr

    if inputs.espn_raw is None:
        raise FileNotFoundError("no cached ESPN payload; the TD fit needs 2025 actuals")
    modelset = tdr.fit_td_models(
        tdr.player_season_actuals(inputs.espn_raw, FIT_SEASON), seasons=[FIT_SEASON]
    )
    return [
        *detect_td_source_bias(inputs, z_min=z_min, modelset=modelset),
        *detect_td_flags(inputs, quantile=quantile, modelset=modelset),
    ]


def _envelope_candidates(inputs: ReviewInputs) -> list[Candidate]:
    """Both envelope checks off ONE fit, so the 37 MB payload is aggregated once."""
    bundle = _envelope_reports(inputs)
    if bundle is None:
        raise FileNotFoundError("no cached ESPN payload; the envelope fit needs 2025 actuals")
    reports = bundle[0]
    return [
        *detect_identity_hygiene(inputs, reports=reports),
        *detect_band_hygiene(inputs, reports=reports),
    ]
