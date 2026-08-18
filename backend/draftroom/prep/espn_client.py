"""ESPN Fantasy Football adapter: player universe + season projections.

ESPN's own fantasy.espn.com projections page is a client-side JS app -- scraping the
HTML gets an empty shell. The backend it calls is a real JSON API and does NOT require
auth for public league-default data (verified live 2026-08-17):

    GET https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}
        /segments/0/leaguedefaults/3?view=kona_player_info
    header: X-Fantasy-Filter: {"players": {"limit": N, "sortPercOwned": {...}}}

This is the same "leaguedefaults" endpoint used by the community `espn-api` Python
package and widely documented in fantasy-tooling projects. No OAuth/cookie/league
membership needed for this read -- it is public default-scoring player data.

RESPONSE SHAPE (verified live): a dict with a single top-level key ``"players"``, a
list of up to ``limit`` entries sorted by percent-rostered descending. Each entry has
an ``id`` and a nested ``"player"`` object with ``fullName``, ``defaultPositionId``,
``proTeamId``, and a ``"stats"`` list. Each element of ``stats`` is ONE stat block --
distinguished by ``seasonId``, ``statSourceId`` (0 = actual, 1 = projection), and
``statSplitTypeId`` (0 = season total, 1 = weekly) -- carrying a ``"stats"`` dict keyed
by **numeric stat id as a string** (e.g. ``"24": 1372.6``). A single player's ``stats``
list mixes past-season actuals, past-season projections, weekly splits, AND the
current season projection all together; the season-total projection for a given
season is the one block where
``seasonId == season and statSourceId == 1 and statSplitTypeId == 0``.

STAT ID MAPPING -- VERIFIED, NOT TRUSTED FROM A TABLE:
A commonly-cited community mapping (`espn-api`'s ``constant.py``, fetched from
GitHub and cross-checked here) got the RIGHT general neighborhood but was WRONG in
at least one case for this exact payload shape (stat id 22 is documented there as a
second "passingYards" duplicate of id 3; the live number for Josh Allen at id 22 was
232.14, which is not passing yards -- it equals id 3 (3946.42) / games (17), i.e.
passing yards PER GAME, matching the OTHER table in that same file,
``SETTINGS_SCORING_FORMAT_MAP``, which labels 22 as "Passing Yards Per Game"). Every
id actually mapped into CANONICAL_STATS below was cross-checked against derived
identities in real players' own stat blocks, not just copied from a table:
  - id 39 (RYPA) == id 24 (rush_yd) / id 23 (rush_att) for every player checked, which
    confirms 23 and 24 independently of the table.
  - id 60 (YPC) == id 42 (rec_yd) / id 53 (rec) for every player checked, which
    confirms 42 AND 53 (not 41, which never appears in this season-projection
    payload at all) as receptions/receiving yards.
  - id 21 (completion pct) == id 1 (pass_cmp) / id 0 (pass_att), confirming both.
  - id 62 ("total 2pt conversions") == id 19 (pass_2pt) + id 26 (rush_2pt) +
    id 44 (rec_2pt) summed, confirming all three 2pt ids at once.
  - id 73 ("total turnovers") == id 20 (pass_int) + id 72 (fum_lost) for every
    QB checked (no receiving/rushing INTs exist), confirming both.
  - id 72 ("FUML", total fumbles lost) == id 69 (passing fumbles lost) + id 70
    (rushing fumbles lost) + id 71 (receiving fumbles lost) summed -- this is the
    TOTAL-lost figure across all play types, which is exactly what canonical
    ``fum_lost`` wants (not id 68, which is total fumbles including recovered ones).
  - id 210 ("GP") == id 40 (rush yards per GAME) * ... i.e. RYPG * GP == RY (id 24)
    for every player checked; also varies player-to-player (452 of 461 skill
    players project exactly 17.0 games, but a handful project 4, 6, 10, 11, 13, 15
    -- a real per-player figure, not a hardcoded constant like FantasyPros' export).

POSITION AND TEAM IDS -- ALSO VERIFIED, NOT COPIED:
``defaultPositionId`` in this live payload does NOT match the community library's
``POSITION_MAP`` (which is for roster-SLOT ids, a different ESPN numbering used in
``eligibleSlots``). The live ``defaultPositionId`` values were confirmed instead by
looking at known players' ``eligibleSlots`` (which DOES follow the library's slot
numbering: QB slot 0, RB slot 2, WR slot 4, TE slot 6) and by a distribution check
across 1000 players: id 16 appears on exactly 32 players, matching the 32 NFL teams
-- i.e. D/ST, one per team. Verified mapping: 1=QB, 2=RB, 3=WR, 4=TE, 5=K (57
entries), 16=D/ST (32 entries). This league drafts none of K/D/ST (CLAUDE.md), so
only 1-4 are kept.

``proTeamId`` DOES match the community library's ``PRO_TEAM_MAP`` -- verified against
8 known players (Josh Allen->BUF, Mahomes->KC, McCaffrey->SF, Chase->CIN, Jefferson->
MIN, Lamb->DAL, Saquon->PHI, McBride->ARI all matched exactly).

RECEIVING TARGETS: id 58 IS present in this payload and IS receiving targets --
verified via catch rate plausibility (Chase: 119.7 rec / 172.4 tgt = 69% catch rate;
McCaffrey: 79.1 rec / 100.1 tgt = 79%; both plausible) and named prominently because
neither Sleeper nor FantasyPros carries this field at all (see CLAUDE.md /
prep/sleeper_client.py) -- this is the actual fix for the receiver blind spot.
"""

from __future__ import annotations

import json
import logging

from draftroom.prep.http import cache_raw, get_json, make_client
from draftroom.prep.schema import PlayerRef, StatLine

log = logging.getLogger("draftroom.prep.espn")

SOURCE = "espn"

BASE_URL = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
    "/segments/0/leaguedefaults/3?view=kona_player_info"
)

# Verified live 2026-08-17 at limit=1000: 911 of the 1000 returned players are
# QB/RB/WR/TE, of which 461 carry a real season projection block -- comfortably
# over this league's "at least 300 skill-position players" gate. Raise this if a
# future check ever shows the 1000-player window cutting off relevant players
# (it is sorted by percent-rostered descending, so anyone excluded is deep bench).
PLAYER_LIMIT = 1000

# Fantasy-relevant skill positions this league drafts. No K/DST/IDP (CLAUDE.md).
SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}

# ESPN `defaultPositionId` -> position. VERIFIED live (see module docstring) --
# deliberately NOT the community-library POSITION_MAP, which is a different
# (roster-slot) numbering. 5 (K) and 16 (D/ST) are recognized but intentionally
# absent from SKILL_POSITIONS -- this league has neither.
ESPN_POSITION_MAP: dict[int, str] = {
    1: "QB",
    2: "RB",
    3: "WR",
    4: "TE",
    # 5: "K"    -- not carried; this league has no kickers.
    # 16: "DST" -- not carried; this league has no defenses.
}

# ESPN `proTeamId` -> team abbreviation. Verified against 8 known players (see
# module docstring) -- matches the community-library PRO_TEAM_MAP exactly.
ESPN_TEAM_MAP: dict[int, str] = {
    0: "", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN",
    8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR",
    15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI",
    22: "ARI", 23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WSH",
    29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU",
}

# ESPN numeric stat id -> canonical stat name. Every id here was cross-checked
# against a real player's derived-identity math, not just copied from a table --
# see the module docstring for exactly which identity confirmed which id(s).
ESPN_STAT_ID_MAP: dict[int, str] = {
    0: "pass_att",
    1: "pass_cmp",
    3: "pass_yd",
    4: "pass_td",
    19: "pass_2pt",
    20: "pass_int",
    23: "rush_att",
    24: "rush_yd",
    25: "rush_td",
    26: "rush_2pt",
    53: "rec",       # NOT id 41 ("receivingReceptions" per the community table) --
                     # 41 never appears in this season-projection payload; 53
                     # ("REC", each reception) is the id that actually carries the
                     # number, confirmed via the YPC identity in the docstring.
    42: "rec_yd",
    43: "rec_td",
    44: "rec_2pt",
    58: "rec_tgt",   # receivingTargets -- the field Sleeper and FantasyPros lack.
    72: "fum_lost",  # total fumbles lost across all play types (not id 68, which
                     # is total fumbles including ones the player's own team
                     # recovered).
    210: "games",    # a real per-player figure here, unlike FantasyPros' blanket
                     # 17 (see prep/manual_csv.py) -- most skill players still
                     # land on 17, but not all.
}

# Stat ids observed in real payloads that are deliberately NOT mapped, because
# they are rate stats (per-game, per-attempt, completion/catch rate), bonus-yardage
# buckets, big-game/long-TD bonus counts, or components already summed into an id
# above -- not distinct canonical stats. Listed explicitly (rather than inferred
# by prefix, since ESPN's ids are unlabeled integers) so a genuinely new/unknown id
# still gets flagged instead of silently vanishing into this set by accident.
ESPN_IGNORED_STAT_IDS: frozenset[int] = frozenset({
    2,  # passingIncompletions -- derivable from pass_att - pass_cmp
    5, 6, 7, 8, 9, 10, 11, 12, 13, 14,  # passing yardage/completion bonus buckets
    15, 16, 17, 18,  # 40+/50+ yd TD pass, 300/400-yard game bonuses
    21,  # completion pct (rate; confirms 0 & 1, not itself mapped)
    22,  # passing yards PER GAME (rate; see docstring -- not a yards duplicate)
    27, 28, 29, 30, 31,  # rushing yardage bonus buckets
    33, 34,  # rushing attempt bonus buckets
    35, 36, 37, 38,  # 40+/50+ yd TD rush, 100/200-yard game bonuses
    39, 40,  # rush yards/attempt, rush yards/game (rates; confirm 23 & 24)
    41,  # alt receptions id -- never populated in this payload; 53 is used instead
    45, 46,  # 40+/50+ yd TD reception bonuses
    47, 48, 49, 50, 51,  # receiving yardage bonus buckets
    56, 57,  # 100/200-yard receiving game bonuses
    59,  # receiving yards after catch -- no canonical stat
    60, 61,  # yards per catch, receiving yards per game (rates; confirm 42 & 53)
    62,  # total 2pt conversions across all types (sum of 19+26+44; confirms those)
    63,  # fumble recovered for a player's own TD -- no canonical stat
    64,  # times sacked -- no canonical stat
    65, 66, 67,  # fumbles BY TYPE (passing/rushing/receiving), not "lost" -- not
                 # the fum_lost figure
    68,  # total fumbles (including recovered by own team) -- not "lost"
    69, 70, 71,  # fumbles LOST by type -- components already summed into 72
    73,  # total turnovers (int + fum_lost; confirms 20 & 72 together)
    211, 212, 213,  # first-down counts by play type -- no canonical stat
    # Return-game stats (kickoff/punt return yards, TDs, and yardage-bucket
    # bonuses) -- observed live on return specialists (e.g. Rashid Shaheed,
    # KaVontae Turpin). No canonical stat exists for return production in this
    # league (same treatment Yahoo's stat_map.py gives "Return TD": recognised,
    # unsupported, never scored -- this league has no IDP/return scoring either).
    101, 102, 103, 104, 105, 114, 115, 116, 117, 118, 119,
    # Defensive stats (sacks, tackles, INTs, fumble recoveries, passes defensed,
    # etc.) -- observed live on Travis Hunter, who plays both WR and CB and
    # therefore carries a defensive stat block alongside his receiving one. This
    # league has no IDP scoring at all (CLAUDE.md: "no kickers, no defenses"), so
    # none of these have a canonical stat regardless of position.
    89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 106, 107, 108, 109, 110,
    111, 112, 113,
})


def fetch_projections(season: int) -> tuple[str, list[dict]]:
    """Fetch ESPN's public league-defaults player/projection endpoint.

    Returns (url_used, raw_players_list) where raw_players_list is the JSON
    payload's top-level "players" array -- each element has an "id" and a nested
    "player" object carrying name/position/team/stats. Caches the FULL raw
    response dict (not just the players list) to data/raw/espn/, so nothing about
    the response shape is lost even though we only return the list.

    Raises RuntimeError if the response isn't the expected shape -- never
    fabricates data. No retry storm: this reuses prep/http.py's shared client
    (3 attempts total on 429/5xx, per CLAUDE.md's "keep it polite" instruction).
    """
    url = BASE_URL.format(season=season)
    filt = json.dumps({"players": {"limit": PLAYER_LIMIT, "sortPercOwned": {"sortAsc": False, "sortPriority": 1}}})
    headers = {"X-Fantasy-Filter": filt}

    with make_client(headers=headers) as client:
        raw = get_json(client, url)

    if not isinstance(raw, dict) or not isinstance(raw.get("players"), list):
        raise RuntimeError(
            f"ESPN projections endpoint ({url}) returned an unexpected shape: "
            f"{type(raw).__name__}. Expected a dict with a 'players' list. ESPN may "
            "have changed this endpoint -- do not guess a fix, inspect the real "
            "response."
        )

    players = raw["players"]
    cache_raw(SOURCE, raw, suffix="json")
    return url, players


def to_statlines(raw: list[dict], season: int) -> dict[str, StatLine]:
    """Map ESPN's per-player stat blocks into CANONICAL_STATS, keyed by player_id.

    NOTE ON SIGNATURE: unlike sleeper_client.to_statlines(raw), this needs `season`
    as an explicit argument. ESPN's payload is not pre-filtered to one season's
    projections the way Sleeper's endpoint is -- every player's "stats" list mixes
    past-season actuals, past-season projections, and weekly splits together, so
    picking the right block (seasonId == season, statSourceId == 1,
    statSplitTypeId == 0) has to happen here, not upstream.

    Players with no such block (no season projection published for them yet, or
    who are K/D/ST/other and therefore not in SKILL_POSITIONS) are omitted from
    the output entirely rather than emitted as an all-zero StatLine.

    Any stat id present with a nonzero value that isn't in ESPN_STAT_ID_MAP and
    isn't in ESPN_IGNORED_STAT_IDS gets logged as a warning (mirroring
    sleeper_client's convention) so an unexpected/new field doesn't disappear
    silently -- see ESPN_STAT_ID_MAP's docstring note for how every mapped id was
    verified, not just copied from a lookup table.
    """
    out: dict[str, StatLine] = {}
    for entry in raw:
        player = entry.get("player") or {}
        pid = player.get("id")
        if pid is None:
            continue

        pos = ESPN_POSITION_MAP.get(player.get("defaultPositionId"))
        if pos not in SKILL_POSITIONS:
            continue  # K/D/ST/unknown -- this league drafts neither (CLAUDE.md)

        stats_list = player.get("stats") or []
        season_block = next(
            (
                s
                for s in stats_list
                if s.get("seasonId") == season
                and s.get("statSourceId") == 1
                and s.get("statSplitTypeId") == 0
            ),
            None,
        )
        if season_block is None:
            continue  # no season-total projection published for this player yet

        stat_dict = season_block.get("stats") or {}
        if not stat_dict:
            continue

        name = player.get("fullName") or f"{player.get('firstName', '')} {player.get('lastName', '')}".strip()
        kwargs: dict[str, float] = {}
        for raw_key, value in stat_dict.items():
            try:
                stat_id = int(raw_key)
            except (TypeError, ValueError):
                log.warning(
                    "ESPN projections: non-numeric stat key %r for player_id=%s (%s)",
                    raw_key, pid, name or "unknown",
                )
                continue

            canonical = ESPN_STAT_ID_MAP.get(stat_id)
            if canonical is not None:
                kwargs[canonical] = float(value or 0.0)
                continue

            if stat_id in ESPN_IGNORED_STAT_IDS:
                continue

            if value:
                log.warning(
                    "ESPN projections: unmapped nonzero stat_id=%r=%r for player_id=%s (%s)",
                    stat_id, value, pid, name or "unknown",
                )

        out[str(pid)] = StatLine(**kwargs)

    return out


def to_player_refs(raw: list[dict], season: int) -> dict[str, PlayerRef]:
    """Identity (name/pos/team) for every ESPN player :func:`to_statlines` would emit a
    StatLine for, keyed by the SAME ``str(pid)`` -- needed so a caller can resolve ESPN rows
    onto the crosswalk's pid (the crosswalk's resolver hooks need name/team/pos, which
    :func:`to_statlines` deliberately discards once it has built a StatLine). Mirrors the
    identical population/filtering :func:`to_statlines` uses (same season-block check) so the
    two functions' outputs line up key-for-key.
    """
    out: dict[str, PlayerRef] = {}
    for entry in raw:
        player = entry.get("player") or {}
        pid = player.get("id")
        if pid is None:
            continue

        pos = ESPN_POSITION_MAP.get(player.get("defaultPositionId"))
        if pos not in SKILL_POSITIONS:
            continue

        stats_list = player.get("stats") or []
        season_block = next(
            (
                s
                for s in stats_list
                if s.get("seasonId") == season
                and s.get("statSourceId") == 1
                and s.get("statSplitTypeId") == 0
            ),
            None,
        )
        if season_block is None or not (season_block.get("stats") or {}):
            continue  # no season-total projection published -- matches to_statlines exactly

        name = player.get("fullName") or f"{player.get('firstName', '')} {player.get('lastName', '')}".strip()
        if not name:
            continue
        team = ESPN_TEAM_MAP.get(player.get("proTeamId"), "")

        out[str(pid)] = PlayerRef(name=name, pos=pos, team=team, source_id=str(pid), source=SOURCE)

    return out
