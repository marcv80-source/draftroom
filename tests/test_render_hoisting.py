"""A bullet true of every candidate belongs to the board, not to each player.

Each candidate gets three lines on screen. If all three are facts that apply equally to every other
option, they have told Marc nothing about which one to take, while crowding out whatever actually
separates them. So identical bullets get hoisted into the board-level warnings and said once.
"""

from __future__ import annotations

from draftroom.explain import primitives as prim
from draftroom.explain.render import render


def _candidate(pid: str, name: str, pos: str, *, p_survive: float, tier_gap: float,
               teams_needing: int = 0, teams_before: int = 8) -> prim.Candidate:
    return prim.Candidate(
        player_id=pid,
        name=name,
        pos=pos,
        team="XXX",
        bye=7,
        draft_value=100.0,
        projected_points=200.0,
        floor=180.0,
        ceiling=220.0,
        utility=100.0,
        tier=prim.TierCliff(
            tier_index=1,
            tier_size_remaining=4,
            points_to_next_tier=tier_gap,
            exhaustion_pick=None,
        ),
        survival=prim.SurvivalInfo(
            next_pick=33,
            next_pick_label="3.09",
            p_survive_next=p_survive,
        ),
        depth=prim.PositionDepth(
            position=pos,
            startable_remaining=20,
            league_demand_remaining=5,
        ),
        vona=1.0,
        opponent_pressure=prim.OpponentPressure(
            position=pos,
            teams_before_next_turn=teams_before,
            teams_needing_position=teams_needing,
        ),
    )


def _rec(candidates) -> prim.Recommendation:
    return prim.Recommendation(
        pick_no=16,
        pick_label="2.04",
        on_the_clock=9,
        is_my_pick=True,
        candidates=tuple(candidates),
    )


def test_identical_bullet_is_hoisted_off_every_candidate():
    """Three players nobody survives to the next turn. Say it once, at the top."""
    cands = [
        _candidate("a", "A", "QB", p_survive=0.004, tier_gap=12.0),
        _candidate("b", "B", "QB", p_survive=0.004, tier_gap=12.0),
        _candidate("c", "C", "QB", p_survive=0.004, tier_gap=12.0),
    ]
    rec = render(_rec(cands))

    shared_line = "<1% chance he's there at 3.09. Take him now or lose him."
    assert any(shared_line in w for w in rec.warnings), rec.warnings
    for c in rec.candidates:
        assert all(shared_line not in b for b in c.bullets), c.bullets


def test_a_bullet_that_differs_stays_on_the_candidate():
    """Different survival odds are exactly what distinguishes options, so they must not be hoisted."""
    cands = [
        _candidate("a", "A", "QB", p_survive=0.004, tier_gap=12.0),
        _candidate("b", "B", "QB", p_survive=0.90, tier_gap=12.0),
    ]
    rec = render(_rec(cands))

    joined = " | ".join(b for c in rec.candidates for b in c.bullets)
    assert "3.09" in joined
    # Neither survival line is board-wide, so nothing about survival got hoisted.
    assert not any("chance he's there" in w or "still there" in w for w in rec.warnings), rec.warnings


def test_single_candidate_hoists_nothing():
    """With one option there is nothing to distinguish it from, so it keeps all its own lines."""
    rec = render(_rec([_candidate("a", "A", "QB", p_survive=0.004, tier_gap=12.0)]))
    assert rec.warnings == ()
    assert rec.candidates[0].bullets


def test_hoisting_frees_a_slot_for_a_distinguishing_line():
    """The point of hoisting: each candidate spends its three lines on what is true of that player.

    Both share a survival line; only one has a bye collision. After hoisting, the bye line has room.
    """
    a = _candidate("a", "A", "WR", p_survive=0.004, tier_gap=12.0, teams_needing=8)
    b = _candidate("b", "B", "WR", p_survive=0.004, tier_gap=12.0, teams_needing=8)
    b.flags = ("BYE_COLLISION",)

    rec = render(_rec([a, b]))
    b_out = rec.candidates[1]
    assert any("bye" in bullet.lower() for bullet in b_out.bullets), b_out.bullets


def test_fallbacks_always_show_a_sign():
    """An unsigned '0.7' next to a '-4.2' reads as a typo, and direction is the whole point."""
    c = _candidate("a", "A", "QB", p_survive=0.5, tier_gap=12.0)
    c.fallbacks = (
        prim.Fallback(player_id="x", name="Better Guy", pos="QB", points_behind=-0.7,
                      p_survive_next=0.4),
        prim.Fallback(player_id="y", name="Worse Guy", pos="QB", points_behind=4.2,
                      p_survive_next=0.8),
    )
    rec = render(_rec([c]))
    line = [b for b in rec.candidates[0].bullets if b.startswith("Don't like him")][0]
    assert "+0.7" in line, line
    assert "-4.2" in line, line
