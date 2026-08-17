"""Tests for the live draft server.

Uses a small, deterministic fixture pool rather than the cached FFC payload so these tests
don't depend on whatever happens to be in data/raw/ffc/ when they run -- the search-ranking
and pick-mechanics behavior under test is about the server's wiring, not about real player
data (that's covered by tests/test_search.py and tests/test_data_layer.py).
"""

from __future__ import annotations

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
    ]


def _client(tmp_path, my_slot: int = 1) -> TestClient:
    app = create_app(cfg=_cfg(), my_slot=my_slot, log_path=tmp_path / "draft.jsonl", pool=_pool())
    return TestClient(app)


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
