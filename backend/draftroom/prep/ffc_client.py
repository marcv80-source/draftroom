"""Fantasy Football Calculator adapter: the only free 2QB-specific ADP source.

This drives the whole survival model (survival is conditioned on
`std_dev`-implied draft-position variance), so its field names and the "is
this really 2QB" sanity check both matter. Verified live on 2026-08-14 against
https://fantasyfootballcalculator.com/api/v1/adp/2qb?teams=12&year=2026 --
see ADP_FIELD_MAP for the exact (unverified-in-advance) field names returned.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from draftroom.prep.http import cache_raw, get_json, make_client

log = logging.getLogger("draftroom.prep.ffc")

SOURCE = "ffc"

BASE_URL = "https://fantasyfootballcalculator.com/api/v1/adp/{fmt}?teams={teams}&year={year}"

# A real 2QB ADP should have QBs pushed well up into the first two rounds.
# "Several" QBs in the top 20 overall is the sanity bar from CLAUDE.md/spec.
MIN_QBS_IN_TOP_20 = 5


@dataclass
class AdpRow:
    name: str
    pos: str
    team: str
    adp: float
    std_dev: float
    high: int
    low: int
    times_drafted: int
    bye: int | None
    # Not in the original spec's field list, but FFC gives it and it's useful
    # for crosswalking; defaulted so positional construction per the spec still works.
    player_id: int | None = None


def fetch_adp(fmt: str = "2qb", teams: int = 12, year: int = 2026) -> dict:
    """GET FFC's ADP endpoint for `fmt` (e.g. "2qb"), cache the raw response.

    Confirmed live response shape (2026-08-14):
        {"status": "Success",
         "meta": {"type": "2 QB", "teams": 12, "rounds": 15,
                   "total_drafts": <int>, "start_date": ..., "end_date": ...},
         "players": [{"player_id": int, "name": str, "position": str,
                      "team": str, "adp": float, "adp_formatted": str,
                      "times_drafted": int, "high": int, "low": int,
                      "stdev": float, "bye": int}, ...]}

    The standard-deviation field IS present -- it's called `stdev`, not
    `std_dev` as the spec guessed.
    """
    url = BASE_URL.format(fmt=fmt, teams=teams, year=year)
    with make_client() as client:
        raw = get_json(client, url)
    cache_raw(SOURCE, raw, suffix="json")
    return raw


def parse_adp_rows(raw: dict) -> list[AdpRow]:
    """Parse FFC's raw JSON into AdpRow objects, in the ADP order FFC returned."""
    rows: list[AdpRow] = []
    for p in raw.get("players", []):
        rows.append(
            AdpRow(
                name=p["name"],
                pos=p["position"],
                team=p.get("team") or "",
                adp=float(p["adp"]),
                std_dev=float(p["stdev"]),
                high=int(p["high"]),
                low=int(p["low"]),
                times_drafted=int(p["times_drafted"]),
                bye=p.get("bye"),
                player_id=p.get("player_id"),
            )
        )
    return rows


def check_is_2qb_format(rows: list[AdpRow]) -> tuple[bool, list[str]]:
    """Sanity check: in a real 2QB ADP, several QBs land in the top 20 overall.

    Returns (passed, qb_names_in_top_20). Raises AssertionError if it fails --
    this is a hard gate per CLAUDE.md, since the whole model assumes 2QB ADP.
    """
    top20 = sorted(rows, key=lambda r: r.adp)[:20]
    qbs = [r.name for r in top20 if r.pos == "QB"]
    passed = len(qbs) >= MIN_QBS_IN_TOP_20
    if not passed:
        log.error(
            "FFC ADP does NOT look like 2QB format: only %d QBs in top 20 (%r). "
            "Expected >= %d. Check the `fmt` param and FFC's response.",
            len(qbs),
            qbs,
            MIN_QBS_IN_TOP_20,
        )
    assert passed, (
        f"FFC ADP sanity check failed: only {len(qbs)} QBs in top 20 overall "
        f"(expected >= {MIN_QBS_IN_TOP_20}). This ADP does not look like 2QB format."
    )
    return passed, qbs
