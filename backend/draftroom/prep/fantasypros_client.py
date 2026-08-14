"""FantasyPros adapter: raw stat-line projections + consensus ECR. Premium, needs an API key.

STATUS AS OF 2026-08-14: no API key is configured (`%LOCALAPPDATA%\\draftroom\\
secrets.json` does not exist on this machine), so NONE of this has been
exercised against a live response. The URL/params below and the field mapping
in to_statlines() are best-effort guesses based on FantasyPros' publicly
documented v2 API shape -- they are UNVERIFIED and must be checked with
`--probe` the moment a key exists, before this module's output is trusted for
anything. Do not treat FIELD_MAP as fact until then.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

from draftroom.prep.http import cache_raw, get_json, make_client
from draftroom.prep.schema import StatLine

log = logging.getLogger("draftroom.prep.fantasypros")

SOURCE = "fantasypros"

SECRETS_PATH = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "draftroom" / "secrets.json"
SECRETS_KEY = "fantasypros_api_key"

# One easily-edited constant, per the spec. Path/params NOT verified live.
URL_TEMPLATE = (
    "https://api.fantasypros.com/public/v2/json/nfl/{season}/projections"
    "?position={position}&scoring=HALF&week=draft"
)


class NotConfiguredError(RuntimeError):
    """Raised when no FantasyPros API key is available. Never hit the network without one."""


def _load_api_key() -> str:
    if not SECRETS_PATH.exists():
        raise NotConfiguredError(
            f"FantasyPros not configured yet: no secrets file at {SECRETS_PATH}. "
            f'Add {{"{SECRETS_KEY}": "<key>"}} to that file to enable this source.'
        )
    data = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    key = data.get(SECRETS_KEY)
    if not key:
        raise NotConfiguredError(
            f"FantasyPros not configured yet: '{SECRETS_KEY}' missing from {SECRETS_PATH}."
        )
    return key


def fetch_projections(position: str, season: int = 2026) -> dict:
    """Fetch raw FantasyPros projections for one position (e.g. "QB", "RB").

    Raises NotConfiguredError if no API key is set up. Never hits the network
    without a key, and never invents one.
    """
    api_key = _load_api_key()
    url = URL_TEMPLATE.format(season=season, position=position.upper())
    with make_client(headers={"x-api-key": api_key}) as client:
        raw = get_json(client, url)
    cache_raw(SOURCE, raw, suffix="json")
    return raw


# ---------------------------------------------------------------------------
# Response mapping -- ISOLATED and UNVERIFIED. Fix this against a real payload
# (captured via --probe) before trusting it. Guessed shape: a top-level
# "players" list, each item with a "stats" dict of FantasyPros field names.
# ---------------------------------------------------------------------------

FIELD_MAP: dict[str, str] = {
    "pass_att": "pass_att",
    "pass_cmp": "pass_cmp",
    "pass_yd": "pass_yd",
    "pass_td": "pass_td",
    "pass_int": "pass_int",
    "rush_att": "rush_att",
    "rush_yd": "rush_yd",
    "rush_td": "rush_td",
    "rec": "rec",
    "rec_tgt": "rec_tgt",
    "rec_yd": "rec_yd",
    "rec_td": "rec_td",
    "fum_lost": "fum_lost",
    "games": "games",
}


def to_statlines(raw: dict) -> dict[str, StatLine]:
    """Map FantasyPros stat field names into CANONICAL_STATS, keyed by player id.

    UNVERIFIED: this assumes raw["players"] is a list of {"player_id": ...,
    "stats": {...}} objects. If the real shape differs (very likely, since it
    has never been probed live), this will raise KeyError/TypeError loudly
    rather than silently returning wrong data -- run --probe and fix FIELD_MAP
    and the shape assumptions here first.
    """
    players = raw.get("players")
    if players is None:
        raise KeyError(
            "Expected raw['players'] -- FantasyPros response shape has not been "
            "verified live. Run `python -m draftroom.prep.fantasypros_client --probe` "
            "with a real API key and fix this function against the real shape."
        )
    out: dict[str, StatLine] = {}
    for p in players:
        pid = p.get("player_id")
        if pid is None:
            continue
        stats = p.get("stats") or {}
        kwargs: dict[str, float] = {}
        for src_key, value in stats.items():
            canonical = FIELD_MAP.get(src_key)
            if canonical is None:
                if value:
                    log.warning(
                        "FantasyPros: unmapped nonzero field %r=%r for player_id=%s",
                        src_key,
                        value,
                        pid,
                    )
                continue
            kwargs[canonical] = float(value or 0.0)
        out[str(pid)] = StatLine(**kwargs)
    return out


def _probe(position: str, season: int) -> None:
    """Print the raw response for a human to eyeball. Requires a real API key."""
    try:
        raw: Any = fetch_projections(position=position, season=season)
    except NotConfiguredError as exc:
        print(f"NOT CONFIGURED: {exc}")
        return
    print(json.dumps(raw, indent=2)[:5000])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FantasyPros probe/debug CLI")
    parser.add_argument("--probe", action="store_true", help="fetch and print the raw response")
    parser.add_argument("--position", default="QB", help="QB, RB, WR, or TE")
    parser.add_argument("--season", type=int, default=2026)
    args = parser.parse_args()

    if args.probe:
        _probe(args.position, args.season)
    else:
        parser.print_help()
