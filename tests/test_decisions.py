"""Tests for the review queue's persistence half (``valuation/decisions.py``).

The properties that matter here are about DISCIPLINE, not arithmetic, and they are modelled on
what ``data/overrides.csv`` already guarantees: checked first, permanent, auditable,
hand-editable. Specifically:

* a MISSING file means no decisions and must never break a board build -- but an existing file
  that is empty is a truncated write, not a decision to reject nothing, and must raise;
* a malformed file must FAIL LOUDLY and name the bad entry -- a rejection Marc made and the
  loader quietly dropped would be invisible on the board;
* ``player_id`` is required even when null, so a dropped line cannot silently widen a
  one-player rejection into a source-wide one;
* a ``keep`` changes no number, ever;
* the ``rejected`` set is the exact type ``blend_statlines`` already accepts;
* and the round trip works end to end: a decision really does change a value on the board, and an
  empty decisions file really does change nothing.
"""

from __future__ import annotations

import json

import pytest

from draftroom.prep.schema import CANONICAL_STATS, StatLine
from draftroom.valuation import decisions as D
from draftroom.valuation.composite import blend_statlines

TODAY = "2026-08-20"


def entry(**kw):
    base = {
        "source": "sleeper",
        "stat": "rec_yd",
        "player_id": "4035",
        "verdict": "reject",
        "reason": "all-zero statline carried with games=18",
        "date": TODAY,
    }
    base.update(kw)
    return base


# ------------------------------------------------------------------------------- loading


def test_a_missing_file_means_no_decisions(tmp_path):
    """The ordinary state before Marc has reviewed anything. It must not raise."""
    assert D.load_decisions(tmp_path / "nope.json") == ()


def test_an_existing_but_empty_file_is_an_error_not_no_decisions(tmp_path):
    """The counterpart to the test above, and the asymmetry between them is the safety property.

    A file that does not exist is the ordinary pre-review state (above). A file that EXISTS and
    is empty is what a truncated write or an interrupted hand-edit looks like, and reading it as
    "no decisions" silently un-applied every rejection Marc had made -- a board that looks fine
    while quietly disagreeing with him (Codex 2026-08-21 finding 4). Only the missing case may
    fail open.
    """
    whitespace_only = tmp_path / "d.json"
    whitespace_only.write_text("   \n", encoding="utf-8")
    with pytest.raises(D.DecisionsFileError, match="empty"):
        D.load_decisions(whitespace_only)

    zero_bytes = tmp_path / "zero.json"
    zero_bytes.write_bytes(b"")
    with pytest.raises(D.DecisionsFileError, match="empty"):
        D.load_decisions(zero_bytes)


def test_both_the_object_form_and_a_bare_list_load(tmp_path):
    """The object form is what save_decisions writes; the bare list is what a hand-edit and the
    review page's clipboard export produce. Refusing either would make the file unfriendly to
    the exact workflow it exists for."""
    listed = tmp_path / "list.json"
    listed.write_text(json.dumps([entry()]), encoding="utf-8")
    objected = tmp_path / "obj.json"
    objected.write_text(json.dumps({"schema": 1, "decisions": [entry()]}), encoding="utf-8")
    assert D.load_decisions(listed) == D.load_decisions(objected)


@pytest.mark.parametrize(
    "bad, needle",
    [
        (entry(verdict="boot"), "not one of"),
        (entry(source="rotoworld"), "unknown source"),
        (entry(stat="touchdowns"), "not a canonical stat"),
        (entry(reason=""), "empty reason"),
        (entry(date=""), "empty date"),
    ],
)
def test_a_malformed_entry_fails_loudly_and_names_itself(tmp_path, bad, needle):
    p = tmp_path / "d.json"
    p.write_text(json.dumps([bad]), encoding="utf-8")
    with pytest.raises(D.DecisionsFileError) as exc:
        D.load_decisions(p)
    message = str(exc.value)
    assert needle in message
    assert "entry #0" in message
    # The offending entry itself must be in the message, or "fix it in the file" is not actionable.
    assert "sleeper" in message


def test_a_missing_required_field_names_the_field(tmp_path):
    p = tmp_path / "d.json"
    broken = entry()
    del broken["reason"]
    p.write_text(json.dumps([broken]), encoding="utf-8")
    with pytest.raises(D.DecisionsFileError, match="missing required field"):
        D.load_decisions(p)


def test_an_omitted_player_id_is_an_error_and_explicit_null_is_source_wide():
    """``player_id`` must be present even when null, which REVERSES the earlier rule here.

    The old rule read an omitted key as source-wide, reasoning that ``null`` is a meaningful
    value so absence cannot be distinguished from a typo. That is true, and it resolved the
    ambiguity the expensive way round: this file is hand-editable by design, so a dropped line
    silently promoted ONE player's rejection into a rejection for every player that source
    publishes (Codex 2026-08-21 finding 4). Requiring the key costs one ``"player_id": null``
    and removes the failure mode entirely.
    """
    without = entry()
    del without["player_id"]
    with pytest.raises(D.DecisionsFileError, match="player_id"):
        D.parse_decisions([without])

    (decision,) = D.parse_decisions([entry(player_id=None)])
    assert decision.player_id is None
    assert decision.is_source_wide


def test_save_decisions_always_writes_player_id_so_a_round_trip_survives(tmp_path):
    """The writer must satisfy the reader's requirement -- including in the source-wide case,
    whose value is JSON null and is exactly what an omit-if-None serializer would drop."""
    p = tmp_path / "d.json"
    source_wide = D.parse_decisions([entry(player_id=None)])
    D.save_decisions(source_wide, p)
    assert '"player_id": null' in p.read_text(encoding="utf-8")
    assert D.load_decisions(p) == source_wide


def test_an_unknown_schema_version_refuses_to_guess():
    with pytest.raises(D.DecisionsFileError, match="schema"):
        D.parse_decisions({"schema": 99, "decisions": []})


def test_bad_json_says_so(tmp_path):
    p = tmp_path / "d.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(D.DecisionsFileError, match="not valid JSON"):
        D.load_decisions(p)


# ------------------------------------------------------------------------------ the rejected set


def test_a_keep_never_changes_a_number():
    """The whole safety property of the file: a reviewed-and-accepted outlier is recorded so it
    stops coming back to the top of the queue, and recording it must not remove anything."""
    index = D.rejected_index(D.parse_decisions([entry(verdict="keep")]))
    assert index.is_empty
    assert index.for_player("4035") == frozenset()


def test_the_rejected_set_is_the_type_blend_statlines_accepts():
    index = D.rejected_index(D.parse_decisions([entry()]))
    pairs = index.for_player("4035")
    assert isinstance(pairs, frozenset)
    assert pairs == frozenset({("sleeper", "rec_yd")})
    # And it really is accepted, unchanged, by the composite.
    blended, prov = blend_statlines(
        {"sleeper": StatLine(rec_yd=200.0), "espn": StatLine(rec_yd=800.0)},
        pos="WR",
        rejected=pairs,
    )
    assert blended.rec_yd == pytest.approx(800.0)
    assert prov.rejected_applied == (("sleeper", "rec_yd"),)


def test_a_per_player_rejection_does_not_leak_onto_other_players():
    index = D.rejected_index(D.parse_decisions([entry()]))
    assert index.for_player("4035") == frozenset({("sleeper", "rec_yd")})
    assert index.for_player("9999") == frozenset()


def test_a_source_wide_rejection_applies_to_everyone():
    index = D.rejected_index(D.parse_decisions([entry(player_id=None)]))
    assert index.for_player("4035") == frozenset({("sleeper", "rec_yd")})
    assert index.for_player("9999") == frozenset({("sleeper", "rec_yd")})
    assert index.source_wide == frozenset({("sleeper", "rec_yd")})


def test_the_star_sentinel_expands_to_every_canonical_stat():
    index = D.rejected_index(D.parse_decisions([entry(stat=D.ALL_STATS)]))
    pairs = index.for_player("4035")
    assert pairs == frozenset(("sleeper", s) for s in CANONICAL_STATS)


def test_a_later_decision_on_the_same_key_wins():
    """Re-deciding is legitimate -- reject in August, keep in September. The file must not grow
    two contradictory lines for one key."""
    first = D.parse_decisions([entry(verdict="reject", reason="looked wrong")])
    second = D.parse_decisions([entry(verdict="keep", reason="checked the depth chart", date="2026-09-01")])
    merged = D.merge_decisions(first, second)
    assert len(merged) == 1
    assert merged[0].verdict == "keep"
    assert D.rejected_index(merged).is_empty


def test_every_rejection_keeps_its_reason_for_the_board_badge():
    """A rejection Marc cannot see on the board is a silent edit. The reason has to survive all
    the way to whatever renders the badge."""
    index = D.rejected_index(D.parse_decisions([entry()]))
    applied = index.decisions_for("4035")
    assert len(applied) == 1
    assert applied[0].reason == "all-zero statline carried with games=18"
    assert applied[0].date == TODAY


def test_save_then_load_round_trips_every_field(tmp_path):
    p = tmp_path / "d.json"
    original = D.parse_decisions(
        [
            entry(),
            entry(source="espn", stat="pass_td", player_id=None, verdict="keep",
                  reason="level bias, accepted for now", detector="td_source_bias"),
        ]
    )
    D.save_decisions(original, p)
    assert D.load_decisions(p) == original
    # Written in the documented object form, with the note a human reads first.
    payload = json.loads(p.read_text(encoding="utf-8"))
    assert payload["schema"] == D.SCHEMA_VERSION
    assert "NOTHING is ever added here automatically" in payload["_note"]


# --------------------------------------------------------- the end-to-end round trip (gate 4)


def _mini_board():
    """A three-source board small enough to check by hand, built the way the board builds it."""
    from draftroom.config import LeagueConfig
    from draftroom.prep.scoring import score_statline_with_bonus
    from draftroom.validate import board as board_mod
    from draftroom.valuation.evob import compute_draft_values
    from draftroom.valuation.replacement import PlayerSeason

    cfg = LeagueConfig(
        teams=10,
        starters={"QB": 2, "RB": 2, "WR": 3, "TE": 1},
        flex_slots=1,
        flex_eligible=frozenset({"RB", "WR", "TE"}),
        bench=6,
        weeks=17,
        scoring={"pass_yd": 0.04, "pass_td": 4.0, "pass_int": -2.0, "rush_yd": 0.1,
                 "rush_td": 6.0, "rec": 0.5, "rec_yd": 0.1, "rec_td": 6.0, "fum_lost": -2.0},
    )
    pos_of: dict[str, str] = {}
    statlines: dict[str, dict[str, StatLine]] = {"sleeper": {}, "espn": {}, "fantasypros": {}}
    for i in range(1, 41):
        for pos, line in (
            ("WR", StatLine(rec=95.0 - i, rec_yd=1350.0 - 25.0 * i, rec_td=9.0 - 0.15 * i)),
            ("RB", StatLine(rush_att=290.0 - 5.0 * i, rush_yd=1300.0 - 25.0 * i,
                            rush_td=11.0 - 0.2 * i, rec=40.0 - 0.5 * i, rec_yd=340.0 - 6.0 * i)),
        ):
            pid = f"{pos}{i}"
            pos_of[pid] = pos
            from dataclasses import replace as _r

            statlines["sleeper"][pid] = _r(line, games=17.0)
            statlines["espn"][pid] = _r(line, games=17.0)
            # FantasyPros runs systematically high on receiving volume -- a measured property of
            # the real source (docs/PROJECTION_CHALLENGES.md: 5-10% above the 2025 median), and
            # what makes a SOURCE-WIDE rejection a different event from a per-player one.
            statlines["fantasypros"][pid] = _r(line, rec_yd=line.rec_yd * 1.08)
    for i in range(1, 31):
        pid = f"QB{i}"
        pos_of[pid] = "QB"
        line = StatLine(pass_att=580.0 - 5.0 * i, pass_cmp=380.0 - 4.0 * i,
                        pass_yd=4600.0 - 60.0 * i, pass_td=34.0 - 0.5 * i, pass_int=10.0)
        from dataclasses import replace as _r2

        statlines["sleeper"][pid] = _r2(line, games=17.0)
        statlines["espn"][pid] = _r2(line, games=17.0)
        statlines["fantasypros"][pid] = line
    for i in range(1, 21):
        pid = f"TE{i}"
        pos_of[pid] = "TE"
        line = StatLine(rec=90.0 - 3.0 * i, rec_yd=1050.0 - 40.0 * i, rec_td=8.0 - 0.3 * i)
        from dataclasses import replace as _r3

        statlines["sleeper"][pid] = _r3(line, games=17.0)
        statlines["espn"][pid] = _r3(line, games=17.0)
        statlines["fantasypros"][pid] = line

    # The number under test: Sleeper is far low on one receiver's yardage.
    from dataclasses import replace as _r4

    statlines["sleeper"]["WR1"] = _r4(statlines["sleeper"]["WR1"], rec_yd=300.0)

    def build(rejected_index: D.RejectedIndex):
        seasons = []
        for pid, pos in pos_of.items():
            line, _prov = blend_statlines(
                {s: lines.get(pid) for s, lines in statlines.items()},
                pos=pos,
                games_sources=frozenset({"sleeper", "espn"}),
                rejected=rejected_index.for_player(pid),
            )
            divisor = board_mod._games_divisor(line, cfg)
            points = score_statline_with_bonus(
                line.as_dict(), cfg.scoring, pos=pos, games=divisor
            )
            seasons.append(
                PlayerSeason(
                    player_id=pid, pos=pos, ppg=points / divisor,
                    expected_games=(line.games if line.games > 0 else None), name=pid,
                )
            )
        capped = board_mod._cap_expected_games_by_curve(seasons, cfg)
        return compute_draft_values(capped, cfg)

    return build


def test_round_trip_a_decision_actually_changes_a_value_on_the_board(tmp_path):
    """candidates -> decisions JSON -> the rejected set -> a board built with it.

    This is the gate the whole feature stands on: if the file does not move a number, the review
    page is theatre.
    """
    build = _mini_board()
    before = build(D.rejected_index(()))

    path = tmp_path / "projection_decisions.json"
    D.save_decisions(
        D.parse_decisions(
            [
                {
                    "source": "sleeper",
                    "stat": "rec_yd",
                    "player_id": "WR1",
                    "player_name": "WR1",
                    "verdict": "reject",
                    "reason": "sleeper's 300 rec_yd is 78% below the 1325 median of the others",
                    "date": TODAY,
                    "detector": "distance",
                }
            ]
        ),
        path,
    )

    after = build(D.rejected_index(D.load_decisions(path)))
    assert after["WR1"].dv > before["WR1"].dv, "dropping the low source must raise the value"
    assert after["WR1"].ppg > before["WR1"].ppg
    # And nobody else's PPG moved: the decision is per player, not per source.
    for pid in before:
        if pid != "WR1":
            assert after[pid].ppg == pytest.approx(before[pid].ppg)


def test_round_trip_an_empty_decisions_file_changes_nothing(tmp_path):
    """Opening the review page and clicking nothing must leave the board bit-for-bit identical."""
    build = _mini_board()
    before = build(D.rejected_index(()))

    path = tmp_path / "projection_decisions.json"
    D.save_decisions((), path)
    after = build(D.rejected_index(D.load_decisions(path)))

    assert {pid: dv.dv for pid, dv in after.items()} == {
        pid: dv.dv for pid, dv in before.items()
    }


def test_round_trip_a_keep_only_file_changes_nothing(tmp_path):
    build = _mini_board()
    before = build(D.rejected_index(()))
    path = tmp_path / "projection_decisions.json"
    D.save_decisions(
        D.parse_decisions([entry(player_id="WR1", verdict="keep", reason="checked, it is right")]),
        path,
    )
    after = build(D.rejected_index(D.load_decisions(path)))
    assert {pid: dv.dv for pid, dv in after.items()} == {
        pid: dv.dv for pid, dv in before.items()
    }


def test_round_trip_a_source_wide_rejection_moves_the_whole_board(tmp_path):
    build = _mini_board()
    before = build(D.rejected_index(()))
    path = tmp_path / "projection_decisions.json"
    D.save_decisions(
        D.parse_decisions(
            [
                {
                    "source": "fantasypros",
                    "stat": "rec_yd",
                    "player_id": None,
                    "verdict": "reject",
                    "reason": "source-wide level bias",
                    "date": TODAY,
                }
            ]
        ),
        path,
    )
    after = build(D.rejected_index(D.load_decisions(path)))
    moved = [pid for pid in before if after[pid].ppg != pytest.approx(before[pid].ppg)]
    assert len(moved) > 1
