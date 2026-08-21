"""The review queue's decisions, wired through board -> pool -> payload.

`candidates.py` and `decisions.py` are tested on their own. What these tests cover is the
INTEGRATION, and specifically one bug that a green suite on either side would not have caught:
a source-wide rejection is in force for every player but only CHANGES the value of players who
actually had a number from that source for that stat. Badging everyone would put a REJ on all
188 rows and train Marc to ignore the badge, which is worse than not having it.

Read only cached data; no network.
"""

from __future__ import annotations

import json

import pytest

from draftroom.valuation.decisions import (
    Decision,
    DecisionsFileError,
    load_decisions,
    rejected_index,
    save_decisions,
)


def _reject(source: str, stat: str, player_id: str | None = None) -> Decision:
    return Decision(
        source=source,
        stat=stat,
        player_id=player_id,
        verdict="reject",
        reason="test",
        date="2026-08-20",
        detector="test",
    )


# --------------------------------------------------------------------------- scoping


def test_source_wide_rejection_is_in_force_for_everyone():
    """`for_player` is the "what rules apply" question, and a source-wide rule applies to all."""
    idx = rejected_index([_reject("fantasysharks", "pass_td")])
    assert ("fantasysharks", "pass_td") in idx.for_player("anyone")
    assert ("fantasysharks", "pass_td") in idx.for_player("someone-else")


def test_player_specific_rejection_does_not_leak_to_other_players():
    idx = rejected_index([_reject("sleeper", "rec_yd", player_id="123")])
    assert ("sleeper", "rec_yd") in idx.for_player("123")
    assert idx.for_player("456") == frozenset()


def test_keep_decisions_never_reject_anything():
    """A keep is the ABSENCE of a rejection. Materialising it as anything else would let a
    'keep' silently remove a number, which is the exact inversion of Marc's intent."""
    keep = Decision(
        source="espn", stat="pass_td", player_id=None, verdict="keep",
        reason="checked, fine", date="2026-08-20", detector="td_source_bias",
    )
    assert rejected_index([keep]).is_empty


# --------------------------------------------------------------------------- file handling


def test_missing_decisions_file_means_no_decisions(tmp_path):
    """The ordinary state. Marc has not adjudicated anything yet and the board must build."""
    assert load_decisions(tmp_path / "nope.json") == ()


def test_malformed_decisions_file_fails_loudly_and_names_the_entry(tmp_path):
    """Every other optional input in the board build degrades to "contributes nothing" on a bad
    cache. A decisions file must NOT: degrading would silently stop applying rejections Marc
    made deliberately, and the board would look fine while quietly overruling him."""
    p = tmp_path / "projection_decisions.json"
    p.write_text(
        json.dumps({"decisions": [{"source": "espn", "verdict": "reject"}]}), encoding="utf-8"
    )
    with pytest.raises(DecisionsFileError) as exc:
        load_decisions(p)
    assert "0" in str(exc.value) or "stat" in str(exc.value), (
        f"the error must identify the offending entry, got: {exc.value}"
    )


def test_decisions_round_trip_through_the_file(tmp_path):
    p = tmp_path / "projection_decisions.json"
    original = (_reject("fantasysharks", "pass_td"), _reject("sleeper", "rec_yd", "999"))
    save_decisions(original, p)
    assert load_decisions(p) == original


# --------------------------------------------------------------------------- the real board


@pytest.mark.parametrize("_", [None])
def test_a_rejection_badges_only_players_it_actually_changed(tmp_path, monkeypatch, _):
    """THE regression test for the integration bug.

    Rejecting `(fantasysharks, pass_td)` source-wide is in force for all 188 board players but
    alters only the quarterbacks who have a FantasySharks passing-TD figure. The first wiring
    recorded it against all 188 (via `decisions_for`, which correctly answers a different
    question); it now filters on `BlendProvenance.rejected_applied`, which is already narrowed
    to pairs that genuinely removed a contribution.
    """
    from draftroom.valuation import decisions as decisions_mod
    from draftroom.validate import board as board_mod

    p = tmp_path / "projection_decisions.json"
    save_decisions((_reject("fantasysharks", "pass_td"),), p)
    monkeypatch.setattr(decisions_mod, "DECISIONS_PATH", p, raising=False)
    monkeypatch.setattr(board_mod, "load_decisions", lambda: load_decisions(p))

    rb = board_mod.build_real_board()
    badged = set(rb.applied_decisions)

    assert badged, "a source-wide pass_td rejection must badge SOMEBODY"
    assert len(badged) < len(rb.players) / 2, (
        f"badged {len(badged)} of {len(rb.players)} players -- a pass_td rejection cannot have "
        "changed a receiver's value; this is the all-188 bug returning"
    )
    pos_by_pid = {pl.player_id: pl.pos for pl in rb.players}
    assert {pos_by_pid.get(pid) for pid in badged} == {"QB"}, (
        "only quarterbacks carry a passing-TD number, so only they can be badged for one"
    )


def test_an_empty_decisions_file_changes_no_value_on_the_real_board(monkeypatch, tmp_path):
    """The safety property: adding the review-queue machinery must not move the board until Marc
    actually decides something."""
    from draftroom.validate import board as board_mod

    monkeypatch.setattr(board_mod, "load_decisions", lambda: ())
    baseline = {p.player_id: p.dv for p in board_mod.build_real_board().players}

    save_decisions((), tmp_path / "projection_decisions.json")
    monkeypatch.setattr(
        board_mod, "load_decisions", lambda: load_decisions(tmp_path / "projection_decisions.json")
    )
    after = {p.player_id: p.dv for p in board_mod.build_real_board().players}

    assert baseline.keys() == after.keys()
    assert all(baseline[k] == after[k] for k in baseline), "an empty file must change nothing"


def test_single_source_boards_ignore_rejections(monkeypatch, tmp_path):
    """A single-source board scores that source's statline UNMODIFIED -- that is the whole point
    of the toggle. So a rejection must not apply there, and no badge may claim it did."""
    from draftroom.validate import board as board_mod

    p = tmp_path / "projection_decisions.json"
    save_decisions((_reject("fantasysharks", "pass_td"),), p)
    monkeypatch.setattr(board_mod, "load_decisions", lambda: load_decisions(p))

    rb = board_mod.build_real_board(source="fantasysharks")
    assert rb.applied_decisions == {}, (
        "the FantasySharks-only board must show FantasySharks as it is, rejections included"
    )


def test_a_whole_statline_rejection_is_badged(monkeypatch, tmp_path):
    """A `stat: "*"` decision is the LARGEST kind of rejection and was the one the badge missed.

    `rejected_index` expands the `"*"` sentinel before the composite sees it, so
    `BlendProvenance.rejected_applied` holds concrete pairs while the `Decision` still reads
    `"*"`. Matching on the raw `d.stat` therefore silently found nothing: the value moved and no
    badge appeared -- exactly what this badge exists to prevent. Match on `d.stats` instead.
    """
    from draftroom.validate import board as board_mod

    p = tmp_path / "projection_decisions.json"
    save_decisions((_reject("fantasysharks", "*"),), p)
    monkeypatch.setattr(board_mod, "load_decisions", lambda: load_decisions(p))

    rb = board_mod.build_real_board()
    assert rb.applied_decisions, (
        "a whole-statline rejection moves values, so it MUST badge the players it moved"
    )

    # Every badged player must genuinely have moved...
    monkeypatch.setattr(board_mod, "load_decisions", lambda: ())
    baseline = {pl.player_id: pl.dv for pl in board_mod.build_real_board().players}
    after = {pl.player_id: pl.dv for pl in rb.players}
    moved = {pid for pid, dv in baseline.items() if abs(dv - after.get(pid, dv)) > 1e-9}
    badged = set(rb.applied_decisions)
    assert badged <= moved, "a badge must never claim a change that did not happen"

    # ...but NOT every mover is badged, and that asymmetry is correct rather than a gap.
    # `dv = (ppg - baseline_ppg) * games`, so removing a source changes the positional
    # REPLACEMENT level too, which nudges every player at that position -- including players
    # FantasySharks never published. Those moved without any number of their own being touched,
    # and badging them would point at a decision that did nothing to them.
    unbadged_movers = moved - badged
    sharks = board_mod._resolve_fantasysharks_statlines(
        board_mod.build_crosswalk(
            board_mod.load_latest_raw("sleeper"),
            board_mod.parse_adp_rows(board_mod.load_latest_raw("ffc")),
        )
    )
    assert all(pid not in sharks for pid in unbadged_movers), (
        "a player who moved AND had a FantasySharks number must be badged; only baseline-shift "
        f"movers may go unbadged, but these had one: {unbadged_movers & set(sharks)}"
    )
