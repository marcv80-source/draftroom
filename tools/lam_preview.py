"""Preview, not a setting: show Marc the top-30 board at lam=0.0 vs a candidate lam=0.10
before he picks a risk-aversion value for `valuation/evob.py`'s `lam` knob.

CLAUDE.md/task framing: `lam` defaults to 0.0 (risk-neutral) and that default is Marc's
explicit preference, promised a preview before changing it. This tool NEVER changes the
default -- it only renders a side-by-side comparison to `data/lam_preview.md` so Marc can see
what a nonzero lam would actually do to the board before deciding whether he wants one.

Reads only the cached real board (`draftroom.validate.board.build_real_board`) -- no network
call, safe to run anytime, including on draft night's own machine.

Usage:
    python -m tools.lam_preview [--lam 0.10] [--top 30]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from draftroom.valuation.evob import DraftValue, compute_draft_values  # noqa: E402
from draftroom.validate import board as board_mod  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "data" / "lam_preview.md"


def _ranked(values: dict[str, DraftValue]) -> list[tuple[int, DraftValue]]:
    ordered = sorted(values.values(), key=lambda v: (-v.dv, v.player_id))
    return list(enumerate(ordered, start=1))


def render_preview(seasons, cfg, *, lam_candidate: float, top: int) -> str:
    values_0 = compute_draft_values(seasons, cfg, lam=0.0)
    values_c = compute_draft_values(seasons, cfg, lam=lam_candidate)

    ranked_0 = _ranked(values_0)
    ranked_c = _ranked(values_c)

    rank_0_by_pid = {v.player_id: rank for rank, v in ranked_0}
    rank_c_by_pid = {v.player_id: rank for rank, v in ranked_c}

    top_ids_0 = {v.player_id for rank, v in ranked_0 if rank <= top}
    top_ids_c = {v.player_id for rank, v in ranked_c if rank <= top}
    union_ids = top_ids_0 | top_ids_c

    # Sort the merged view by the candidate-lam rank (the "new" ordering), pushing anyone who
    # dropped out of the top N under the candidate lam (but was in it at lam=0) to the bottom
    # by their (deeper) candidate rank, so the reader sees the whole story in one pass.
    rows = sorted(union_ids, key=lambda pid: rank_c_by_pid[pid])

    lines: list[str] = []
    lines.append(f"# lam preview: 0.0 vs {lam_candidate:.2f}")
    lines.append("")
    lines.append(f"Generated {datetime.now(timezone.utc).isoformat()} from the cached real board "
                 f"(`draftroom.validate.board.build_real_board()`). This is a PREVIEW ONLY -- "
                 f"the `lam` default in `valuation/evob.py` stays 0.0 (Marc's explicit "
                 f"preference) until he says otherwise; nothing here changes it.")
    lines.append("")
    lines.append(f"`dv = evob - lam * sigma_season`. At lam=0.0 the board is risk-neutral "
                 f"(dv == evob). At lam={lam_candidate:.2f}, a player's dv is penalised by "
                 f"{lam_candidate:.2f} points per point of season-total sigma -- so this only "
                 f"actually moves anyone whose `sigma_season` is nonzero (see "
                 f"`DraftValue.sigma_source`: cross-source disagreement is now one way that "
                 f"gets populated instead of reading 'absent').")
    lines.append("")
    lines.append(f"Top {top} at each lam, merged (sorted by rank at lam={lam_candidate:.2f}). "
                 f"`move` is the rank change from lam=0.0 to lam={lam_candidate:.2f} (positive "
                 f"= moved UP/better, negative = moved DOWN/worse). `NEW`/`OUT` mark players "
                 f"who entered or fell out of the top {top} entirely.")
    lines.append("")
    lines.append(
        f"| Rank@{lam_candidate:.2f} | Rank@0.0 | Move | Player | Pos | dv@0.0 | dv@{lam_candidate:.2f} | sigma_season |"
    )
    lines.append("|---:|---:|:---:|:---|:---:|---:|---:|---:|")

    by_pid_0 = {v.player_id: v for _rank, v in ranked_0}
    by_pid_c = {v.player_id: v for _rank, v in ranked_c}

    n_moved = 0
    n_new = 0
    n_out = 0
    for pid in rows:
        v0 = by_pid_0[pid]
        vc = by_pid_c[pid]
        r0 = rank_0_by_pid[pid]
        rc = rank_c_by_pid[pid]

        in_top_0 = pid in top_ids_0
        in_top_c = pid in top_ids_c
        if in_top_0 and not in_top_c:
            move_str = "OUT"
            n_out += 1
        elif in_top_c and not in_top_0:
            move_str = "NEW"
            n_new += 1
        else:
            delta = r0 - rc  # positive: candidate rank is better (smaller number)
            if delta == 0:
                move_str = "-"
            else:
                n_moved += 1
                move_str = f"UP {delta}" if delta > 0 else f"DOWN {-delta}"

        rank_c_display = str(rc) if in_top_c else f"({rc})"
        rank_0_display = str(r0) if in_top_0 else f"({r0})"
        lines.append(
            f"| {rank_c_display} | {rank_0_display} | {move_str} | {v0.name or pid} | {v0.pos} | "
            f"{v0.dv:.1f} | {vc.dv:.1f} | {v0.sigma_season:.2f} |"
        )

    lines.append("")
    lines.append(
        f"Summary: {len(rows)} players in the merged top-{top} view; {n_moved} moved rank "
        f"within it, {n_new} entered under lam={lam_candidate:.2f} who weren't in the lam=0.0 "
        f"top {top}, {n_out} fell out. "
        + ("No sigma_season is populated on this board yet, so a lam!=0 changes NOTHING -- "
           "every dv above is identical to evob. Run this again once cross-source disagreement "
           "or another sigma source is populated for these players." if n_moved == 0 and n_new == 0 and n_out == 0
           else "")
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lam", type=float, default=0.10, help="candidate lam to preview against 0.0")
    parser.add_argument("--top", type=int, default=30, help="board depth to compare")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output .md path")
    args = parser.parse_args(argv)

    real = board_mod.build_real_board()
    text = render_preview(real.seasons, real.cfg, lam_candidate=args.lam, top=args.top)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out}")
    print()
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
