"""Player-identity crosswalk: joins Sleeper, FFC, FantasyPros, and Yahoo onto one
internal `pid` (Sleeper's own player_id -- it's the richest source and already
carries most other sources' cross-IDs, so there's no reason to invent a new key).

Resolution cascade, in order, per player row from a non-Sleeper source:
  0. data/overrides.csv        -- manual fix, checked FIRST, wins over everything.
  1. direct ID equality        -- via Sleeper's own cross-ID fields (espn_id,
                                   yahoo_id, fantasy_data_id, rotowire_id, ...)
                                   and via the DynastyProcess ff_playerids CSV.
  2. normalize_name+pos+team   -- exact match, team included.
  3. normalize_name+pos        -- exact match, team ignored (catches trades /
                                   source disagreement on team). Ties -> unresolved.
  4. fuzzy (rapidfuzz)         -- token_sort_ratio >= 90 within position, best
                                   match must beat the second-best by >= 5. Ties
                                   -> unresolved.
  else: unresolved.

Every resolution records HOW it resolved (`resolve_method`) so a surprising
join is auditable later. See CLAUDE.md gate #2: zero unresolved inside the
top 200 FFC ADP players.

NEVER GUESS A JOIN. Ambiguous means unresolved means it shows up on the
triage report (unresolved_report / data/unresolved_report.csv), never a
silently-picked candidate.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from rapidfuzz import fuzz

from draftroom.prep.ffc_client import AdpRow
from draftroom.prep.http import cache_raw, make_client, request_with_retry
from draftroom.prep.schema import PlayerRef, normalize_name
from draftroom.prep.sleeper_client import SKILL_POSITIONS, filter_active_skill_players

log = logging.getLogger("draftroom.prep.crosswalk")

# backend/draftroom/prep/crosswalk.py -> parents[3] == repo root (C:\dev\draftroom),
# same depth as prep/http.py's REPO_ROOT.
REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
OVERRIDES_PATH = DATA_DIR / "overrides.csv"

# Verified live 2026-08-14: this URL returns 200, ~12,472 rows, header
# mfl_id,sportradar_id,fantasypros_id,gsis_id,pff_id,sleeper_id,nfl_id,espn_id,
# yahoo_id,fleaflicker_id,cbs_id,pfr_id,cfbref_id,rotowire_id,rotoworld_id,
# ktc_id,stats_id,stats_global_id,fantasy_data_id,swish_id,name,merge_name,
# position,team,birthdate,age,draft_year,draft_round,draft_pick,draft_ovr,
# twitter_username,height,weight,college,db_season. `sleeper_id` is the pivot
# column used to build the direct-ID index below.
DYNASTYPROCESS_URL = "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv"
DYNASTYPROCESS_SOURCE = "dynastyprocess"

# Cross-ID fields present on many Sleeper player records (confirmed live
# 2026-08-14 against data/raw/sleeper/*.json). Values are often null/None for
# any given player; only nonempty values go into the index.
SLEEPER_CROSS_ID_FIELDS: tuple[str, ...] = (
    "espn_id",
    "yahoo_id",
    "fantasy_data_id",
    "rotowire_id",
    "rotoworld_id",
    "gsis_id",
    "sportradar_id",
    "stats_id",
    "stats_global_id",
    "pff_id",
)

# DynastyProcess ff_playerids CSV columns that are cross-source IDs (excludes
# name/position/team/bio columns). sleeper_id is the pivot: every other ID
# column in the same row maps to that row's sleeper_id.
DYNASTYPROCESS_ID_COLUMNS: tuple[str, ...] = (
    "mfl_id",
    "sportradar_id",
    "fantasypros_id",
    "gsis_id",
    "pff_id",
    "nfl_id",
    "espn_id",
    "yahoo_id",
    "fleaflicker_id",
    "cbs_id",
    "pfr_id",
    "cfbref_id",
    "rotowire_id",
    "rotoworld_id",
    "ktc_id",
    "stats_id",
    "stats_global_id",
    "fantasy_data_id",
    "swish_id",
)

FUZZY_THRESHOLD = 90.0
FUZZY_MARGIN = 5.0

# "0" is included because DynastyProcess's stats_global_id column uses 0 as a
# missing-value sentinel (confirmed 2026-08-14: 6,538 of 12,472 rows have
# stats_global_id == "0", all colliding onto whichever row is seen first --
# not a real shared ID). No real cross-source ID in this data is legitimately "0".
_NA_VALUES = {"", "na", "none", "null", "0"}


def _clean_id_value(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in _NA_VALUES:
        return None
    return s


# ---------------------------------------------------------------------------
# DynastyProcess crosswalk CSV: fetch/cache + parse into a per-field ID index
# ---------------------------------------------------------------------------


def fetch_dynastyprocess_csv() -> str:
    """Fetch + cache the DynastyProcess ff_playerids ID crosswalk CSV.

    Verified live 2026-08-14 (see DYNASTYPROCESS_URL comment above). If this
    URL ever 404s, this raises loudly instead of silently skipping stage 1 --
    go find the new path under https://github.com/dynastyprocess/data/tree/master/files
    and update DYNASTYPROCESS_URL, don't guess a replacement here.
    """
    with make_client() as client:
        resp = request_with_retry(client, "GET", DYNASTYPROCESS_URL)
    if resp.status_code == 404:
        raise RuntimeError(
            f"DynastyProcess crosswalk URL 404'd: {DYNASTYPROCESS_URL}. The file "
            "moved -- check https://github.com/dynastyprocess/data/tree/master/files "
            "for the current path and update DYNASTYPROCESS_URL in crosswalk.py. "
            "Do not silently skip stage 1."
        )
    resp.raise_for_status()
    text = resp.text
    cache_raw(DYNASTYPROCESS_SOURCE, text, suffix="csv")
    return text


def _build_dynastyprocess_sleeper_index(csv_text: str) -> dict[str, dict[str, str]]:
    """id_field -> {id_value: sleeper_id}, pivoting on the CSV's sleeper_id column."""
    index: dict[str, dict[str, str]] = {col: {} for col in DYNASTYPROCESS_ID_COLUMNS}
    reader = csv.DictReader(csv_text.splitlines())
    for row in reader:
        sleeper_id = _clean_id_value(row.get("sleeper_id"))
        if sleeper_id is None:
            continue
        for col in DYNASTYPROCESS_ID_COLUMNS:
            val = _clean_id_value(row.get(col))
            if val is None:
                continue
            existing = index[col].get(val)
            if existing is not None and existing != sleeper_id:
                log.warning(
                    "DynastyProcess crosswalk: %s=%r maps to multiple sleeper_ids "
                    "(%s and %s); keeping the first seen.",
                    col,
                    val,
                    existing,
                    sleeper_id,
                )
                continue
            index[col].setdefault(val, sleeper_id)
    return index


def _build_sleeper_cross_id_index(sleeper_raw: dict) -> dict[str, dict[str, str]]:
    """id_field -> {id_value: sleeper player_id}, straight off Sleeper's own records."""
    index: dict[str, dict[str, str]] = {f: {} for f in SLEEPER_CROSS_ID_FIELDS}
    for pid, p in sleeper_raw.items():
        if not p:
            continue
        for field_name in SLEEPER_CROSS_ID_FIELDS:
            val = _clean_id_value(p.get(field_name))
            if val is None:
                continue
            index[field_name][val] = pid
    return index


# ---------------------------------------------------------------------------
# overrides.csv
# ---------------------------------------------------------------------------

_OVERRIDES_HEADER = "source,source_key,pid\n"
_OVERRIDES_COMMENT = (
    "# Manual crosswalk overrides. Checked FIRST on every run, before any "
    "automatic resolution stage, so a fix here is permanent. source_key must "
    "match exactly what that source's resolve() call uses as its row key "
    "(e.g. FFC's is the FFC player_id, as a string). pid is the Sleeper "
    "player_id this row should resolve to.\n"
)


def load_overrides(path: Path = OVERRIDES_PATH) -> dict[tuple[str, str], str]:
    """Load (source, source_key) -> pid overrides. Creates the file if absent."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_OVERRIDES_HEADER + _OVERRIDES_COMMENT, encoding="utf-8")
        return {}

    out: dict[tuple[str, str], str] = {}
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)  # header
        for row in reader:
            if not row or not row[0].strip() or row[0].lstrip().startswith("#"):
                continue
            if len(row) < 3:
                continue
            source, source_key, pid = row[0].strip(), row[1].strip(), row[2].strip()
            if not source or not source_key or not pid:
                continue
            out[(source, source_key)] = pid
    return out


# ---------------------------------------------------------------------------
# Core resolution
# ---------------------------------------------------------------------------


@dataclass
class ResolvedEntry:
    """One source row's resolution outcome, kept for audit and reporting."""

    source: str
    source_key: str
    pid: str | None
    resolve_method: str  # override | direct_id | exact_name_team_pos | exact_name_pos | fuzzy | unresolved
    name: str
    team: str
    pos: str
    detail: str = ""
    adp: float | None = None


@dataclass
class Crosswalk:
    """The resolved join: Sleeper's active-player universe plus every other
    source row that's been resolved against it so far."""

    players: dict[str, PlayerRef]  # Sleeper player_id -> PlayerRef (the spine)
    entries: dict[tuple[str, str], ResolvedEntry] = field(default_factory=dict)
    _overrides: dict[tuple[str, str], str] = field(default_factory=dict)
    _sleeper_cross_index: dict[str, dict[str, str]] = field(default_factory=dict)
    _dynastyprocess_index: dict[str, dict[str, str]] = field(default_factory=dict)
    _by_norm_pos: dict[tuple[str, str], list[PlayerRef]] = field(default_factory=dict)
    _by_pos: dict[str, list[PlayerRef]] = field(default_factory=dict)

    def resolve(self, source: str, source_key: str) -> str | None:
        entry = self.entries.get((source, str(source_key)))
        return entry.pid if entry else None

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries.values():
            counts[entry.resolve_method] = counts.get(entry.resolve_method, 0) + 1
        return counts

    def unresolved_report(self) -> list[dict]:
        """Rows of (source, name, team, pos, adp_if_known, detail incl. top
        candidate guesses+scores), sorted ADP-ascending (unknown ADP last) so
        the players that matter most for the completeness gate show first."""
        rows = []
        for entry in self.entries.values():
            if entry.pid is not None:
                continue
            rows.append(
                {
                    "source": entry.source,
                    "name": entry.name,
                    "team": entry.team,
                    "pos": entry.pos,
                    "adp": entry.adp,
                    "detail": entry.detail,
                }
            )
        rows.sort(key=lambda r: (r["adp"] is None, r["adp"] if r["adp"] is not None else 0.0))
        return rows

    # -- internal: the actual cascade, shared by every source -------------

    def _resolve_row(
        self,
        source: str,
        source_key: str,
        name: str,
        team: str,
        pos: str,
        extra_ids: dict[str, str | None] | None = None,
    ) -> ResolvedEntry:
        key = (source, str(source_key))

        # Stage 0: overrides win over everything.
        override_pid = self._overrides.get(key)
        if override_pid is not None:
            return ResolvedEntry(
                source, str(source_key), override_pid, "override", name, team, pos,
                detail="data/overrides.csv",
            )

        # Stage 1: direct ID equality (Sleeper's own cross-IDs + DynastyProcess).
        if extra_ids:
            for field_name, raw_value in extra_ids.items():
                value = _clean_id_value(raw_value)
                if value is None:
                    continue
                pid = self._sleeper_cross_index.get(field_name, {}).get(value)
                via = "sleeper"
                if pid is None:
                    pid = self._dynastyprocess_index.get(field_name, {}).get(value)
                    via = "dynastyprocess"
                if pid is not None and pid in self.players:
                    return ResolvedEntry(
                        source, str(source_key), pid, "direct_id", name, team, pos,
                        detail=f"{field_name}={value} via {via}",
                    )

        norm = normalize_name(name)
        pos_u = (pos or "").strip().upper()
        team_u = (team or "").strip().upper()

        # Stage 2: exact normalize_name + position + team.
        same_norm_pos = self._by_norm_pos.get((norm, pos_u), [])
        team_matches = [p for p in same_norm_pos if p.team.strip().upper() == team_u]
        if len(team_matches) == 1:
            return ResolvedEntry(
                source, str(source_key), team_matches[0].source_id,
                "exact_name_team_pos", name, team, pos,
            )
        if len(team_matches) > 1:
            return ResolvedEntry(
                source, str(source_key), None, "unresolved", name, team, pos,
                detail=f"ambiguous: {len(team_matches)} Sleeper players share name+team+pos",
            )

        # Stage 3: exact normalize_name + position, team ignored.
        if len(same_norm_pos) == 1:
            return ResolvedEntry(
                source, str(source_key), same_norm_pos[0].source_id,
                "exact_name_pos", name, team, pos,
                detail=f"team differs (source={team_u!r} vs sleeper={same_norm_pos[0].team!r})",
            )
        if len(same_norm_pos) > 1:
            return ResolvedEntry(
                source, str(source_key), None, "unresolved", name, team, pos,
                detail=f"ambiguous: {len(same_norm_pos)} Sleeper players share name+pos, team ignored",
            )

        # Stage 4: fuzzy match within position, must be uniquely above threshold.
        candidates = self._by_pos.get(pos_u, [])
        scored = sorted(
            ((fuzz.token_sort_ratio(norm, normalize_name(p.name)), p) for p in candidates),
            key=lambda t: t[0],
            reverse=True,
        )
        if scored:
            best_score, best_p = scored[0]
            second_score = scored[1][0] if len(scored) > 1 else -1.0
            if best_score >= FUZZY_THRESHOLD and (best_score - second_score) >= FUZZY_MARGIN:
                return ResolvedEntry(
                    source, str(source_key), best_p.source_id, "fuzzy", name, team, pos,
                    detail=f"{best_p.name} score={best_score:.1f} next={second_score:.1f}",
                )
            top = ", ".join(f"{p.name}({p.team}) {s:.0f}" for s, p in scored[:3])
            return ResolvedEntry(
                source, str(source_key), None, "unresolved", name, team, pos,
                detail=f"best fuzzy candidates: {top or 'none'}",
            )

        return ResolvedEntry(
            source, str(source_key), None, "unresolved", name, team, pos,
            detail=f"no Sleeper players at position {pos_u!r}",
        )

    # -- public resolver hooks for sources not yet wired into build_crosswalk --

    def resolve_fantasypros_row(
        self, source_key: str, name: str, team: str, pos: str, fantasypros_id: str | None = None,
    ) -> ResolvedEntry:
        """Resolver hook for FantasyPros. Not yet called from build_crosswalk --
        there's no cached FantasyPros data to iterate (no API key configured, see
        prep/fantasypros_client.py). Once real rows exist, call this per-row; it
        reuses the same cascade (override -> fantasypros_id direct match via the
        DynastyProcess index -> name/team/pos -> fuzzy)."""
        extra_ids: dict[str, str | None] = {"fantasypros_id": fantasypros_id} if fantasypros_id else None
        entry = self._resolve_row("fantasypros", source_key, name, team, pos, extra_ids=extra_ids)
        self.entries[(entry.source, entry.source_key)] = entry
        return entry

    def resolve_yahoo_row(
        self, source_key: str, name: str, team: str, pos: str, yahoo_id: str | None = None,
    ) -> ResolvedEntry:
        """Resolver hook for Yahoo. Not yet called from build_crosswalk -- Yahoo
        access is gated by manual application (see CLAUDE.md). Once rosters/picks
        are available, call this per-row; Sleeper's own yahoo_id field means most
        rows should resolve at stage 1 (direct_id) without ever touching name
        matching."""
        extra_ids: dict[str, str | None] = {"yahoo_id": yahoo_id} if yahoo_id else None
        entry = self._resolve_row("yahoo", source_key, name, team, pos, extra_ids=extra_ids)
        self.entries[(entry.source, entry.source_key)] = entry
        return entry


def _ffc_source_key(row: AdpRow) -> str:
    if row.player_id is not None:
        return str(row.player_id)
    return f"{row.name}|{row.team}|{row.pos}"


def build_crosswalk(
    sleeper_raw: dict,
    ffc_rows: Iterable[AdpRow],
    *,
    dynastyprocess_csv_text: str | None = None,
    overrides_path: Path = OVERRIDES_PATH,
) -> Crosswalk:
    """Build the crosswalk: Sleeper is the spine, FFC rows get resolved onto it.

    `dynastyprocess_csv_text` is optional so this can run (with a reduced
    stage-1 index -- Sleeper's own cross-IDs only) even before
    fetch_dynastyprocess_csv() has ever been run; resolve_cli warns loudly
    when that happens rather than silently degrading.
    """
    players = filter_active_skill_players(sleeper_raw)

    cw = Crosswalk(players=players)
    cw._overrides = load_overrides(overrides_path)
    cw._sleeper_cross_index = _build_sleeper_cross_id_index(sleeper_raw)
    cw._dynastyprocess_index = (
        _build_dynastyprocess_sleeper_index(dynastyprocess_csv_text)
        if dynastyprocess_csv_text
        else {}
    )

    for p in players.values():
        norm = normalize_name(p.name)
        pos_u = p.pos.strip().upper()
        cw._by_norm_pos.setdefault((norm, pos_u), []).append(p)
        cw._by_pos.setdefault(pos_u, []).append(p)

    for row in ffc_rows:
        source_key = _ffc_source_key(row)
        pos_u = (row.pos or "").strip().upper()
        if pos_u not in SKILL_POSITIONS:
            # This league drafts no K/DST (CLAUDE.md: "No kickers. No defenses.").
            # Sleeper's own spine is filtered to QB/RB/WR/TE by design, so a DEF/PK
            # row can never join -- that's a scope fact, not a crosswalk miss, so
            # it gets its own resolve_method rather than polluting "unresolved".
            entry = ResolvedEntry(
                "ffc", source_key, None, "out_of_league_scope", row.name, row.team, row.pos,
                detail=f"position {row.pos!r} is not drafted in this league (no K/DST)",
            )
        else:
            entry = cw._resolve_row("ffc", source_key, row.name, row.team, row.pos)
        entry.adp = row.adp
        cw.entries[(entry.source, entry.source_key)] = entry

    return cw
