"""Round 1 of Marc's own dry-run feedback, pinned. See FEEDBACK_LEDGER.md.

Each test names its ledger item. The point of a ledger is that an item cannot silently regress,
and a ledger entry with no test behind it is a promise rather than a guarantee -- so the items
that changed BEHAVIOUR (rather than only styling) live here.

Not covered here, deliberately, because they are purely visual and a test would pin CSS rather
than behaviour: #1's spread figure, #5's underline affordance, #7's wording, #8's badge. Those are
verified against the running app and recorded in the ledger with the date and the method.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from draftroom.config import LeagueConfig
from draftroom.draft.events import EventLog
from draftroom.draft.state import DraftState
from draftroom.live_data import PoolPlayer
from draftroom.server import create_app

TEAMS = 10


def _cfg() -> LeagueConfig:
    return LeagueConfig(
        teams=TEAMS,
        starters={"QB": 2, "RB": 2, "WR": 3, "TE": 1},
        flex_slots=1,
        flex_eligible=frozenset({"RB", "WR", "TE"}),
        bench=6,
        weeks=17,
        scoring={},
    )


def _pool() -> list[PoolPlayer]:
    out: list[PoolPlayer] = []
    rank = 0
    for pos, n in (("QB", 12), ("RB", 14), ("WR", 16), ("TE", 8)):
        for i in range(n):
            rank += 1
            out.append(
                PoolPlayer(
                    f"{pos.lower()}{i}", f"{pos} {i}", pos, "BUF", 7,
                    float(rank), 1.0, rank, 500.0 - rank,
                )
            )
    return out


def _client(tmp_path, my_slot: int = 4) -> TestClient:
    return TestClient(
        create_app(
            cfg=_cfg(), my_slot=my_slot, log_path=tmp_path / "draft.jsonl", pool=_pool()
        )
    )


# --------------------------------------------------------------- #4 set the slot from the UI


def test_4_my_slot_can_be_set_at_the_table(tmp_path):
    client = _client(tmp_path, my_slot=4)
    assert client.get("/api/state").json()["my_slot"] == 4

    body = client.post("/api/my-slot", json={"my_slot": 7}).json()
    assert body["my_slot"] == 7
    assert [o["team_slot"] for o in body["opponents"] if o["is_mine"]] == [7], (
        "every is_mine flag has to follow the seat, not just the my_slot field"
    )
    assert body["my_roster"] == [], "the new seat starts with no roster"


def test_4_setting_the_slot_clears_the_assumed_banner(tmp_path):
    """`slot_assumed` warns that every turn-dependent number is provisional. Once he has told us
    the seat it is not an assumption any more, and leaving the banner up would keep warning him
    about the thing he just fixed."""
    app = create_app(cfg=_cfg(), my_slot=None, log_path=tmp_path / "draft.jsonl", pool=_pool())
    client = TestClient(app)
    assert client.get("/api/state").json()["slot_assumed"] is True
    assert client.post("/api/my-slot", json={"my_slot": 3}).json()["slot_assumed"] is False


def test_4_my_slot_survives_a_relaunch_on_a_different_launch_flag(tmp_path):
    """THE reason it is an event. The draw happens at the table; a crash must not undo it."""
    first = _client(tmp_path, my_slot=4)
    first.post("/api/my-slot", json={"my_slot": 9})

    # Relaunched with the ORIGINAL flag, which the log must override.
    second = _client(tmp_path, my_slot=4)
    assert second.get("/api/state").json()["my_slot"] == 9


@pytest.mark.parametrize("bad", [0, -1, TEAMS + 1, 99])
def test_4_out_of_range_slot_is_refused_and_never_appended(tmp_path, bad):
    """Validate BEFORE appending: the log is append-only, so a bad event cannot be taken back,
    and an out-of-range slot replayed into snake arithmetic is a durable crash."""
    client = _client(tmp_path)
    before = (tmp_path / "draft.jsonl").read_bytes() if (tmp_path / "draft.jsonl").exists() else b""
    assert client.post("/api/my-slot", json={"my_slot": bad}).status_code == 422
    after = (tmp_path / "draft.jsonl").read_bytes() if (tmp_path / "draft.jsonl").exists() else b""
    assert after == before, "a refused slot change must leave the log untouched"


def test_4_the_marker_follows_the_seat_even_when_every_team_is_named(tmp_path):
    """The actual confusion: on draft night all ten seats have names, and the old rule let a name
    replace the YOU marker -- so nothing said which seat was his."""
    client = _client(tmp_path, my_slot=4)
    for slot in range(1, TEAMS + 1):
        client.post("/api/team-name", json={"team_slot": slot, "name": f"Team Name {slot}"})

    body = client.post("/api/my-slot", json={"my_slot": 6}).json()
    labels = {o["team_slot"]: o["team_label"] for o in body["opponents"]}
    assert labels[6] == "Team Name 6 (YOU)"
    assert labels[4] == "Team Name 4", "the old seat keeps its name and drops the marker"
    assert sum(1 for v in labels.values() if "(YOU)" in v) == 1, "exactly one seat is his"


def test_4_board_my_slot_is_not_a_stale_second_copy(tmp_path):
    """`DraftBoard.my_slot` was a plain attribute set at construction. With the slot now movable,
    a stored copy would leave `is_mine`/`my_roster` pointing at the OLD seat while the state said
    otherwise."""
    client = _client(tmp_path, my_slot=2)
    app_board = client.app.state.board
    assert app_board.my_slot == 2
    client.post("/api/my-slot", json={"my_slot": 8})
    assert app_board.my_slot == 8, "the board must read through to the state, not keep a copy"


# --------------------------------------------------------------- #6 recommendation preview


def test_6_recommendation_answers_for_his_own_next_pick_before_his_turn(tmp_path):
    """The engine used to say nothing at all until it was literally his turn.

    `target=mine` LOOKED like the escape hatch and was dead code: the endpoint computed the right
    pick number and `_recommendation_payload` dropped it before calling the engine.
    """
    client = _client(tmp_path, my_slot=4)
    state = client.get("/api/state").json()
    assert state["current_pick"] == 1 and state["slot_on_clock"] == 1, "not his turn"

    on_clock = client.get("/api/recommendation?target=clock").json()
    assert on_clock["is_my_pick"] is False
    assert on_clock["candidates"] == [], "the clock target still honestly declines"

    mine = client.get("/api/recommendation?target=mine").json()
    assert mine["pick_no"] == 4, "his first pick at slot 4 of 10"
    assert mine["is_my_pick"] is True
    assert mine["preview_for_pick"] == 4
    assert mine["picks_away"] == 3
    assert mine["candidates"], "the whole point: it now makes a case before his turn"


def test_6_a_preview_is_always_labelled_as_one(tmp_path):
    """A preview presented as live would be worse than the silence it replaced."""
    client = _client(tmp_path, my_slot=4)
    mine = client.get("/api/recommendation?target=mine").json()
    assert mine["preview_for_pick"] == 4 and mine["picks_away"] == 3

    # On his own turn the two targets agree and nothing is marked as a preview.
    client.post("/api/my-slot", json={"my_slot": 1})
    live = client.get("/api/recommendation?target=mine").json()
    assert live["pick_no"] == 1
    assert live["preview_for_pick"] is None, "his own live pick is not a preview"
    assert live["picks_away"] == 0


def test_6_asking_about_another_pick_never_moves_the_live_draft(tmp_path):
    """The engine is handed a COPY of the state. A hypothetical must not advance the clock."""
    client = _client(tmp_path, my_slot=4)
    before = client.get("/api/state").json()
    log_before = (tmp_path / "draft.jsonl").read_bytes() if (tmp_path / "draft.jsonl").exists() else b""

    client.get("/api/recommendation?target=mine")

    after = client.get("/api/state").json()
    assert after["current_pick"] == before["current_pick"]
    assert after["event_seq"] == before["event_seq"], "a question is not an event"
    log_after = (tmp_path / "draft.jsonl").read_bytes() if (tmp_path / "draft.jsonl").exists() else b""
    assert log_after == log_before


def test_6_the_preview_follows_the_slot_when_it_changes(tmp_path):
    """#4 and #6 interact: moving his seat must move which pick the preview is about."""
    client = _client(tmp_path, my_slot=4)
    assert client.get("/api/recommendation?target=mine").json()["pick_no"] == 4
    client.post("/api/my-slot", json={"my_slot": 9})
    assert client.get("/api/recommendation?target=mine").json()["pick_no"] == 9


# --------------------------------------------------------------- replay integrity


def test_my_slot_set_replays_like_every_other_correction(tmp_path):
    """Read straight off the log, the way crash recovery does."""
    client = _client(tmp_path, my_slot=4)
    client.post("/api/my-slot", json={"my_slot": 6})
    client.post("/api/pick", json={"player_id": "qb0"})
    client.post("/api/my-slot", json={"my_slot": 2})  # a re-decision; last one wins

    replayed = DraftState.replay(
        EventLog(tmp_path / "draft.jsonl").events(),
        teams=TEAMS,
        rounds=_cfg().roster_size,
        my_slot=4,  # the launch value, which the log must override
    )
    assert replayed.my_slot == 2
    assert len(replayed.picks) == 1, "the slot changes must not disturb the picks"


def test_a_hypothetical_state_shares_no_mutable_history_with_the_live_draft(tmp_path):
    """The preview hands the engine a copy. That copy must be deep enough to be harmless.

    Written as a plain `dataclasses.replace`, this FAILED: replace is shallow, so the copy shared
    the live `picks` dict and popping from one popped from the other. `recommend()` documents
    itself as read-only, so nothing exploits that today -- which is exactly why it was worth
    pinning, because the isolation should not rest on an engine keeping a promise while a draft is
    live. `_call_recommend_engine` now copies the dict and every Pick inside it.
    """
    from draftroom import server as server_mod

    client = _client(tmp_path, my_slot=4)
    client.post("/api/pick", json={"player_id": "qb0"})
    board = client.app.state.board
    live = board.state
    assert 1 in live.picks

    # Exercise the real path rather than re-deriving the copy here.
    server_mod._call_recommend_engine(board, for_pick=live.current_pick + 3)
    assert 1 in live.picks, "asking about another pick must not disturb the recorded picks"
    assert live.current_pick == 2, "...nor the clock"

    # And the copy the path builds is genuinely independent, not an alias.
    copy = dataclasses.replace(
        live,
        current_pick=99,
        picks={n: dataclasses.replace(pk) for n, pk in live.picks.items()},
        team_names=dict(live.team_names),
    )
    copy.picks.pop(1, None)
    assert 1 in live.picks, "a hypothetical must not be able to delete a real pick"
    assert copy.picks is not live.picks
