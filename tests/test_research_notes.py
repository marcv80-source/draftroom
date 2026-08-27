"""Ledger #10: research that no number on the board reflects must reach the screen.

The gap this pins, in one sentence: ``data/injury_research.json`` could hold a finding that
``tools/injury_sweep.py`` was structurally unable to act on (it can only turn a finding into a
games override, and some findings have no games figure), which meant the finding produced no
override, no badge, and no trace anywhere Marc would see it in a live room.

The tests below are grouped by the thing that would silently break:

* the null/zero distinction, which is the whole schema change and is easy to collapse by accident
* fail-closed loading, matching its two sibling decision files exactly
* the note-selection asymmetry (a note appears BECAUSE no number moved, which is the inverse of
  every other badge on the row)
* the wiring, end to end, on the real cached board
"""

from __future__ import annotations

import json

import pytest

from draftroom.valuation.injury_research import (
    SCHEMA_VERSION,
    Finding,
    InjuryResearchError,
    load_research,
    parse_research,
    unpriced_notes,
)


def _entry(**over):
    base = {
        "player_id": "5850",
        "player_name": "Josh Jacobs",
        "status": "Under review",
        "report_date": "2026-08-11",
        "citation": "https://example.test/report",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- null vs zero


class TestUnpricedIsNotZero:
    """``null`` means "no number exists"; ``0`` means "he plays the full season". Collapsing
    these is the single mistake that would make this whole feature dishonest, because it would
    turn an absence of knowledge into a confident claim."""

    def test_explicit_null_parses_as_unpriced(self):
        (f,) = parse_research([_entry(games_missed=None)])
        assert f.games_missed is None
        assert f.is_unpriced is True

    def test_zero_is_a_real_claim_not_unpriced(self):
        (f,) = parse_research([_entry(games_missed=0)])
        assert f.games_missed == 0.0
        assert f.is_unpriced is False

    def test_absent_field_keeps_the_historical_default_of_zero(self):
        # Every entry written before this schema change omitted nothing, but the sweep's own
        # parser defaulted a missing key to 0. Changing that default to None would silently
        # reclassify existing findings as unpriced.
        (f,) = parse_research([_entry()])
        assert f.games_missed == 0.0
        assert f.is_unpriced is False

    def test_a_real_figure_still_parses(self):
        (f,) = parse_research([_entry(games_missed=4)])
        assert f.games_missed == 4.0
        assert f.is_unpriced is False


class TestValidationStillRefusesGarbage:
    def test_negative_games_missed_is_refused(self):
        with pytest.raises(InjuryResearchError, match="cannot be negative"):
            parse_research([_entry(games_missed=-1)])

    def test_a_string_games_missed_is_refused(self):
        with pytest.raises(InjuryResearchError, match="must be a number, or null"):
            parse_research([_entry(games_missed="four")])

    def test_bool_is_not_a_number(self):
        with pytest.raises(InjuryResearchError, match="must be a number, or null"):
            parse_research([_entry(games_missed=True)])

    def test_null_player_id_is_refused(self):
        # decisions.py gives null a real meaning (source-wide). Availability has no such grain,
        # so the same shape here is refused rather than reinterpreted.
        with pytest.raises(InjuryResearchError, match="never null"):
            parse_research([_entry(player_id=None)])

    @pytest.mark.parametrize("field", ["report_date", "citation"])
    def test_a_claim_with_no_source_or_date_is_refused(self, field):
        with pytest.raises(InjuryResearchError, match=field):
            parse_research([_entry(**{field: ""})])

    def test_season_ending_cannot_be_unpriced(self):
        # A season-ending finding IS a games figure (zero played). Letting it be null would let
        # the most actionable finding in the file render as a soft note.
        with pytest.raises(InjuryResearchError, match="season-ending finding"):
            parse_research([_entry(season_ending=True, games_missed=None)])

    def test_season_ending_with_a_figure_is_fine(self):
        (f,) = parse_research([_entry(season_ending=True, games_missed=17)])
        assert f.is_severe is True

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999"])
    def test_non_finite_games_missed_is_refused(self, literal, tmp_path):
        # Codex 2026-08-27 P1. Python's json parser ACCEPTS these, and none of them satisfies
        # `raw < 0` (every comparison against NaN is False). Left in, NaN reaches
        # `max(0.0, weeks - NaN)` in the sweep, which returns 0.0 -- so `--apply` would write a
        # ZERO-GAMES override for a healthy player. Written through a real file rather than a
        # dict, because that is the only way NaN/Infinity actually arrive.
        p = tmp_path / "r.json"
        p.write_text(
            '{"schema": 1, "findings": [{"player_id": "1", "games_missed": ' + literal + ","
            ' "report_date": "2026-08-11", "citation": "https://x.test"}]}',
            encoding="utf-8",
        )
        with pytest.raises(InjuryResearchError, match="finite number"):
            load_research(p)

    def test_a_finite_float_still_passes(self, tmp_path):
        p = tmp_path / "r.json"
        p.write_text(
            '{"schema": 1, "findings": [{"player_id": "1", "games_missed": 4.5,'
            ' "report_date": "2026-08-11", "citation": "https://x.test"}]}',
            encoding="utf-8",
        )
        (f,) = load_research(p)
        assert f.games_missed == 4.5


# --------------------------------------------------------------------------- fail closed


class TestFailsClosedLikeItsSiblings:
    """Identical rule to decisions.py and playing_time.py. A missing file is the ordinary
    state; a present-but-broken one must raise rather than read as "nothing researched", because
    an empty file is what a truncated write looks like."""

    def test_missing_file_means_no_findings(self, tmp_path):
        assert load_research(tmp_path / "nope.json") == ()

    def test_empty_findings_list_raises(self, tmp_path):
        p = tmp_path / "r.json"
        p.write_text(json.dumps({"findings": []}), encoding="utf-8")
        with pytest.raises(InjuryResearchError, match="truncated write"):
            load_research(p)

    def test_mapping_with_no_findings_key_raises(self, tmp_path):
        p = tmp_path / "r.json"
        p.write_text(json.dumps({"schema": 1}), encoding="utf-8")
        with pytest.raises(InjuryResearchError, match="no 'findings' key"):
            load_research(p)

    def test_malformed_json_raises(self, tmp_path):
        p = tmp_path / "r.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(InjuryResearchError, match="not valid JSON"):
            load_research(p)

    def test_a_zero_byte_file_raises(self, tmp_path):
        # Codex 2026-08-27 P1. The classic truncated write.
        p = tmp_path / "r.json"
        p.write_text("", encoding="utf-8")
        with pytest.raises(InjuryResearchError, match="empty"):
            load_research(p)

    def test_a_whitespace_only_file_raises(self, tmp_path):
        p = tmp_path / "r.json"
        p.write_text("   \n\t ", encoding="utf-8")
        with pytest.raises(InjuryResearchError, match="empty"):
            load_research(p)

    def test_invalid_utf8_raises_as_our_error(self, tmp_path):
        # An interrupted write that ends inside a multibyte character raises UnicodeDecodeError,
        # which used to escape as a generic exception, land in live_data's broad handler, and
        # degrade the app to placeholder mode with /healthz still 200.
        p = tmp_path / "r.json"
        # bytes.fromhex keeps THIS file pure ASCII while still writing a real
        # truncated multibyte sequence to disk.
        p.write_bytes(
            b'{"findings": [{"player_id": "1", "citation": "' + bytes.fromhex("e282")
        )
        with pytest.raises(InjuryResearchError, match="not valid UTF-8"):
            load_research(p)

    def test_an_unreadable_file_raises_as_our_error(self, tmp_path, monkeypatch):
        p = tmp_path / "r.json"
        p.write_text('{"findings": []}', encoding="utf-8")

        def boom(*a, **k):
            raise PermissionError("locked by another process")

        monkeypatch.setattr(type(p), "read_text", boom)
        with pytest.raises(InjuryResearchError, match="could not be read"):
            load_research(p)

    def test_an_unknown_schema_version_is_refused(self, tmp_path):
        # Codex 2026-08-27 P2. Matches playing_time.py: a file declaring a shape this code does
        # not understand is refused rather than read under the wrong assumptions.
        p = tmp_path / "r.json"
        p.write_text(json.dumps({"schema": 99, "findings": [_entry()]}), encoding="utf-8")
        with pytest.raises(InjuryResearchError, match="declares schema"):
            load_research(p)

    def test_the_current_schema_version_is_accepted(self, tmp_path):
        p = tmp_path / "r.json"
        p.write_text(
            json.dumps({"schema": SCHEMA_VERSION, "findings": [_entry()]}), encoding="utf-8"
        )
        assert len(load_research(p)) == 1

    def test_a_bare_list_is_accepted(self, tmp_path):
        p = tmp_path / "r.json"
        p.write_text(json.dumps([_entry(games_missed=None)]), encoding="utf-8")
        assert len(load_research(p)) == 1


# --------------------------------------------------------------------------- note selection


def _finding(pid: str, games_missed):
    return Finding(
        player_id=pid,
        player_name=f"P{pid}",
        status="Under review",
        season_ending=False,
        games_missed=games_missed,
        confidence="MEDIUM",
        report_date="2026-08-11",
        citation="https://example.test/x",
    )


class TestNoteSelectionAsymmetry:
    """A note appears BECAUSE no number moved. That is the inverse of REJ and NN.NG, which
    appear because a number DID move, and getting it backwards would either badge everything or
    badge nothing."""

    def test_unpriced_finding_becomes_a_note(self):
        notes = unpriced_notes([_finding("1", None)])
        assert set(notes) == {"1"}
        assert "No games figure exists" in notes["1"].reason

    def test_priced_finding_with_no_applied_override_still_becomes_a_note(self):
        # Pierce and Charbonnet: real figures, deferred to the cutdown by Marc's own call. The
        # board does not reflect them, so the row must say so.
        notes = unpriced_notes([_finding("2", 4.0)])
        assert set(notes) == {"2"}
        assert "does NOT reflect" in notes["2"].reason
        assert "misses 4 game(s)" in notes["2"].reason

    def test_an_applied_override_suppresses_the_note(self):
        # For him the NN.NG badge is the stronger, more specific statement -- it says what the
        # research COST, not merely that research exists.
        notes = unpriced_notes([_finding("3", 4.0)], priced_pids={"3"})
        assert notes == {}

    def test_an_applied_override_suppresses_an_unpriced_note_too(self):
        notes = unpriced_notes([_finding("4", None)], priced_pids={"4"})
        assert notes == {}

    def test_the_two_reasons_are_different_text(self):
        # They read differently on purpose: "nobody CAN price it" and "nobody HAS priced it yet"
        # lead Marc to different actions.
        notes = unpriced_notes([_finding("5", None), _finding("6", 3.0)])
        assert notes["5"].reason != notes["6"].reason

    def test_payload_carries_the_researched_name(self):
        # Codex 2026-08-27 P1 (partial). A valid id pointing at the wrong player binds cleanly
        # and silently. The board logs a name mismatch, but a log is not visible in a room --
        # carrying the researched name in the payload lets the row itself be checked.
        payload = unpriced_notes([_finding("8", None)])["8"].as_payload()
        assert payload["player_name"] == "P8"

    def test_payload_carries_the_citation_and_the_verdict_separately(self):
        payload = unpriced_notes([_finding("7", None)])["7"].as_payload()
        assert payload["citation"] == "https://example.test/x"
        assert payload["report_date"] == "2026-08-11"
        assert payload["games_missed"] is None
        # `notes` is the researcher's evidence; `why_unpriced` is this module's sentence. A UI
        # that collapsed them would make the badge look like something a human typed.
        assert "why_unpriced" in payload
        assert payload["why_unpriced"] != payload["notes"]


# --------------------------------------------------------------------------- end to end


class TestShippedResearchFile:
    """The file that actually ships. These assert the two entries the feature was built for
    exist and are UNPRICED, because a later edit that gave them an invented games figure would
    be the exact failure this whole design refuses."""

    def test_it_parses(self):
        assert len(load_research()) >= 2

    def test_jacobs_and_nacua_are_unpriced(self):
        by_pid = {f.player_id: f for f in load_research()}
        # Sleeper ids, verified against the cached Sleeper universe by name.
        for pid, name in (("5850", "Josh Jacobs"), ("9493", "Puka Nacua")):
            assert pid in by_pid, f"{name} ({pid}) missing from the research file"
            assert by_pid[pid].is_unpriced, f"{name} must carry NO invented games figure"
            assert by_pid[pid].citation, "every finding needs a traceable basis"


class TestBoardWiring:
    """The real cached board. If this passes and the badge is still invisible, the break is in
    the frontend, not here."""

    def test_the_board_exposes_research_notes(self):
        from draftroom.validate.board import build_real_board

        rb = build_real_board()
        notes = getattr(rb, "research_notes", None)
        assert notes is not None, "RealBoard must carry research_notes"
        on_board = {p.player_id for p in rb.players}
        assert set(notes) <= on_board, "a note on a player with no row has nowhere to render"

    def test_nacua_carries_a_note_on_the_real_board(self):
        from draftroom.validate.board import build_real_board

        rb = build_real_board()
        assert "9493" in rb.research_notes
        assert rb.research_notes["9493"].finding.is_unpriced

    def test_a_note_never_moves_a_value(self):
        # The whole promise of this badge. Building the board with the research file present
        # must produce identical draft values to building it with the file absent.
        from unittest import mock

        from draftroom.validate import board as board_mod

        rb_with = board_mod.build_real_board()
        with mock.patch.object(board_mod, "load_research", return_value=()):
            rb_without = board_mod.build_real_board()

        assert rb_without.research_notes == {}
        dv_with = {p.player_id: p.dv for p in rb_with.players}
        dv_without = {p.player_id: p.dv for p in rb_without.players}
        assert dv_with == dv_without, "a research note must never change a number"

    def test_the_pool_payload_carries_the_note(self):
        # LOOK UP BY NAME, NOT BY ID. `PoolPlayer.player_id` is FFC-derived and Nacua is `5714`
        # there while the research file (Sleeper's space) calls him `9493` -- the two-id-spaces
        # trap in CLAUDE.md. The join that actually carries the note across is name|team|pos,
        # done in `_real_board_enrichment`. The first version of this test asserted on the
        # Sleeper id and failed against working code, which is the trap doing its job.
        from draftroom.live_data import load_player_pool

        pool = {p.name: p for p in load_player_pool()}
        nacua = pool.get("Puka Nacua")
        assert nacua is not None
        note = getattr(nacua, "research_note", None)
        assert note is not None, "the note must reach PoolPlayer for the UI to render it"
        assert note["games_missed"] is None
        assert note["citation"]
        assert note["why_unpriced"]
