"""QB-timing strategy tournament: does deferring QB cause the engine's 0/10 mock-draft failure,
or is it a genuine sequencing disadvantage that timing alone can't fix?

BACKGROUND. `tools/mock_draft_sim.py` (read-only here) already shows the current engine takes
RBs, defers QB, and loses from all ten draft slots -- it ends up forced into QBs worth -121 and
-147 `dv` at picks 140-141 because nine other 2-QB-needing opponents have already cleared the
position out. Static per-pick VORP can't diagnose *why*: is it that specific end-of-draft
catastrophe, or would deferring QB lose even with a clean landing?

APPROACH. Fix eight mechanical QB-timing rules -- seven vary on ONE axis (the QB deadline round,
or for `qb_never_below_line` a scarcity trigger; every other pick is "best available `dv`", full
stop), plus one deliberately compound rule, `qb_one_elite_one_cheap`, added after a parallel
workstream's real-preseason-rank finding that QB surplus is real only at rank 1-3 and RB holds
value through rank 12 -- and run each from all ten draft slots against the project's own
ADP-following bot model (`draftroom.draft.opponents`, exactly as `mock_draft_sim` uses it). Score every final roster on the sum of `dv` across its OPTIMAL starting lineup, reusing
`mock_draft_sim.starting_lineup_value` verbatim -- see that module's docstring for why
greedy-per-position-then-flex is exactly optimal here, not a heuristic.

THE CIRCULARITY TRAP (read `mock_draft_sim.py` and CLAUDE.md before trusting a projection-only
run). Scoring rosters with the same 2026 projections used to draft them is partly self-confirming,
and this repo's own QB1-QB22 spread (5.6 PPG) versus 10.6 PPG in seven years of actuals is reason
to distrust those projections specifically for QB separation. This module supports two modes:

  * `--mode historical` (preferred): drafts on a REAL past season's PRESEASON ECR (FantasyPros,
    via `nflreadpy.load_ff_rankings(type="all")`) and scores rosters on that season's REAL
    results (`nflreadpy.load_player_stats`), scored through this repo's own
    `prep.scoring.score_statline` + `valuation.bonuses.actual_bonus` (ground-truth per-game
    bonus, no model). Draft-time value and scoring-time value are two SEPARATE numbers computed
    from two separate time periods -- see `Board`/`build_historical_board` -- so there is no
    look-ahead: a strategy never gets to "know" who was about to break out.
  * `--mode projection` (fallback): drafts and scores on the SAME 2026 real board
    (`draftroom.validate.board.build_real_board`) the live engine uses today. Circular by
    construction; the report must say so plainly, never present it as settled.

TWO preseason ECR products are used in historical mode, deliberately NOT the same one -- see
`build_historical_board`'s docstring for why a single one (tried first, and reverted) made every
strategy converge to the same behavior and tested nothing: bots pace off FantasyPros' PPR
Superflex ECR (already prices 2-QB scarcity correctly, mirroring CLAUDE.md's own live choice of
FFC's 2QB ADP endpoint for opponents), while strategies draft on standard 1-QB redraft ECR
(systematically underrates QB, mirroring the SAME kind of mispricing in the live engine's own
Sleeper-projection-derived `dv`).

Network use here is READ-ONLY against nflreadpy's own (non-repo) cache -- never
`prep/fetch_all.py`, never a write into `data/raw/` (CLAUDE.md: that breaks other tests). This
machine needs the corp CA bundle (`prep/http.py`'s own trick) or every request 403s/SSL-fails;
`_ensure_ca_bundle_env` sets the same two env vars `requests` reads, if unset.

Run (everything in ONE process -- no shelling out per draft, this machine's process-launch tax
is real):
    C:\\dev\\draftroom\\.venv\\Scripts\\python.exe tools\\strategy_tournament.py
        --mode historical --season 2025 --scrape-date 2025-08-29
        --baseline-reps 1500 --strategy-reps 250 --seed 2026
        --out data\\strategy_tournament_results.json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import numpy as np  # noqa: E402

from draftroom.config import LeagueConfig  # noqa: E402
from draftroom.draft import opponents as opp  # noqa: E402
from draftroom.draft import snake  # noqa: E402
from draftroom.draft.recommend import BoardPlayer  # noqa: E402
from draftroom.draft.survival import PositionalRun  # noqa: E402
from draftroom.prep.scoring import score_statline  # noqa: E402
from draftroom.valuation.bonuses import actual_bonus, load_bonus_schedule  # noqa: E402
from draftroom.valuation.evob import compute_draft_values  # noqa: E402
from draftroom.valuation.replacement import PlayerSeason  # noqa: E402

from mock_draft_sim import (  # noqa: E402
    _bot_pick,
    starting_lineup_value,
    waiver_fill_for_draft,
)

CORP_CA_BUNDLE = Path(r"C:\Users\mvaldes\.claude\corp-ca-bundle.pem")
DEFAULT_SEASON = 2025
DEFAULT_SCRAPE_DATE = "2025-08-29"  # last FantasyPros superflex scrape before 2025 Week 1

SKILL_POS: tuple[str, ...] = ("QB", "RB", "WR", "TE")
FUM_COLS: tuple[str, ...] = ("rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost")


# ============================================================================ QB-timing strategies


@dataclass(frozen=True)
class Strategy:
    """One mechanical QB-timing rule. `qb_deadline_round` / `reactive` / `elite_one_cheap` are
    mutually exclusive "kinds" -- every strategy shares the identical "best available dv"
    fallback, so QB timing is provably the only dimension that differs between rows of the
    tournament table (`elite_one_cheap` is the one exception, and it says so explicitly below).

    `qb_deadline_round`: from this round of MY OWN picks onward, if I don't yet have my full
    complement of starting QBs, I take the best available QB instead of the best available
    ANYTHING. Before it, QB competes for my pick on `dv` exactly like every other position.
    `None` means "never force" (`best_value`, the control).

    `reactive=True` (`qb_never_below_line`) replaces the fixed round with a scarcity trigger:
    force a QB the moment the number of startable QBs left in the pool would not survive the
    picks that elapse before my next turn, given how many QB slots the WHOLE LEAGUE (not just
    me) still has unfilled. This is the hypothesis under test -- it reacts to the supply/demand
    line, never to a value premium.

    `elite_one_cheap=True` (`qb_one_elite_one_cheap`) is a deliberately COMPOUND rule, added on
    top of the single-axis set at the coordinator's direction after a parallel workstream's
    finding: measured on real 2021-2025 preseason positional rank, QB surplus-over-replacement is
    real and low-variance ONLY at rank 1-3 (7.59/6.29 PPG) and collapses to ~0 by rank 13, while
    RB holds value through rank 12 -- i.e. the money is in ONE elite QB plus premium mid-round RB,
    not in QB volume. It has two rules, not one: (a) force a QB by `ELITE_QB_DEADLINE_ROUND`
    ONLY if a top-`ELITE_QB_RANK_CUTOFF` QB is still on the board (never reaches for a mediocre
    one just to make the deadline); (b) once that first QB is rostered, the SECOND QB slot uses
    the exact same scarcity trigger as `qb_never_below_line` -- filled "as cheaply as safety
    allows," never for a value premium.
    """

    name: str
    qb_deadline_round: int | None = None
    reactive: bool = False
    elite_one_cheap: bool = False


#: `qb_one_elite_one_cheap`-only constants. Elite QBs (rank 1-3) are ranked #1-3 OVERALL (not
#: merely among QBs) in the bot model's own superflex signal (2025-08-29: Josh Allen ECR 1.48,
#: Lamar Jackson 1.73, Jayden Daniels 3.36) -- bots sweep them within the first few picks of the
#: WHOLE draft, not merely by round 4. Verified empirically: a first attempt at
#: `ELITE_QB_DEADLINE_ROUND=4` (reusing `qb_elite`'s deadline) never once found an elite QB still
#: on the board in 80/80 test drafts -- the deadline check must engage from a strategy's very
#: FIRST pick to have any chance at all, since the tier is usually gone well before round 4.
ELITE_QB_RANK_CUTOFF = 3
ELITE_QB_DEADLINE_ROUND = 1

STRATEGIES: tuple[Strategy, ...] = (
    Strategy("qb_elite", 4),
    Strategy("qb_early", 6),
    Strategy("qb_balanced", 8),
    Strategy("qb_late", 11),
    Strategy("qb_punt", 14),
    Strategy("qb_never_below_line", reactive=True),
    Strategy("qb_one_elite_one_cheap", elite_one_cheap=True),
    Strategy("best_value"),
)


def _best_value(available: Sequence[BoardPlayer]) -> str:
    return max(available, key=lambda p: p.dv).player_id


def _best_qb_or_value(available: Sequence[BoardPlayer]) -> str:
    qbs = [p for p in available if p.pos == "QB"]
    pool = qbs if qbs else available
    return max(pool, key=lambda p: p.dv).player_id


def qb_startable_rank(cfg: LeagueConfig) -> int:
    """How many QBs (best-to-worst) this league's man-games demand actually needs to call
    'startable', from PRESEASON durability priors alone -- no real outcome, no ADP, nothing but
    `teams`, `starters['QB']`, `weeks`, and the repo's rank-conditional availability curve
    (`draftroom.valuation.replacement.EXPECTED_GAMES_CURVE`, which replaced the old flat
    per-position prior on 2026-08-18). QB has no flex eligibility in this league, so its demand
    needs no flex-allocation machinery: `demand = teams * starters['QB'] * weeks` man-games,
    covered by accumulating each rank's own expected games (a rank-3 QB supplies ~16.6 of them,
    a rank-25 QB only ~12.9) until demand is met. At this league's real settings (10 teams,
    2 QB, 17 weeks) that lands at 22 -- matching CLAUDE.md's own "replacement level QB is QB22"
    line, which is exactly the cross-check this number should reproduce.

    This is the fix for a bug caught in dry-run testing: an earlier version of
    `qb_never_below_line` counted ANY remaining QB (including token QB4s/QB5s nobody would ever
    start) as "supply," so the scarcity trigger never fired in 80/80 test drafts even though the
    team finished with ZERO QBs -- the pool always looked "safe" because deep, unstartable
    arms were still sitting there. Capping "remaining startable QBs" at real man-games demand is
    what makes the trigger mean what its name says.
    """
    # MOVED 2026-08-18 to draftroom.draft.scarcity.startable_rank_cutoff so the LIVE engine
    # (draftroom.draft.recommend) uses the identical cutoff this tournament validated -- a
    # Codex review caught the two sides defining "startable" differently. This wrapper stays
    # so every existing call site and the results JSON's field name keep working.
    from draftroom.draft.scarcity import startable_rank_cutoff

    return startable_rank_cutoff(cfg, "QB")


def build_qb_rank(draft_players: Sequence[BoardPlayer]) -> dict[str, int]:
    """1-based rank AMONG QBs ONLY, by draft-time `dv` descending (best QB = 1). This is what
    `qb_never_below_line` checks against `qb_startable_rank(cfg)` -- "is this QB good enough to
    count as supply", not merely "does a QB still exist somewhere in the pool"."""
    qbs = sorted((p for p in draft_players if p.pos == "QB"), key=lambda p: -p.dv)
    return {p.player_id: i + 1 for i, p in enumerate(qbs)}


def _reactive_trigger_fires(
    available: Sequence[BoardPlayer],
    have_all: dict[int, dict[str, int]],
    my_slot: int,
    pick_no: int,
    cfg: LeagueConfig,
    qb_need: int,
    qb_rank: dict[str, int],
    qb_startable_rank_cutoff: int,
) -> bool:
    """Shared scarcity math for `qb_never_below_line` AND `qb_one_elite_one_cheap`'s 2nd-QB
    rule, delegated to draftroom.draft.scarcity (the SAME code the live engine now runs --
    Codex 2026-08-18 caught the live and tournament triggers diverging, and separately that
    the old `startable - leaguewide_unfilled <= gap` form over-fired by assuming every
    intervening pick consumes supply while demand stays constant; see that module's docstring)."""
    from draftroom.draft import scarcity

    remaining_startable_qb = sum(
        1 for p in available
        if p.pos == "QB" and qb_rank.get(p.player_id, 10**9) <= qb_startable_rank_cutoff
    )
    my_unfilled = max(0, qb_need - have_all[my_slot].get("QB", 0))
    nxt = snake.next_pick_for(cfg.teams, my_slot, cfg.roster_size, pick_no)
    gap_slots = (
        [snake.slot_on_clock(cfg.teams, pk) for pk in range(pick_no + 1, nxt)]
        if nxt is not None
        else []
    )
    unfilled_by_slot = {
        t: max(0, qb_need - have_all[t].get("QB", 0))
        for t in range(1, cfg.teams + 1)
        if t != my_slot
    }
    consumption = scarcity.opponent_consumption_bound(gap_slots, unfilled_by_slot)
    return scarcity.scarcity_trigger_fires(
        startable_remaining=remaining_startable_qb,
        opponent_consumption_bound=consumption,
        my_unfilled=my_unfilled,
    )


def strategy_pick(
    strategy: Strategy,
    available: Sequence[BoardPlayer],
    have_all: dict[int, dict[str, int]],
    my_slot: int,
    pick_no: int,
    cfg: LeagueConfig,
    *,
    qb_rank: dict[str, int],
    qb_startable_rank_cutoff: int,
) -> str:
    """The ONE function every strategy shares. See `Strategy` docstring for each rule's axis of
    variation. `have_all` is EVERY team's roster-position counts (not just mine) -- legitimate
    in a real in-person draft, where every prior pick is visible on the board, and required by
    the leaguewide scarcity count both `qb_never_below_line` and `qb_one_elite_one_cheap` use."""
    my_have = have_all[my_slot]
    qb_need = int(cfg.starters.get("QB", 0))
    my_qb = my_have.get("QB", 0)

    if my_qb >= qb_need:
        return _best_value(available)  # QB need already met -- no positional logic left at all

    if strategy.elite_one_cheap:
        if my_qb == 0:
            rnd = snake.round_of(cfg.teams, pick_no)
            if rnd >= ELITE_QB_DEADLINE_ROUND:
                elites = [
                    p for p in available
                    if p.pos == "QB" and qb_rank.get(p.player_id, 10**9) <= ELITE_QB_RANK_CUTOFF
                ]
                if elites:
                    return max(elites, key=lambda p: p.dv).player_id
                # elite tier already gone -- do NOT reach for a mediocre QB, fall through
            return _best_value(available)
        # my_qb == 1 (or, if qb_need > 2, still short of it): fill the remaining slot(s) via the
        # exact same "as cheaply as safety allows" trigger as qb_never_below_line.
        if _reactive_trigger_fires(available, have_all, my_slot, pick_no, cfg, qb_need, qb_rank, qb_startable_rank_cutoff):
            return _best_qb_or_value(available)
        return _best_value(available)

    if strategy.reactive:
        if _reactive_trigger_fires(available, have_all, my_slot, pick_no, cfg, qb_need, qb_rank, qb_startable_rank_cutoff):
            return _best_qb_or_value(available)
        return _best_value(available)

    if strategy.qb_deadline_round is not None:
        rnd = snake.round_of(cfg.teams, pick_no)
        if rnd >= strategy.qb_deadline_round:
            return _best_qb_or_value(available)

    return _best_value(available)


# ============================================================================ board construction


def _ensure_ca_bundle_env() -> None:
    """Mirror `prep.http._resolve_default_verify`'s trick for the `requests`-based
    `nflreadpy` downloader, which does not read `httpx`'s verify kwarg at all. Harmless where
    the corp proxy doesn't exist -- `os.environ.setdefault` never overrides an already-set var."""
    import os

    if CORP_CA_BUNDLE.is_file():
        os.environ.setdefault("REQUESTS_CA_BUNDLE", str(CORP_CA_BUNDLE))
        os.environ.setdefault("SSL_CERT_FILE", str(CORP_CA_BUNDLE))


def synthetic_draft_value(ecr_rank: float, sd: float | None) -> tuple[float, float]:
    """SAME formula as `tools/demo_recommendation.py`'s `synthetic_draft_value`: earlier rank ->
    higher value, nothing more sophisticated than that. This is what the strategies draft ON --
    it is built ONLY from preseason information (ECR rank + rank spread), so a strategy never
    gets a peek at how the season actually turned out. Kept numerically identical to the
    established precedent elsewhere in this repo rather than inventing a new transform."""
    dv = max(0.5, 200.0 - ecr_rank)
    dv_sd = 0.4 * (sd if sd is not None else 3.0) * (dv / 20.0 + 1.0)
    return dv, dv_sd


def _canon_row(r: dict) -> dict:
    return {
        "pass_att": r.get("attempts") or 0,
        "pass_cmp": r.get("completions") or 0,
        "pass_yd": r.get("passing_yards") or 0,
        "pass_td": r.get("passing_tds") or 0,
        "pass_int": r.get("passing_interceptions") or 0,
        "pass_2pt": r.get("passing_2pt_conversions") or 0,
        "rush_att": r.get("carries") or 0,
        "rush_yd": r.get("rushing_yards") or 0,
        "rush_td": r.get("rushing_tds") or 0,
        "rush_2pt": r.get("rushing_2pt_conversions") or 0,
        "rec": r.get("receptions") or 0,
        "rec_tgt": r.get("targets") or 0,
        "rec_yd": r.get("receiving_yards") or 0,
        "rec_td": r.get("receiving_tds") or 0,
        "rec_2pt": r.get("receiving_2pt_conversions") or 0,
        "fum_lost": sum(r.get(c) or 0 for c in FUM_COLS),
    }


@dataclass(frozen=True)
class Board:
    """What `run_one_draft` needs, for either mode. `draft_players` carries the DRAFT-TIME value
    (`dv`) every STRATEGY picks on; `scoring_dv` carries the (possibly different) value the
    FINAL roster is graded on. In `--mode projection` these are the same numbers by construction
    (stated plainly as the circularity); in `--mode historical` they come from two different time
    periods (preseason vs. real season result) and are never the same number for the same player.

    `bot_resolved`, when set, OVERRIDES what the bot opponent model reads (adp, stdev, pos) --
    see `build_historical_board` for why bots and strategies deliberately read two different
    preseason signals in historical mode. `None` (the projection-mode case) means "derive it from
    `draft_players.adp/.stdev/.pos`", i.e. bots and the value ranking share one board, exactly as
    the live 2026 tool does today (FFC ADP drives bots, Sleeper-projection-derived `dv` drives
    ranking -- already two different numbers on the SAME `BoardPlayer`, nothing new needed here)."""

    draft_players: tuple[BoardPlayer, ...]
    scoring_dv: dict[str, float]
    cfg: LeagueConfig
    label: str
    diagnostics: dict
    bot_resolved: dict[str, tuple[float, float | None, str]] | None = None


def build_projection_board(cfg: LeagueConfig | None = None) -> Board:
    """The circular fallback: today's real 2026 board, used to both draft AND score."""
    from draftroom.validate.board import build_real_board

    real = build_real_board(cfg)
    scoring_dv = {p.player_id: p.dv for p in real.players}
    return Board(
        draft_players=real.players,
        scoring_dv=scoring_dv,
        cfg=real.cfg,
        label=f"PROJECTION (circular): 2026 real board, {len(real.players)} players",
        diagnostics={"mode": "projection", "n_players": len(real.players)},
    )


def build_historical_board(
    season: int = DEFAULT_SEASON,
    scrape_date: str = DEFAULT_SCRAPE_DATE,
    cfg: LeagueConfig | None = None,
) -> Board:
    """True backtest board: draft on `season`'s real preseason ECR, score on `season`'s real
    results. See module docstring for the full method and CA-bundle note.

    TWO SEPARATE preseason signals are pulled on purpose, because a single one cannot reproduce
    the failure this tournament exists to decompose:

    * `rsf` (FantasyPros "PPR Superflex" ECR) drives the BOT opponent model (`resolved`). A
      superflex market already prices QB scarcity correctly (2025-08-29: Josh Allen ECR #1.5,
      Lamar #1.7) -- this is this repo's own convention for "how a 2-QB-aware room paces itself"
      (CLAUDE.md's live choice of FFC's 2QB ADP endpoint for the same reason).
    * `ro` (FantasyPros "PPR" standard REDRAFT ECR, i.e. 1-QB) drives what STRATEGIES draft on
      (`draft_players.dv`, via `synthetic_draft_value`). A 1-QB market drastically underrates
      QB (same date: Josh Allen ECR #26.2) -- this is deliberately the SAME kind of mispricing
      CLAUDE.md documents in the live engine's own Sleeper-projection-derived `dv` (QB1-QB22
      spread of 5.6 PPG vs. 10.6 PPG in seven years of actuals). Using it as the "what does
      best-available-value say" signal is what lets `qb_punt`/`best_value` actually defer QB in
      this backtest, instead of a superflex-priced board making every strategy converge trivially
      (verified empirically: an earlier build using `rsf` for BOTH signals made every strategy,
      including `best_value` with zero QB logic, complete its 2nd QB by round ~3 regardless of
      `qb_deadline_round` -- the deadline never bound, because the "value" signal already knew
      better. That run is not reported; it tested nothing.).

    Player universe is the INTERSECTION of `ro` and `rsf` (both keyed on FantasyPros `id`,
    confirmed the same id space across both products) -- 439 of 493 preseason-relevant players on
    2025-08-29, comfortably above the 150 total roster spots this league fills.
    """
    _ensure_ca_bundle_env()
    import nflreadpy as nfl
    import polars as pl

    cfg = cfg or LeagueConfig.from_yaml()

    rank_df = nfl.load_ff_rankings(type="all")
    rsf_df = rank_df.filter((pl.col("ecr_type") == "rsf") & (pl.col("scrape_date") == scrape_date))
    ro_df = rank_df.filter((pl.col("ecr_type") == "ro") & (pl.col("scrape_date") == scrape_date))
    rsf_rows = rsf_df.select(["player", "pos", "ecr", "sd", "id"]).to_dicts()
    ro_rows = ro_df.select(["player", "pos", "ecr", "sd", "id"]).to_dicts()
    if not rsf_rows or not ro_rows:
        raise RuntimeError(
            f"no preseason ECR rows (rsf={len(rsf_rows)}, ro={len(ro_rows)}) for "
            f"scrape_date={scrape_date!r} -- check nflreadpy.load_ff_rankings(type='all')"
            "['scrape_date'] for the closest real date"
        )
    rsf_by_fpid = {str(r["id"]): r for r in rsf_rows if (r["pos"] or "").upper() in SKILL_POS}
    ro_by_fpid = {str(r["id"]): r for r in ro_rows if (r["pos"] or "").upper() in SKILL_POS}
    universe_fpids = sorted(set(rsf_by_fpid) & set(ro_by_fpid))
    rows = [ro_by_fpid[fpid] for fpid in universe_fpids]  # canonical name/pos from `ro`

    ids = nfl.load_ff_playerids()
    fp_to_gsis: dict[str, str] = {}
    for fpid, gsis in ids.select(["fantasypros_id", "gsis_id"]).iter_rows():
        if fpid and gsis and str(fpid) != "0":
            fp_to_gsis[str(fpid)] = str(gsis)

    stats = nfl.load_player_stats(seasons=[season])
    stats = stats.filter((pl.col("season_type") == "REG") & (pl.col("week") <= cfg.weeks))
    needed_cols = [
        "player_id", "attempts", "completions", "passing_yards", "passing_tds",
        "passing_interceptions", "passing_2pt_conversions", "carries", "rushing_yards",
        "rushing_tds", "rushing_2pt_conversions", "receptions", "targets", "receiving_yards",
        "receiving_tds", "receiving_2pt_conversions", "rushing_fumbles_lost",
        "receiving_fumbles_lost", "sack_fumbles_lost",
    ]
    by_player: dict[str, list[dict]] = {}
    for r in stats.select(needed_cols).iter_rows(named=True):
        by_player.setdefault(r["player_id"], []).append(_canon_row(r))

    schedule = load_bonus_schedule()

    seasons: list[PlayerSeason] = []
    # ro-based draft value (name, pos, ecr, sd) -- what strategies pick on.
    draft_meta: dict[str, tuple[str, str, float, float]] = {}
    # rsf-based bot signal (adp, stdev, pos) -- what the opponent model paces on.
    bot_resolved: dict[str, tuple[float, float | None, str]] = {}
    n_resolved = 0
    n_no_games = 0
    for fpid in universe_fpids:
        row = ro_by_fpid[fpid]
        pos = (row["pos"] or "").upper()
        gsis = fp_to_gsis.get(fpid)
        pid = gsis if (gsis and gsis in by_player) else f"ecr:{fpid}"
        games_rows = by_player.get(pid, [])
        n_games = len(games_rows)
        if n_games > 0:
            n_resolved += 1
            total = sum(score_statline(g, cfg.scoring) for g in games_rows)
            total += actual_bonus(games_rows, schedule).total
            ppg = total / n_games
        else:
            n_no_games += 1
            ppg = 0.0
        seasons.append(
            PlayerSeason(
                player_id=pid, pos=pos, ppg=ppg, expected_games=float(n_games), name=row["player"]
            )
        )
        ro_sd = row.get("sd")
        draft_meta[pid] = (row["player"], pos, float(row["ecr"]), float(ro_sd) if ro_sd is not None else 3.0)

        rsf_row = rsf_by_fpid[fpid]
        rsf_sd = rsf_row.get("sd")
        bot_resolved[pid] = (
            float(rsf_row["ecr"]), float(rsf_sd) if rsf_sd is not None else None, pos,
        )

    dv_map = compute_draft_values(seasons, cfg)  # REAL backtest dv: actual outcome vs actual replacement

    draft_players: list[BoardPlayer] = []
    scoring_dv: dict[str, float] = {}
    for pid, (name, pos, ecr, sd) in draft_meta.items():
        d_dv, d_sd = synthetic_draft_value(ecr, sd)
        draft_players.append(
            BoardPlayer(player_id=pid, name=name, pos=pos, team="", bye=None, adp=ecr, stdev=sd, dv=d_dv, dv_sd=d_sd)
        )
        scoring_dv[pid] = dv_map[pid].dv if pid in dv_map else 0.0

    return Board(
        draft_players=tuple(draft_players),
        scoring_dv=scoring_dv,
        cfg=cfg,
        label=(
            f"HISTORICAL BACKTEST: draft-time value from {season} preseason standard redraft "
            f"ECR (ro), bots paced on {season} preseason superflex ECR (rsf), both scrape_date="
            f"{scrape_date}; scored on {season} real results"
        ),
        diagnostics={
            "mode": "historical", "season": season, "scrape_date": scrape_date,
            "n_players": len(draft_players), "n_resolved_to_real_stats": n_resolved,
            "n_zero_games": n_no_games, "n_rsf_preseason": len(rsf_by_fpid),
            "n_ro_preseason": len(ro_by_fpid), "n_universe": len(universe_fpids),
        },
        bot_resolved=bot_resolved,
    )


# ==================================================================================== draft loop


def run_one_draft(
    *,
    seed: int,
    strategy: Strategy | None,
    strategy_slot: int | None,
    draft_players_by_id: dict[str, BoardPlayer],
    resolved: dict[str, tuple[float, float | None, str]],
    cfg: LeagueConfig,
    qb_rank: dict[str, int],
    qb_startable_rank_cutoff: int,
) -> dict[int, list[str]]:
    """One full `cfg.roster_size`-round, `cfg.teams`-team snake draft. `strategy=None` means
    every seat is the bot model (the baseline run); otherwise `strategy_slot` plays the
    mechanical rule and every other seat is the bot (`draftroom.draft.opponents`), identical in
    structure to `mock_draft_sim.run_one_draft` (imported, not reimplemented, where shared)."""
    rng = np.random.default_rng(seed)
    pool: dict[str, BoardPlayer] = dict(draft_players_by_id)
    have: dict[int, dict[str, int]] = {t: {} for t in range(1, cfg.teams + 1)}
    rosters: dict[int, list[str]] = {t: [] for t in range(1, cfg.teams + 1)}
    run = PositionalRun()
    rounds = cfg.roster_size
    total_picks = cfg.teams * rounds

    for pick_no in range(1, total_picks + 1):
        slot = snake.slot_on_clock(cfg.teams, pick_no)
        available = list(pool.values())
        if not available:
            break

        if strategy is not None and slot == strategy_slot:
            chosen = strategy_pick(
                strategy, available, have, slot, pick_no, cfg,
                qb_rank=qb_rank, qb_startable_rank_cutoff=qb_startable_rank_cutoff,
            )
        else:
            chosen = _bot_pick(rng, available, resolved, slot, pick_no, have[slot], cfg, run)

        pos = pool[chosen].pos
        pool.pop(chosen)
        have[slot][pos] = have[slot].get(pos, 0) + 1
        rosters[slot].append(chosen)
        run.observe(pos, remaining=list(pool.values()))

    return rosters


# =============================================================================== stats plumbing


@dataclass(frozen=True)
class CellStats:
    n: int
    mean: float
    std: float
    se: float
    median: float
    pct_of_baseline_mean: float  # baseline percentile the strategy's mean lands at
    per_rep_pct_range: tuple[float, float]


def summarize(values: list[float], baseline: np.ndarray) -> CellStats:
    arr = np.array(values, dtype=float)
    n = len(arr)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1)) if n > 1 else 0.0
    se = std / math.sqrt(n) if n > 0 else 0.0
    median = float(np.median(arr))
    pct_mean = 100.0 * float((baseline <= mean).mean())
    per_rep_pct = [100.0 * float((baseline <= v).mean()) for v in arr]
    return CellStats(
        n=n, mean=mean, std=std, se=se, median=median, pct_of_baseline_mean=pct_mean,
        per_rep_pct_range=(min(per_rep_pct), max(per_rep_pct)),
    )


# ==================================================================================== main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("historical", "projection"), default="historical")
    ap.add_argument("--season", type=int, default=DEFAULT_SEASON)
    ap.add_argument("--scrape-date", default=DEFAULT_SCRAPE_DATE)
    ap.add_argument("--baseline-reps", type=int, default=1500)
    ap.add_argument("--strategy-reps", type=int, default=250, help="per slot, per strategy")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--out", default=str(REPO_ROOT / "data" / "strategy_tournament_results.json"))
    args = ap.parse_args()

    logging.getLogger("draftroom.prep.crosswalk").setLevel(logging.ERROR)

    print("=" * 100)
    print("QB-TIMING STRATEGY TOURNAMENT")
    print("=" * 100)

    t_build0 = time.perf_counter()
    if args.mode == "historical":
        try:
            board = build_historical_board(args.season, args.scrape_date)
        except Exception as exc:  # noqa: BLE001 -- network/env failure -> documented fallback
            print(f"\n!! historical board build failed ({exc!r}) -- falling back to --mode projection")
            args.mode = "projection"
            board = build_projection_board()
    else:
        board = build_projection_board()
    print(f"\nboard: {board.label}")
    print(f"diagnostics: {board.diagnostics}")
    print(f"board build: {time.perf_counter() - t_build0:.1f}s")

    cfg = board.cfg
    draft_players_by_id = {p.player_id: p for p in board.draft_players}
    resolved = board.bot_resolved or {p.player_id: (p.adp, p.stdev, p.pos) for p in board.draft_players}
    scoring_players_by_id = {
        pid: replace(p, dv=board.scoring_dv.get(pid, 0.0)) for pid, p in draft_players_by_id.items()
    }

    print(
        f"league: {cfg.teams} teams, starters={dict(cfg.starters)}, flex={cfg.flex_slots}x"
        f"{sorted(cfg.flex_eligible)}, rounds={cfg.roster_size}"
    )
    print(f"strategies: {[s.name for s in STRATEGIES]}")

    qb_rank = build_qb_rank(board.draft_players)
    qb_cutoff = qb_startable_rank(cfg)
    print(f"qb_startable_rank cutoff (man-games demand, preseason prior only): {qb_cutoff}")

    # ------------------------------------------------------------ shared bot-only baseline
    t0 = time.perf_counter()
    baseline_by_slot: dict[int, list[float]] = {s: [] for s in range(1, cfg.teams + 1)}
    for i in range(args.baseline_reps):
        rosters = run_one_draft(
            seed=args.seed + i, strategy=None, strategy_slot=None,
            draft_players_by_id=draft_players_by_id, resolved=resolved, cfg=cfg,
            qb_rank=qb_rank, qb_startable_rank_cutoff=qb_cutoff,
        )
        fill = waiver_fill_for_draft(rosters, scoring_players_by_id, cfg)
        for slot, ids in rosters.items():
            baseline_by_slot[slot].append(
                starting_lineup_value(ids, scoring_players_by_id, cfg, waiver_fill=fill)
            )
    baseline_elapsed = time.perf_counter() - t0
    print(
        f"\nbaseline (bot-only, all {cfg.teams} seats): {args.baseline_reps} drafts in "
        f"{baseline_elapsed:.1f}s -> {args.baseline_reps} samples per slot"
    )

    # ------------------------------------------------------------------- per-strategy, per-slot
    results: dict[str, dict[int, list[float]]] = {}
    qb2_round: dict[str, dict[int, list[int | None]]] = {}
    t1 = time.perf_counter()
    for strat in STRATEGIES:
        results[strat.name] = {s: [] for s in range(1, cfg.teams + 1)}
        qb2_round[strat.name] = {s: [] for s in range(1, cfg.teams + 1)}
        strat_t0 = time.perf_counter()
        for slot in range(1, cfg.teams + 1):
            for i in range(args.strategy_reps):
                rosters = run_one_draft(
                    seed=args.seed + 10_000 * slot + i, strategy=strat, strategy_slot=slot,
                    draft_players_by_id=draft_players_by_id, resolved=resolved, cfg=cfg,
                    qb_rank=qb_rank, qb_startable_rank_cutoff=qb_cutoff,
                )
                ids = rosters[slot]
                fill = waiver_fill_for_draft(rosters, scoring_players_by_id, cfg)
                results[strat.name][slot].append(
                    starting_lineup_value(ids, scoring_players_by_id, cfg, waiver_fill=fill)
                )
                qb_seen = 0
                rnd_2nd_qb = None
                for rnd, pid in enumerate(ids, start=1):
                    if draft_players_by_id[pid].pos == "QB":
                        qb_seen += 1
                        if qb_seen == 2:
                            rnd_2nd_qb = rnd
                            break
                qb2_round[strat.name][slot].append(rnd_2nd_qb)
        print(f"  {strat.name:22s} {cfg.teams * args.strategy_reps} drafts in {time.perf_counter() - strat_t0:.1f}s")
    print(f"strategies total: {time.perf_counter() - t1:.1f}s")

    # =================================================================================== report
    baseline_arr = {s: np.array(v) for s, v in baseline_by_slot.items()}

    print("\n" + "=" * 100)
    print("RESULTS: strategy x slot -- mean +/- SE (n reps), percentile vs. bot-only baseline")
    print("=" * 100)
    header = f"{'strategy':22s}" + "".join(f"{'slot ' + str(s):>16s}" for s in range(1, cfg.teams + 1))
    print(header)

    summary: dict[str, dict] = {}
    for strat in STRATEGIES:
        row = f"{strat.name:22s}"
        summary[strat.name] = {}
        for slot in range(1, cfg.teams + 1):
            cs = summarize(results[strat.name][slot], baseline_arr[slot])
            summary[strat.name][slot] = cs
            row += f"{cs.mean:9.1f}+-{cs.se:4.1f}"
        print(row)

    print("\nper-slot percentile (mean) vs. bot-only baseline:")
    header2 = f"{'strategy':22s}" + "".join(f"{'slot ' + str(s):>10s}" for s in range(1, cfg.teams + 1))
    print(header2)
    for strat in STRATEGIES:
        row = f"{strat.name:22s}"
        for slot in range(1, cfg.teams + 1):
            row += f"{summary[strat.name][slot].pct_of_baseline_mean:9.1f}th"
        print(row)

    print("\nbaseline (bot-only) mean +/- SE per slot:")
    for slot in range(1, cfg.teams + 1):
        b = baseline_arr[slot]
        se = b.std(ddof=1) / math.sqrt(len(b))
        print(f"  slot {slot:2d}: {b.mean():9.1f} +/- {se:.2f}  (n={len(b)})")

    # ---------------------------------------------------------------- decomposition
    def pooled(strategy_name: str) -> tuple[float, float, int]:
        vals: list[float] = []
        for slot in range(1, cfg.teams + 1):
            vals.extend(results[strategy_name][slot])
        arr = np.array(vals)
        return float(arr.mean()), float(arr.std(ddof=1) / math.sqrt(len(arr))), len(arr)

    print("\n" + "=" * 100)
    print("DECOMPOSITION: catastrophe vs. genuine sequencing disadvantage")
    print("=" * 100)
    pooled_means = {}
    for strat in STRATEGIES:
        m, se, n = pooled(strat.name)
        pooled_means[strat.name] = (m, se, n)
        print(f"  {strat.name:22s} pooled mean {m:8.1f} +/- {se:5.2f}  (n={n} drafts across {cfg.teams} slots)")

    def diff_report(a: str, b: str) -> None:
        ma, sea, na = pooled_means[a]
        mb, seb, nb = pooled_means[b]
        d = ma - mb
        se_d = math.sqrt(sea**2 + seb**2)
        sig = "EXCEEDS noise (|diff| > 2*SE)" if abs(d) > 2 * se_d else "within noise band"
        print(f"  {a} - {b} = {d:+7.1f}  (SE of diff = {se_d:.2f})  -> {sig}")

    print("\ncatastrophe isolated (qb_never_below_line vs qb_punt -- same best-value logic,")
    print("differ ONLY in whether a QB is force-picked to avoid running out of startable QBs):")
    diff_report("qb_never_below_line", "qb_punt")

    print("\nremaining gap after fixing the catastrophe (qb_never_below_line vs the best fixed-early rule):")
    early_candidates = [s.name for s in STRATEGIES if s.qb_deadline_round is not None and s.qb_deadline_round <= 8]
    best_early = max(early_candidates, key=lambda n: pooled_means[n][0])
    diff_report(best_early, "qb_never_below_line")

    print("\nfor reference -- qb_punt vs best_value (does deferring QB cost anything if the bot")
    print("model itself is priced for QB scarcity, i.e. is qb_punt distinguishable from pure greed):")
    diff_report("best_value", "qb_punt")

    # ---------------------------------------------------------------- QB2-round diagnostics
    print("\n" + "=" * 100)
    print("WHEN does each strategy actually complete its 2nd QB? (round, pooled across slots)")
    print("=" * 100)
    qb2_summary: dict[str, dict] = {}
    for strat in STRATEGIES:
        rounds: list[int] = []
        never = 0
        for slot in range(1, cfg.teams + 1):
            for r in qb2_round[strat.name][slot]:
                if r is None:
                    never += 1
                else:
                    rounds.append(r)
        if rounds:
            arr = np.array(rounds, dtype=float)
            qb2_summary[strat.name] = {
                "median_round": float(np.median(arr)), "mean_round": float(arr.mean()),
                "p90_round": float(np.percentile(arr, 90)), "n_never": never, "n": len(rounds),
            }
            print(
                f"  {strat.name:22s} median round {np.median(arr):4.1f}  mean {arr.mean():5.1f}  "
                f"p90 {np.percentile(arr, 90):5.1f}  never-completed={never}/{never + len(rounds)}"
            )
        else:
            qb2_summary[strat.name] = {"n_never": never, "n": 0}
            print(f"  {strat.name:22s} NEVER completed 2 QBs in any of {never} drafts")

    # ---------------------------------------------------------------------------- persist
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": args.mode,
        "board_label": board.label,
        "diagnostics": board.diagnostics,
        "qb_startable_rank_cutoff": qb_cutoff,
        "baseline_reps": args.baseline_reps,
        "strategy_reps": args.strategy_reps,
        "seed": args.seed,
        "cfg": {
            "teams": cfg.teams, "starters": dict(cfg.starters), "flex_slots": cfg.flex_slots,
            "flex_eligible": sorted(cfg.flex_eligible), "rounds": cfg.roster_size, "weeks": cfg.weeks,
        },
        "baseline_by_slot": {
            str(s): {"mean": float(b.mean()), "std": float(b.std(ddof=1)), "n": len(b)}
            for s, b in baseline_arr.items()
        },
        "results": {
            strat.name: {
                str(slot): {
                    "n": summary[strat.name][slot].n,
                    "mean": summary[strat.name][slot].mean,
                    "std": summary[strat.name][slot].std,
                    "se": summary[strat.name][slot].se,
                    "median": summary[strat.name][slot].median,
                    "pct_of_baseline_mean": summary[strat.name][slot].pct_of_baseline_mean,
                    "per_rep_pct_range": list(summary[strat.name][slot].per_rep_pct_range),
                }
                for slot in range(1, cfg.teams + 1)
            }
            for strat in STRATEGIES
        },
        "pooled": {
            strat.name: {"mean": pooled_means[strat.name][0], "se": pooled_means[strat.name][1], "n": pooled_means[strat.name][2]}
            for strat in STRATEGIES
        },
        "qb2_completion_round": qb2_summary,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nresults written: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
