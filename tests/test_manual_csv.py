"""Tests for the manual CSV ingest adapter (prep/manual_csv.py).

No network access anywhere in this file, per CLAUDE.md ("never re-fetch in a
test") -- everything here reads from committed fixtures under
tests/fixtures/manual_csv/ or from files this test writes itself into a
tmp_path.
"""

from __future__ import annotations

import csv
import os
from datetime import date
from pathlib import Path

import pytest

from draftroom.prep import manual_csv as mc

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "manual_csv"

SEASON = 2026


# --------------------------------------------------------------------------- helpers


def _write_rows(path: Path, rows: list[list[str]], *, delimiter: str = ",") -> None:
    """Write a plain (unquoted) delimited file -- used for constructed
    scenarios below where the exact real-export quoting doesn't matter."""
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=delimiter)
        writer.writerows(rows)


QB_HEADER = ["Player", "Team", "ATT", "CMP", "YDS", "TDS", "INTS", "ATT", "YDS", "TDS", "FL", "FPTS"]
RB_HEADER = ["Player", "Team", "ATT", "YDS", "TDS", "REC", "YDS", "TDS", "FL", "FPTS"]
WR_HEADER = ["Player", "Team", "REC", "YDS", "TDS", "ATT", "YDS", "TDS", "FL", "FPTS"]
TE_HEADER = ["Player", "Team", "REC", "YDS", "TDS", "FL", "FPTS"]


# --------------------------------------------------------------------------- real-fixture parsing (all four positions)


@pytest.mark.parametrize(
    ("position", "expected_names"),
    [
        ("qb", ["Josh Allen|BUF", "Lamar Jackson|BAL", "Patrick Mahomes|KC", "Jalen Hurts|PHI", "Joe Burrow|CIN"]),
        ("rb", ["Jahmyr Gibbs|DET", "Bijan Robinson|ATL", "Christian McCaffrey|SF", "Saquon Barkley|PHI", "Breece Hall|NYJ"]),
        ("wr", ["Puka Nacua|LAR", "Ja'Marr Chase|CIN", "Justin Jefferson|MIN", "CeeDee Lamb|DAL", "Tyreek Hill|MIA"]),
        ("te", ["Trey McBride|ARI", "Brock Bowers|LV", "Sam LaPorta|DET", "Mark Andrews|BAL", "George Kittle|SF"]),
    ],
)
def test_parses_real_fixture_row_counts_and_keys(position, expected_names):
    result = mc.load_position(position, season=SEASON, manual_dir=FIXTURES_DIR, min_rows=1)
    assert result.row_count == 5
    assert sorted(result.statlines) == sorted(expected_names)


def test_qb_fixture_resolves_passing_then_rushing_never_receiving():
    result = mc.load_position("qb", season=SEASON, manual_dir=FIXTURES_DIR, min_rows=1)
    allen = result.statlines["Josh Allen|BUF"]
    assert allen.pass_att == 491.9
    assert allen.pass_cmp == 333.4
    assert allen.pass_yd == 3815.9
    assert allen.pass_td == 27.4
    assert allen.pass_int == 11.2
    assert allen.rush_att == 118.1
    assert allen.rush_yd == 585.8
    assert allen.rush_td == 11.8
    assert allen.fum_lost == 4.1
    # No receiving columns exist for QB at all.
    assert allen.rec == 0.0
    assert allen.rec_yd == 0.0
    assert allen.rec_td == 0.0


def test_te_fixture_has_zero_rushing_never_a_parse_failure():
    # TE has no rushing block at all (a third distinct shape from RB/WR) --
    # confirm a missing rush block yields clean zeros, not a shifted column
    # or a crash.
    result = mc.load_position("te", season=SEASON, manual_dir=FIXTURES_DIR, min_rows=1)
    mcbride = result.statlines["Trey McBride|ARI"]
    assert mcbride.rec == 109.0
    assert mcbride.rec_yd == 1051.3
    assert mcbride.rec_td == 6.8
    assert mcbride.fum_lost == 0.2
    assert mcbride.rush_att == 0.0
    assert mcbride.rush_yd == 0.0
    assert mcbride.rush_td == 0.0


def test_fpts_column_is_discarded_not_mapped_to_any_field():
    # StatLine has no fantasy-points field at all -- the only way to prove
    # FPTS was read and thrown away, not silently kept, is that it can't
    # show up anywhere in CANONICAL_STATS.
    from draftroom.prep.schema import CANONICAL_STATS

    assert "fpts" not in [s.lower() for s in CANONICAL_STATS]
    result = mc.load_position("qb", season=SEASON, manual_dir=FIXTURES_DIR, min_rows=1)
    burrow = result.statlines["Joe Burrow|CIN"]
    # Burrow's real FPTS (335.0) must not have landed on any component stat.
    assert 335.0 not in burrow.as_dict().values()


def test_junk_nbsp_row_and_trailing_blank_lines_never_become_a_player():
    # Every real fixture carries the NBSP junk line 2 and two trailing blank
    # lines (see tests/fixtures/manual_csv/*.csv) -- if either leaked
    # through, row_count would be off and/or a garbage "player" would exist.
    result = mc.load_position("wr", season=SEASON, manual_dir=FIXTURES_DIR, min_rows=1)
    assert result.row_count == 5
    for key in result.statlines:
        name = key.split("|", 1)[0]
        assert name.strip() != ""
        assert "\xa0" not in name


# --------------------------------------------------------------------------- THE swap test (rushing vs. receiving)


def test_rb_and_wr_distinguish_rushing_from_receiving_yards():
    """The load-bearing test: RB is rushing-then-receiving, WR is
    receiving-then-rushing -- mirror images of each other. Every assertion
    below uses two DIFFERENT numbers for the rushing and receiving side of
    the same player, so if the column mapping for either position is ever
    swapped, at least one of these assertions fails loudly instead of
    silently producing a plausible-looking wrong total.
    """
    rb = mc.load_position("rb", season=SEASON, manual_dir=FIXTURES_DIR, min_rows=1)
    gibbs = rb.statlines["Jahmyr Gibbs|DET"]
    assert gibbs.rush_yd == 1382.0  # first YDS column for RB
    assert gibbs.rec_yd == 581.1  # second YDS column for RB
    assert gibbs.rush_att == 274.7
    assert gibbs.rec == 71.3
    assert gibbs.rush_td == 13.8
    assert gibbs.rec_td == 4.1

    wr = mc.load_position("wr", season=SEASON, manual_dir=FIXTURES_DIR, min_rows=1)
    nacua = wr.statlines["Puka Nacua|LAR"]
    assert nacua.rec_yd == 1539.0  # first YDS column for WR
    assert nacua.rush_yd == 85.0  # second YDS column for WR
    assert nacua.rec == 117.0
    assert nacua.rush_att == 13.6
    assert nacua.rec_td == 9.0
    assert nacua.rush_td == 1.4


def test_manually_swapping_the_rb_layout_breaks_the_swap_test():
    """Proof that the swap test above actually has teeth: simulate the exact
    mistake (swapping the rushing and receiving stat-column assignments for
    RB) by monkeypatching POSITION_LAYOUTS, and confirm the swap-detecting
    assertions above would fail against the corrupted layout. Restores the
    real layout in a `finally` so this can't leak into other tests.
    """
    real_layout = mc.POSITION_LAYOUTS["rb"]
    swapped_layout = (
        ("Player", None),
        ("Team", None),
        ("ATT", "rec"),       # swapped: was rush_att
        ("YDS", "rec_yd"),    # swapped: was rush_yd
        ("TDS", "rec_td"),    # swapped: was rush_td
        ("REC", "rush_att"),  # swapped: was rec
        ("YDS", "rush_yd"),   # swapped: was rec_yd
        ("TDS", "rush_td"),   # swapped: was rec_td
        ("FL", "fum_lost"),
        ("FPTS", None),
    )
    try:
        mc.POSITION_LAYOUTS["rb"] = swapped_layout
        result = mc.load_position("rb", season=SEASON, manual_dir=FIXTURES_DIR, min_rows=1)
        gibbs = result.statlines["Jahmyr Gibbs|DET"]
        # Under the swap, rush_yd now reads what was really receiving yards.
        assert gibbs.rush_yd == 581.1
        assert gibbs.rec_yd == 1382.0
        # And the real assertion from the swap test above now FAILS:
        with pytest.raises(AssertionError):
            assert gibbs.rush_yd == 1382.0
    finally:
        mc.POSITION_LAYOUTS["rb"] = real_layout

    # Confirm the real layout is restored and the swap test passes again.
    result = mc.load_position("rb", season=SEASON, manual_dir=FIXTURES_DIR, min_rows=1)
    assert result.statlines["Jahmyr Gibbs|DET"].rush_yd == 1382.0


# --------------------------------------------------------------------------- staleness guard


def test_no_file_found_raises_and_names_both_accepted_filename_forms(tmp_path):
    with pytest.raises(mc.NoFileFoundError) as exc_info:
        mc.load_position("wr", season=SEASON, manual_dir=tmp_path)
    msg = str(exc_info.value)
    assert "FantasyPros_Fantasy_Football_Projections_WR.csv" in msg
    assert "fantasypros_wr_2026_YYYY-MM-DD.csv" in msg


def test_stale_native_file_raises_loudly(tmp_path):
    path = tmp_path / "FantasyPros_Fantasy_Football_Projections_WR.csv"
    _write_rows(path, [WR_HEADER, ["Puka Nacua", "LAR", "117", "1539", "9", "13", "85", "1", "1", "281"]])
    old_mtime = date(2026, 7, 1)
    os.utime(path, (0, __import__("time").mktime(old_mtime.timetuple())))

    with pytest.raises(mc.StaleFileError) as exc_info:
        mc.load_position(
            "wr", season=SEASON, manual_dir=tmp_path, min_rows=1, as_of=date(2026, 8, 17),
        )
    msg = str(exc_info.value)
    assert "FantasyPros_Fantasy_Football_Projections_WR.csv" in msg
    assert "2026-07-01" in msg
    assert "days" in msg


def test_fresh_native_file_within_threshold_loads_fine(tmp_path):
    path = tmp_path / "FantasyPros_Fantasy_Football_Projections_WR.csv"
    _write_rows(path, [WR_HEADER, ["Puka Nacua", "LAR", "117", "1539", "9", "13", "85", "1", "1", "281"]])
    recent_mtime = date(2026, 8, 10)
    os.utime(path, (0, __import__("time").mktime(recent_mtime.timetuple())))

    result = mc.load_position(
        "wr", season=SEASON, manual_dir=tmp_path, min_rows=1, as_of=date(2026, 8, 17),
    )
    assert result.age_days == 7
    assert result.file.name == "FantasyPros_Fantasy_Football_Projections_WR.csv"
    assert "2026-08-10" in result.summary


def test_season_mismatch_on_dated_filename_form_raises(tmp_path):
    path = tmp_path / "fantasypros_wr_2025_2026-08-10.csv"
    _write_rows(path, [WR_HEADER, ["Puka Nacua", "LAR", "117", "1539", "9", "13", "85", "1", "1", "281"]])

    with pytest.raises(mc.SeasonMismatchError) as exc_info:
        mc.load_position(
            "wr", season=2026, manual_dir=tmp_path, min_rows=1, as_of=date(2026, 8, 17),
        )
    assert "2025" in str(exc_info.value)
    assert "2026" in str(exc_info.value)


def test_native_filename_form_has_no_season_to_check_so_never_raises_season_mismatch(tmp_path):
    # A native FantasyPros filename carries no season marker -- the season
    # check must be a documented no-op for it, not a false-positive raise.
    path = tmp_path / "FantasyPros_Fantasy_Football_Projections_WR.csv"
    _write_rows(path, [WR_HEADER, ["Puka Nacua", "LAR", "117", "1539", "9", "13", "85", "1", "1", "281"]])
    os.utime(path, (0, __import__("time").mktime(date(2026, 8, 15).timetuple())))

    result = mc.load_position(
        "wr", season=1999, manual_dir=tmp_path, min_rows=1, as_of=date(2026, 8, 17),
    )
    assert result.row_count == 1


def test_dated_form_staleness_uses_filename_date_not_mtime(tmp_path):
    # The dated form must NOT trust filesystem mtime (that's exactly why it
    # embeds its own date -- mtimes get reset by copying/syncing). Give the
    # file a fresh mtime but an old filename date and confirm it still
    # raises stale.
    path = tmp_path / "fantasypros_wr_2026_2026-07-01.csv"
    _write_rows(path, [WR_HEADER, ["Puka Nacua", "LAR", "117", "1539", "9", "13", "85", "1", "1", "281"]])
    os.utime(path, (0, __import__("time").mktime(date(2026, 8, 17).timetuple())))  # fresh mtime, old filename date

    with pytest.raises(mc.StaleFileError):
        mc.load_position(
            "wr", season=2026, manual_dir=tmp_path, min_rows=1, as_of=date(2026, 8, 17),
        )


def test_staleness_threshold_is_a_parameter_not_buried(tmp_path):
    path = tmp_path / "FantasyPros_Fantasy_Football_Projections_WR.csv"
    _write_rows(path, [WR_HEADER, ["Puka Nacua", "LAR", "117", "1539", "9", "13", "85", "1", "1", "281"]])
    os.utime(path, (0, __import__("time").mktime(date(2026, 8, 1).timetuple())))

    # 16 days old: fails the default 10-day threshold...
    with pytest.raises(mc.StaleFileError):
        mc.load_position(
            "wr", season=SEASON, manual_dir=tmp_path, min_rows=1, as_of=date(2026, 8, 17),
        )
    # ...but passes when the caller widens the threshold explicitly.
    result = mc.load_position(
        "wr", season=SEASON, manual_dir=tmp_path, min_rows=1, as_of=date(2026, 8, 17), max_age_days=30,
    )
    assert result.row_count == 1


# --------------------------------------------------------------------------- header drift / unmapped columns


def test_missing_team_column_is_a_hard_failure_not_a_guess(tmp_path):
    # A genuine layout change (Team column removed) must never be silently
    # absorbed by the leading-blank-column tolerance.
    path = tmp_path / "FantasyPros_Fantasy_Football_Projections_WR.csv"
    bad_header = ["Player", "REC", "YDS", "TDS", "ATT", "YDS", "TDS", "FL", "FPTS"]
    _write_rows(path, [bad_header, ["Puka Nacua", "117", "1539", "9", "13", "85", "1", "1", "281"]])

    with pytest.raises(mc.UnmappedColumnError) as exc_info:
        mc.load_position("wr", season=SEASON, manual_dir=tmp_path, min_rows=1)
    assert "WR" in str(exc_info.value)


def test_extra_unexpected_column_is_a_hard_failure(tmp_path):
    path = tmp_path / "FantasyPros_Fantasy_Football_Projections_TE.csv"
    bad_header = ["Player", "Team", "REC", "YDS", "TDS", "TGT", "FL", "FPTS"]  # unexpected TGT column
    _write_rows(path, [bad_header, ["Trey McBride", "ARI", "109", "1051", "6", "150", "0", "199"]])

    with pytest.raises(mc.UnmappedColumnError):
        mc.load_position("te", season=SEASON, manual_dir=tmp_path, min_rows=1)


# --------------------------------------------------------------------------- paste-artifact tolerance


def test_leading_blank_column_is_tolerated(tmp_path):
    path = tmp_path / "FantasyPros_Fantasy_Football_Projections_TE.csv"
    rows = [
        [""] + TE_HEADER,
        [""] + ["Trey McBride", "ARI", "109", "1051.3", "6.8", "0.2", "199.9"],
    ]
    _write_rows(path, rows)

    result = mc.load_position("te", season=SEASON, manual_dir=tmp_path, min_rows=1)
    mcbride = result.statlines["Trey McBride|ARI"]
    assert mcbride.rec == 109.0
    assert mcbride.rec_yd == 1051.3


def test_repeated_header_row_mid_file_is_skipped(tmp_path):
    path = tmp_path / "FantasyPros_Fantasy_Football_Projections_TE.csv"
    rows = [
        TE_HEADER,
        ["Trey McBride", "ARI", "109", "1051.3", "6.8", "0.2", "199.9"],
        TE_HEADER,  # repeated header, e.g. a second copied table
        ["Brock Bowers", "LV", "96.5", "1026.3", "7.5", "0.2", "195.7"],
    ]
    _write_rows(path, rows)

    result = mc.load_position("te", season=SEASON, manual_dir=tmp_path, min_rows=1)
    assert result.row_count == 2
    assert "Trey McBride|ARI" in result.statlines
    assert "Brock Bowers|LV" in result.statlines


def test_footnote_row_with_wrong_column_count_is_skipped(tmp_path):
    path = tmp_path / "FantasyPros_Fantasy_Football_Projections_TE.csv"
    rows = [
        TE_HEADER,
        ["Trey McBride", "ARI", "109", "1051.3", "6.8", "0.2", "199.9"],
        ["Consensus update: 2026-08-15"],  # footnote, wrong column count
        ["Brock Bowers", "LV", "96.5", "1026.3", "7.5", "0.2", "195.7"],
    ]
    _write_rows(path, rows)

    result = mc.load_position("te", season=SEASON, manual_dir=tmp_path, min_rows=1)
    assert result.row_count == 2


def test_row_with_non_numeric_stat_value_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "FantasyPros_Fantasy_Football_Projections_TE.csv"
    rows = [
        TE_HEADER,
        ["Trey McBride", "ARI", "109", "1051.3", "6.8", "0.2", "199.9"],
        ["Bye Week Note", "--", "N/A", "N/A", "N/A", "N/A", "N/A"],  # right column count, garbage values
        ["Brock Bowers", "LV", "96.5", "1026.3", "7.5", "0.2", "195.7"],
    ]
    _write_rows(path, rows)

    result = mc.load_position("te", season=SEASON, manual_dir=tmp_path, min_rows=1)
    assert result.row_count == 2
    assert "Bye Week Note|--" not in result.statlines


def test_tsv_delimiter_is_sniffed_and_parses_identically(tmp_path):
    path = tmp_path / "FantasyPros_Fantasy_Football_Projections_TE.tsv"
    rows = [TE_HEADER, ["Trey McBride", "ARI", "109", "1051.3", "6.8", "0.2", "199.9"]]
    _write_rows(path, rows, delimiter="\t")

    result = mc.load_position("te", season=SEASON, manual_dir=tmp_path, min_rows=1)
    mcbride = result.statlines["Trey McBride|ARI"]
    assert mcbride.rec == 109.0
    assert mcbride.rec_yd == 1051.3


def test_thousands_separator_in_yardage_is_tolerated(tmp_path):
    path = tmp_path / "FantasyPros_Fantasy_Football_Projections_TE.csv"
    rows = [TE_HEADER, ["Trey McBride", "ARI", "109", "1,051.3", "6.8", "0.2", "199.9"]]
    _write_rows(path, rows)

    result = mc.load_position("te", season=SEASON, manual_dir=tmp_path, min_rows=1)
    assert result.statlines["Trey McBride|ARI"].rec_yd == 1051.3


def test_blank_rushing_cells_yield_zero_not_a_crash(tmp_path):
    # A WR row with no rushing line at all -- blank cells, not "0".
    path = tmp_path / "FantasyPros_Fantasy_Football_Projections_WR.csv"
    rows = [WR_HEADER, ["Some Possession WR", "NYG", "80", "900", "5", "", "", "", "0", "150"]]
    _write_rows(path, rows)

    result = mc.load_position("wr", season=SEASON, manual_dir=tmp_path, min_rows=1)
    wr = result.statlines["Some Possession WR|NYG"]
    assert wr.rec == 80.0
    assert wr.rush_att == 0.0
    assert wr.rush_yd == 0.0
    assert wr.rush_td == 0.0


# --------------------------------------------------------------------------- row-count floor


def test_too_few_rows_raises_using_default_floor():
    # The real fixtures are deliberately trimmed to 5 rows -- below every
    # position's default floor (qb=24, rb=40, wr=40, te=20) -- so loading
    # without a min_rows override must raise TooFewRowsError.
    with pytest.raises(mc.TooFewRowsError) as exc_info:
        mc.load_position("qb", season=SEASON, manual_dir=FIXTURES_DIR)
    assert "24" in str(exc_info.value)


def test_too_few_rows_floor_is_a_parameter(tmp_path):
    path = tmp_path / "FantasyPros_Fantasy_Football_Projections_TE.csv"
    rows = [TE_HEADER] + [
        [f"Player {i}", "XX", "50", "500", "3", "0.5", "100"] for i in range(5)
    ]
    _write_rows(path, rows)

    with pytest.raises(mc.TooFewRowsError):
        mc.load_position("te", season=SEASON, manual_dir=tmp_path, min_rows=10)

    result = mc.load_position("te", season=SEASON, manual_dir=tmp_path, min_rows=5)
    assert result.row_count == 5


# --------------------------------------------------------------------------- graceful multi-source degrade


def test_load_all_positions_omits_missing_positions_without_raising(tmp_path):
    path = tmp_path / "FantasyPros_Fantasy_Football_Projections_WR.csv"
    _write_rows(path, [WR_HEADER, ["Puka Nacua", "LAR", "117", "1539", "9", "13", "85", "1", "1", "281"]])
    # No QB/RB/TE files at all.

    results = mc.load_all_positions(season=SEASON, manual_dir=tmp_path, min_rows={"wr": 1})
    assert set(results) == {"wr"}
    assert results["wr"].row_count == 1


def test_load_all_positions_still_raises_for_a_stale_present_file(tmp_path):
    fresh = tmp_path / "FantasyPros_Fantasy_Football_Projections_WR.csv"
    _write_rows(fresh, [WR_HEADER, ["Puka Nacua", "LAR", "117", "1539", "9", "13", "85", "1", "1", "281"]])
    os.utime(fresh, (0, __import__("time").mktime(date(2026, 6, 1).timetuple())))

    with pytest.raises(mc.StaleFileError):
        mc.load_all_positions(
            season=SEASON, manual_dir=tmp_path, min_rows={"wr": 1}, as_of=date(2026, 8, 17),
        )


# --------------------------------------------------------------------------- filename discovery / newest-wins


def test_find_latest_file_prefers_the_newer_of_two_dated_files(tmp_path):
    older = tmp_path / "fantasypros_wr_2026_2026-08-01.csv"
    newer = tmp_path / "fantasypros_wr_2026_2026-08-15.csv"
    _write_rows(older, [WR_HEADER, ["Old Guy", "XX", "1", "1", "1", "1", "1", "1", "0", "1"]])
    _write_rows(newer, [WR_HEADER, ["New Guy", "XX", "1", "1", "1", "1", "1", "1", "0", "1"]])

    latest = mc.find_latest_file("wr", manual_dir=tmp_path)
    assert latest.path == newer
    assert latest.download_date == date(2026, 8, 15)


def test_unknown_position_raises_manual_csv_error(tmp_path):
    with pytest.raises(mc.ManualCsvError):
        mc.load_position("k", season=SEASON, manual_dir=tmp_path)
