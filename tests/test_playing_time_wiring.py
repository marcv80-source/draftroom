"""Playing-time overrides wired through board -> pool -> payload, on the REAL cached board.

Companion to ``test_playing_time.py`` (the file format and the clamp rule in isolation). What
these tests cover is the integration, and specifically the four things a green suite on either
side alone would not catch:

1. The override actually reaches ``expected_games`` and moves the player's value -- the entire
   point, and the gap that ``injury_vs_expected_games`` could only complain about.
2. It moves the VOLUME and not the RATE. PPG must be byte-identical.
3. The availability-curve invariant stays true with overrides in force. This feature was built
   under the constraint that it must not loosen a gate to admit itself.
4. An overridden player stops coming back to the review queue as an open question.

Reads only cached data; no network.
"""

from __future__ import annotations

import pytest

from draftroom.valuation.playing_time import (
    PlayingTimeFileError,
    load_overrides,
    new_override,
    save_overrides,
)


def _install(monkeypatch, tmp_path, *overrides):
    """Point the board build at a temp overrides file holding exactly ``overrides``."""
    from draftroom.valuation import playing_time as pt_mod
    from draftroom.validate import board as board_mod

    p = tmp_path / "playing_time.json"
    save_overrides(overrides, p)
    monkeypatch.setattr(pt_mod, "OVERRIDES_PATH", p, raising=False)
    monkeypatch.setattr(board_mod, "load_overrides", lambda: load_overrides(p))
    return p


def _baseline(monkeypatch):
    from draftroom.validate import board as board_mod

    monkeypatch.setattr(board_mod, "load_overrides", lambda: ())
    return board_mod.build_real_board()


def _pick_designated_player(rb):
    """A ranked player the board credited the FULL healthy-rank curve figure for.

    Chosen from the board itself rather than hardcoded to a name: Alec Pierce is the 2026
    example, but a re-run of prep moves who that is, and a test that names him would go green
    for the wrong reason (or red for no reason) after the next refresh.
    """
    from draftroom.valuation.replacement import expected_games as curve_games

    by_pos: dict[str, list] = {}
    for s in rb.seasons:
        by_pos.setdefault(s.pos, []).append(s)
    for pos, group in by_pos.items():
        for rank, s in enumerate(sorted(group, key=lambda x: -x.ppg), start=1):
            curve = curve_games(pos, rank=rank, weeks=rb.cfg.weeks)
            if s.expected_games is not None and abs(float(s.expected_games) - curve) < 1e-9:
                return s, curve
    pytest.skip("no board player is sitting exactly on the curve figure in this cache")


# --------------------------------------------------------------------- it reaches the board


def test_an_override_lowers_expected_games_and_the_value(monkeypatch, tmp_path):
    base = _baseline(monkeypatch)
    season, curve = _pick_designated_player(base)
    base_dv = {p.player_id: p.dv for p in base.players}

    from draftroom.validate import board as board_mod

    target = curve / 2.0
    _install(
        monkeypatch,
        tmp_path,
        new_override(
            player_id=season.player_id,
            games=target,
            reason="test: half a season",
            date="2026-08-24",
        ),
    )
    rb = board_mod.build_real_board()

    after = {s.player_id: s for s in rb.seasons}[season.player_id]
    assert after.expected_games == pytest.approx(target)
    dv = {p.player_id: p.dv for p in rb.players}
    assert dv[season.player_id] < base_dv[season.player_id], (
        "halving a player's expected games must reduce his EVoB -- value is "
        "(ppg - baseline_ppg) * expected_games"
    )


def test_an_override_moves_the_volume_and_never_the_rate(monkeypatch, tmp_path):
    """PPG is a per-game rate. An availability judgement has no business touching it.

    A view that a player will be WORSE per game is a projection question and belongs in the
    review queue instead.
    """
    base = _baseline(monkeypatch)
    season, curve = _pick_designated_player(base)

    from draftroom.validate import board as board_mod

    _install(
        monkeypatch,
        tmp_path,
        new_override(
            player_id=season.player_id, games=curve / 3.0, reason="test", date="2026-08-24"
        ),
    )
    rb = board_mod.build_real_board()
    after = {s.player_id: s for s in rb.seasons}[season.player_id]
    assert after.ppg == pytest.approx(season.ppg), "expected_games must not disturb ppg"


def test_no_overrides_on_file_changes_no_value(monkeypatch, tmp_path):
    """The safety property: adding this machinery must not move the board on its own."""
    from draftroom.validate import board as board_mod

    baseline = {p.player_id: p.dv for p in _baseline(monkeypatch).players}
    _install(monkeypatch, tmp_path)  # an empty (but present and valid) overrides file
    after = {p.player_id: p.dv for p in board_mod.build_real_board().players}
    assert after == baseline


def test_only_the_named_player_moves(monkeypatch, tmp_path):
    """An override is one player's fact. It must not disturb anybody else's expected games.

    Note this is about expected_games, NOT dv: dv legitimately shifts for other players at the
    same position, because lowering one player's games moves the man-games replacement level.
    That is the valuation working, not leakage.
    """
    base = _baseline(monkeypatch)
    season, curve = _pick_designated_player(base)
    base_games = {s.player_id: s.expected_games for s in base.seasons}

    from draftroom.validate import board as board_mod

    _install(
        monkeypatch,
        tmp_path,
        new_override(player_id=season.player_id, games=2.0, reason="test", date="2026-08-24"),
    )
    after = {s.player_id: s.expected_games for s in board_mod.build_real_board().seasons}
    moved = [pid for pid in base_games if after.get(pid) != base_games[pid]]
    assert moved == [season.player_id]


# --------------------------------------------------------------------- the clamp holds


def test_an_override_cannot_push_a_player_above_the_curve(monkeypatch, tmp_path):
    base = _baseline(monkeypatch)
    season, curve = _pick_designated_player(base)

    from draftroom.validate import board as board_mod

    _install(
        monkeypatch,
        tmp_path,
        new_override(
            player_id=season.player_id,
            games=999.0,
            reason="test: an absurd figure must be clamped, not honoured",
            date="2026-08-24",
        ),
    )
    rb = board_mod.build_real_board()
    after = {s.player_id: s for s in rb.seasons}[season.player_id]
    assert float(after.expected_games) <= curve + 1e-9


def test_the_availability_invariant_still_passes_with_an_override_in_force(
    monkeypatch, tmp_path
):
    """The hard requirement: this feature must not loosen a gate to admit itself.

    ``check_expected_games_capped_by_curve`` is the guard against the curve going inert. An
    override mechanism that had to be exempted from it would be indistinguishable from the bug
    that check exists to catch.
    """
    from draftroom.validate import board as board_mod
    from draftroom.validate.invariants import check_expected_games_capped_by_curve

    base = _baseline(monkeypatch)
    season, curve = _pick_designated_player(base)
    _install(
        monkeypatch,
        tmp_path,
        new_override(player_id=season.player_id, games=999.0, reason="t", date="2026-08-24"),
    )
    rb = board_mod.build_real_board()
    result = check_expected_games_capped_by_curve(rb.seasons, rb.cfg)
    assert result.passed, result.detail


# --------------------------------------------------------------------- badge scoping


def test_only_overrides_that_moved_a_number_are_badged(monkeypatch, tmp_path):
    """Same asymmetry as the REJ badge: no badge for a decision that did nothing.

    Here the inert case is an override that the curve clamps straight back to the figure the
    board already had. It is on file, it is logged, and it must NOT appear as an applied change.
    """
    base = _baseline(monkeypatch)
    season, curve = _pick_designated_player(base)

    from draftroom.validate import board as board_mod

    _install(
        monkeypatch,
        tmp_path,
        new_override(
            player_id=season.player_id,
            games=curve + 50.0,  # clamped back to exactly what the board already used
            reason="test: inert",
            date="2026-08-24",
        ),
    )
    rb = board_mod.build_real_board()
    assert season.player_id in rb.playing_time_overrides, "it is still on file"
    assert season.player_id not in rb.applied_playing_time, (
        "an override clamped back to the existing figure changed nothing and must not be badged"
    )


def test_an_override_naming_an_unknown_player_is_harmless(monkeypatch, tmp_path):
    """A typo in a hand-edited id must not break the board -- but it must not silently vanish
    either, which is why the build logs it. The board itself is unchanged."""
    from draftroom.validate import board as board_mod

    baseline = {p.player_id: p.dv for p in _baseline(monkeypatch).players}
    _install(
        monkeypatch,
        tmp_path,
        new_override(player_id="not-a-real-player-id", games=3, reason="t", date="2026-08-24"),
    )
    rb = board_mod.build_real_board()
    assert {p.player_id: p.dv for p in rb.players} == baseline
    assert rb.applied_playing_time == {}


# --------------------------------------------------------------------- fails closed


def test_a_malformed_overrides_file_also_fails_the_LIVE_POOL_not_just_the_board(
    monkeypatch, tmp_path
):
    """THE blocker regression (Codex 2026-08-24 finding 1).

    `live_data._real_board_enrichment` re-raised only DecisionsFileError; everything else became
    `{}` = ADP-placeholder mode. So a truncated overrides file raised correctly out of
    `build_real_board`, got swallowed one layer up, and draft mode booted with /healthz at 200
    on placeholder values -- the failure read as "the cache is stale" rather than "your
    availability judgements stopped applying". Testing only `build_real_board` missed it
    entirely, which is why this test goes through the pool.
    """
    from draftroom import live_data
    from draftroom.valuation import playing_time as pt_mod
    from draftroom.validate import board as board_mod

    p = tmp_path / "playing_time.json"
    p.write_text("   \n", encoding="utf-8")  # the truncated-write shape
    monkeypatch.setattr(pt_mod, "OVERRIDES_PATH", p, raising=False)
    monkeypatch.setattr(board_mod, "load_overrides", lambda: load_overrides(p))
    with pytest.raises(PlayingTimeFileError):
        live_data.load_player_pool()


def test_a_malformed_overrides_file_fails_the_board_build(monkeypatch, tmp_path):
    """Deliberately NOT caught in the board build, exactly like DecisionsFileError.

    Every other optional input here degrades to "this source contributes nothing", because a
    missing FantasyPros CSV has nothing to do with whether the board is sound. A bad overrides
    file is the opposite: degrading would silently stop applying a judgement Marc made, and the
    board would look fine while ignoring him.
    """
    from draftroom.valuation import playing_time as pt_mod
    from draftroom.validate import board as board_mod

    p = tmp_path / "playing_time.json"
    p.write_text('{"schema": 1, "overrides": [{"player_id": "1"}]}', encoding="utf-8")
    monkeypatch.setattr(pt_mod, "OVERRIDES_PATH", p, raising=False)
    monkeypatch.setattr(board_mod, "load_overrides", lambda: load_overrides(p))
    with pytest.raises(PlayingTimeFileError):
        board_mod.build_real_board()


# --------------------------------------------------------------------- the pool payload


def test_the_override_reaches_the_pool_and_the_payload(monkeypatch, tmp_path):
    """It has to be VISIBLE. A games figure that came from Marc must never read as model output."""
    from draftroom import live_data
    from draftroom.validate import board as board_mod

    base = _baseline(monkeypatch)
    season, curve = _pick_designated_player(base)
    name_by_pid = {p.player_id: p.name for p in base.players}
    _install(
        monkeypatch,
        tmp_path,
        new_override(
            player_id=season.player_id,
            games=curve / 2.0,
            reason="test: reaches the payload",
            designation="PUP",
            date="2026-08-24",
        ),
    )
    rb = board_mod.build_real_board()
    binding = rb.applied_playing_time[season.player_id]
    payload = live_data._playing_time_payload(rb, season.player_id)

    assert payload is not None
    assert payload["games"] == pytest.approx(binding.now)
    assert payload["reason"] == "test: reaches the payload"
    assert payload["designation"] == "PUP"
    assert payload["clamped"] is False
    assert live_data._playing_time_payload(rb, "nobody") is None
    assert name_by_pid[season.player_id]  # the row this will render on actually exists


# --------------------------------------------------------------------- the review queue


def test_an_overridden_player_stops_being_an_open_review_question(monkeypatch, tmp_path):
    """Handing Marc his own decision back as a fresh candidate is noise.

    And the ``hygiene`` wording the detector would otherwise use -- "a source did price in N
    games" -- would be actively false: no source did, he did. The row is replaced by an entry in
    ``settled_by_override``, because a suppression nobody can see is indistinguishable from a
    detector that stopped working.
    """
    from draftroom.valuation import candidates as C

    from draftroom.validate import board as board_mod

    monkeypatch.setattr(board_mod, "load_overrides", lambda: ())
    try:
        before = C.load_review_inputs()
    except FileNotFoundError as exc:
        pytest.skip(f"no cached prep data for the review queue: {exc}")

    rows = C.detect_injury_vs_expected_games(before)
    if not rows:
        pytest.skip("no designated player carries an injury row in this cache")
    pid = rows[0].player_id
    designation = before.injury_status.get(pid)

    _install(
        monkeypatch,
        tmp_path,
        # The override must RECORD the designation it answers -- suppression is scoped to it.
        new_override(
            player_id=pid, games=4.0, reason="test: settled",
            designation=designation, date="2026-08-24",
        ),
    )
    after = C.load_review_inputs()
    assert pid in after.board.applied_playing_time, "the override must have taken effect"

    still_open = {r.player_id for r in C.detect_injury_vs_expected_games(after)}
    assert pid not in still_open, "an answered question must not come back as a candidate"
    # And the disappearance is REPORTED, not silent.
    queue = C.collect_candidates(after, include=("injury",))
    assert pid in queue.settled_by_override
    assert any("settled by a manual playing-time override" in n for n in queue.notes)


def _injury_row_pid(monkeypatch):
    """A pid that currently carries an injury row, with no overrides in force."""
    from draftroom.valuation import candidates as C
    from draftroom.validate import board as board_mod

    monkeypatch.setattr(board_mod, "load_overrides", lambda: ())
    try:
        before = C.load_review_inputs()
    except FileNotFoundError as exc:
        pytest.skip(f"no cached prep data for the review queue: {exc}")
    rows = C.detect_injury_vs_expected_games(before)
    if not rows:
        pytest.skip("no designated player carries an injury row in this cache")
    return rows[0].player_id, before


def test_an_override_for_a_DIFFERENT_designation_does_not_suppress_the_row(
    monkeypatch, tmp_path
):
    """The finding-3 regression: a stale override must not absorb later news.

    Marc records 12 games for, say, a suspension. A refresh then marks the player IR.
    Suppressing on the mere EXISTENCE of an override left him on the board at a figure written
    before anyone knew about the IR, with no row asking about it -- the most expensive thing
    this detector could get wrong, because an unbadged overridden player reads as "somebody
    looked at this".
    """
    from draftroom.valuation import candidates as C

    pid, _before = _injury_row_pid(monkeypatch)
    _install(
        monkeypatch,
        tmp_path,
        new_override(
            player_id=pid, games=4.0, reason="test: stale",
            designation="SUSPENDED-NOT-THE-CURRENT-ONE", date="2026-08-24",
        ),
    )
    after = C.load_review_inputs()
    assert pid in after.board.applied_playing_time, "the override still applies to the number"

    match = [r for r in C.detect_injury_vs_expected_games(after) if r.player_id == pid]
    assert match, "a mismatched designation must leave the row standing"
    assert "override IS in force" in match[0].reason, (
        "and the row must SAY there is an override, or it reads as an unexamined player"
    )
    queue = C.collect_candidates(after, include=("injury",))
    assert pid not in queue.settled_by_override


def test_an_override_with_no_recorded_designation_never_suppresses(monkeypatch, tmp_path):
    """This repo does not guess what a human meant. No designation recorded = answers none."""
    from draftroom.valuation import candidates as C

    pid, _before = _injury_row_pid(monkeypatch)
    _install(
        monkeypatch,
        tmp_path,
        new_override(player_id=pid, games=4.0, reason="test: none", date="2026-08-24"),
    )
    after = C.load_review_inputs()
    open_rows = [r for r in C.detect_injury_vs_expected_games(after) if r.player_id == pid]
    assert open_rows, "an override recording no designation answers none of them"
    assert "records no designation at all" in open_rows[0].reason


def test_a_healthy_player_override_is_never_reported_as_a_settled_injury_row(
    monkeypatch, tmp_path
):
    """Second half of finding 3: `settled_by_override` described healthy players as vanished
    injury rows, because it was built from every applied override rather than from the ones
    that answer a current designation."""
    from draftroom.valuation import candidates as C
    from draftroom.validate import board as board_mod

    monkeypatch.setattr(board_mod, "load_overrides", lambda: ())
    base = board_mod.build_real_board()
    try:
        probe = C.load_review_inputs()
    except FileNotFoundError as exc:
        pytest.skip(f"no cached prep data for the review queue: {exc}")
    healthy = [
        s.player_id
        for s in base.seasons
        if not C.is_long_term_designation(probe.injury_status.get(s.player_id))
    ]
    if not healthy:
        pytest.skip("no undesignated player on this board")
    pid = healthy[0]

    _install(
        monkeypatch,
        tmp_path,
        new_override(player_id=pid, games=3.0, reason="test: healthy", date="2026-08-24"),
    )
    after = C.load_review_inputs()
    assert pid in after.board.applied_playing_time
    queue = C.collect_candidates(after, include=("injury",))
    assert pid not in queue.settled_by_override


def test_a_name_that_disagrees_with_the_id_is_flagged(monkeypatch, tmp_path, caplog):
    """A VALID id pointing at the WRONG player applies cleanly and badges cleanly. The loader
    cannot catch it (no board); the board build can, and warns."""
    import logging

    from draftroom.validate import board as board_mod

    base = _baseline(monkeypatch)
    season, _curve = _pick_designated_player(base)
    _install(
        monkeypatch,
        tmp_path,
        new_override(
            player_id=season.player_id, games=5.0, reason="t",
            player_name="Definitely Not This Player", date="2026-08-24",
        ),
    )
    with caplog.at_level(logging.WARNING, logger="draftroom.validate.board"):
        board_mod.build_real_board()
    assert any(
        "names" in r.getMessage() and "on the board" in r.getMessage() for r in caplog.records
    ), "a name/id mismatch must be said out loud"
