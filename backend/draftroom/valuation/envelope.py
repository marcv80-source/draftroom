"""Team-envelope validator: do the per-player slices add up to a plausible team pie?

Every public projection set is built one player at a time. Nobody adds the players on a team
back up and asks whether the offense they imply could actually exist. This module does that,
**per source**, so the answer is not "somebody here is wrong" but "THIS source is wrong about
THIS stat on THIS team" -- which is exactly the shape
:func:`draftroom.valuation.composite.blend_statlines`'s ``rejected`` argument consumes.

Two independent checks, deliberately kept separate because they have very different strength:

1. **Accounting identities** (:func:`check_identities`) -- no history needed, no band to fit.
   Every completed pass is exactly one reception, so summed over a whole team a season's
   ``rec`` must equal its ``pass_cmp``, ``rec_yd`` must equal ``pass_yd``, and ``rec_td`` must
   equal ``pass_td``. These are not approximations, they are the same events counted from the
   two ends. A source whose receiving side exceeds its passing side has projected catches of
   balls nobody threw. This is the strongest check in the file.

2. **Fitted bands** (:func:`check_bands`) -- team-season totals for volume stats
   (attempts, targets, yards, TDs) compared against a band FITTED from cached history, never
   asserted. See :func:`fit_bands` for exactly what was fitted, on what, and what could not be.

**The coverage asymmetry, which decides which violations count.** None of our sources publishes
every player on an NFL roster, and the crosswalk drops a handful more. A partial roster
therefore *always* undershoots a real team total, so an undershoot proves nothing at all --
it is indistinguishable from "this source doesn't publish the team's 6th receiver". An
**overage** has no such excuse: a partial roster that already exceeds a full team's plausible
total is broken regardless of who is missing. Every verdict in this module is built on that
asymmetry. Undershoots are reported as ``"shortfall"`` / ``"under"`` and are explicitly
**informational**; only overages are treated as violations, and only overages ever become
rejection candidates. Getting this backwards produces a validator that flags all 32 teams and
means nothing.

**The independence caveat still applies** (see :mod:`draftroom.valuation.disagreement`): the
three source families are not three independent looks at reality. Three sources agreeing that
a team will throw 700 targets does not make 700 targets plausible -- which is precisely why
this check is against fitted history and an accounting identity rather than against the other
sources.

Reads only cached files under ``data/raw/``. No network call on any path here.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from draftroom.prep.schema import StatLine

log = logging.getLogger("draftroom.valuation.envelope")

# backend/draftroom/valuation/envelope.py -> parents[3] == repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
WEEKLY_HISTORY_DIR = REPO_ROOT / "data" / "raw" / "nflreadpy_weekly"

__all__ = [
    "TEAM_SUM_STATS",
    "BAND_STATS",
    "IDENTITY_RULES",
    "COVERAGE_CAVEAT",
    "Band",
    "BandSet",
    "BandViolation",
    "IdentityCheck",
    "TeamSum",
    "EnvelopeReport",
    "sum_by_team",
    "fit_bands",
    "fit_identity_tolerances",
    "check_identities",
    "check_bands",
    "build_report",
    "rejection_candidates",
    "team_season_actuals",
    "league_yardage_means",
    "load_weekly_history_rows",
]

#: Component stats summed per team. ``games`` is per-player and meaningless summed, and the
#: 2pt/int/fumble stats are too small per team for a band to say anything, so neither is here.
TEAM_SUM_STATS: tuple[str, ...] = (
    "pass_att",
    "pass_cmp",
    "pass_yd",
    "pass_td",
    "rush_att",
    "rush_yd",
    "rush_td",
    "rec",
    "rec_tgt",
    "rec_yd",
    "rec_td",
)

#: Derived team stat -> the component stats it sums. ``pass_td`` and ``rec_td`` are the SAME
#: touchdowns counted from the two ends, so a team's offensive TD total is passing + rushing;
#: adding rec_td as well would double-count every passing score.
DERIVED_TEAM_STATS: Mapping[str, tuple[str, ...]] = {
    "total_td": ("pass_td", "rush_td"),
}

#: The stats a fitted band exists for. Deliberately a subset of the above: ``pass_cmp`` and
#: ``rec`` are two views of one quantity (see IDENTITY_RULES) so only one needs a band, and
#: ``pass_td``/``rec_td`` roll into ``total_td``.
BAND_STATS: tuple[str, ...] = (
    "pass_att",
    "pass_yd",
    "rush_att",
    "rush_yd",
    "rec",
    "rec_tgt",
    "rec_yd",
    "total_td",
)

#: name -> (passing-side stat, receiving-side stat). Exact accounting identities when summed
#: over a whole team: one completion is one reception, and they share their yards and TDs.
IDENTITY_RULES: Mapping[str, tuple[str, str]] = {
    "completions_vs_receptions": ("pass_cmp", "rec"),
    "pass_yards_vs_rec_yards": ("pass_yd", "rec_yd"),
    "pass_tds_vs_rec_tds": ("pass_td", "rec_td"),
}

COVERAGE_CAVEAT = (
    "No source publishes every player on an NFL roster, and the crosswalk drops a few more, so "
    "a team's projected slices ALWAYS undershoot the real pie by an unknown amount. An "
    "undershoot is therefore uninformative -- it cannot be told apart from a missing 6th "
    "receiver -- and is reported here as informational only. An OVERAGE has no such excuse: a "
    "partial roster that already exceeds a plausible full-team total is broken no matter who is "
    "missing. Only overages count as violations, and only overages become rejection candidates."
)


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TeamSum:
    """One NFL team's summed projections from one source, plus who contributed what."""

    team: str
    n_players: int
    stats: Mapping[str, float]
    #: stat -> ((player_id, name, value), ...) sorted by value descending, truncated to
    #: ``top_n``. Kept so a violation can name the players driving it, not just the total.
    contributors: Mapping[str, tuple[tuple[str, str, float], ...]]
    #: stat -> how many players contributed a non-zero value, UNtruncated. This is what makes
    #: the identity check's confound measurable: a team whose passing side is one quarterback
    #: with no backup will show a receiving "overage" that is really a missing passer.
    contributor_counts: Mapping[str, int] = field(default_factory=dict)

    def get(self, stat: str) -> float:
        return float(self.stats.get(stat, 0.0))

    def count(self, stat: str) -> int:
        return int(self.contributor_counts.get(stat, 0))


@dataclass(frozen=True)
class Band:
    """A plausible range for one team-season stat, with its whole provenance attached.

    Nothing here is a round number somebody liked: ``observed_min``/``observed_max``/``median``
    come from real team-seasons, and ``drift_low``/``drift_high`` come from measured
    year-to-year movement of the league mean. ``low``/``high`` are those two combined.
    """

    stat: str
    low: float
    high: float
    median: float
    observed_min: float
    observed_max: float
    n_team_seasons: int
    fit_seasons: tuple[int, ...]
    fit_source: str
    #: Fractional widening applied to observed_min / observed_max, from measured league drift.
    drift_low: float
    drift_high: float
    #: False when drift could not be measured for THIS stat and a proxy was transported in.
    drift_measured: bool
    drift_note: str


@dataclass(frozen=True)
class BandSet:
    bands: Mapping[str, Band]
    provenance: Mapping[str, object]


@dataclass(frozen=True)
class IdentityCheck:
    """One team's passing-side vs receiving-side accounting identity, for one source."""

    team: str
    rule: str
    pass_stat: str
    recv_stat: str
    pass_side: float
    recv_side: float
    #: recv_side - pass_side, and the same as a fraction of pass_side.
    delta: float
    delta_pct: float
    tolerance_pct: float
    #: "ok" | "overage" (receiving exceeds passing -- broken) | "shortfall" (informational)
    verdict: str

    @property
    def is_violation(self) -> bool:
        return self.verdict == "overage"


@dataclass(frozen=True)
class BandViolation:
    team: str
    stat: str
    value: float
    band: Band
    #: Signed distance outside the band (positive = above high, negative = below low).
    excess: float
    excess_pct: float
    #: "over" (a violation) | "under" (informational, see COVERAGE_CAVEAT)
    direction: str
    top_contributors: tuple[tuple[str, str, float], ...]

    @property
    def is_violation(self) -> bool:
        return self.direction == "over"


@dataclass(frozen=True)
class EnvelopeReport:
    """Everything the envelope check found for ONE source."""

    source: str
    team_sums: Mapping[str, TeamSum]
    identity: tuple[IdentityCheck, ...]
    band: tuple[BandViolation, ...]
    #: Source rows that never made it into a team sum, so an undershoot is interpretable.
    n_statlines: int = 0
    n_no_team: int = 0
    n_dropped_unresolved: int = 0
    caveats: tuple[str, ...] = (COVERAGE_CAVEAT,)

    @property
    def identity_violations(self) -> tuple[IdentityCheck, ...]:
        return tuple(c for c in self.identity if c.is_violation)

    @property
    def band_violations(self) -> tuple[BandViolation, ...]:
        return tuple(v for v in self.band if v.is_violation)


# ---------------------------------------------------------------------------
# Summing a source's projections by NFL team
# ---------------------------------------------------------------------------


def sum_by_team(
    statlines: Mapping[str, StatLine],
    team_of: Callable[[str], str] | Mapping[str, str],
    *,
    name_of: Callable[[str], str] | Mapping[str, str] | None = None,
    top_n: int = 5,
) -> dict[str, TeamSum]:
    """Sum ``statlines`` by NFL team.

    ``team_of`` should be ONE authority for every source (this repo uses the Sleeper player
    universe's ``team`` field). Using each source's own team label instead would let a stale
    label on one source move a player between teams and make the per-source comparison
    apples-to-oranges.

    Players whose team resolves to a falsy value are collected under the ``""`` key rather than
    dropped, so a caller can see how much production was unattributable.
    """
    tf = team_of.get if isinstance(team_of, Mapping) else team_of
    nf = (name_of.get if isinstance(name_of, Mapping) else name_of) if name_of else (lambda _pid: "")

    acc: dict[str, dict[str, float]] = {}
    contrib: dict[str, dict[str, list[tuple[str, str, float]]]] = {}
    counts: dict[str, int] = {}

    for pid, line in statlines.items():
        team = (tf(pid) or "") if tf else ""
        team = team.strip().upper()
        stats = acc.setdefault(team, {s: 0.0 for s in TEAM_SUM_STATS})
        rows = contrib.setdefault(team, {s: [] for s in TEAM_SUM_STATS})
        counts[team] = counts.get(team, 0) + 1
        name = nf(pid) or pid
        for stat in TEAM_SUM_STATS:
            value = float(getattr(line, stat, 0.0) or 0.0)
            stats[stat] += value
            if value:
                rows[stat].append((pid, name, value))

    out: dict[str, TeamSum] = {}
    for team, stats in acc.items():
        for derived, parts in DERIVED_TEAM_STATS.items():
            stats[derived] = sum(stats.get(p, 0.0) for p in parts)
            merged: dict[str, tuple[str, float]] = {}
            for part in parts:
                for pid, name, value in contrib[team].get(part, []):
                    prev = merged.get(pid)
                    merged[pid] = (name, (prev[1] if prev else 0.0) + value)
            contrib[team][derived] = [(pid, nm, val) for pid, (nm, val) in merged.items()]
        out[team] = TeamSum(
            team=team,
            n_players=counts[team],
            stats=dict(stats),
            contributors={
                stat: tuple(sorted(rows, key=lambda r: -r[2])[:top_n])
                for stat, rows in contrib[team].items()
            },
            contributor_counts={stat: len(rows) for stat, rows in contrib[team].items()},
        )
    return out


# ---------------------------------------------------------------------------
# Fitting the bands from cached history
# ---------------------------------------------------------------------------


def team_season_actuals(
    espn_raw: Mapping[str, object], season: int
) -> dict[str, dict[str, float]]:
    """Real team-season totals for ``season``, from the cached ESPN payload's weekly ACTUALS.

    ESPN's ``kona_player_info`` payload carries, per player, a stat block per game played with
    ``statSourceId == 0`` (actual), ``statSplitTypeId == 1`` (weekly) and -- critically -- the
    ``proTeamId`` the player was on THAT WEEK. That per-week team id is what makes team-season
    aggregation possible at all: the player object's own ``proTeamId`` is his CURRENT team, so
    using it would credit every offseason mover's past production to his new offense.

    The cached ``nflreadpy_weekly`` CSVs cannot do this job: they carry no team column at all
    (only ``season, week, player_id, player_display_name, position, pass_yd, rush_yd, rec_yd``),
    so there is no way to attribute a game to an offense. They are used here only for the
    year-to-year league drift measured in :func:`league_yardage_means`.
    """
    from draftroom.prep import espn_client as espn

    players = espn_raw.get("players") if isinstance(espn_raw, Mapping) else None
    if not isinstance(players, list):
        raise ValueError(
            "ESPN payload has no 'players' list; cannot fit team envelopes from it. Do not "
            "guess a fix -- inspect the cached file."
        )

    teams: dict[str, dict[str, float]] = {}
    for entry in players:
        player = (entry or {}).get("player") or {}
        pos = espn.ESPN_POSITION_MAP.get(player.get("defaultPositionId"))
        if pos not in espn.SKILL_POSITIONS:
            continue
        for block in player.get("stats") or []:
            if (
                block.get("seasonId") != season
                or block.get("statSourceId") != 0
                or block.get("statSplitTypeId") != 1
            ):
                continue
            team = espn.ESPN_TEAM_MAP.get(block.get("proTeamId") or 0, "")
            if not team:
                continue
            stats = block.get("stats") or {}
            if not stats:
                continue
            bucket = teams.setdefault(team, {s: 0.0 for s in TEAM_SUM_STATS})
            for raw_key, value in stats.items():
                try:
                    stat_id = int(raw_key)
                except (TypeError, ValueError):
                    continue
                canonical = espn.ESPN_STAT_ID_MAP.get(stat_id)
                if canonical in bucket:
                    bucket[canonical] += float(value or 0.0)

    for bucket in teams.values():
        for derived, parts in DERIVED_TEAM_STATS.items():
            bucket[derived] = sum(bucket.get(p, 0.0) for p in parts)
    return teams


def load_weekly_history_rows(path: Path | None = None) -> tuple[Path, list[dict[str, str]]]:
    """Read the newest cached ``nflreadpy_weekly`` CSV. Never touches the network.

    Fails loudly if the file includes postseason weeks. ``tools/fetch_weekly_history.py``
    filters to ``season_type == "REG"`` for a documented reason (this league's season is 17
    NFL regular-season weeks), but the cache in this repo also holds an OLDER file written
    before that filter existed, whose weeks run to 22. A season-total fitted over 21 weeks of
    football is not a season total, and the cached CSV drops the ``season_type`` column, so the
    contamination cannot be filtered out after the fact -- only detected.
    """
    if path is None:
        if not WEEKLY_HISTORY_DIR.exists():
            raise FileNotFoundError(f"no cached weekly history at {WEEKLY_HISTORY_DIR}")
        files = sorted(p for p in WEEKLY_HISTORY_DIR.iterdir() if p.suffix == ".csv")
        if not files:
            raise FileNotFoundError(f"no cached weekly history csv files in {WEEKLY_HISTORY_DIR}")
        path = files[-1]

    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    max_week = max((int(r["week"]) for r in rows), default=0)
    if max_week > 18:
        raise ValueError(
            f"{path.name} carries week {max_week}, i.e. NFL POSTSEASON games. Season totals fit "
            "from it would not be season totals, and the cached CSV has no season_type column "
            "to filter on. Re-run tools/fetch_weekly_history.py (it filters season_type=='REG') "
            "and use the newer file."
        )
    return path, rows


def league_yardage_means(rows: Sequence[Mapping[str, str]]) -> dict[int, dict[str, float]]:
    """Per season, the league's mean team yardage (league total / 32) for the three yardage
    stats the cached weekly history actually carries.

    This is the ONLY multi-season figure available offline, and it is what makes the bands more
    than a single-season snapshot: it measures how far the whole league's mean moves from year
    to year, which is the drift a one-season cross-team band cannot see.
    """
    totals: dict[int, dict[str, float]] = {}
    for row in rows:
        season = int(row["season"])
        bucket = totals.setdefault(season, {"pass_yd": 0.0, "rush_yd": 0.0, "rec_yd": 0.0})
        for stat in bucket:
            raw = row.get(stat)
            if raw in (None, "", "NA"):
                continue
            bucket[stat] += float(raw)
    return {season: {k: v / 32.0 for k, v in b.items()} for season, b in totals.items()}


#: Which BAND_STATS stat borrows its drift from which measured yardage stat. Only the three
#: yardage stats have multi-season history cached, so drift for the volume/TD stats is
#: TRANSPORTED, not measured -- flagged as ``drift_measured=False`` on every Band it touches.
_DRIFT_PROXY: Mapping[str, str] = {
    "pass_yd": "pass_yd",
    "rec_yd": "rec_yd",
    "rush_yd": "rush_yd",
}


def fit_bands(
    *,
    team_actuals: Mapping[str, Mapping[str, float]],
    yardage_means: Mapping[int, Mapping[str, float]],
    fit_season: int,
    stats: Iterable[str] = BAND_STATS,
) -> BandSet:
    """Fit a plausible band per team-season stat. Every number traced to data, nothing invented.

    Construction, and the exact honest limits of it:

    * ``observed_min``/``median``/``observed_max`` are the real cross-team spread of
      ``fit_season``, from ``team_actuals`` -- **n = 32 team-seasons, one season**. That is a
      within-season spread across offenses, which is real but says nothing about how the whole
      league drifts year to year.
    * ``drift_low``/``drift_high`` supply the missing year-to-year component, measured from
      ``yardage_means``: how far below and above ``fit_season``'s league mean the league mean
      got in the other cached seasons. Applied multiplicatively to the observed extremes.
    * For the three yardage stats that drift is **measured directly**. For attempts, targets and
      TDs the cached history carries no such column, so the widest measured yardage drift is
      transported in as a stated proxy and the Band says ``drift_measured=False``. That is a
      documented assumption, not a fitted coefficient, and a band relying on it deserves less
      trust than one that doesn't.
    """
    if not team_actuals:
        raise ValueError("no team actuals supplied; cannot fit bands")

    drift: dict[str, tuple[float, float]] = {}
    if yardage_means and fit_season in yardage_means:
        base = yardage_means[fit_season]
        for stat in ("pass_yd", "rush_yd", "rec_yd"):
            anchor = base.get(stat, 0.0)
            if anchor <= 0:
                continue
            series = [m[stat] for m in yardage_means.values() if stat in m]
            drift[stat] = (min(series) / anchor - 1.0, max(series) / anchor - 1.0)

    if drift:
        proxy_low = min(lo for lo, _ in drift.values())
        proxy_high = max(hi for _, hi in drift.values())
    else:
        proxy_low = proxy_high = 0.0

    bands: dict[str, Band] = {}
    for stat in stats:
        values = sorted(float(t.get(stat, 0.0)) for t in team_actuals.values())
        if not values or values[-1] <= 0:
            log.warning("no %s in the fitted team actuals; no band produced", stat)
            continue
        n = len(values)
        median = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2.0

        proxy_key = _DRIFT_PROXY.get(stat)
        measured = proxy_key is not None and proxy_key in drift
        if measured:
            d_low, d_high = drift[proxy_key]
            note = (
                f"drift measured directly on {proxy_key} across seasons "
                f"{sorted(yardage_means)}: {d_low:+.1%} / {d_high:+.1%} of the {fit_season} "
                "league mean"
            )
        else:
            d_low, d_high = proxy_low, proxy_high
            note = (
                "NO multi-season history is cached for this stat (the weekly cache carries only "
                f"pass_yd/rush_yd/rec_yd), so the widest measured yardage drift ({proxy_low:+.1%} "
                f"/ {proxy_high:+.1%}) is transported in as a stated proxy. Assumption, not a fit."
            )

        bands[stat] = Band(
            stat=stat,
            low=values[0] * (1.0 + d_low),
            high=values[-1] * (1.0 + d_high),
            median=median,
            observed_min=values[0],
            observed_max=values[-1],
            n_team_seasons=n,
            fit_seasons=(fit_season,),
            fit_source=f"ESPN cached weekly ACTUALS, {fit_season}",
            drift_low=d_low,
            drift_high=d_high,
            drift_measured=measured,
            drift_note=note,
        )

    return BandSet(
        bands=bands,
        provenance={
            "fit_season": fit_season,
            "n_team_seasons": len(team_actuals),
            "team_actual_source": f"ESPN cached weekly actuals, statSourceId=0/statSplitTypeId=1, {fit_season}",
            "drift_seasons": tuple(sorted(yardage_means)),
            "drift_source": "cached nflreadpy weekly history, league total / 32 per season",
            "drift_measured_stats": tuple(sorted(drift)),
            "drift_proxy_low": proxy_low,
            "drift_proxy_high": proxy_high,
            "limits": (
                "One season of team-season observations (n=32). The cross-team spread is real; "
                "the year-to-year component is bolted on from league means, and for attempts, "
                "targets and TDs it is transported from yardage rather than measured."
            ),
        },
    )


def fit_identity_tolerances(
    team_actuals: Mapping[str, Mapping[str, float]],
    *,
    rules: Mapping[str, tuple[str, str]] = IDENTITY_RULES,
) -> dict[str, float]:
    """The measurement noise floor for each accounting identity, FITTED not chosen.

    In reality these identities are exact, so any deviation seen in *real* aggregated actuals
    is measurement noise -- players outside the payload's window, a mid-season team change
    landing on the wrong side of a week boundary. Taking the largest such deviation observed
    across the fitted team-seasons gives a tolerance nothing in the source data has to invent.
    """
    out: dict[str, float] = {}
    for rule, (pass_stat, recv_stat) in rules.items():
        worst = 0.0
        for stats in team_actuals.values():
            base = float(stats.get(pass_stat, 0.0))
            if base <= 0:
                continue
            worst = max(worst, abs(float(stats.get(recv_stat, 0.0)) - base) / base)
        out[rule] = worst
    return out


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def check_identities(
    team_sums: Mapping[str, TeamSum],
    tolerances: Mapping[str, float],
    *,
    rules: Mapping[str, tuple[str, str]] = IDENTITY_RULES,
) -> tuple[IdentityCheck, ...]:
    """Passing-side vs receiving-side accounting identity, per team.

    ``overage`` (receiving side above the passing side beyond tolerance) is a real violation:
    a team cannot catch more passes than it threw. ``shortfall`` is informational only -- it is
    what missing players look like, and it is also what an over-projected QB looks like, and
    this check cannot tell those apart.
    """
    out: list[IdentityCheck] = []
    for team in sorted(team_sums):
        if not team:
            continue  # unattributable players; a team-level identity means nothing here
        sums = team_sums[team]
        for rule, (pass_stat, recv_stat) in rules.items():
            base = sums.get(pass_stat)
            recv = sums.get(recv_stat)
            if base <= 0 and recv <= 0:
                continue
            delta = recv - base
            pct = delta / base if base > 0 else float("inf")
            tol = float(tolerances.get(rule, 0.0))
            if base <= 0:
                verdict = "overage"
            elif pct > tol:
                verdict = "overage"
            elif pct < -tol:
                verdict = "shortfall"
            else:
                verdict = "ok"
            out.append(
                IdentityCheck(
                    team=team,
                    rule=rule,
                    pass_stat=pass_stat,
                    recv_stat=recv_stat,
                    pass_side=base,
                    recv_side=recv,
                    delta=delta,
                    delta_pct=pct,
                    tolerance_pct=tol,
                    verdict=verdict,
                )
            )
    return tuple(out)


def check_bands(
    team_sums: Mapping[str, TeamSum],
    bandset: BandSet,
    *,
    include_under: bool = True,
    use_drift: bool = True,
) -> tuple[BandViolation, ...]:
    """Team sums against the fitted bands. Only ``direction == "over"`` is a violation.

    ``use_drift=False`` compares against the raw observed extremes of the fitted season instead
    of the drift-widened band. Worth running both ways: for attempts, targets and TDs the
    widening is a transported proxy rather than a measurement (see :func:`fit_bands`), and it is
    wide enough to swallow real overages, so the difference between the two runs is exactly the
    cost of that assumption.
    """
    out: list[BandViolation] = []
    for team in sorted(team_sums):
        if not team:
            continue
        sums = team_sums[team]
        for stat, band in bandset.bands.items():
            value = sums.get(stat)
            if value <= 0:
                continue  # a source that publishes nothing for this stat (e.g. rec_tgt)
            high = band.high if use_drift else band.observed_max
            low = band.low if use_drift else band.observed_min
            if value > high:
                excess = value - high
                direction = "over"
            elif value < low:
                excess = value - low
                direction = "under"
            else:
                continue
            if direction == "under" and not include_under:
                continue
            out.append(
                BandViolation(
                    team=team,
                    stat=stat,
                    value=value,
                    band=band,
                    excess=excess,
                    excess_pct=excess / high if high else 0.0,
                    direction=direction,
                    top_contributors=sums.contributors.get(stat, ()),
                )
            )
    out.sort(key=lambda v: (v.direction != "over", -abs(v.excess_pct)))
    return tuple(out)


def build_report(
    source: str,
    statlines: Mapping[str, StatLine],
    team_of: Callable[[str], str] | Mapping[str, str],
    bandset: BandSet,
    tolerances: Mapping[str, float],
    *,
    name_of: Callable[[str], str] | Mapping[str, str] | None = None,
    n_dropped_unresolved: int = 0,
) -> EnvelopeReport:
    """Run both checks for one source and package the result."""
    sums = sum_by_team(statlines, team_of, name_of=name_of)
    return EnvelopeReport(
        source=source,
        team_sums=sums,
        identity=check_identities(sums, tolerances),
        band=check_bands(sums, bandset),
        n_statlines=len(statlines),
        n_no_team=sums[""].n_players if "" in sums else 0,
        n_dropped_unresolved=n_dropped_unresolved,
    )


def rejection_candidates(
    reports: Iterable[EnvelopeReport],
) -> tuple[tuple[str, str, str], ...]:
    """``(source, stat, reason)`` triples this check would put in front of a human.

    NOT wired into :func:`draftroom.valuation.composite.blend_statlines`'s ``rejected``, and
    deliberately named "candidates". Rejection at the (source, stat) grain throws away that
    source's number for EVERY player at that stat, and an envelope bust localises to a team,
    not to a source-wide stat. What this returns is evidence for a decision, not the decision.
    """
    out: list[tuple[str, str, str]] = []
    for report in reports:
        by_stat: dict[str, list[str]] = {}
        for check in report.identity_violations:
            by_stat.setdefault(check.recv_stat, []).append(
                f"{check.team} {check.recv_stat} {check.delta:+.0f} ({check.delta_pct:+.1%}) "
                f"vs its own {check.pass_stat}"
            )
        for violation in report.band_violations:
            by_stat.setdefault(violation.stat, []).append(
                f"{violation.team} {violation.value:.0f} vs band high {violation.band.high:.0f} "
                f"({violation.excess_pct:+.1%})"
            )
        for stat, reasons in sorted(by_stat.items()):
            out.append(
                (
                    report.source,
                    stat,
                    f"{len(reasons)} team(s): " + "; ".join(reasons[:4]),
                )
            )
    return tuple(out)
