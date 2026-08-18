"""Player pool for the live server: search, tier board, and roster tracking.

This module is deliberately independent of whatever `draftroom.draft.recommend` needs
internally for real draft-value math (EVoB, replacement levels, VONA) -- that engine is being
built concurrently and this file must not depend on it. What the live UI needs *right now* is
much smaller: every player's identity (id/name/pos/team/bye) and an ordering to rank on.

The pool has TWO TIERS, and the distinction is the whole point of this module:

* **Ranked** players -- present in the cached FFC 2QB ADP payload (~189). These carry a real
  ADP, a std_dev the survival model needs, and an ADP-derived placeholder `value`.
* **Unranked** players -- every other active skill-position player on an NFL roster, taken from
  the cached Sleeper universe (~949 total). No ADP, no projection, `value` 0.0, and they are
  never recommended. They exist so the board can RECORD them.

Why both: this tool's first job is bookkeeping. A 10-team x 15-round draft is 150 picks, and a
pool of only 189 ranked players leaves 39 of margin, so by the late rounds the room is taking
players the board cannot see and every one becomes a manual write-in. Marc asked for every
draftable name to be listed "even if we don't have projections" -- tracking needs the name,
recommending needs the projection, and those are different requirements.

Bye weeks are a property of the TEAM, not the player, so the 32-team bye map is derived from
whichever FFC rows carry one and applied to the whole universe (Sleeper's own payload carries
`bye_week` for 0 of 988 records -- verified 2026-08-17).

Offline-safe by construction: both inputs are read from `data/raw/` caches, never the network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

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
    "REAL_VALUE_NOTE",
    "DISAGREEMENT_CV_THRESHOLD",
]

log = logging.getLogger("draftroom.live_data")

PLACEHOLDER_VALUE_NOTE = (
    "FALLBACK MODE: the real valuation board could not be built from cache, so value is an "
    "ADP-derived placeholder -- NOT the validated model. Recommendations in this mode are not "
    "trustworthy (re-run prep to restore the cached board); players with is_ranked=False have "
    "no projection at all and are listed so they can be recorded, never recommended"
)

REAL_VALUE_NOTE = (
    "value is the real risk-adjusted DraftValue from the validated board "
    "(draftroom.validate.board.build_real_board: league-scored Sleeper projections + bonus "
    "model + availability-capped games + EVoB); ranked players that failed the board join "
    "carry value_is_real=False and value 0.0 (name kept for bookkeeping, no evaluation "
    "implied); players with is_ranked=False have no projection at all"
)

#: Cross-source disagreement is flagged as a "danger" badge when the coefficient of variation
#: (points_stdev / points_mean across Sleeper/FantasyPros/ESPN) is at or above this. Chosen from
#: the actual 2026-08-18 distribution over the 186 ranked players with >=2 independent sources:
#: median CV ~0.045, 80th percentile ~0.10, max ~0.36 -- this threshold flags roughly the top
#: fifth of the board, not a token handful and not everyone. Per
#: draftroom.valuation.disagreement's mandated caveat: HIGH disagreement is the real signal;
#: its absence below this line is NOT evidence the projection is safe.
DISAGREEMENT_CV_THRESHOLD = 0.10


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
    #: The real DraftValue from the validated board when the join landed (value_is_real=True,
    #: see REAL_VALUE_NOTE); otherwise the ADP placeholder in fallback mode, or 0.0 for a
    #: ranked player the board build excluded (name kept for bookkeeping, no evaluation).
    value: float
    #: True when this player came from the ADP feed and so has a real ADP/std_dev/value.
    #: False for roster-only players who exist to be RECORDED, never recommended. The UI must
    #: show these as "no projection" rather than implying a value of zero is an evaluation.
    is_ranked: bool = True
    #: Cross-source (Sleeper/FantasyPros/ESPN) PPG sigma from draftroom.valuation.disagreement,
    #: joined in by name+team -- None when <2 independent sources resolved (never a fabricated
    #: 0.0). Best-effort: absent entirely if the cached prep data needed to compute it is
    #: missing (see `_load_disagreement_by_key`).
    sigma_ppg: float | None = None
    #: Coefficient of variation (points_stdev / points_mean) across those same sources. None
    #: under the same conditions as sigma_ppg.
    disagreement_cv: float | None = None
    #: True when disagreement_cv >= DISAGREEMENT_CV_THRESHOLD. A danger signal only -- LOW/False
    #: here is NOT evidence the projection is accurate (see valuation/disagreement.py's caveat).
    disagreement_high: bool = False
    #: Sleeper's own injury/practice-report fields, from the cached universe file. None when
    #: Sleeper has nothing on record (the normal case for a healthy player).
    injury_status: str | None = None
    practice_participation: str | None = None
    depth_chart_order: int | None = None
    #: Season-total SD of `value` (from cross-source disagreement, via the real board). 0.0
    #: when unknown -- absence of spread data, never "certainty". (Appended at the end of the
    #: field list deliberately: tests construct PoolPlayer positionally up to earlier fields.)
    value_sd: float = 0.0
    #: True only when `value` came from the validated real board. False for the ADP
    #: placeholder, for excluded-ranked players, and for all unranked players.
    value_is_real: bool = False


def _player_id_for(row: ffc_client.AdpRow) -> str:
    """FFC's numeric player_id when present; otherwise a stable name/team/pos key.

    The cached payload always carries `player_id` as of the 2026-08-14 verification in
    ffc_client.py, but this falls back rather than crashing if a future refresh drops it.
    """
    if row.player_id is not None:
        return str(row.player_id)
    return f"{clean_name(row.name)}|{row.team}|{row.pos}"


#: ADP assigned to unranked players so they sort after every ranked player without
#: needing a separate sort key. Not a real ADP; `is_ranked` is the field to test.
UNRANKED_ADP = 999.0
#: Wide std_dev for unranked players. They are never recommended (value 0.0), so this only
#: keeps the survival math from dividing by a zero spread if it is ever handed one.
UNRANKED_STDEV = 50.0


def _match_key(name: str, team: str, pos: str) -> str:
    """name|team|POSITION. Position was added 2026-08-18 (Codex review): a name+team key can
    silently collide (and silently overwrite) if a team ever carries two same-named players at
    different positions; with position in the key, `_load_real_board_by_key` can also ASSERT
    uniqueness instead of last-writer-wins."""
    return f"{clean_name(name)}|{(team or '').strip().upper()}|{(pos or '').strip().upper()}"


def _load_real_board_by_key() -> dict[str, dict[str, Any]]:
    """The validated real board's per-player values, keyed by name|team|pos for joining onto
    this module's FFC-ID-based pool.

    Built from `draftroom.validate.board.build_real_board()` -- the one production path through
    the full pipeline (league-scored Sleeper projections + bonus model + availability-capped
    games + EVoB + cross-source disagreement). That module's player_id is the crosswalk's
    (Sleeper-derived) id, which does NOT match this module's FFC-derived `player_id` (verified
    2026-08-18: Josh Allen is FFC id 2885 here, Sleeper/crosswalk id 4984 there) -- name+team+
    pos is the key both sides share.

    Reads only cached files under data/raw/ (same guarantee as everything else in this module,
    and required for draft night). Any failure -- a missing cache, a crosswalk hiccup, the
    validate/valuation pipeline not being available -- degrades to an EMPTY result, which
    callers must treat as "fallback placeholder mode" and surface LOUDLY (the recommendations
    served in that mode are not the validated model).

    Raises:
        ValueError: two board players collapse to the same name|team|pos key. Silent overwrite
            here would quietly hand one player another's valuation; per repo convention that is
            a hard failure, never a skip.
    """
    try:
        from draftroom.validate.board import build_real_board

        rb = build_real_board()
    except Exception as exc:  # noqa: BLE001 - degrades to fallback mode, surfaced by callers
        log.warning(
            "REAL BOARD UNAVAILABLE (%s): pool will fall back to ADP-placeholder values -- "
            "recommendations are NOT the validated model until prep restores the cache", exc,
        )
        return {}

    season_by_pid = {s.player_id: s for s in rb.seasons}
    out: dict[str, dict[str, Any]] = {}
    collisions: list[str] = []
    for bp in rb.players:
        d = rb.disagreement.get(bp.player_id)
        season = season_by_pid.get(bp.player_id)
        sigma_ppg = season.sigma_ppg if season is not None else None
        cv: float | None = None
        high = False
        if d is not None and d.has_disagreement_signal and d.points_mean > 0:
            cv = d.points_stdev / d.points_mean
            high = cv >= DISAGREEMENT_CV_THRESHOLD
        key = _match_key(bp.name, bp.team, bp.pos)
        if key in out:
            collisions.append(key)
            continue
        out[key] = {
            "value": float(bp.dv),
            "value_sd": float(bp.dv_sd or 0.0),
            "sigma_ppg": sigma_ppg,
            "disagreement_cv": cv,
            "disagreement_high": high,
        }
    if collisions:
        raise ValueError(
            f"real-board join key collision(s) -- two players share name|team|pos: {collisions}. "
            "Refusing to guess which valuation belongs to whom."
        )
    return out


def load_player_pool(
    path: str | Path | None = None, *, include_unranked: bool = True
) -> list[PoolPlayer]:
    """Build the live player pool: ranked ADP players plus the whole rosterable universe.

    No network call, ever -- reads `data/raw/ffc/*.json` and `data/raw/sleeper/*.json` off
    disk. Draft night runs with wifi off; this must work from whatever prep last cached.

    Args:
        path: explicit cached FFC payload, for tests.
        include_unranked: set False to get only the ADP-ranked players (the pre-2026-08-17
            behavior). Kept so tests that assert on the ranked set stay meaningful.

    Invariant, asserted below: every player the ADP feed knows about survives into the pool.
    Widening the universe must never DROP someone who was previously visible.
    """
    raw = load_latest_raw("ffc") if path is None else __import__("json").loads(
        Path(path).read_text(encoding="utf-8")
    )
    rows = ffc_client.parse_adp_rows(raw)
    # FFC's ADP feed is generic (it carries K/DEF); this league rosters neither
    # (CLAUDE.md: "No kickers. No defenses."), so they never enter the live pool.
    skill_rows = [r for r in rows if (r.pos or "").strip().upper() in SKILL_POSITIONS]
    rows_sorted = sorted(skill_rows, key=lambda r: r.adp)

    # The validated real board's values (dv, dv_sd, disagreement -- see _load_real_board_by_key)
    # and Sleeper's own injury/practice-report fields (best-effort, see below) are both keyed by
    # name|team|pos so they can enrich BOTH ranked and unranked players from one lookup each,
    # built once up front. An empty real board means FALLBACK PLACEHOLDER MODE (loudly logged).
    real_by_key = _load_real_board_by_key()
    fallback_mode = not real_by_key
    if fallback_mode:
        log.warning(
            "POOL IN FALLBACK MODE: no real board available; ranked players carry the "
            "ADP-derived placeholder value. Do not trust recommendations from this state."
        )

    sleeper_meta_by_key: dict[str, dict[str, Any]] = {}
    universe: dict[str, Any] | None = None
    try:
        universe = load_latest_raw("sleeper")
    except Exception as exc:  # noqa: BLE001 - a missing cache must not break draft night
        log.warning(
            "no cached Sleeper universe (%s); pool will have no injury/practice-report data "
            "and (if include_unranked) will be ADP-only.", exc,
        )
    if universe:
        for p in universe.values():
            if not p or p.get("position") not in SKILL_POSITIONS:
                continue
            name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            if not name:
                continue
            key = _match_key(name, p.get("team") or "", p.get("position") or "")
            sleeper_meta_by_key.setdefault(key, p)

    def _injury_fields(key: str) -> dict[str, Any]:
        meta = sleeper_meta_by_key.get(key)
        if not meta:
            return {"injury_status": None, "practice_participation": None, "depth_chart_order": None}
        dco = meta.get("depth_chart_order")
        return {
            "injury_status": meta.get("injury_status"),
            "practice_participation": meta.get("practice_participation"),
            "depth_chart_order": int(dco) if isinstance(dco, (int, float)) else None,
        }

    out: list[PoolPlayer] = []
    seen_ids: set[str] = set()
    ranked_keys: set[str] = set()
    joined = 0
    for rank, row in enumerate(rows_sorted, start=1):
        pid = _player_id_for(row)
        key = _match_key(row.name, row.team, row.pos)
        enrich = real_by_key.get(key)
        if enrich is not None:
            # Real DraftValue from the validated board -- the same numbers the mock-draft sim
            # was validated on (Codex 2026-08-18: draft night previously served an ADP
            # placeholder the sims never exercised).
            value = float(enrich["value"])
            value_sd = float(enrich["value_sd"])
            value_is_real = True
            joined += 1
        elif fallback_mode:
            # No real board at all: the old monotone ADP placeholder, loudly flagged upstream.
            value, value_sd, value_is_real = max(0.0, 300.0 - row.adp), 0.0, False
        else:
            # Real board exists but excluded this player (unresolved crosswalk / no projection).
            # Keep the NAME -- bookkeeping beats valuation -- but 0.0 with value_is_real=False,
            # rendered as "no real projection", never mixed onto the real-dv scale (a 300-scale
            # placeholder among ~90-scale real dvs would top every ranking it doesn't belong in).
            value, value_sd, value_is_real = 0.0, 0.0, False
        out.append(
            PoolPlayer(
                player_id=pid,
                name=row.name,
                pos=row.pos,
                team=row.team,
                bye=row.bye,
                adp=row.adp,
                stdev=row.std_dev,
                overall_rank=rank,
                value=value,
                is_ranked=True,
                sigma_ppg=(enrich or {}).get("sigma_ppg"),
                disagreement_cv=(enrich or {}).get("disagreement_cv"),
                disagreement_high=bool((enrich or {}).get("disagreement_high", False)),
                value_sd=value_sd,
                value_is_real=value_is_real,
                **_injury_fields(key),
            )
        )
        seen_ids.add(pid)
        ranked_keys.add(key)
    if not fallback_mode:
        log.info(
            "real board joined onto %d of %d ranked players (%d kept by name only)",
            joined, len(rows_sorted), len(rows_sorted) - joined,
        )

    ranked_count = len(out)
    if not include_unranked:
        return out

    # Bye weeks come from the ADP feed (Sleeper carries none), and a bye belongs to the team.
    bye_by_team: dict[str, int] = {}
    for row in skill_rows:
        team = (row.team or "").strip().upper()
        if team and row.bye and team not in bye_by_team:
            bye_by_team[team] = row.bye

    if universe is None:
        return out

    extras: list[tuple[int, str, dict]] = []
    for pid, p in universe.items():
        if not p or p.get("position") not in SKILL_POSITIONS:
            continue
        if not p.get("active") or not p.get("team"):
            continue
        name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        if not name:
            continue
        if _match_key(name, p.get("team", ""), p.get("position", "")) in ranked_keys:
            continue  # already in, with a real ADP
        # Sleeper's own search_rank orders the unranked tail sensibly (lower = more relevant);
        # fall back to depth chart, then to a constant so the sort stays total and stable.
        sr = p.get("search_rank")
        order = int(sr) if isinstance(sr, (int, float)) else 10_000 + int(
            p.get("depth_chart_order") or 99
        )
        extras.append((order, name, p))

    extras.sort(key=lambda t: (t[0], t[1]))
    for offset, (_order, name, p) in enumerate(extras, start=1):
        pid = f"sl-{p.get('player_id')}"
        if pid in seen_ids:
            continue
        team = (p.get("team") or "").strip().upper()
        dco = p.get("depth_chart_order")
        out.append(
            PoolPlayer(
                player_id=pid,
                name=name,
                pos=p["position"],
                team=team,
                bye=bye_by_team.get(team),
                adp=UNRANKED_ADP,
                stdev=UNRANKED_STDEV,
                overall_rank=ranked_count + offset,
                value=0.0,
                is_ranked=False,
                # Unranked players are never valued, so cross-source disagreement (a valuation
                # input) is not computed for them -- sigma_ppg/disagreement stay None/False.
                injury_status=p.get("injury_status"),
                practice_participation=p.get("practice_participation"),
                depth_chart_order=int(dco) if isinstance(dco, (int, float)) else None,
            )
        )
        seen_ids.add(pid)

    # The invariant. Widening the pool must be purely additive.
    assert sum(1 for p in out if p.is_ranked) == ranked_count, (
        "widening the pool dropped an ADP-ranked player; that is a regression, not a widening"
    )
    log.info(
        "player pool: %d ranked (ADP) + %d roster-only = %d total; byes known for %d teams",
        ranked_count, len(out) - ranked_count, len(out), len(bye_by_team),
    )
    return out


def to_searchable(pool: list[PoolPlayer]) -> list[SearchablePlayer]:
    return [
        SearchablePlayer(
            player_id=p.player_id,
            name=p.name,
            pos=p.pos,
            team=p.team,
            overall_rank=p.overall_rank,
            is_ranked=p.is_ranked,
        )
        for p in pool
    ]


def pos_of_map(pool: list[PoolPlayer]) -> dict[str, str]:
    return {p.player_id: p.pos for p in pool}


def index_by_id(pool: list[PoolPlayer]) -> Mapping[str, PoolPlayer]:
    return {p.player_id: p for p in pool}
