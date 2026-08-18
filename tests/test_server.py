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
