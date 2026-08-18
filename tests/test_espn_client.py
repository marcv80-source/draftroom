"""Tests for the ESPN projections adapter (prep/espn_client.py).

No network access anywhere in this file, per CLAUDE.md ("never re-fetch in a
test") -- everything here reads from the committed fixture at
tests/fixtures/espn/leaguedefaults_trimmed.json (a trimmed real payload, fetched
live 2026-08-17 and cross-checked against known players' actual 2026 stat
projections -- see the docstrings in prep/espn_client.py for exactly how each
stat id was verified) or monkeypatches the HTTP layer for fetch_projections().
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from draftroom.prep import espn_client as ec

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "espn" / "leaguedefaults_trimmed.json"
SEASON = 2026


def _load_fixture_players() -> list[dict]:
    with FIXTURE_PATH.open(encoding="utf-8") as f:
        return json.load(f)["players"]


# --------------------------------------------------------------------------- to_statlines: real fixture data


def test_maps_known_qb_passing_and_rushing():
    raw = _load_fixture_players()
    statlines = ec.to_statlines(raw, SEASON)
    allen = statlines["3918298"]  # Josh Allen
    assert allen.pass_att == pytest.approx(508.6697767)
    assert allen.pass_cmp == pytest.approx(340.1107698)
    assert allen.pass_yd == pytest.approx(3946.421042)
    assert allen.pass_td == pytest.approx(26.26616468)
    assert allen.pass_int == pytest.approx(11.578569)
    assert allen.pass_2pt == pytest.approx(2.098061094)
    assert allen.rush_att == pytest.approx(116.386929)
    assert allen.rush_yd == pytest.approx(579.902447)
    assert allen.rush_td == pytest.approx(12.45115907)
    assert allen.rush_2pt == pytest.approx(0.593269424)
    assert allen.fum_lost == pytest.approx(4.203455259)
    assert allen.games == pytest.approx(17.0)
    # A pure passer/rusher has no receiving line at all.
    assert allen.rec == 0.0
    assert allen.rec_tgt == 0.0
    assert allen.rec_yd == 0.0


def test_maps_known_rb_rushing_and_receiving():
    raw = _load_fixture_players()
    statlines = ec.to_statlines(raw, SEASON)
    cmc = statlines["3117251"]  # Christian McCaffrey
    assert cmc.rush_att == pytest.approx(274.1848239)
    assert cmc.rush_yd == pytest.approx(1130.922159)
    assert cmc.rush_td == pytest.approx(9.169602421)
    assert cmc.rec == pytest.approx(79.14739545)
    assert cmc.rec_tgt == pytest.approx(100.0970758)
    assert cmc.rec_yd == pytest.approx(683.9656098)
    assert cmc.rec_td == pytest.approx(4.758268122)
    assert cmc.fum_lost == pytest.approx(1.197326564)
    assert cmc.games == pytest.approx(17.0)
    # A pure rusher/receiver has no passing line at all.
    assert cmc.pass_att == 0.0
    assert cmc.pass_yd == 0.0


def test_maps_known_wr_receiving_including_targets():
    raw = _load_fixture_players()
    statlines = ec.to_statlines(raw, SEASON)
    chase = statlines["4362628"]  # Ja'Marr Chase
    assert chase.rec == pytest.approx(119.7167566)
    assert chase.rec_tgt == pytest.approx(172.4258627)
    assert chase.rec_yd == pytest.approx(1508.828876)
    assert chase.rec_td == pytest.approx(10.79976544)
    assert chase.rec_2pt == pytest.approx(0.505289115)
    # rec_tgt must be a genuinely distinct figure from rec, not a copy -- this
    # is the field neither Sleeper nor FantasyPros carries at all (CLAUDE.md /
    # prep/sleeper_client.py).
    assert chase.rec_tgt > chase.rec
    assert chase.games == pytest.approx(17.0)


def test_maps_known_te():
    raw = _load_fixture_players()
    statlines = ec.to_statlines(raw, SEASON)
    mcbride = statlines["4361307"]  # Trey McBride
    assert mcbride.rec == pytest.approx(107.8161256)
    assert mcbride.rec_tgt == pytest.approx(148.9629083)
    assert mcbride.rec_yd == pytest.approx(1023.250462)
    assert mcbride.fum_lost == pytest.approx(0.47326934)
    # TEs carry no rushing block in this payload -- must come through as zero,
    # not a shifted/misread column (same shape hazard prep/manual_csv.py
    # documents for FantasyPros' TE layout).
    assert mcbride.rush_att == 0.0
    assert mcbride.rush_yd == 0.0


# --------------------------------------------------------------------------- position filtering


def test_kicker_and_dst_are_dropped_entirely():
    raw = _load_fixture_players()
    statlines = ec.to_statlines(raw, SEASON)
    # Brandon Aubrey (K, defaultPositionId=5) and the Texans D/ST
    # (defaultPositionId=16) are both in the raw fixture but must never appear
    # in the output -- this league drafts neither (CLAUDE.md).
    assert "3953687" not in statlines  # Brandon Aubrey (K)
    assert "-16034" not in statlines  # Texans D/ST


def test_player_with_no_season_projection_block_is_skipped():
    raw = _load_fixture_players()
    statlines = ec.to_statlines(raw, SEASON)
    # Tyreek Hill's fixture entry (id 3116406) deliberately carries 2025 stats
    # but no 2026 statSourceId=1/statSplitTypeId=0 block -- must be omitted
    # entirely, never emitted as an all-zero StatLine.
    assert "3116406" not in statlines


# --------------------------------------------------------------------------- deliberately-ignored stat ids


def test_two_way_player_defensive_stats_are_ignored_without_warning(caplog):
    """Travis Hunter (WR who also plays CB) carries a defensive stat block
    (ids 93-113) alongside his receiving one. None of it has a canonical stat
    in this league (no IDP, no D/ST -- CLAUDE.md), and it must not trigger an
    'unmapped stat id' warning -- that would mean the ignore-list regressed."""
    raw = _load_fixture_players()
    with caplog.at_level(logging.WARNING, logger="draftroom.prep.espn"):
        statlines = ec.to_statlines(raw, SEASON)
    hunter = statlines["4685415"]
    assert hunter.rec == pytest.approx(41.59412963)
    assert hunter.rec_yd == pytest.approx(496.6229211)
    unmapped_warnings = [r for r in caplog.records if "unmapped" in r.message]
    assert unmapped_warnings == []


def test_return_specialist_return_stats_are_ignored_without_warning(caplog):
    """Rashid Shaheed carries kick/punt-return yardage and bucket-bonus stats
    (ids 101, 102, 114-119) alongside his receiving line -- no canonical stat
    for return production exists in this league, and it must not warn."""
    raw = _load_fixture_players()
    with caplog.at_level(logging.WARNING, logger="draftroom.prep.espn"):
        statlines = ec.to_statlines(raw, SEASON)
    shaheed = statlines["4032473"]
    assert shaheed.rec == pytest.approx(42.84192179)
    assert shaheed.rec_yd == pytest.approx(646.5425277)
    unmapped_warnings = [r for r in caplog.records if "unmapped" in r.message]
    assert unmapped_warnings == []


def test_genuinely_unknown_nonzero_stat_id_is_logged_not_silently_dropped(caplog):
    """A stat id this module has never seen before must surface as a warning
    (mirroring sleeper_client's convention) rather than vanishing -- per
    CLAUDE.md: an unmapped source field is a reported failure, never a silent
    skip."""
    synthetic = [
        {
            "player": {
                "id": 999999,
                "fullName": "Synthetic Testplayer",
                "defaultPositionId": 2,  # RB
                "proTeamId": 1,
                "stats": [
                    {
                        "seasonId": SEASON,
                        "statSourceId": 1,
                        "statSplitTypeId": 0,
                        "stats": {"24": 900.0, "99999": 42.0},
                    }
                ],
            }
        }
    ]
    with caplog.at_level(logging.WARNING, logger="draftroom.prep.espn"):
        statlines = ec.to_statlines(synthetic, SEASON)
    assert statlines["999999"].rush_yd == pytest.approx(900.0)
    unmapped_warnings = [r for r in caplog.records if "unmapped nonzero stat_id=99999" in r.message]
    assert len(unmapped_warnings) == 1


def test_unknown_zero_valued_stat_id_does_not_warn():
    """A stat id we don't recognize but whose value is 0 shouldn't spam a
    warning -- matches sleeper_client's 'if value:' gate exactly."""
    synthetic = [
        {
            "player": {
                "id": 888888,
                "fullName": "Synthetic Zero",
                "defaultPositionId": 2,
                "proTeamId": 1,
                "stats": [
                    {
                        "seasonId": SEASON,
                        "statSourceId": 1,
                        "statSplitTypeId": 0,
                        "stats": {"24": 500.0, "99999": 0.0},
                    }
                ],
            }
        }
    ]
    statlines = ec.to_statlines(synthetic, SEASON)
    assert statlines["888888"].rush_yd == pytest.approx(500.0)


# --------------------------------------------------------------------------- fetch_projections (network mocked)


def test_fetch_projections_returns_url_and_players(monkeypatch):
    raw_players = _load_fixture_players()
    raw_payload = {"players": raw_players}

    class _StubClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(ec, "make_client", lambda **kwargs: _StubClient())

    captured_urls: list[str] = []

    def _stub_get_json(client, url, **kwargs):
        captured_urls.append(url)
        return raw_payload

    monkeypatch.setattr(ec, "get_json", _stub_get_json)

    cached: list[tuple] = []
    monkeypatch.setattr(ec, "cache_raw", lambda source, payload, suffix="json": cached.append((source, payload, suffix)))

    url_used, players = ec.fetch_projections(SEASON)

    assert url_used == ec.BASE_URL.format(season=SEASON)
    assert captured_urls == [url_used]
    assert players == raw_players
    assert len(cached) == 1
    assert cached[0][0] == ec.SOURCE
    assert cached[0][1] == raw_payload


def test_fetch_projections_raises_on_unexpected_shape(monkeypatch):
    class _StubClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(ec, "make_client", lambda **kwargs: _StubClient())
    monkeypatch.setattr(ec, "get_json", lambda client, url, **kwargs: ["not", "a", "dict"])

    cached: list[tuple] = []
    monkeypatch.setattr(ec, "cache_raw", lambda *a, **k: cached.append((a, k)))

    with pytest.raises(RuntimeError, match="unexpected shape"):
        ec.fetch_projections(SEASON)

    # Never cache a payload we're about to reject as malformed.
    assert cached == []


def test_fetch_projections_sends_fantasy_filter_header(monkeypatch):
    captured_headers: list[dict] = []

    def _stub_make_client(**kwargs):
        captured_headers.append(kwargs.get("headers", {}))

        class _StubClient:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _StubClient()

    monkeypatch.setattr(ec, "make_client", _stub_make_client)
    monkeypatch.setattr(ec, "get_json", lambda client, url, **kwargs: {"players": []})
    monkeypatch.setattr(ec, "cache_raw", lambda *a, **k: None)

    ec.fetch_projections(SEASON)

    assert len(captured_headers) == 1
    filt = json.loads(captured_headers[0]["X-Fantasy-Filter"])
    assert filt["players"]["limit"] == ec.PLAYER_LIMIT
