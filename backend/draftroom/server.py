"""The live draft server: one process, API + WebSocket + the built frontend.

Draft night runs with wifi physically off (CLAUDE.md). This module is the offline half of
the two-phase architecture: it opens the append-only event log (`draftroom.draft.events`),
replays it into a `DraftState` (`draftroom.draft.state`) on every request, and never makes an
outbound network call -- enforced at runtime by `install_socket_guard`, not just by review.

Run it with:

    python -m draftroom.server --draft [--port 8484]

`draftroom.draft.recommend` is being built concurrently by another agent and may not exist
yet, or may not match the interface this module tries first. Every call into it is wrapped so
a missing or mismatched module degrades to an explicit placeholder recommendation rather than
a 500 -- see `_call_recommend_engine`.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import socket as _socket_module
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from draftroom import live_data
from draftroom.config import LeagueConfig, REPO_ROOT
from draftroom.draft import snake
from draftroom.draft.events import EventLog
from draftroom.draft.search import search as run_search
from draftroom.draft.state import DraftSession, DraftState
from draftroom.explain.primitives import Recommendation

log = logging.getLogger("draftroom.server")

DEFAULT_PORT = 8484
DEFAULT_LOG_PATH = REPO_ROOT / "data" / "drafts" / "draft.jsonl"
#: The projection source served when the log names none (plan B1: the equal-weight composite).
#: Defined once here so `DraftBoard`'s default and `create_app`'s resume logic cannot drift.
DEFAULT_SOURCE_KEY = "blend"

# `draftroom.draft.recommend` is owned by a concurrent agent. Import defensively: any failure
# here (module not written yet, or a broken partial state) must not stop this server from
# starting, because the rest of the UI (board, search, roster, event log) is independently
# useful and testable without it.
try:
    from draftroom.draft import recommend as _recommend_mod  # type: ignore
except Exception as exc:  # noqa: BLE001 - intentionally broad, see module docstring
    _recommend_mod = None
    log.warning("draftroom.draft.recommend not importable yet (%s); using placeholder recommendations", exc)

# `draftroom.sources` (plan B1/B2) is owned by another concurrent agent and may not exist yet,
# or may not match the interface `/api/sources` and `/api/source` expect. Imported defensively
# for the same reason as `recommend` above: the rest of the server must start and work even if
# this module is missing or broken. See `_sources_payload` / `_switch_source`.
try:
    from draftroom import sources as _sources_mod  # type: ignore
except Exception as exc:  # noqa: BLE001 - intentionally broad, see module docstring
    _sources_mod = None
    log.warning("draftroom.sources not importable yet (%s); source toggle degraded", exc)

#: The "elite QB grab" knob (fix "C"(b)), mirrored here so the UI has a visible control and a
#: default even when `draftroom.draft.recommend` isn't importable. Falls back to the spec
#: default (3) rather than failing if that module's own constant isn't available.
_DEFAULT_ELITE_QB_CUTOFF: int = getattr(_recommend_mod, "ELITE_QB_RANK_CUTOFF", 3)


# --------------------------------------------------------------------------- offline guard


class OfflineViolation(RuntimeError):
    """Raised when draft-mode code attempts an outbound non-localhost connection."""


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
_original_connect = _socket_module.socket.connect
_guard_installed = False


def _guarded_connect(self: _socket_module.socket, address: Any) -> Any:  # noqa: ANN401
    host = address[0] if isinstance(address, tuple) else address
    if host not in _LOCAL_HOSTS:
        raise OfflineViolation(
            f"blocked outbound connection to {address!r} -- draft mode is offline-only "
            "(wifi is physically off on draft night; see CLAUDE.md)"
        )
    return _original_connect(self, address)


def install_socket_guard() -> None:
    """Monkeypatch socket.socket.connect so any non-localhost destination raises.

    Idempotent: calling this more than once does not stack wrappers.
    """
    global _guard_installed
    if _guard_installed:
        return
    _socket_module.socket.connect = _guarded_connect  # type: ignore[assignment]
    _guard_installed = True


def uninstall_socket_guard() -> None:
    """Restore the real `connect`. Mainly for tests that need to undo the guard."""
    global _guard_installed
    _socket_module.socket.connect = _original_connect  # type: ignore[assignment]
    _guard_installed = False


def assert_socket_guard_blocks_external() -> None:
    """Self-test: prove the guard actually blocks a non-localhost destination.

    Connects to a literal IP (8.8.8.8) so no DNS lookup happens -- the guard raises before
    the real `connect` syscall runs, so this never touches the network even though it looks
    like it might. If the guard is broken, this raises AssertionError and the server refuses
    to start rather than silently drafting on an assumption that turned out false.
    """
    s = _socket_module.socket(_socket_module.AF_INET, _socket_module.SOCK_STREAM)
    try:
        try:
            s.connect(("8.8.8.8", 53))
        except OfflineViolation:
            return
        raise AssertionError("socket guard did not block an outbound connection to 8.8.8.8:53")
    finally:
        s.close()


# --------------------------------------------------------------------------- request models


class PickRequest(BaseModel):
    player_id: str
    pick_no: int | None = None
    team_slot: int | None = None
    raw_query: str = ""


class StubRequest(BaseModel):
    name: str
    pos: str
    pick_no: int | None = None
    team_slot: int | None = None


class CorrectRequest(BaseModel):
    pick_no: int
    player_id: str | None = None
    stub_name: str | None = None
    stub_pos: str | None = None
    # Reassign-to-team (plan A3): when present, the correction also moves ownership of the
    # pick. Absent (None) leaves team_slot byte-for-byte unchanged, same as before this field
    # existed.
    team_slot: int | None = None


class VoidRequest(BaseModel):
    pick_no: int


class ReassignRequest(BaseModel):
    """Move one pick's ownership. Carries no player identity ON PURPOSE -- see the
    `pick_reassigned` note in draft/events.py."""

    pick_no: int
    team_slot: int


class ClockRequest(BaseModel):
    pick_no: int


class TeamNameRequest(BaseModel):
    team_slot: int
    name: str


class TeamNamesRequest(BaseModel):
    # Keys are team_slot as a string (JSON object keys are always strings), e.g. {"1": "..."}.
    names: dict[str, str]


class SourceRequest(BaseModel):
    key: str


# --------------------------------------------------------------------------- serialization helpers


def _open_slots_summary(fill: dict[str, Any]) -> str:
    """One-line 'what does this team still need' summary, e.g. 'QB done · needs 1 WR · flex
    open' -- the derived line the opponent roster cards render so Marc doesn't have to do the
    arithmetic himself off a bare position-count grid."""
    parts: list[str] = []
    for pos, s in fill["starters"].items():
        if s["need"] <= 0:
            continue
        if s["filled"] >= s["need"]:
            parts.append(f"{pos} done")
        else:
            parts.append(f"needs {s['need'] - s['filled']} {pos}")
    if fill["flex"]["need"] > 0:
        parts.append("flex open" if fill["flex"]["filled"] < fill["flex"]["need"] else "flex filled")
    return " · ".join(parts) if parts else "starters set"


def _pick_view(p, pool: dict[str, live_data.PoolPlayer]) -> dict[str, Any]:
    if p.player_id is not None:
        player = pool.get(p.player_id)
        name = player.name if player else p.player_id
        pos = player.pos if player else None
        team = player.team if player else None
        bye = player.bye if player else None
    else:
        name = p.stub_name
        pos = p.stub_pos
        team = None
        bye = None
    return {
        "pick_no": p.pick_no,
        "pick_label": None,  # filled in by caller, which knows `teams`
        "team_slot": p.team_slot,
        "player_id": p.player_id,
        "name": name,
        "pos": pos,
        "team": team,
        "bye": bye,
        "is_stub": p.player_id is None,
        "voided": p.voided,
        "out_of_order": p.out_of_order,
    }


class DraftBoard:
    """Holds everything the server needs beyond the raw event log: config, session, pool."""

    def __init__(
        self,
        *,
        cfg: LeagueConfig,
        my_slot: int,
        session: DraftSession,
        pool: list[live_data.PoolPlayer],
        active_source: str = DEFAULT_SOURCE_KEY,
    ):
        self.cfg = cfg
        self.my_slot = my_slot
        self.session = session
        # Which projection-source board is currently being served (plan B1/B2: "blend",
        # "sleeper", "espn", "fantasypros"). This is the ground truth for what's on screen.
        # The POOL cannot be reconstructed by replay (it is built from cached projections, not
        # from events), but the SELECTION is replayable and `create_app` does resume it from the
        # last `source_changed` event -- see `_last_source_from_log` for why that matters.
        self.active_source = active_source
        self._load_pool(pool)

    def _load_pool(self, pool: list[live_data.PoolPlayer]) -> None:
        """(Re)build every index derived from the pool. Shared by __init__ and switch_source
        so the two paths can never drift -- a stale index after a source switch would mean
        search and the board disagree about who exists."""
        self.pool = pool
        self.pool_by_id = live_data.index_by_id(pool)
        self.searchable = live_data.to_searchable(pool)
        self.pos_of = live_data.pos_of_map(pool)
        self.board_players = _build_board_players(pool)
        # "real" = values came from the validated board (the model the sims exercised);
        # "placeholder" = fallback ADP values, loudly surfaced -- recommendations in that mode
        # are NOT the validated model (Codex 2026-08-18).
        self.board_source = "real" if any(p.value_is_real for p in pool) else "placeholder"
        self.real_value_count = sum(1 for p in pool if p.value_is_real)
        if self.board_source == "placeholder":
            log.warning(
                "SERVING PLACEHOLDER VALUES: the validated real board was not available at "
                "startup. Bookkeeping is unaffected; recommendations are not trustworthy."
            )

    def switch_source(self, key: str, new_pool: list[live_data.PoolPlayer]) -> None:
        """Swap the served pool for a different projection source (plan B1/B2).

        Logs a `source_changed` event for the post-draft audit trail (CLAUDE.md: "the record
        must show which board a pick was made against"), then rebuilds every derived index via
        `_load_pool`. That event is NOT replayed back into DraftState -- the pool lives outside
        the event log, so there is nothing for replay to reconstruct from it.

        Order matters: the event is appended (and fsync'd) BEFORE any in-memory state moves. The
        old order mutated the pool and `active_source` first, so a failed disk write left the
        running app serving one source while replay would rebuild another (Codex 2026-08-21
        finding 5). The pool is already built and validated by the caller, so nothing between
        the append and the swap can fail.
        """
        self.session.log.append("source_changed", key=key)
        self._load_pool(new_pool)
        self.active_source = key
        self.session._refresh()

    @property
    def state(self) -> DraftState:
        return self.session.state

    # ------------------------------------------------------------- board view

    def upcoming_picks(self, n: int = 16) -> list[dict[str, Any]]:
        st = self.state
        out = []
        for offset in range(n):
            pick_no = st.current_pick + offset
            slot = snake.slot_on_clock(self.cfg.teams, pick_no)
            existing = st.picks.get(pick_no)
            out.append(
                {
                    "pick_no": pick_no,
                    "pick_label": snake.pick_label(self.cfg.teams, pick_no),
                    "team_slot": slot,
                    "team_label": st.team_label(slot),
                    "is_mine": slot == self.my_slot,
                    "is_on_clock": offset == 0,
                    "filled": bool(existing and existing.is_filled),
                }
            )
        return out

    def my_upcoming_offsets(self) -> list[int]:
        """Picks-until-mine for each of my future picks in `upcoming_picks`, e.g. [0, 7, 17]."""
        st = self.state
        return [
            pick["pick_no"] - st.current_pick for pick in self.upcoming_picks() if pick["is_mine"]
        ]

    def roster_view(self, team_slot: int) -> list[dict[str, Any]]:
        out = []
        for p in self.state.roster(team_slot):
            view = _pick_view(p, self.pool_by_id)
            view["pick_label"] = snake.pick_label(self.cfg.teams, p.pick_no)
            out.append(view)
        return out

    def starter_fill(self, team_slot: int) -> dict[str, Any]:
        """How many of each starter slot (plus flex) are filled for one team."""
        counts = self.state.roster_positions(team_slot, self.pos_of)
        starters: dict[str, dict[str, int]] = {}
        leftover: dict[str, int] = {}
        for pos, need in self.cfg.starters.items():
            have = counts.get(pos, 0)
            filled = min(have, need)
            starters[pos] = {"filled": filled, "need": need}
            leftover[pos] = have - filled
        flex_pool = sum(leftover.get(pos, 0) for pos in self.cfg.flex_eligible)
        flex_filled = min(flex_pool, self.cfg.flex_slots)
        return {
            "starters": starters,
            "flex": {"filled": flex_filled, "need": self.cfg.flex_slots},
            "bench_used": max(0, sum(counts.values()) - sum(s["filled"] for s in starters.values()) - flex_filled),
            "bench_size": self.cfg.bench,
        }

    def opponent_grid(self) -> list[dict[str, Any]]:
        """Positional counts (unchanged) PLUS, per team, the actual roster by name -- the
        biggest gap in the pre-existing payload: a player drafted by another team updated their
        position count but never appeared anywhere by name, and write-ins (not in self.pool)
        couldn't be shown by tier_board either. `roster_view` already resolves both real
        players and stub write-ins; reusing it here is what closes that gap."""
        out = []
        for slot in range(1, self.cfg.teams + 1):
            counts = self.state.roster_positions(slot, self.pos_of)
            unfilled = self.state.unfilled_starters(slot, dict(self.cfg.starters), self.pos_of)
            fill = self.starter_fill(slot)
            qb_fill = fill["starters"].get("QB", {"filled": 0, "need": 0})
            out.append(
                {
                    "team_slot": slot,
                    "team_label": self.state.team_label(slot),
                    "is_mine": slot == self.my_slot,
                    "counts": counts,
                    "qb_count": counts.get("QB", 0),
                    "qb_unfilled": unfilled.get("QB", 0),
                    "unfilled": unfilled,
                    "starter_fill": fill,
                    "qb_complete": qb_fill.get("filled", 0) >= qb_fill.get("need", 0) > 0,
                    "roster": self.roster_view(slot),
                    "open_slots_summary": _open_slots_summary(fill),
                }
            )
        return out

    def all_picks(self) -> list[dict[str, Any]]:
        """Every recorded pick, sorted by pick_no, INCLUDING voided ones (plan A3).

        This is the draft-results tab's audit trail -- hiding voided rows would defeat the
        append-only design, so `voided` picks stay in the list (struck through by the UI)
        rather than being filtered out here.
        """
        out = []
        for p in sorted(self.state.picks.values(), key=lambda x: x.pick_no):
            view = _pick_view(p, self.pool_by_id)
            view["pick_label"] = snake.pick_label(self.cfg.teams, p.pick_no)
            view["round"] = snake.round_of(self.cfg.teams, p.pick_no)
            view["team_label"] = self.state.team_label(p.team_slot)
            view["is_mine"] = p.team_slot == self.my_slot
            out.append(view)
        return out

    def demand_clock(self) -> dict[str, dict[str, Any]]:
        """Per-position supply vs. demand before Marc's own next turn.

        Tone contract (CLAUDE.md): informs, never recommends -- no position here is ranked,
        ordered, or suggested, just the numbers Marc would otherwise have to count in his head
        mid-conversation. `startable_remaining` mirrors the tier board's own ranked-only
        convention: unranked players (value 0.0, `is_ranked=False`) never count as startable
        supply, because a value of 0.0 for them is "no projection", not "worthless".
        """
        st = self.state
        ctx = st.turn_context()
        # The window is "opponent picks before Marc's own NEXT turn". When someone else is on
        # the clock, that window starts at the current pick (it IS an opponent pick) and runs to
        # his next pick. When MARC is on the clock, ctx.next_pick equals current_pick, so using
        # it produced a zero-width window ("0 opponent picks") -- Codex 2026-08-18 -- when the
        # true window is current+1 through his FOLLOWING pick (at slot 1 pick 1: the 18 opponent
        # picks before pick 20).
        if st.is_my_pick:
            window_start = st.current_pick + 1
            window_end = ctx.following_pick  # exclusive
        else:
            window_start = st.current_pick
            window_end = ctx.next_pick  # exclusive
        picks_before_next = max(0, window_end - window_start) if window_end is not None else 0
        slots_before: list[int] = []
        for pick in range(window_start, window_start + picks_before_next):
            slot = snake.slot_on_clock(self.cfg.teams, pick)
            if slot not in slots_before:
                slots_before.append(slot)

        starters = dict(self.cfg.starters)
        drafted_ids = st.drafted_player_ids
        out: dict[str, dict[str, Any]] = {}
        for pos in sorted(self.cfg.positions):
            startable_remaining = sum(
                1
                for p in self.pool
                if p.pos == pos and p.is_ranked and p.value > 0 and p.player_id not in drafted_ids
            )
            league_demand_remaining = sum(
                st.unfilled_starters(t, starters, self.pos_of).get(pos, 0)
                for t in range(1, self.cfg.teams + 1)
            )
            teams_needing_before_next_turn = sum(
                1 for slot in slots_before if pos in st.unfilled_starters(slot, starters, self.pos_of)
            )
            out[pos] = {
                "position": pos,
                "startable_remaining": startable_remaining,
                "league_demand_remaining": league_demand_remaining,
                "teams_needing_before_next_turn": teams_needing_before_next_turn,
                "picks_before_next_turn": picks_before_next,
                # Negative = more unfilled league demand than startable supply left -- the same
                # shut-out condition draftroom.draft.recommend's guardrail 2 watches per-candidate,
                # shown here at the board level for every position, not just the one being picked.
                "cushion": startable_remaining - league_demand_remaining,
            }
        return out

    def tier_board(self) -> dict[str, list[dict[str, Any]]]:
        """Every position's players in ADP order, with tier index for the undrafted ones.

        Tiers are computed on the remaining (undrafted) pool only -- "a tier is defined by
        who's left" (CLAUDE.md). Drafted players still appear (struck through in the UI, with
        their owning team) so the board reads as a full depth chart, not just what's left.
        """
        from draftroom.tiers.dynamic import largest_gap_tiers

        drafted_ids = self.state.drafted_player_ids
        owner_by_id: dict[str, int] = {}
        for p in self.state.picks.values():
            if p.is_filled and p.player_id is not None:
                owner_by_id[p.player_id] = p.team_slot

        by_pos: dict[str, list[live_data.PoolPlayer]] = {}
        for p in self.pool:
            by_pos.setdefault(p.pos, []).append(p)

        out: dict[str, list[dict[str, Any]]] = {}
        for pos, players in by_pos.items():
            # Only RANKED players get tiered. The pool also carries roster-only players with
            # no projection (value 0.0) so they can be recorded; feeding those to the tier
            # engine would bury the real tiers under one huge flat block of zeros and imply
            # we had evaluated them. They come back with tier=None instead.
            available = [
                p for p in players
                if p.player_id not in drafted_ids and getattr(p, "is_ranked", True)
            ]
            tier_of: dict[str, int] = {}
            if available:
                # largest_gap_tiers reads a `dv`/`draft_value` field (it's written against the
                # real valuation module's DraftValue); PoolPlayer's field is `value`, so wrap.
                dv_wrapped = [{"player_id": p.player_id, "dv": p.value} for p in available]
                for tier in largest_gap_tiers(dv_wrapped):
                    for member in tier.members:
                        tier_of[member["player_id"]] = tier.tier
            rows = []
            for p in sorted(players, key=lambda x: x.adp):
                drafted = p.player_id in drafted_ids
                rows.append(
                    {
                        "player_id": p.player_id,
                        "name": p.name,
                        "team": p.team,
                        "bye": p.bye,
                        "adp": p.adp,
                        "value": p.value,
                        # False = roster-only, no projection. The UI must render these as
                        # "no projection" rather than as a player valued at zero.
                        "is_ranked": getattr(p, "is_ranked", True),
                        # False on a RANKED player = the real board excluded them (unresolved
                        # crosswalk / no projection): name kept for bookkeeping, value carries
                        # no evaluation. Also False for every player in placeholder mode.
                        "value_is_real": getattr(p, "value_is_real", False),
                        "drafted": drafted,
                        "owner_team_slot": owner_by_id.get(p.player_id),
                        "owner_label": (
                            self.state.team_label(owner_by_id[p.player_id])
                            if p.player_id in owner_by_id
                            else None
                        ),
                        "tier": tier_of.get(p.player_id),
                        # Flag badges (informational only -- never alter value/tier/ranking).
                        "sigma_ppg": getattr(p, "sigma_ppg", None),
                        # Danger signal only: True means the sources disagree a lot. False/None
                        # is NOT a safety signal -- see draftroom.valuation.disagreement.
                        "disagreement_high": getattr(p, "disagreement_high", False),
                        "injury_status": getattr(p, "injury_status", None),
                        # Each source's own league-scored SEASON POINTS (not dv -- dv depends on
                        # the whole pool's replacement level, so it isn't comparable row to row).
                        # None when the active board couldn't produce a per-source breakdown;
                        # the UI must read that as "not available", never as agreement.
                        "value_by_source": getattr(p, "value_by_source", None),
                        # Rejections Marc adjudicated in the review queue that actually changed
                        # this player's number (docs/REVIEW_QUEUE.md). Rendered as a badge --
                        # a decision of his must never be silently folded into a value.
                        "projection_decisions": (
                            [dict(d) for d in (getattr(p, "projection_decisions", None) or [])]
                            or None
                        ),
                    }
                )
            out[pos] = rows
        return out

    # ------------------------------------------------------------- full payload

    def state_payload(self) -> dict[str, Any]:
        st = self.state
        ctx = st.turn_context()
        return {
            "teams": self.cfg.teams,
            "rounds": self.session.rounds,
            "my_slot": self.my_slot,
            # True when nobody told us the slot and we fell back to 1. The UI must surface this;
            # a silently-assumed slot poisons every turn-dependent number on the page.
            "slot_assumed": getattr(self, "slot_assumed", False),
            "current_pick": st.current_pick,
            "current_pick_label": snake.pick_label(self.cfg.teams, st.current_pick),
            "slot_on_clock": st.slot_on_clock,
            "is_my_pick": st.is_my_pick,
            "next_pick": ctx.next_pick,
            "gap_to_next": ctx.gap_to_next,
            "at_the_turn": ctx.at_the_turn,
            "gaps": st.gaps(),
            "upcoming_picks": self.upcoming_picks(),
            "my_roster": self.roster_view(self.my_slot),
            "my_starter_fill": self.starter_fill(self.my_slot),
            "opponents": self.opponent_grid(),
            "all_picks": self.all_picks(),
            # Only slots with a name actually set (plan A1) -- an absent slot means "no name
            # set", which the UI renders via the same team_label() precedence used everywhere
            # else, not a separate default baked in here.
            "team_names": {str(slot): name for slot, name in sorted(self.state.team_names.items())},
            # Candidate names read off the real 2026 Yahoo league page (data/league_manual.yaml,
            # plan A1) -- NOT a slot assignment. Given so the UI can offer real names to assign
            # at the table without hardcoding them a second time in the frontend.
            "team_name_candidates": list(self.cfg.team_names),
            "tier_board": self.tier_board(),
            "demand_clock": self.demand_clock(),
            "active_source": self.active_source,
            "elite_qb_rank_cutoff_default": _DEFAULT_ELITE_QB_CUTOFF,
            # Monotone event counter: bumps on every appended event (pick, stub, correct, void,
            # clock, undo). The frontend keys its recommendation refetch on this -- current_pick
            # alone missed void/correct, which change availability without moving the clock.
            "event_seq": self.session.log.last_seq,
            "board_source": self.board_source,
            "real_value_count": self.real_value_count,
            "value_note": (
                live_data.REAL_VALUE_NOTE
                if self.board_source == "real"
                else live_data.PLACEHOLDER_VALUE_NOTE
            ),
        }


# --------------------------------------------------------------------------- recommendation


def _build_board_players(pool: list[live_data.PoolPlayer]) -> list[Any] | None:
    """Convert the live pool to `recommend.BoardPlayer`, if that module is importable.

    As of 2026-08-18 the pool's `value`/`value_sd` ARE the real risk-adjusted DraftValues from
    the validated board (`live_data` joins `validate.board.build_real_board()` at load time --
    a Codex review caught draft night serving an ADP placeholder the sims never exercised).
    In fallback mode (no cached board) `value` degrades to the ADP placeholder, loudly flagged
    in the payload as `board_source: "placeholder"`. `is_ranked` passes through so the engine
    keeps roster-only write-ins in its need math without ever recommending them.
    """
    if _recommend_mod is None:
        return None
    try:
        return [
            _recommend_mod.BoardPlayer(
                player_id=p.player_id,
                name=p.name,
                pos=p.pos,
                team=p.team,
                bye=p.bye,
                adp=p.adp,
                stdev=p.stdev,
                dv=p.value,
                dv_sd=p.value_sd,
                is_ranked=p.is_ranked,
            )
            for p in pool
        ]
    except Exception as exc:  # noqa: BLE001 - defensive, see module docstring
        log.warning("could not build BoardPlayer list for recommend.recommend(): %s", exc)
        return None


def _placeholder_recommendation(board: DraftBoard, pick_no: int) -> Recommendation:
    slot = snake.slot_on_clock(board.cfg.teams, pick_no)
    return Recommendation(
        pick_no=pick_no,
        pick_label=snake.pick_label(board.cfg.teams, pick_no),
        on_the_clock=slot,
        is_my_pick=slot == board.my_slot,
        candidates=(),
        warnings=(
            "Recommendation engine (draftroom.draft.recommend) is not available yet -- "
            "showing an empty placeholder. Search and the tier board are live.",
        ),
    )


def _call_recommend_engine(
    board: DraftBoard, *, elite_qb_rank_cutoff: int = _DEFAULT_ELITE_QB_CUTOFF
) -> Recommendation | None:
    """Call the real recommendation engine if it's importable and the pool converted cleanly.

    `recommend.recommend(state, cfg, players)` always answers for whoever is on the clock
    *right now* (`state.current_pick`) -- it has no pick-number parameter, and itself returns
    an explicit "Not on the clock" placeholder when `state.is_my_pick` is False. So there is no
    real per-pick target to forward here; `target=mine` vs `target=clock` is purely a client-
    side label until it's my turn, at which point they agree. Any failure at all (missing
    module, a partially-built one, an unexpected exception mid-computation) falls back to our
    own placeholder rather than a 500 -- see module docstring.

    `elite_qb_rank_cutoff` is fix "C"(b)'s visible knob (CLAUDE.md/task spec): the UI exposes it
    as a control rather than hardcoding the spec default, so Marc can dial it to 0 (off) or wider
    live if the room's QB run looks different from what the backtest assumed.
    """
    if _recommend_mod is None or board.board_players is None:
        return None
    try:
        result = _recommend_mod.recommend(
            board.state, board.cfg, board.board_players, elite_qb_rank_cutoff=elite_qb_rank_cutoff
        )
    except Exception as exc:  # noqa: BLE001 - defensive, see module docstring
        log.warning("recommend.recommend() raised, falling back to placeholder: %s", exc)
        return None
    return result if isinstance(result, Recommendation) else None


def _recommendation_payload(
    board: DraftBoard, pick_no: int, *, elite_qb_rank_cutoff: int = _DEFAULT_ELITE_QB_CUTOFF
) -> dict[str, Any]:
    rec = _call_recommend_engine(board, elite_qb_rank_cutoff=elite_qb_rank_cutoff) or (
        _placeholder_recommendation(board, pick_no)
    )
    return dataclasses.asdict(rec)


# --------------------------------------------------------------------------- source toggle (B2)


def _sources_payload(board: DraftBoard) -> dict[str, Any]:
    """`GET /api/sources` body. Degrades to a single-entry description of the board actually
    being served when `draftroom.sources` is missing or broken -- see module docstring."""
    if _sources_mod is not None:
        try:
            return {"active": board.active_source, "sources": _sources_mod.available_sources()}
        except Exception as exc:  # noqa: BLE001 - defensive, see module docstring
            log.warning("draftroom.sources.available_sources() raised: %s", exc)
    return {
        "active": board.active_source,
        "sources": [
            {
                "key": board.active_source,
                "label": f"Current board ({board.board_source})",
                "player_count": len(board.pool),
                "note": (
                    "source-toggle module (draftroom.sources) is not available yet -- serving "
                    "the board loaded at startup, and the toggle itself is disabled"
                ),
            }
        ],
    }


def _switch_source(board: DraftBoard, key: str) -> None:
    """Validate and perform a source switch, or raise the matching HTTPException.

    Every check runs before `board.switch_source` touches anything, matching the
    validate-before-append discipline used for every other mutation in this file.
    """
    if _sources_mod is None:
        raise HTTPException(
            status_code=503,
            detail="source switching unavailable: draftroom.sources is not importable",
        )
    valid_keys = getattr(_sources_mod, "SOURCE_KEYS", ())
    if key not in valid_keys:
        raise HTTPException(
            status_code=422,
            detail=f"unknown source key {key!r} (valid: {list(valid_keys)})",
        )
    try:
        # STRICT: a pool that built but valued nothing must not become the active source. The
        # lenient accessor returns an ADP-placeholder pool, which used to be accepted, logged,
        # and then resumed on relaunch under the real source's name (Codex 2026-08-21 finding 5).
        new_pool = _sources_mod.pool_for_source_strict(key)
    except Exception as exc:  # noqa: BLE001 - defensive, see module docstring
        raise HTTPException(
            status_code=503,
            detail=f"could not build board for source {key!r}: {exc}",
        ) from exc
    board.switch_source(key, new_pool)


def _last_source_from_log(session: DraftSession) -> str | None:
    """The key of the most recent `source_changed` event, or None if the source was never
    switched.

    Why this exists: `source_changed` was being appended to the log but never read back, so a
    relaunch mid-draft (the exact scenario the append-only log is FOR) silently reverted to the
    startup board. Marc would have kept drafting against a different valuation than the one he
    chose, with only the header toggle to give it away. An event that is written and never
    replayed also breaks this repo's core rule that state is a pure function of
    (snapshot, events), which is precisely the kind of thing that rots quietly.
    """
    key: str | None = None
    for ev in session.log.events():
        if ev.type == "source_changed":
            k = ev.payload.get("key")
            if isinstance(k, str) and k:
                key = k
    return key


def _resume_pool_for_source(key: str) -> list[live_data.PoolPlayer] | None:
    """The pool for a log-resumed source, or None if it cannot be rebuilt.

    Returning None is a LOUD fallback, never a silent one: the caller warns and serves the
    default board with `active_source` left at the default, so the header never claims to be
    showing a source that failed to load.
    """
    if _sources_mod is None:
        log.warning(
            "the draft log says source %r was selected, but draftroom.sources is not "
            "importable -- serving the startup board instead. Re-select the source once the "
            "module is available; picks are unaffected.", key,
        )
        return None
    try:
        # STRICT for the same reason as the toggle: resuming a placeholder pool would put the
        # header back on the chosen source's name with none of its values behind it, which is
        # the failure that looks most like success (Codex 2026-08-21 finding 5).
        return _sources_mod.pool_for_source_strict(key)
    except Exception as exc:  # noqa: BLE001 - defensive, see module docstring
        log.warning(
            "the draft log says source %r was selected, but its board could not be rebuilt "
            "(%s) -- serving the startup board instead. Picks are unaffected.", key, exc,
        )
        return None


# --------------------------------------------------------------------------- app factory


class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast_json(self, payload: dict[str, Any]) -> None:
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:  # noqa: BLE001 - a dropped client shouldn't break the pick path
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


def create_app(
    *,
    cfg: LeagueConfig | None = None,
    my_slot: int | None = None,
    log_path: Path | str | None = None,
    pool: list[live_data.PoolPlayer] | None = None,
    static_dir: Path | None = None,
) -> FastAPI:
    """Build the FastAPI app. Kept as a factory so tests can inject an isolated event log
    and a deterministic player pool instead of touching the real draft-night files."""
    cfg = cfg or LeagueConfig.from_yaml()
    # The slot is drawn at/near draft night, so it is legitimately unknown during prep. What is NOT
    # acceptable is silently pretending it is 1: every turn-dependent number (survival to your next
    # pick, gaps, VONA, upcoming picks) would be confidently wrong with nothing on screen saying so.
    # Prep tolerates an unknown slot but must ANNOUNCE the assumption; draft night refuses outright
    # (see main()).
    slot_assumed = my_slot is None and cfg.draft_slot is None
    my_slot = my_slot if my_slot is not None else (cfg.draft_slot or 1)
    if slot_assumed:
        log.warning(
            "DRAFT SLOT UNKNOWN -- assuming slot 1. Every turn-dependent number (survival to your "
            "next pick, gaps, VONA) is provisional. Set draft_slot in data/league_manual.yaml or "
            "pass --my-slot once the draw is known."
        )
    log_path = Path(log_path) if log_path is not None else DEFAULT_LOG_PATH
    session = DraftSession(EventLog(log_path), teams=cfg.teams, rounds=cfg.roster_size, my_slot=my_slot)

    # Resume the projection source the log says was last selected. Without this a relaunch
    # mid-draft (crash, closed lid, dead battery) silently drops back to the default board --
    # see `_last_source_from_log`. An explicitly injected `pool` always wins: that is how tests
    # pin a deterministic board, and honouring the log there would make them non-hermetic.
    #
    # Ordering matters: the resume is attempted BEFORE the default pool is loaded, so a resumed
    # draft builds one board instead of building the default and discarding it.
    active_source = DEFAULT_SOURCE_KEY
    if pool is None:
        logged_key = _last_source_from_log(session)
        if logged_key is not None and logged_key != active_source:
            resumed = _resume_pool_for_source(logged_key)
            if resumed is not None:
                pool = resumed
                active_source = logged_key
                log.info("resumed projection source %r from the draft log", logged_key)
    if pool is None:
        pool = live_data.load_player_pool()

    board = DraftBoard(
        cfg=cfg, my_slot=my_slot, session=session, pool=pool, active_source=active_source
    )
    board.slot_assumed = slot_assumed
    manager = ConnectionManager()

    app = FastAPI(title="draftroom")
    app.state.board = board
    app.state.manager = manager

    async def _broadcast() -> None:
        await manager.broadcast_json(board.state_payload())

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/state")
    def api_state() -> dict[str, Any]:
        return board.state_payload()

    @app.get("/api/search")
    def api_search(q: str = "", limit: int = 8, include_drafted: bool = False) -> dict[str, Any]:
        matches = run_search(
            q,
            board.searchable,
            drafted=board.state.drafted_player_ids,
            limit=limit,
            include_drafted=include_drafted,
        )
        return {
            "query": q,
            "matches": [
                {
                    "player_id": m.player.player_id,
                    "name": m.player.name,
                    "pos": m.player.pos,
                    "team": m.player.team,
                    "overall_rank": m.player.overall_rank,
                    "score": m.score,
                    "reason": m.reason,
                    "drafted": m.player.player_id in board.state.drafted_player_ids,
                    # False = roster-only write-in target with no real projection. The UI must
                    # never imply this player was evaluated at zero.
                    "is_ranked": m.player.is_ranked,
                }
                for m in matches
            ],
        }

    # ------------------------------------------------------------------ mutation validation
    # Every check below runs BEFORE anything is appended to the event log. The log is fsync'd
    # and replayed on every request, so an invalid event isn't just a bad response -- it is
    # durable state that can crash or silently corrupt every subsequent replay (Codex
    # 2026-08-18: a clock_set of 0 broke snake arithmetic on replay; a pick on an occupied
    # pick_no silently replaced the earlier pick). Reject with a 4xx; never append.

    total_picks = cfg.teams * board.session.rounds

    def _check_pick_no_bounds(pick_no: int) -> None:
        if not 1 <= pick_no <= total_picks:
            raise HTTPException(
                status_code=422,
                detail=f"pick_no {pick_no} out of range (this draft is picks 1..{total_picks})",
            )

    def _check_team_slot(team_slot: int | None) -> None:
        if team_slot is not None and not 1 <= team_slot <= cfg.teams:
            raise HTTPException(
                status_code=422,
                detail=f"team_slot {team_slot} out of range (1..{cfg.teams})",
            )

    def _check_target_pick_free(pick_no: int | None) -> None:
        n = pick_no if pick_no is not None else board.state.current_pick
        _check_pick_no_bounds(n)
        existing = board.state.picks.get(n)
        if existing is not None and existing.is_filled:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"pick {n} is already filled -- use /api/correct to change it or "
                    f"/api/void to remove it, never a second pick event (replay would "
                    f"silently replace the first)"
                ),
            )

    @app.post("/api/pick")
    async def api_pick(req: PickRequest) -> dict[str, Any]:
        if req.player_id not in board.pool_by_id:
            raise HTTPException(
                status_code=404,
                detail=f"unknown player_id {req.player_id!r} -- use /api/stub for a write-in",
            )
        if req.player_id in board.state.drafted_player_ids:
            raise HTTPException(status_code=409, detail=f"{req.player_id} is already drafted")
        _check_team_slot(req.team_slot)
        _check_target_pick_free(req.pick_no)
        board.session.record_pick(
            req.player_id, pick_no=req.pick_no, team_slot=req.team_slot, raw_query=req.raw_query
        )
        await _broadcast()
        return board.state_payload()

    @app.post("/api/stub")
    async def api_stub(req: StubRequest) -> dict[str, Any]:
        if not req.name.strip():
            raise HTTPException(status_code=422, detail="stub name must not be empty")
        pos = req.pos.upper().strip()
        if pos not in cfg.positions:
            raise HTTPException(
                status_code=422,
                detail=f"unknown position {req.pos!r} (this league: {sorted(cfg.positions)})",
            )
        _check_team_slot(req.team_slot)
        _check_target_pick_free(req.pick_no)
        board.session.record_stub(req.name, pos, pick_no=req.pick_no, team_slot=req.team_slot)
        await _broadcast()
        return board.state_payload()

    @app.post("/api/undo")
    async def api_undo() -> dict[str, Any]:
        board.session.undo_last()
        await _broadcast()
        return board.state_payload()

    @app.post("/api/correct")
    async def api_correct(req: CorrectRequest) -> dict[str, Any]:
        _check_pick_no_bounds(req.pick_no)
        _check_team_slot(req.team_slot)
        existing = board.state.picks.get(req.pick_no)
        if existing is None:
            raise HTTPException(
                status_code=404, detail=f"no recorded pick {req.pick_no} to correct"
            )
        if req.player_id is None and req.stub_name is None:
            raise HTTPException(
                status_code=422,
                detail="a correction needs a player_id or a stub_name -- nothing to correct to",
            )
        if req.player_id is not None:
            if req.player_id not in board.pool_by_id:
                raise HTTPException(
                    status_code=404, detail=f"unknown player_id {req.player_id!r}"
                )
            if (
                req.player_id in board.state.drafted_player_ids
                and existing.player_id != req.player_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail=f"{req.player_id} is already drafted at another pick",
                )
        if req.stub_name is not None:
            pos = (req.stub_pos or "").upper().strip()
            if pos not in cfg.positions:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"a stub correction needs a valid stub_pos "
                        f"(this league: {sorted(cfg.positions)}), got {req.stub_pos!r}"
                    ),
                )
        board.session.correct_pick(
            req.pick_no,
            player_id=req.player_id,
            stub_name=req.stub_name,
            stub_pos=req.stub_pos,
            team_slot=req.team_slot,
        )
        await _broadcast()
        return board.state_payload()

    @app.post("/api/void")
    async def api_void(req: VoidRequest) -> dict[str, Any]:
        _check_pick_no_bounds(req.pick_no)
        existing = board.state.picks.get(req.pick_no)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"no recorded pick {req.pick_no} to void")
        if existing.voided:
            raise HTTPException(status_code=409, detail=f"pick {req.pick_no} is already voided")
        board.session.void_pick(req.pick_no)
        await _broadcast()
        return board.state_payload()

    @app.post("/api/reassign")
    async def api_reassign(req: ReassignRequest) -> dict[str, Any]:
        """Move a recorded pick to a different team slot, leaving the player untouched.

        Separate from /api/correct because a correction requires a player_id or stub_name, so a
        reassign-only request (which has neither) was rejected 422 and the Draft Results tab's
        "Reassign to team..." never worked at all (Codex 2026-08-21 finding 3).
        """
        _check_pick_no_bounds(req.pick_no)
        if not 1 <= req.team_slot <= cfg.teams:
            raise HTTPException(
                status_code=422,
                detail=f"team_slot {req.team_slot} out of range (1..{cfg.teams})",
            )
        existing = board.state.picks.get(req.pick_no)
        if existing is None:
            raise HTTPException(
                status_code=404, detail=f"no recorded pick {req.pick_no} to reassign"
            )
        board.session.reassign_pick(req.pick_no, req.team_slot)
        await _broadcast()
        return board.state_payload()

    @app.post("/api/undraft")
    async def api_undraft(req: VoidRequest) -> dict[str, Any]:
        """Remove a pick, rewinding the clock when the pick being removed is the newest one.

        The UI's `x` used to call /api/void for this, which left the clock advanced and pushed
        every later pick one slot out of alignment with the physical board (Codex 2026-08-21
        finding 2). One appended event either way -- never a void+clock_set pair, which a crash
        could tear in half.
        """
        _check_pick_no_bounds(req.pick_no)
        existing = board.state.picks.get(req.pick_no)
        if existing is None:
            raise HTTPException(
                status_code=404, detail=f"no recorded pick {req.pick_no} to undraft"
            )
        if existing.voided:
            raise HTTPException(
                status_code=409, detail=f"pick {req.pick_no} is already voided"
            )
        _, mode = board.session.undraft_pick(req.pick_no)
        log.info("undraft pick %d -> %s", req.pick_no, mode)
        await _broadcast()
        payload = board.state_payload()
        payload["last_undraft"] = {"pick_no": req.pick_no, "mode": mode}
        return payload

    @app.post("/api/clock")
    async def api_clock(req: ClockRequest) -> dict[str, Any]:
        # A clock_set outside 1..total_picks is durably replayed into snake arithmetic that
        # cannot serialize it (current_pick=0 crashed replay in review testing). Bounds-check
        # BEFORE appending, not after.
        _check_pick_no_bounds(req.pick_no)
        board.session.set_clock(req.pick_no)
        await _broadcast()
        return board.state_payload()

    # ------------------------------------------------------------------ team names (plan A1)

    def _check_team_name_length(name: str) -> str:
        """Returns the stripped name; raises if it's over the limit. Stripping happens BEFORE
        the length check and BEFORE the event is appended, so what lands in the log is exactly
        what the length check validated."""
        stripped = name.strip()
        if len(stripped) > 40:
            raise HTTPException(
                status_code=422,
                detail=f"team name too long (max 40 chars after trimming): {name!r}",
            )
        return stripped

    @app.post("/api/team-name")
    async def api_team_name(req: TeamNameRequest) -> dict[str, Any]:
        # req.team_slot is a required (non-Optional) field, so this also covers "missing".
        _check_team_slot(req.team_slot)
        name = _check_team_name_length(req.name)
        board.session.set_team_name(req.team_slot, name)
        await _broadcast()
        return board.state_payload()

    @app.post("/api/team-names")
    async def api_team_names(req: TeamNamesRequest) -> dict[str, Any]:
        # Validate every entry BEFORE appending any event, so a single bad slot in a bulk
        # request never leaves a partial write behind.
        parsed: list[tuple[int, str]] = []
        for slot_key, name in req.names.items():
            try:
                slot = int(slot_key)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=422,
                    detail=f"invalid team_slot key {slot_key!r} (must be an integer)",
                )
            if not 1 <= slot <= cfg.teams:
                raise HTTPException(
                    status_code=422,
                    detail=f"team_slot {slot} out of range (1..{cfg.teams})",
                )
            parsed.append((slot, _check_team_name_length(name)))
        for slot, name in sorted(parsed):
            board.session.set_team_name(slot, name)
        await _broadcast()
        return board.state_payload()

    # ------------------------------------------------------------------ source toggle (plan B2)

    @app.get("/api/sources")
    def api_sources() -> dict[str, Any]:
        return _sources_payload(board)

    @app.post("/api/source")
    async def api_source(req: SourceRequest) -> dict[str, Any]:
        _switch_source(board, req.key)
        await _broadcast()
        return board.state_payload()

    @app.get("/api/recommendation")
    def api_recommendation(
        target: str = "clock", elite_qb_rank_cutoff: int = _DEFAULT_ELITE_QB_CUTOFF
    ) -> dict[str, Any]:
        if target == "mine":
            ctx = board.state.turn_context()
            pick_no = ctx.next_pick if ctx.next_pick is not None else board.state.current_pick
        else:
            pick_no = board.state.current_pick
        return _recommendation_payload(board, pick_no, elite_qb_rank_cutoff=elite_qb_rank_cutoff)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await manager.connect(ws)
        try:
            await ws.send_text(json.dumps(board.state_payload()))
            while True:
                # The UI never needs to send anything over this socket; it exists purely to
                # push state after every mutation (a second "projected board" window). Just
                # keep the connection alive until the client disconnects.
                await ws.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(ws)

    static_dir = static_dir or (Path(__file__).resolve().parent / "static")
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


# --------------------------------------------------------------------------- CLI


def _announce_existing_draft(board: DraftBoard) -> None:
    """Say loudly, at startup, that the event log already contains picks.

    A non-empty log is LEGITIMATE -- it is exactly what a crash-recovery relaunch looks like, and
    replaying it is the whole point of the append-only design -- so this never refuses to start.
    But it is also what a stale log looks like, and the two are indistinguishable from the outside.

    This exists because it actually happened: on 2026-08-20 the live log still held four
    smoke-test picks from two days earlier. Launching on that would have opened the draft at pick
    5 with Josh Allen, Jaxson Dart, Sam Darnold and C.J. Stroud already off the board, and the
    only clue on screen would have been a board that looked subtly wrong in a room full of people.
    Silence was the bug; a loud, specific summary is the fix.
    """
    st = board.state
    filled = [p for p in sorted(st.picks.values(), key=lambda x: x.pick_no) if p.is_filled]
    if not filled:
        log.info("Draft log is empty -- starting a fresh draft at pick 1.")
        return

    recent = ", ".join(
        f"{p.pick_no}:{(board.pool_by_id[p.player_id].name if p.player_id in board.pool_by_id else p.player_id) if p.player_id else p.stub_name}"
        for p in filled[-4:]
    )
    log.warning(
        "\n"
        "  ============================================================\n"
        "  RESUMING AN EXISTING DRAFT -- the log already has %d pick(s).\n"
        "  Clock opens at pick %s. Most recent: %s\n"
        "\n"
        "  If this IS your draft in progress, carry on -- this is crash recovery working.\n"
        "  If you expected a FRESH board, stop now and archive the log:\n"
        "      move %s %s.archived\n"
        "  Deleting picks is never necessary; the log is append-only by design.\n"
        "  ============================================================",
        len(filled), st.current_pick, recent or "(none)", board.session.log.path,
        board.session.log.path,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m draftroom.server")
    parser.add_argument("--draft", action="store_true", help="Run in offline draft-night mode.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--my-slot", type=int, default=None, help="Override the draft slot.")
    parser.add_argument("--log-path", type=str, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    cfg = LeagueConfig.from_yaml()

    # Checked BEFORE the socket guard is installed, so a refusal exits with the process in a clean
    # state rather than leaving a patched socket module behind.
    #
    # Draft night is the one mode where an unknown slot is unacceptable: the board becomes the only
    # record of the draft, and every turn calculation depends on where we sit in the snake. Refuse to
    # start rather than run a whole draft off a silently-assumed slot 1.
    if args.draft and args.my_slot is None and cfg.draft_slot is None:
        log.error(
            "REFUSING TO START DRAFT MODE: draft slot unknown.\n"
            "  The slot is drawn at draft night, so this is expected until then -- but every turn "
            "number depends on it.\n"
            "  Fix either way:\n"
            "    python -m draftroom.server --draft --port 8484 --my-slot 7\n"
            "  or set  draft_slot: 7  in data/league_manual.yaml"
        )
        return 2

    if args.draft:
        install_socket_guard()
        assert_socket_guard_blocks_external()
        log.info("Socket guard installed and verified: outbound non-localhost connections blocked.")

    app = create_app(
        cfg=cfg,
        my_slot=args.my_slot,
        log_path=Path(args.log_path) if args.log_path else None,
    )
    _announce_existing_draft(app.state.board)

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
