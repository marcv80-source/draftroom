"""Sleeper API adapter: player universe + season projections.

Two endpoints, both undocumented-but-stable in practice:
  - GET https://api.sleeper.app/v1/players/nfl        (note: .app)  -> player universe
  - GET https://api.sleeper.com/projections/nfl/...   (note: .com)  -> season projections

The projections endpoint is NOT documented by Sleeper. Verified live on 2026-08-14:
the URL shape given in the task (`/projections/nfl/<season>?season_type=regular&
position[]=...&order_by=adp_half_ppr`) returns HTTP 200 with a JSON *list* of
per-player season-projection records (not a dict keyed by player_id). Each record
has a `player_id`, a `player` sub-object, and a `stats` dict of Sleeper's own stat
field names. See fetch_projections() docstring for the fallback URLs tried if the
primary one ever breaks.
"""

from __future__ import annotations

import logging
from typing import Any

from draftroom.prep.http import cache_raw, get_json, make_client
from draftroom.prep.schema import PlayerRef, StatLine

log = logging.getLogger("draftroom.prep.sleeper")

SOURCE = "sleeper"
SOURCE_PROJECTIONS = "sleeper_projections"

PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"

# Confirmed live 2026-08-14: this is the URL given in the task spec, and it works.
PROJECTIONS_URL_PRIMARY = (
    "https://api.sleeper.com/projections/nfl/{season}"
    "?season_type=regular&position[]=QB&position[]=RB&position[]=WR&position[]=TE"
    "&order_by=adp_half_ppr"
)
# Documented-in-the-wild fallbacks, tried only if the primary URL stops working.
PROJECTIONS_URL_FALLBACK_WEEK = "https://api.sleeper.com/projections/nfl/{season}/1"
PROJECTIONS_URL_FALLBACK_PLAYER = (
    "https://api.sleeper.com/projections/nfl/player/{player_id}"
    "?season={season}&season_type=regular&grouping=season"
)

# Fantasy-relevant skill positions this league drafts. No K/DST/IDP.
SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}

# Sleeper stat field name -> canonical stat name. Confirmed against a live pull of
# the 2026 season projections (3,111 records, union of all `stats` keys observed).
SLEEPER_STAT_MAP: dict[str, str] = {
    "pass_att": "pass_att",
    "pass_cmp": "pass_cmp",
    "pass_yd": "pass_yd",
    "pass_td": "pass_td",
    "pass_int": "pass_int",
    "pass_2pt": "pass_2pt",
    "rush_att": "rush_att",
    "rush_yd": "rush_yd",
    "rush_td": "rush_td",
    "rush_2pt": "rush_2pt",
    "rec": "rec",
    "rec_yd": "rec_yd",
    "rec_td": "rec_td",
    "rec_2pt": "rec_2pt",
    "fum_lost": "fum_lost",
    "gp": "games",
}

# NOTE: Sleeper's season projections contain NO reception-target field under any
# name (confirmed: no `rec_tgt`, `trg`, or `targets` key across a full 3,111-record
# pull). `rec_tgt` therefore always comes back 0.0 from this source. If targets are
# needed, they'll have to come from FantasyPros or another source.

# Fields Sleeper returns that we deliberately do NOT map into CANONICAL_STATS,
# because they are fantasy-points/ADP/derived-bucket metadata, not component
# stats (mapping fantasy points would violate "adapters emit stats, never
# points"). These are expected and NOT logged as dropped.
_IGNORED_PREFIXES = ("adp_", "pts_", "bonus_", "idp_")
_IGNORED_EXACT = {
    "cmp_pct",
    "def_kr_td",
    "pr_td",
    "pass_fd",
    "rush_fd",
    "rec_fd",
    "rec_0_4",
    "rec_5_9",
    "rec_10_19",
    "rec_20_29",
    "rec_30_39",
    "rec_40p",
}


def _is_ignored_field(key: str) -> bool:
    return key in _IGNORED_EXACT or key.startswith(_IGNORED_PREFIXES)


def fetch_players() -> dict:
    """GET the full Sleeper player universe (~5MB), cache it, return the raw dict.

    Raw shape: {player_id (str): {full_name, first_name, last_name, position,
    team, active (bool), status, fantasy_positions (list), ...}, ...}. Not
    filtered here -- use filter_active_skill_players() for that.
    """
    with make_client() as client:
        raw = get_json(client, PLAYERS_URL)
    cache_raw(SOURCE, raw, suffix="json")
    return raw


def filter_active_skill_players(raw: dict) -> dict[str, PlayerRef]:
    """Filter the raw player universe to active QB/RB/WR/TE, keyed by source_id.

    This league has no K/DST, so those positions (and everything else -- OL,
    DL, LB, CB, S, etc.) are dropped here, along with inactive/retired players.
    """
    out: dict[str, PlayerRef] = {}
    for pid, p in raw.items():
        if not p:
            continue
        if p.get("position") not in SKILL_POSITIONS:
            continue
        if not p.get("active"):
            continue
        name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        if not name:
            continue
        out[pid] = PlayerRef(
            name=name,
            pos=p["position"],
            team=p.get("team") or "",
            source_id=pid,
            source=SOURCE,
        )
    return out


def fetch_projections(season: int) -> tuple[str, list[dict]]:
    """Fetch Sleeper's undocumented season-projections endpoint.

    NOTE ON RETURN TYPE: the task spec guessed `-> dict`, but the verified live
    shape is a JSON *list* of per-player records, so this returns
    (url_used, raw_list) -- the url_used lets callers/reports show exactly which
    endpoint variant actually worked, since none of this is documented by Sleeper.

    Tries, in order: the primary URL given in the task spec (confirmed working
    2026-08-14), then two documented-in-the-wild fallbacks if the primary ever
    404s or stops returning the expected list-of-records-with-`stats` shape.
    Raises RuntimeError if none work -- never fabricates data.
    """
    urls_tried: list[str] = []
    with make_client() as client:
        primary_url = PROJECTIONS_URL_PRIMARY.format(season=season)
        urls_tried.append(primary_url)
        try:
            raw = get_json(client, primary_url)
            if isinstance(raw, list) and (not raw or ("stats" in raw[0] and "player_id" in raw[0])):
                cache_raw(SOURCE_PROJECTIONS, raw, suffix="json")
                return primary_url, raw
            log.warning("Sleeper primary projections URL returned unexpected shape: %r", type(raw))
        except Exception as exc:  # noqa: BLE001 - deliberately broad, we fall back
            log.warning("Sleeper primary projections URL failed: %s", exc)

        week_url = PROJECTIONS_URL_FALLBACK_WEEK.format(season=season)
        urls_tried.append(week_url)
        try:
            raw = get_json(client, week_url)
            if isinstance(raw, list) and (not raw or ("stats" in raw[0] and "player_id" in raw[0])):
                cache_raw(SOURCE_PROJECTIONS, raw, suffix="json")
                return week_url, raw
            log.warning("Sleeper week-form projections URL returned unexpected shape: %r", type(raw))
        except Exception as exc:  # noqa: BLE001
            log.warning("Sleeper week-form projections URL failed: %s", exc)

    raise RuntimeError(
        "No Sleeper projections URL worked. Tried: "
        + ", ".join(urls_tried)
        + ". The per-player fallback "
        + PROJECTIONS_URL_FALLBACK_PLAYER
        + " would need a list of player_ids to loop over one at a time and was not "
        "attempted here since the season-wide endpoints are far cheaper."
    )


def to_statlines(raw: list[dict]) -> dict[str, StatLine]:
    """Map Sleeper's stat field names into CANONICAL_STATS, keyed by player_id.

    Any field not in SLEEPER_STAT_MAP and not a known-ignored metadata field
    (ADP/points/bucket-breakdown fields Sleeper also returns) gets logged if it
    carries a nonzero value, so an unexpected/new field doesn't disappear silently.
    """
    out: dict[str, StatLine] = {}
    for rec in raw:
        pid = rec.get("player_id")
        if pid is None:
            continue
        stats = rec.get("stats") or {}
        kwargs: dict[str, float] = {}
        for src_key, value in stats.items():
            if src_key in SLEEPER_STAT_MAP:
                kwargs[SLEEPER_STAT_MAP[src_key]] = float(value or 0.0)
                continue
            if _is_ignored_field(src_key):
                continue
            if value:
                player = rec.get("player") or {}
                pname = player.get("full_name") or f"{player.get('first_name', '')} {player.get('last_name', '')}".strip()
                log.warning(
                    "Sleeper projections: unmapped nonzero field %r=%r for player_id=%s (%s)",
                    src_key,
                    value,
                    pid,
                    pname or "unknown",
                )
        out[pid] = StatLine(**kwargs)
    return out
