"""Tests for the Clay PDF ingest adapter (prep/clay_pdf.py).

No network access anywhere in this file, per CLAUDE.md ("never re-fetch in a
test"). Two kinds of fixture, matching the two layers of the module:

  - Pure-text-line fixtures (hardcoded strings below) exercise the column
    resolution logic (_parse_section_lines, _identify_position) directly,
    with no PDF library involved at all.
  - tests/fixtures/clay/sample_pages.pdf is a tiny 2-page slice of the REAL
    committed PDF (page "Quarterback Projections" in full, plus page
    "Wide Receiver Projections (1/5)" in full -- 40 real QB rows and 40 real
    WR rows, sliced with pypdfium2's page-import, not hand-authored) --
    small enough to commit, and never the full 4.9MB data/manual/ file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from draftroom.prep import clay_pdf as cp

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "clay"
SAMPLE_PDF = FIXTURES_DIR / "sample_pages.pdf"


# --------------------------------------------------------------------------- pure-line fixtures (no PDF involved)

QB_HEADER = cp.EXPECTED_HEADERS["qb"]
RB_HEADER = cp.EXPECTED_HEADERS["rb"]
WR_HEADER = cp.EXPECTED_HEADERS["wr"]
TE_HEADER = cp.EXPECTED_HEADERS["te"]

# A few real rows, copied verbatim from the actual PDF (confirmed 2026-08-17
# against data/manual/clay_projections_2026-08-17.pdf) -- not invented.
QB_ROWS = [
    "Josh Allen BUF 1 369 17 509 340 3946 26 12 36 116 580 12",
    "Lamar Jackson BLT 2 323 17 467 303 3888 26 10 39 122 671 4",
]

RB_ROWS = [
    "Jahmyr Gibbs DET 1 365 17 283 1373 14 86 68 546 3 61% 16%",
    "Ken Walker III KC 11 274 17 277 1239 9 60 48 376 2 67% 11%",
    "Jonathan Taylor IND 4 316 17 325 1500 12 63 51 381 1 78% 12%",
]

WR_ROWS = [
    "Puka Nacua LAR 1 356 17 16 106 1 174 123 1590 10 4% 31%",
    "Brian Thomas Jr. JAX 39 177 17 4 23 0 102 57 861 5 1% 18%",
    "Amon-Ra St. Brown DET 1 300 17 2 13 0 167 118 1426 10 0% 20%",
]

TE_ROWS = [
    # Trey McBride: a real receiving-only TE, 0/0/0 rushing -- the
    # column-alignment proof (see module docstring / clay_pdf.py).
    "Trey McBride ARZ 1 242 17 0 0 0 149 108 1023 5 0% 26%",
]


def _report() -> cp.ClayExtractionReport:
    return cp.ClayExtractionReport()


# --------------------------------------------------------------------------- _parse_section_lines: field mapping


def test_qb_row_maps_passing_and_rushing_but_no_receiving() -> None:
    report = _report()
    out = cp._parse_section_lines(QB_ROWS, "qb", "test", report)
    assert report.skipped_count == 0

    allen = out["Josh Allen|BUF"]
    assert (allen.pass_att, allen.pass_cmp, allen.pass_yd, allen.pass_td, allen.pass_int) == (
        509.0, 340.0, 3946.0, 26.0, 12.0,
    )
    assert (allen.rush_att, allen.rush_yd, allen.rush_td) == (116.0, 580.0, 12.0)
    assert allen.games == 17.0
    # QB layout has no receiving block at all -- must default to zero, not
    # be left unset or contaminated by an adjacent column.
    assert (allen.rec, allen.rec_tgt, allen.rec_yd, allen.rec_td) == (0.0, 0.0, 0.0, 0.0)


def test_qb_drops_sk_pos_rk_and_ff_pt() -> None:
    # Josh Allen's real row has Sk=36, Pos Rk=1, FF Pt=369 -- none of these
    # have a canonical slot and none should leak into any StatLine field.
    report = _report()
    out = cp._parse_section_lines(QB_ROWS, "qb", "test", report)
    allen = out["Josh Allen|BUF"]
    values = allen.as_dict().values()
    assert 36.0 not in values  # Sk
    assert 369.0 not in values  # FF Pt (would collide with nothing real, but confirms it's absent)


def test_rb_wr_te_share_layout_and_map_targets() -> None:
    report = _report()
    out = cp._parse_section_lines(RB_ROWS, "rb", "test", report)
    assert report.skipped_count == 0

    gibbs = out["Jahmyr Gibbs|DET"]
    assert (gibbs.rush_att, gibbs.rush_yd, gibbs.rush_td) == (283.0, 1373.0, 14.0)
    assert (gibbs.rec_tgt, gibbs.rec, gibbs.rec_yd, gibbs.rec_td) == (86.0, 68.0, 546.0, 3.0)
    assert gibbs.games == 17.0


def test_column_alignment_proof_rushing_and_receiving_not_swapped() -> None:
    """The exact proof the task asks for: pick one player and show rushing
    landed in rush_* and receiving landed in rec_* -- not swapped. Trey
    McBride is a real receiving-only TE (0 rushing volume, real receiving
    volume), which makes a swap immediately visible if it happened: a
    swapped mapping would show 149 rush attempts and 0 targets, which is
    not what a real TE profile looks like.
    """
    report = _report()
    out = cp._parse_section_lines(TE_ROWS, "te", "test", report)
    mcbride = out["Trey McBride|ARZ"]
    assert (mcbride.rush_att, mcbride.rush_yd, mcbride.rush_td) == (0.0, 0.0, 0.0)
    assert (mcbride.rec_tgt, mcbride.rec, mcbride.rec_yd, mcbride.rec_td) == (149.0, 108.0, 1023.0, 5.0)


def test_rb_workhorse_has_both_real_rushing_and_real_receiving_in_right_slots() -> None:
    # Jonathan Taylor: real rushing AND real (smaller) receiving volume in
    # the same row -- proves both blocks land correctly at once, not just
    # in the all-zero-or-all-nonzero edge cases.
    report = _report()
    out = cp._parse_section_lines(RB_ROWS, "rb", "test", report)
    taylor = out["Jonathan Taylor|IND"]
    assert (taylor.rush_att, taylor.rush_yd, taylor.rush_td) == (325.0, 1500.0, 12.0)
    assert (taylor.rec_tgt, taylor.rec, taylor.rec_yd, taylor.rec_td) == (63.0, 51.0, 381.0, 1.0)


# --------------------------------------------------------------------------- multi-word / suffixed / punctuated names


@pytest.mark.parametrize(
    ("rows", "pos", "expected_key"),
    [
        (RB_ROWS, "rb", "Ken Walker III|KC"),
        (WR_ROWS, "wr", "Brian Thomas Jr.|JAX"),
        (WR_ROWS, "wr", "Amon-Ra St. Brown|DET"),
    ],
)
def test_multiword_and_suffixed_names_resolve_correctly(rows, pos, expected_key) -> None:
    report = _report()
    out = cp._parse_section_lines(rows, pos, "test", report)
    assert expected_key in out
    assert report.skipped_count == 0


def test_apostrophe_name_is_a_single_token_and_still_resolves() -> None:
    report = _report()
    row = ["De'Von Achane MIA 5 293 17 258 1308 5 80 65 511 3 60% 16%"]
    out = cp._parse_section_lines(row, "rb", "test", report)
    assert "De'Von Achane|MIA" in out
    assert report.skipped_count == 0


# --------------------------------------------------------------------------- malformed rows: logged and counted, never silently dropped


def test_too_few_tokens_is_skipped_and_counted_not_raised() -> None:
    report = _report()
    rows = QB_ROWS + ["Truncated Row BUF 1 2 3"]  # far short of 13 trailing stat tokens
    out = cp._parse_section_lines(rows, "qb", "test", report)
    assert len(out) == 2  # only the two real rows
    assert report.skipped_count == 1
    assert "tokens" in report.skips[0].reason


def test_non_numeric_stat_value_is_skipped_and_counted() -> None:
    report = _report()
    # Same shape as a real QB row, but a footnote-style non-numeric value
    # sitting where P Att should be.
    bad_row = "Some Guy BUF 1 369 17 N/A 340 3946 26 12 36 116 580 12"
    out = cp._parse_section_lines(QB_ROWS + [bad_row], "qb", "test", report)
    assert len(out) == 2
    assert report.skipped_count == 1
    assert "non-numeric" in report.skips[0].reason


def test_blank_lines_are_skipped_silently_not_counted_as_errors() -> None:
    report = _report()
    out = cp._parse_section_lines(["", *QB_ROWS, "   ", ""], "qb", "test", report)
    assert len(out) == 2
    assert report.skipped_count == 0


# --------------------------------------------------------------------------- _identify_position: title/header detection, and the column-drift hard-fail


def test_identify_position_recognizes_all_four_positions() -> None:
    assert cp._identify_position(["Quarterback Projections", QB_HEADER], "p") == "qb"
    assert cp._identify_position(["Running Back Projections (2/3)", RB_HEADER], "p") == "rb"
    assert cp._identify_position(["Wide Receiver Projections (5/5)", WR_HEADER], "p") == "wr"
    assert cp._identify_position(["Tight End Projections (1/2)", TE_HEADER], "p") == "te"


def test_identify_position_skips_unrelated_pages() -> None:
    assert cp._identify_position(["2026 Leaderboard", "Projections"], "p") is None
    assert cp._identify_position(["Kicker Projections", "KICKER Tm FF Pt FGM FGA"], "p") is None
    assert cp._identify_position(["Interior Defensive Line Projections (1/2)", "Defender Team Pos Rk"], "p") is None
    assert cp._identify_position([], "p") is None


def test_identify_position_raises_on_column_drift() -> None:
    # Title matches a known position, but the header line doesn't match
    # what was verified against the real PDF -- must be a hard failure,
    # never a silent best-guess remapping.
    with pytest.raises(cp.ClayColumnDriftError):
        cp._identify_position(
            ["Quarterback Projections", "Quarterback Team Pos Rk FF Pt G P Att Comp P Yds P TD INT Carry Ru Yds Ru TD"],
            "test page",
        )


def test_identify_position_raises_when_header_line_missing() -> None:
    with pytest.raises(cp.ClayColumnDriftError):
        cp._identify_position(["Quarterback Projections"], "test page")


# --------------------------------------------------------------------------- full pipeline against the real 2-page PDF fixture


def test_extract_projections_from_real_pdf_fixture() -> None:
    statlines, report = cp.extract_projections_with_report(SAMPLE_PDF, min_total_players=0)

    assert report.counts_by_position == {"qb": 40, "wr": 40}
    assert report.total_players == 80
    assert report.skipped_count == 0

    allen = statlines["Josh Allen|BUF"]
    assert (allen.pass_att, allen.pass_yd, allen.pass_td) == (509.0, 3946.0, 26.0)

    nacua = statlines["Puka Nacua|LAR"]
    assert (nacua.rec_tgt, nacua.rec, nacua.rec_yd, nacua.rec_td) == (174.0, 123.0, 1590.0, 10.0)


def test_extract_projections_pagination_suffix_is_stripped() -> None:
    # The WR page in the fixture is titled "Wide Receiver Projections (1/5)"
    # -- confirms the " (n/m)" suffix-stripping actually runs against a real
    # PDF-extracted title, not just the hand-written unit test above.
    statlines, report = cp.extract_projections_with_report(SAMPLE_PDF, min_total_players=0)
    assert "wr" in report.counts_by_position
    assert report.counts_by_position["wr"] == 40


def test_too_few_players_raises_when_floor_not_overridden() -> None:
    # Same fixture, but WITHOUT overriding the floor: 80 real players is
    # correctly below the production floor (418 real players is normal;
    # 80 would mean a badly truncated read in production).
    with pytest.raises(cp.TooFewPlayersError):
        cp.extract_projections(SAMPLE_PDF)


def test_extract_projections_simple_api_matches_report_api() -> None:
    statlines = cp.extract_projections(SAMPLE_PDF, min_total_players=0)
    statlines2, _report = cp.extract_projections_with_report(SAMPLE_PDF, min_total_players=0)
    assert statlines.keys() == statlines2.keys()


# --------------------------------------------------------------------------- injury discount: default off, explicit, quotes Clay's own wording


def test_injury_discount_default_off() -> None:
    statlines = cp.extract_projections(SAMPLE_PDF, min_total_players=0)
    allen = statlines["Josh Allen|BUF"]
    assert allen.games == 17.0
    assert allen.pass_yd == 3946.0


def test_injury_discount_when_applied_reduces_games_and_scales_stats() -> None:
    statlines = cp.extract_projections(SAMPLE_PDF, min_total_players=0, apply_injury_discount=True)
    allen = statlines["Josh Allen|BUF"]
    # QB discount is 2 games (Clay's own wording, quoted in the module docstring).
    assert allen.games == pytest.approx(15.0)
    assert allen.pass_yd == pytest.approx(3946.0 * 15.0 / 17.0)
    assert allen.pass_td == pytest.approx(26.0 * 15.0 / 17.0)


def test_apply_injury_discount_to_stat_uses_per_position_game_counts() -> None:
    from draftroom.prep.schema import StatLine

    rb = StatLine(rush_att=300.0, rush_yd=1500.0, games=17.0)
    discounted = cp.apply_injury_discount_to_stat(rb, "rb")
    assert discounted.games == pytest.approx(14.0)  # RB discount is 3 games
    assert discounted.rush_yd == pytest.approx(1500.0 * 14.0 / 17.0)

    wr = StatLine(rec=80.0, rec_yd=1000.0, games=17.0)
    discounted_wr = cp.apply_injury_discount_to_stat(wr, "wr")
    assert discounted_wr.games == pytest.approx(15.0)  # WR discount is 2 games


def test_apply_injury_discount_handles_zero_games_without_dividing_by_zero() -> None:
    from draftroom.prep.schema import StatLine

    stat = StatLine(games=0.0)
    result = cp.apply_injury_discount_to_stat(stat, "qb")
    assert result.games == 0.0


# --------------------------------------------------------------------------- caching: data/manual/clay_parsed_<season>.json


def test_load_or_parse_writes_and_reuses_cache(tmp_path: Path) -> None:
    season = 2099  # obviously not a real season, avoids any collision with data/manual/
    statlines1, report1 = cp.load_or_parse(
        season, pdf_path=SAMPLE_PDF, manual_dir=tmp_path, min_total_players=0,
    )
    assert report1 is not None
    assert report1.total_players == 80

    cache_file = cp.cache_path_for_season(season, tmp_path)
    assert cache_file.exists()
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    assert payload["counts_by_position"] == {"qb": 40, "wr": 40}
    assert payload["apply_injury_discount"] is False
    assert len(payload["players"]) == 80

    # Second call: cache is fresh (cache mtime >= source mtime), so this
    # must NOT re-parse -- report is None is the signal for "served from cache".
    statlines2, report2 = cp.load_or_parse(
        season, pdf_path=SAMPLE_PDF, manual_dir=tmp_path, min_total_players=0,
    )
    assert report2 is None
    assert statlines2.keys() == statlines1.keys()
    assert statlines2["Josh Allen|BUF"].pass_yd == statlines1["Josh Allen|BUF"].pass_yd


def test_load_or_parse_reparses_when_discount_flag_differs(tmp_path: Path) -> None:
    season = 2098
    cp.load_or_parse(season, pdf_path=SAMPLE_PDF, manual_dir=tmp_path, min_total_players=0)

    # Same season, but now asking for the discounted version -- must not
    # silently serve the un-discounted cache just because the file is fresh.
    statlines, report = cp.load_or_parse(
        season, pdf_path=SAMPLE_PDF, manual_dir=tmp_path, min_total_players=0, apply_injury_discount=True,
    )
    assert report is not None  # re-parsed, not served from the mismatched cache
    allen = statlines["Josh Allen|BUF"]
    assert allen.games == pytest.approx(15.0)


def test_load_or_parse_force_reparses_even_when_cache_is_fresh(tmp_path: Path) -> None:
    season = 2097
    cp.load_or_parse(season, pdf_path=SAMPLE_PDF, manual_dir=tmp_path, min_total_players=0)
    statlines, report = cp.load_or_parse(
        season, pdf_path=SAMPLE_PDF, manual_dir=tmp_path, min_total_players=0, force=True,
    )
    assert report is not None


def test_load_or_parse_raises_when_no_pdf_found(tmp_path: Path) -> None:
    with pytest.raises(cp.ClayPdfError):
        cp.load_or_parse(2026, manual_dir=tmp_path)


# --------------------------------------------------------------------------- filename convention


def test_filename_regex_matches_the_real_staged_file() -> None:
    m = cp.FILENAME_RE.match("clay_projections_2026-08-17.pdf")
    assert m is not None
    assert m.group("date") == "2026-08-17"


def test_find_source_pdf_picks_newest_by_embedded_date(tmp_path: Path) -> None:
    older = tmp_path / "clay_projections_2026-08-01.pdf"
    newer = tmp_path / "clay_projections_2026-08-17.pdf"
    older.write_bytes(SAMPLE_PDF.read_bytes())
    newer.write_bytes(SAMPLE_PDF.read_bytes())
    found = cp._find_source_pdf(tmp_path)
    assert found == newer
