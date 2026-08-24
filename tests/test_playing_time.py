"""The playing-time override file: parsing, the fail-closed rule, and the clamp semantics.

Companion to ``test_playing_time_wiring.py``, which pins what an override does to a real board.
This file is about the FILE and the RULE -- everything provable without touching cached data.
"""

from __future__ import annotations

import json

import pytest

from draftroom.valuation.playing_time import (
    OVERRIDES_PATH,
    REQUIRED_FIELDS,
    PlayingTimeFileError,
    PlayingTimeOverride,
    bind,
    load_overrides,
    merge_overrides,
    new_override,
    overrides_by_pid,
    parse_overrides,
    save_overrides,
)


def _entry(**kw):
    base = {
        "player_id": "8112",
        "player_name": "Alec Pierce",
        "games": 11.0,
        "reason": "PUP, not expected back before ~week 5",
        "date": "2026-08-24",
    }
    base.update(kw)
    return base


# ------------------------------------------------------------------ shapes accepted


def test_dict_form_parses():
    got = parse_overrides({"schema": 1, "overrides": [_entry()]})
    assert len(got) == 1
    assert got[0].player_id == "8112"
    assert got[0].games == 11.0
    assert got[0].reason.startswith("PUP")


def test_bare_list_form_parses():
    """What a hand-edit eventually looks like, so it must be accepted."""
    assert len(parse_overrides([_entry()])) == 1


def test_raw_json_text_parses():
    assert len(parse_overrides(json.dumps({"schema": 1, "overrides": [_entry()]}))) == 1


def test_round_trip_through_save_normalises_to_the_dict_form(tmp_path):
    p = tmp_path / "pt.json"
    save_overrides(parse_overrides([_entry()]), p)
    payload = json.loads(p.read_text(encoding="utf-8"))
    assert payload["schema"] == 1
    assert payload["_note"]  # the file explains itself to whoever opens it
    assert [o["player_id"] for o in payload["overrides"]] == ["8112"]
    assert load_overrides(p)[0].games == 11.0


# ------------------------------------------------------------------ rejected entries


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_every_required_field_is_required(field):
    entry = _entry()
    del entry[field]
    with pytest.raises(PlayingTimeFileError) as exc:
        parse_overrides([entry])
    assert field in str(exc.value)


def test_null_player_id_is_refused_rather_than_reinterpreted():
    """decisions.py gives `null` a meaning (source-wide). Availability has no such grain.

    The two files sit side by side and look alike, so the shape that is meaningful in one and
    meaningless in the other must fail loudly instead of being guessed at.
    """
    with pytest.raises(PlayingTimeFileError) as exc:
        parse_overrides([_entry(player_id=None)])
    assert "source-wide" in str(exc.value)


@pytest.mark.parametrize("blank_pid", ["   ", "", " null ", "NULL", "None"])
def test_whitespace_or_null_text_player_id_is_refused(blank_pid):
    """Normalize BEFORE validating. Checking emptiness first let "   " through as an id, which
    then matched no board player and degraded to a warning -- the judgement never applied
    (Codex 2026-08-24 finding 4)."""
    with pytest.raises(PlayingTimeFileError):
        parse_overrides([_entry(player_id=blank_pid)])


@pytest.mark.parametrize("bad_pid", [8142.0, ["8142"], {"id": "8142"}, True])
def test_non_scalar_player_id_is_refused(bad_pid):
    """A float id stringifies to "8142.0" and matches nothing; a list stringifies to something
    that looks like an id and is not one."""
    with pytest.raises(PlayingTimeFileError) as exc:
        parse_overrides([_entry(player_id=bad_pid)])
    assert "string or integer" in str(exc.value)


def test_integer_player_id_is_accepted_and_normalised():
    """JSON hand-edits write ids unquoted often enough that refusing would be hostile."""
    assert parse_overrides([_entry(player_id=8142)])[0].player_id == "8142"


@pytest.mark.parametrize("field", ["reason", "date"])
def test_null_reason_or_date_is_refused_not_stringified(field):
    """`str(None)` is the NONEMPTY string "None", so a null sailed past the emptiness check and
    applied a valuation change with an unusable audit trail (Codex 2026-08-24 finding 4)."""
    with pytest.raises(PlayingTimeFileError) as exc:
        parse_overrides([_entry(**{field: None})])
    assert "must be a string" in str(exc.value)


def test_negative_games_is_refused():
    with pytest.raises(PlayingTimeFileError) as exc:
        parse_overrides([_entry(games=-1.0)])
    assert "negative" in str(exc.value)


def test_no_maximum_games_is_enforced_by_the_loader():
    """Deliberately NOT validated: the fitted curve supplies the per-player ceiling.

    A second hardcoded maximum here would be a number nobody derived -- and it would be wrong
    for at least one league length, since this repo's team count and week count are read from
    config rather than assumed. `bind` is what stops an over-large figure reaching the board.
    """
    assert parse_overrides([_entry(games=99.0)])[0].games == 99.0


@pytest.mark.parametrize("bad", ["seventeen", None, True, [11], {"games": 11}])
def test_non_numeric_games_is_refused(bad):
    with pytest.raises(PlayingTimeFileError):
        parse_overrides([_entry(games=bad)])


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_games_is_refused(bad):
    with pytest.raises(PlayingTimeFileError) as exc:
        parse_overrides([_entry(games=bad)])
    assert "not a real number" in str(exc.value)


@pytest.mark.parametrize("blank", ["", "   "])
def test_empty_reason_is_refused(blank):
    with pytest.raises(PlayingTimeFileError) as exc:
        parse_overrides([_entry(reason=blank)])
    assert "not auditable" in str(exc.value)


def test_empty_date_is_refused():
    with pytest.raises(PlayingTimeFileError):
        parse_overrides([_entry(date="")])


def test_unknown_schema_refuses_to_guess():
    with pytest.raises(PlayingTimeFileError) as exc:
        parse_overrides({"schema": 7, "overrides": []})
    assert "Refusing to guess" in str(exc.value)


def test_object_without_overrides_key_is_refused():
    with pytest.raises(PlayingTimeFileError) as exc:
        parse_overrides({"schema": 1, "decisions": []})
    assert "no 'overrides' key" in str(exc.value)


def test_a_bad_entry_is_never_skipped_and_the_error_names_it():
    """Partial success is the one outcome this loader must never produce."""
    with pytest.raises(PlayingTimeFileError) as exc:
        parse_overrides([_entry(), _entry(player_id="99", games=-3)])
    msg = str(exc.value)
    assert "entry #1" in msg and '"player_id": "99"' in msg


# ------------------------------------------------------------------ the fail-closed rule


def test_missing_file_means_no_overrides(tmp_path):
    """The ordinary pre-review state. Must never break a board build."""
    assert load_overrides(tmp_path / "nope.json") == ()


def test_present_but_empty_file_raises(tmp_path):
    """An empty file is what a truncated write looks like, not a statement of 'no overrides'.

    Same asymmetry, and the same reason, as decisions.load_decisions: failing open here would
    silently un-apply a judgement Marc made about a player he knows something about.
    """
    p = tmp_path / "pt.json"
    p.write_text("   \n", encoding="utf-8")
    with pytest.raises(PlayingTimeFileError) as exc:
        load_overrides(p)
    assert "not the same as no overrides" in str(exc.value)


def test_malformed_json_raises_with_the_path(tmp_path):
    p = tmp_path / "pt.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(PlayingTimeFileError) as exc:
        load_overrides(p)
    assert str(p) in str(exc.value)


def test_the_repo_default_path_is_data_playing_time_json():
    assert OVERRIDES_PATH.name == "playing_time.json"
    assert OVERRIDES_PATH.parent.name == "data"


# ------------------------------------------------------------------ merge / index


def test_later_entry_wins_on_the_same_player():
    first = new_override(player_id="1", games=6, reason="PUP", date="2026-08-24")
    second = new_override(player_id="1", games=16, reason="activated", date="2026-09-06")
    assert overrides_by_pid([first, second])["1"].games == 16
    assert merge_overrides([first], [second]) == (second,)


def test_merge_keeps_original_order_for_untouched_entries():
    a = new_override(player_id="a", games=1, reason="x", date="d")
    b = new_override(player_id="b", games=2, reason="x", date="d")
    a2 = new_override(player_id="a", games=9, reason="y", date="d")
    assert [o.player_id for o in merge_overrides([a, b], [a2])] == ["a", "b"]
    assert merge_overrides([a, b], [a2])[0].games == 9


def test_new_override_stamps_today_when_no_date_is_given():
    assert new_override(player_id="1", games=5, reason="r").date


def test_duplicate_player_in_one_file_is_allowed_not_an_error():
    """A re-judgement is legitimate: PUP in August, activated in September."""
    got = parse_overrides([_entry(games=6.0), _entry(games=15.0)])
    assert len(got) == 2
    assert overrides_by_pid(got)["8112"].games == 15.0


# ------------------------------------------------------------------ the clamp: bind()


def _o(games: float) -> PlayingTimeOverride:
    return new_override(player_id="p", games=games, reason="r", date="2026-08-24")


def test_downward_override_passes_straight_through():
    """The whole point. Bad news is not second-guessed by the curve."""
    b = bind(_o(11.0), source_games=17.0, curve=15.5)
    assert b.now == 11.0
    assert b.moved and not b.clamped


def test_upward_override_is_clamped_at_the_curve():
    """A human may restore a player to the healthy-rank figure and no further.

    This is what keeps check_expected_games_capped_by_curve true BY CONSTRUCTION -- the feature
    admits itself without loosening an invariant.
    """
    b = bind(_o(17.0), source_games=11.0, curve=15.5)
    assert b.now == 15.5
    assert b.clamped
    assert b.moved  # 11.0 -> 15.5 is a real change, just not the one that was asked for


def test_an_override_landing_on_the_current_value_moved_nothing():
    """Reported, never badged: a badge on this would point at a decision that did nothing."""
    b = bind(_o(15.5), source_games=15.5, curve=15.5)
    assert b.now == 15.5
    assert not b.moved


def test_an_override_clamped_all_the_way_back_to_the_current_value_moved_nothing():
    b = bind(_o(20.0), source_games=15.5, curve=15.5)
    assert b.clamped and not b.moved


def test_a_source_with_no_games_column_falls_back_to_the_curve_as_the_counterfactual():
    """source_games=None is the FantasyPros/FantasySharks case -- no games column at all.

    The no-override figure for those players is the CURVE, because the fitted prior supplies
    their volume downstream. Carrying None through as "unknown" made every such override report
    a change (Codex 2026-08-24 finding 2), which is how an override clamped straight back to the
    prior got badged.
    """
    b = bind(_o(9.0), source_games=None, curve=15.5)
    assert b.was == 15.5
    assert not b.source_published_games
    assert b.now == 9.0
    assert b.moved


def test_a_clamped_override_on_a_source_with_no_games_moved_nothing():
    """THE regression test for finding 2: 99 games clamps to the prior, so EVoB is unchanged."""
    b = bind(_o(99.0), source_games=None, curve=15.5)
    assert b.now == 15.5
    assert b.clamped
    assert not b.moved, "clamped back to the prior is not a change, and must not be badged"


def test_source_published_games_distinguishes_the_two_ways_of_reaching_the_same_figure():
    """`moved` is a pure number comparison; the provenance lives in its own field."""
    from_source = bind(_o(9.0), source_games=15.5, curve=15.5)
    from_prior = bind(_o(9.0), source_games=None, curve=15.5)
    assert from_source.was == from_prior.was == 15.5
    assert from_source.source_published_games and not from_prior.source_published_games
    assert "fitted prior" in from_prior.describe()
    assert "fitted prior" not in from_source.describe()


def test_zero_games_is_a_legal_override():
    """Out for the season is a thing Marc knows and the sources sometimes do not."""
    b = bind(_o(0.0), source_games=17.0, curve=15.5)
    assert b.now == 0.0 and b.moved


def test_describe_states_the_before_the_after_and_the_clamp():
    text = bind(
        new_override(
            player_id="p", games=17.0, reason="cleared", date="2026-09-06", player_name="X",
            designation="PUP",
        ),
        source_games=11.0,
        curve=15.5,
    ).describe()
    assert "X" in text and "PUP" in text and "cleared" in text
    assert "11.00" in text and "15.50" in text and "clamped" in text
