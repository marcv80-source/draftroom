"""Gate two room-behavior experiments against the real 2025 draft -- ship only what wins.

Both experiments extend the opponent model with structure the flat-offset calibration (which
already FAILED its leave-one-manager-out gate -- see ``tools/calibrate_opponents.py`` and the
shipped empty offsets in ``data/opponent_calibration_2025.json``) cannot express:

1. **Room QB-timing prior** (``LeagueCalibration.qb_mu_curve``): a monotone piecewise-linear
   remap of scaled-national QB ADP onto this room's own observed QB pick numbers,
   rank-matched (the r-th cheapest QB nationally maps to the pick where the room took its
   r-th QB -- no player identity involved, so ADP-year drift cannot confound it). A CURVE,
   not a constant, because the room's pace error changes sign: 7 QBs by pick 20 where the
   scaled feed expects ~14 (slow start), then an avalanche at picks 44-60 (fast middle). A
   flat mean nets those to ~nothing, which is exactly why the flat fit failed.

2. **Satiation damper** (``LeagueCalibration.satiation_damper``): picks of softmax utility
   subtracted from a non-flex position where the manager has already filled every dedicated
   starter slot. Observed room reality: 1 luxury QB in 21 QB picks before pick 85, while the
   un-damped model still feels the full ``-mu`` pull toward elite leftover QBs on
   QB-complete teams. Grid-searched over a few values; with one season of data the search IS
   in-sample model selection, so the verdict states the margin, not just the winner.

THE GATE (same as calibrate_opponents.py): leave-one-manager-out over the 10 slots. Each
held-out slot's real picks are scored by ``opponents.opponent_pick_probabilities`` under a
calibration fit ONLY on the other nine managers' picks; the metric is the mean rank of the
actual pick among the truly-available pool. Per the project's own rule: if it does not beat
plain ADP out of sample, plain ADP ships and the measured numbers are recorded under a
``measured_*``-style block with ``enabled: false``.

Output: ``data/room_priors_2025.json``. This tool NEVER writes
``data/opponent_calibration_2025.json`` (owned by calibrate_opponents.py).

Run:
    C:\\dev\\draftroom\\.venv\\Scripts\\python.exe tools\\fit_room_prior.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools"))

import analyze_2025_draft as draft2025  # noqa: E402
import calibrate_opponents as co  # noqa: E402
from draftroom.config import LeagueConfig  # noqa: E402
from draftroom.draft import opponents as opp  # noqa: E402

OUT_PATH = ROOT / "data" / "room_priors_2025.json"
SATIATION_GRID = (4.0, 8.0, 16.0)


def fit_qb_curve(
    rows: Sequence[dict], ffc_players: Sequence[dict], *, scale: float, exclude_slot: int | None
) -> tuple[tuple[float, float], ...]:
    """Rank-matched monotone map: scaled-national QB ADP -> room QB pick number.

    Knot r pairs the r-th smallest scaled-national QB ADP with the pick number of the room's
    r-th QB selection (optionally excluding one held-out manager's picks). Both sequences are
    sorted ascending, so the curve is monotone by construction.
    """
    nat = sorted(float(p["adp"]) * scale for p in ffc_players if p["position"] == "QB")
    room = sorted(
        float(r["pick_no"])
        for r in rows
        if r["pos"] == "QB" and (exclude_slot is None or r["slot"] != exclude_slot)
    )
    k = min(len(nat), len(room))
    return tuple((nat[i], room[i]) for i in range(k))


def main() -> int:
    rows = draft2025.load()
    if not draft2025.validate(rows):
        print("Transcription checks failed -- refusing to fit on untrusted data.")
        return 1

    raw, ffc_filename = co.load_newest_ffc_payload()
    ffc_players = raw["players"]
    teams_national = int(raw["meta"]["teams"])
    scale = opp.scale_adp_to_league(
        1.0, teams_national=teams_national, teams_league=co.TEAMS_LEAGUE
    )
    cfg = LeagueConfig.from_yaml()
    pick_ids = co.match_pick_ids(rows, ffc_players)
    pool_template = co.build_player_pool(ffc_players, scale=scale)

    print("=" * 92)
    print("ROOM PRIOR GATE -- leave-one-manager-out, real 2025 picks, real opponent softmax")
    print("=" * 92)
    print(f"ADP source: data/raw/ffc/{ffc_filename}, rescale factor {scale:.4f}")

    def lomo(make_calib) -> dict:
        """make_calib(exclude_slot) -> LeagueCalibration fit without that slot's picks."""
        per_slot = {s: make_calib(s) for s in range(1, co.TEAMS_LEAGUE + 1)}
        return co.replay_and_score(rows, pick_ids, pool_template, cfg, lambda slot: per_slot[slot])

    plain = lomo(lambda s: opp.LeagueCalibration.national_only())

    variants: dict[str, dict] = {}
    variants["qb_curve"] = lomo(
        lambda s: opp.LeagueCalibration(
            qb_mu_curve=fit_qb_curve(rows, ffc_players, scale=scale, exclude_slot=s)
        )
    )
    for d in SATIATION_GRID:
        variants[f"satiation_{d:g}"] = lomo(
            lambda s, d=d: opp.LeagueCalibration(satiation_damper=d)
        )
    best_sat = min(
        (name for name in variants if name.startswith("satiation_")),
        key=lambda name: variants[name]["mean_rank"],
    )
    best_d = float(best_sat.split("_", 1)[1])
    variants["qb_curve_plus_" + best_sat] = lomo(
        lambda s: opp.LeagueCalibration(
            qb_mu_curve=fit_qb_curve(rows, ffc_players, scale=scale, exclude_slot=s),
            satiation_damper=best_d,
        )
    )

    def _fmt(res: dict) -> str:
        return (
            f"n={res['n_scored']}  top1={res['top1_accuracy']:.3f}  "
            f"top3={res['top3_accuracy']:.3f}  mean_rank={res['mean_rank']:.2f}  "
            f"mrr={res['mrr']:.4f}"
        )

    print(f"\n  {'plain ADP (national_only)':34s}: {_fmt(plain)}")
    for name, res in variants.items():
        delta = plain["mean_rank"] - res["mean_rank"]
        print(f"  {name:34s}: {_fmt(res)}  delta_vs_plain={delta:+.2f}")

    # A variant ships only on a MEANINGFUL out-of-sample win. With 129 scored picks and a
    # per-pick rank sd around 30, a mean-rank delta under ~1 pick is indistinguishable from
    # noise -- and the grid search over satiation values is itself model selection on the same
    # 129 picks, so a hair's-breadth "win" there is exactly the overfit the gate exists to
    # block. (For scale: the flat offset FAILED this gate at -2.9.)
    MIN_MEANINGFUL_DELTA = 1.0
    winners = {
        name: res
        for name, res in variants.items()
        if plain["mean_rank"] - res["mean_rank"] >= MIN_MEANINGFUL_DELTA
    }
    if winners:
        best = min(winners, key=lambda n: winners[n]["mean_rank"])
        verdict = (
            f"{best} BEATS plain ADP out-of-sample by a meaningful margin "
            f"(mean rank {winners[best]['mean_rank']:.2f} vs {plain['mean_rank']:.2f})."
        )
        enabled = best
    else:
        best_any = min(variants, key=lambda n: variants[n]["mean_rank"])
        best_delta = plain["mean_rank"] - variants[best_any]["mean_rank"]
        verdict = (
            f"NO variant beats plain ADP by a meaningful margin (best: {best_any} at "
            f"{best_delta:+.2f}, threshold {MIN_MEANINGFUL_DELTA:+.2f}) -- per the project's "
            "own rule, plain ADP stays shipped; everything here is recorded as measured-only."
        )
        enabled = None
    print(f"\n  VERDICT: {verdict}")

    full_curve = fit_qb_curve(rows, ffc_players, scale=scale, exclude_slot=None)
    payload = {
        "generated_by": "tools/fit_room_prior.py",
        "source_draft": "data/draft_2025.csv",
        "adp_source": f"data/raw/ffc/{ffc_filename}",
        "gate": "leave-one-manager-out mean rank vs plain ADP (see calibrate_opponents.py)",
        "plain_adp": plain,
        "variants": variants,
        "verdict": verdict,
        "enabled_variant": enabled,
        "qb_mu_curve_full_sample": [[x, y] for x, y in full_curve],
        "satiation_grid": list(SATIATION_GRID),
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
