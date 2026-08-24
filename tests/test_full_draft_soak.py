"""A COMPLETE draft, driven through the real API. The bookkeeping soak.

WHY THIS FILE EXISTS
--------------------
Bookkeeping is this tool's FIRST job. It is the only record of an in-person sticker-board draft,
so a projection being 8% off costs a marginal pick, while the clock drifting one slot at pick 47
costs the draft. Before this file, ``tests/test_server.py`` had 69 tests and **not one of them
drafted more than a handful of picks**: every mechanic was pinned in isolation and the thing that
actually happens on draft night -- 150 consecutive mutations against one append-only event log --
had never run in the suite at all.

That gap is not hypothetical. Two bugs of exactly this shape reached the repo and were caught by
review rather than by tests, both recorded in CLAUDE.md:

* ``out_of_order`` was computed as ``team_slot is not None``, so click-anywhere drafting (which
  always supplies a slot) badged EVERY ordinary pick as out of order.
* "Undraft" on the newest pick appended ``pick_voided`` and left the clock advanced, so the
  replacement landed at the next pick number for the next team and the whole board drifted one
  slot against the physical one -- silently.

Neither is visible in a 3-pick test. Both are obvious in a 150-pick one, and both were used as
mutation tests against this file before it shipped: injecting the first turns 3 tests red,
injecting the second turns 1 red.

IT RUNS ON A SYNTHETIC POOL, ON PURPOSE
---------------------------------------
The obvious way to write this was against the real cached pool, and that was the first draft of
this file. It was wrong, and Codex caught it as the blocker: ``data/raw/`` is gitignored, the
pool fixture skipped when the cache was absent, and **every test in the file depended on that
fixture**. So on any machine without Marc's local cache -- a fresh clone, a CI runner, this repo
after a cache tidy-up -- the whole file reported ``13 skipped``, exited 0, and left the exact
coverage gap it exists to close wide open while looking closed. That is the same failure this
repo already refuses everywhere else ("a suppression nobody can see is indistinguishable from a
detector that stopped working").

So the bookkeeping tests build their own deterministic pool and **cannot skip**. Pick mechanics,
the clock, the event log and replay have nothing to do with whether a projection is any good --
they need enough PLAYERS, not real ones. ``LeagueConfig.from_yaml()`` is still the real league,
because ``data/league_manual.yaml`` is tracked.

One test at the bottom does run against the real cached pool, for end-to-end confidence with real
data. That one may skip, and it is additive: nothing this file guarantees depends on it.

WHAT IS ASSERTED, AND WHY EACH ONE
----------------------------------
``_assert_consistent`` runs after EVERY mutation -- roughly 150 times per soak -- and is where
all the value of this file lives, so it reconciles the whole board rather than spot-checking:

1. **The clock matches snake arithmetic.** ``slot_on_clock(teams, pick)`` is the physical room's
   own rule; a drift here is the board disagreeing with the people at the table.
2. **No player is drafted twice**, and the live pick numbers are the contiguous run ``1..N``
   with no gaps below the clock. The failure a mis-numbered pick creates is a drafted player
   still looking available.
3. **Every live pick reconciles with its board row** -- drafted flag AND owner. Checking only the
   player just drafted (the first version of this file) would pass while 149 rows showed no
   owner at all.
4. **No row claims to be drafted that has no live pick behind it**, the same check from the other
   side, and the one that catches a void which failed to free its player.
5. **``out_of_order`` is exactly "did not go to the team on the clock"** for every live pick, not
   only the ones deliberately misfiled.
6. **Rosters are counted from the SERVER's own ``opponents`` payload**, never from this file's
   record of what it asked for -- otherwise the test grades its own homework.

There is exactly ONE loosening, ``allow_unpooled``, and writing the strict version is what
surfaced the property behind it: a source change REBUILDS the pool, so a player drafted under the
old board can legitimately have no row on the new one. The picks are unaffected, because
``all_picks`` is replayed from the event log rather than read off the pool -- which is precisely
why bookkeeping survives a mid-draft source change. Only the source-toggle test passes that flag,
and the tests that do not additionally assert the skip count is zero, so it cannot quietly hollow
out the check.

And at the end: **a cold replay of the event log reproduces the core pick state** (number,
player, owner, void and out-of-order flags, and the clock). That is what crash recovery does, so
a disagreement means a mid-draft relaunch serves a different board than the one on screen.

Strengthening these assertions after review was not cosmetic: re-running the two mutation tests
against the stricter helper, the ``out_of_order`` bug went from failing 1 test to failing **12**.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from draftroom import live_data
from draftroom.config import LeagueConfig
from draftroom.draft import snake
from draftroom.draft.events import EventLog
from draftroom.draft.state import DraftState
from draftroom.live_data import PoolPlayer
from draftroom.server import create_app

MY_SLOT = 4

#: Rough real-board shape, and deliberately more players than a draft consumes so the soak never
#: runs the pool dry (which would be a test artefact, not a finding).
_SYNTHETIC_BY_POS = {"QB": 45, "RB": 65, "WR": 85, "TE": 35}
_UNRANKED_PER_POS = 15
_NFL_TEAMS = ("BUF", "DET", "CIN", "MIN", "SEA", "NE", "PHI", "KC", "SF", "IND", "NYJ", "TB")


@pytest.fixture(scope="module")
def cfg() -> LeagueConfig:
    """The REAL league. ``data/league_manual.yaml`` is tracked, so this needs no cache."""
    return LeagueConfig.from_yaml()


@pytest.fixture(scope="module")
def pool(cfg) -> list[PoolPlayer]:
    """A deterministic pool big enough for a full draft. Builds on any checkout, never skips.

    Two tiers, like the real one (CLAUDE.md): ranked players carry a real ADP and a positive
    value; unranked ones carry ``value=0.0``, which is NOT an evaluation -- they exist so the
    board can RECORD a late-round name it has no projection for.
    """
    out: list[PoolPlayer] = []
    rank = 0
    for pos, n in _SYNTHETIC_BY_POS.items():
        for i in range(n):
            rank += 1
            out.append(
                PoolPlayer(
                    player_id=f"{pos.lower()}{i:03d}",
                    name=f"{pos} Player {i:03d}",
                    pos=pos,
                    team=_NFL_TEAMS[rank % len(_NFL_TEAMS)],
                    bye=(rank % 14) + 1,
                    adp=float(rank),
                    stdev=1.0 + (rank % 7),
                    overall_rank=rank,
                    # Strictly descending and unique across the whole pool, so "best available"
                    # is a total order and any failure is reproducible.
                    value=1000.0 - rank,
                )
            )
    for pos in _SYNTHETIC_BY_POS:
        for i in range(_UNRANKED_PER_POS):
            rank += 1
            out.append(
                PoolPlayer(
                    player_id=f"u_{pos.lower()}{i:03d}",
                    name=f"Unranked {pos} {i:03d}",
                    pos=pos,
                    team=_NFL_TEAMS[rank % len(_NFL_TEAMS)],
                    bye=(rank % 14) + 1,
                    adp=999.0,
                    stdev=50.0,
                    overall_rank=rank,
                    value=0.0,
                    is_ranked=False,
                )
            )
    total_picks = cfg.teams * cfg.roster_size
    ranked = sum(1 for p in out if p.is_ranked)
    assert ranked > total_picks + 25, (
        f"the synthetic pool has {ranked} ranked players for a {total_picks}-pick draft; it must "
        "carry comfortable headroom or a soak failure could just be an exhausted pool"
    )
    return out


@pytest.fixture(scope="module")
def real_pool():
    """The real cached pool. MAY SKIP -- used only by the additive real-data soak at the bottom.

    An absent cache skips: ``data/raw/`` is gitignored, so it is legitimately missing on a fresh
    clone. A cache that LOADS but is empty or tiny FAILS instead -- that is a broken cache rather
    than an absent one, and skipping on it would hide a real data-layer regression.
    """
    try:
        got = live_data.load_player_pool()
    except FileNotFoundError as exc:
        pytest.skip(f"no cached prep data (gitignored, so this is normal): {exc}")
    assert got, "load_player_pool() returned an EMPTY pool -- a data-layer bug, not a missing cache"
    assert len(got) > 200, (
        f"cached pool has only {len(got)} players; the real one is ~980. A cache this small is "
        "broken rather than missing, so this fails instead of skipping."
    )
    return got


def _client(tmp_path, cfg, pool, *, name: str = "draft.jsonl") -> TestClient:
    app = create_app(cfg=cfg, my_slot=MY_SLOT, log_path=tmp_path / name, pool=list(pool))
    return TestClient(app)


def _rows(payload) -> list[dict]:
    return [r for pos in payload["tier_board"] for r in payload["tier_board"][pos]]


def _best_available(payload) -> dict:
    """The best undrafted RANKED player -- what a manager plausibly takes.

    A real choice rather than a random one: it walks the board down in value order, so early
    rounds drain the top and late rounds reach the tail, and any failure is reproducible.
    """
    avail = [r for r in _rows(payload) if not r["drafted"] and r["is_ranked"]]
    assert avail, "the pool ran dry before the draft finished"
    return max(avail, key=lambda r: (r["value"], -r["adp"]))


def _assert_consistent(
    payload, *, picks_made: int, cfg: LeagueConfig, allow_unpooled: bool = False
) -> None:
    """Every invariant that must hold after ANY mutation. Called ~150 times per soak.

    Deliberately reconciles the WHOLE board, not just the row that moved. The first version of
    this helper checked only the player just drafted, which would have passed while a payload
    regression left 149 other rows with no owner (Codex 2026-08-24 finding 2).

    ``allow_unpooled`` is the one deliberate loosening, and it exists because writing this helper
    strictly SURFACED A REAL PROPERTY: switching the projection source REBUILDS the pool, so a
    player who was drafted under the old board can legitimately be absent from the new one. The
    picks themselves are unaffected (``all_picks`` is replayed from the event log, not read off
    the pool), which is precisely why bookkeeping survives a mid-draft source change -- but the
    tier board genuinely has no row for him any more.

    So: pass ``allow_unpooled=True`` ONLY across a pool swap. Everywhere else the default keeps
    full strictness, and the callers that matter additionally assert that nothing was skipped for
    absence, so this argument cannot quietly hollow out the check.
    """
    total = cfg.teams * cfg.roster_size

    # 1 - the clock agrees with the room's own snake rule
    expected_clock = min(picks_made + 1, total + 1)
    assert payload["current_pick"] == expected_clock, (
        f"after {picks_made} picks the clock should be at {expected_clock}, "
        f"got {payload['current_pick']}"
    )
    if payload["current_pick"] <= total:
        assert payload["slot_on_clock"] == snake.slot_on_clock(
            cfg.teams, payload["current_pick"]
        ), "slot_on_clock drifted from snake arithmetic -- the board now disagrees with the table"

    live = [p for p in payload["all_picks"] if not p["voided"]]

    # 2 - nobody drafted twice; numbers are the contiguous run with no holes
    ids = [p["player_id"] for p in live if p["player_id"]]
    assert len(ids) == len(set(ids)), (
        f"duplicate player(s) on the board: {sorted({i for i in ids if ids.count(i) > 1})}"
    )
    assert len(live) == picks_made, f"expected {picks_made} live picks, found {len(live)}"
    assert payload["gaps"] == [], f"gaps below the clock: {payload['gaps']}"
    assert sorted(p["pick_no"] for p in live) == list(range(1, picks_made + 1)), (
        "live pick numbers are not the contiguous run 1..N"
    )

    # 5 - out_of_order means exactly one thing, for EVERY pick and not just the misfiled ones
    for p in live:
        should_flag = p["team_slot"] != snake.slot_on_clock(cfg.teams, p["pick_no"])
        assert p["out_of_order"] == should_flag, (
            f"pick {p['pick_no']} went to slot {p['team_slot']}, the clock said "
            f"{snake.slot_on_clock(cfg.teams, p['pick_no'])}, out_of_order={p['out_of_order']}"
        )

    # 3 - every live pick reconciles with its board row, drafted flag AND owner
    rows = {r["player_id"]: r for r in _rows(payload)}
    owned: dict[str, int] = {}
    unpooled = 0
    for p in live:
        if p["player_id"] is None:
            continue  # a write-in stub is not a board row at all
        row = rows.get(p["player_id"])
        if row is None:
            assert allow_unpooled, (
                f"pick {p['pick_no']} names a player absent from the board. That is only legal "
                "across a source change, which rebuilds the pool -- pass allow_unpooled=True "
                "there and nowhere else."
            )
            unpooled += 1
            continue
        assert row["drafted"], (
            f"{row['name']} was drafted at pick {p['pick_no']} but the board still shows him free"
        )
        assert row["owner_team_slot"] == p["team_slot"], (
            f"{row['name']} is owned by slot {row['owner_team_slot']} on the board but was "
            f"picked by slot {p['team_slot']}"
        )
        owned[p["player_id"]] = p["team_slot"]
    payload["_unpooled_picks"] = unpooled  # read back by callers that assert it is zero

    # 4 - and from the other side: no row may claim to be drafted with no live pick behind it.
    # This is the check that catches a void which failed to free its player.
    for pid, row in rows.items():
        if row["drafted"]:
            assert pid in owned, (
                f"{row['name']} shows as drafted but no live pick claims him -- a voided or "
                "undrafted player who was never freed"
            )

    # 6 - rosters from the SERVER's own payload, never from the caller's record of what it asked
    for team in payload["opponents"]:
        expected_n = sum(1 for p in live if p["team_slot"] == team["team_slot"])
        assert len(team["roster"]) == expected_n, (
            f"slot {team['team_slot']} has {len(team['roster'])} on its roster but "
            f"{expected_n} live picks"
        )


def _replay_state(tmp_path, cfg, name: str = "draft.jsonl") -> DraftState:
    return DraftState.replay(
        EventLog(tmp_path / name).events(),
        teams=cfg.teams,
        rounds=cfg.roster_size,
        my_slot=MY_SLOT,
    )


def _core_pick_state(payload) -> dict[int, tuple]:
    """The pick facts a replay must reproduce, read from an API payload."""
    return {
        p["pick_no"]: (p["player_id"], p["team_slot"], p["voided"], p["out_of_order"])
        for p in payload["all_picks"]
    }


def _core_pick_state_replayed(state: DraftState) -> dict[int, tuple]:
    """The same facts from a replayed DraftState.

    Includes the FLAGS, not just identity: replay derives ``out_of_order`` from
    ``(team_slot, pick_no)`` rather than reading it out of the event, so it is exactly the sort of
    thing that can come back wrong while player and owner still look right (Codex 2026-08-24
    finding 4 -- the earlier version of this comparison claimed "exactly" while checking less).
    """
    return {
        n: (pk.player_id, pk.team_slot, pk.voided, pk.out_of_order)
        for n, pk in state.picks.items()
    }


def _draft_n(client, payload, n: int, start: int = 1):
    """Draft n picks in order, returning (payload, [(pick_no, player_id, slot), ...])."""
    taken = []
    for pick_no in range(start, start + n):
        target = _best_available(payload)
        slot = payload["slot_on_clock"]
        payload = client.post(
            "/api/pick",
            json={"player_id": target["player_id"], "pick_no": pick_no, "team_slot": slot},
        ).json()
        taken.append((pick_no, target["player_id"], slot))
    return payload, taken


# --------------------------------------------------------------------------- the soak


def test_a_complete_draft_stays_consistent_at_every_single_pick(tmp_path, cfg, pool):
    """150 picks, every invariant checked after each one. THE test this file exists for."""
    client = _client(tmp_path, cfg, pool)
    total = cfg.teams * cfg.roster_size

    payload = client.get("/api/state").json()
    _assert_consistent(payload, picks_made=0, cfg=cfg)

    taken: list[tuple[int, str, int]] = []
    for pick_no in range(1, total + 1):
        on_clock = payload["slot_on_clock"]
        target = _best_available(payload)
        resp = client.post(
            "/api/pick",
            json={"player_id": target["player_id"], "pick_no": pick_no, "team_slot": on_clock},
        )
        assert resp.status_code == 200, f"pick {pick_no} failed: {resp.text}"
        payload = resp.json()
        taken.append((pick_no, target["player_id"], on_clock))
        _assert_consistent(payload, picks_made=pick_no, cfg=cfg)
        assert payload["_unpooled_picks"] == 0, (
            "the pool is constant here, so every pick must have reconciled against a real board "
            "row -- a nonzero count would mean the strict check silently found nothing to check"
        )

    assert len(taken) == total

    # Every team ends with a full roster, counted from the server's own payload.
    for team in payload["opponents"]:
        assert len(team["roster"]) == cfg.roster_size, (
            f"slot {team['team_slot']} ended a snake draft with {len(team['roster'])} players, "
            f"not {cfg.roster_size}"
        )

    # THE strongest check: a cold replay of the log reproduces the core pick state. This is what
    # a mid-draft relaunch does, so a disagreement means crash recovery serves a different draft
    # than the one on screen.
    replayed = _replay_state(tmp_path, cfg)
    assert replayed.current_pick == payload["current_pick"]
    assert _core_pick_state_replayed(replayed) == _core_pick_state(payload)
    assert len(replayed.picks) == total


def test_the_whole_draft_is_one_appended_event_per_pick(tmp_path, cfg, pool):
    """The log is append-only and must not accumulate hidden extra events.

    A pick that writes two events can be torn in half by a crash -- the reason `undraft` appends
    ONE event rather than a void+clock_set pair (CLAUDE.md). Cheap to assert, and it would catch
    a well-meaning refactor that "helpfully" also stamps the clock.
    """
    client = _client(tmp_path, cfg, pool)
    payload = client.get("/api/state").json()
    n = 25
    payload, _ = _draft_n(client, payload, n)

    lines = [
        json.loads(line)
        for line in (tmp_path / "draft.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == n, f"{n} picks wrote {len(lines)} events"
    assert all(ev["type"] == "pick" for ev in lines)


# --------------------------------------------------------------------------- messy reality


def test_undrafting_the_newest_pick_rewinds_the_clock_and_leaves_no_gap(tmp_path, cfg, pool):
    """The board-drift bug, pinned mid-draft rather than at pick 1.

    Voiding the newest pick instead of undrafting it left the clock advanced, so the replacement
    landed at the next pick number for the NEXT team and every later pick sat one slot off the
    physical board. Nothing on screen said so.
    """
    client = _client(tmp_path, cfg, pool)
    payload = client.get("/api/state").json()
    payload, taken = _draft_n(client, payload, 20)
    last_pick, last_player, last_slot = taken[-1]

    resp = client.post("/api/undraft", json={"pick_no": last_pick})
    assert resp.status_code == 200, resp.text
    payload = resp.json()

    assert payload["last_undraft"]["mode"] == "undone", (
        "the newest pick must be UNDONE (clock rewinds), never voided (clock stays advanced)"
    )
    _assert_consistent(payload, picks_made=last_pick - 1, cfg=cfg)
    assert payload["slot_on_clock"] == last_slot, (
        "the clock must return to the team whose pick was removed"
    )
    row = next(r for r in _rows(payload) if r["player_id"] == last_player)
    assert not row["drafted"], "the undrafted player must be available again"

    # And the replacement lands at the SAME pick number for the SAME team.
    replacement = _best_available(payload)
    payload = client.post(
        "/api/pick",
        json={
            "player_id": replacement["player_id"],
            "pick_no": last_pick,
            "team_slot": payload["slot_on_clock"],
        },
    ).json()
    _assert_consistent(payload, picks_made=last_pick, cfg=cfg)
    landed = next(p for p in payload["all_picks"] if p["pick_no"] == last_pick and not p["voided"])
    assert landed["team_slot"] == last_slot


def test_undrafting_an_OLDER_pick_voids_it_and_reports_the_hole(tmp_path, cfg, pool):
    """The other half of the two-acts rule: an older pick cannot rewind the clock.

    Rewinding would renumber everything after it. So it is voided and the hole is REPORTED,
    because a silently missing pick makes a drafted player look available.
    """
    client = _client(tmp_path, cfg, pool)
    payload = client.get("/api/state").json()
    payload, taken = _draft_n(client, payload, 20)
    old_pick, old_player, _ = taken[5]
    clock_before = payload["current_pick"]

    resp = client.post("/api/undraft", json={"pick_no": old_pick})
    assert resp.status_code == 200, resp.text
    payload = resp.json()

    assert payload["last_undraft"]["mode"] == "voided", "an older pick must not rewind the clock"
    assert payload["current_pick"] == clock_before, "the clock moved on an older-pick undraft"
    assert old_pick in payload["gaps"], (
        f"pick {old_pick} was removed and must be reported as a gap, not vanish"
    )
    row = next(r for r in _rows(payload) if r["player_id"] == old_player)
    assert not row["drafted"], "the voided pick's player must be available again"


def test_an_out_of_order_pick_is_flagged_and_an_ordinary_one_is_not(tmp_path, cfg, pool):
    """`out_of_order` must mean "this pick did not go to the team on the clock" -- nothing else.

    It was once computed as `team_slot is not None`, and since click-anywhere drafting always
    supplies a slot, that badged every ordinary pick.
    """
    client = _client(tmp_path, cfg, pool)
    payload = client.get("/api/state").json()
    payload, _ = _draft_n(client, payload, 10)

    assert not any(p["out_of_order"] for p in payload["all_picks"] if not p["voided"]), (
        "ten picks all made by the team on the clock; none may be flagged out of order"
    )

    on_clock = payload["slot_on_clock"]
    wrong_slot = (on_clock % cfg.teams) + 1
    target = _best_available(payload)
    payload = client.post(
        "/api/pick",
        json={"player_id": target["player_id"], "pick_no": 11, "team_slot": wrong_slot},
    ).json()

    flagged = next(p for p in payload["all_picks"] if p["pick_no"] == 11)
    assert flagged["out_of_order"], (
        f"pick 11 went to slot {wrong_slot} while slot {on_clock} was on the clock"
    )
    assert sum(1 for p in payload["all_picks"] if p["out_of_order"]) == 1


def test_reassigning_a_pick_moves_the_owner_and_not_the_player(tmp_path, cfg, pool):
    """Right-click -> "Reassign to team" -- the in-room fix for "that was actually Jay's pick"."""
    client = _client(tmp_path, cfg, pool)
    payload = client.get("/api/state").json()
    payload, taken = _draft_n(client, payload, 12)
    pick_no, player_id, slot = taken[4]
    new_slot = (slot % cfg.teams) + 1

    resp = client.post("/api/reassign", json={"pick_no": pick_no, "team_slot": new_slot})
    assert resp.status_code == 200, resp.text
    payload = resp.json()

    moved = next(p for p in payload["all_picks"] if p["pick_no"] == pick_no)
    assert moved["team_slot"] == new_slot
    assert moved["player_id"] == player_id, "a reassign must not touch the player"
    _assert_consistent(payload, picks_made=12, cfg=cfg)


def test_a_write_in_stub_records_a_player_the_board_cannot_see(tmp_path, cfg, pool):
    """Late rounds reach names outside the ADP feed. Recording them is the point of the stub."""
    client = _client(tmp_path, cfg, pool)
    payload = client.get("/api/state").json()
    payload, _ = _draft_n(client, payload, 8)

    resp = client.post(
        "/api/stub",
        json={
            "name": "Some Camp Body",
            "pos": "WR",
            "pick_no": 9,
            "team_slot": payload["slot_on_clock"],
        },
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    _assert_consistent(payload, picks_made=9, cfg=cfg)
    stub = next(p for p in payload["all_picks"] if p["pick_no"] == 9)
    assert stub["player_id"] is None
    assert stub["is_stub"] is True
    assert stub["name"] == "Some Camp Body"
    assert stub["pos"] == "WR"


# --------------------------------------------------------------------------- the two-tier pool


def test_the_late_rounds_can_record_UNRANKED_players(tmp_path, cfg, pool):
    """The pool is two tiers and the late rounds live in the second one.

    Most of the real pool carries `is_ranked=False` and `value=0.0`, which is NOT an evaluation --
    those players exist so the board can RECORD them (CLAUDE.md). Round 12 of a real draft is
    full of such names, so drafting one has to work and leave the bookkeeping intact.
    """
    client = _client(tmp_path, cfg, pool)
    payload = client.get("/api/state").json()
    payload, _ = _draft_n(client, payload, 10)

    unranked = [r for r in _rows(payload) if not r["is_ranked"] and not r["drafted"]]
    assert unranked, "the synthetic pool is built with unranked players; this cannot be empty"
    target = unranked[0]
    assert target["value"] == 0.0, "an unranked player carries no valuation, by design"

    resp = client.post(
        "/api/pick",
        json={
            "player_id": target["player_id"],
            "pick_no": 11,
            "team_slot": payload["slot_on_clock"],
        },
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    _assert_consistent(payload, picks_made=11, cfg=cfg)

    recorded = next(p for p in payload["all_picks"] if p["pick_no"] == 11)
    assert recorded["player_id"] == target["player_id"]
    assert recorded["name"] == target["name"], (
        "an unranked pick must record the real NAME -- bookkeeping is the point of tier two"
    )


# --------------------------------------------------------------------------- missed picks


def test_a_missed_pick_is_reported_as_a_gap_and_closes_when_filled(tmp_path, cfg, pool):
    """Marc looks up, three picks have happened, and he only caught the last one.

    A gap is the honest state. It must be REPORTED, because the alternative -- a pick silently
    absent -- makes an already-drafted player look available, which is the worst thing this board
    can do in a live room.
    """
    client = _client(tmp_path, cfg, pool)
    payload = client.get("/api/state").json()
    payload, _ = _draft_n(client, payload, 8)

    # Two picks happen at the table while he is talking to someone: jump the clock past them.
    payload = client.post("/api/clock", json={"pick_no": 11}).json()
    assert payload["gaps"] == [9, 10], f"missed picks 9 and 10 must be reported: {payload['gaps']}"

    # He records pick 11 normally. The holes stay visible -- they are not papered over.
    target = _best_available(payload)
    payload = client.post(
        "/api/pick",
        json={
            "player_id": target["player_id"],
            "pick_no": 11,
            "team_slot": payload["slot_on_clock"],
        },
    ).json()
    assert payload["gaps"] == [9, 10], "recording a later pick must not erase the earlier holes"

    # Then he backfills them from the physical board, and the gaps close.
    for pick_no in (9, 10):
        fill = _best_available(payload)
        payload = client.post(
            "/api/pick",
            json={
                "player_id": fill["player_id"],
                "pick_no": pick_no,
                "team_slot": snake.slot_on_clock(cfg.teams, pick_no),
            },
        ).json()
    assert payload["gaps"] == [], "backfilled picks must close the gaps"
    _assert_consistent(payload, picks_made=11, cfg=cfg)


# --------------------------------------------------------------------------- source toggle


def test_a_source_change_can_be_interleaved_without_damaging_the_draft(tmp_path, cfg, pool):
    """Marc changes his mind about the board at round 4. The PICKS must not care.

    Scoped deliberately, and the scope was TIGHTENED after review. This proves three things: the
    toggle takes effect in the payload, it is recorded in the log with the key that was actually
    requested, and every pick survives it untouched. It does NOT prove the board rebuilds on
    resume -- an injected pool always wins over the log on construction (that is what keeps these
    tests hermetic, and `test_injected_pool_never_resumes_from_log` pins it), so a relaunch here
    cannot exercise the rebuild at all. That half lives in `tests/test_server.py`
    (`test_create_app_resumes_logged_source`, `test_source_changed_is_fsynced_before_the_served_pool_moves`).

    The earlier version of this test asserted neither the active source nor the logged key, so it
    would have passed against a `/api/source` that appended an event and switched nothing, or that
    recorded the wrong key entirely (Codex 2026-08-24 finding 3).
    """
    client = _client(tmp_path, cfg, pool)
    payload = client.get("/api/state").json()
    payload, taken = _draft_n(client, payload, 35)
    before_source = payload["active_source"]

    listed = client.get("/api/sources").json()
    keys = [
        s["key"]
        for s in listed.get("sources", [])
        if s.get("key") != before_source and s.get("available", True)
    ]
    if not keys:
        pytest.skip("only one projection source is offered for this pool")
    chosen = keys[0]

    resp = client.post("/api/source", json={"key": chosen})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["active_source"] == chosen, (
        f"asked for source {chosen!r} and the payload still reports "
        f"{payload['active_source']!r} -- a toggle that appends an event but switches nothing is "
        "exactly what this must catch"
    )
    # allow_unpooled: the switch REBUILT the pool, so these synthetic players are no longer on
    # the board. That is the honest behaviour and it is exactly why bookkeeping survives a source
    # change -- `all_picks` is replayed from the log, not read off the pool. See the helper.
    _assert_consistent(payload, picks_made=35, cfg=cfg, allow_unpooled=True)
    assert payload["_unpooled_picks"] == 35, (
        "every pick was made from the pre-switch pool, so all 35 should now be off-board -- if "
        "some are still found, this test is not exercising a real pool swap"
    )

    # The draft itself is untouched by a board change -- picks are facts, values are opinions.
    live = {p["pick_no"]: p["player_id"] for p in payload["all_picks"] if not p["voided"]}
    assert live == {n: pid for n, pid, _ in taken}

    # ...and drafting continues normally afterwards, on the NEW board.
    payload, _ = _draft_n(client, payload, 5, start=36)
    _assert_consistent(payload, picks_made=40, cfg=cfg, allow_unpooled=True)

    # The choice is in the log, with the key that was actually requested.
    changes = [
        ev for ev in EventLog(tmp_path / "draft.jsonl").events() if ev.type == "source_changed"
    ]
    assert len(changes) == 1, f"expected one source_changed event, found {len(changes)}"
    assert changes[0].payload.get("key") == chosen, (
        f"the log records source {changes[0].payload.get('key')!r}, not the requested {chosen!r}"
    )

    # A relaunch keeps the draft intact (the board-rebuild half is out of scope, see docstring).
    relaunched = _client(tmp_path, cfg, pool).get("/api/state").json()
    assert relaunched["current_pick"] == 41
    # The relaunch injects the synthetic pool again, so the five post-switch picks are the ones
    # off-board this time. The DRAFT is what has to survive, and it does.
    _assert_consistent(relaunched, picks_made=40, cfg=cfg, allow_unpooled=True)
    assert len([p for p in relaunched["all_picks"] if not p["voided"]]) == 40


# --------------------------------------------------------------------------- crash recovery


def test_a_relaunch_mid_draft_serves_exactly_the_board_it_left(tmp_path, cfg, pool):
    """Closed lid, dead battery, or a stray Ctrl-C at pick 63.

    The event log is the only durable state, so a second app on the same log must come back with
    the identical draft. Anything less and a mid-draft restart quietly loses picks in a room where
    nobody can tell.
    """
    first = _client(tmp_path, cfg, pool)
    payload = first.get("/api/state").json()
    payload, taken = _draft_n(first, payload, 63)
    before = payload

    second = _client(tmp_path, cfg, pool)  # same log_path
    after = second.get("/api/state").json()

    assert after["current_pick"] == before["current_pick"] == 64
    assert after["slot_on_clock"] == before["slot_on_clock"]
    assert after["gaps"] == before["gaps"] == []
    assert _core_pick_state(after) == _core_pick_state(before)
    _assert_consistent(after, picks_made=63, cfg=cfg)

    # And the resumed app can keep drafting, at the right pick, for the right team.
    resumed, _ = _draft_n(second, after, 5, start=64)
    _assert_consistent(resumed, picks_made=68, cfg=cfg)


def test_a_relaunch_after_an_undraft_does_not_resurrect_the_removed_pick(tmp_path, cfg, pool):
    """The undo/void distinction has to survive replay, or crash recovery undoes the correction.

    This is the pairing that matters: `undraft` appends ONE event precisely so a crash cannot
    tear it, and replay is what proves the single event was enough.
    """
    first = _client(tmp_path, cfg, pool)
    payload = first.get("/api/state").json()
    payload, taken = _draft_n(first, payload, 30)
    last_pick, last_player, _ = taken[-1]
    first.post("/api/undraft", json={"pick_no": last_pick})

    after = _client(tmp_path, cfg, pool).get("/api/state").json()

    assert after["current_pick"] == last_pick, "the rewound clock must survive a relaunch"
    row = next(r for r in _rows(after) if r["player_id"] == last_player)
    assert not row["drafted"], "the undrafted player came back to life on replay"
    _assert_consistent(after, picks_made=last_pick - 1, cfg=cfg)


# --------------------------------------------------------------------------- interleaved


def test_a_full_draft_with_corrections_INTERLEAVED_stays_consistent(tmp_path, cfg, pool):
    """The closest thing in this suite to the actual room.

    Every other test here exercises one messy path on an otherwise clean draft. Draft night is
    not like that: a mis-entered pick gets undrafted and replaced, someone points out that a pick
    was actually Jay's, a name gets recorded against the wrong team because two people spoke at
    once. The bugs this file exists to catch were all ORDERING bugs -- a clock left advanced, an
    event replayed out of sequence -- and ordering bugs only show up when operations interleave.

    So: a complete 150-pick draft with a perturbation roughly every 12 picks, every invariant
    checked after every mutation, and a cold replay compared against the live board at the end.
    The schedule is FIXED rather than random -- a soak that fails once a week on a seed nobody
    recorded is worse than no soak, and coverage grows by adding fixed cases.
    """
    client = _client(tmp_path, cfg, pool)
    total = cfg.teams * cfg.roster_size
    payload = client.get("/api/state").json()

    # Chosen to land in different rounds, and to put an undraft-and-replace immediately before a
    # snake turn, where a clock error does the most damage because the same team picks twice.
    UNDRAFT_AND_REPLACE = {19, 20, 61, 100, 139}
    REASSIGN = {33, 78, 121}
    OUT_OF_ORDER = {47, 94}
    UNRANKED = {131, 145}  # late-round write-in territory, per Codex's coverage suggestion

    expected: dict[int, tuple[str, int]] = {}
    perturbations = 0

    for pick_no in range(1, total + 1):
        on_clock = payload["slot_on_clock"]
        slot = on_clock
        if pick_no in OUT_OF_ORDER:
            # Recorded against the wrong team on purpose: it happens, and the board must take it
            # and FLAG it rather than refuse or silently accept.
            slot = (on_clock % cfg.teams) + 1
        if pick_no in UNRANKED:
            candidates = [r for r in _rows(payload) if not r["is_ranked"] and not r["drafted"]]
            assert candidates, "no unranked player left for the late-round case"
            target = candidates[0]
        else:
            target = _best_available(payload)

        resp = client.post(
            "/api/pick",
            json={"player_id": target["player_id"], "pick_no": pick_no, "team_slot": slot},
        )
        assert resp.status_code == 200, f"pick {pick_no} failed: {resp.text}"
        payload = resp.json()
        expected[pick_no] = (target["player_id"], slot)
        _assert_consistent(payload, picks_made=pick_no, cfg=cfg)
        if pick_no in OUT_OF_ORDER or pick_no in UNRANKED:
            perturbations += 1

        if pick_no in UNDRAFT_AND_REPLACE:
            # "No wait, that was the wrong guy." The newest pick, so the clock must rewind and
            # the replacement must land at the SAME number for the SAME team.
            resp = client.post("/api/undraft", json={"pick_no": pick_no})
            assert resp.status_code == 200, resp.text
            payload = resp.json()
            assert payload["last_undraft"]["mode"] == "undone"
            _assert_consistent(payload, picks_made=pick_no - 1, cfg=cfg)
            assert payload["slot_on_clock"] == slot, (
                f"after undrafting pick {pick_no} the clock must return to slot {slot}"
            )

            replacement = _best_available(payload)
            payload = client.post(
                "/api/pick",
                json={
                    "player_id": replacement["player_id"],
                    "pick_no": pick_no,
                    "team_slot": slot,
                },
            ).json()
            expected[pick_no] = (replacement["player_id"], slot)
            _assert_consistent(payload, picks_made=pick_no, cfg=cfg)
            perturbations += 1

        if pick_no in REASSIGN:
            # "That was actually Jay's pick." Ownership moves; the player does not.
            old_player, old_slot = expected[pick_no]
            new_slot = (old_slot % cfg.teams) + 1
            resp = client.post("/api/reassign", json={"pick_no": pick_no, "team_slot": new_slot})
            assert resp.status_code == 200, resp.text
            payload = resp.json()
            expected[pick_no] = (old_player, new_slot)
            moved = next(p for p in payload["all_picks"] if p["pick_no"] == pick_no)
            assert moved["team_slot"] == new_slot
            assert moved["player_id"] == old_player, "a reassign must not touch the player"
            _assert_consistent(payload, picks_made=pick_no, cfg=cfg)
            perturbations += 1

    assert perturbations == (
        len(UNDRAFT_AND_REPLACE) + len(REASSIGN) + len(OUT_OF_ORDER) + len(UNRANKED)
    )

    # The live board matches what we believe we entered, pick by pick.
    live = {
        p["pick_no"]: (p["player_id"], p["team_slot"])
        for p in payload["all_picks"]
        if not p["voided"]
    }
    assert live == expected, "the live board disagrees with the sequence of entered picks"

    # And a cold replay matches it too -- through 12 perturbations, including five undo events
    # that have to drop their own pick during replay, and flags that replay DERIVES.
    replayed = _replay_state(tmp_path, cfg)
    assert replayed.current_pick == payload["current_pick"]
    assert _core_pick_state_replayed(replayed) == _core_pick_state(payload), (
        "crash recovery would serve a different draft than the one on screen"
    )

    # Rosters still reconcile, from the server's payload, after three reassignments.
    assert sum(len(t["roster"]) for t in payload["opponents"]) == total
    assert payload["_unpooled_picks"] == 0, "the pool was constant; the strict check must have run"


# --------------------------------------------------------------------------- real data (additive)


def test_a_complete_draft_against_the_REAL_cached_pool(tmp_path, cfg, real_pool):
    """The same soak against real data. ADDITIVE -- may skip, and nothing depends on it.

    Every bookkeeping guarantee above is pinned on a pool that cannot go missing. This one exists
    for end-to-end confidence in what will actually run on draft night: the real ~980-player
    two-tier pool, its real ADP ordering, and whatever the current cache happens to contain.
    """
    client = _client(tmp_path, cfg, real_pool)
    total = cfg.teams * cfg.roster_size
    payload = client.get("/api/state").json()

    for pick_no in range(1, total + 1):
        target = _best_available(payload)
        resp = client.post(
            "/api/pick",
            json={
                "player_id": target["player_id"],
                "pick_no": pick_no,
                "team_slot": payload["slot_on_clock"],
            },
        )
        assert resp.status_code == 200, f"pick {pick_no} failed: {resp.text}"
        payload = resp.json()
        _assert_consistent(payload, picks_made=pick_no, cfg=cfg)

    assert payload["_unpooled_picks"] == 0, "the pool was constant; the strict check must have run"
    replayed = _replay_state(tmp_path, cfg)
    assert _core_pick_state_replayed(replayed) == _core_pick_state(payload)
    for team in payload["opponents"]:
        assert len(team["roster"]) == cfg.roster_size
