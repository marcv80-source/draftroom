"""Should we renormalize a source's receiving side down to its own team passing total?

THE QUESTION
    Summed over an NFL team, projected receiving yards must equal projected passing yards --
    every receiving yard is a passing yard, and every completed pass is exactly one reception.
    ``docs/PROJECTION_CHALLENGES.md`` measured that identity on the cached 2026 projections and
    found Sleeper violating it on 26 of 32 teams (worst TB +21.9%), FantasyPros worse, ESPN not
    at all. The proposed remedy is to scale each team's receiving side down to that team's
    projected passing total, preserving every player's share and fixing only the level.

    The identity proves the two sides DISAGREE. It does not say which side is wrong. This tool
    answers that empirically, because we hold 2025 projections AND 2025 actuals:

    Q1  Which side is guilty? Compare 2025 PROJECTED team passing and receiving against 2025
        ACTUAL team totals, per source.
    Q2  Would the correction have IMPROVED 2025 player-level accuracy, in this league's own
        scoring? Measured before and after, paired, with a bootstrap CI -- plus the two obvious
        alternative remedies (scale the passing side UP, split the gap) so the choice is
        informed rather than binary.

WHAT IS REUSED, AND WHY NOTHING IS REBUILT
    ``tools/backtest_sources.py`` already owns a verified 2025 spine: ESPN stat ids re-derived
    from ESPN's own ratio fields (a hard gate, not a warning), ONE actuals source cross-checked
    against nflreadpy, projection vintage verified by content rather than timestamp, a 449-player
    population with documented join provenance, and seeded bootstraps. This tool imports that
    module and calls its functions. It adds exactly one thing that backtest does not need: TEAM
    ATTRIBUTION.

TEAM ATTRIBUTION -- the trap that would make every number here garbage
    ``docs/PROJECTION_CHALLENGES.md`` documents it: a player's *current* ``proTeamId`` is his
    2026 team, so using it for 2025 credits every offseason mover's production to the wrong
    offense. Three separate attributions are needed here and each is verified on every run
    (:func:`team_vintage_lines`), never assumed:

    * **2025 ACTUALS** -- the ``proTeamId`` carried on each WEEKLY actual stat block, i.e. the
      team the player was on THAT WEEK. A player traded mid-season splits correctly.
    * **ESPN 2025 PROJECTION** -- the player-level ``proTeamId`` *inside the 2025 payload*,
      which is 2025-vintage, not 2026. Verified two ways: it disagrees with the cached 2026
      ESPN payload on 170 players (A.J. Brown PHI-not-NE, Mike Evans TB-not-SF, ...) and agrees
      with the modal 2025 weekly team on 541 of 577 players who played, the residue being
      genuine mid-season trades where the season-start team is the right one for a preseason
      projection.
    * **SLEEPER 2025 PROJECTION** -- the ``team`` on the PROJECTION ROW, never
      ``row["player"]["team"]``. The embedded player object is the live 2026 record: it matches
      the cached 2026 Sleeper universe 974/992 and disagrees with ESPN's 2025 team on 169
      players. The row-level field is the 2025 team and agrees with ESPN 524/531. Reading the
      embedded one is exactly the documented trap, wearing a different key name.

COVERAGE -- handled, not hoped away
    Sources publish 14-19 skill players per team, so a projected team total can undershoot a
    real one for no reason but roster depth, and an undershoot would read as pessimism. Every
    Q1 comparison is therefore reported twice: against the FULL actual team total, and
    DEPTH-MATCHED -- if a source projects K receivers for a team, its receiving sum is compared
    against that team's top-K actual receivers. The actual spine's own completeness is bounded
    against the cached nflreadpy 2025 weekly history in the same section.

    A third control matters more than either: RUSHING. Rushing is not part of the identity, so
    a source's rushing ratio measures its general volume/availability optimism, and the guilty
    side is whichever of passing/receiving departs from that neutral yardstick. Shared optimism
    cancels out of the passing-versus-receiving comparison, which is the whole point.

SURVIVORSHIP
    Same rule as the harness, for the same reason: a player ESPN published an EMPTY 2025 actual
    block for really did record nothing, and he is exactly the row that punishes an optimistic
    projection, so he is kept and scored as a true zero. A player with NO actual block is
    unobserved rather than zero and is held out of the primary tables and reported separately.

WRITES NOTHING
    Read-only over ``data/backtest/`` and ``data/raw/``. No correction is applied to the
    production board: that decision is Marc's, and this tool exists to inform it.

Usage:
    python tools/check_renormalization.py                  # the full report
    python tools/check_renormalization.py --teams-detail   # + per-team Q1 rows
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import math
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))


def _load_backtest_module():
    """Import ``tools/backtest_sources.py`` by path.

    ``tools/`` is a plain directory of scripts with no ``__init__.py``, so there is no
    importable package name to rely on -- and this module must import the same way whether it
    is run as a script or loaded by the test suite.
    """
    if "backtest_sources" in sys.modules:
        return sys.modules["backtest_sources"]
    spec = importlib.util.spec_from_file_location(
        "backtest_sources", REPO_ROOT / "tools" / "backtest_sources.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - environment failure
        raise ImportError("cannot load tools/backtest_sources.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["backtest_sources"] = module
    spec.loader.exec_module(module)
    return module


bt = _load_backtest_module()

from draftroom.config import LeagueConfig  # noqa: E402
from draftroom.prep import espn_client  # noqa: E402
from draftroom.prep.schema import normalize_name  # noqa: E402

SEASON = bt.SEASON
POSITIONS = bt.POSITIONS

#: The identity, stat by stat. Every completed pass is exactly one reception, for exactly the
#: same yards, and a passing touchdown IS a receiving touchdown. Summed over a team these are
#: equalities, not approximations.
IDENTITY_PAIRS: tuple[tuple[str, str], ...] = (
    ("pass_cmp", "rec"),
    ("pass_yd", "rec_yd"),
    ("pass_td", "rec_td"),
)
PASS_STATS = tuple(p for p, _ in IDENTITY_PAIRS)
REC_STATS = tuple(r for _, r in IDENTITY_PAIRS)

#: Stats outside the identity, used as the neutral yardstick for a source's general volume
#: optimism. Nothing in any remedy touches them.
NEUTRAL_STATS: tuple[str, ...] = ("rush_att", "rush_yd")

#: Team-abbreviation aliases, ESPN's spelling winning because the actuals spine is ESPN's.
#: Sleeper writes WAS where ESPN writes WSH; the rest are defensive against historical
#: spellings that appear in other feeds.
TEAM_ALIASES: dict[str, str] = {
    "WAS": "WSH",
    "JAC": "JAX",
    "LA": "LAR",
    "OAK": "LV",
    "SD": "LAC",
    "STL": "LAR",
    "ARZ": "ARI",
    "BLT": "BAL",
    "CLV": "CLE",
    "HST": "HOU",
}

#: The remedies compared. Each is a rule for reconciling a team's projected passing total P
#: against its projected receiving total R, per identity pair. ``(side, only_when_rec_high)``.
#:
#: The ``_over`` variants exist because the two-sided version does something the proposal never
#: asked for: on a team where the source's receiving side is BELOW its own passing side -- which
#: is usually just receiver-depth truncation, not a projection error -- a two-sided rule scales
#: those receivers UP, by as much as 58% on the 2025 feeds. ``docs/PROJECTION_CHALLENGES.md``
#: treats only OVERAGES as violations, for exactly that reason, so the one-sided variant is the
#: faithful implementation of the proposal and the two-sided one is the literal reading of it.
REMEDY_SPECS: dict[str, tuple[str, bool]] = {
    "rec_down": ("rec", False),
    "rec_down_over": ("rec", True),
    "pass_up": ("pass", False),
    "pass_up_over": ("pass", True),
    "split": ("split", False),
}

#: LEVEL-MATCHED NULLS -- the control without which none of this means anything.
#:
#: ``docs/SOURCE_BACKTEST.md`` measured both sources running +8 to +48 league points HOT in
#: 2025. Any remedy whose net effect is to take points OFF the board will therefore improve MAE
#: whether or not it has anything to do with the accounting identity. So each identity remedy is
#: paired with a null that removes exactly the same TOTAL from exactly the same players' side of
#: the ball, spread as a single uniform multiplier over all 32 teams -- i.e. a flat haircut that
#: knows nothing about which team violated what. If the identity remedy cannot beat its own
#: null, the identity contributed nothing and the gain was a haircut wearing a validator's coat.
NULL_SPECS: dict[str, tuple[str, tuple[str, ...]]] = {
    "rec_flat": ("rec_down_over", REC_STATS),
    "pass_flat": ("pass_up", PASS_STATS),
}

#: Display order: each identity remedy next to the null that controls it.
REMEDIES: tuple[str, ...] = (
    "rec_down",
    "rec_down_over",
    "rec_flat",
    "pass_up",
    "pass_up_over",
    "pass_flat",
    "split",
)

SOURCES: tuple[str, ...] = ("sleeper", "espn", "blend")


def normalize_team(abbr: Any) -> str:
    """One spelling per NFL team, so two feeds' abbreviations aggregate into one bucket."""
    text = str(abbr or "").strip().upper()
    return TEAM_ALIASES.get(text, text)


# ========================================================================= team attribution


def espn_projection_teams(raw_players: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """espn_id -> the team ESPN's 2025 payload put the player on (2025-vintage, verified)."""
    out: dict[str, str] = {}
    for entry in raw_players:
        player = entry.get("player") or {}
        pid = player.get("id")
        if pid is None:
            continue
        out[str(pid)] = normalize_team(espn_client.ESPN_TEAM_MAP.get(player.get("proTeamId")))
    return out


def _espn_2026_players() -> list[Mapping[str, Any]]:
    """The cached 2026 ESPN payload's player list. Read-only; never fetched here."""
    from draftroom.prep.http import load_latest_raw

    raw = load_latest_raw("espn")
    players = raw.get("players") if isinstance(raw, Mapping) else raw
    if not players:
        raise RuntimeError("cached ESPN payload has no players list")
    return list(players)


def espn_current_teams_2026() -> dict[str, str]:
    """espn_id -> team from the cached 2026 ESPN payload, for the vintage contrast only."""
    from draftroom.prep.http import load_latest_raw

    raw = load_latest_raw("espn")
    players = raw.get("players") if isinstance(raw, Mapping) else raw
    out: dict[str, str] = {}
    for entry in players or []:
        player = entry.get("player") or {}
        pid = player.get("id")
        if pid is None:
            continue
        out[str(pid)] = normalize_team(espn_client.ESPN_TEAM_MAP.get(player.get("proTeamId")))
    return out


def player_team_weeks(raw_players: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """espn_id -> {team: weeks with a populated 2025 actual block on that team}."""
    out: dict[str, dict[str, int]] = {}
    for entry in raw_players:
        player = entry.get("player") or {}
        pid = player.get("id")
        if pid is None:
            continue
        counts: dict[str, int] = {}
        for block in player.get("stats") or []:
            if (
                block.get("seasonId") == SEASON
                and block.get("statSourceId") == 0
                and block.get("statSplitTypeId") == 1
                and block.get("stats")
            ):
                team = normalize_team(espn_client.ESPN_TEAM_MAP.get(block.get("proTeamId")))
                counts[team] = counts.get(team, 0) + 1
        if counts:
            out[str(pid)] = counts
    return out


def sleeper_projection_teams(raw_rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Sleeper pid -> 2025 team, read off the PROJECTION ROW, never the embedded player.

    See the module docstring: ``row["player"]["team"]`` is the live 2026 record and using it
    is the documented offseason-mover trap.
    """
    out: dict[str, str] = {}
    for row in raw_rows:
        pid = row.get("player_id")
        if pid is None:
            continue
        out.setdefault(str(pid), normalize_team(row.get("team")))
    return out


def sleeper_embedded_teams(raw_rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Sleeper pid -> the team on the EMBEDDED player object, for the vintage contrast only."""
    out: dict[str, str] = {}
    for row in raw_rows:
        pid = row.get("player_id")
        if pid is None:
            continue
        player = row.get("player") or {}
        out.setdefault(str(pid), normalize_team(player.get("team")))
    return out


def team_vintage_lines(
    espn_raw_players: Sequence[Mapping[str, Any]],
    sleeper_raw: Sequence[Mapping[str, Any]],
    *,
    min_weekly_agreement: float = 0.85,
    min_cross_source_agreement: float = 0.90,
) -> list[str]:
    """Prove -- on every run -- that each team attribution is the 2025 one, and gate on it.

    Raises AssertionError rather than warning. A silently-2026 team map would move whole
    receiving corps between offenses and every ratio in this report would be meaningless while
    still looking perfectly plausible.
    """
    lines: list[str] = []
    proj_teams = espn_projection_teams(espn_raw_players)
    weeks = player_team_weeks(espn_raw_players)
    current = espn_current_teams_2026()

    agree = disagree = 0
    for pid, counts in weeks.items():
        modal = max(counts.items(), key=lambda kv: kv[1])[0]
        if pid not in proj_teams:
            continue
        if modal == proj_teams[pid]:
            agree += 1
        else:
            disagree += 1
    total = agree + disagree
    rate = agree / total if total else 0.0
    lines.append(
        f"  ESPN 2025 payload team vs modal 2025 WEEKLY team: {agree}/{total} agree "
        f"({rate:.1%}); residue is mid-season trades, where the season-start team is the "
        f"right one for a PRESEASON projection"
    )
    assert rate >= min_weekly_agreement, (
        f"ESPN 2025 payload proTeamId agrees with the modal 2025 weekly team on only "
        f"{rate:.1%} of players -- it is not 2025-vintage and every team total here would be "
        f"attributed to the wrong offense."
    )

    moved = sum(
        1 for pid, team in proj_teams.items() if pid in current and current[pid] != team and team
    )
    lines.append(
        f"  ESPN 2025 payload team vs cached 2026 ESPN payload: {moved} players differ "
        f"-> the 2025 payload is NOT serving 2026 rosters (that is the trap avoided)"
    )
    assert moved > 0, (
        "The 2025 ESPN payload's teams are identical to the 2026 payload's, which means it is "
        "serving current rosters and cannot be used for 2025 team attribution."
    )

    row_teams = sleeper_projection_teams(sleeper_raw)
    embedded = sleeper_embedded_teams(sleeper_raw)
    espn_by_name: dict[tuple[str, str], str] = {}
    for entry in espn_raw_players:
        player = entry.get("player") or {}
        pos = espn_client.ESPN_POSITION_MAP.get(player.get("defaultPositionId"))
        if pos not in POSITIONS:
            continue
        espn_by_name.setdefault(
            (normalize_name(player.get("fullName") or ""), pos),
            normalize_team(espn_client.ESPN_TEAM_MAP.get(player.get("proTeamId"))),
        )

    row_agree = row_dis = emb_agree = emb_dis = 0
    for row in sleeper_raw:
        player = row.get("player") or {}
        pos = (player.get("position") or "").upper()
        pid = str(row.get("player_id"))
        name = normalize_name(f"{player.get('first_name', '')} {player.get('last_name', '')}")
        espn_team = espn_by_name.get((name, pos))
        if not espn_team:
            continue
        if row_teams.get(pid):
            if row_teams[pid] == espn_team:
                row_agree += 1
            else:
                row_dis += 1
        if embedded.get(pid):
            if embedded[pid] == espn_team:
                emb_agree += 1
            else:
                emb_dis += 1
    row_total = row_agree + row_dis
    emb_total = emb_agree + emb_dis
    row_rate = row_agree / row_total if row_total else 0.0
    emb_rate = emb_agree / emb_total if emb_total else 0.0
    lines.append(
        f"  Sleeper ROW-level team vs ESPN 2025 team:      {row_agree}/{row_total} agree "
        f"({row_rate:.1%})  <- used"
    )
    lines.append(
        f"  Sleeper EMBEDDED player.team vs ESPN 2025 team: {emb_agree}/{emb_total} agree "
        f"({emb_rate:.1%})  <- the 2026 record, NOT used"
    )
    assert row_rate >= min_cross_source_agreement, (
        f"Sleeper's row-level team agrees with ESPN's 2025 team on only {row_rate:.1%} of "
        f"players; it is not the 2025 team either and there is no verified attribution left."
    )
    assert row_rate > emb_rate, (
        "Sleeper's embedded player.team agrees with the 2025 spine at least as well as the "
        "row-level team does. One of the two assumptions in this tool is wrong -- inspect the "
        "payload rather than picking a field."
    )
    return lines


# ============================================================================= actual totals


@dataclass
class ActualSide:
    """2025 actual production, aggregated the only way that is defensible: per player-team."""

    #: (espn_id, team) -> canonical stat totals from that player's weeks on that team.
    player_team: dict[tuple[str, str], dict[str, float]] = field(default_factory=dict)

    def team_totals(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for (_pid, team), stats in self.player_team.items():
            bucket = out.setdefault(team, {})
            for stat, value in stats.items():
                bucket[stat] = bucket.get(stat, 0.0) + value
        return out

    def top_k_total(self, team: str, stat: str, k: int) -> float:
        """Sum of the team's top ``k`` players by ``stat`` -- the depth-matched comparison."""
        values = sorted(
            (
                stats.get(stat, 0.0)
                for (_pid, t), stats in self.player_team.items()
                if t == team and stats.get(stat, 0.0) > 0
            ),
            reverse=True,
        )
        return float(sum(values[:k])) if k > 0 else 0.0

    def player_count(self, team: str, stat: str) -> int:
        return sum(
            1
            for (_pid, t), stats in self.player_team.items()
            if t == team and stats.get(stat, 0.0) > 0
        )


def actual_side(raw_players: Sequence[Mapping[str, Any]]) -> ActualSide:
    """Aggregate 2025 actuals by (player, team-that-week). All 32 offenses, whole rosters.

    Deliberately NOT restricted to the matched population or to skill positions: a team total
    is a team total, and a quarterback ESPN classifies oddly still threw the passes.
    """
    side = ActualSide()
    for entry in raw_players:
        player = entry.get("player") or {}
        pid = player.get("id")
        if pid is None:
            continue
        for block in player.get("stats") or []:
            if not (
                block.get("seasonId") == SEASON
                and block.get("statSourceId") == 0
                and block.get("statSplitTypeId") == 1
                and block.get("stats")
            ):
                continue
            team = normalize_team(espn_client.ESPN_TEAM_MAP.get(block.get("proTeamId")))
            if not team:
                continue
            stats = bt._canonicalize(block["stats"])
            bucket = side.player_team.setdefault((str(pid), team), {})
            for stat, value in stats.items():
                bucket[stat] = bucket.get(stat, 0.0) + value
    return side


def actual_spine_coverage_lines(side: ActualSide) -> list[str]:
    """Bound the actuals spine's completeness against the cached nflreadpy weekly history.

    The ESPN payload is a 700-player window, so it can be short of the whole league. This says
    by how much, per yardage type, rather than assuming it is complete.
    """
    lines: list[str] = []
    try:
        sys.path.insert(0, str(REPO_ROOT / "tools"))
        from fetch_weekly_history import load_latest_weekly_history
    except Exception as exc:  # pragma: no cover - depends on the local cache
        return [f"  SKIPPED -- nflreadpy weekly cache unreadable ({exc})"]

    frame = load_latest_weekly_history()
    rows = frame.filter(frame["season"] == SEASON) if "season" in frame.columns else frame
    weeks = sorted(set(rows["week"].to_list()))
    if weeks and max(weeks) > 18:  # the older cached file still holds postseason weeks 19-22
        raise AssertionError(
            f"cached weekly history reaches week {max(weeks)}; that file includes postseason "
            "games and would inflate every season total"
        )
    espn_totals: dict[str, float] = {}
    for stats in side.player_team.values():
        for stat in ("pass_yd", "rush_yd", "rec_yd"):
            espn_totals[stat] = espn_totals.get(stat, 0.0) + stats.get(stat, 0.0)
    for stat in ("pass_yd", "rush_yd", "rec_yd"):
        if stat not in rows.columns:
            continue
        reference = float(sum(v or 0.0 for v in rows[stat].to_list()))
        ours = espn_totals.get(stat, 0.0)
        ratio = ours / reference if reference else float("nan")
        lines.append(
            f"  league 2025 {stat:<8} ESPN actuals {ours:>9,.0f} vs nflreadpy {reference:>9,.0f}"
            f"  -> spine covers {ratio:.1%}"
        )
    return lines


# ========================================================================== projection sides


@dataclass
class ProjectionSet:
    """One source's full 2025 projected board, with 2025 team attribution."""

    name: str
    lines: dict[str, dict[str, float]]
    teams: dict[str, str]

    def team_totals(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for key, line in self.lines.items():
            team = self.teams.get(key, "")
            if not team:
                continue
            bucket = out.setdefault(team, {})
            for stat, value in line.items():
                bucket[stat] = bucket.get(stat, 0.0) + value
        return out

    def team_player_count(self, team: str, stat: str) -> int:
        return sum(
            1
            for key, line in self.lines.items()
            if self.teams.get(key) == team and line.get(stat, 0.0) > 0
        )

    def unattributed(self) -> int:
        return sum(1 for key in self.lines if not self.teams.get(key))


def _espn_season_projection(player: Mapping[str, Any], season: int) -> dict[str, float] | None:
    """One player's season-total PROJECTION block for ``season``, canonicalized.

    ``backtest_sources._season_block`` hardcodes 2025 because that is all a 2025 backtest
    needs; the external-validity section below has to read the 2026 feed with the same code
    path, so the season is a parameter here.
    """
    for block in player.get("stats") or []:
        if (
            block.get("seasonId") == season
            and block.get("statSourceId") == 1
            and block.get("statSplitTypeId") == 0
            and block.get("stats")
        ):
            return bt._canonicalize(block["stats"])
    return None


def espn_projection_set_for_season(
    raw_players: Sequence[Mapping[str, Any]], season: int
) -> ProjectionSet:
    """ESPN's whole published board for ``season``, with that payload's own team attribution."""
    lines: dict[str, dict[str, float]] = {}
    teams: dict[str, str] = {}
    for entry in raw_players:
        player = entry.get("player") or {}
        pid = player.get("id")
        if pid is None:
            continue
        if espn_client.ESPN_POSITION_MAP.get(player.get("defaultPositionId")) not in POSITIONS:
            continue
        proj = _espn_season_projection(player, season)
        if not proj:
            continue
        lines[str(pid)] = proj
        teams[str(pid)] = normalize_team(espn_client.ESPN_TEAM_MAP.get(player.get("proTeamId")))
    return ProjectionSet(name="espn", lines=lines, teams=teams)


def espn_projection_set(espn: Mapping[str, bt.EspnRecord], teams: Mapping[str, str]) -> ProjectionSet:
    return ProjectionSet(
        name="espn",
        lines={pid: dict(rec.proj) for pid, rec in espn.items() if rec.proj},
        teams={pid: teams.get(pid, "") for pid in espn},
    )


def sleeper_projection_set(
    sleeper: Mapping[str, bt.SleeperRecord], teams: Mapping[str, str]
) -> ProjectionSet:
    return ProjectionSet(
        name="sleeper",
        lines={pid: dict(rec.proj) for pid, rec in sleeper.items()},
        teams={pid: teams.get(pid, "") for pid in sleeper},
    )


# ================================================================ how broken is each feed?


def identity_gaps(projection_set: ProjectionSet) -> dict[str, dict[str, float]]:
    """Per team, ``(receiving - passing) / passing`` for each identity pair.

    This is the quantity ``docs/PROJECTION_CHALLENGES.md`` measured on the 2026 feeds. Measured
    here on the SOURCE'S WHOLE PUBLISHED FEED rather than on the crosswalk-resolved board, so
    the number describes what the source published rather than what our joins kept.
    """
    out: dict[str, dict[str, float]] = {}
    for team, totals in projection_set.team_totals().items():
        row: dict[str, float] = {}
        for pass_stat, rec_stat in IDENTITY_PAIRS:
            p = float(totals.get(pass_stat, 0.0) or 0.0)
            r = float(totals.get(rec_stat, 0.0) or 0.0)
            if p > 0:
                row[rec_stat] = (r - p) / p
        if row:
            out[team] = row
    return out


def identity_distribution_lines(
    label: str, projection_set: ProjectionSet, *, threshold: float = 0.01
) -> list[str]:
    gaps = identity_gaps(projection_set)
    lines: list[str] = []
    for _pass_stat, rec_stat in IDENTITY_PAIRS:
        values = [row[rec_stat] for row in gaps.values() if rec_stat in row]
        if not values:
            continue
        over = sum(1 for v in values if v > threshold)
        under = sum(1 for v in values if v < -threshold)
        lines.append(
            f"  {label:<22} {rec_stat:<8} teams={len(values):>3}  median {statistics.median(values):+7.2%}"
            f"  min {min(values):+7.2%}  max {max(values):+7.2%}"
            f"  over>{threshold:.0%}: {over:>2}  under<-{threshold:.0%}: {under:>2}"
        )
    # The confound PROJECTION_CHALLENGES measured and this section has to keep visible: a team
    # whose quarterback room the source barely projected has a short passing side for no reason
    # but coverage, and its receivers then look inflated for free.
    totals = projection_set.team_totals()
    passer_counts = {t: projection_set.team_player_count(t, "pass_att") for t in totals}
    thin = [t for t, c in passer_counts.items() if c < 2]
    thick = [t for t, c in passer_counts.items() if c >= 2]
    thin_gaps = [gaps[t]["rec_yd"] for t in thin if t in gaps and "rec_yd" in gaps[t]]
    thick_gaps = [gaps[t]["rec_yd"] for t in thick if t in gaps and "rec_yd" in gaps[t]]
    lines.append(
        f"  {label:<22} {'context':<8} median team pass_att "
        f"{statistics.median([totals[t].get('pass_att', 0.0) for t in totals]):>6.0f}; "
        f"<2 projected passers: {len(thin)} teams"
        + (f" (median rec_yd gap {statistics.median(thin_gaps):+.1%})" if thin_gaps else "")
        + f"; >=2: {len(thick)} teams"
        + (f" (median {statistics.median(thick_gaps):+.1%})" if thick_gaps else "")
    )
    # The two extreme teams, with the passing volume behind them: a team whose whole quarterback
    # room the source barely committed to has a short passing side for no reason but coverage,
    # and naming it is the difference between a finding and an artifact.
    extremes = sorted(
        ((row["rec_yd"], team) for team, row in gaps.items() if "rec_yd" in row)
    )
    for value, team in (extremes[:1] + extremes[-1:]) if extremes else []:
        lines.append(
            f"  {label:<22} {'extreme':<8} {team} rec_yd gap {value:+.1%} on "
            f"{passer_counts.get(team, 0)} projected passers / "
            f"{totals[team].get('pass_att', 0.0):.0f} projected pass_att"
        )
    return lines


def projection_sets_2026() -> dict[str, ProjectionSet]:
    """The CURRENT feeds, read from ``data/raw/`` -- the board the remedy would actually touch.

    Read-only, and never through ``prep/fetch_all.py``: CLAUDE.md documents that a live fetch
    moves what ``load_latest_raw`` resolves to and breaks unrelated tests.
    """
    from draftroom.prep.http import load_latest_raw

    espn_raw = load_latest_raw("espn")
    sleeper_rows = load_latest_raw("sleeper_projections")
    espn_players = espn_raw.get("players") if isinstance(espn_raw, Mapping) else espn_raw
    sleeper = bt.sleeper_records(sleeper_rows)
    return {
        "sleeper": sleeper_projection_set(sleeper, sleeper_projection_teams(sleeper_rows)),
        "espn": espn_projection_set_for_season(espn_players or [], 2026),
    }


# =========================================================================== the remedies


def team_factors(
    team_totals: Mapping[str, Mapping[str, float]], remedy: str
) -> dict[str, dict[str, float]]:
    """Per team, the multiplier each identity stat gets under ``remedy``.

    ``rec_down``       scale receiving to the team's projected passing total (the proposal).
    ``rec_down_over``  same, but ONLY on teams where receiving exceeds passing.
    ``pass_up``        scale passing to the team's projected receiving total.
    ``pass_up_over``   same, but only on the overage teams.
    ``split``          move both to the midpoint, so neither side is assumed to be the truth.

    A pair where either side is zero gets a multiplier of 1.0: with no denominator there is no
    defensible target, and inventing one would be worse than leaving the incoherence visible.
    """
    if remedy not in REMEDY_SPECS:
        raise KeyError(f"unknown remedy {remedy!r}; expected one of {REMEDIES}")
    side, only_when_rec_high = REMEDY_SPECS[remedy]

    out: dict[str, dict[str, float]] = {}
    for team, totals in team_totals.items():
        factors: dict[str, float] = {}
        for pass_stat, rec_stat in IDENTITY_PAIRS:
            p = float(totals.get(pass_stat, 0.0) or 0.0)
            r = float(totals.get(rec_stat, 0.0) or 0.0)
            factors[pass_stat] = 1.0
            factors[rec_stat] = 1.0
            if p <= 0 or r <= 0:
                continue
            if only_when_rec_high and r <= p:
                continue
            if side == "rec":
                factors[rec_stat] = p / r
            elif side == "pass":
                factors[pass_stat] = r / p
            else:  # split
                mid = 0.5 * (p + r)
                factors[pass_stat] = mid / p
                factors[rec_stat] = mid / r
        out[team] = factors
    return out


def apply_factors(
    line: Mapping[str, float], factors: Mapping[str, float] | None
) -> dict[str, float]:
    """Rescale one statline's identity stats. Never mutates the input; shares are preserved.

    Only the six identity stats move. Rushing, targets, interceptions, fumbles and games are
    left exactly as the source published them -- the remedy is a level fix on one accounting
    identity, not a rewrite of the projection.
    """
    out = dict(line)
    if not factors:
        return out
    for stat in PASS_STATS + REC_STATS:
        if stat in out:
            out[stat] = float(out[stat]) * float(factors.get(stat, 1.0))
    return out


def level_matched_factors(
    projection_set: ProjectionSet, target_remedy: str, stats: Sequence[str]
) -> dict[str, dict[str, float]]:
    """One uniform multiplier per stat, sized to match ``target_remedy``'s LEAGUE-WIDE effect.

    The null hypothesis made concrete: same total taken off the same side of the ball, same
    players touched, but distributed flat instead of per team. Teams with no projection are
    left out of the factor map, exactly as they are under the identity remedies, so the two
    variants act on an identical population and the paired test is clean.
    """
    before = projection_set.team_totals()
    factors = team_factors(before, target_remedy)
    uniform: dict[str, float] = {}
    for stat in stats:
        base = sum(float(t.get(stat, 0.0) or 0.0) for t in before.values())
        after = sum(
            float(totals.get(stat, 0.0) or 0.0) * factors.get(team, {}).get(stat, 1.0)
            for team, totals in before.items()
        )
        uniform[stat] = after / base if base else 1.0
    return {team: dict(uniform) for team in before}


def corrected_lines(
    projection_set: ProjectionSet, remedy: str
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """Apply ``remedy`` to a whole projected board. Returns (corrected lines, factors used)."""
    if remedy in NULL_SPECS:
        target, stats = NULL_SPECS[remedy]
        factors = level_matched_factors(projection_set, target, stats)
    else:
        factors = team_factors(projection_set.team_totals(), remedy)
    out = {
        key: apply_factors(line, factors.get(projection_set.teams.get(key, "")))
        for key, line in projection_set.lines.items()
    }
    return out, factors


# ================================================================================ Q1 report


@dataclass
class SideRatio:
    stat: str
    projected: float
    actual_full: float
    actual_depth: float

    @property
    def ratio_full(self) -> float:
        return self.projected / self.actual_full if self.actual_full else float("nan")

    @property
    def ratio_depth(self) -> float:
        return self.projected / self.actual_depth if self.actual_depth else float("nan")


def side_ratios(
    projection_set: ProjectionSet, actuals: ActualSide, stats: Iterable[str]
) -> dict[str, SideRatio]:
    """League-wide projected vs actual, per stat, both raw and depth-matched."""
    proj_teams = projection_set.team_totals()
    actual_teams = actuals.team_totals()
    out: dict[str, SideRatio] = {}
    for stat in stats:
        projected = full = depth = 0.0
        for team, totals in proj_teams.items():
            if team not in actual_teams:
                continue
            value = float(totals.get(stat, 0.0) or 0.0)
            if value <= 0:
                continue
            projected += value
            full += float(actual_teams[team].get(stat, 0.0) or 0.0)
            depth += actuals.top_k_total(team, stat, projection_set.team_player_count(team, stat))
        out[stat] = SideRatio(stat, projected, full, depth)
    return out


def identity_closure_lines(
    actuals: ActualSide, *, gate: bool, tolerance: float = 0.02
) -> list[str]:
    """Does the actuals spine itself close the identity? If not, it cannot adjudicate one.

    ``tolerance`` applies to receptions and yards only. Touchdowns are small integer counts --
    one TD on a 15-TD team is 6.7% -- so the TD row is reported and never gated, exactly as
    ``valuation/envelope.py`` fits its own tolerances.
    """
    lines: list[str] = []
    team_totals = actuals.team_totals()
    for pass_stat, rec_stat in IDENTITY_PAIRS:
        worst = 0.0
        worst_team = ""
        for team, totals in team_totals.items():
            p = float(totals.get(pass_stat, 0.0) or 0.0)
            if p <= 0:
                continue
            dev = (float(totals.get(rec_stat, 0.0) or 0.0) - p) / p
            if abs(dev) > abs(worst):
                worst, worst_team = dev, team
        lines.append(
            f"     identity {rec_stat:<8} vs {pass_stat:<9} worst team deviation "
            f"{worst:+.2%} ({worst_team})"
        )
        if gate and rec_stat != "rec_td":
            assert abs(worst) <= tolerance, (
                f"the actuals spine does not close {rec_stat} == {pass_stat}: worst team "
                f"{worst_team} is off by {worst:+.2%}. A spine that cannot close the identity "
                f"cannot be used to decide which projected side violates it."
            )
    return lines


def q1_lines(
    projection_sets: Mapping[str, ProjectionSet], actuals: ActualSide, *, teams_detail: bool
) -> list[str]:
    out: list[str] = []
    actual_teams = actuals.team_totals()

    out.append("")
    out.append("PROJECTED vs ACTUAL, league-wide (ratio > 1 = the source projected more than")
    out.append("happened). 'depth' compares against the top-K actual players where K is how")
    out.append("many the source published for that team, which removes the coverage gap.")
    out.append(
        f"  {'source':<9} {'stat':<9} {'projected':>11} {'actual':>11} {'ratio':>7} "
        f"{'act(depth)':>11} {'ratio':>7}"
    )
    for name, pset in projection_sets.items():
        ratios = side_ratios(pset, actuals, PASS_STATS + REC_STATS + NEUTRAL_STATS)
        for stat in PASS_STATS + REC_STATS + NEUTRAL_STATS:
            r = ratios[stat]
            out.append(
                f"  {name:<9} {stat:<9} {r.projected:>11,.0f} {r.actual_full:>11,.0f} "
                f"{r.ratio_full:>7.3f} {r.actual_depth:>11,.0f} {r.ratio_depth:>7.3f}"
            )
        out.append("")

    out.append("THE DECISIVE CUT: each side's ratio against the source's own NEUTRAL rushing")
    out.append("ratio. Shared availability optimism cancels; what is left is which side moved.")
    out.append(
        f"  {'source':<9} {'basis':<7} {'pass ratio':>11} {'rec ratio':>11} "
        f"{'rush ratio':>11} {'pass/rush':>10} {'rec/rush':>9} {'verdict':<28}"
    )
    verdicts: dict[str, str] = {}
    for name, pset in projection_sets.items():
        ratios = side_ratios(pset, actuals, PASS_STATS + REC_STATS + NEUTRAL_STATS)
        for basis in ("full", "depth"):
            def get(stat: str) -> float:
                r = ratios[stat]
                return r.ratio_full if basis == "full" else r.ratio_depth

            pass_r = statistics.fmean([get(s) for s in PASS_STATS])
            rec_r = statistics.fmean([get(s) for s in REC_STATS])
            rush_r = statistics.fmean([get(s) for s in NEUTRAL_STATS])
            pass_gap = pass_r / rush_r if rush_r else float("nan")
            rec_gap = rec_r / rush_r if rush_r else float("nan")
            verdict = _guilty_side(pass_gap, rec_gap)
            if basis == "depth":
                verdicts[name] = verdict
            out.append(
                f"  {name:<9} {basis:<7} {pass_r:>11.3f} {rec_r:>11.3f} {rush_r:>11.3f} "
                f"{pass_gap:>10.3f} {rec_gap:>9.3f} {verdict:<28}"
            )

    if teams_detail:
        out.append("")
        out.append("PER-TEAM DETAIL (depth-matched receiving ratio, and the internal identity gap)")
        for name, pset in projection_sets.items():
            proj_teams = pset.team_totals()
            out.append(f"  -- {name}")
            out.append(
                f"    {'team':<5} {'proj rec_yd':>12} {'act rec_yd':>11} {'ratio':>7} "
                f"{'proj pass_yd':>13} {'act pass_yd':>12} {'ratio':>7} {'identity':>9}"
            )
            for team in sorted(proj_teams):
                if team not in actual_teams:
                    continue
                totals = proj_teams[team]
                p_rec = totals.get("rec_yd", 0.0)
                p_pass = totals.get("pass_yd", 0.0)
                a_rec = actuals.top_k_total(team, "rec_yd", pset.team_player_count(team, "rec_yd"))
                a_pass = actuals.top_k_total(
                    team, "pass_yd", pset.team_player_count(team, "pass_yd")
                )
                identity = (p_rec - p_pass) / p_pass if p_pass else float("nan")
                out.append(
                    f"    {team:<5} {p_rec:>12,.0f} {a_rec:>11,.0f} "
                    f"{(p_rec / a_rec if a_rec else float('nan')):>7.3f} "
                    f"{p_pass:>13,.0f} {a_pass:>12,.0f} "
                    f"{(p_pass / a_pass if a_pass else float('nan')):>7.3f} {identity:>+9.2%}"
                )
            out.append("")

    out.append("")
    out.append("PER-TEAM ROBUSTNESS: the league-wide ratios above could be driven by a few big")
    out.append("offenses, so count teams instead. Depth-matched, yards.")
    out.append(
        f"  {'source':<9} {'teams':>6} {'pass over-projected':>20} {'rec over-projected':>19} "
        f"{'passing the WORSE side':>23}"
    )
    for name, pset in projection_sets.items():
        proj_teams = pset.team_totals()
        n = pass_over = rec_over = pass_worse = 0
        for team, totals in proj_teams.items():
            if team not in actual_teams:
                continue
            a_pass = actuals.top_k_total(team, "pass_yd", pset.team_player_count(team, "pass_yd"))
            a_rec = actuals.top_k_total(team, "rec_yd", pset.team_player_count(team, "rec_yd"))
            if a_pass <= 0 or a_rec <= 0:
                continue
            n += 1
            p_ratio = totals.get("pass_yd", 0.0) / a_pass
            r_ratio = totals.get("rec_yd", 0.0) / a_rec
            pass_over += p_ratio > 1.0
            rec_over += r_ratio > 1.0
            pass_worse += p_ratio > r_ratio
        out.append(
            f"  {name:<9} {n:>6} {pass_over:>20} {rec_over:>19} {pass_worse:>23}"
        )

    out.append("")
    for name, verdict in verdicts.items():
        out.append(f"  Q1 [{name}]: {verdict}")
    return out


def _guilty_side(pass_gap: float, rec_gap: float, *, tolerance: float = 0.03) -> str:
    """Name the guilty side from the two neutral-adjusted ratios.

    ``tolerance`` is the band inside which a side is called consistent with the source's own
    general optimism. 3% is one standard NFL team-total rounding's worth and is deliberately
    generous: the point is to avoid convicting a side on noise.
    """
    pass_off = pass_gap - 1.0
    rec_off = rec_gap - 1.0
    if math.isnan(pass_gap) or math.isnan(rec_gap):
        return "undetermined"
    if abs(pass_off) <= tolerance and abs(rec_off) <= tolerance:
        return "both sides consistent"
    if abs(rec_off) > tolerance >= abs(pass_off):
        return f"RECEIVING is the outlier ({rec_off:+.1%})"
    if abs(pass_off) > tolerance >= abs(rec_off):
        return f"PASSING is the outlier ({pass_off:+.1%})"
    return f"BOTH off (pass {pass_off:+.1%}, rec {rec_off:+.1%})"


# ================================================================================ Q2 report


@dataclass
class Scored:
    """One (source, remedy) combination's projected points for every player, in order."""

    label: str
    projected: list[float]


def _blend(
    a: Mapping[str, float], b: Mapping[str, float], games_variation: Mapping[str, bool]
) -> dict[str, float]:
    """Sleeper+ESPN component-stat blend, honouring the constant-games rule.

    ``games_variation`` is threaded rather than assumed: Sleeper's 2025 ``gp`` is the constant
    18.0 for every player, and averaging it against ESPN's real per-player figure put a
    placeholder halfway into a forecast (Codex 2026-08-21 finding 9). This module's whole output
    is a MAE comparison, so a corrupted games figure moves the very numbers the renormalization
    verdict rests on.
    """
    return bt.blend_statlines([a, b], games_varies=bt.blend_games_mask(games_variation))


def score_population(
    players: Sequence[bt.MatchedPlayer],
    corrected: Mapping[str, Mapping[str, Mapping[str, float]]],
    scoring: Mapping[str, float],
    *,
    bonus: bool = False,
    schedule: Any = None,
    curves: Any = None,
    games_cap: float | None = None,
    games_variation: Mapping[str, bool] | None = None,
) -> tuple[dict[str, list[float]], list[float]]:
    """Score every (source x remedy) variant on the same players, plus the actuals.

    ``corrected[remedy][source][key]`` is the corrected statline. ``remedy == "raw"`` is the
    published projection. The blend is built by correcting each source FIRST and blending the
    corrected component stats -- which is what the pipeline would actually do, and it keeps the
    blend an average of two fixed sources rather than a fix applied to an average.
    """
    out: dict[str, list[float]] = {}
    # Measured from the pool in hand when the caller did not already measure it, so this
    # function cannot be called into the old (silently wrong) behaviour.
    variation = dict(games_variation) if games_variation else bt.measure_games_variation(players)
    actual = [bt.actual_points(p, scoring, bonus=bonus, schedule=schedule) for p in players]
    for remedy, by_source in corrected.items():
        for source in SOURCES:
            key = f"{source}/{remedy}"
            values: list[float] = []
            for player in players:
                sl = by_source["sleeper"].get(player.sleeper_pid, player.sleeper_proj)
                es = by_source["espn"].get(player.espn_id, player.espn_proj)
                line = {
                    "sleeper": sl,
                    "espn": es,
                    "blend": _blend(sl, es, variation),
                }[source]
                values.append(
                    bt.league_points(
                        line,
                        scoring,
                        pos=player.pos,
                        bonus=bonus,
                        schedule=schedule,
                        curves=curves,
                        games_cap=games_cap,
                    )
                )
            out[key] = values
    return out, actual


def q2_lines(
    players: Sequence[bt.MatchedPlayer],
    scored: Mapping[str, list[float]],
    actual: Sequence[float],
    *,
    title: str,
) -> list[str]:
    out: list[str] = []
    out.append("")
    out.append(title)
    groups: list[tuple[str, list[int]]] = [
        ("all", list(range(len(players)))),
        ("in 2025 ADP feed", [i for i, p in enumerate(players) if p.adp_rank is not None]),
    ]
    for pos in POSITIONS:
        groups.append((pos, [i for i, p in enumerate(players) if p.pos == pos]))
    for label, lo, hi in bt.ADP_TIERS:
        groups.append(
            (label, [i for i, p in enumerate(players) if p.adp_rank and lo <= p.adp_rank <= hi])
        )
    groups.append(
        (bt.UNRANKED_TIER, [i for i, p in enumerate(players) if p.adp_rank is None])
    )

    header = f"  {'group':<16} {'n':>4} {'source':<9}"
    for remedy in ("raw",) + REMEDIES:
        header += f" {remedy:>10}"
    header += f" {'best':>10}"
    out.append(header)
    for label, idx in groups:
        if len(idx) < 3:
            continue
        actual_slice = [actual[i] for i in idx]
        for source in SOURCES:
            maes: dict[str, float] = {}
            for remedy in ("raw",) + REMEDIES:
                proj = [scored[f"{source}/{remedy}"][i] for i in idx]
                maes[remedy] = bt.metrics(proj, actual_slice).mae
            best = min(maes, key=lambda r: maes[r])
            row = f"  {label:<16} {len(idx):>4} {source:<9}"
            for remedy in ("raw",) + REMEDIES:
                row += f" {maes[remedy]:>10.2f}"
            row += f" {best:>10}"
            out.append(row)
        out.append("")
    return out


def paired_lines(
    scored: Mapping[str, list[float]],
    actual: Sequence[float],
    players: Sequence[bt.MatchedPlayer],
) -> list[str]:
    """Every remedy against the published projection, paired, on the same players."""
    out: list[str] = []
    out.append("")
    out.append("IS ANY OF IT REAL? Paired on the same players, |error| after minus |error|")
    out.append("before. Negative = the remedy HELPED. 10,000 bootstrap resamples, seed fixed.")
    populations: list[tuple[str, list[int]]] = [
        ("all", list(range(len(players)))),
        ("in 2025 ADP feed", [i for i, p in enumerate(players) if p.adp_rank is not None]),
        ("ADP 1-60", [i for i, p in enumerate(players) if p.adp_rank and p.adp_rank <= 60]),
        ("WR", [i for i, p in enumerate(players) if p.pos == "WR"]),
        ("TE", [i for i, p in enumerate(players) if p.pos == "TE"]),
        ("QB", [i for i, p in enumerate(players) if p.pos == "QB"]),
    ]
    out.append(
        f"  {'population':<18} {'n':>4} {'source':<9} {'remedy':<9} {'MAE gap':>9} "
        f"{'95% CI':>20} {'p':>7}  read"
    )
    for label, idx in populations:
        if len(idx) < 5:
            continue
        actual_slice = [actual[i] for i in idx]
        for source in SOURCES:
            base = [scored[f"{source}/raw"][i] for i in idx]
            err_base = [p - a for p, a in zip(base, actual_slice)]
            for remedy in REMEDIES:
                after = [scored[f"{source}/{remedy}"][i] for i in idx]
                err_after = [p - a for p, a in zip(after, actual_slice)]
                res = bt.paired_compare(err_after, err_base)
                if math.isnan(res.mean_diff) or abs(res.mean_diff) < 1e-9:
                    read = "no change"
                elif res.p_value < 0.05:
                    read = "HELPED" if res.mean_diff < 0 else "HURT"
                else:
                    read = "not distinguishable"
                out.append(
                    f"  {label:<18} {len(idx):>4} {source:<9} {remedy:<9} "
                    f"{res.mean_diff:>+9.2f} "
                    f"{f'{res.ci_low:+.2f} .. {res.ci_high:+.2f}':>20} {res.p_value:>7.3f}  {read}"
                )
        out.append("")
    return out


def null_test_lines(
    scored: Mapping[str, list[float]],
    actual: Sequence[float],
    players: Sequence[bt.MatchedPlayer],
) -> list[str]:
    """THE decisive test: does the identity remedy beat a flat haircut of the same size?

    If it does not, then whatever MAE it bought came from taking points off an optimistic board,
    not from reconciling anything, and the accounting identity added no information.
    """
    out: list[str] = []
    out.append("")
    out.append("THE NULL TEST -- identity remedy vs a FLAT haircut of the same league-wide size.")
    out.append("Negative = the per-team identity beat the flat cut. If it is not negative and")
    out.append("significant, the identity contributed nothing and the gain was just a haircut.")
    populations: list[tuple[str, list[int]]] = [
        ("all", list(range(len(players)))),
        ("in 2025 ADP feed", [i for i, p in enumerate(players) if p.adp_rank is not None]),
        ("ADP 1-60", [i for i, p in enumerate(players) if p.adp_rank and p.adp_rank <= 60]),
        ("WR", [i for i, p in enumerate(players) if p.pos == "WR"]),
    ]
    out.append(
        f"  {'population':<18} {'n':>4} {'source':<9} {'identity vs null':<28} "
        f"{'MAE gap':>9} {'95% CI':>20} {'p':>7}  read"
    )
    for null, (target, _stats) in NULL_SPECS.items():
        for label, idx in populations:
            if len(idx) < 5:
                continue
            actual_slice = [actual[i] for i in idx]
            for source in SOURCES:
                err_identity = [
                    scored[f"{source}/{target}"][i] - actual_slice[j] for j, i in enumerate(idx)
                ]
                err_null = [
                    scored[f"{source}/{null}"][i] - actual_slice[j] for j, i in enumerate(idx)
                ]
                res = bt.paired_compare(err_identity, err_null)
                if math.isnan(res.mean_diff) or abs(res.mean_diff) < 1e-9:
                    read = "identical"
                elif res.p_value < 0.05:
                    read = "identity WINS" if res.mean_diff < 0 else "flat cut WINS"
                else:
                    read = "not distinguishable"
                out.append(
                    f"  {label:<18} {len(idx):>4} {source:<9} {f'{target} vs {null}':<28} "
                    f"{res.mean_diff:>+9.2f} "
                    f"{f'{res.ci_low:+.2f} .. {res.ci_high:+.2f}':>20} {res.p_value:>7.3f}  {read}"
                )
            out.append("")
    return out


def magnitude_lines(
    players: Sequence[bt.MatchedPlayer], scored: Mapping[str, list[float]]
) -> list[str]:
    """How much does each remedy even move? A remedy that moves nothing cannot help or hurt."""
    out: list[str] = []
    out.append("")
    out.append("HOW BIG IS THE CORRECTION? mean |change| in season league points per player")
    out.append(f"  {'source':<9} {'remedy':<9} {'mean |d|':>9} {'max |d|':>9} {'mean d':>9}")
    for source in SOURCES:
        base = scored[f"{source}/raw"]
        for remedy in REMEDIES:
            after = scored[f"{source}/{remedy}"]
            deltas = [a - b for a, b in zip(after, base)]
            out.append(
                f"  {source:<9} {remedy:<9} "
                f"{statistics.fmean(abs(d) for d in deltas):>9.2f} "
                f"{max((abs(d) for d in deltas), default=0.0):>9.2f} "
                f"{statistics.fmean(deltas):>+9.2f}"
            )
    return out


def verdict_lines(
    players: Sequence[bt.MatchedPlayer],
    scored: Mapping[str, list[float]],
    actual: Sequence[float],
) -> list[str]:
    """The one thing a reader must not have to assemble by hand.

    For each source: the published MAE, the best remedy, how much it buys against the published
    projection -- and then the number that decides it, whether that remedy beats a flat haircut
    of its own size. A remedy that cannot beat its null has not earned a place in the pipeline
    no matter how good its headline looks.
    """
    out: list[str] = []
    idx_all = list(range(len(players)))
    idx_adp = [i for i, p in enumerate(players) if p.adp_rank is not None]
    for label, idx in (("all matched", idx_all), ("in 2025 ADP feed", idx_adp)):
        actual_slice = [actual[i] for i in idx]
        for source in SOURCES:
            raw = [scored[f"{source}/raw"][i] for i in idx]
            raw_mae = bt.metrics(raw, actual_slice).mae
            maes = {
                remedy: bt.metrics([scored[f"{source}/{remedy}"][i] for i in idx], actual_slice).mae
                for remedy in REMEDIES
            }
            best = min(maes, key=lambda r: maes[r])
            err_raw = [p - a for p, a in zip(raw, actual_slice)]
            err_best = [
                scored[f"{source}/{best}"][i] - actual_slice[j] for j, i in enumerate(idx)
            ]
            vs_raw = bt.paired_compare(err_best, err_raw)
            null_note = "no matched null"
            for null, (target, _stats) in NULL_SPECS.items():
                if target != best:
                    continue
                err_null = [
                    scored[f"{source}/{null}"][i] - actual_slice[j] for j, i in enumerate(idx)
                ]
                res = bt.paired_compare(err_best, err_null)
                null_note = (
                    f"vs {null}: {res.mean_diff:+.2f} p={res.p_value:.3f} "
                    f"({'identity wins' if res.p_value < 0.05 and res.mean_diff < 0 else 'NOT distinguishable'})"
                )
            out.append(
                f"  {label:<17} {source:<9} raw MAE {raw_mae:6.2f} -> best remedy "
                f"{best:<14} {maes[best]:6.2f} "
                f"(gap {vs_raw.mean_diff:+.2f}, p={vs_raw.p_value:.3f}); {null_note}"
            )
        out.append("")
    return out


# ==================================================================================== report


def build_report(*, teams_detail: bool = False) -> str:
    from draftroom.prep.http import load_latest_raw

    espn_raw = bt.load_cached(bt.ESPN_CACHE)
    sleeper_raw = bt.load_cached(bt.SLEEPER_CACHE)
    ffc_raw = bt.load_cached(bt.FFC_CACHE)
    universe = load_latest_raw("sleeper")  # READ ONLY

    # The harness's own hard gate, re-run here rather than trusted: a wrong ESPN stat id puts
    # plausible numbers in the wrong field and every table below would be quietly wrong.
    id_check_lines = bt.verify_espn_stat_ids(espn_raw["players"])

    espn = bt.espn_records(espn_raw["players"])
    sleeper = bt.sleeper_records(sleeper_raw)
    everyone, join_counts, _dropped = bt.join_sources(espn, sleeper, universe)
    adp_hits = bt.attach_adp(everyone, ffc_raw)

    cfg = LeagueConfig.from_yaml()
    scoring = cfg.scoring
    schedule = bt.load_bonus_schedule()
    curves = bt.load_curves()

    matched = [p for p in everyone if p.actual_status != "missing_block"]
    unobserved = [p for p in everyone if p.actual_status == "missing_block"]

    vintage = team_vintage_lines(espn_raw["players"], sleeper_raw)

    # The ACTUALS SPINE for team totals comes from the cached 2026 ESPN payload's 2025 WEEKLY
    # actual blocks, not from the 2025 backtest payload. Both carry the same numbers for the
    # players they share; the difference is the window. The 2025 payload is a 700-player pull
    # and misses ~2% of the league's real production, and it misses it UNEVENLY -- NYJ's real
    # receiving comes out 23% above its own real completions, which is impossible and is purely
    # a missing quarterback. A spine that cannot close the identity cannot adjudicate it. The
    # 2026 payload is a 1000-player pull, closes the identity to 0.7%, and matches nflreadpy's
    # league yardage to 0.1%. Gate 3 prints both so the choice is visible rather than asserted.
    #
    # This is the TEAM-LEVEL spine only. Q2's per-player actuals are the harness's own
    # (``bt.actual_points`` over the 2025 payload's season blocks), unchanged and untouched.
    actuals_wide = actual_side(_espn_2026_players())
    actuals_narrow = actual_side(espn_raw["players"])
    actuals = actuals_wide

    espn_set = espn_projection_set(espn, espn_projection_teams(espn_raw["players"]))
    sleeper_set = sleeper_projection_set(sleeper, sleeper_projection_teams(sleeper_raw))
    projection_sets = {"sleeper": sleeper_set, "espn": espn_set}

    out: list[str] = []
    out.append("=" * 100)
    out.append("SHOULD THE RECEIVING SIDE BE RENORMALIZED? -- measured on 2025, not argued")
    out.append("=" * 100)
    out.append(
        f"league: {cfg.teams} teams, starters {dict(cfg.starters)}, flex {cfg.flex_slots}, "
        f"{cfg.weeks} weeks, pass_int {scoring.get('pass_int')}"
    )
    out.append(
        "spine: tools/backtest_sources.py (verified ESPN stat ids, one actuals source, "
        "vintage checked by content, seeded bootstraps). This tool adds team attribution only."
    )

    out.append("")
    out.append("GATE 1: ESPN stat-id identities (projection AND actual blocks)")
    out.extend(id_check_lines)

    out.append("")
    out.append("GATE 2: TEAM ATTRIBUTION IS 2025, NOT 2026 (the trap in PROJECTION_CHALLENGES)")
    out.extend(vintage)

    out.append("")
    out.append("GATE 3: is the actuals spine complete enough to adjudicate an identity?")
    out.append("  -- the 2025 backtest payload (700-player window):")
    out.extend(actual_spine_coverage_lines(actuals_narrow))
    out.extend(identity_closure_lines(actuals_narrow, gate=False))
    out.append("  -- the cached 2026 payload's 2025 weekly blocks (1000-player window)  <- USED:")
    out.extend(actual_spine_coverage_lines(actuals_wide))
    out.extend(identity_closure_lines(actuals_wide, gate=True))

    out.append("")
    out.append("POPULATION")
    out.append(
        f"  primary (ESPN proj + Sleeper proj + OBSERVED 2025 production): {len(matched)}"
    )
    out.append(
        f"    joined by Sleeper's own espn_id {join_counts['espn_id']}, "
        f"by normalized name+position {join_counts['name_pos']}, "
        f"with a 2025 ADP {adp_hits}"
    )
    out.append(
        f"    kept: {join_counts['actual_empty_block']} players with an EMPTY ESPN actual "
        f"block -- projected real production, recorded none, scored as a true zero. Dropping "
        f"them would flatter whichever side was more optimistic."
    )
    out.append(
        f"    held out: {len(unobserved)} with NO actual block at all (unobserved, not zero); "
        f"sensitivity below."
    )
    for name, pset in projection_sets.items():
        out.append(
            f"  {name} projected board: {len(pset.lines)} players, "
            f"{pset.unattributed()} with no 2025 team (excluded from team totals), "
            f"median receivers/team "
            f"{statistics.median([pset.team_player_count(t, 'rec') for t in pset.team_totals()]):.0f}, "
            f"passers/team "
            f"{statistics.median([pset.team_player_count(t, 'pass_att') for t in pset.team_totals()]):.0f}"
        )

    out.append("")
    out.append("=" * 100)
    out.append("Q0. EXTERNAL VALIDITY: is the 2025 feed as incoherent as the 2026 feed we")
    out.append("    actually want to fix? Same method, both seasons, whole published feeds.")
    out.append("=" * 100)
    sets_2026 = projection_sets_2026()
    for season_label, sets in (("2025", projection_sets), ("2026", sets_2026)):
        for name, pset in sets.items():
            out.extend(identity_distribution_lines(f"{name} {season_label}", pset))
    out.append("")
    out.append(
        "  A remedy tested on a season where the incoherence is smaller than it is today is a "
        "weaker test than it looks. Compare the medians and the over-count directly."
    )

    out.append("")
    out.append("=" * 100)
    out.append("Q1. WHICH SIDE IS ACTUALLY WRONG?")
    out.append("=" * 100)
    out.extend(q1_lines(projection_sets, actuals, teams_detail=teams_detail))

    # ---------------------------------------------------------------- Q2
    corrected: dict[str, dict[str, dict[str, dict[str, float]]]] = {
        "raw": {"sleeper": sleeper_set.lines, "espn": espn_set.lines}
    }
    factor_report: list[str] = []
    for remedy in REMEDIES:
        corrected[remedy] = {}
        for name, pset in projection_sets.items():
            lines, factors = corrected_lines(pset, remedy)
            corrected[remedy][name] = lines
            rec_factors = [f.get(REC_STATS[1], 1.0) for f in factors.values()] or [1.0]
            pass_factors = [f.get(PASS_STATS[1], 1.0) for f in factors.values()] or [1.0]
            factor_report.append(
                f"  {name:<9} {remedy:<9} rec_yd multiplier median "
                f"{statistics.median(rec_factors):.4f} "
                f"(min {min(rec_factors):.4f}, max {max(rec_factors):.4f}); "
                f"pass_yd multiplier median {statistics.median(pass_factors):.4f}"
            )

    scored, actual = score_population(matched, corrected, scoring)

    out.append("")
    out.append("=" * 100)
    out.append("Q2. WOULD IT HAVE IMPROVED 2025 PLAYER-LEVEL ACCURACY?")
    out.append("=" * 100)
    out.append("")
    out.append("THE MULTIPLIERS EACH REMEDY APPLIES (per team, from that team's own totals)")
    out.extend(factor_report)
    out.extend(magnitude_lines(matched, scored))
    out.extend(
        q2_lines(
            matched,
            scored,
            actual,
            title="MAE in season league points -- no per-game bonus (lower is better)",
        )
    )
    out.extend(paired_lines(scored, actual, matched))
    out.extend(null_test_lines(scored, actual, matched))

    scored_bonus, actual_bonus_pts = score_population(
        matched,
        corrected,
        scoring,
        bonus=True,
        schedule=schedule,
        curves=curves,
        games_cap=float(cfg.weeks),
    )
    out.extend(
        q2_lines(
            matched,
            scored_bonus,
            actual_bonus_pts,
            title=(
                "SAME, including the per-game yardage bonus (projections get the modelled "
                "bonus, actuals get the real one)"
            ),
        )
    )

    out.append("")
    out.append("=" * 100)
    out.append("VERDICT, computed from the run above (no narrative, just the decisive numbers)")
    out.append("=" * 100)
    out.extend(verdict_lines(matched, scored, actual))

    out.append("")
    out.append("SURVIVORSHIP SENSITIVITY")
    if not unobserved:
        out.append(
            "  0 players had a projection and no 2025 actual block, so there is nothing to "
            "hold out and no judgement call to make this year."
        )
    else:
        with_unobs = list(matched) + list(unobserved)
        scored_all, actual_all = score_population(with_unobs, corrected, scoring)
        out.append(
            f"  re-running with all {len(with_unobs)} players, unobserved production scored "
            f"as zero:"
        )
        for source in SOURCES:
            row = f"    {source:<9}"
            for remedy in ("raw",) + REMEDIES:
                row += f" {remedy}={bt.metrics(scored_all[f'{source}/{remedy}'], actual_all).mae:.2f}"
            out.append(row)

    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--teams-detail", action="store_true", help="print the per-team Q1 rows as well"
    )
    args = parser.parse_args(argv)
    print(build_report(teams_detail=args.teams_detail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
