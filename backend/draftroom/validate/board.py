"""Build a REAL (not synthetic) draft board from cached prep data, for validation tooling.

Every other tool in this repo that needs a board either uses hand-built synthetic fixtures
(tests) or derives a SYNTHETIC draft value straight from ADP (``tools/demo_recommendation.py``)
because a real projections -> valuation pipeline isn't wired end to end yet. This module closes
that gap for validation purposes only: it joins the cached Sleeper season projections onto the
cached FFC ADP board via the real crosswalk (:mod:`draftroom.prep.crosswalk`), scores each
player's projected stat line with the league's OWN scoring PLUS the validated per-game yardage
bonus model (:func:`draftroom.prep.scoring.score_statline_with_bonus`, additive on top of the
untouched ``score_statline`` dot product -- see that function's docstring; MAE 1.335, mean bias
0.254 against the 2025 backtest), and runs the result through the real replacement/EVoB
pipeline (:mod:`draftroom.valuation.replacement`, :mod:`draftroom.valuation.evob`). This is the
first (and, as of 2026-08-18, only) production path that actually calls the bonus model --
``score_statline`` itself stays a pure dot product, untouched, exactly as its docstring
requires.

Nothing here invents a number: PPG comes from Sleeper's projected stat line divided by Sleeper's
own projected games played, ``expected_games`` is ``min(Sleeper's games, the rank-conditional
availability curve)`` -- see :func:`_cap_expected_games_by_curve`; the fitted curve corrects
source optimism about durability while a source projecting FEWER games than the curve is
trusted outright -- and ADP/stdev come straight from the cached FFC payload. The only approximation is the join
itself (name/ID crosswalk) and the well-documented FFC-is-published-at-12-teams caveat
(CLAUDE.md: "One deliberate 12-team exception").

Reads only cached files under ``data/raw/`` (:func:`draftroom.prep.http.load_latest_raw`) -- no
network call, so this is safe to run offline and on draft night's own machine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping

from draftroom.config import LeagueConfig
from draftroom.draft.recommend import BoardPlayer
from draftroom.prep import espn_client, manual_csv
from draftroom.prep.crosswalk import DYNASTYPROCESS_SOURCE, Crosswalk, build_crosswalk
from draftroom.prep.ffc_client import AdpRow, parse_adp_rows
from draftroom.prep.http import load_latest_raw
from draftroom.prep.schema import StatLine
from draftroom.prep.scoring import score_statline_with_bonus
from draftroom.prep.sleeper_client import SKILL_POSITIONS, to_statlines
from draftroom.valuation.bonuses import load_bonus_schedule, load_curves
from draftroom.valuation.disagreement import (
    DISAGREEMENT_CAVEAT,
    SourceDisagreement,
    compute_disagreement,
    sigma_ppg_from_disagreement,
)
from draftroom.valuation.evob import compute_draft_values
from draftroom.valuation.replacement import PlayerSeason

__all__ = ["RealBoard", "build_real_board", "SEASON"]

log = logging.getLogger("draftroom.validate.board")

#: Hardcoded per this repo's convention (prep/fantasypros_client.py, prep/fetch_all.py do the
#: same) -- there is no shared "current season" constant elsewhere in the codebase to import.
SEASON = 2026


@dataclass(frozen=True)
class RealBoard:
    """The joined, valued board plus a record of what did NOT make it on, for transparency.

    Both ``players`` and ``seasons`` describe the same underlying pool: ``seasons`` is the raw
    valuation INPUT (ppg + expected_games, what :func:`~draftroom.valuation.evob.compute_draft_values`
    itself takes), ``players`` is the OUTPUT joined onto ADP (what the draft engine's
    :class:`~draftroom.draft.recommend.BoardPlayer` needs). Callers doing their own valuation
    sweep (e.g. the sanity-invariant gate re-deriving a baseline at a different config) want
    ``seasons``; callers driving a mock draft want ``players``.
    """

    players: tuple[BoardPlayer, ...]
    seasons: tuple[PlayerSeason, ...]
    #: FFC rows at a skill position that never got a real dv (unresolved, or resolved but with
    #: no/zero-game projection). Never silently dropped -- callers can inspect this.
    excluded: tuple[AdpRow, ...]
    cfg: LeagueConfig
    #: Cross-source (Sleeper/FantasyPros/ESPN) projection spread, keyed by player_id. Populated
    #: for whichever players resolved onto at least one of those sources -- a player absent
    #: here simply had no data to compare (never a fabricated zero). See
    #: :mod:`draftroom.valuation.disagreement` and ``disagreement_caveat`` below before reading
    #: any of these numbers as a confidence signal.
    disagreement: Mapping[str, SourceDisagreement]
    #: The mandated caveat (verbatim, see draftroom.valuation.disagreement.DISAGREEMENT_CAVEAT):
    #: attached directly to the data, not just to a docstring, so nothing that carries a
    #: RealBoard around loses it.
    disagreement_caveat: str = DISAGREEMENT_CAVEAT


def build_real_board(cfg: LeagueConfig | None = None) -> RealBoard:
    """Join cached FFC ADP onto cached Sleeper projections, score with ``cfg``, value with EVoB.

    Args:
        cfg: league config to score and value against. Defaults to
            :meth:`~draftroom.config.LeagueConfig.from_yaml` -- the real, CONFIRMED 10-team
            league (``data/league_manual.yaml``).
    """
    cfg = cfg or LeagueConfig.from_yaml()

    sleeper_raw = load_latest_raw("sleeper")
    ffc_raw = load_latest_raw("ffc")
    ffc_rows = parse_adp_rows(ffc_raw)
    try:
        dp_csv = load_latest_raw(DYNASTYPROCESS_SOURCE)
    except FileNotFoundError:
        dp_csv = None
        log.warning(
            "no cached DynastyProcess crosswalk under data/raw/dynastyprocess/; falling back "
            "to Sleeper's own cross-ID fields only for stage-1 matching"
        )

    cw = build_crosswalk(sleeper_raw, ffc_rows, dynastyprocess_csv_text=dp_csv)
    statlines = to_statlines(load_latest_raw("sleeper_projections"))

    # Bonus model (task: wire the validated additive bonus into production scoring). Best
    # effort: a missing fitted-curves cache degrades to plain score_statline (bonus_schedule/
    # bonus_curves=None makes score_statline_with_bonus byte-for-byte equal to score_statline,
    # per that function's own docstring/tests) rather than failing the whole board build.
    try:
        bonus_schedule = load_bonus_schedule()
        bonus_curves = load_curves()
    except FileNotFoundError:
        bonus_schedule = None
        bonus_curves = None
        log.warning(
            "no cached fitted bonus curves under data/bonus_curves.json; scoring without the "
            "per-game yardage bonus for this build"
        )

    # Cross-source disagreement inputs (Marc's own idea, approved 2026-08-17): ESPN and the
    # manual FantasyPros CSVs, each resolved onto the crosswalk's pid the same way Sleeper
    # already is. Both are best-effort -- a missing/stale/malformed source degrades to "this
    # source contributes nothing" rather than failing the whole board build, because the core
    # ADP x Sleeper valuation pipeline has nothing to do with whether Marc has downloaded a
    # fresh FantasyPros CSV this week.
    espn_by_pid = _resolve_espn_statlines(cw)
    fantasypros_by_pid = _resolve_fantasypros_statlines(cw)

    seasons: list[PlayerSeason] = []
    meta: dict[str, AdpRow] = {}
    excluded: list[AdpRow] = []
    disagreement: dict[str, SourceDisagreement] = {}

    for row in ffc_rows:
        pos = (row.pos or "").strip().upper()
        if pos not in SKILL_POSITIONS:
            continue  # DEF/PK: out of this league's scope entirely, not a data gap.
        key = str(row.player_id) if row.player_id is not None else f"{row.name}|{row.team}|{row.pos}"
        pid = cw.resolve("ffc", key)
        statline = statlines.get(pid) if pid is not None else None
        if pid is None or statline is None or statline.games <= 0:
            excluded.append(row)
            continue
        pid = str(pid)

        total_points = score_statline_with_bonus(
            statline.as_dict(), cfg.scoring,
            pos=pos, games=statline.games,
            bonus_schedule=bonus_schedule, bonus_curves=bonus_curves,
        )

        d = compute_disagreement(
            pid,
            {
                "sleeper": statline.as_dict(),
                "espn": (espn_by_pid[pid].as_dict() if pid in espn_by_pid else None),
                "fantasypros": (fantasypros_by_pid[pid].as_dict() if pid in fantasypros_by_pid else None),
            },
            cfg.scoring,
        )
        disagreement[pid] = d
        sigma_ppg = sigma_ppg_from_disagreement(d, statline.games)

        seasons.append(
            PlayerSeason(
                player_id=pid,
                pos=pos,
                ppg=total_points / statline.games,
                expected_games=statline.games,  # capped by the availability curve below.
                sigma_ppg=sigma_ppg,  # None unless >=2 independent sources resolved -- see disagreement.py.
                name=row.name,
            )
        )
        meta[pid] = row

    # Cap each player's expected games at the rank-conditional availability curve (Codex
    # 2026-08-18: passing Sleeper's games straight through made EXPECTED_GAMES_CURVE inert on
    # the real board -- the curve only applied when expected_games was None, which it never
    # was here). Policy: `min(source_games, curve)`. The curve is FITTED actual availability
    # by positional rank (late QBs really average ~11 of 17), so a source projecting MORE
    # games than players at that rank historically play is optimism the fit corrects; a source
    # projecting FEWER is trusted outright -- it knows something player-specific (suspension,
    # a dated return timeline) that a rank curve cannot.
    seasons = _cap_expected_games_by_curve(seasons, cfg)

    dv_map = compute_draft_values(seasons, cfg)

    players = tuple(
        BoardPlayer(
            player_id=pid,
            name=meta[pid].name,
            pos=dv.pos,
            team=meta[pid].team,
            bye=meta[pid].bye,
            adp=meta[pid].adp,
            stdev=meta[pid].std_dev,
            dv=dv.dv,
            dv_sd=dv.sigma_season,  # populated wherever cross-source disagreement gave a sigma_ppg.
        )
        for pid, dv in dv_map.items()
    )
    return RealBoard(
        players=players, seasons=tuple(seasons), excluded=tuple(excluded), cfg=cfg,
        disagreement=disagreement,
    )


def _cap_expected_games_by_curve(
    seasons: list[PlayerSeason], cfg: LeagueConfig
) -> list[PlayerSeason]:
    """``expected_games = min(source_games, curve(pos, rank-by-ppg))`` for every season.

    Rank is 1-based by projected PPG within the position -- the same ranking convention
    :func:`draftroom.valuation.replacement.resolve_players` uses for curve lookups, so this cap
    and the valuation pipeline agree on who "rank 25" is. PPG itself is untouched (it is a
    per-game rate; the cap only reduces the games VOLUME that rate is credited for).
    """
    from dataclasses import replace as _dc_replace

    from draftroom.valuation.replacement import expected_games as _curve_games

    by_pos: dict[str, list[PlayerSeason]] = {}
    for s in seasons:
        by_pos.setdefault(s.pos, []).append(s)

    capped: dict[str, PlayerSeason] = {}
    for pos, group in by_pos.items():
        for rank, s in enumerate(sorted(group, key=lambda x: -x.ppg), start=1):
            cap = _curve_games(pos, rank=rank, weeks=cfg.weeks)
            source_games = float(s.expected_games or 0.0)
            capped[s.player_id] = (
                _dc_replace(s, expected_games=min(source_games, cap))
                if source_games > cap
                else s
            )
    return [capped[s.player_id] for s in seasons]


def _resolve_espn_statlines(cw: Crosswalk) -> dict[str, StatLine]:
    """ESPN's projected statlines, keyed by the crosswalk's pid (not ESPN's own id).

    Best-effort: no cached ESPN payload is a normal, expected state (this source is optional
    enrichment, not part of the core ADP x Sleeper valuation), so it degrades to "no ESPN data"
    with a warning rather than failing the whole board build.
    """
    try:
        raw = load_latest_raw("espn")
    except FileNotFoundError:
        log.warning("no cached ESPN payload under data/raw/espn/; cross-source disagreement "
                    "will run on Sleeper + FantasyPros only wherever ESPN is missing")
        return {}

    players_raw = raw.get("players", []) if isinstance(raw, dict) else []
    refs = espn_client.to_player_refs(players_raw, SEASON)
    espn_statlines = espn_client.to_statlines(players_raw, SEASON)

    out: dict[str, StatLine] = {}
    for espn_id, ref in refs.items():
        entry = cw.resolve_espn_row(espn_id, ref.name, ref.team, ref.pos, espn_id=espn_id)
        statline = espn_statlines.get(espn_id)
        if entry.pid is not None and statline is not None:
            out[entry.pid] = statline
    return out


def _resolve_fantasypros_statlines(cw: Crosswalk) -> dict[str, StatLine]:
    """FantasyPros' (manual CSV) projected statlines, keyed by the crosswalk's pid.

    Best-effort in the same spirit as `_resolve_espn_statlines`: a missing, stale, or malformed
    manual CSV degrades to "no FantasyPros data" for the disagreement measure rather than
    failing the whole board build -- the core valuation pipeline (ADP x Sleeper) has nothing to
    do with whether Marc has downloaded this week's FantasyPros export.
    """
    try:
        results = manual_csv.load_all_positions(season=SEASON)
    except manual_csv.ManualCsvError as exc:
        log.warning(
            "manual FantasyPros CSV(s) unusable for the disagreement measure (%s: %s); "
            "cross-source disagreement will run on Sleeper + ESPN only wherever FantasyPros "
            "is missing", type(exc).__name__, exc,
        )
        return {}

    out: dict[str, StatLine] = {}
    for pos, result in results.items():
        pos_u = pos.upper()
        for name_team_key, statline in result.statlines.items():
            name, _, team = name_team_key.partition("|")
            entry = cw.resolve_fantasypros_row(name_team_key, name, team, pos_u)
            if entry.pid is not None:
                out[entry.pid] = statline
    return out
