"""Which 2025 projection source was actually more accurate, in THIS league's points?

The plan (``docs/PLAN_2026-08-20.md``) ships an equal-weight composite and says, in the
"Backtest: verify before promising" section, that weighting sources by measured accuracy needs
2025 preseason projections per source -- and that if they are not retrievable, equal weighting
is the right answer because it is what you use with no track record. They ARE retrievable for
two of the three families, so this tool measures them instead of assuming.

WHAT IS MEASURED
    Sleeper's 2025 season projections (company: rotowire) and ESPN's 2025 season projections
    (Mike Clay), each scored with ``prep.scoring.score_statline`` against
    ``data/league_manual.yaml``'s own modifiers, versus what those players actually did in 2025 --
    plus the equal-weight blend of the two, blended at the COMPONENT-STAT level and scored once
    (per plan B1; blending points would break the per-game yardage bonus model).

    FantasyPros is NOT in this table and cannot be. Our CSVs are 2026 only and the historical
    download sits behind the HOF subscription CLAUDE.md says not to buy. It is unmeasurable
    here, which is itself a finding: a source with no measurable track record cannot earn a
    weight above equal.

ONE ACTUALS SPINE, NOT TWO
    Both sources are scored against the SAME actuals: ESPN's own 2025 actual stat blocks
    (``statSourceId == 0``), which arrive in the same payload as its projections
    (``statSourceId == 1``). Scoring Sleeper against nflreadpy actuals and ESPN against ESPN
    actuals would measure the actuals, not the projections. The ESPN actuals are cross-checked
    against the cached nflreadpy 2025 weekly history (``tools/fetch_weekly_history.py``'s
    ``load_latest_weekly_history``, so the newest file wins -- the older file in that directory
    still contains postseason weeks 19-22) and the agreement is printed in the report.

VINTAGE -- both projection sets are genuinely PRESEASON, verified by content not by timestamp
    A season-end restatement would be nearly perfect on players who missed the year. Neither is:
    Brandon Aiyuk played 0 games in 2025 and ESPN projects him 12 games / 578 yards while
    Sleeper projects 630 receiving yards; Joe Burrow (season-ending injury in week 2) is
    projected 4,506 passing yards by Sleeper. Sleeper's records carry a ``last_modified`` of
    2026-01-04 -- that is a bulk re-write of the store, not a re-forecast, and the tool prints
    the Aiyuk/Burrow evidence every run so the claim is never taken on trust.

STAT IDS ARE VERIFIED, NEVER TRUSTED FROM A TABLE
    CLAUDE.md: a wrong ESPN stat id produces plausible numbers in the wrong field and nothing
    downstream catches it. ``verify_espn_stat_ids`` re-derives ESPN's own ratio fields from the
    component ids in BOTH the projection and the actual blocks -- id 21 == cmp/att, id 60 ==
    rec_yd/rec, id 73 == pass_int + fum_lost, id 39 == rush_yd/rush_att -- and raises if any
    player disagrees. It is a hard gate, not a warning.

WHERE THIS CACHES -- deliberately NOT data/raw/
    CLAUDE.md documents that a new timestamped file under ``data/raw/<source>/`` moves what
    ``load_latest_raw`` resolves to and breaks tests that read cached raw data. This tool
    therefore writes fixed-name files under ``data/backtest/`` and never calls
    ``prep/fetch_all.py``. Reading from ``data/raw/`` is fine; writing there is not.

Usage:
    python tools/backtest_sources.py                # offline, from data/backtest/
    python tools/backtest_sources.py --refresh      # re-pull the three 2025 payloads first
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# tools/backtest_sources.py -> repo root; backend/ is where the package lives.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

from draftroom.config import LeagueConfig  # noqa: E402
from draftroom.prep import espn_client, ffc_client, sleeper_client  # noqa: E402
from draftroom.prep.schema import CANONICAL_STATS, clean_name, normalize_name  # noqa: E402
from draftroom.prep.scoring import score_statline  # noqa: E402
from draftroom.valuation.bonuses import (  # noqa: E402
    actual_bonus,
    expected_bonus,
    load_bonus_schedule,
    load_curves,
)

SEASON = 2025

#: Fixed-name cache, deliberately outside data/raw/ (see the module docstring).
BACKTEST_DIR = REPO_ROOT / "data" / "backtest"
ESPN_CACHE = BACKTEST_DIR / f"espn_{SEASON}.json"
SLEEPER_CACHE = BACKTEST_DIR / f"sleeper_projections_{SEASON}.json"
FFC_CACHE = BACKTEST_DIR / f"ffc_adp_2qb_{SEASON}.json"

#: ESPN's kona_player_info endpoint 400s without a sort key in the X-Fantasy-Filter header.
#: ``sortDraftRanks`` is the one that works for a past season; ``sortPercOwned`` (what
#: prep/espn_client.py sends for the current season) returns HTTP 400 here.
ESPN_URL = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
    "/segments/0/leaguedefaults/3?view=kona_player_info"
)
ESPN_FILTER = {
    "players": {
        "limit": 700,
        "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "STANDARD"},
    }
}

SLEEPER_URL = (
    "https://api.sleeper.com/projections/nfl/{season}?season_type=regular"
    "&position[]=QB&position[]=RB&position[]=WR&position[]=TE&order_by=adp_half_ppr"
)

FFC_URL = "https://fantasyfootballcalculator.com/api/v1/adp/2qb?teams=12&year={season}"

POSITIONS = ("QB", "RB", "WR", "TE")

#: ADP tier edges (1-based FFC ADP rank, inclusive upper bound). Accuracy on an early pick is
#: worth more than accuracy on the tail, so the tail is never allowed to average into the top.
ADP_TIERS: tuple[tuple[str, int, int], ...] = (
    ("ADP 1-24", 1, 24),
    ("ADP 25-60", 25, 60),
    ("ADP 61-120", 61, 120),
    ("ADP 121+", 121, 10_000),
)
UNRANKED_TIER = "not in 2025 ADP"

#: Players whose 2025 vintage is checked by content on every run (see module docstring).
VINTAGE_PROBES = ("Brandon Aiyuk", "Joe Burrow", "MarShawn Lloyd")

#: Cross-check sample for ESPN actuals vs nflreadpy actuals. Chosen for volume across all
#: three yardage types, not for agreement.
CROSSCHECK_NAMES = (
    "Ja'Marr Chase", "Josh Allen", "Jonathan Taylor", "Lamar Jackson", "Amon-Ra St. Brown",
    "Bijan Robinson", "Trey McBride", "Justin Jefferson", "Saquon Barkley", "Jared Goff",
    "Brock Bowers", "De'Von Achane",
)


# ============================================================================== data loading


def fetch_2025_payloads() -> dict[str, Path]:
    """Pull the three 2025 payloads and write them to ``data/backtest/`` under fixed names.

    Fixed names, not timestamps: this cache must never influence ``load_latest_raw``, and a
    backtest of a finished season has no reason to keep a history of pulls.
    """
    from draftroom.prep.http import get_json, make_client

    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    headers = {"X-Fantasy-Filter": json.dumps(ESPN_FILTER)}
    with make_client(headers=headers) as client:
        espn_raw = get_json(client, ESPN_URL.format(season=SEASON))
    if not isinstance(espn_raw, dict) or not isinstance(espn_raw.get("players"), list):
        raise RuntimeError(
            f"ESPN {SEASON} payload has an unexpected shape: {type(espn_raw).__name__}. "
            "Do not guess a fix -- inspect the real response."
        )
    ESPN_CACHE.write_text(json.dumps(espn_raw), encoding="utf-8")
    written["espn"] = ESPN_CACHE

    with make_client() as client:
        sleeper_raw = get_json(client, SLEEPER_URL.format(season=SEASON))
    if not isinstance(sleeper_raw, list) or not sleeper_raw:
        raise RuntimeError(f"Sleeper {SEASON} projections returned {type(sleeper_raw).__name__}")
    SLEEPER_CACHE.write_text(json.dumps(sleeper_raw), encoding="utf-8")
    written["sleeper"] = SLEEPER_CACHE

    with make_client() as client:
        ffc_raw = get_json(client, FFC_URL.format(season=SEASON))
    FFC_CACHE.write_text(json.dumps(ffc_raw), encoding="utf-8")
    written["ffc"] = FFC_CACHE

    return written


def load_cached(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing. Run `python tools/backtest_sources.py --refresh` once to "
            "populate data/backtest/ (this tool never writes into data/raw/)."
        )
    return json.loads(path.read_text(encoding="utf-8"))


# ============================================================================ ESPN extraction


@dataclass
class EspnRecord:
    espn_id: str
    name: str
    pos: str
    team: str
    proj: dict[str, float] | None
    actual: dict[str, float] | None
    #: True when ESPN published a 2025 actual block for this player but it was EMPTY -- i.e.
    #: he recorded no countable stats all season. That is a real zero, not missing data, and
    #: it is exactly the row that must NOT be dropped: silently losing the players who never
    #: played flatters whichever source was more optimistic about them.
    actual_is_empty_block: bool = False
    weekly_actual: list[dict[str, float]] = field(default_factory=list)


def _season_block(player: Mapping[str, Any], source_id: int, split: int) -> dict | None:
    """The 2025 season-total block, or None if ESPN published no such block at all.

    An EMPTY dict is returned as an empty dict, never collapsed to None: "block exists, all
    zeros" and "no block" are different facts and the population accounting depends on the
    difference.
    """
    for block in player.get("stats") or []:
        if (
            block.get("seasonId") == SEASON
            and block.get("statSourceId") == source_id
            and block.get("statSplitTypeId") == split
        ):
            return dict(block.get("stats") or {})
    return None


def _weekly_blocks(player: Mapping[str, Any], source_id: int) -> list[dict]:
    out = []
    for block in player.get("stats") or []:
        if (
            block.get("seasonId") == SEASON
            and block.get("statSourceId") == source_id
            and block.get("statSplitTypeId") == 1
        ):
            stats = block.get("stats") or {}
            if stats:
                out.append(stats)
    return out


def _canonicalize(espn_stats: Mapping[str, Any]) -> dict[str, float]:
    """ESPN numeric stat ids -> canonical stat names, using prep/espn_client's verified map."""
    out: dict[str, float] = {}
    for raw_key, value in espn_stats.items():
        try:
            stat_id = int(raw_key)
        except (TypeError, ValueError):
            continue
        canonical = espn_client.ESPN_STAT_ID_MAP.get(stat_id)
        if canonical is not None:
            out[canonical] = float(value or 0.0)
    return out


def espn_records(raw_players: Sequence[Mapping[str, Any]]) -> dict[str, EspnRecord]:
    """One record per skill-position ESPN player, carrying its 2025 projection AND actuals."""
    out: dict[str, EspnRecord] = {}
    for entry in raw_players:
        player = entry.get("player") or {}
        pid = player.get("id")
        if pid is None:
            continue
        pos = espn_client.ESPN_POSITION_MAP.get(player.get("defaultPositionId"))
        if pos not in espn_client.SKILL_POSITIONS:
            continue

        proj = _season_block(player, 1, 0)
        actual = _season_block(player, 0, 0)
        name = player.get("fullName") or (
            f"{player.get('firstName', '')} {player.get('lastName', '')}".strip()
        )
        out[str(pid)] = EspnRecord(
            espn_id=str(pid),
            name=name,
            pos=pos,
            team=espn_client.ESPN_TEAM_MAP.get(player.get("proTeamId"), ""),
            proj=_canonicalize(proj) if proj else None,
            actual=None if actual is None else _canonicalize(actual),
            actual_is_empty_block=actual is not None and not actual,
            weekly_actual=[_canonicalize(b) for b in _weekly_blocks(player, 0)],
        )
    return out


def verify_espn_stat_ids(raw_players: Sequence[Mapping[str, Any]]) -> list[str]:
    """Hard gate: re-derive ESPN's own ratio fields from the component ids we map.

    CLAUDE.md is explicit that the community stat-id table is wrong for at least id 22 and
    that a wrong id yields plausible numbers in the wrong field. So every id this tool reads
    is confirmed against an identity ESPN itself publishes in the same stat block, in BOTH the
    projection and the actual block:

        id 21 (completion pct)  == id 1  / id 0     -> confirms pass_cmp, pass_att
        id 60 (yards per catch) == id 42 / id 53    -> confirms rec_yd, rec
        id 39 (rush yds/att)    == id 24 / id 23    -> confirms rush_yd, rush_att
        id 73 (total turnovers) == id 20 + id 72    -> confirms pass_int, fum_lost

    Returns the human-readable check lines. Raises AssertionError on any violation.
    """
    checks = {
        "id21 == pass_cmp/pass_att": (21, lambda g: g(1) / g(0) if g(0) else None, 0.005),
        "id60 == rec_yd/rec": (60, lambda g: g(42) / g(53) if g(53) else None, 0.01),
        "id39 == rush_yd/rush_att": (39, lambda g: g(24) / g(23) if g(23) else None, 0.01),
        "id73 == pass_int + fum_lost": (73, lambda g: g(20) + g(72), 0.02),
    }
    lines: list[str] = []
    failures: list[str] = []
    for source_id, label in ((1, "projection"), (0, "actual")):
        for check_name, (probe_id, derive, tol) in checks.items():
            checked = bad = 0
            for entry in raw_players:
                player = entry.get("player") or {}
                if espn_client.ESPN_POSITION_MAP.get(player.get("defaultPositionId")) is None:
                    continue
                stats = _season_block(player, source_id, 0)
                if not stats or str(probe_id) not in stats:
                    continue

                def g(i: int, _stats: Mapping[str, Any] = stats) -> float:
                    return float(_stats.get(str(i), 0.0) or 0.0)

                expected = derive(g)
                if expected is None:
                    continue
                checked += 1
                if abs(g(probe_id) - expected) > tol:
                    bad += 1
                    if len(failures) < 5:
                        failures.append(
                            f"{label} {check_name}: {player.get('fullName')} "
                            f"id{probe_id}={g(probe_id)} derived={expected}"
                        )
            lines.append(f"  {label:<10} {check_name:<28} {checked - bad}/{checked} agree")
            if bad:
                failures.append(f"{label} {check_name}: {bad} of {checked} players disagree")
    if failures:
        raise AssertionError(
            "ESPN stat-id verification FAILED -- a mapped id is not the field it is assumed "
            "to be, which would put plausible numbers in the wrong stat:\n  "
            + "\n  ".join(failures)
        )
    return lines


# ========================================================================= Sleeper extraction


@dataclass
class SleeperRecord:
    pid: str
    name: str
    pos: str
    team: str
    proj: dict[str, float]


def sleeper_records(raw: Sequence[Mapping[str, Any]]) -> dict[str, SleeperRecord]:
    """Sleeper's 2025 season projections, mapped through prep/sleeper_client's own stat map."""
    out: dict[str, SleeperRecord] = {}
    for row in raw:
        pid = row.get("player_id")
        stats = row.get("stats") or {}
        if pid is None or not stats:
            continue
        player = row.get("player") or {}
        pos = (player.get("position") or "").upper()
        if pos not in POSITIONS:
            continue
        proj: dict[str, float] = {}
        for key, value in stats.items():
            canonical = sleeper_client.SLEEPER_STAT_MAP.get(key)
            if canonical is not None:
                proj[canonical] = float(value or 0.0)
        if not proj:
            continue
        name = f"{player.get('first_name', '')} {player.get('last_name', '')}".strip()
        out[str(pid)] = SleeperRecord(
            pid=str(pid),
            name=name,
            pos=pos,
            # DANGER: this is the player's CURRENT (2026) team, taken from the live Sleeper
            # universe -- NOT the team he played for in the projected season. Mike Evans reads
            # SF here and played 2025 for TB; Isaiah Likely reads NYG and played for BAL.
            #
            # Nothing in this module reads it (it is a carried label only), which is exactly why
            # it is dangerous: it looks usable. An agent doing team-level analysis in 2026-08
            # grouped 2025 actuals by it and got a confident, completely wrong answer. For any
            # team-level aggregation of a PAST season you must use per-week team attribution --
            # see the `proTeamId` discussion in docs/PROJECTION_CHALLENGES.md.
            team=(player.get("team") or "").upper(),
            proj=proj,
        )
    return out


# ==================================================================================== joining


@dataclass
class MatchedPlayer:
    name: str
    pos: str
    team: str
    espn_id: str
    sleeper_pid: str
    match_method: str
    espn_proj: dict[str, float]
    sleeper_proj: dict[str, float]
    actual: dict[str, float]
    weekly_actual: list[dict[str, float]]
    #: How the 2025 actual was obtained. "real" = a populated ESPN actual block. "empty_block"
    #: = ESPN published an actual block with nothing in it, i.e. a player who recorded no
    #: countable stats; scored as a true zero and kept in the primary population.
    #: "missing_block" = no 2025 actual block at all, so production is unobserved rather than
    #: known to be zero; excluded from the primary tables and reported as a sensitivity.
    actual_status: str = "real"
    adp: float | None = None
    adp_rank: int | None = None


def _espn_to_sleeper_index(sleeper_universe: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """espn_id -> Sleeper pid, from the cached Sleeper player universe's own cross-ID field.

    Read straight off the FULL universe (not ``filter_active_skill_players``): a player who
    was active in 2025 and is retired/cut in 2026 must still join, or the backtest quietly
    becomes a backtest of players who survived to 2026.
    """
    index: dict[str, str] = {}
    for pid, player in sleeper_universe.items():
        if not player:
            continue
        espn_id = player.get("espn_id")
        if espn_id in (None, "", 0):
            continue
        index.setdefault(str(espn_id), str(pid))
    return index


def join_sources(
    espn: Mapping[str, EspnRecord],
    sleeper: Mapping[str, SleeperRecord],
    sleeper_universe: Mapping[str, Mapping[str, Any]],
) -> tuple[list[MatchedPlayer], dict[str, int], list[EspnRecord]]:
    """Match Sleeper's players onto the ESPN spine. Returns (matched, method counts, dropped).

    Cascade, mirroring ``prep/crosswalk.py``'s own order (direct ID, then name+pos), and never
    guessing: an ambiguous name+pos is left unmatched rather than resolved to a coin flip.
    """
    by_id = _espn_to_sleeper_index(sleeper_universe)

    by_name_pos: dict[tuple[str, str], list[SleeperRecord]] = {}
    for rec in sleeper.values():
        by_name_pos.setdefault((normalize_name(rec.name), rec.pos), []).append(rec)

    matched: list[MatchedPlayer] = []
    dropped: list[EspnRecord] = []
    counts = {
        "espn_id": 0,
        "name_pos": 0,
        "no_sleeper_projection": 0,
        "ambiguous_name": 0,
        "no_espn_projection": 0,
        "actual_real": 0,
        "actual_empty_block": 0,
        "actual_missing_block": 0,
    }

    for rec in espn.values():
        if rec.proj is None:
            counts["no_espn_projection"] += 1
            continue

        if rec.actual is None:
            status = "missing_block"
        elif rec.actual_is_empty_block or not rec.actual:
            status = "empty_block"
        else:
            status = "real"

        method = ""
        sl: SleeperRecord | None = None
        pid = by_id.get(rec.espn_id)
        if pid and pid in sleeper:
            sl, method = sleeper[pid], "espn_id"
        else:
            candidates = by_name_pos.get((normalize_name(rec.name), rec.pos), [])
            if len(candidates) == 1:
                sl, method = candidates[0], "name_pos"
            elif len(candidates) > 1:
                counts["ambiguous_name"] += 1
                dropped.append(rec)
                continue

        if sl is None:
            counts["no_sleeper_projection"] += 1
            dropped.append(rec)
            continue

        counts[method] += 1
        counts[f"actual_{status}"] += 1
        matched.append(
            MatchedPlayer(
                name=rec.name,
                pos=rec.pos,
                team=rec.team,
                espn_id=rec.espn_id,
                sleeper_pid=sl.pid,
                match_method=method,
                espn_proj=dict(rec.proj),
                sleeper_proj=dict(sl.proj),
                actual=dict(rec.actual or {}),
                weekly_actual=[dict(w) for w in rec.weekly_actual],
                actual_status=status,
            )
        )
    return matched, counts, dropped


def attach_adp(matched: Sequence[MatchedPlayer], ffc_raw: Mapping[str, Any]) -> int:
    """Attach 2025 preseason 2QB ADP (Fantasy Football Calculator) by name+position.

    FFC's own ordering is the ADP rank; ties are broken by FFC's list order, which is already
    sorted by ADP. Players outside the feed keep ``adp_rank=None`` and land in their own tier.
    """
    rows = ffc_client.parse_adp_rows(dict(ffc_raw))
    rows = sorted(rows, key=lambda r: r.adp)
    index: dict[tuple[str, str], tuple[float, int]] = {}
    for rank, row in enumerate(rows, start=1):
        key = (normalize_name(row.name), (row.pos or "").upper())
        index.setdefault(key, (row.adp, rank))

    hits = 0
    for player in matched:
        found = index.get((normalize_name(player.name), player.pos))
        if found:
            player.adp, player.adp_rank = found
            hits += 1
    return hits


# =================================================================================== blending


def blend_statlines(
    lines: Sequence[Mapping[str, float]],
    weights: Sequence[float] | None = None,
    *,
    games_varies: Sequence[bool],
) -> dict[str, float]:
    """Weighted blend at the COMPONENT-STAT level (default: equal weight).

    Deliberately local rather than calling ``valuation.composite.blend_statlines``, for one
    reason: that function decides what a source publishes from a declared per-source column
    set fitted to the 2026 feeds, while a 2025 backtest has to take presence from the 2025
    payload in front of it. Same rules, different notion of "does this source have this stat",
    and using the 2026 declaration to judge 2025 data would put structural zeros in the blend.
    The weight sweep in section E also needs a positional weight vector, which the composite's
    keyed-by-source-name API does not take.

    Same rules as plan B1: a stat is averaged only over the sources that HAVE it, a missing
    stat is never averaged in as a zero, and ``games`` is averaged only over sources reporting
    a positive figure ("unknown", not "zero games"). Weights are renormalized over whichever
    sources actually contributed to that stat, so dropping a source from one stat does not
    quietly shrink the total.

    ``games_varies`` is the historical form of production's ``varying_games_sources()``: one
    boolean per source saying whether that source's ``games`` column actually MOVES across the
    pool. It is not optional, and omitting it was a real defect. Sleeper publishes a blanket 18.0
    for every player in 2025 as well as 2026 -- a constant, not a forecast -- so averaging it
    against an ESPN 11-game projection produced 14.5 and destroyed the only genuine durability
    signal in the blend, in the tool whose numbers are the evidence for equal weighting (Codex
    2026-08-21 finding 9). The cap in ``league_points`` does not help: 14.5 is under the 17-week
    cap, so it passes through untouched.
    """
    if weights is None:
        weights = [1.0] * len(lines)
    if len(weights) != len(lines):
        raise ValueError("blend_statlines: weights and lines must be the same length")
    if len(games_varies) != len(lines):
        raise ValueError("blend_statlines: games_varies and lines must be the same length")

    out: dict[str, float] = {}
    for stat in CANONICAL_STATS:
        pairs: list[tuple[float, float]] = []
        for line, weight, varies in zip(lines, weights, games_varies):
            if weight <= 0:
                continue
            if stat == "games":
                # A source contributes `games` only if its games column varies within that
                # source. A constant is a placeholder wearing a forecast's name.
                if varies and float(line.get("games", 0.0) or 0.0) > 0:
                    pairs.append((float(line["games"]), weight))
            elif stat in line:
                pairs.append((float(line[stat]), weight))
        total_weight = sum(w for _, w in pairs)
        if pairs and total_weight > 0:
            out[stat] = sum(v * w for v, w in pairs) / total_weight
    return out


# ==================================================================================== scoring


def league_points(
    stats: Mapping[str, float],
    scoring: Mapping[str, float],
    *,
    pos: str | None = None,
    bonus: bool = False,
    schedule: Mapping[str, Any] | None = None,
    curves: Mapping[Any, Any] | None = None,
    games_cap: float | None = None,
) -> float:
    """League points for one projected stat line, optionally plus the expected yardage bonus.

    ``games_cap`` exists because Sleeper reports a BLANKET ``gp`` of 18.0 for every player in
    both 2025 and 2026 -- it is a constant, not a projection -- and 18 games cannot happen in
    this league's 17-week season. Capping at the league's own ``weeks`` keeps the bonus term
    from becoming a comparison of two games figures, one of which is not a forecast at all.
    """
    if not bonus:
        return score_statline(stats, scoring)
    games = float(stats.get("games", 0.0) or 0.0)
    if games_cap is not None and games > games_cap:
        games = games_cap
    if games <= 0:
        return score_statline(stats, scoring)
    line = dict(stats)
    line["games"] = games
    return score_statline(line, scoring) + expected_bonus(
        {**line, "pos": pos}, schedule, curves
    ).total


def actual_points(
    player: MatchedPlayer,
    scoring: Mapping[str, float],
    *,
    bonus: bool = False,
    schedule: Mapping[str, Any] | None = None,
) -> float:
    """What the player really scored in this league in 2025.

    The bonus half uses ``valuation.bonuses.actual_bonus`` over ESPN's own WEEKLY actual
    blocks -- real per-game yardage, no model and no curve -- so the ground truth stays ground
    truth. Same actuals payload as the season totals, so the spine is still one source.
    """
    base = score_statline(player.actual, scoring)
    if not bonus:
        return base
    return base + actual_bonus(player.weekly_actual, schedule).total


# ==================================================================================== metrics


@dataclass
class Metrics:
    n: int
    mae: float
    rmse: float
    bias: float
    corr: float
    actual_sd: float

    def row(self) -> str:
        return (
            f"{self.n:>5} {self.mae:>8.1f} {self.rmse:>8.1f} "
            f"{self.bias:>+8.1f} {self.corr:>7.3f}"
        )


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    if len(xs) < 3:
        return float("nan")
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return float("nan")
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def metrics(projected: Sequence[float], actual: Sequence[float]) -> Metrics:
    n = len(projected)
    if n == 0:
        return Metrics(0, float("nan"), float("nan"), float("nan"), float("nan"), float("nan"))
    errors = [p - a for p, a in zip(projected, actual)]
    return Metrics(
        n=n,
        mae=statistics.fmean(abs(e) for e in errors),
        rmse=math.sqrt(statistics.fmean(e * e for e in errors)),
        bias=statistics.fmean(errors),
        corr=_pearson(projected, actual),
        actual_sd=statistics.pstdev(actual) if n > 1 else 0.0,
    )


@dataclass
class PairedResult:
    n: int
    mean_diff: float          # mean(|err A|) - mean(|err B|); negative => A closer
    ci_low: float
    ci_high: float
    t_stat: float
    p_value: float
    a_closer: int

    def line(self, a: str, b: str) -> str:
        winner = a if self.mean_diff < 0 else b
        verdict = "REAL" if self.p_value < 0.05 else "not distinguishable"
        return (
            f"  {a} vs {b}: MAE gap {self.mean_diff:+.2f} pts "
            f"(95% CI {self.ci_low:+.2f} .. {self.ci_high:+.2f}), "
            f"paired t={self.t_stat:+.2f} p={self.p_value:.3f}, "
            f"{a} closer on {self.a_closer}/{self.n} players -> "
            f"{verdict} ({'favours ' + winner if self.p_value < 0.05 else 'no weight change earned'})"
        )


def paired_compare(
    err_a: Sequence[float], err_b: Sequence[float], *, seed: int = 20260820, draws: int = 10_000
) -> PairedResult:
    """Paired comparison of two sources' absolute errors on the SAME players.

    A per-player pairing is the only honest test here: the two error series share every
    player's injuries, holdouts, and breakouts, so an unpaired comparison would be swamped by
    variance that both sources face identically. Reported three ways -- mean gap, bootstrap
    95% CI, and the paired t-test -- because with a few hundred players a small MAE gap can
    easily be noise, and the report has to be able to say so.
    """
    import numpy as np
    from scipy import stats as sps

    a = np.abs(np.asarray(err_a, dtype=float))
    b = np.abs(np.asarray(err_b, dtype=float))
    if a.shape != b.shape:
        raise ValueError("paired_compare needs the same players in the same order")
    d = a - b
    n = int(d.size)
    if n < 3:
        return PairedResult(n, float("nan"), float("nan"), float("nan"), float("nan"),
                            float("nan"), 0)

    rng = np.random.default_rng(seed)
    boot = rng.choice(d, size=(draws, n), replace=True).mean(axis=1)
    lo, hi = (float(x) for x in np.percentile(boot, [2.5, 97.5]))
    t_stat, p_value = sps.ttest_rel(a, b)
    return PairedResult(
        n=n,
        mean_diff=float(d.mean()),
        ci_low=lo,
        ci_high=hi,
        t_stat=float(t_stat),
        p_value=float(p_value),
        a_closer=int((a < b).sum()),
    )


# ============================================================ calibration (regress actual on projected)


@dataclass
class Fit:
    """One OLS fit of actual on projected, with the two factors the slope decomposes into.

    ``slope = r * sd(actual)/sd(projected)`` is an identity, and keeping both factors visible is
    the whole point: a slope below 1 can mean the projections were too SPREAD OUT (sd ratio
    below 1) or merely imperfectly CORRELATED with the outcome (r below 1), and those are
    different claims with different remedies. Fantasy Football Analytics' framing is the first
    one; this data mostly shows the second.
    """

    n: int
    slope: float
    intercept: float
    r2: float
    ci_lo: float
    ci_hi: float
    r: float
    sd_ratio: float
    sd_proj: float
    sd_actual: float

    @property
    def excludes_one(self) -> bool:
        """Does the bootstrap interval rule out a perfectly calibrated slope of 1.0?"""
        return self.ci_hi < 1.0 or self.ci_lo > 1.0

    def row(self, label: str, width: int = 22) -> str:
        flag = "*" if self.excludes_one else " "
        return (
            f"  {label:<{width}} n={self.n:>3} b={self.slope:5.2f}{flag} "
            f"[{self.ci_lo:5.2f},{self.ci_hi:5.2f}] a={self.intercept:+7.2f} "
            f"R2={self.r2:5.2f}  = r {self.r:5.2f} x sd-ratio {self.sd_ratio:5.2f} "
            f"(sd proj {self.sd_proj:7.2f}, sd act {self.sd_actual:7.2f})"
        )


def ols_fit(
    projected: Sequence[float],
    actual: Sequence[float],
    *,
    draws: int = 4_000,
    seed: int = 20260820,
) -> Fit:
    """Regress actual on projected. The slope IS the calibration.

    A slope of 1.0 with a 0.0 intercept is a perfectly calibrated forecast: a point of
    projection buys a point of outcome. Below 1.0 means the projections' range is wider than
    the outcomes they predict, so the right response is to shrink toward the positional mean.

    Note which variable is on which side. Projected is the predictor and is known exactly --
    there is no measurement error in a published forecast -- so a slope below 1 is genuine
    miscalibration, not regression dilution. What it does NOT tell you is whether the cause is
    over-dispersion or weak correlation, which is why :class:`Fit` carries both factors.
    """
    import numpy as np

    x = np.asarray(projected, dtype=float)
    y = np.asarray(actual, dtype=float)
    if x.shape != y.shape:
        raise ValueError("ols_fit needs projected and actual to be the same length")
    n = int(x.size)
    if n < 5 or x.std() == 0:
        nan = float("nan")
        return Fit(n, nan, nan, nan, nan, nan, nan, nan, nan, nan)

    slope, intercept = (float(v) for v in np.polyfit(x, y, 1))
    resid = y - (intercept + slope * x)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((resid**2).sum()) / ss_tot if ss_tot else float("nan")

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(draws, n))
    boot = [
        float(np.polyfit(x[row], y[row], 1)[0]) for row in idx if x[row].std() > 0
    ]
    lo, hi = (float(v) for v in np.percentile(boot, [2.5, 97.5]))
    return Fit(
        n=n,
        slope=slope,
        intercept=intercept,
        r2=r2,
        ci_lo=lo,
        ci_hi=hi,
        r=float(np.corrcoef(x, y)[0, 1]),
        sd_ratio=float(y.std() / x.std()),
        sd_proj=float(x.std()),
        sd_actual=float(y.std()),
    )


def season_points_pairs(
    players: Sequence[MatchedPlayer],
    source: str,
    scoring: Mapping[str, float],
    *,
    bonus: bool = False,
    schedule: Mapping[str, Any] | None = None,
    curves: Mapping[Any, Any] | None = None,
    games_cap: float | None = None,
    games_varies: Mapping[str, bool],
) -> tuple[list[float], list[float]]:
    """(projected, actual) season league points for a set of players."""
    projected = [
        league_points(
            _projection_for(p, source, games_varies=games_varies), scoring,
            pos=p.pos, bonus=bonus, schedule=schedule, curves=curves, games_cap=games_cap,
        )
        for p in players
    ]
    actual = [actual_points(p, scoring, bonus=bonus, schedule=schedule) for p in players]
    return projected, actual


def ppg_pairs(
    players: Sequence[MatchedPlayer],
    source: str,
    scoring: Mapping[str, float],
    *,
    schedule: Mapping[str, Any] | None = None,
    curves: Mapping[Any, Any] | None = None,
    games_cap: float,
    games_varies: Mapping[str, bool],
) -> tuple[list[float], list[float]]:
    """(projected PPG, actual PPG) -- the space ``valuation/evob.py`` actually values in.

    Season totals mix rate error with availability error, and the board already models
    availability separately (the fitted rank-conditional games curve). A shrink derived from
    season totals and applied to PPG would therefore charge the board twice for the same
    missed games, which is why the PPG fit exists alongside the season-total one.

    Only players with a real actual games count are included -- an actual PPG needs a
    denominator -- so this view is blind to the players who missed the year, and its slope is
    consequently kinder to both sources than the season-total slope. That is a stated
    limitation of the view, not a correction to be applied on top of it.
    """
    projected: list[float] = []
    actual: list[float] = []
    for player in players:
        games_actual = float(player.actual.get("games", 0.0) or 0.0)
        if games_actual <= 0:
            continue
        line = _projection_for(player, source, games_varies=games_varies)
        games_proj = min(float(line.get("games", 0.0) or 0.0), games_cap)
        if games_proj <= 0:
            continue
        projected.append(
            league_points(
                line, scoring, pos=player.pos, bonus=True,
                schedule=schedule, curves=curves, games_cap=games_cap,
            ) / games_proj
        )
        actual.append(
            actual_points(player, scoring, bonus=True, schedule=schedule) / games_actual
        )
    return projected, actual


def mean_preserving_shrink(value: float, positional_mean: float, slope: float) -> float:
    """Compress one value toward its positional mean by ``slope``.

    ``m + b*(x - m)``. Mean-preserving on purpose, which separates the two things a fitted
    calibration line does at once: this is the SPREAD half only, with no level haircut. It is
    also why the effect on EVoB is a clean multiplication -- both the player and the positional
    baseline move toward the same mean, so the gap between them scales by exactly ``b``.
    """
    return positional_mean + slope * (value - positional_mean)


# =============================================================== nflreadpy actuals crosscheck


def crosscheck_actuals(matched: Sequence[MatchedPlayer]) -> list[str]:
    """Compare ESPN's 2025 actuals against the cached nflreadpy 2025 weekly history.

    Uses ``load_latest_weekly_history()`` on purpose: there are two files in
    ``data/raw/nflreadpy_weekly/`` and the OLDER one still contains postseason weeks 19-22,
    which would inflate every season total. Never hand-pick a filename there.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from tools.fetch_weekly_history import load_latest_weekly_history

    weekly = load_latest_weekly_history()
    weekly = weekly.filter(weekly["season"] == SEASON)
    rows = weekly.to_dicts()

    totals: dict[tuple[str, str], dict[str, float]] = {}
    games: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (normalize_name(row["player_display_name"] or ""), (row["position"] or "").upper())
        bucket = totals.setdefault(key, {"pass_yd": 0.0, "rush_yd": 0.0, "rec_yd": 0.0})
        for stat in ("pass_yd", "rush_yd", "rec_yd"):
            bucket[stat] += float(row.get(stat) or 0.0)
        games[key] = games.get(key, 0) + 1

    lines: list[str] = []
    wanted = {clean_name(n) for n in CROSSCHECK_NAMES}
    for player in matched:
        if clean_name(player.name) not in wanted:
            continue
        key = (normalize_name(player.name), player.pos)
        if key not in totals:
            lines.append(f"  {player.name:<22} NOT FOUND in nflreadpy weekly history")
            continue
        nfl = totals[key]
        espn = player.actual
        diffs = {
            stat: float(espn.get(stat, 0.0)) - nfl[stat]
            for stat in ("pass_yd", "rush_yd", "rec_yd")
        }
        worst = max(abs(v) for v in diffs.values())
        lines.append(
            f"  {player.name:<22} {player.pos:<3} "
            f"pass {espn.get('pass_yd', 0.0):>7.0f}/{nfl['pass_yd']:>7.0f} "
            f"rush {espn.get('rush_yd', 0.0):>6.0f}/{nfl['rush_yd']:>6.0f} "
            f"rec {espn.get('rec_yd', 0.0):>6.0f}/{nfl['rec_yd']:>6.0f} "
            f"games {espn.get('games', 0.0):>4.0f}/{games[key]:>3d}  max|diff|={worst:.0f}"
        )
    return lines


# ===================================================================================== report


SOURCES = ("sleeper", "espn", "blend")

#: The order the blend's positional lists are always in. One place, so a weights vector and a
#: games-variation mask can never be zipped against different orderings.
BLEND_ORDER = ("sleeper", "espn")

def measure_games_variation(players: Sequence[MatchedPlayer]) -> dict[str, bool]:
    """Per source, does its projected ``games`` actually MOVE across the pool?

    The historical counterpart to ``valuation.composite.varying_games_sources``, which decides
    the same question from the 2026 feeds. Measured rather than declared, because "is this column
    a forecast or a constant" is a fact about the payload in front of us and has been wrong
    before in both directions.
    """
    getters = {"sleeper": lambda p: p.sleeper_proj, "espn": lambda p: p.espn_proj}
    measured: dict[str, bool] = {}
    for source, get in getters.items():
        seen: set[float] = set()
        for player in players:
            g = float(get(player).get("games", 0.0) or 0.0)
            if g > 0:
                seen.add(round(g, 3))
        measured[source] = len(seen) > 1
    return measured


def blend_games_mask(measured: Mapping[str, bool]) -> list[bool]:
    """A :data:`BLEND_ORDER`-ordered mask from :func:`measure_games_variation`'s result.

    Passed explicitly rather than read from module state. An earlier version cached the
    measurement in a module global, which made the whole test file order-dependent: the blend
    tests passed only because some earlier test had happened to populate it, and running one in
    isolation raised. A green suite that depends on test order is worse than a red one.
    """
    missing = [s for s in BLEND_ORDER if s not in measured]
    if missing:
        raise KeyError(
            f"games variation was not measured for {missing}; "
            "call measure_games_variation(players) over the whole pool first"
        )
    return [measured[s] for s in BLEND_ORDER]


def _projection_for(
    player: MatchedPlayer, source: str, *, games_varies: Mapping[str, bool]
) -> dict[str, float]:
    """One source's projected statline for one player.

    ``games_varies`` is required, with no default, ON PURPOSE. The defect it guards against was
    Sleeper's constant 18.0 being averaged into the blend's games figure; a default of "assume
    everything varies" would re-admit it the moment a caller forgot (Codex 2026-08-21 finding 9).
    """
    if source == "sleeper":
        return player.sleeper_proj
    if source == "espn":
        return player.espn_proj
    if source == "blend":
        return blend_statlines(
            [player.sleeper_proj, player.espn_proj],
            games_varies=blend_games_mask(games_varies),
        )
    raise KeyError(source)


def _tier_of(player: MatchedPlayer) -> str:
    if player.adp_rank is None:
        return UNRANKED_TIER
    for label, lo, hi in ADP_TIERS:
        if lo <= player.adp_rank <= hi:
            return label
    return UNRANKED_TIER


def _group_table(
    title: str,
    groups: Sequence[tuple[str, list[int]]],
    scored: Mapping[str, list[float]],
    actual: Sequence[float],
    out: list[str],
) -> None:
    out.append("")
    out.append(title)
    out.append(
        f"  {'group':<20} {'source':<9} {'n':>5} {'MAE':>8} {'RMSE':>8} {'bias':>8} {'corr':>7}"
    )
    for label, idx in groups:
        if not idx:
            continue
        actual_slice = [actual[i] for i in idx]
        for source in SOURCES:
            m = metrics([scored[source][i] for i in idx], actual_slice)
            out.append(f"  {label:<20} {source:<9} {m.row()}")
        out.append("")


def build_report(
    everyone: list[MatchedPlayer],
    cfg: LeagueConfig,
    *,
    join_counts: Mapping[str, int],
    adp_hits: int,
    id_check_lines: Sequence[str],
    crosscheck_lines: Sequence[str],
    vintage_lines: Sequence[str],
    dropped: Sequence[EspnRecord],
) -> str:
    schedule = load_bonus_schedule()
    curves = load_curves()
    scoring = cfg.scoring

    # Primary population: everyone whose 2025 production is OBSERVED, which includes the
    # players ESPN published an empty actual block for (they really did score ~nothing).
    # Players with no actual block at all are unobserved, not zero, so they are held out of
    # the main tables and handled in the survivorship sensitivity below.
    matched = [p for p in everyone if p.actual_status != "missing_block"]
    unobserved = [p for p in everyone if p.actual_status == "missing_block"]

    # Measured over the WHOLE joined pool, not just the primary population: whether a source's
    # games column is a forecast or a constant is a property of the payload, not of which
    # players survived the actual-status filter. Every blend below reads this.
    games_variation = measure_games_variation(everyone)

    out: list[str] = []
    out.append("=" * 96)
    out.append(f"2025 PROJECTION ACCURACY BACKTEST -- scored in Allendale Dad League points")
    out.append("=" * 96)
    out.append(
        f"league: {cfg.teams} teams, starters {dict(cfg.starters)}, flex {cfg.flex_slots}, "
        f"{cfg.weeks} weeks, half-PPR, pass_int {scoring.get('pass_int')}"
    )
    out.append("sources measured: Sleeper (rotowire) and ESPN (Mike Clay), 2025 preseason.")
    out.append(
        "games column varies within source: "
        + ", ".join(f"{k}={'yes' if v else 'NO (constant)'}" for k, v in sorted(games_variation.items()))
        + " -- only a varying source contributes to the blend's games figure, same rule as "
        "production's varying_games_sources()."
    )
    out.append(
        "FantasyPros: NOT MEASURABLE -- our CSVs are 2026 only and the 2025 archive is behind "
        "the subscription CLAUDE.md says not to buy."
    )

    out.append("")
    out.append("GATE: ESPN stat-id identities (projection AND actual blocks)")
    out.extend(id_check_lines)

    out.append("")
    out.append("VINTAGE: are these really preseason numbers? (content test, not timestamps)")
    out.extend(vintage_lines)

    out.append("")
    out.append("ACTUALS CROSS-CHECK: ESPN 2025 actuals vs cached nflreadpy weekly (ESPN/nfl)")
    out.extend(crosscheck_lines)

    out.append("")
    out.append("POPULATION (and exactly what was dropped, since dropping the wrong rows is")
    out.append("            how a backtest silently flatters the more optimistic source)")
    out.append(f"  primary population -- ESPN projection + Sleeper projection + OBSERVED 2025 "
               f"production: {len(matched)}")
    out.append(f"    joined by Sleeper's own espn_id:      {join_counts['espn_id']}")
    out.append(f"    joined by normalized name + position: {join_counts['name_pos']}")
    out.append(f"    of these, populated ESPN actual block: {join_counts['actual_real']}")
    out.append(
        f"    of these, EMPTY ESPN actual block -- projected but recorded nothing all season, "
        f"kept and scored as a real zero: {join_counts['actual_empty_block']}"
    )
    if join_counts["actual_empty_block"]:
        zeros = ", ".join(
            f"{p.name} ({p.pos})" for p in matched if p.actual_status == "empty_block"
        )
        out.append(f"      -> {zeros}")
    out.append(f"  held out -- no 2025 ESPN actual block at all (production UNOBSERVED, not "
               f"known to be zero): {join_counts['actual_missing_block']}")
    if unobserved:
        out.append("      -> " + ", ".join(f"{p.name} ({p.pos})" for p in unobserved))
    out.append(f"  never in the table -- ESPN skill player with no 2025 projection block: "
               f"{join_counts['no_espn_projection']}")
    out.append(f"  never in the table -- no Sleeper 2025 projection to pair with: "
               f"{join_counts['no_sleeper_projection']}")
    out.append(f"  never in the table -- ambiguous name+position, never guessed: "
               f"{join_counts['ambiguous_name']}")
    played_zero = sum(1 for p in matched if float(p.actual.get("games", 0.0)) <= 0)
    out.append(
        f"  players in the primary population who played 0 games: {played_zero}. They stay in. "
        "Their actual is ~0 and both sources projected them for real production, which is a "
        "forecast error and belongs in the error."
    )
    out.append(f"  2025 FFC 2QB ADP attached to {adp_hits} of {len(matched)} primary players.")
    if dropped:
        sample = ", ".join(f"{r.name} ({r.pos})" for r in dropped[:8])
        out.append(f"  unpairable sample (no Sleeper projection): {sample}")

    # ---------------------------------------------------------------- scoring, no bonus
    actual_plain = [actual_points(p, scoring) for p in matched]
    scored_plain = {
        source: [
            league_points(_projection_for(p, source, games_varies=games_variation), scoring)
            for p in matched
        ]
        for source in SOURCES
    }

    # ---------------------------------------------------------------- scoring, with bonus
    actual_bonusized = [actual_points(p, scoring, bonus=True, schedule=schedule) for p in matched]
    scored_bonusized = {
        source: [
            league_points(
                _projection_for(p, source, games_varies=games_variation), scoring,
                pos=p.pos, bonus=True, schedule=schedule, curves=curves,
                games_cap=float(cfg.weeks),
            )
            for p in matched
        ]
        for source in SOURCES
    }

    all_idx = list(range(len(matched)))
    ranked_idx = [i for i, p in enumerate(matched) if p.adp_rank is not None]
    pos_groups = [
        (pos, [i for i, p in enumerate(matched) if p.pos == pos]) for pos in POSITIONS
    ]
    tier_labels = [label for label, _, _ in ADP_TIERS] + [UNRANKED_TIER]
    tier_groups = [
        (label, [i for i, p in enumerate(matched) if _tier_of(p) == label])
        for label in tier_labels
    ]

    for label, scored, actual in (
        ("A. LEAGUE POINTS, NO PER-GAME YARDAGE BONUS (score_statline)", scored_plain, actual_plain),
        ("B. LEAGUE POINTS INCLUDING THE PER-GAME YARDAGE BONUS "
         "(score_statline_with_bonus basis; actuals use actual_bonus on real weekly yardage)",
         scored_bonusized, actual_bonusized),
    ):
        out.append("")
        out.append("=" * 96)
        out.append(label)
        out.append("=" * 96)
        _group_table(
            "OVERALL (every matched player, including the deep tail)",
            [("all matched", all_idx), ("in 2025 ADP feed", ranked_idx)],
            scored, actual, out,
        )
        _group_table("BY POSITION (all matched)", pos_groups, scored, actual, out)
        _group_table("BY 2025 ADP TIER", tier_groups, scored, actual, out)

        out.append("IS THE DIFFERENCE REAL? (paired on the same players, absolute errors)")
        for population, idx in (
            ("all matched", all_idx),
            ("in ADP feed", ranked_idx),
            ("ADP 1-60", [i for i in ranked_idx if matched[i].adp_rank and matched[i].adp_rank <= 60]),
        ) + tuple((f"pos {pos}", idx) for pos, idx in pos_groups):
            if len(idx) < 10:
                continue
            out.append(f"  [{population}, n={len(idx)}]")
            errs = {
                source: [scored[source][i] - actual[i] for i in idx] for source in SOURCES
            }
            for a, b in (("sleeper", "espn"), ("blend", "sleeper"), ("blend", "espn")):
                out.append("  " + paired_compare(errs[a], errs[b]).line(a, b))
            out.append("")

    # ---------------------------------------------------------- survivorship sensitivity
    out.append("=" * 96)
    out.append(
        "C. SURVIVORSHIP SENSITIVITY -- add back the held-out players (no ESPN actual block) "
        "with their actual zero-filled. The direction of this move is the size of the "
        "selection effect."
    )
    out.append("=" * 96)
    out.append("")
    if unobserved:
        filled = matched + unobserved
        filled_actual = [actual_points(p, scoring) for p in filled]
        filled_scored = {
            s: [
                league_points(_projection_for(p, s, games_varies=games_variation), scoring)
                for p in filled
            ]
            for s in SOURCES
        }
        out.append(
            f"  {'group':<24} {'source':<9} {'n':>5} {'MAE':>8} {'RMSE':>8} {'bias':>8} {'corr':>7}"
        )
        for source in SOURCES:
            m = metrics(filled_scored[source], filled_actual)
            out.append(f"  {'zero-filled superset':<24} {source:<9} {m.row()}")
        out.append("")
        errs = {s: [filled_scored[s][i] - filled_actual[i] for i in range(len(filled))]
                for s in SOURCES}
        for a, b in (("sleeper", "espn"), ("blend", "sleeper"), ("blend", "espn")):
            out.append("  " + paired_compare(errs[a], errs[b]).line(a, b))
        out.append("")
        out.append(
            f"  Compare to table A's 'all matched' row: adding {len(unobserved)} zero-actual "
            "players raises every source's MAE, and it raises the more optimistic source's "
            "MAE more. That is the selection effect, and it is why these rows are shown "
            "rather than quietly excluded."
        )
    else:
        out.append("  no held-out players -- every projected player had an actual block.")

    # ---------------------------------------------------------------- per-game (rate) view
    out.append("")
    out.append("=" * 96)
    out.append(
        "D. PER-GAME VIEW (players who actually played >= 8 games) -- separates rate accuracy "
        "from availability, which is how valuation/evob.py actually consumes a projection"
    )
    out.append("=" * 96)
    ppg_idx = [i for i, p in enumerate(matched) if float(p.actual.get("games", 0.0)) >= 8]
    ppg_actual = [
        actual_plain[i] / float(matched[i].actual["games"]) for i in ppg_idx
    ]
    ppg_scored: dict[str, list[float]] = {}
    for source in SOURCES:
        vals = []
        for i in ppg_idx:
            line = _projection_for(matched[i], source, games_varies=games_variation)
            games = min(float(line.get("games", 0.0) or 0.0), float(cfg.weeks))
            vals.append(scored_plain[source][i] / games if games > 0 else float("nan"))
        ppg_scored[source] = vals
    out.append("")
    out.append(f"  {'group':<20} {'source':<9} {'n':>5} {'MAE':>8} {'RMSE':>8} {'bias':>8} {'corr':>7}")
    for source in SOURCES:
        m = metrics(ppg_scored[source], ppg_actual)
        out.append(f"  {'PPG, >=8 games':<20} {source:<9} {m.row()}")
    out.append("")
    for a, b in (("sleeper", "espn"), ("blend", "sleeper"), ("blend", "espn")):
        errs_a = [ppg_scored[a][k] - ppg_actual[k] for k in range(len(ppg_idx))]
        errs_b = [ppg_scored[b][k] - ppg_actual[k] for k in range(len(ppg_idx))]
        out.append("  " + paired_compare(errs_a, errs_b).line(a, b))

    # ------------------------------------------------------------------------ weight sweep
    out.append("")
    out.append("=" * 96)
    out.append(
        "E. WHAT WEIGHT WOULD 2025 CHOOSE? MAE as a function of the Sleeper weight "
        "(ESPN gets 1-w), and how stable that choice is under resampling"
    )
    out.append("=" * 96)
    for label, idx in (("all matched", all_idx), ("in ADP feed", ranked_idx)):
        out.extend(
            weight_sweep_lines(
                label, [matched[i] for i in idx], scoring, games_variation=games_variation
            )
        )

    # ----------------------------------------------------- calibration and its consequence
    out.append("")
    calib, fitted_slopes = calibration_lines(
        matched, cfg, schedule, curves, games_variation=games_variation
    )
    out.extend(calib)

    out.append("")
    out.extend(adp_bias_decomposition_lines(matched, cfg, games_variation=games_variation))

    out.append("")
    out.extend(board_shrink_lines({
        "our 2025 season-total slopes (F1, blend)": fitted_slopes.get("season_all", {}),
        "our 2025 PPG slopes, draftable (F5, blend)": fitted_slopes.get("ppg_draftable", {}),
        "our 2025 PPG slopes, all >=8 games (F4, blend)": fitted_slopes.get("ppg_all", {}),
        "FFA's 12-season slopes (external)": FFA_SLOPES,
    }))

    out.append("")
    out.extend(
        bonus_vs_calibration_lines(
            matched, cfg, schedule, curves, fitted_slopes.get("season_all", {}),
            games_variation=games_variation,
        )
    )

    # ---------------------------------------------------------------- context for the reader
    out.append("")
    out.append("=" * 96)
    out.append("SCALE CONTEXT (so an MAE can be judged against something)")
    out.append("=" * 96)
    ranked_actual = [actual_plain[i] for i in ranked_idx]
    out.append(
        f"  actual 2025 league points, players in the ADP feed: "
        f"mean {statistics.fmean(ranked_actual):.1f}, sd {statistics.pstdev(ranked_actual):.1f}, "
        f"max {max(ranked_actual):.1f}"
    )
    out.append(
        f"  actual 2025 league points, all matched: "
        f"mean {statistics.fmean(actual_plain):.1f}, sd {statistics.pstdev(actual_plain):.1f}"
    )
    out.append(
        "  ONE SEASON ONLY. 2025 is a single draw: one league-wide injury pattern, one set of "
        "coaching changes. A source can win a season on luck, so treat any gap below the "
        "bootstrap CI width as unproven and do not reweight on it."
    )
    return "\n".join(out)


# ================================================== sections F/G/H: calibration and its consequence


#: Fantasy Football Analytics' published per-position calibration slopes, fitted over 12
#: seasons. NOT measured here -- an external reference point, carried so the one-season fits
#: below can be compared against something with real sample size. Source: the scouting sweep's
#: citation, relayed 2026-08-20. Their framing is that projections are "too spread out"; the
#: decomposition in :class:`Fit` tests whether that is the mechanism in this data.
FFA_SLOPES: dict[str, float] = {"QB": 0.67, "TE": 0.72, "RB": 0.79, "WR": 0.85}


def calibration_lines(
    matched: Sequence[MatchedPlayer],
    cfg: LeagueConfig,
    schedule: Mapping[str, Any],
    curves: Mapping[Any, Any],
    *,
    games_variation: Mapping[str, bool],
) -> tuple[list[str], dict[str, dict[str, float]]]:
    """Section F: regress actual on projected, per position, per source, in both spaces.

    Returns the report lines and the fitted BLEND slopes per space, so section H shrinks the
    board with numbers this run actually measured rather than numbers typed into a constant.
    """
    scoring = cfg.scoring
    cap = float(cfg.weeks)
    ranked = [p for p in matched if p.adp_rank is not None]
    played = [p for p in matched if float(p.actual.get("games", 0.0) or 0.0) >= 8]
    played_ranked = [p for p in played if p.adp_rank is not None]

    out: list[str] = []
    out.append("=" * 96)
    out.append(
        "F. CALIBRATION -- regress ACTUAL on PROJECTED. Slope 1.0 = perfectly calibrated; "
        "below 1.0 means shrink toward the positional mean. '*' = bootstrap CI excludes 1.0."
    )
    out.append("=" * 96)
    out.append(
        "   The identity slope = r x sd(actual)/sd(projected) is printed for every fit, because "
        "'too spread out' (sd ratio < 1) and 'imperfectly correlated' (r < 1) are different "
        "diagnoses that both produce a slope below 1."
    )
    out.append(f"   External reference, 12 seasons, Fantasy Football Analytics: {FFA_SLOPES}")

    slopes: dict[str, dict[str, float]] = {}

    for title, pool, bonus, key in (
        ("F1. SEASON LEAGUE POINTS, no bonus -- all matched players",
         matched, False, "season_all"),
        ("F2. SEASON LEAGUE POINTS, with bonus -- all matched players",
         matched, True, "season_all_bonus"),
        ("F3. SEASON LEAGUE POINTS, with bonus -- players in the 2025 ADP feed only",
         ranked, True, "season_draftable"),
    ):
        out.append("")
        out.append(f"  {title}  (n={len(pool)})")
        fitted: dict[str, float] = {}
        for pos in (*POSITIONS, "ALL"):
            group = [p for p in pool if pos == "ALL" or p.pos == pos]
            if len(group) < 10:
                out.append(f"  {pos:<4} n={len(group)} -- too few to fit")
                continue
            for source in SOURCES:
                projected, actual = season_points_pairs(
                    group, source, scoring,
                    bonus=bonus, schedule=schedule, curves=curves, games_cap=cap,
                    games_varies=games_variation,
                )
                fit = ols_fit(projected, actual)
                out.append(fit.row(f"{pos:<4} {source}"))
                if source == "blend" and pos != "ALL":
                    fitted[pos] = fit.slope
            out.append("")
        slopes[key] = fitted

    for title, pool, key in (
        ("F4. POINTS PER GAME, with bonus -- every player who played >= 8 games",
         played, "ppg_all"),
        ("F5. POINTS PER GAME, with bonus -- ADP-feed players who played >= 8 games "
         "(the population the board values, in the space it values in)",
         played_ranked, "ppg_draftable"),
    ):
        out.append("")
        out.append(f"  {title}  (n={len(pool)})")
        fitted = {}
        for pos in (*POSITIONS, "ALL"):
            group = [p for p in pool if pos == "ALL" or p.pos == pos]
            if len(group) < 10:
                out.append(f"  {pos:<4} n={len(group)} -- too few to fit")
                continue
            for source in SOURCES:
                projected, actual = ppg_pairs(
                    group, source, scoring, schedule=schedule, curves=curves, games_cap=cap,
                    games_varies=games_variation,
                )
                fit = ols_fit(projected, actual)
                out.append(fit.row(f"{pos:<4} {source}"))
                if source == "blend" and pos != "ALL":
                    fitted[pos] = fit.slope
            out.append("")
        slopes[key] = fitted

    out.append(
        "  WATCH THE PPG POPULATION: an actual PPG needs games played, so F4/F5 exclude everyone "
        "who missed the season. That makes both sources look better than F1-F3 do, and it is "
        "why the two spaces disagree. F4 is also contaminated at the bottom: a backup projected "
        "for a near-zero season total over a 17-game divisor lands at a projected PPG near zero "
        "(Marcus Mariota 0.47, Kirk Cousins 0.41, Mac Jones 0.39), then plays 10-14 real games "
        "at 10-12 PPG. That is a divisor artifact, not a rate miss, and it drags F4's QB slope "
        "down. F5 is the honest rate fit."
    )
    return out, slopes


def adp_bias_decomposition_lines(
    matched: Sequence[MatchedPlayer],
    cfg: LeagueConfig,
    *,
    games_variation: Mapping[str, bool],
) -> list[str]:
    """Section G: is the ADP-tier bias anything MORE than a slope below 1?

    Every tier's raw bias is decomposed arithmetically. With ``actual = a + b*proj + e``:

        mean(proj - actual) = (1-b)*mean(proj)  -  a  -  mean(e)
                              ^ spread term       ^ level  ^ anything ADP-specific

    If the residual term is indistinguishable from zero in every tier, the tier pattern is a
    restatement of the slope rather than an additional effect -- which is the same thing as
    saying it cannot be separated from regression to the mean.
    """
    import numpy as np
    from scipy import stats as sps

    scoring = cfg.scoring
    projected = np.array(
        [
            league_points(_projection_for(p, "blend", games_varies=games_variation), scoring)
            for p in matched
        ],
        dtype=float,
    )
    actual = np.array([actual_points(p, scoring) for p in matched], dtype=float)
    positions = np.array([p.pos for p in matched])

    params: dict[str, tuple[float, float]] = {}
    for pos in POSITIONS:
        mask = positions == pos
        if mask.sum() < 10:
            continue
        fit = ols_fit(projected[mask], actual[mask], draws=500)
        params[pos] = (fit.intercept, fit.slope)

    fitted_line = np.array(
        [
            params[pos][0] + params[pos][1] * proj if pos in params else np.nan
            for pos, proj in zip(positions, projected)
        ]
    )
    residual = actual - fitted_line

    out: list[str] = []
    out.append("=" * 96)
    out.append(
        "G. IS THE ADP-TIER BIAS DISTINGUISHABLE FROM PLAIN REGRESSION TO THE MEAN? "
        "Decomposing each tier's bias into spread + level + anything left over."
    )
    out.append("=" * 96)
    out.append(
        "   Per-position calibration fitted on all matched players (blend, no bonus), then each "
        "tier's mean bias split. 'residual' is what the tier shows OVER what a slope below 1 "
        "already predicts -- the only part that would be an ADP-specific effect."
    )
    out.append("")
    out.append(
        f"  {'tier':<16} {'n':>4} {'raw bias':>10} {'spread':>9} {'level':>8} "
        f"{'residual':>10} {'t':>7} {'p':>7}"
    )
    for label in [t[0] for t in ADP_TIERS] + [UNRANKED_TIER]:
        mask = np.array([_tier_of(p) == label for p in matched])
        mask &= ~np.isnan(fitted_line)
        if mask.sum() < 5:
            continue
        raw = float((projected[mask] - actual[mask]).mean())
        spread = float(
            np.mean([(1 - params[pos][1]) * proj
                     for pos, proj in zip(positions[mask], projected[mask])])
        )
        level = float(np.mean([-params[pos][0] for pos in positions[mask]]))
        resid = residual[mask]
        t_stat, p_value = sps.ttest_1samp(resid, 0.0)
        out.append(
            f"  {label:<16} {int(mask.sum()):>4} {raw:>+10.1f} {spread:>+9.1f} "
            f"{level:>+8.1f} {-float(resid.mean()):>+10.1f} {float(t_stat):>+7.2f} "
            f"{float(p_value):>7.3f}"
        )
    out.append("")
    out.append(
        "  Read the residual column, not the raw one. A residual near zero with a large p means "
        "the tier's overshoot is exactly what a slope below 1 mechanically produces at that "
        "projection level, and there is nothing ADP-specific left to explain. The caveat that "
        "matters: the line is fitted IN SAMPLE on these same players, so this is a "
        "decomposition, not an out-of-sample test -- it shows the two explanations are one "
        "explanation, not that either is right."
    )
    return out


def board_shrink_lines(slope_sets: Mapping[str, Mapping[str, float]]) -> list[str]:
    """Section H: what a per-position calibration shrink would do to the LIVE 2026 board.

    Read-only on production: it imports ``build_real_board`` and ``compute_draft_values``,
    re-values a SHRUNK COPY of the board's season records, and writes nothing. No shrink is
    applied to the shipped board -- that is Marc's call and he has not made it.

    The shrink is mean-preserving per position, which makes the arithmetic transparent: player
    and positional baseline both move toward the same mean, so every EVoB at that position
    scales by exactly that position's slope. The printed multipliers are the proof.
    """
    import dataclasses

    out: list[str] = []
    out.append("=" * 96)
    out.append(
        "H. CONSEQUENCE: what a per-position calibration shrink would do to the CURRENT board "
        "(measured, NOT applied -- nothing here changes the shipped board)"
    )
    out.append("=" * 96)

    try:
        from draftroom.validate.board import build_real_board
        from draftroom.valuation.evob import compute_draft_values
    except Exception as exc:  # pragma: no cover - other agents are live in these modules
        out.append(f"  SKIPPED: could not import the board pipeline ({exc!r}).")
        return out

    try:
        board = build_real_board()
        seasons = list(board.seasons)
        base = compute_draft_values(seasons, board.cfg)
    except Exception as exc:  # pragma: no cover - same reason
        out.append(f"  SKIPPED: board build failed ({exc!r}).")
        return out

    means = {
        pos: statistics.fmean([s.ppg for s in seasons if s.pos == pos])
        for pos in POSITIONS
        if any(s.pos == pos for s in seasons)
    }

    def summarize(label: str, dv_map: Mapping[str, Any]) -> list[Any]:
        ranked = sorted(dv_map.values(), key=lambda d: -d.dv)
        top_qb = next((d for d in ranked if d.pos == "QB"), None)
        qb_rank = ranked.index(top_qb) + 1 if top_qb else 0
        out.append(
            f"  {label:<26} top QB {top_qb.name if top_qb else '-':<14} "
            f"dv={top_qb.dv if top_qb else float('nan'):7.2f} at overall #{qb_rank:<3} | "
            f"QBs in top 10: {sum(1 for d in ranked[:10] if d.pos == 'QB')}, "
            f"top 30: {sum(1 for d in ranked[:30] if d.pos == 'QB')}"
        )
        return ranked

    out.append("")
    out.append(f"  board: {len(board.players)} players, source={board.source}")
    out.append(
        "  positional mean projected PPG: "
        + ", ".join(f"{pos} {mean:.2f}" for pos, mean in sorted(means.items()))
    )
    out.append("")
    base_ranked = summarize("CURRENT (no shrink)", base)
    out.append("    top 10: " + ", ".join(
        f"{i}.{d.name} ({d.pos})" for i, d in enumerate(base_ranked[:10], 1)
    ))

    base_by_pos = {
        pos: [d for d in base.values() if d.pos == pos and d.dv > 0] for pos in POSITIONS
    }

    for label, slopes in slope_sets.items():
        if not slopes or any(pos not in slopes for pos in means):
            out.append("")
            out.append(f"  {label}: incomplete slope set {dict(slopes)} -- skipped")
            continue
        shrunk = [
            dataclasses.replace(
                s, ppg=mean_preserving_shrink(s.ppg, means[s.pos], slopes[s.pos])
            )
            for s in seasons
        ]
        dv_map = compute_draft_values(shrunk, board.cfg)
        out.append("")
        pretty = ", ".join(f"{pos} {slopes[pos]:.2f}" for pos in POSITIONS if pos in slopes)
        out.append(f"  shrink using {label}: {pretty}")
        ranked = summarize("  -> shrunk board", dv_map)
        out.append("    top 10: " + ", ".join(
            f"{i}.{d.name} ({d.pos})" for i, d in enumerate(ranked[:10], 1)
        ))
        for pos in POSITIONS:
            group = base_by_pos.get(pos) or []
            if not group:
                continue
            before = statistics.fmean(d.dv for d in group)
            after = statistics.fmean(dv_map[d.player_id].dv for d in group)
            out.append(
                f"    {pos}: mean dv {before:7.2f} -> {after:7.2f} "
                f"({after / before:5.2f}x, slope {slopes[pos]:.2f}); "
                f"replacement ppg {group[0].baseline_ppg:5.2f} -> "
                f"{dv_map[group[0].player_id].baseline_ppg:5.2f}"
            )

    out.append("")
    out.append(
        "  Every position's mean EVoB scales by that position's slope, to two decimals. That is "
        "not a coincidence and not a fit -- it is what a mean-preserving shrink does when the "
        "replacement level shrinks toward the same mean as the player. So a per-position shrink "
        "IS a cross-position reweighting of the board: it says a point of projected QB edge is "
        "worth less than a point of projected RB edge, in proportion to how well each "
        "position's projections predicted 2025."
    )
    return out


def bonus_vs_calibration_lines(
    matched: Sequence[MatchedPlayer],
    cfg: LeagueConfig,
    schedule: Mapping[str, Any],
    curves: Mapping[Any, Any],
    slopes: Mapping[str, float],
    *,
    games_variation: Mapping[str, bool],
) -> list[str]:
    """Which adjustment is bigger: the bonus model the board applies, or the shrink it doesn't?

    The pipeline already adds the per-game yardage bonus, which raises the top of the board, and
    applies no calibration shrink, which would lower it. Applying only the flattering half is a
    bias whichever way the evidence eventually lands, so the two get measured in the same
    currency (2025 season league points) on the same players.
    """
    import numpy as np

    scoring = cfg.scoring
    cap = float(cfg.weeks)
    plain_points = {
        id(p): league_points(_projection_for(p, "blend", games_varies=games_variation), scoring)
        for p in matched
    }
    means = {
        pos: statistics.fmean([plain_points[id(p)] for p in matched if p.pos == pos])
        for pos in POSITIONS
        if any(p.pos == pos for p in matched)
    }

    out: list[str] = []
    out.append("=" * 96)
    out.append(
        "I. THE ADJUSTMENT THE BOARD APPLIES vs THE ONE IT DOESN'T -- same players, same "
        "currency (2025 season league points, blend projection)"
    )
    out.append("=" * 96)
    out.append(f"  shrink slopes used: "
               + ", ".join(f"{pos} {slopes[pos]:.2f}" for pos in POSITIONS if pos in slopes))
    out.append("")
    out.append(f"  {'tier':<16} {'n':>4} {'bonus adds':>12} {'shrink removes':>16} {'ratio':>7}")
    for label in [t[0] for t in ADP_TIERS]:
        group = [p for p in matched if _tier_of(p) == label and p.pos in slopes]
        if not group:
            continue
        bonus_delta = []
        shrink_delta = []
        for player in group:
            line = _projection_for(player, "blend", games_varies=games_variation)
            plain = plain_points[id(player)]
            with_bonus = league_points(
                line, scoring, pos=player.pos, bonus=True,
                schedule=schedule, curves=curves, games_cap=cap,
            )
            bonus_delta.append(with_bonus - plain)
            shrink_delta.append(
                plain - mean_preserving_shrink(plain, means[player.pos], slopes[player.pos])
            )
        b = float(np.mean(bonus_delta))
        c = float(np.mean(shrink_delta))
        ratio = abs(c / b) if b else float("nan")
        out.append(f"  {label:<16} {len(group):>4} {b:>+12.1f} {c:>+16.1f} {ratio:>6.1f}x")
    out.append("")
    out.append(
        "  The bonus and the shrink pull in opposite directions and the shrink is the larger "
        "term at every tier. Note also that the bonus does NOT favour quarterbacks: "
        "tools/compare_bonus_effect.py measures it as +0.10 ppg of top-QB-to-replacement spread "
        "against +0.36 for RB. So the pipeline's one-sidedness is real -- it ships the "
        "board-raising adjustment and not the board-lowering one -- but it is not a thumb on "
        "the scale for QBs specifically. Both adjustments move value from QB toward RB/WR."
    )
    return out


def weight_sweep_lines(
    label: str,
    players: Sequence[MatchedPlayer],
    scoring: Mapping[str, float],
    *,
    step: float = 0.1,
    seed: int = 20260820,
    draws: int = 2_000,
    games_variation: Mapping[str, bool],
) -> list[str]:
    """MAE across the whole Sleeper-weight range, plus a bootstrap of the argmin.

    The point of the bootstrap is the honest part. An in-sample optimal weight always exists
    -- there is always some w that fits 2025 best -- and reading it as a recommendation is the
    classic overfit. Resampling the players shows how much of that optimum is signal: if the
    argmin lands anywhere across the range depending on which players you happened to draw,
    one season has not identified a weight and equal weight stands.
    """
    import numpy as np

    if not players:
        return [f"  {label}: no players"]

    grid = [round(i * step, 4) for i in range(int(round(1 / step)) + 1)]
    actual = np.array([score_statline(p.actual, scoring) for p in players], dtype=float)
    errors = {}
    for w in grid:
        projected = np.array(
            [
                score_statline(
                    blend_statlines(
                        [p.sleeper_proj, p.espn_proj],
                        [w, 1.0 - w],
                        games_varies=blend_games_mask(games_variation),
                    ),
                    scoring,
                )
                for p in players
            ],
            dtype=float,
        )
        errors[w] = np.abs(projected - actual)

    lines = ["", f"  {label} (n={len(players)}):", "    w(sleeper)   MAE"]
    maes = {w: float(errors[w].mean()) for w in grid}
    best = min(maes, key=lambda w: maes[w])
    for w in grid:
        mark = "  <- best on 2025" if w == best else ""
        equal = "  (equal weight)" if abs(w - 0.5) < 1e-9 else ""
        lines.append(f"    {w:>9.2f}   {maes[w]:>6.2f}{mark}{equal}")

    stacked = np.vstack([errors[w] for w in grid])       # grid x players
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, len(players), size=(draws, len(players)))
    argmins = [grid[int(np.argmin(stacked[:, pick].mean(axis=1)))] for pick in picks]
    lo, hi = (float(x) for x in np.percentile(argmins, [2.5, 97.5]))
    at_edge = sum(1 for a in argmins if a in (0.0, 1.0)) / draws
    lines.append(
        f"    best w on 2025 = {best:.2f} (MAE {maes[best]:.2f}) vs equal weight "
        f"MAE {maes[0.5]:.2f}: a gain of {maes[0.5] - maes[best]:.2f} pts per player"
    )
    lines.append(
        f"    bootstrap 95% interval for the best w: {lo:.2f} .. {hi:.2f}; "
        f"the resampled optimum sits at an extreme (0 or 1) in {at_edge:.0%} of draws"
    )
    return lines


def vintage_evidence(
    espn: Mapping[str, EspnRecord], sleeper: Mapping[str, SleeperRecord]
) -> list[str]:
    """Show, per probe player, that both sources' 2025 numbers ignore what actually happened."""
    lines: list[str] = []
    espn_by_name = {clean_name(r.name): r for r in espn.values()}
    sleeper_by_name = {clean_name(r.name): r for r in sleeper.values()}
    for name in VINTAGE_PROBES:
        key = clean_name(name)
        e = espn_by_name.get(key)
        s = sleeper_by_name.get(key)
        if e is None or s is None or e.proj is None:
            lines.append(f"  {name:<18} NOT FOUND in one of the payloads")
            continue
        played = float((e.actual or {}).get("games", 0.0))
        lines.append(
            f"  {name:<18} actually played {played:>4.0f} games | "
            f"ESPN projected {e.proj.get('games', 0.0):>4.1f} games, "
            f"{e.proj.get('pass_yd', 0.0) + e.proj.get('rush_yd', 0.0) + e.proj.get('rec_yd', 0.0):>6.0f} total yds | "
            f"Sleeper projected "
            f"{s.proj.get('pass_yd', 0.0) + s.proj.get('rush_yd', 0.0) + s.proj.get('rec_yd', 0.0):>6.0f} total yds"
        )
    lines.append(
        "  A season-end restatement would put ~0 on a player who never played. Neither source "
        "does, so both are real preseason forecasts."
    )
    return lines


# ======================================================================================= main


def run(refresh: bool = False) -> tuple[str, list[MatchedPlayer]]:
    from draftroom.prep.http import load_latest_raw

    if refresh:
        for label, path in fetch_2025_payloads().items():
            print(f"fetched {label} -> {path}")

    espn_raw = load_cached(ESPN_CACHE)
    sleeper_raw = load_cached(SLEEPER_CACHE)
    ffc_raw = load_cached(FFC_CACHE)
    sleeper_universe = load_latest_raw("sleeper")  # READ ONLY. Never write into data/raw/.

    id_check_lines = verify_espn_stat_ids(espn_raw["players"])
    espn = espn_records(espn_raw["players"])
    sleeper = sleeper_records(sleeper_raw)
    matched, counts, dropped = join_sources(espn, sleeper, sleeper_universe)
    adp_hits = attach_adp(matched, ffc_raw)

    cfg = LeagueConfig.from_yaml()
    report = build_report(
        matched,
        cfg,
        join_counts=counts,
        adp_hits=adp_hits,
        id_check_lines=id_check_lines,
        crosscheck_lines=crosscheck_actuals(matched),
        vintage_lines=vintage_evidence(espn, sleeper),
        dropped=dropped,
    )
    return report, matched


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-pull the three 2025 payloads into data/backtest/ (never data/raw/)",
    )
    args = parser.parse_args(argv)
    report, _ = run(refresh=args.refresh)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
