"""Tests for the outlier review queue's detection half (``valuation/candidates.py``).

Two kinds of test, deliberately separated, matching how ``test_composite.py`` and
``test_envelope.py`` are already organised.

*Hand-built fixtures* prove the properties that must hold regardless of which season is cached:
that nothing is ever auto-rejected, that the odd-source-out is identified correctly, that a
constant is caught and a varying figure is not, that one row exists per decision key, and that
the board-impact column is a real revaluation rather than a guess.

*Real cached data* smoke tests pin the findings the queue actually produced on the 2026 board, so
a future change cannot quietly lose them. They SKIP rather than fail when the cache is absent --
no test in this repo may hit the network.
"""

from __future__ import annotations

import pytest

from draftroom.config import LeagueConfig
from draftroom.prep.schema import StatLine
from draftroom.valuation import candidates as C
from draftroom.valuation.decisions import ALL_STATS, RejectedIndex, parse_decisions, rejected_index
from draftroom.valuation.evob import compute_draft_values
from draftroom.valuation.replacement import PlayerSeason

HALF_PPR = {
    "pass_yd": 0.04, "pass_td": 4.0, "pass_int": -2.0,
    "rush_yd": 0.1, "rush_td": 6.0,
    "rec": 0.5, "rec_yd": 0.1, "rec_td": 6.0,
    "fum_lost": -2.0,
}


def cfg_10team() -> LeagueConfig:
    """The confirmed real league shape (10 teams, 2 QB), small enough to reason about."""
    return LeagueConfig(
        teams=10,
        starters={"QB": 2, "RB": 2, "WR": 3, "TE": 1},
        flex_slots=1,
        flex_eligible=frozenset({"RB", "WR", "TE"}),
        bench=6,
        weeks=17,
        scoring=HALF_PPR,
    )


class FakeBoard:
    """The three attributes the queue reads off a ``RealBoard``, and nothing else."""

    def __init__(self, seasons, points_by_source=None, source="blend"):
        self.seasons = tuple(seasons)
        self.players = tuple(seasons)
        self.points_by_source = points_by_source or {}
        self.source = source


def wr(pid: str, rec_yd: float, rec: float = 60.0, rec_td: float = 5.0) -> StatLine:
    return StatLine(rec=rec, rec_yd=rec_yd, rec_td=rec_td, rec_tgt=rec * 1.5)


def build_inputs(
    *,
    statlines_by_source,
    adp_of=None,
    pos_of=None,
    games_sources=frozenset({"espn"}),
    games_distinct=None,
    board=None,
    cfg=None,
    injury_status=None,
    rejections=None,
) -> C.ReviewInputs:
    """A hermetic :class:`ReviewInputs`. No cached file is read and no network is touched."""
    pids = sorted({pid for lines in statlines_by_source.values() for pid in lines})
    pos_of = pos_of or {pid: "WR" for pid in pids}
    seasons = board.seasons if board is not None else tuple(
        PlayerSeason(
            player_id=pid, pos=pos_of[pid], ppg=10.0, expected_games=16.0, name=f"P{pid}"
        )
        for pid in pids
    )
    return C.ReviewInputs(
        cfg=cfg or cfg_10team(),
        board=board or FakeBoard(seasons),
        statlines_by_source=statlines_by_source,
        pos_of=pos_of,
        name_of={pid: f"P{pid}" for pid in pids},
        team_of={pid: "SF" for pid in pids},
        adp_of=adp_of if adp_of is not None else {pid: 10.0 + i for i, pid in enumerate(pids)},
        games_sources=games_sources,
        unresolved=(),
        unresolved_ffc=(),
        espn_raw=None,
        bonus_schedule=None,
        bonus_curves=None,
        games_distinct=games_distinct
        or {s: 0 for s in statlines_by_source},
        injury_status=injury_status or {},
        practice_participation={},
        depth_chart_order={},
        rejections=rejections or RejectedIndex.empty(),
    )


# --------------------------------------------------------------- nothing is ever auto-rejected


def test_the_module_exposes_no_way_to_reject_anything():
    """The load-bearing constraint. Detection and persistence are separate modules on purpose:
    a candidate is evidence, and only a human-written line in data/projection_decisions.json can
    remove a number. If this module ever grows a verdict, this test is the alarm."""
    exported = set(C.__all__)
    assert not any("reject" in name.lower() for name in exported)
    assert not any(
        hasattr(getattr(C, name), "verdict")
        for name in exported
        if isinstance(getattr(C, name), type)
    )
    assert not hasattr(C.Candidate, "verdict")


def test_severity_never_orders_the_queue_board_impact_does():
    """A hygiene row with a big impact must outrank a defect-severity row with a small one --
    otherwise the queue is sorted by how alarming a detector's name is."""
    big_hygiene = C.Candidate(
        source="sleeper", stat="rec_yd", player_id="1", player_name="Big", pos="WR", team="SF",
        adp=5.0, values_by_source={}, unpublished_by=(), value_label="rec_yd",
        detector="identity_hygiene", severity=C.SEV_HYGIENE, reason="",
        impact=C.Impact(scope="player", computable=True, note="", dv_delta=-40.0),
    )
    small_defect = C.Candidate(
        source="sleeper", stat="rec_yd", player_id="2", player_name="Small", pos="WR", team="SF",
        adp=6.0, values_by_source={}, unpublished_by=(), value_label="rec_yd",
        detector="contamination_zero_statline", severity=C.SEV_DEFECT, reason="",
        impact=C.Impact(scope="player", computable=True, note="", dv_delta=-0.5),
    )
    assert sorted([small_defect, big_hygiene], key=C.queue_sort_key)[0] is big_hygiene


def test_a_defect_with_no_measurable_impact_sorts_first():
    """The one documented exception: an unresolved crosswalk row means the player has NO value
    at all, which is not expressible as a delta and is bigger than any of them."""
    unmeasurable = C.Candidate(
        source="crosswalk", stat=ALL_STATS, player_id=None, player_name="Ghost", pos="WR",
        team="SF", adp=99.0, values_by_source={}, unpublished_by=(), value_label="",
        detector="crosswalk_unresolved", severity=C.SEV_DEFECT, reason="",
        impact=C.Impact(scope="player", computable=False, note="not on the board"),
        actionable=False,
    )
    big = C.Candidate(
        source="sleeper", stat="rec_yd", player_id="1", player_name="Big", pos="WR", team="SF",
        adp=5.0, values_by_source={}, unpublished_by=(), value_label="rec_yd",
        detector="distance", severity=C.SEV_DISTANCE, reason="",
        impact=C.Impact(scope="player", computable=True, note="", dv_delta=-99.0),
    )
    assert sorted([big, unmeasurable], key=C.queue_sort_key)[0] is unmeasurable


# ------------------------------------------------------------------------- the distance grain


def test_odd_one_out_flags_the_source_furthest_from_the_others_median():
    flagged = C._odd_one_out({"sleeper": 185.0, "espn": 340.0, "fantasypros": 320.0})
    assert flagged is not None
    source, value, median, dev = flagged
    assert source == "sleeper"
    assert value == 185.0
    assert median == pytest.approx(330.0)
    # Denominator is the LARGER of the two, so the figure reads as a share of that number.
    assert dev == pytest.approx((330.0 - 185.0) / 330.0)


def test_odd_one_out_reads_a_published_zero_as_a_total_miss_not_a_division_by_zero():
    flagged = C._odd_one_out({"sleeper": 0.0, "espn": 320.0, "fantasypros": 320.0})
    assert flagged is not None
    assert flagged[0] == "sleeper"
    assert flagged[3] == pytest.approx(1.0)


def test_odd_one_out_needs_three_sources_to_have_a_direction():
    assert C._odd_one_out({"sleeper": 100.0, "espn": 300.0}) is None


def test_distance_needs_three_sources_and_ignores_unanimous_zeros():
    inputs = build_inputs(
        statlines_by_source={
            "sleeper": {"1": wr("1", 185.0)},
            "espn": {"1": wr("1", 340.0)},
        }
    )
    assert C.detect_distance(inputs) == []

    unanimous = build_inputs(
        statlines_by_source={
            "sleeper": {"1": StatLine(rec=50.0, rec_yd=600.0)},
            "espn": {"1": StatLine(rec=50.0, rec_yd=600.0)},
            "fantasypros": {"1": StatLine(rec=50.0, rec_yd=600.0)},
        }
    )
    # Every stat agrees exactly, so nothing is an outlier -- including the structural zeros.
    assert C.detect_distance(unanimous) == []


def test_distance_flags_the_low_source_and_names_every_number_in_its_reason():
    inputs = build_inputs(
        statlines_by_source={
            "sleeper": {"1": wr("1", 185.0)},
            "espn": {"1": wr("1", 340.0)},
            "fantasypros": {"1": wr("1", 320.0)},
        }
    )
    rows = [c for c in C.detect_distance(inputs) if c.stat == "rec_yd"]
    assert len(rows) == 1
    row = rows[0]
    assert (row.source, row.stat, row.player_id) == ("sleeper", "rec_yd", "1")
    assert row.severity == C.SEV_DISTANCE
    assert row.values_by_source == {"sleeper": 185.0, "espn": 340.0, "fantasypros": 320.0}
    assert "185.0" in row.reason and "330.0" in row.reason
    assert row.detail["distance"]["n_contributing"] == 3


def test_distance_threshold_only_selects_what_is_SHOWN():
    """A tighter threshold hides rows; it never turns one into a rejection. Proven by the
    absence of any verdict on the candidate at either setting."""
    inputs = build_inputs(
        statlines_by_source={
            "sleeper": {"1": wr("1", 300.0)},
            "espn": {"1": wr("1", 340.0)},
            "fantasypros": {"1": wr("1", 320.0)},
        }
    )
    assert [c for c in C.detect_distance(inputs, rel_min=0.30) if c.stat == "rec_yd"] == []
    loose = [c for c in C.detect_distance(inputs, rel_min=0.01) if c.stat == "rec_yd"]
    assert len(loose) == 1
    assert not hasattr(loose[0], "verdict")


def test_distance_never_averages_in_a_stat_a_source_does_not_publish():
    """FantasyPros publishes no targets column at all, so it can never BE the target outlier and
    can never make one -- the same trap composite.py exists to avoid, one layer up."""
    inputs = build_inputs(
        statlines_by_source={
            "sleeper": {"1": wr("1", 300.0)},
            "espn": {"1": wr("1", 300.0)},
            "fantasypros": {"1": wr("1", 300.0)},
        }
    )
    assert not [c for c in C.detect_distance(inputs, rel_min=0.01) if c.stat == "rec_tgt"]


# ------------------------------------------------------------------------- contamination


def test_a_constant_games_figure_is_flagged_source_wide_and_a_varying_one_is_not():
    inputs = build_inputs(
        statlines_by_source={
            "sleeper": {"1": StatLine(rec_yd=900.0, games=18.0), "2": StatLine(rec_yd=800.0, games=18.0)},
            "espn": {"1": StatLine(rec_yd=900.0, games=17.0), "2": StatLine(rec_yd=800.0, games=11.0)},
        },
        games_distinct={"sleeper": 1, "espn": 2},
    )
    rows = C.detect_constant_projections(inputs)
    assert [(c.source, c.stat, c.player_id) for c in rows] == [("sleeper", "games", None)]
    assert rows[0].severity == C.SEV_DEFECT
    assert rows[0].detail["contamination_constant"]["constant"] == 18.0
    assert rows[0].detail["contamination_constant"]["n_records"] == 2
    # 18 > the league's own 17 weeks, and the reason says so rather than leaving it implied.
    assert "17" in rows[0].reason


def test_the_constant_row_says_when_the_composite_already_excludes_it():
    inputs = build_inputs(
        statlines_by_source={"sleeper": {"1": StatLine(rec_yd=900.0, games=18.0)}},
        games_distinct={"sleeper": 1},
        games_sources=frozenset({"espn"}),
    )
    row = C.detect_constant_projections(inputs)[0]
    assert row.detail["contamination_constant"]["already_excluded_by_composite"] is True
    assert "already excludes" in row.reason


def test_an_all_zero_statline_with_positive_games_is_a_placeholder_not_a_projection():
    inputs = build_inputs(
        statlines_by_source={
            "sleeper": {"1": StatLine(games=18.0), "2": StatLine(rec_yd=900.0, games=18.0)},
        },
        adp_of={"1": 118.0, "2": 20.0},
    )
    rows = C.detect_zero_statlines(inputs)
    assert [(c.source, c.stat, c.player_id) for c in rows] == [("sleeper", ALL_STATS, "1")]
    assert rows[0].severity == C.SEV_DEFECT
    assert rows[0].detail["contamination_zero_statline"]["games"] == 18.0


def test_zero_statlines_are_only_reported_for_players_in_the_ADP_feed():
    """Sleeper's universe is 3,111 records and thousands of them are unprojected placeholders.
    Reporting all of them would be a wall, not a queue -- so the whole-pool count travels as a
    number and only ranked players get a row."""
    inputs = build_inputs(
        statlines_by_source={"sleeper": {"1": StatLine(games=18.0), "9": StatLine(games=18.0)}},
        adp_of={"1": 118.0},
    )
    rows = C.detect_zero_statlines(inputs)
    assert [c.player_id for c in rows] == ["1"]


# ------------------------------------------------------------------------- board impact


def _seasons_from_statlines(statlines_by_source, pos_of, cfg, games_sources):
    """Build the board's ``PlayerSeason`` list the way ``build_real_board`` does.

    Deriving the fixture's baseline from the same statlines the impact column re-blends is the
    whole point: a fixture whose ``ppg`` was hand-written would make every before/after delta a
    comparison between two unrelated numbers, and the test would pass or fail for the wrong
    reason.
    """
    from draftroom.prep.scoring import score_statline_with_bonus
    from draftroom.validate import board as board_mod
    from draftroom.valuation.composite import blend_statlines

    seasons = []
    for pid, pos in pos_of.items():
        line, _prov = blend_statlines(
            {s: lines.get(pid) for s, lines in statlines_by_source.items()},
            pos=pos,
            games_sources=games_sources,
        )
        divisor = board_mod._games_divisor(line, cfg)
        points = score_statline_with_bonus(line.as_dict(), cfg.scoring, pos=pos, games=divisor)
        seasons.append(
            PlayerSeason(
                player_id=pid,
                pos=pos,
                ppg=points / divisor,
                expected_games=(line.games if line.games > 0 else None),
                name=pid,
            )
        )
    capped, _ = board_mod._cap_expected_games_by_curve(seasons, cfg)
    return capped


def _impact_fixture():
    """A three-source board where rejecting one source's rec_yd must move a real value."""
    cfg = cfg_10team()
    games_sources = frozenset({"sleeper", "espn"})
    pos_of: dict[str, str] = {}
    statlines: dict[str, dict[str, StatLine]] = {"sleeper": {}, "espn": {}, "fantasypros": {}}

    def add(pid: str, pos: str, line: StatLine, fp_line: StatLine | None = None) -> None:
        pos_of[pid] = pos
        from dataclasses import replace as _r

        statlines["sleeper"][pid] = _r(line, games=17.0)
        statlines["espn"][pid] = _r(line, games=17.0 - (0.05 * len(pos_of)))
        statlines["fantasypros"][pid] = _r(fp_line or line, games=0.0)

    for i in range(1, 41):
        add(f"WR{i}", "WR", StatLine(rec=95.0 - i, rec_yd=1350.0 - 25.0 * i,
                                     rec_td=9.0 - 0.15 * i, rec_tgt=140.0 - 1.5 * i))
    for i in range(1, 31):
        add(f"QB{i}", "QB", StatLine(pass_att=580.0 - 5.0 * i, pass_cmp=380.0 - 4.0 * i,
                                     pass_yd=4600.0 - 60.0 * i, pass_td=34.0 - 0.5 * i,
                                     pass_int=10.0, rush_yd=300.0 - 5.0 * i, rush_td=3.0))
    for i in range(1, 41):
        add(f"RB{i}", "RB", StatLine(rush_att=290.0 - 5.0 * i, rush_yd=1300.0 - 25.0 * i,
                                     rush_td=11.0 - 0.2 * i, rec=55.0 - 0.8 * i,
                                     rec_yd=450.0 - 8.0 * i, rec_td=2.0))
    for i in range(1, 21):
        add(f"TE{i}", "TE", StatLine(rec=90.0 - 3.0 * i, rec_yd=1050.0 - 40.0 * i,
                                     rec_td=8.0 - 0.3 * i, rec_tgt=125.0 - 4.0 * i))

    # One player where Sleeper is far low: that is the number under test.
    statlines["sleeper"]["WR1"] = StatLine(rec=94.0, rec_yd=300.0, rec_td=8.85,
                                          rec_tgt=138.5, games=17.0)

    seasons = _seasons_from_statlines(statlines, pos_of, cfg, games_sources)
    inputs = build_inputs(
        statlines_by_source=statlines,
        pos_of=pos_of,
        board=FakeBoard(seasons),
        cfg=cfg,
        games_sources=games_sources,
    )
    return inputs, C.ImpactEngine(inputs)


def test_impact_is_measured_on_top_of_decisions_already_in_force(tmp_path):
    """A second review of the same player must not show his FIRST rejection being undone.

    The impact columns rebuild each statline from the raw source lines. With no knowledge of the
    standing decisions, `rejected=()` meant "nothing was ever rejected" rather than "the board as
    it is today", so reviewing an ESPN candidate for a player whose Sleeper number Marc had
    already thrown out showed the Sleeper number coming BACK and attributed that movement to the
    ESPN decision (Codex 2026-08-21 finding 8). The delta was real; the cause named on the page
    was not.
    """
    statlines = {
        # Sleeper is wildly low, and its rec_yd is the number already rejected.
        "sleeper": {"WR1": wr("WR1", 300.0)},
        "espn": {"WR1": wr("WR1", 1200.0)},
        "fantasypros": {"WR1": wr("WR1", 1150.0)},
    }

    standing = rejected_index(
        parse_decisions(
            [
                {
                    "source": "sleeper",
                    "stat": "rec_yd",
                    "player_id": "WR1",
                    "verdict": "reject",
                    "reason": "300 rec_yd against 1150-1200 from the other two",
                    "date": "2026-08-21",
                }
            ]
        )
    )

    naive = C.ImpactEngine(build_inputs(statlines_by_source=statlines))
    aware = C.ImpactEngine(
        build_inputs(statlines_by_source=statlines, rejections=standing)
    )

    # "Before" is the board as it stands. Once the standing rejection is known, Sleeper's 300 is
    # already gone from it, so the baseline points are strictly higher than the naive engine's.
    naive_before, _ = naive.points_of("WR1", "espn", "rec_yd")
    aware_before, _ = aware.points_of("WR1", "espn", "rec_yd")
    assert aware_before > naive_before, (
        "the standing rejection must be reflected in the BEFORE column, or the page compares "
        "the proposed decision against a board that no longer exists"
    )

    # And the rejection is genuinely still applied under the hypothetical, not silently restored.
    line, _ = aware._blend("WR1", frozenset({("espn", "rec_yd")}))
    assert line.rec_yd == pytest.approx(1150.0), (
        "with Sleeper's rec_yd standing-rejected and ESPN's hypothetically rejected, only "
        "FantasyPros' 1150 should remain -- Sleeper's 300 reappearing is the bug"
    )


def test_the_impact_baseline_equals_the_boards_own_valuation():
    """The column is a diff, so the baseline has to be the board's own numbers. If this drifts,
    every impact figure on the page is measured against the wrong zero.

    The board caps expected games by the fitted availability curve BEFORE valuing, so the
    comparison has to walk the same two steps -- comparing against an uncapped valuation would
    pass only by accident.
    """
    from draftroom.validate import board as board_mod

    inputs, engine = _impact_fixture()
    capped, _ = board_mod._cap_expected_games_by_curve(
        list(inputs.board.seasons), inputs.cfg
    )
    plain = compute_draft_values(capped, inputs.cfg)
    for pid, dv in plain.items():
        assert engine._baseline_dv[pid].dv == pytest.approx(dv.dv)


def test_board_impact_is_a_real_revaluation_with_a_signed_delta_and_a_rank_move():
    inputs, engine = _impact_fixture()
    impact = engine.for_player("WR1", "sleeper", "rec_yd")
    assert impact.computable and impact.scope == "player"
    # Dropping the LOW source raises the blended yardage, so the value goes UP.
    assert impact.dv_delta > 0
    assert impact.dv_after > impact.dv_before
    assert impact.rank_before is not None and impact.rank_after is not None
    assert impact.magnitude == abs(impact.dv_delta)


def test_rejecting_a_stat_nobody_else_publishes_leaves_the_value_untouched():
    """`rec_tgt` comes from ESPN alone. Rejecting a source that never published it must be a
    no-op, and the page must be able to say so rather than implying a pending change."""
    inputs, engine = _impact_fixture()
    impact = engine.for_player("WR2", "fantasypros", "rec_tgt")
    assert impact.dv_delta == pytest.approx(0.0)
    assert impact.n_players_moved == 0


def test_a_rejection_that_removes_every_projection_drops_the_player_off_the_board():
    inputs, engine = _impact_fixture()
    only_sleeper = dict(inputs.statlines_by_source)
    only_sleeper["espn"] = {k: v for k, v in only_sleeper["espn"].items() if k != "WR3"}
    only_sleeper["fantasypros"] = {
        k: v for k, v in only_sleeper["fantasypros"].items() if k != "WR3"
    }
    inputs2 = build_inputs(
        statlines_by_source=only_sleeper,
        pos_of=inputs.pos_of,
        board=inputs.board,
        cfg=inputs.cfg,
    )
    impact = C.ImpactEngine(inputs2).for_player("WR3", "sleeper", ALL_STATS)
    assert impact.drops_from_board
    assert impact.dv_after is None
    assert impact.dv_delta == pytest.approx(-impact.dv_before)
    assert "drops off the board" in impact.describe()


def test_source_scope_impact_reports_the_aggregate_not_one_player():
    inputs, engine = _impact_fixture()
    impact = engine.for_source("sleeper", "rec_yd")
    assert impact.scope == "source"
    assert impact.n_players_moved > 1
    assert impact.worst_player


def test_source_scope_impact_says_plainly_when_nothing_would_change():
    inputs, engine = _impact_fixture()
    impact = engine.for_source("fantasypros", "rec_tgt")
    assert impact.n_players_moved == 0
    assert "no value on the board moves" in impact.note


# ------------------------------------------------------------------------- one row per key


def test_two_detectors_on_the_same_number_merge_into_one_row():
    """A decision is per (source, stat, player). Two rows for one number would let Marc keep it
    on one row and reject it on the other."""
    def cand(detector, severity, reason):
        return C.Candidate(
            source="fantasysharks", stat="pass_td", player_id="7", player_name="QB", pos="QB",
            team="NE", adp=100.0, values_by_source={}, unpublished_by=(), value_label="pass_td",
            detector=detector, severity=severity, reason=reason,
            detail={detector: {"n": 1}}, detectors=(detector,),
        )

    merged = C._merge_by_key(
        [
            cand("distance", C.SEV_DISTANCE, "far from the median."),
            cand("td_regression", C.SEV_BADGE, "outside its own yardage."),
        ]
    )
    assert len(merged) == 1
    row = merged[0]
    assert set(row.detectors) == {"distance", "td_regression"}
    assert row.severity == C.SEV_DISTANCE  # the stronger of the two
    assert "far from the median." in row.reason and "outside its own yardage." in row.reason
    assert set(row.detail) == {"distance", "td_regression"}


def test_non_actionable_rows_sharing_a_decision_key_are_not_merged_away():
    """Every unresolved FFC row keys to ("crosswalk", "*", None). Merging them would erase the
    players, and they never become decisions anyway."""
    def ghost(name):
        return C.Candidate(
            source="crosswalk", stat=ALL_STATS, player_id=None, player_name=name, pos="WR",
            team="SF", adp=120.0, values_by_source={}, unpublished_by=(), value_label="",
            detector="crosswalk_unresolved", severity=C.SEV_DEFECT, reason="",
            detectors=("crosswalk_unresolved",), actionable=False,
        )

    merged = C._merge_by_key([ghost("A"), ghost("B")])
    assert sorted(c.player_name for c in merged) == ["A", "B"]
    assert len({c.row_id() for c in merged}) == 2


def test_a_join_failure_is_not_offered_as_a_keep_or_reject():
    """The number is MISSING, not wrong, so a rejection would be a no-op recorded forever."""
    inputs = build_inputs(
        statlines_by_source={
            "sleeper": {"1": wr("1", 900.0)},
            "espn": {"1": wr("1", 900.0)},
            "fantasypros": {},
        },
        adp_of={"1": 40.0},
    )
    rows = C.detect_crosswalk_failures(inputs)
    missing = [c for c in rows if c.detector == "crosswalk_missing_source"]
    assert [c.source for c in missing] == ["fantasypros"]
    assert missing[0].actionable is False
    assert "overrides.csv" in missing[0].reason


# ------------------------------------------------------------------------- real cached data


@pytest.fixture(scope="module")
def real_inputs():
    try:
        return C.load_review_inputs()
    except FileNotFoundError as exc:
        pytest.skip(f"no cached prep data for the review queue: {exc}")


@pytest.fixture(scope="module")
def real_queue(real_inputs):
    return C.collect_candidates(real_inputs)


def test_real_board_impact_baseline_matches_the_board_itself(real_inputs):
    """The strongest available check that the impact column is measured against the real board:
    every baseline dv must equal what build_real_board already computed for that player."""
    engine = C.ImpactEngine(real_inputs)
    for player in real_inputs.board.players:
        assert engine._baseline_dv[player.player_id].dv == pytest.approx(player.dv)


def test_real_queue_finds_sleepers_constant_games_figure(real_queue):
    rows = real_queue.by_detector("contamination_constant")
    assert [(c.source, c.stat) for c in rows] == [("sleeper", "games")]
    detail = rows[0].detail["contamination_constant"]
    # The FINDING is that one value covers the entire universe and exceeds the league's own
    # season length -- a constant posing as a projection. The record COUNT is incidental: it was
    # pinned at 3111 and moved to 3113 on the next refresh, which reddened this test for a reason
    # that has nothing to do with what it checks. Printed instead, with a band wide enough to
    # catch a truncated payload.
    print(f"\nsleeper games: constant={detail['constant']} over {detail['n_records']} records")
    assert detail["constant"] == 18.0
    assert detail["constant"] > detail["league_weeks"], "the whole point: more than a full season"
    assert 2_000 <= detail["n_records"] <= 6_000, "not a plausible Sleeper skill-position universe"
    assert detail["league_weeks"] == 17.0
    # The pipeline already refuses to blend it, so the row must not imply a pending change.
    assert detail["already_excluded_by_composite"] is True
    assert rows[0].impact.n_players_moved == 0


def test_real_queue_still_sees_the_all_zero_statline_but_no_longer_calls_it_contamination(
    real_inputs, real_queue
):
    """The placeholder is still there in the cached data -- Sleeper carries Ricky Pearsall with
    games=18.0 and every component stat 0.0. What changed is the reading of it: he is out for the
    season, so an all-zero projection is CORRECT and the row is suppressed rather than surfaced.

    This test asserts both halves, because either one alone would be misleading: the raw defect
    is still detectable in the data, and the queue deliberately does not report it.
    """
    placeholders = [
        pid
        for pid, line in real_inputs.statlines_by_source["sleeper"].items()
        if pid in real_inputs.adp_of and line.games > 0 and not line.has_nonzero_stats()
    ]
    assert placeholders, "expected at least one all-zero Sleeper statline in the ranked pool"
    assert all(
        C.suppresses_missing_data(real_inputs.injury_status.get(pid)) for pid in placeholders
    ), "an all-zero statline for a player with NO designation would be real contamination"
    assert not real_queue.by_detector("contamination_zero_statline")
    assert real_queue.suppressed_by_injury.get("contamination_zero_statline", 0) == len(
        placeholders
    )


def test_real_queue_reproduces_the_documented_espn_passing_td_bias(real_queue):
    """docs/archive/PROJECTION_CHALLENGES.md: ESPN projects 11.7% fewer QB passing touchdowns than its
    own yardage implies, aggregate z -4.32. Pinned here because it is the one number that proves
    this module is wired to the same fit that write-up used."""
    rows = [
        c
        for c in real_queue.by_detector("td_source_bias")
        if c.source == "espn" and c.stat == "pass_td"
    ]
    assert len(rows) == 1
    detail = rows[0].detail["td_source_bias"]
    assert detail["pos"] == "QB"
    assert detail["ratio"] == pytest.approx(0.883, abs=0.01)
    assert detail["z"] == pytest.approx(-4.32, abs=0.05)


def test_real_queue_every_actionable_row_carries_a_computed_impact(real_queue):
    for c in real_queue.candidates:
        assert c.impact is not None
        if c.actionable:
            assert c.impact.computable or "not on the valued board" in c.impact.note


def test_real_queue_is_ranked_by_board_impact(real_queue):
    measured = [
        c for c in real_queue.candidates if c.impact and c.impact.computable and c.actionable
    ]
    magnitudes = [c.impact.magnitude for c in measured]
    assert magnitudes == sorted(magnitudes, reverse=True)


def test_real_queue_reports_its_own_floods_rather_than_hiding_them(real_queue):
    """A queue nobody can read is the same as no queue, so a detector over the flood threshold
    has to be named in the queue itself."""
    for detector, n in real_queue.counts_by_detector.items():
        if n > C.FLOOD_THRESHOLD:
            assert detector in real_queue.flooded
    if real_queue.flooded:
        assert any("FLOODED" in note for note in real_queue.notes)


def test_real_queue_identity_rows_carry_the_passer_count_and_disclaim_a_correction(real_queue):
    """docs/archive/PLAN_2026-08-20.md's VERDICT: the identity is a hygiene flag, the honest per-team
    signal is PASSER count, and no remedy ships. The row has to say all three."""
    rows = real_queue.by_detector("identity_hygiene")
    if not rows:
        pytest.skip("no identity violations in the cached board")
    for c in rows[:20]:
        detail = c.detail["identity_hygiene"]
        assert "projected_passers" in detail
        assert detail["no_correction_warranted"] is True
        assert "HYGIENE FLAG ONLY" in c.reason
        assert "no correction is warranted" in c.reason


def test_real_queue_never_contains_a_verdict(real_queue):
    """The whole point, asserted against real data: detection produces evidence, never a
    decision. Not one row carries a keep/reject."""
    for c in real_queue.candidates:
        assert not hasattr(c, "verdict")
        assert C.SEV_DEFECT in C.SEVERITIES  # severity is a claim on attention, not a verdict


# ------------------------------------------------- the injury / playing-time detector


def test_the_designation_vocabulary_is_split_by_what_a_season_projection_must_price_in():
    """A weekly game-status tag and a will-not-play designation are different facts. 28 of the
    33 designated players in the real ranked pool carry Questionable in August, Puka Nacua and
    Christian McCaffrey among them -- a detector that fires on those is noise with a severity
    label on it."""
    for long_term in ("IR", "PUP", "Out", "Sus", "NA", "DNR", "suspended"):
        assert C.is_long_term_designation(long_term)
    for short in ("Questionable", "Doubtful", "probable"):
        assert not C.is_long_term_designation(short)
    assert not C.is_long_term_designation(None)
    assert not C.is_long_term_designation("")


def test_an_unrecognised_designation_is_surfaced_but_may_not_excuse_missing_data():
    """Sleeper can add a code at any time. Surfacing an unknown one is safe (the row only ever
    informs); letting an unknown one silence a contamination finding is not."""
    assert C.is_long_term_designation("Sprained-Vibes")
    assert not C.suppresses_missing_data("Sprained-Vibes")
    assert C.suppresses_missing_data("IR")
    assert not C.suppresses_missing_data("Questionable")


def _injury_fixture(injury_status, *, espn_games=17.0):
    """One board, one designated player, everything else healthy."""
    cfg = cfg_10team()
    games_sources = frozenset({"espn"})
    pos_of, statlines = {}, {"sleeper": {}, "espn": {}}
    from dataclasses import replace as _r

    for i in range(1, 41):
        pid = f"WR{i}"
        pos_of[pid] = "WR"
        line = StatLine(rec=95.0 - i, rec_yd=1350.0 - 25.0 * i, rec_td=9.0 - 0.15 * i)
        statlines["sleeper"][pid] = _r(line, games=18.0)
        statlines["espn"][pid] = _r(line, games=espn_games if pid == "WR5" else 17.0)
    for i in range(1, 31):
        pid = f"QB{i}"
        pos_of[pid] = "QB"
        line = StatLine(pass_yd=4600.0 - 60.0 * i, pass_td=34.0 - 0.5 * i, pass_int=10.0)
        statlines["sleeper"][pid] = _r(line, games=18.0)
        statlines["espn"][pid] = _r(line, games=17.0)
    for i in range(1, 41):
        pid = f"RB{i}"
        pos_of[pid] = "RB"
        line = StatLine(rush_yd=1300.0 - 25.0 * i, rush_td=11.0 - 0.2 * i, rec=40.0 - 0.5 * i)
        statlines["sleeper"][pid] = _r(line, games=18.0)
        statlines["espn"][pid] = _r(line, games=17.0)
    for i in range(1, 21):
        pid = f"TE{i}"
        pos_of[pid] = "TE"
        line = StatLine(rec=90.0 - 3.0 * i, rec_yd=1050.0 - 40.0 * i)
        statlines["sleeper"][pid] = _r(line, games=18.0)
        statlines["espn"][pid] = _r(line, games=17.0)

    seasons = _seasons_from_statlines(statlines, pos_of, cfg, games_sources)
    return build_inputs(
        statlines_by_source=statlines,
        pos_of=pos_of,
        board=FakeBoard(seasons),
        cfg=cfg,
        games_sources=games_sources,
        injury_status=injury_status,
    )


def test_a_designation_with_no_playing_time_discount_at_all_is_a_defect():
    """The Alec Pierce case: PUP, and the board credits him with the curve figure, which is the
    figure for a player nothing player-specific is known about."""
    inputs = _injury_fixture({"WR5": "PUP"})
    rows = C.detect_injury_vs_expected_games(inputs)
    assert [c.player_id for c in rows] == ["WR5"]
    row = rows[0]
    assert row.severity == C.SEV_DEFECT
    detail = row.detail["injury_vs_expected_games"]
    assert detail["no_discount_applied"] is True
    assert detail["discount_applied"] == pytest.approx(0.0)
    assert detail["games_credited_by_board"] == pytest.approx(detail["healthy_rank_curve_games"])


def test_a_designation_a_source_already_discounted_is_hygiene_not_a_defect():
    """Kittle and Charbonnet: ESPN priced something in, so the only open question is whether it
    priced in ENOUGH -- and this module has no number for that, by design."""
    inputs = _injury_fixture({"WR5": "PUP"}, espn_games=11.0)
    (row,) = C.detect_injury_vs_expected_games(inputs)
    assert row.severity == C.SEV_HYGIENE
    detail = row.detail["injury_vs_expected_games"]
    assert detail["no_discount_applied"] is False
    assert detail["discount_applied"] > 0
    assert "ENOUGH" in row.reason


def test_the_reason_prints_BOTH_numbers_being_compared_and_the_adp():
    inputs = _injury_fixture({"WR5": "PUP"})
    (row,) = C.detect_injury_vs_expected_games(inputs)
    detail = row.detail["injury_vs_expected_games"]
    assert f"{detail['games_credited_by_board']:.2f}" in row.reason
    assert "PUP" in row.reason
    assert f"ADP {row.adp:.1f}" in row.reason
    # Every source's own games figure, so "no source is at fault" is checkable on the row.
    assert "espn 17.0" in row.reason and "sleeper 18.0" in row.reason


def test_no_games_missed_figure_is_asserted_for_any_designation():
    """The constraint that matters most: the detector must not invent what PUP costs."""
    inputs = _injury_fixture({"WR5": "PUP"})
    (row,) = C.detect_injury_vs_expected_games(inputs)
    assert "No games-missed figure is asserted" in row.reason
    assert row.detail["injury_vs_expected_games"]["empirical_fit"] == (
        C.NO_EMPIRICAL_DESIGNATION_FIT
    )
    assert "not fittable from this repo's cache" in C.NO_EMPIRICAL_DESIGNATION_FIT


def test_a_short_term_game_status_tag_never_fires():
    assert C.detect_injury_vs_expected_games(_injury_fixture({"WR5": "Questionable"})) == []
    assert C.detect_injury_vs_expected_games(_injury_fixture({})) == []


def test_a_playing_time_row_is_never_offered_as_a_keep_or_reject():
    """`blend_statlines(rejected=...)` cannot express "the availability figure is wrong", and no
    source's number is at fault, so a rejection here would be a no-op recorded forever."""
    inputs = _injury_fixture({"WR5": "PUP"})
    (row,) = C.detect_injury_vs_expected_games(inputs)
    assert row.actionable is False
    assert row.source == C.PLAYING_TIME_PSEUDO_SOURCE
    assert row.impact is not None and row.impact.computable is False
    assert "not a rejection" in row.reason
    assert "playing-time override" in row.reason


def test_the_playing_time_pseudo_source_cannot_reach_the_decisions_file():
    """The strongest form of "not actionable": even a hand-written entry naming it is refused."""
    from draftroom.valuation import decisions as D

    with pytest.raises(D.DecisionsFileError, match="unknown source"):
        D.parse_decisions(
            [
                {
                    "source": C.PLAYING_TIME_PSEUDO_SOURCE,
                    "stat": "games",
                    "player_id": "WR5",
                    "verdict": "reject",
                    "reason": "PUP",
                    "date": "2026-08-20",
                }
            ]
        )


def test_a_designated_player_already_off_the_board_gets_no_row():
    """His designation EXPLAINS the exclusion rather than contradicting it, so there is no
    playing-time assumption left to be wrong. Ricky Pearsall's case."""
    inputs = _injury_fixture({"WR5": "IR"})
    ghost = build_inputs(
        statlines_by_source=inputs.statlines_by_source,
        pos_of=inputs.pos_of,
        board=FakeBoard([s for s in inputs.board.seasons if s.player_id != "WR5"]),
        cfg=inputs.cfg,
        games_sources=inputs.games_sources,
        injury_status={"WR5": "IR"},
    )
    assert C.detect_injury_vs_expected_games(ghost) == []


def test_effective_games_matches_what_the_board_actually_valued(real_inputs):
    """The detector's left-hand number has to be the games the board CREDITED, including the
    case where no source published one and the availability prior filled it in."""
    engine = C.ImpactEngine(real_inputs)
    games = C.effective_games_by_pid(real_inputs)
    for player in real_inputs.board.players:
        credited, _curve, _rank = games[player.player_id]
        assert credited == pytest.approx(engine._baseline_dv[player.player_id].expected_games)


# ------------------------------------------------- injury gating on the contamination detectors


def test_an_all_zero_statline_is_contamination_for_a_healthy_player_and_truth_for_an_IR_one():
    healthy = build_inputs(
        statlines_by_source={"sleeper": {"1": StatLine(games=18.0)}},
        adp_of={"1": 118.0},
    )
    assert len(C.detect_zero_statlines(healthy)) == 1

    injured = build_inputs(
        statlines_by_source={"sleeper": {"1": StatLine(games=18.0)}},
        adp_of={"1": 118.0},
        injury_status={"1": "IR"},
    )
    suppressed: dict[str, int] = {}
    assert C.detect_zero_statlines(injured, suppressed=suppressed) == []
    assert suppressed == {"contamination_zero_statline": 1}


def test_a_source_declining_to_publish_an_IR_player_is_not_a_join_failure():
    lines = {"sleeper": {"1": wr("1", 900.0)}, "espn": {}, "fantasypros": {}}
    healthy = build_inputs(statlines_by_source=lines, adp_of={"1": 40.0})
    assert len(C.detect_crosswalk_failures(healthy)) == 2

    injured = build_inputs(
        statlines_by_source=lines, adp_of={"1": 40.0}, injury_status={"1": "IR"}
    )
    suppressed: dict[str, int] = {}
    assert C.detect_crosswalk_failures(injured, suppressed=suppressed) == []
    assert suppressed == {"crosswalk_missing_source": 2}


def test_an_unrecognised_designation_does_not_silence_contamination():
    """Suppression is the one place an unknown code must NOT get the benefit of the doubt."""
    inputs = build_inputs(
        statlines_by_source={"sleeper": {"1": StatLine(games=18.0)}},
        adp_of={"1": 118.0},
        injury_status={"1": "Vibes"},
    )
    assert len(C.detect_zero_statlines(inputs)) == 1


# ------------------------------------------------- real cached data


def test_the_injury_detector_contract_holds_for_every_row_it_produces(real_queue):
    """The detector's own invariant, over whatever it flags today. Never player-specific.

    ``no_discount_applied`` must mean exactly ``credited == curve``: the board gave this player
    the figure for someone nothing is known about. That equality IS the finding, and it holds
    regardless of which players happen to be designated this week.
    """
    rows = real_queue.by_detector("injury_vs_expected_games")
    assert rows, "expected at least one long-term designation in the ranked pool"
    for row in rows:
        detail = row.detail["injury_vs_expected_games"]
        assert detail["designation"], row.player_name
        assert detail["designation_recognised"] is True, row.player_name
        credited = detail["games_credited_by_board"]
        curve = detail["healthy_rank_curve_games"]
        if detail["no_discount_applied"]:
            assert credited == pytest.approx(curve, abs=0.01), (
                f"{row.player_name}: no_discount_applied claims the board applied nothing, so "
                f"credited ({credited}) must equal the curve ({curve})"
            )
        else:
            assert credited < curve, row.player_name
            assert detail["discount_applied"] == pytest.approx(curve - credited, abs=0.01)
        # A rejection cannot move an availability figure, so no row here is ever actionable.
        assert row.actionable is False, row.player_name


def test_real_queue_flags_alec_pierce_as_the_top_playing_time_row(real_queue):
    """The finding that produced this detector: a top-75 receiver on PUP whom nothing in the
    pipeline discounted, because the availability curve cannot read a designation and ESPN
    projected him for a full season.

    Kept as the original regression case, but it is pinned to ONE MAN'S REAL-WORLD HEALTH, so it
    skips rather than fails once he is no longer designated -- a healed player is the world
    changing, not this detector breaking. The contract test above is what stays non-vacuous
    either way. The exact figures it used to assert (15.50 games, ADP 70.3) are deliberately
    gone: his positional rank slid WR30 -> WR32 on an ordinary refresh, moving the curve value to
    15.23, and the ADP to 69.7.
    """
    rows = real_queue.by_detector("injury_vs_expected_games")
    pierce = [c for c in rows if c.player_name == "Alec Pierce"]
    if not pierce:
        pytest.skip(
            "Alec Pierce carries no long-term designation in the current cache -- he cleared, or "
            "an override settled him (ReviewQueue.settled_by_override). The detector contract is "
            f"covered by the test above, over: {[c.player_name for c in rows]}"
        )
    row = pierce[0]
    detail = row.detail["injury_vs_expected_games"]
    assert detail["designation"] == "PUP"
    assert detail["no_discount_applied"] is True
    # He is a top-75 pick getting the healthy-rank figure. That combination is the whole finding;
    # the specific numbers behind it move every refresh.
    assert row.adp is not None and row.adp < 75.0
    assert row.actionable is False
    # And he is at the very top of the queue, because ADP orders the rows whose impact cannot be
    # expressed as a delta.
    assert real_queue.candidates[0].row_id() == row.row_id()


def test_real_queue_suppresses_the_pearsall_false_positives(real_queue):
    """Four of the top ten rows were Ricky Pearsall, one per source, before his IR status was
    read. An all-zero statline for a player who is out is a correct projection."""
    assert not [c for c in real_queue.candidates if "Pearsall" in c.player_name]
    assert real_queue.suppressed_by_injury.get("contamination_zero_statline", 0) >= 1
    assert real_queue.suppressed_by_injury.get("crosswalk_missing_source", 0) >= 3
    # But he is still NAMED, so a suppression is never invisible.
    assert any("Pearsall" in note for note in real_queue.notes)


def test_real_queue_never_fires_the_injury_detector_on_a_weekly_tag(real_queue):
    for c in real_queue.by_detector("injury_vs_expected_games"):
        designation = c.detail["injury_vs_expected_games"]["designation"]
        assert designation not in {"QUESTIONABLE", "DOUBTFUL", "PROBABLE"}
