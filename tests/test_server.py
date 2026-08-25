"""Tests for the live draft server.

Uses a small, deterministic fixture pool rather than the cached FFC payload so these tests
don't depend on whatever happens to be in data/raw/ffc/ when they run -- the search-ranking
and pick-mechanics behavior under test is about the server's wiring, not about real player
data (that's covered by tests/test_search.py and tests/test_data_layer.py).
"""

from __future__ import annotations

import dataclasses
import socket

import pytest
from fastapi.testclient import TestClient

from draftroom.config import LeagueConfig
from draftroom.live_data import PoolPlayer
from draftroom.server import OfflineViolation, create_app, install_socket_guard, uninstall_socket_guard

TEAMS = 4


def _cfg() -> LeagueConfig:
    return LeagueConfig(
        teams=TEAMS,
        starters={"QB": 1, "RB": 1, "WR": 1},
        flex_slots=0,
        flex_eligible=frozenset(),
        bench=1,
        weeks=17,
        scoring={},
    )


def _pool() -> list[PoolPlayer]:
    return [
        PoolPlayer("qb1", "Josh Allen", "QB", "BUF", 7, 1.0, 0.7, 1, 200.0),
        PoolPlayer("rb1", "Jahmyr Gibbs", "RB", "DET", 5, 2.0, 1.0, 2, 199.0),
        PoolPlayer("wr1", "Ja'Marr Chase", "WR", "CIN", 9, 3.0, 1.2, 3, 198.0),
        PoolPlayer("wr2", "Justin Jefferson", "WR", "MIN", 6, 20.0, 2.0, 20, 180.0),
        PoolPlayer("jsn", "Jaxon Smith-Njigba", "WR", "SEA", 8, 22.0, 2.0, 22, 178.0),
        PoolPlayer("qb2", "Drake Maye", "QB", "NE", 10, 25.0, 3.0, 25, 175.0),
        PoolPlayer("abrown", "A.J. Brown", "WR", "PHI", 11, 30.0, 3.0, 30, 170.0),
        PoolPlayer("hbrown", "Hollywood Brown", "WR", "KC", 12, 90.0, 4.0, 90, 120.0),
        # A roster-only write-in target: no ADP, value 0.0 (NOT an evaluation), never recommended.
        PoolPlayer(
            player_id="wrU",
            name="Uncle Rookie",
            pos="WR",
            team="NYJ",
            bye=None,
            adp=999.0,
            stdev=50.0,
            overall_rank=900,
            value=0.0,
            is_ranked=False,
            injury_status="Questionable",
        ),
    ]


def _client(tmp_path, my_slot: int = 1) -> TestClient:
    app = create_app(cfg=_cfg(), my_slot=my_slot, log_path=tmp_path / "draft.jsonl", pool=_pool())
    return TestClient(app)


# ------------------------------------------------------------------ unknown draft slot
#
# The slot is drawn at/near draft night, so it is legitimately unknown for most of prep. The
# danger is the silent fallback: without these guards the board computes every turn-dependent
# number as if we pick 1st and says nothing about it.


def test_unknown_slot_is_flagged_as_assumed_not_silently_treated_as_slot_one(tmp_path):
    """Prep tolerates an unknown slot, but the payload must admit the assumption."""
    app = create_app(cfg=_cfg(), my_slot=None, log_path=tmp_path / "draft.jsonl", pool=_pool())
    payload = TestClient(app).get("/api/state").json()

    assert payload["my_slot"] == 1, "falls back to 1 so prep stays usable"
    assert payload["slot_assumed"] is True, (
        "an unknown slot MUST be advertised to the UI; a silently-assumed slot 1 makes every "
        "survival/gap/VONA number on the page wrong with nothing saying so"
    )


def test_known_slot_is_not_flagged_as_assumed(tmp_path):
    payload = _client(tmp_path, my_slot=3).get("/api/state").json()
    assert payload["my_slot"] == 3
    assert payload["slot_assumed"] is False


def test_draft_mode_refuses_to_start_when_the_slot_is_unknown(monkeypatch):
    """Draft night is the one mode where assuming the slot is unacceptable, so it must refuse.

    Also asserts the refusal happens BEFORE the socket guard is installed -- an early return that
    left the socket module patched would poison the rest of the process.
    """
    from draftroom import server as server_mod

    monkeypatch.setattr(server_mod.LeagueConfig, "from_yaml", classmethod(lambda cls, *a, **k: _cfg()))

    installed: list[str] = []
    monkeypatch.setattr(server_mod, "install_socket_guard", lambda: installed.append("guard"))

    rc = server_mod.main(["--draft", "--port", "8499"])

    assert rc == 2, "draft mode with no slot must exit non-zero rather than draft off a guess"
    assert installed == [], "must refuse before installing the socket guard, not after"


def test_draft_mode_starts_when_the_slot_is_supplied(monkeypatch, tmp_path):
    """The refusal must be specific to a missing slot, not a blanket block on draft mode.

    NOTE: ``main()`` does ``import uvicorn`` at call time, so the patch has to land on the real
    uvicorn module (same object via sys.modules), not on a server-module attribute.
    """
    import uvicorn

    from draftroom import server as server_mod

    monkeypatch.setattr(server_mod.LeagueConfig, "from_yaml", classmethod(lambda cls, *a, **k: _cfg()))
    monkeypatch.setattr(server_mod, "install_socket_guard", lambda: None)
    monkeypatch.setattr(server_mod, "assert_socket_guard_blocks_external", lambda: None)
    monkeypatch.setattr(server_mod.live_data, "load_player_pool", lambda: _pool())

    served: list[tuple] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: served.append((a, k)))

    rc = server_mod.main(
        ["--draft", "--port", "8499", "--my-slot", "4", "--log-path", str(tmp_path / "d.jsonl")]
    )
    assert rc == 0
    assert len(served) == 1, "should have reached the serve call rather than bailing out"


# --------------------------------------------------------------------------- search


def test_search_ranks_by_value_among_available(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/api/search", params={"q": "brown"})
    assert resp.status_code == 200
    matches = resp.json()["matches"]
    ids = [m["player_id"] for m in matches]
    # Two Browns match the last-name prefix equally; the better-value one (lower ADP /
    # rank 30 vs rank 90) must lead -- this is the server wiring the pool's overall_rank
    # through to draftroom.draft.search, not string-match order.
    assert ids[0] == "abrown"
    assert ids.index("abrown") < ids.index("hbrown")


def test_search_excludes_drafted_players(tmp_path):
    client = _client(tmp_path)
    client.post("/api/pick", json={"player_id": "qb1"})
    resp = client.get("/api/search", params={"q": "josh"})
    ids = [m["player_id"] for m in resp.json()["matches"]]
    assert "qb1" not in ids


# --------------------------------------------------------------------------- pick / advance


def test_pick_assigns_to_team_on_clock_and_advances(tmp_path):
    client = _client(tmp_path, my_slot=1)
    state = client.get("/api/state").json()
    assert state["slot_on_clock"] == 1
    assert state["current_pick"] == 1

    resp = client.post("/api/pick", json={"player_id": "qb1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["current_pick"] == 2
    assert body["slot_on_clock"] == 2

    my_roster = body["my_roster"]
    assert len(my_roster) == 1
    assert my_roster[0]["player_id"] == "qb1"
    assert my_roster[0]["team_slot"] == 1


def test_picking_an_already_drafted_player_is_rejected(tmp_path):
    client = _client(tmp_path)
    client.post("/api/pick", json={"player_id": "qb1"})
    resp = client.post("/api/pick", json={"player_id": "qb1"})
    assert resp.status_code == 409


# --------------------------------------------------------------------------- undo


def test_undo_restores_previous_state(tmp_path):
    client = _client(tmp_path)
    client.post("/api/pick", json={"player_id": "qb1"})
    client.post("/api/pick", json={"player_id": "rb1"})
    resp = client.post("/api/undo")
    body = resp.json()
    assert body["current_pick"] == 2
    # rb1 goes back on the board.
    search_resp = client.get("/api/search", params={"q": "gibbs"})
    assert search_resp.json()["matches"][0]["player_id"] == "rb1"


# --------------------------------------------------------------------------- stub


def test_stub_creates_a_positioned_placeholder(tmp_path):
    client = _client(tmp_path, my_slot=1)
    resp = client.post("/api/stub", json={"name": "Some Rookie", "pos": "rb"})
    body = resp.json()
    my_roster = body["my_roster"]
    assert len(my_roster) == 1
    assert my_roster[0]["is_stub"] is True
    assert my_roster[0]["name"] == "Some Rookie"
    assert my_roster[0]["pos"] == "RB"


# --------------------------------------------------------------------------- correct


def test_correcting_a_past_pick_frees_the_wrong_player(tmp_path):
    client = _client(tmp_path)
    client.post("/api/pick", json={"player_id": "qb1"})
    client.post("/api/pick", json={"player_id": "rb1"})
    client.post("/api/pick", json={"player_id": "wr1"})

    resp = client.post("/api/correct", json={"pick_no": 2, "player_id": "wr2"})
    assert resp.status_code == 200

    # Gibbs (rb1) is back on the board -- freed by the correction.
    search_resp = client.get("/api/search", params={"q": "gibbs"})
    ids = [m["player_id"] for m in search_resp.json()["matches"]]
    assert "rb1" in ids

    # Jefferson (wr2) is now the one drafted at pick 2, so he's excluded by default.
    search_resp2 = client.get("/api/search", params={"q": "jefferson"})
    assert search_resp2.json()["matches"] == []


# --------------------------------------------------------------------------- state / board shape


def test_state_endpoint_reports_roster_and_gaps(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/api/state")
    body = resp.json()
    assert body["teams"] == TEAMS
    assert body["gaps"] == []
    assert len(body["opponents"]) == TEAMS
    assert "QB" in body["tier_board"]


def test_recommendation_endpoint_returns_a_safe_placeholder_when_engine_missing(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/api/recommendation")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pick_no"] == 1
    assert isinstance(body["candidates"], list)
    # Either the real engine answered (candidates present or not) or the explicit placeholder
    # warning is there -- either way this must never 500.
    assert isinstance(body["warnings"], list)


# --------------------------------------------------------------------------- socket guard


def test_socket_guard_blocks_a_non_localhost_connection():
    install_socket_guard()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(OfflineViolation):
                s.connect(("8.8.8.8", 53))
        finally:
            s.close()
    finally:
        uninstall_socket_guard()


def test_socket_guard_still_allows_localhost():
    install_socket_guard()
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            c.connect(("127.0.0.1", port))
            c.close()
        finally:
            srv.close()
    finally:
        uninstall_socket_guard()


# --------------------------------------------------------------------------- opponent rosters


def test_opponent_grid_carries_roster_names_and_write_ins(tmp_path):
    """The gap this closes: a player drafted by another team used to update only their position
    count, never their name; write-ins (not in board.pool at all) couldn't be shown either."""
    client = _client(tmp_path, my_slot=1)
    # Pick 1 -> slot 1 (me), pick 2 -> slot 2, pick 3 -> slot 3 (write-in stub).
    client.post("/api/pick", json={"player_id": "qb1"})
    client.post("/api/pick", json={"player_id": "rb1"})
    resp = client.post("/api/stub", json={"name": "Some Deep Sleeper", "pos": "wr"})
    body = resp.json()

    team2 = next(o for o in body["opponents"] if o["team_slot"] == 2)
    assert [r["name"] for r in team2["roster"]] == ["Jahmyr Gibbs"]
    assert team2["roster"][0]["pos"] == "RB"

    team3 = next(o for o in body["opponents"] if o["team_slot"] == 3)
    assert team3["roster"][0]["is_stub"] is True
    assert team3["roster"][0]["name"] == "Some Deep Sleeper"
    assert team3["roster"][0]["pos"] == "WR"


def test_opponent_grid_open_slots_summary_and_qb_complete_badge(tmp_path):
    client = _client(tmp_path, my_slot=1)
    body = client.get("/api/state").json()
    team1 = next(o for o in body["opponents"] if o["team_slot"] == 1)
    # Nobody has drafted anything yet: every starter slot in this config (QB/RB/WR, 1 each) is open.
    assert team1["qb_complete"] is False
    assert "needs 1 QB" in team1["open_slots_summary"]

    resp = client.post("/api/pick", json={"player_id": "qb1"})  # slot 1's QB slot
    body2 = resp.json()
    team1_after = next(o for o in body2["opponents"] if o["team_slot"] == 1)
    assert team1_after["qb_complete"] is True
    assert "QB done" in team1_after["open_slots_summary"]


# --------------------------------------------------------------------------- unranked players


def test_unranked_player_carries_is_ranked_false_through_tier_board_and_search(tmp_path):
    client = _client(tmp_path)
    body = client.get("/api/state").json()
    wr_rows = {r["player_id"]: r for r in body["tier_board"]["WR"]}
    assert "wrU" in wr_rows
    assert wr_rows["wrU"]["is_ranked"] is False
    assert wr_rows["wrU"]["value"] == 0.0
    assert wr_rows["wrU"]["tier"] is None
    assert wr_rows["wrU"]["injury_status"] == "Questionable"

    resp = client.get("/api/search", params={"q": "uncle"})
    matches = resp.json()["matches"]
    assert matches and matches[0]["player_id"] == "wrU"
    assert matches[0]["is_ranked"] is False


def test_unranked_player_excluded_from_demand_clock_startable_supply(tmp_path):
    client = _client(tmp_path)
    body = client.get("/api/state").json()
    # "Uncle Rookie" (value 0.0, unranked) must never count as startable WR supply.
    ranked_wr_count = sum(1 for r in body["tier_board"]["WR"] if r["is_ranked"])
    assert body["demand_clock"]["WR"]["startable_remaining"] == ranked_wr_count


# --------------------------------------------------------------------------- demand clock


def test_demand_clock_fields_present_and_arithmetically_consistent(tmp_path):
    client = _client(tmp_path, my_slot=1)
    body = client.get("/api/state").json()
    clock = body["demand_clock"]
    # This fixture league (see _cfg) only starts QB/RB/WR -- no TE slot, so TE carries no
    # lineup demand and is correctly absent from cfg.positions.
    assert set(clock.keys()) == {"QB", "RB", "WR"}
    for pos, entry in clock.items():
        assert entry["position"] == pos
        assert entry["startable_remaining"] >= 0
        assert entry["league_demand_remaining"] >= 0
        assert 0 <= entry["teams_needing_before_next_turn"] <= TEAMS - 1
        assert entry["cushion"] == entry["startable_remaining"] - entry["league_demand_remaining"]
    # It's slot 1's own turn right now, so the window runs from the pick AFTER this one to
    # slot 1's FOLLOWING pick (snake: picks 1 and 8 in this 4-team fixture) -- six opponent
    # picks, three distinct opponents, all of whom still need a QB. The old payload reported
    # zero here (it measured to next_pick, which equals current_pick on your own turn), which
    # made the panel blind exactly when Marc is deciding (Codex 2026-08-18).
    assert clock["QB"]["picks_before_next_turn"] == 6
    assert clock["QB"]["teams_needing_before_next_turn"] == 3


def test_demand_clock_league_demand_matches_unfilled_starters_across_teams(tmp_path):
    client = _client(tmp_path, my_slot=1)
    # 4 teams, 1 QB starter each, nobody drafted yet -> 4 unfilled QB slots league-wide.
    body = client.get("/api/state").json()
    assert body["demand_clock"]["QB"]["league_demand_remaining"] == TEAMS

    client.post("/api/pick", json={"player_id": "qb1"})
    body2 = client.get("/api/state").json()
    assert body2["demand_clock"]["QB"]["league_demand_remaining"] == TEAMS - 1


# --------------------------------------------------------------------------- elite QB knob


def test_recommendation_endpoint_accepts_elite_qb_rank_cutoff_param(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/api/recommendation", params={"elite_qb_rank_cutoff": 0})
    assert resp.status_code == 200
    resp2 = client.get("/api/recommendation", params={"elite_qb_rank_cutoff": 5})
    assert resp2.status_code == 200


def test_state_payload_exposes_elite_qb_rank_cutoff_default(tmp_path):
    client = _client(tmp_path)
    body = client.get("/api/state").json()
    assert body["elite_qb_rank_cutoff_default"] == 3


# --------------------------------------------------------------------------- void / clock


def test_void_marks_a_pick_voided_without_touching_others(tmp_path):
    client = _client(tmp_path, my_slot=1)
    client.post("/api/pick", json={"player_id": "qb1"})
    client.post("/api/pick", json={"player_id": "rb1"})
    client.post("/api/pick", json={"player_id": "wr1"})

    resp = client.post("/api/void", json={"pick_no": 2})
    assert resp.status_code == 200
    # rb1 (voided pick 2) is back on the board...
    search_resp = client.get("/api/search", params={"q": "gibbs"})
    ids = [m["player_id"] for m in search_resp.json()["matches"]]
    assert "rb1" in ids
    # ...but pick 3 (wr1, Ja'Marr Chase) is untouched -- still drafted, still excluded by default.
    search_resp2 = client.get("/api/search", params={"q": "chase"})
    ids2 = [m["player_id"] for m in search_resp2.json()["matches"]]
    assert "wr1" not in ids2


def test_clock_endpoint_jumps_current_pick(tmp_path):
    client = _client(tmp_path, my_slot=1)
    # In range for this fixture (4 teams x 4 roster spots = picks 1..16). 50 -- the old test
    # value -- is now correctly REJECTED as out of range, covered separately below.
    resp = client.post("/api/clock", json={"pick_no": 10})
    assert resp.status_code == 200
    assert resp.json()["current_pick"] == 10


# ------------------------------------------------------- mutation validation (Codex 2026-08-18)
#
# Every rejection below must ALSO leave the event log byte-identical: the log is fsync'd and
# replayed on every request, so an invalid event isn't a bad response, it is durable corruption
# (a clock_set of 0 crashed replay; a pick on an occupied pick_no silently replaced the first).


def _log_bytes(tmp_path) -> bytes:
    p = tmp_path / "draft.jsonl"
    return p.read_bytes() if p.exists() else b""


def test_clock_rejects_out_of_range_and_never_appends(tmp_path):
    client = _client(tmp_path, my_slot=1)
    client.post("/api/pick", json={"player_id": "qb1"})  # one real event so the log is non-empty
    before = _log_bytes(tmp_path)
    for bad in (0, -3, 999):
        resp = client.post("/api/clock", json={"pick_no": bad})
        assert resp.status_code == 422, f"clock_set {bad} must be rejected"
    assert _log_bytes(tmp_path) == before, "a rejected clock_set must never reach the log"


def test_pick_rejects_unknown_player_and_never_appends(tmp_path):
    client = _client(tmp_path, my_slot=1)
    before = _log_bytes(tmp_path)
    resp = client.post("/api/pick", json={"player_id": "nobody-by-this-id"})
    assert resp.status_code == 404
    assert _log_bytes(tmp_path) == before


def test_pick_rejects_occupied_pick_no_and_never_appends(tmp_path):
    client = _client(tmp_path, my_slot=1)
    client.post("/api/pick", json={"player_id": "qb1"})  # fills pick 1
    before = _log_bytes(tmp_path)
    resp = client.post("/api/pick", json={"player_id": "rb1", "pick_no": 1})
    assert resp.status_code == 409, "a second pick event on pick 1 would silently replace the first on replay"
    assert _log_bytes(tmp_path) == before


def test_pick_rejects_out_of_range_team_slot(tmp_path):
    client = _client(tmp_path, my_slot=1)
    before = _log_bytes(tmp_path)
    resp = client.post("/api/pick", json={"player_id": "qb1", "team_slot": 99})
    assert resp.status_code == 422
    assert _log_bytes(tmp_path) == before


def test_correct_rejects_unknown_pick_unknown_player_and_double_draft(tmp_path):
    client = _client(tmp_path, my_slot=1)
    client.post("/api/pick", json={"player_id": "qb1"})  # pick 1
    client.post("/api/pick", json={"player_id": "rb1"})  # pick 2
    before = _log_bytes(tmp_path)

    assert client.post("/api/correct", json={"pick_no": 9}).status_code == 404  # nothing recorded there
    assert client.post("/api/correct", json={"pick_no": 1}).status_code == 422  # nothing to correct to
    assert (
        client.post("/api/correct", json={"pick_no": 1, "player_id": "ghost"}).status_code == 404
    )
    # rb1 is already drafted at pick 2 -- correcting pick 1 to him would double-roster him.
    assert (
        client.post("/api/correct", json={"pick_no": 1, "player_id": "rb1"}).status_code == 409
    )
    # a stub correction without a valid position is unusable by the need math
    assert (
        client.post(
            "/api/correct", json={"pick_no": 1, "stub_name": "Somebody", "stub_pos": "K"}
        ).status_code
        == 422
    )
    assert _log_bytes(tmp_path) == before, "no rejected correction may reach the log"

    # ...and correcting pick 1 to himself (a no-op re-assert) is legitimate and accepted.
    assert client.post("/api/correct", json={"pick_no": 1, "player_id": "qb1"}).status_code == 200


def test_void_rejects_unknown_pick_and_double_void(tmp_path):
    client = _client(tmp_path, my_slot=1)
    client.post("/api/pick", json={"player_id": "qb1"})
    assert client.post("/api/void", json={"pick_no": 7}).status_code == 404
    client.post("/api/void", json={"pick_no": 1})
    before = _log_bytes(tmp_path)
    assert client.post("/api/void", json={"pick_no": 1}).status_code == 409
    assert _log_bytes(tmp_path) == before


def test_stub_rejects_bad_position_and_blank_name(tmp_path):
    client = _client(tmp_path, my_slot=1)
    before = _log_bytes(tmp_path)
    assert client.post("/api/stub", json={"name": "Some Kicker", "pos": "K"}).status_code == 422
    assert client.post("/api/stub", json={"name": "   ", "pos": "WR"}).status_code == 422
    assert _log_bytes(tmp_path) == before


# ------------------------------------------------------------- event_seq + board_source flags


def test_event_seq_bumps_on_every_mutation_including_void(tmp_path):
    """The UI keys recommendation refetches on event_seq because current_pick alone missed
    void/correct (they change availability without moving the clock -- Codex 2026-08-18)."""
    client = _client(tmp_path, my_slot=1)
    s0 = client.get("/api/state").json()["event_seq"]
    s1 = client.post("/api/pick", json={"player_id": "qb1"}).json()["event_seq"]
    assert s1 > s0
    s2 = client.post("/api/void", json={"pick_no": 1}).json()["event_seq"]
    assert s2 > s1, "void must bump event_seq even though current_pick does not move"


def test_board_source_flag_is_placeholder_for_the_synthetic_test_pool(tmp_path):
    """The fixture pool carries no real-board values, so the payload must ADMIT it is serving
    placeholders rather than presenting them as the validated model."""
    body = _client(tmp_path).get("/api/state").json()
    assert body["board_source"] == "placeholder"
    assert body["real_value_count"] == 0
    assert "placeholder" in body["value_note"].lower() or "FALLBACK" in body["value_note"]


# --------------------------------------------------------------------------- team names (A1)


def test_team_name_endpoint_sets_and_clears(tmp_path):
    client = _client(tmp_path, my_slot=1)
    resp = client.post("/api/team-name", json={"team_slot": 2, "name": "Jaxson Fart"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["team_names"] == {"2": "Jaxson Fart"}
    # It shows up name-aware everywhere a team_label is rendered.
    upcoming2 = next(p for p in body["upcoming_picks"] if p["team_slot"] == 2)
    assert upcoming2["team_label"] == "Jaxson Fart"
    opp2 = next(o for o in body["opponents"] if o["team_slot"] == 2)
    assert opp2["team_label"] == "Jaxson Fart"

    # An empty string clears it back to "Team N".
    resp2 = client.post("/api/team-name", json={"team_slot": 2, "name": ""})
    body2 = resp2.json()
    assert body2["team_names"] == {}
    upcoming2b = next(p for p in body2["upcoming_picks"] if p["team_slot"] == 2)
    assert upcoming2b["team_label"] == "Team 2"


def test_team_name_on_his_own_slot_keeps_the_YOU_marker(tmp_path):
    """Ledger #4: naming his own team must not erase which seat is his.

    Was `== "Country Club Boys"`. Changed deliberately -- see
    `test_team_label_precedence_and_his_own_slot_is_ALWAYS_marked` for why.
    """
    client = _client(tmp_path, my_slot=1)
    before = client.get("/api/state").json()
    assert next(p for p in before["upcoming_picks"] if p["team_slot"] == 1)["team_label"] == "YOU"

    resp = client.post("/api/team-name", json={"team_slot": 1, "name": "Country Club Boys"})
    body = resp.json()
    assert next(p for p in body["upcoming_picks"] if p["team_slot"] == 1)["team_label"] == (
        "Country Club Boys (YOU)"
    )


def test_team_name_endpoint_validates_before_appending(tmp_path):
    client = _client(tmp_path, my_slot=1)
    before = _log_bytes(tmp_path)
    resp = client.post("/api/team-name", json={"team_slot": 99, "name": "X"})
    assert resp.status_code == 422
    resp2 = client.post("/api/team-name", json={"team_slot": 1, "name": "x" * 41})
    assert resp2.status_code == 422
    assert _log_bytes(tmp_path) == before


def test_team_names_bulk_endpoint_appends_one_event_per_slot(tmp_path):
    client = _client(tmp_path, my_slot=1)
    resp = client.post(
        "/api/team-names",
        json={"names": {"1": "Country Club Boys", "2": "Jaxson Fart", "3": "Tee Swift"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["team_names"] == {
        "1": "Country Club Boys",
        "2": "Jaxson Fart",
        "3": "Tee Swift",
    }
    # One event per slot -- three lines added to the log.
    lines = _log_bytes(tmp_path).decode("utf-8").strip().splitlines()
    assert sum(1 for ln in lines if '"team_named"' in ln) == 3


def test_team_names_bulk_endpoint_validates_all_before_appending_any(tmp_path):
    client = _client(tmp_path, my_slot=1)
    before = _log_bytes(tmp_path)
    resp = client.post(
        "/api/team-names",
        json={"names": {"1": "Country Club Boys", "99": "Out Of Range"}},
    )
    assert resp.status_code == 422
    assert _log_bytes(tmp_path) == before, "no slot should be named if any entry is invalid"


def test_team_name_candidates_seeded_from_league_manual_yaml(tmp_path):
    """The real 2026 names (plan A1) are available to the UI without hardcoding a second copy
    of them in the frontend -- read straight off LeagueConfig."""
    client = _client(tmp_path, my_slot=1)
    body = client.get("/api/state").json()
    # This fixture's cfg is built in-test (not from league_manual.yaml) and carries no names,
    # so the field must simply exist and be a list -- the real-yaml content is covered by a
    # config-layer test, not this server-fixture one.
    assert isinstance(body["team_name_candidates"], list)


# --------------------------------------------------------------------------- all_picks (A3)


def test_all_picks_includes_voided_picks(tmp_path):
    client = _client(tmp_path, my_slot=1)
    client.post("/api/pick", json={"player_id": "qb1"})
    client.post("/api/pick", json={"player_id": "rb1"})
    resp = client.post("/api/void", json={"pick_no": 2})
    body = resp.json()

    all_picks = body["all_picks"]
    assert [p["pick_no"] for p in all_picks] == [1, 2]
    pick2 = next(p for p in all_picks if p["pick_no"] == 2)
    assert pick2["voided"] is True, "voided picks stay in the audit trail, not hidden"
    assert pick2["player_id"] == "rb1"
    assert pick2["pick_label"] == "1.02"
    assert pick2["round"] == 1
    assert pick2["team_label"] == "Team 2"
    assert pick2["is_mine"] is False

    pick1 = next(p for p in all_picks if p["pick_no"] == 1)
    assert pick1["is_mine"] is True
    assert pick1["team_label"] == "YOU"
    assert pick1["bye"] == 7  # qb1's bye from the fixture pool


def test_all_picks_is_name_aware(tmp_path):
    client = _client(tmp_path, my_slot=1)
    client.post("/api/team-name", json={"team_slot": 1, "name": "Country Club Boys"})
    resp = client.post("/api/pick", json={"player_id": "qb1"})
    body = resp.json()
    pick1 = next(p for p in body["all_picks"] if p["pick_no"] == 1)
    # my_slot=1 here, so his own seat carries the marker too (ledger #4).
    assert pick1["team_label"] == "Country Club Boys (YOU)"


# --------------------------------------------------------------------------- reassign to team


def test_correct_reassigns_pick_to_a_different_team(tmp_path):
    client = _client(tmp_path, my_slot=1)
    client.post("/api/pick", json={"player_id": "qb1"})  # pick 1 -> slot 1

    resp = client.post(
        "/api/correct", json={"pick_no": 1, "player_id": "qb1", "team_slot": 3}
    )
    assert resp.status_code == 200
    body = resp.json()
    pick1 = next(p for p in body["all_picks"] if p["pick_no"] == 1)
    assert pick1["team_slot"] == 3
    assert pick1["team_label"] == "Team 3"
    # No longer on slot 1's roster...
    assert body["my_roster"] == []
    # ...and now on slot 3's.
    team3 = next(o for o in body["opponents"] if o["team_slot"] == 3)
    assert [r["player_id"] for r in team3["roster"]] == ["qb1"]


def test_correct_without_team_slot_leaves_ownership_unchanged(tmp_path):
    client = _client(tmp_path, my_slot=1)
    client.post("/api/pick", json={"player_id": "qb1"})
    resp = client.post("/api/correct", json={"pick_no": 1, "player_id": "qb1"})
    body = resp.json()
    pick1 = next(p for p in body["all_picks"] if p["pick_no"] == 1)
    assert pick1["team_slot"] == 1


def test_correct_reassign_rejects_out_of_range_team_slot_and_never_appends(tmp_path):
    client = _client(tmp_path, my_slot=1)
    client.post("/api/pick", json={"player_id": "qb1"})
    before = _log_bytes(tmp_path)
    resp = client.post(
        "/api/correct", json={"pick_no": 1, "player_id": "qb1", "team_slot": 99}
    )
    assert resp.status_code == 422
    assert _log_bytes(tmp_path) == before


def test_reassigned_pick_survives_relaunch(tmp_path):
    log_path = tmp_path / "draft.jsonl"
    app1 = create_app(cfg=_cfg(), my_slot=1, log_path=log_path, pool=_pool())
    client1 = TestClient(app1)
    client1.post("/api/pick", json={"player_id": "qb1"})
    client1.post("/api/correct", json={"pick_no": 1, "player_id": "qb1", "team_slot": 4})

    # Simulate a relaunch: a fresh app/session over the same log.
    app2 = create_app(cfg=_cfg(), my_slot=1, log_path=log_path, pool=_pool())
    client2 = TestClient(app2)
    body = client2.get("/api/state").json()
    pick1 = next(p for p in body["all_picks"] if p["pick_no"] == 1)
    assert pick1["team_slot"] == 4


# --------------------------------------------------------------------------- undraft (finding 2)
#
# The bug these pin was the worst kind this tool can have: bookkeeping that drifts silently. The
# `x` on a row called /api/void, which marked the pick void and LEFT THE CLOCK ADVANCED, so the
# replacement player landed at the next pick number for the next team and every later pick on the
# physical board was attributed one slot off. Nothing on screen said so.


def test_undrafting_the_newest_pick_rewinds_the_clock_so_the_replacement_lands_in_place(tmp_path):
    client = _client(tmp_path, my_slot=1)
    for pid in ("qb1", "rb1", "wr1"):
        client.post("/api/pick", json={"player_id": pid})
    assert client.get("/api/state").json()["current_pick"] == 4

    resp = client.post("/api/undraft", json={"pick_no": 3})
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_undraft"] == {"pick_no": 3, "mode": "undone"}
    # The clock came back to 3, so the replacement goes to the team that actually owns pick 3.
    assert body["current_pick"] == 3
    assert "wr1" not in {p["player_id"] for p in body["all_picks"]}
    # No hole left behind, because the pick was never made rather than made-and-cancelled.
    assert body["gaps"] == []

    replacement = client.post("/api/pick", json={"player_id": "wr2"}).json()
    pick3 = next(p for p in replacement["all_picks"] if p["pick_no"] == 3)
    assert pick3["player_id"] == "wr2"
    assert pick3["team_slot"] == 3
    assert replacement["current_pick"] == 4


def test_undrafting_an_older_pick_leaves_a_visible_gap_and_does_not_move_the_clock(tmp_path):
    """History cannot rewind without renumbering everything after it, so an older pick becomes a
    void -- and the hole must be REPORTED, because that is what an unfilled sticker looks like."""
    client = _client(tmp_path, my_slot=1)
    for pid in ("qb1", "rb1", "wr1"):
        client.post("/api/pick", json={"player_id": pid})

    body = client.post("/api/undraft", json={"pick_no": 1}).json()
    assert body["last_undraft"] == {"pick_no": 1, "mode": "voided"}
    assert body["current_pick"] == 4
    assert body["gaps"] == [1]
    assert "qb1" not in {p["player_id"] for p in body["all_picks"] if not p["voided"]}


def test_undraft_writes_exactly_one_event_so_a_crash_cannot_tear_it_in_half(tmp_path):
    """A void+clock_set pair would let a power cut land between the two halves and leave the log
    describing a draft that never happened. One event, either path."""
    from draftroom.draft.events import EventLog

    client = _client(tmp_path, my_slot=1)
    for pid in ("qb1", "rb1"):
        client.post("/api/pick", json={"player_id": pid})
    before = len(EventLog(tmp_path / "draft.jsonl").events())

    client.post("/api/undraft", json={"pick_no": 2})
    assert len(EventLog(tmp_path / "draft.jsonl").events()) == before + 1

    client.post("/api/undraft", json={"pick_no": 1})
    assert len(EventLog(tmp_path / "draft.jsonl").events()) == before + 2


def test_undraft_rejects_unknown_and_already_voided_picks_without_appending(tmp_path):
    client = _client(tmp_path, my_slot=1)
    client.post("/api/pick", json={"player_id": "qb1"})
    client.post("/api/pick", json={"player_id": "rb1"})
    client.post("/api/void", json={"pick_no": 1})
    before = _log_bytes(tmp_path)

    assert client.post("/api/undraft", json={"pick_no": 99}).status_code == 422
    assert client.post("/api/undraft", json={"pick_no": 7}).status_code == 404
    assert client.post("/api/undraft", json={"pick_no": 1}).status_code == 409
    assert _log_bytes(tmp_path) == before


def test_undraft_survives_relaunch_with_the_clock_where_replay_puts_it(tmp_path):
    log_path = tmp_path / "draft.jsonl"
    app1 = create_app(cfg=_cfg(), my_slot=1, log_path=log_path, pool=_pool())
    client1 = TestClient(app1)
    for pid in ("qb1", "rb1", "wr1"):
        client1.post("/api/pick", json={"player_id": pid})
    client1.post("/api/undraft", json={"pick_no": 3})

    app2 = create_app(cfg=_cfg(), my_slot=1, log_path=log_path, pool=_pool())
    body = TestClient(app2).get("/api/state").json()
    assert body["current_pick"] == 3
    assert "wr1" not in {p["player_id"] for p in body["all_picks"]}


# --------------------------------------------------------------------------- reassign (finding 3)


def test_reassign_endpoint_accepts_the_exact_payload_the_ui_sends(tmp_path):
    """The UI sends {pick_no, team_slot} and NOTHING else.

    Routed through /api/correct that was a 422 -- a correction requires a player_id or a
    stub_name -- so "Reassign to team..." in the Draft Results tab never once worked. The older
    tests here resend player_id, so they passed while the shipped contract was broken (Codex
    2026-08-21 finding 3).
    """
    client = _client(tmp_path, my_slot=1)
    client.post("/api/pick", json={"player_id": "qb1"})

    resp = client.post("/api/reassign", json={"pick_no": 1, "team_slot": 3})
    assert resp.status_code == 200
    body = resp.json()
    pick1 = next(p for p in body["all_picks"] if p["pick_no"] == 1)
    assert pick1["team_slot"] == 3
    assert pick1["player_id"] == "qb1"  # identity preserved
    assert body["my_roster"] == []
    team3 = next(o for o in body["opponents"] if o["team_slot"] == 3)
    assert [r["player_id"] for r in team3["roster"]] == ["qb1"]


def test_reassign_preserves_a_stub_identity(tmp_path):
    """A write-in has no player_id at all, so a reassign that carried identity would erase it."""
    client = _client(tmp_path, my_slot=1)
    client.post("/api/stub", json={"name": "Some Rookie", "pos": "WR"})

    body = client.post("/api/reassign", json={"pick_no": 1, "team_slot": 2}).json()
    pick1 = next(p for p in body["all_picks"] if p["pick_no"] == 1)
    assert pick1["team_slot"] == 2
    assert pick1["is_stub"]
    assert pick1["name"] == "Some Rookie"


def test_reassign_recomputes_out_of_order_and_keeps_void_state(tmp_path):
    client = _client(tmp_path, my_slot=1)
    client.post("/api/pick", json={"player_id": "qb1"})  # pick 1, slot 1, in order

    moved = client.post("/api/reassign", json={"pick_no": 1, "team_slot": 4}).json()
    pick1 = next(p for p in moved["all_picks"] if p["pick_no"] == 1)
    assert pick1["out_of_order"], "slot 4 was not on the clock for pick 1"

    back = client.post("/api/reassign", json={"pick_no": 1, "team_slot": 1}).json()
    pick1 = next(p for p in back["all_picks"] if p["pick_no"] == 1)
    assert not pick1["out_of_order"]

    client.post("/api/void", json={"pick_no": 1})
    after = client.post("/api/reassign", json={"pick_no": 1, "team_slot": 2}).json()
    pick1 = next(p for p in after["all_picks"] if p["pick_no"] == 1)
    assert pick1["voided"], "a reassign must not silently un-void a pick"


def test_reassign_rejects_unknown_pick_and_out_of_range_slot_without_appending(tmp_path):
    client = _client(tmp_path, my_slot=1)
    client.post("/api/pick", json={"player_id": "qb1"})
    before = _log_bytes(tmp_path)
    assert client.post("/api/reassign", json={"pick_no": 1, "team_slot": 99}).status_code == 422
    assert client.post("/api/reassign", json={"pick_no": 6, "team_slot": 2}).status_code == 404
    assert _log_bytes(tmp_path) == before


# --------------------------------------------------------------------------- source toggle (B2)
#
# These pin the DEGRADED path: `draftroom.sources` missing or broken must leave the rest of the
# server fully working (see server.py's module docstring). Originally these relied on the module
# genuinely not existing yet; it exists now, so the absence is forced with monkeypatch instead.
# That is strictly better -- the degraded path stays covered no matter what ships alongside it.


def test_sources_endpoint_degrades_gracefully_when_module_absent(tmp_path, monkeypatch):
    from draftroom import server as server_mod

    monkeypatch.setattr(server_mod, "_sources_mod", None)
    client = _client(tmp_path, my_slot=1)
    resp = client.get("/api/sources")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["sources"], list)
    assert len(body["sources"]) == 1
    assert body["active"] == body["sources"][0]["key"]
    assert body["sources"][0]["player_count"] == len(_pool())
    assert "not available" in body["sources"][0]["note"]


def test_source_switch_endpoint_returns_503_when_module_absent(tmp_path, monkeypatch):
    from draftroom import server as server_mod

    monkeypatch.setattr(server_mod, "_sources_mod", None)
    client = _client(tmp_path, my_slot=1)
    before = _log_bytes(tmp_path)
    resp = client.post("/api/source", json={"key": "espn"})
    assert resp.status_code == 503
    assert _log_bytes(tmp_path) == before, "a failed switch must never append source_changed"


def test_state_payload_exposes_default_active_source(tmp_path):
    body = _client(tmp_path).get("/api/state").json()
    assert body["active_source"] == "blend"


# --------------------------------------------------------------------------- source resume
#
# `source_changed` was originally appended to the log and never read back, so a relaunch
# mid-draft silently reverted to the startup board -- the exact scenario the append-only log
# exists for. These tests pin the resume, both when it works and when it can't.


def _write_log(tmp_path, *lines: str):
    p = tmp_path / "draft.jsonl"
    p.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return p


def test_last_source_from_log_takes_the_most_recent(tmp_path):
    from draftroom.draft.events import EventLog
    from draftroom.draft.state import DraftSession
    from draftroom.server import _last_source_from_log

    log = EventLog(tmp_path / "draft.jsonl")
    assert _last_source_from_log(
        DraftSession(log, teams=TEAMS, rounds=2, my_slot=1)
    ) is None, "an untouched log must not claim a source was selected"

    log.append("source_changed", key="espn")
    log.append("source_changed", key="sleeper")
    session = DraftSession(log, teams=TEAMS, rounds=2, my_slot=1)
    assert _last_source_from_log(session) == "sleeper"


def test_injected_pool_never_resumes_from_log(tmp_path):
    """Tests pin a deterministic board by injecting `pool`; honouring the log there would make
    every test in this file depend on cached projection data."""
    from draftroom.draft.events import EventLog

    EventLog(tmp_path / "draft.jsonl").append("source_changed", key="espn")
    client = _client(tmp_path, my_slot=1)
    assert client.get("/api/state").json()["active_source"] == "blend"


def test_create_app_resumes_logged_source(tmp_path, monkeypatch):
    from draftroom import live_data, server as server_mod
    from draftroom.draft.events import EventLog

    EventLog(tmp_path / "draft.jsonl").append("source_changed", key="espn")

    espn_pool = _pool()[:3]

    class _FakeSources:
        SOURCE_KEYS = ("blend", "sleeper", "espn", "fantasypros")

        @staticmethod
        def available_sources():
            return [{"key": k, "label": k, "player_count": 3, "note": ""} for k in _FakeSources.SOURCE_KEYS]

        @staticmethod
        def pool_for_source_strict(key):
            # STRICT is what the resume path calls: a pool that built but valued nobody must not
            # come back as the active source (Codex 2026-08-21 finding 5).
            assert key == "espn", f"resume asked for {key!r}, not the logged source"
            return espn_pool

    monkeypatch.setattr(server_mod, "_sources_mod", _FakeSources)
    # Not injecting `pool` is what enables the resume, so the default loader must not be hit.
    monkeypatch.setattr(
        live_data, "load_player_pool", lambda *a, **k: pytest.fail("resume should not load the default pool")
    )
    app = create_app(cfg=_cfg(), my_slot=1, log_path=tmp_path / "draft.jsonl")
    body = TestClient(app).get("/api/state").json()
    assert body["active_source"] == "espn"


def test_resume_falls_back_loudly_when_source_cannot_be_rebuilt(tmp_path, monkeypatch, caplog):
    """A resume that fails must serve the default board AND say so -- never leave the header
    claiming a source whose values never loaded."""
    from draftroom import live_data, server as server_mod
    from draftroom.draft.events import EventLog

    EventLog(tmp_path / "draft.jsonl").append("source_changed", key="espn")

    class _BrokenSources:
        SOURCE_KEYS = ("blend", "espn")

        @staticmethod
        def pool_for_source_strict(key):
            raise RuntimeError("cached ESPN payload is missing")

    monkeypatch.setattr(server_mod, "_sources_mod", _BrokenSources)
    monkeypatch.setattr(live_data, "load_player_pool", lambda *a, **k: _pool())
    with caplog.at_level("WARNING"):
        app = create_app(cfg=_cfg(), my_slot=1, log_path=tmp_path / "draft.jsonl")
    body = TestClient(app).get("/api/state").json()
    assert body["active_source"] == "blend", "a failed resume must not claim the logged source"
    assert any("could not be rebuilt" in r.getMessage() for r in caplog.records), (
        "the fallback must be logged, not silent"
    )


def test_switching_to_a_source_that_valued_nobody_is_refused_and_never_logged(tmp_path, monkeypatch):
    """An unavailable source must not become active, and must not leave a trace saying it did.

    `load_player_pool` degrades a failed board build to an ADP-placeholder pool, so the lenient
    accessor never raised: the switch "succeeded", the header said ESPN, a `source_changed` event
    recorded ESPN, and a relaunch resumed that placeholder pool under the ESPN label. The picks
    were fine; the record of what board they were made against was a fiction (Codex 2026-08-21
    finding 5).
    """
    from draftroom import server as server_mod
    from draftroom.draft.events import EventLog

    class _PlaceholderSources:
        SOURCE_KEYS = ("blend", "espn")

        @staticmethod
        def pool_for_source_strict(key):
            raise RuntimeError(
                f"source {key!r} valued 0 of 8 ranked players -- ADP-placeholder fallback"
            )

    client = _client(tmp_path, my_slot=1)
    client.post("/api/pick", json={"player_id": "qb1"})
    monkeypatch.setattr(server_mod, "_sources_mod", _PlaceholderSources)

    before = _log_bytes(tmp_path)
    resp = client.post("/api/source", json={"key": "espn"})
    assert resp.status_code == 503
    assert "valued 0" in resp.json()["detail"]
    # Nothing appended: no source_changed event to resume from later.
    assert _log_bytes(tmp_path) == before
    assert client.get("/api/state").json()["active_source"] != "espn"
    assert not any(e.type == "source_changed" for e in EventLog(tmp_path / "draft.jsonl").events())


def test_source_changed_is_fsynced_before_the_served_pool_moves(tmp_path, monkeypatch):
    """Append first, then swap. The old order mutated the pool and `active_source` and appended
    afterwards, so a failed disk write left the running app on one source while replay would
    rebuild another -- the running state and the record disagreeing is precisely what an
    append-only log exists to prevent."""
    from draftroom import server as server_mod

    class _Boom(RuntimeError):
        pass

    class _FakeSources:
        SOURCE_KEYS = ("blend", "espn")

        @staticmethod
        def pool_for_source_strict(key):
            return _pool()[:3]

    client = _client(tmp_path, my_slot=1)
    monkeypatch.setattr(server_mod, "_sources_mod", _FakeSources)
    active_before = client.get("/api/state").json()["active_source"]

    # The failure is injected at the log, which is the only thing between "validated pool in
    # hand" and "state moved".
    import draftroom.draft.events as events_mod

    real_append = events_mod.EventLog.append

    def _failing_append(self, type_, **payload):
        if type_ == "source_changed":
            raise _Boom("disk full")
        return real_append(self, type_, **payload)

    monkeypatch.setattr(events_mod.EventLog, "append", _failing_append)
    with pytest.raises(_Boom):
        client.post("/api/source", json={"key": "espn"})

    monkeypatch.setattr(events_mod.EventLog, "append", real_append)
    # The pool never moved, so the header still names the source the log agrees with.
    assert client.get("/api/state").json()["active_source"] == active_before


def test_available_sources_marks_a_placeholder_only_source_unavailable(monkeypatch):
    """The `available` flag the UI greys out on. Computed from real values, not from whether the
    build threw -- a pool can come back fine and still have valued nobody."""
    from draftroom import live_data, sources as sources_mod

    monkeypatch.setattr(sources_mod, "_SOURCES_CACHE", None)
    sources_mod._POOL_CACHE.clear()

    placeholder = [
        PoolPlayer("qb1", "Josh Allen", "QB", "BUF", 7, 1.0, 0.7, 1, 200.0, value_is_real=False),
    ]
    monkeypatch.setattr(live_data, "load_player_pool", lambda *a, **k: placeholder)
    try:
        entries = {e["key"]: e for e in sources_mod.available_sources()}
        assert entries, "available_sources must always return one entry per source"
        for key, entry in entries.items():
            assert entry["available"] is False, key
            assert entry["player_count"] == 0
            assert "UNAVAILABLE" in entry["note"]
        with pytest.raises(sources_mod.SourceUnavailable, match="valued 0"):
            sources_mod.pool_for_source_strict("blend")
    finally:
        sources_mod._POOL_CACHE.clear()
        monkeypatch.setattr(sources_mod, "_SOURCES_CACHE", None)


def test_tier_rows_expose_value_by_source(tmp_path):
    """Plan A5: the per-source breakdown must reach the payload, or the UI's side-by-side
    comparison silently renders nothing. `frontend/src/types.ts` declares this field, and it was
    populated on PoolPlayer but never emitted -- exactly the kind of gap a green test suite hides.

    Absent is rendered as "not available", NEVER as agreement between sources, so the key must be
    present even when the value is None.
    """
    pool = _pool()
    with_breakdown = dataclasses.replace(
        pool[0], value_by_source={"sleeper": 191.8, "espn": 250.0, "fantasypros": 240.3}
    )
    app = create_app(
        cfg=_cfg(), my_slot=1, log_path=tmp_path / "draft.jsonl",
        pool=[with_breakdown] + pool[1:],
    )
    board = TestClient(app).get("/api/state").json()["tier_board"]
    rows = {r["player_id"]: r for rows_ in board.values() for r in rows_}

    assert "value_by_source" in rows["qb1"], "the key must always be present"
    assert rows["qb1"]["value_by_source"] == {
        "sleeper": 191.8, "espn": 250.0, "fantasypros": 240.3
    }
    # A player the board could not break down carries None, not {} and not a fabricated spread.
    assert rows["rb1"]["value_by_source"] is None


def test_announce_existing_draft_is_loud_about_a_non_empty_log(tmp_path, caplog):
    """A stale log and a crash-recovery resume look identical from outside, so startup must SAY
    which one it thinks it is. This is not hypothetical: the live log carried four smoke-test
    picks for two days, and launching on it would have opened the draft at pick 5 with four real
    players already gone and nothing on screen explaining why."""
    from draftroom.server import _announce_existing_draft

    app = create_app(cfg=_cfg(), my_slot=1, log_path=tmp_path / "draft.jsonl", pool=_pool())
    client = TestClient(app)
    client.post("/api/pick", json={"player_id": "qb1"})
    client.post("/api/pick", json={"player_id": "rb1"})

    with caplog.at_level("WARNING"):
        _announce_existing_draft(app.state.board)
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "RESUMING AN EXISTING DRAFT" in msg
    assert "2 pick(s)" in msg
    # The player NAMES must appear, not just ids -- "2885" tells Marc nothing at a glance.
    assert "Josh Allen" in msg
    assert "archived" in msg, "must tell him how to get a fresh board"


def test_announce_existing_draft_says_nothing_alarming_on_a_fresh_log(tmp_path, caplog):
    from draftroom.server import _announce_existing_draft

    app = create_app(cfg=_cfg(), my_slot=1, log_path=tmp_path / "draft.jsonl", pool=_pool())
    with caplog.at_level("INFO"):
        _announce_existing_draft(app.state.board)
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "fresh draft" in msg
    assert "RESUMING" not in msg


def test_announce_existing_draft_ignores_voided_picks(tmp_path, caplog):
    """A log whose every pick was voided is an empty BOARD, and must not read as a draft in
    progress -- otherwise undoing a mistaken pick would leave a permanent false alarm."""
    from draftroom.server import _announce_existing_draft

    app = create_app(cfg=_cfg(), my_slot=1, log_path=tmp_path / "draft.jsonl", pool=_pool())
    client = TestClient(app)
    client.post("/api/pick", json={"player_id": "qb1"})
    client.post("/api/void", json={"pick_no": 1})

    with caplog.at_level("INFO"):
        _announce_existing_draft(app.state.board)
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "fresh draft" in msg
    assert "RESUMING" not in msg
