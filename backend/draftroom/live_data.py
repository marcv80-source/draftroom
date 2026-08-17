"""Player pool for the live server: search, tier board, and roster tracking.

This module is deliberately independent of whatever `draftroom.draft.recommend` needs
internally for real draft-value math (EVoB, replacement levels, VONA) -- that engine is being
built concurrently and this file must not depend on it. What the live UI needs *right now* is
much smaller: every player's identity (id/name/pos/team/bye) and an ordering to rank on.

The only artifact guaranteed to exist offline is the cached FFC 2QB ADP payload
(`data/raw/ffc/*.json`, via CLAUDE.md's "never re-fetch in a test" convention). So the pool
here is built straight from that cache, and the `value` field is an explicit ADP-derived
PLACEHOLDER -- good enough to draw a value bar and group tiers, not a real DraftValue. When
`draftroom.draft.recommend` (and whatever snapshot it consumes) lands, the recommendation
endpoint's real candidates carry the true numbers; this pool keeps the rest of the UI
(search, tier board, roster) working in the meantime.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from draftroom.draft.search import SearchablePlayer
from draftroom.prep import ffc_client
from draftroom.prep.http import load_latest_raw
from draftroom.prep.schema import clean_name
from draftroom.prep.sleeper_client import SKILL_POSITIONS

__all__ = [
    "PoolPlayer",
    "load_player_pool",
    "to_searchable",
    "pos_of_map",
    "index_by_id",
    "PLACEHOLDER_VALUE_NOTE",
]

PLACEHOLDER_VALUE_NOTE = (
    "value is an ADP-derived placeholder pending draftroom.draft.recommend's real DraftValue"
)


@dataclass(frozen=True)
class PoolPlayer:
    """One player, reduced to what the live server needs to render and search on."""

    player_id: str
    name: str
    pos: str
    team: str
    bye: int | None
    adp: float
    stdev: float
    overall_rank: int  # 1 = best ADP, i.e. most valuable available
    #: ADP-derived placeholder, monotonically decreasing in ADP. See PLACEHOLDER_VALUE_NOTE.
    value: float


def _player_id_for(row: ffc_client.AdpRow) -> str:
    """FFC's numeric player_id when present; otherwise a stable name/team/pos key.

    The cached payload always carries `player_id` as of the 2026-08-14 verification in
    ffc_client.py, but this falls back rather than crashing if a future refresh drops it.
    """
    if row.player_id is not None:
        return str(row.player_id)
    return f"{clean_name(row.name)}|{row.team}|{row.pos}"


def load_player_pool(path: str | Path | None = None) -> list[PoolPlayer]:
    """Build the live player pool from the newest cached FFC 2QB ADP payload.

    No network call, ever -- this reads `data/raw/ffc/*.json` off disk (or an explicit
    `path` to a cached payload, for tests). Draft night runs with wifi off; this must work
    from whatever was cached the last time prep ran online.
    """
    raw = load_latest_raw("ffc") if path is None else __import__("json").loads(
        Path(path).read_text(encoding="utf-8")
    )
    rows = ffc_client.parse_adp_rows(raw)
    # FFC's ADP feed is generic (it carries K/DEF); this league rosters neither
    # (CLAUDE.md: "No kickers. No defenses."), so they never enter the live pool.
    skill_rows = [r for r in rows if (r.pos or "").strip().upper() in SKILL_POSITIONS]
    rows_sorted = sorted(skill_rows, key=lambda r: r.adp)

    out: list[PoolPlayer] = []
    for rank, row in enumerate(rows_sorted, start=1):
        out.append(
            PoolPlayer(
                player_id=_player_id_for(row),
                name=row.name,
                pos=row.pos,
                team=row.team,
                bye=row.bye,
                adp=row.adp,
                stdev=row.std_dev,
                overall_rank=rank,
                # Monotonically decreasing placeholder "value" so the tier engine and value
                # bars have something sensible to group/scale on before real DraftValue exists.
                value=max(0.0, 300.0 - row.adp),
            )
        )
    return out


def to_searchable(pool: list[PoolPlayer]) -> list[SearchablePlayer]:
    return [
        SearchablePlayer(
            player_id=p.player_id, name=p.name, pos=p.pos, team=p.team, overall_rank=p.overall_rank
        )
        for p in pool
    ]


def pos_of_map(pool: list[PoolPlayer]) -> dict[str, str]:
    return {p.player_id: p.pos for p in pool}


def index_by_id(pool: list[PoolPlayer]) -> Mapping[str, PoolPlayer]:
    return {p.player_id: p for p in pool}
