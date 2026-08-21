"""Tests for the FantasySharks adapter (prep/fantasysharks_client.py).

NO NETWORK ANYWHERE IN THIS FILE, per CLAUDE.md ("never re-fetch in a test"). Everything reads
the committed fixture at ``tests/fixtures/fantasysharks/projections_trimmed.json`` -- a real
payload fetched live 2026-08-20 and trimmed to the top eight players per position plus two
rookies and one suffixed name per position, keeping the page's own ``<select>`` elements and one
of the mid-table repeated header blocks so every artifact path is exercised. The one test that
touches :func:`fetch_projections` monkeypatches the HTTP layer.

What these tests are actually defending, in rough order of how much a silent failure would cost:

1. **The segment is never hardcoded.** A frozen segment id serves a different season's numbers
   next year, and nothing downstream could tell.
2. **Position=4 is WR.** Getting this wrong labels 187 receivers as something else.
3. **The duplicate-header trap.** The RB table repeats ">= 50 yd"/">= 100 yd" for rushing and
   receiving; a header-keyed parse would collapse them.
4. **No fantasy points escape the parser**, and the page's "Points Awarded" scoring row never
   becomes a player.
5. **`games` is absent and measured as absent**, not quietly defaulted to something plausible.
6. **Structurally unpublished stats are declared as such**, so a later composite cannot divide
   another source's real number by a zero this source never projected.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from draftroom.prep import fantasysharks_client as fsc
from draftroom.prep.crosswalk import Crosswalk
from draftroom.prep.schema import CANONICAL_STATS, PlayerRef

FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "fantasysharks" / "projections_trimmed.json"
)
SEASON = 2026


@pytest.fixture(scope="module")
def payload() -> dict:
    with FIXTURE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def pages(payload: dict) -> dict[str, str]:
    return fsc.pages_of(payload)


@pytest.fixture(scope="module")
def rows(pages: dict[str, str]) -> list[fsc.FantasySharksRow]:
    return fsc.parse_all(pages)


@pytest.fixture(scope="module")
def by_key(rows: list[fsc.FantasySharksRow]) -> dict[str, fsc.FantasySharksRow]:
    return {r.source_key: r for r in rows}


# --------------------------------------------------------------------------- segment discovery


def test_segment_is_read_from_the_page_not_hardcoded(pages):
    segment, label = fsc.discover_segment(pages["QB"], SEASON)
    assert label == "2026 NFL Season"
    assert segment == 874  # what the page said on 2026-08-20 -- an OUTPUT, not an input
    # The number must appear nowhere in the module as a literal VALUE. Prose in a docstring or
    # a comment is documentation; an int or str constant would be the bug. Checked over the
    # parsed AST rather than the text, so the distinction is exact.
    tree = ast.parse(Path(fsc.__file__).read_text(encoding="utf-8"))
    docstrings = {
        ast.get_docstring(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    offenders = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and node.value in (874, "874")
        and node.value not in docstrings
    ]
    assert not offenders, (
        "segment id 874 appears as a literal constant in prep/fantasysharks_client.py; it must "
        "only ever be discovered at runtime from the page's own <select>"
    )


def test_segment_matches_the_season_label_exactly_not_a_substring(pages):
    """"2026 Rest of Year" and "2026 Playoffs" both mention 2026 and are both partial seasons."""
    segment, _ = fsc.discover_segment(pages["QB"], SEASON)
    parser = fsc._SegmentSelectParser()
    parser.feed(pages["QB"])
    options = dict(parser.options)
    assert options[str(segment)] == "2026 NFL Season"
    # Those decoys really are present in the fixture, so the exact match is doing work.
    assert "2026 Rest of Year" in options.values()
    assert "2026 Playoffs" in options.values()


def test_missing_season_raises_rather_than_falling_back(pages):
    with pytest.raises(fsc.SegmentNotFoundError) as exc:
        fsc.discover_segment(pages["QB"], 2031)
    assert "2031 NFL Season" in str(exc.value)
    # The error must name what WAS available, or the fix needs an investigation.
    assert "2026 NFL Season" in str(exc.value)


def test_no_segment_select_at_all_raises():
    with pytest.raises(fsc.SegmentNotFoundError):
        fsc.discover_segment("<html><body>nothing here</body></html>", SEASON)


# --------------------------------------------------------------------------- position ids


def test_position_four_is_wide_receiver():
    assert fsc.POSITION_IDS["WR"] == 4
    assert fsc.POSITION_IDS["TE"] == 5
    assert 3 not in fsc.POSITION_IDS.values()


def test_wr_page_really_contains_receivers(by_key):
    chase = by_key["15281"]
    assert (chase.name, chase.pos, chase.team) == ("Ja'Marr Chase", "WR", "CIN")


def test_position_label_check_passes_on_real_pages(pages):
    for pos in ("QB", "RB", "WR", "TE"):
        fsc._assert_position_label(pages[pos], pos)  # must not raise


def test_position_renumbering_is_caught(pages):
    doctored = pages["WR"].replace(
        '<option value="4" selected>Wide Receiver</option>',
        '<option value="4" selected>Fullback</option>',
    )
    assert doctored != pages["WR"], "fixture no longer contains the WR option text"
    with pytest.raises(fsc.ColumnLayoutError) as exc:
        fsc._assert_position_label(doctored, "WR")
    assert "Wide Receiver" in str(exc.value)


# --------------------------------------------------------------------------- canonical mapping


def test_qb_maps_passing_and_rushing(by_key):
    allen = by_key["13589"]  # Josh Allen
    assert allen.name == "Josh Allen"
    s = allen.stats
    assert s.pass_att == pytest.approx(450.1)
    assert s.pass_cmp == pytest.approx(303.8)
    assert s.pass_yd == pytest.approx(3601.0)
    assert s.pass_td == pytest.approx(31.1)
    assert s.pass_int == pytest.approx(8.5)
    assert s.rush_att == pytest.approx(106.9)
    assert s.rush_yd == pytest.approx(514.0)
    assert s.rush_td == pytest.approx(12.8)
    assert s.fum_lost == pytest.approx(3.0)
    # A QB table has no receiving columns at all.
    assert (s.rec, s.rec_tgt, s.rec_yd, s.rec_td) == (0.0, 0.0, 0.0, 0.0)


def test_rb_maps_rushing_and_receiving_including_targets(by_key):
    gibbs = by_key["16162"]  # Jahmyr Gibbs
    s = gibbs.stats
    assert s.rush_att == pytest.approx(234.3)
    assert s.rush_yd == pytest.approx(1216.0)
    assert s.rush_td == pytest.approx(14.3)
    assert s.rec_tgt == pytest.approx(83.3)  # the stat this source exists for
    assert s.rec == pytest.approx(64.6)
    assert s.rec_yd == pytest.approx(592.0)
    assert s.rec_td == pytest.approx(4.4)
    assert s.fum_lost == pytest.approx(0.9)
    assert s.pass_att == 0.0


def test_wr_maps_receiving_including_targets(by_key):
    chase = by_key["15281"]
    s = chase.stats
    assert s.rec_tgt == pytest.approx(201.0)
    assert s.rec == pytest.approx(131.8)
    assert s.rec_yd == pytest.approx(1592.0)
    assert s.rec_td == pytest.approx(12.7)
    assert s.rush_yd == pytest.approx(22.0)
    # WR/TE tables publish rushing YARDS and TDs but no attempt count.
    assert s.rush_att == 0.0
    assert "rush_att" not in fsc.PUBLISHED_STATS_BY_POS["WR"]


def test_te_maps_receiving(by_key):
    mcbride = by_key["15794"]  # Trey McBride
    assert mcbride.pos == "TE"
    assert mcbride.stats.rec_tgt == pytest.approx(172.1)
    assert mcbride.stats.rec_yd == pytest.approx(1252.0)


def test_every_row_emits_only_canonical_stats(rows):
    for r in rows:
        assert set(r.stats.as_dict()) == set(CANONICAL_STATS)


def test_targets_are_published_broadly(rows):
    """The headline reason this source was added: rec_tgt from a second source."""
    with_targets = [r for r in rows if r.stats.rec_tgt > 0]
    assert {r.pos for r in with_targets} == {"RB", "WR", "TE"}
    assert "rec_tgt" not in fsc.PUBLISHED_STATS_BY_POS["QB"]


# --------------------------------------------------------------------------- the duplicate-header trap


def test_rb_duplicate_threshold_headers_are_not_collapsed(by_key):
    """RB's table has ">= 50 yd" and ">= 100 yd" TWICE -- rushing, then receiving."""
    gibbs = by_key["16162"]
    published = [(c.stat, c.threshold, c.games) for c in gibbs.thresholds]
    assert published == [
        ("rush_yd", 50.0, pytest.approx(13.4)),
        ("rush_yd", 100.0, pytest.approx(2.9)),
        ("rec_yd", 50.0, pytest.approx(2.4)),
        ("rec_yd", 100.0, pytest.approx(1.2)),
    ]
    # The whole point: rushing and receiving at the same threshold are DIFFERENT numbers, so a
    # header-keyed parse would have silently produced one of them twice.
    proj = fsc.ThresholdProjection(
        source_key=gibbs.source_key, name=gibbs.name, pos=gibbs.pos, team=gibbs.team,
        counts=gibbs.thresholds,
    )
    assert proj.get("rush_yd", 100.0) != proj.get("rec_yd", 100.0)


def test_rb_layout_really_repeats_the_header_text():
    headers = [spec.header for spec in fsc.POSITION_LAYOUTS["RB"]]
    assert headers.count(">= 50 yd") == 2
    assert headers.count(">= 100 yd") == 2


def test_header_drift_raises(pages):
    doctored = pages["WR"].replace(">= 150 yd</a>", ">= 175 yd</a>")
    assert doctored != pages["WR"]
    with pytest.raises(fsc.ColumnLayoutError) as exc:
        fsc.parse_page(doctored, "WR")
    assert "drifted" in str(exc.value)


def test_repeated_mid_table_header_is_skipped_not_ingested(pages, rows):
    """The fixture deliberately keeps one of the page's repeated header blocks."""
    header_rows = len(re.findall(r"<th>#</th>", pages["WR"]))
    assert header_rows >= 2, "fixture lost the repeated header block this test exists for"
    wr = [r for r in rows if r.pos == "WR"]
    assert all(r.name not in ("#", "Player", "Points Awarded") for r in wr)
    assert all(r.stats.rec_yd >= 0 for r in wr)


def test_table_with_rows_but_no_header_refuses_to_parse():
    html = (
        '<table id="toolData"><tr><td>1</td><td>Nobody, Mr</td><td>BUF</td>'
        + "".join("<td>0</td>" for _ in range(21))
        + "</tr></table>"
    )
    with pytest.raises(fsc.ColumnLayoutError) as exc:
        fsc.parse_page(html, "QB")
    assert "no header row" in str(exc.value)


def test_missing_table_raises():
    with pytest.raises(fsc.ColumnLayoutError):
        fsc.parse_page("<html><body>no table</body></html>", "QB")


# --------------------------------------------------------------------------- fantasy points never leak


def test_points_awarded_row_never_becomes_a_player(pages, rows):
    assert "Points Awarded" in pages["QB"], "fixture lost the scoring row"
    assert all(r.name != "Points Awarded" for r in rows)
    assert all(r.rank > 0 for r in rows)


def test_no_fantasy_points_anywhere_in_the_output(pages, by_key):
    """The `Pts` value the page publishes must appear nowhere in the parsed output."""
    # Read the served Pts cell for the QB table's first row straight out of the fixture, so the
    # test does not depend on a number typed in here.
    parser = fsc._TableRowParser()
    parser.feed(pages["QB"])
    first_data = next(r for r in parser.rows if r.cells and r.cells[0] == "1")
    published_pts = float(first_data.cells[-1])
    assert published_pts > 300  # sanity: this really is a season points total

    allen = by_key["13589"]
    assert "Pts" not in allen.extras
    assert published_pts not in set(allen.stats.as_dict().values())
    assert published_pts not in set(allen.extras.values())
    assert all(c.games != published_pts for c in allen.thresholds)


def test_no_extras_key_is_a_points_column(rows):
    for r in rows:
        assert "Pts" not in r.extras


def test_pts_column_is_marked_discard_on_every_position():
    for pos, layout in fsc.POSITION_LAYOUTS.items():
        pts = [spec for spec in layout if spec.header == "Pts"]
        assert len(pts) == 1, pos
        assert pts[0].discard is True
        assert pts[0].canonical is None


# --------------------------------------------------------------------------- names, rookies, teams


def test_last_comma_first_names_are_reversed(by_key):
    assert by_key["13589"].name == "Josh Allen"
    assert by_key["15287"].name == "Amon-Ra St. Brown"  # a period and a hyphen in one name


def test_generational_suffix_stays_on_the_last_name(by_key):
    assert by_key["16583"].name == "Michael Penix Jr."
    assert by_key["15711"].name == "Kenneth Walker III"
    assert by_key["16618"].name == "Brian Thomas Jr."


def test_rookie_sup_tag_is_a_flag_not_part_of_the_name(by_key):
    mendoza = by_key["17462"]
    assert mendoza.rookie is True
    assert mendoza.name == "Fernando Mendoza"  # NOT "FernandoR Mendoza"
    assert not mendoza.name.endswith("R")
    assert by_key["13589"].rookie is False


def test_fantasysharks_team_codes_are_mapped_to_the_sleeper_spine(by_key):
    assert (by_key["16641"].fs_team, by_key["16641"].team) == ("LVR", "LV")  # Brock Bowers
    assert (by_key["16618"].fs_team, by_key["16618"].team) == ("JAC", "JAX")
    assert (by_key["13116"].fs_team, by_key["13116"].team) == ("KCC", "KC")
    assert (by_key["13130"].fs_team, by_key["13130"].team) == ("SFO", "SF")


def test_team_map_covers_all_32_and_only_renames_nine():
    assert len(fsc.FS_TEAM_MAP) == 32
    renamed = {k: v for k, v in fsc.FS_TEAM_MAP.items() if k != v}
    assert renamed == {
        "GBP": "GB", "JAC": "JAX", "KCC": "KC", "LVR": "LV", "NEP": "NE",
        "NOS": "NO", "SFO": "SF", "TBB": "TB",
    }


def test_unknown_team_code_raises_rather_than_blanking_the_team(pages):
    doctored = pages["QB"].replace(">BUF<", ">ZZZ<")
    assert doctored != pages["QB"]
    with pytest.raises(fsc.ColumnLayoutError) as exc:
        fsc.parse_page(doctored, "QB")
    assert "ZZZ" in str(exc.value)


# --------------------------------------------------------------------------- games (load-bearing)


def test_no_games_column_exists_and_that_is_measured(pages, rows):
    report = fsc.games_report(pages, rows)
    assert report["games_columns"] == []
    assert report["distinct_values"] == 0
    assert report["values"] == []
    assert report["players_parsed"] == len(rows)
    for pos, info in report["positions"].items():
        assert info["header"], pos
        assert info["games_headers"] == [], pos


def test_games_is_zero_meaning_unknown_on_every_row(rows):
    assert all(r.stats.games == 0.0 for r in rows)
    for published in fsc.PUBLISHED_STATS_BY_POS.values():
        assert "games" not in published


def test_games_report_would_notice_a_games_column_if_one_appeared(pages):
    """The measurement has to be able to change its answer, or it is not a measurement."""
    doctored = {"WR": pages["WR"].replace("<th>#</th>", "<th>#</th><th>Games</th>", 1)}
    report = fsc.games_report(doctored, rows=[])
    assert report["games_columns"] == [("WR", "Games")]


# --------------------------------------------------------------------------- structural absences


def test_two_point_conversions_are_structurally_unpublished():
    for pos, published in fsc.PUBLISHED_STATS_BY_POS.items():
        for stat in ("pass_2pt", "rush_2pt", "rec_2pt"):
            assert stat not in published, (pos, stat)


def test_published_stats_by_position_is_position_specific():
    assert "rush_att" in fsc.PUBLISHED_STATS_BY_POS["QB"]
    assert "rush_att" in fsc.PUBLISHED_STATS_BY_POS["RB"]
    assert "rush_att" not in fsc.PUBLISHED_STATS_BY_POS["WR"]
    assert "rush_att" not in fsc.PUBLISHED_STATS_BY_POS["TE"]
    assert "pass_yd" in fsc.PUBLISHED_STATS_BY_POS["QB"]
    assert "pass_yd" not in fsc.PUBLISHED_STATS_BY_POS["WR"]


def test_published_stats_match_what_the_parser_actually_fills(rows):
    """Anything a row reports as nonzero must be declared published at that position."""
    for r in rows:
        declared = fsc.PUBLISHED_STATS_BY_POS[r.pos]
        for stat, value in r.stats.as_dict().items():
            if value:
                assert stat in declared, (r.name, r.pos, stat, value)


def test_every_column_is_either_mapped_or_has_a_written_reason():
    for pos, layout in fsc.POSITION_LAYOUTS.items():
        for idx, spec in enumerate(layout):
            mapped = spec.canonical is not None or spec.threshold is not None
            assert mapped or spec.note, (pos, idx, spec.header)


# --------------------------------------------------------------------------- bonus threshold coverage


def test_threshold_coverage_against_this_leagues_bonus_schedule():
    """Receiving covers all three league tiers (for WR/TE); passing and rushing only the +3."""
    schedule = {
        "pass_yd": [{"threshold": 300, "points": 3}, {"threshold": 400, "points": 1},
                    {"threshold": 500, "points": 1}],
        "rush_yd": [{"threshold": 100, "points": 3}, {"threshold": 150, "points": 1},
                    {"threshold": 200, "points": 1}],
        "rec_yd": [{"threshold": 100, "points": 3}, {"threshold": 150, "points": 1},
                   {"threshold": 200, "points": 1}],
    }
    cov = fsc.bonus_tier_coverage(schedule)

    assert cov["pass_yd"]["covered"] == {300.0: ["QB"]}
    assert cov["pass_yd"]["missing"] == [400.0, 500.0]

    assert cov["rush_yd"]["covered"] == {100.0: ["RB"]}
    assert cov["rush_yd"]["missing"] == [150.0, 200.0]

    # Receiving is the fully covered one -- and note RB's receiving table stops at 100, so the
    # 150/200 tiers are WR/TE only. That distinction is exactly what the report must not blur.
    assert cov["rec_yd"]["missing"] == []
    assert cov["rec_yd"]["covered"][100.0] == ["RB", "TE", "WR"]
    assert cov["rec_yd"]["covered"][150.0] == ["TE", "WR"]
    assert cov["rec_yd"]["covered"][200.0] == ["TE", "WR"]

    # Extra thresholds the league does not pay, reported rather than dropped.
    assert set(cov["pass_yd"]["extra_thresholds"]) == {250.0, 350.0}
    assert set(cov["rush_yd"]["extra_thresholds"]) == {50.0}
    assert set(cov["rec_yd"]["extra_thresholds"]) == {50.0}


def test_threshold_lookup_returns_none_for_an_unpublished_tier(by_key):
    gibbs = by_key["16162"]
    proj = fsc.to_threshold_projections([gibbs])[gibbs.source_key]
    assert proj.get("rush_yd", 100.0) == pytest.approx(2.9)
    # FantasySharks publishes no rushing 150/200 column. None, never 0.0: a zero would assert
    # the player never clears 150 rushing yards, which this source never projected.
    assert proj.get("rush_yd", 150.0) is None
    assert proj.get("rush_yd", 200.0) is None
    assert proj.get("pass_yd", 300.0) is None  # an RB row has no passing thresholds


def test_thresholds_are_not_in_the_statline(by_key):
    """Threshold counts are not canonical component stats and must stay out of StatLine.

    Structural, not value-based: a value check would pass or fail by coincidence (a fumble
    projection of 0.4 and a 200-yard-game count of 0.4 are the same float and unrelated facts).
    """
    chase = by_key["15281"]
    assert chase.thresholds
    # No threshold count is reachable through the canonical vocabulary at all.
    for count in chase.thresholds:
        assert f"{count.stat}_ge_{count.threshold:g}" not in CANONICAL_STATS
        assert not hasattr(chase.stats, "thresholds")
    # And the yardage stat a threshold is defined ON is a season total, not a game count.
    assert chase.stats.rec_yd > 1000
    assert all(c.games < 20 for c in chase.thresholds)


# --------------------------------------------------------------------------- adapter outputs


def test_statlines_refs_and_thresholds_are_keyed_identically(rows):
    statlines = fsc.to_statlines(rows)
    refs = fsc.to_player_refs(rows)
    thresholds = fsc.to_threshold_projections(rows)
    assert set(statlines) == set(refs) == set(thresholds)
    assert len(statlines) == len(rows)


def test_player_refs_carry_the_source_name(rows):
    refs = fsc.to_player_refs(rows)
    ref = refs["15281"]
    assert isinstance(ref, PlayerRef)
    assert ref.source == fsc.SOURCE == "fantasysharks"
    assert ref.source_id == "15281"


def test_source_key_is_the_fantasysharks_player_id(by_key):
    assert by_key["15281"].source_key == "15281"


# --------------------------------------------------------------------------- crosswalk registration


def test_crosswalk_resolves_a_fantasysharks_row_by_name_team_pos():
    spine = {
        "4046": PlayerRef(name="Ja'Marr Chase", pos="WR", team="CIN",
                          source_id="4046", source="sleeper"),
    }
    cw = Crosswalk(players=spine)
    cw._by_norm_pos = {("jamarr chase", "WR"): [spine["4046"]]}
    cw._by_pos = {"WR": [spine["4046"]]}

    entry = cw.resolve_fantasysharks_row("15281", "Ja'Marr Chase", "CIN", "WR")
    assert entry.pid == "4046"
    assert entry.resolve_method == "exact_name_team_pos"
    assert entry.source == "fantasysharks"
    assert cw.resolve("fantasysharks", "15281") == "4046"


def test_crosswalk_leaves_an_unknown_player_unresolved_rather_than_guessing():
    cw = Crosswalk(players={})
    entry = cw.resolve_fantasysharks_row("99999", "Nobody At All", "BUF", "WR")
    assert entry.pid is None
    assert entry.resolve_method == "unresolved"


# --------------------------------------------------------------------------- fetch (HTTP mocked)


def test_fetch_uses_the_discovered_segment_and_caches(monkeypatch, tmp_path, payload):
    """No network: the transport is replaced. Asserts the URL carries the DISCOVERED segment."""
    pages = fsc.pages_of(payload)
    requested: list[str] = []

    class _Resp:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_make_client(**_kwargs):
        return _Client()

    def fake_request(_client, _method, url, **_kwargs):
        requested.append(url)
        for pos, pid in fsc.POSITION_IDS.items():
            if f"Position={pid}" in url:
                return _Resp(pages[pos])
        raise AssertionError(f"unexpected url {url}")

    cached: list[tuple[str, dict]] = []
    monkeypatch.setattr(fsc, "make_client", fake_make_client)
    monkeypatch.setattr(fsc, "request_with_retry", fake_request)
    monkeypatch.setattr(fsc, "cache_raw", lambda src, p, suffix="json": cached.append((src, p)))

    result = fsc.fetch_projections(SEASON)

    assert result["segment"] == 874
    assert result["segment_label"] == "2026 NFL Season"
    assert set(result["positions"]) == set(fsc.POSITION_IDS)
    # The bootstrap request must carry NO Segment (it is what discovers the segment), and every
    # subsequent one must carry the discovered value.
    assert "Segment=" not in requested[0]
    assert all("Segment=874" in u for u in requested[1:])
    # Four positions, and the bootstrap page is reused for the first rather than re-fetched.
    assert len(requested) == 4
    assert cached and cached[0][0] == "fantasysharks"
    assert len(fsc.parse_all(fsc.pages_of(result))) == len(fsc.parse_all(pages))


def test_fetch_refuses_a_pinned_segment_from_another_season(monkeypatch, payload):
    pages = fsc.pages_of(payload)

    class _Resp:
        text = pages["QB"]

        def raise_for_status(self):
            return None

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(fsc, "make_client", lambda **_k: _Client())
    monkeypatch.setattr(fsc, "request_with_retry", lambda *a, **k: _Resp())
    monkeypatch.setattr(fsc, "cache_raw", lambda *a, **k: None)

    with pytest.raises(fsc.SegmentNotFoundError) as exc:
        fsc.fetch_projections(SEASON, segment=906)  # 906 is the 2027 season
    assert "874" in str(exc.value)


def test_unknown_position_is_rejected_before_any_request():
    with pytest.raises(ValueError):
        fsc.fetch_projections(SEASON, positions=["K"])


def test_load_cached_rejects_a_wrong_shaped_payload(monkeypatch):
    monkeypatch.setattr(fsc, "load_latest_raw", lambda _src: ["not", "a", "dict"])
    with pytest.raises(fsc.FantasySharksError):
        fsc.load_cached()
