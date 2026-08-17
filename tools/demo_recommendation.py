"""Demo: the full recommendation engine on a plausible mid-draft board, real ADP.

Builds a board from the REAL cached FFC 2QB ADP payload (`data/raw/ffc/*.json`) -- 222 real
players, real mean ADP, real ADP standard deviation. Nothing about WHO the players are or WHERE
they typically go is invented.

What IS invented, clearly labeled below: **draft value**. This codebase's real valuation model
(`draftroom.valuation.evob`) needs per-player PPG projections, which do not exist yet (CLAUDE.md:
projections are a separate, not-yet-wired pipeline). So this demo derives a SYNTHETIC draft value
directly from ADP -- earlier ADP means a higher synthetic value, nothing more sophisticated than
that. Every number that traces back to this synthetic value is exactly as fake as this sentence
says it is; treat the RECOMMENDATION LOGIC as the thing under test here, not the specific
players it names.

Scenario: 15 picks have gone by, followed roughly by ADP ("chalk") -- exactly enough that draft
slot 9's own pick 2.04 (overall pick 16) is the CURRENT pick on the clock. (The task's "~20
picks" is approximate scene-setting; landing exactly on slot 9's own pick 2.04 requires exactly
15 picks to have already happened, since pick 16 belongs to slot 9 -- 20 would mean pick 16
already happened too.)

Run:
    C:\\dev\\draftroom\\.venv\\Scripts\\python.exe tools\\demo_recommendation.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from draftroom.config import LeagueConfig  # noqa: E402
from draftroom.draft import opponents as opp  # noqa: E402
from draftroom.draft import snake  # noqa: E402
from draftroom.draft.recommend import BoardPlayer, recommend  # noqa: E402
from draftroom.draft.state import DraftState, Pick  # noqa: E402
from draftroom.draft.survival import load_ffc_adp  # noqa: E402
from draftroom.explain.render import as_text  # noqa: E402

TEAMS = 12
DRAFT_SLOT = 9
N_PRIOR_PICKS = 15  # see module docstring: lands exactly on slot 9's pick 2.04 (overall 16)
SEED = 2026


def synthetic_draft_value(adp: float, stdev: float | None) -> tuple[float, float]:
    """SYNTHETIC (adp, stdev) -> (dv, dv_sd). Earlier ADP = higher value, nothing else. The
    real model (`draftroom.valuation.evob.DraftValue`) needs PPG projections this demo does not
    have; this is a clearly-labeled stand-in so the recommendation engine has *something*
    ordered to rank, not a claim about any player's actual worth."""
    dv = max(0.5, 200.0 - adp)
    dv_sd = 0.4 * (stdev if stdev is not None else 3.0) * (dv / 20.0 + 1.0)
    return dv, dv_sd


def main() -> None:
    adp_players = load_ffc_adp()
    print("=" * 92)
    print("DEMO: full recommendation engine, real FFC ADP, SYNTHETIC draft values")
    print(f"cached FFC payload: {len(adp_players)} players")

    cfg = LeagueConfig.from_yaml()  # the real (provisional) league config, data/league_manual.yaml
    print(
        f"league: {cfg.teams} teams, starters={dict(cfg.starters)}, flex={cfg.flex_slots}x"
        f"{sorted(cfg.flex_eligible)}, bench={cfg.bench}, roster_size={cfg.roster_size}"
    )
    for note in cfg.provenance:
        print(f"  [config note] {note}")

    players = [
        BoardPlayer(
            player_id=str(p.player_id),
            name=p.name,
            pos=p.pos,
            team=p.team,
            bye=p.bye,
            adp=p.adp,
            stdev=p.stdev,
            dv=synthetic_draft_value(p.adp, p.stdev)[0],
            dv_sd=synthetic_draft_value(p.adp, p.stdev)[1],
        )
        for p in adp_players
    ]

    current_pick = snake.overall_pick(TEAMS, 2, DRAFT_SLOT)
    assert current_pick == 16, current_pick
    assert snake.pick_label(TEAMS, current_pick) == "2.04"

    rounds = cfg.roster_size
    state = DraftState(teams=TEAMS, rounds=rounds, my_slot=DRAFT_SLOT, current_pick=current_pick)

    by_adp = sorted(adp_players, key=lambda p: p.adp)
    prior = by_adp[:N_PRIOR_PICKS]
    for i, p in enumerate(prior, start=1):
        team_slot = snake.slot_on_clock(TEAMS, i)
        state.picks[i] = Pick(pick_no=i, team_slot=team_slot, player_id=str(p.player_id))
    state.current_pick = current_pick

    print(f"\n{N_PRIOR_PICKS} picks made so far (chalk -- ADP rank order), by team:")
    for i, p in enumerate(prior, start=1):
        team_slot = snake.slot_on_clock(TEAMS, i)
        marker = "  <-- YOU" if team_slot == DRAFT_SLOT else ""
        print(f"  pick {i:>3} ({snake.pick_label(TEAMS, i):>5})  team {team_slot:>2}: {p.name} ({p.pos}){marker}")

    calibration = opp.LeagueCalibration.national_only()
    print(
        "\nopponent calibration: LeagueCalibration.national_only() -- Yahoo pick-by-pick "
        "history for this league does not exist yet (CLAUDE.md), so the opponent model runs "
        "on pure national ADP + live positional runs, zero league-specific timing/reach offset."
    )

    print(f"\nrunning recommend() with n_sims=500, seed={SEED} ...")
    t0 = time.perf_counter()
    rec = recommend(state, cfg, players, n_sims=500, calibration=calibration, seed=SEED)
    elapsed = time.perf_counter() - t0
    print(f"recommend() wall clock (includes the shared 500-sim Monte Carlo roll-forward): {elapsed:.3f}s")

    print("\n" + "=" * 92)
    print("RENDERED RECOMMENDATION")
    print("=" * 92)
    print(as_text(rec, top_n=4))
    print()
    return rec


if __name__ == "__main__":
    main()
