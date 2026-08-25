"""Tests for the pre-draft availability job: injury_sweep and injury_worklist.

These two tools WRITE DECISION FILES (`data/playing_time.json`,
`data/projection_decisions.json`), which puts them at the same bar as the loaders they feed.
Two behaviours matter more than the rest and both are pinned here:

  1. The loader FAILS CLOSED. An absent research file means "nothing researched"; a file that
     exists but is empty or malformed RAISES. Degrading would silently stop applying a judgement
     about a player who is out for the season, while the board kept looking fine.
  2. Research is authoritative DOWNWARD ONLY. The first version of `decide_action` proposed
     RAISING a player's games because a press report was rosier than the source (real case:
     Jordyn Tyson, 2026-08-25, research implied 12 games against ESPN's 10). Logic that has been
     wrong once gets a test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import injury_sweep as sweep_mod
from tools import injury_worklist as wl
from tools.injury_sweep import (
    Finding,
    InjuryResearchError,
    Row,
    apply,
    decide_action,
    load_research,
    parse_research,
)

WEEKS = 17.0


def _entry(**over: object) -> dict[str, object]:
    """A valid research entry. Tests corrupt one field at a time."""
    base: dict[str, object] = {
        "player_id": "8142",
        "player_name": "Alec Pierce",
        "status": "PUP",
        "season_ending": False,
        "games_missed": 4,
        "confidence": "MEDIUM",
        "report_date": "2026-08-19",
        "citation": "https://example.com/report",
        "notes": "",
    }
    base.update(over)
    return base


def _finding(**over: object) -> Finding:
    e = _entry(**over)
    return parse_research([e])[0]


# --------------------------------------------------------------------- fails closed


class TestResearchFileFailsClosed:
    def test_missing_file_means_nothing_researched(self, tmp_path: Path) -> None:
        """The ordinary state. Absent is the ONLY thing that reads as 'no findings'."""
        assert load_research(tmp_path / "nope.json") == ()

    def test_present_but_empty_raises_rather_than_reading_as_nothing(self, tmp_path: Path) -> None:
        """An empty file is what a truncated write looks like, not a decision to sweep nothing."""
        p = tmp_path / "r.json"
        p.write_text(json.dumps({"schema": 1, "findings": []}), encoding="utf-8")
        with pytest.raises(InjuryResearchError, match="truncated write"):
            load_research(p)

    def test_mapping_without_findings_key_raises(self) -> None:
        with pytest.raises(InjuryResearchError, match="no 'findings' key"):
            parse_research({"schema": 1})

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "r.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(InjuryResearchError, match="not valid JSON"):
            load_research(p)

    def test_a_bare_list_is_accepted_because_that_is_what_a_hand_edit_becomes(self) -> None:
        assert len(parse_research([_entry()])) == 1

    @pytest.mark.parametrize(
        "override, match",
        [
            ({"player_id": None}, "never null"),
            ({"player_id": ""}, "never null"),
            ({"player_id": "   "}, "never null"),
            ({"games_missed": "four"}, "must be a number"),
            ({"games_missed": True}, "must be a number"),
            ({"games_missed": -1}, "cannot be negative"),
            ({"season_ending": "true"}, "must be true or false"),
            ({"citation": ""}, "'citation' is required"),
            ({"report_date": ""}, "'report_date' is required"),
        ],
    )
    def test_every_corrupt_field_raises(self, override: dict, match: str) -> None:
        with pytest.raises(InjuryResearchError, match=match):
            parse_research([_entry(**override)])

    def test_a_non_object_entry_raises(self) -> None:
        with pytest.raises(InjuryResearchError, match="is not an object"):
            parse_research(["Alec Pierce is hurt"])

    def test_player_id_null_is_refused_rather_than_reinterpreted(self) -> None:
        """decisions.py gives null a real meaning (source-wide). Availability has no such grain.

        The same shape here would have to be INVENTED a meaning, so it is refused instead --
        the identical rule playing_time.py already enforces.
        """
        with pytest.raises(InjuryResearchError):
            parse_research([_entry(player_id=None)])


# --------------------------------------------------------------------- the decision rule


class TestDecideAction:
    def test_season_ending_zeroes_games(self) -> None:
        d = decide_action(
            _finding(season_ending=True, games_missed=17), credited=15.5, curve=15.5, weeks=WEEKS
        )
        assert d.games == 0.0
        assert "0.0" in d.action

    def test_season_ending_zeroes_even_when_he_is_off_the_valued_board(self) -> None:
        """Pearsall/Higgins: no source published them, so there is no credited figure.

        The override is inert TODAY and load-bearing the moment a refresh republishes a row, so
        it must still be produced.
        """
        d = decide_action(
            _finding(season_ending=True, games_missed=17), credited=None, curve=None, weeks=WEEKS
        )
        assert d.games == 0.0

    def test_research_saying_he_is_fine_writes_nothing(self) -> None:
        d = decide_action(_finding(games_missed=0), credited=15.0, curve=16.0, weeks=WEEKS)
        assert d.games is None
        assert "full season" in d.action

    def test_partial_absence_is_weeks_minus_missed(self) -> None:
        d = decide_action(_finding(games_missed=4), credited=15.23, curve=15.23, weeks=WEEKS)
        assert d.games == pytest.approx(13.0)

    def test_the_asymmetry_an_upward_override_is_never_applied(self) -> None:
        """THE load-bearing rule. Research implies 12; the source already says 10. Keep 10.

        This is the real Jordyn Tyson case from 2026-08-25. The first version of this function
        proposed raising him to 12, which is the one error direction that inflates a player Marc
        then drafts at full value.
        """
        d = decide_action(_finding(games_missed=5), credited=10.0, curve=15.06, weeks=WEEKS)
        assert d.games is None, "an upward override must never be proposed"
        assert "NO CHANGE" in d.action
        assert "more conservative than" in d.action

    def test_the_asymmetry_holds_at_exact_equality(self) -> None:
        """Equal is not lower. Rewriting a figure to itself is churn with an audit trail."""
        d = decide_action(_finding(games_missed=4), credited=13.0, curve=17.0, weeks=WEEKS)
        assert d.games is None

    def test_downward_passes_through_because_bad_news_is_the_point(self) -> None:
        d = decide_action(_finding(games_missed=10), credited=11.0, curve=14.77, weeks=WEEKS)
        assert d.games == pytest.approx(7.0)

    def test_the_curve_clamps_an_optimistic_target_and_says_so(self) -> None:
        """weeks - missed = 16, but the curve for his rank is 12. The curve wins."""
        d = decide_action(_finding(games_missed=1), credited=None, curve=12.0, weeks=WEEKS)
        assert d.games == pytest.approx(12.0)
        assert d.clamped_to_curve is True

    def test_no_credited_figure_means_the_override_still_applies(self) -> None:
        """FantasyPros/FantasySharks publish no games column, so credited can legitimately be
        absent -- and an override is then the ONLY way to reach the player at all."""
        d = decide_action(_finding(games_missed=6), credited=None, curve=17.0, weeks=WEEKS)
        assert d.games == pytest.approx(11.0)

    def test_games_missed_beyond_the_season_floors_at_zero(self) -> None:
        d = decide_action(_finding(games_missed=40), credited=10.0, curve=15.0, weeks=WEEKS)
        assert d.games == pytest.approx(0.0)

    def test_behind_sources_are_named_in_the_action(self) -> None:
        d = decide_action(
            _finding(games_missed=4), credited=15.23, curve=15.23, weeks=WEEKS, behind=["espn"]
        )
        assert "behind: espn" in d.action


# --------------------------------------------------------------------- production proxy


class _Line:
    """A minimal statline stand-in. Only the attributes the proxy reads matter."""

    def __init__(self, **kw: float) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


class TestProductionProxy:
    def test_a_games_figure_alone_is_not_production(self) -> None:
        """Sleeper carries games=18 with an all-zero statline for players who are out.

        Counting that as production would flag a source that has ALREADY caught up.
        """
        assert sweep_mod._production(_Line(games=18.0)) == 0.0

    def test_attempts_and_targets_alone_are_not_production(self) -> None:
        assert sweep_mod._production(_Line(rush_att=100.0, rec_tgt=80.0, pass_att=50.0)) == 0.0

    def test_real_output_counts(self) -> None:
        assert sweep_mod._production(_Line(rec_yd=900.0, rec=60.0, rec_td=5.0)) > 0.0

    def test_a_touchdown_only_projection_is_not_rounded_away(self) -> None:
        assert sweep_mod._production(_Line(rush_td=1.0)) > 0.0


# --------------------------------------------------------------------- apply


def _row(finding: Finding, games: float | None, rejections: list[str] | None = None) -> Row:
    return Row(
        finding=finding,
        pos="WR",
        team="IND",
        adp=70.0,
        credited_games=15.0,
        curve_games=15.5,
        positional_rank=30,
        proposed_games=games,
        proposed_rejections=rejections or [],
    )


class TestApply:
    def test_only_severe_defers_the_still_moving_rows(self, tmp_path: Path) -> None:
        """Before the 53-man cutdown, a PUP timeline is provisional and a torn ACL is not."""
        severe = _row(_finding(season_ending=True, games_missed=17, player_id="12484"), 0.0)
        partial = _row(_finding(games_missed=4, player_id="8142"), 13.0)
        out = tmp_path / "pt.json"
        apply([severe, partial], today="2026-08-25", only_severe=True, overrides_path=out)

        written = json.loads(out.read_text(encoding="utf-8"))["overrides"]
        assert [o["player_id"] for o in written] == ["12484"]

    def test_without_only_severe_both_are_written(self, tmp_path: Path) -> None:
        severe = _row(_finding(season_ending=True, games_missed=17, player_id="12484"), 0.0)
        partial = _row(_finding(games_missed=4, player_id="8142"), 13.0)
        out = tmp_path / "pt.json"
        apply([severe, partial], today="2026-08-25", overrides_path=out)
        written = json.loads(out.read_text(encoding="utf-8"))["overrides"]
        assert {o["player_id"] for o in written} == {"12484", "8142"}

    def test_a_row_with_no_proposed_games_writes_nothing(self, tmp_path: Path) -> None:
        """The asymmetry's output must actually reach the file system as silence."""
        out = tmp_path / "pt.json"
        pt_path, dc_path = apply(
            [_row(_finding(games_missed=5), None)], today="2026-08-25", overrides_path=out
        )
        assert pt_path is None and dc_path is None
        assert not out.exists()

    def test_every_written_override_carries_its_citation(self, tmp_path: Path) -> None:
        """`playing_time.py`'s file note promises a citable basis in every reason."""
        out = tmp_path / "pt.json"
        apply(
            [_row(_finding(season_ending=True, games_missed=17), 0.0)],
            today="2026-08-25",
            overrides_path=out,
        )
        written = json.loads(out.read_text(encoding="utf-8"))["overrides"]
        assert "https://example.com/report" in written[0]["reason"]
        assert written[0]["date"] == "2026-08-25"

    def test_a_stale_source_becomes_a_contamination_rejection(self, tmp_path: Path) -> None:
        """Season-ending + a source still publishing = a failed identity, so a whole-statline
        rejection. Matched on the `*` sentinel, which is what expands to every stat."""
        row = _row(_finding(season_ending=True, games_missed=17), 0.0, rejections=["espn"])
        dec_out = tmp_path / "dec.json"
        apply(
            [row],
            today="2026-08-25",
            overrides_path=tmp_path / "pt.json",
            decisions_path=dec_out,
        )
        written = json.loads(dec_out.read_text(encoding="utf-8"))["decisions"]
        assert len(written) == 1
        assert written[0]["source"] == "espn"
        assert written[0]["stat"] == "*"
        assert written[0]["verdict"] == "reject"
        assert "CONTAMINATION" in written[0]["reason"]


# --------------------------------------------------------------------- worklist


class TestWorklistIdJoin:
    def test_generational_suffixes_are_normalized_away(self) -> None:
        """Sleeper stores "Luther Burden"; FFC has "Luther Burden III".

        Joining on the raw string left three players with no id and a printed instruction to
        look it up by hand -- which is the step that gets skipped the night before a draft.
        """
        assert wl._pid_key("Luther Burden III", "WR") == wl._pid_key("Luther Burden", "WR")
        assert wl._pid_key("Michael Pittman Jr.", "WR") == wl._pid_key("Michael Pittman", "WR")

    def test_position_still_separates_players(self) -> None:
        assert wl._pid_key("Josh Allen", "QB") != wl._pid_key("Josh Allen", "WR")


class TestAdpMovers:
    @staticmethod
    def _snapshot(path: Path, rows: list[dict]) -> None:
        path.write_text(json.dumps({"players": rows}), encoding="utf-8")

    def test_movers_are_ranked_by_absolute_change_in_either_direction(
        self, tmp_path: Path
    ) -> None:
        self._snapshot(
            tmp_path / "2026-08-14T00-00-00Z.json",
            [
                {"name": "Faller", "position": "RB", "adp": 100.0},
                {"name": "Riser", "position": "WR", "adp": 100.0},
                {"name": "Steady", "position": "TE", "adp": 100.0},
            ],
        )
        self._snapshot(
            tmp_path / "2026-08-25T00-00-00Z.json",
            [
                {"name": "Faller", "position": "RB", "adp": 130.0},
                {"name": "Riser", "position": "WR", "adp": 80.0},
                {"name": "Steady", "position": "TE", "adp": 101.0},
            ],
        )
        moves, label = wl._adp_movers(10, raw_dir=tmp_path)
        assert [m[1] for m in moves] == ["Faller", "Riser", "Steady"]
        assert moves[0][0] == pytest.approx(30.0)
        assert moves[1][0] == pytest.approx(-20.0), "a riser must rank on absolute movement"
        assert "2026-08-14 -> 2026-08-25" == label

    def test_kickers_and_defenses_are_excluded_because_this_league_drafts_neither(
        self, tmp_path: Path
    ) -> None:
        self._snapshot(
            tmp_path / "2026-08-14T00-00-00Z.json",
            [
                {"name": "Some Kicker", "position": "PK", "adp": 100.0},
                {"name": "Some Defense", "position": "DEF", "adp": 100.0},
                {"name": "Real Player", "position": "RB", "adp": 100.0},
            ],
        )
        self._snapshot(
            tmp_path / "2026-08-25T00-00-00Z.json",
            [
                {"name": "Some Kicker", "position": "PK", "adp": 180.0},
                {"name": "Some Defense", "position": "DEF", "adp": 170.0},
                {"name": "Real Player", "position": "RB", "adp": 105.0},
            ],
        )
        moves, _ = wl._adp_movers(10, raw_dir=tmp_path)
        assert [m[1] for m in moves] == ["Real Player"]

    def test_the_limit_is_a_depth_and_the_caller_can_see_it_bit(self, tmp_path: Path) -> None:
        """`--movers` bounds coverage, so a shallow run must be visibly shallow."""
        self._snapshot(
            tmp_path / "2026-08-14T00-00-00Z.json",
            [{"name": f"P{i}", "position": "RB", "adp": 100.0} for i in range(10)],
        )
        self._snapshot(
            tmp_path / "2026-08-25T00-00-00Z.json",
            [{"name": f"P{i}", "position": "RB", "adp": 100.0 + i} for i in range(10)],
        )
        moves, _ = wl._adp_movers(3, raw_dir=tmp_path)
        assert len(moves) == 3

    def test_a_single_snapshot_yields_no_movement_and_says_why(self, tmp_path: Path) -> None:
        self._snapshot(
            tmp_path / "2026-08-25T00-00-00Z.json",
            [{"name": "Only", "position": "RB", "adp": 100.0}],
        )
        moves, label = wl._adp_movers(10, raw_dir=tmp_path)
        assert moves == []
        assert "only one ADP snapshot" in label
