import type { DraftPick } from "../types";

/** player_id -> pick_no, over currently-filled (non-voided, non-stub) picks only. Used so any
 * player-name click site (tier board, search results, recommendation candidates) can offer
 * "undraft" without the backend needing to carry pick_no on every payload shape that names a
 * player -- `all_picks` (plan A3) is the one source of truth for the mapping. */
export function buildPickNoIndex(picks: DraftPick[] | undefined): Record<string, number> {
  const out: Record<string, number> = {};
  for (const p of picks ?? []) {
    if (p.player_id && !p.voided) out[p.player_id] = p.pick_no;
  }
  return out;
}

/** The highest pick_no among filled, non-voided picks -- "the most recent pick" for the A2
 * confirm rule (undraft is instant for the most recent pick, and confirms otherwise because it
 * rewrites history mid-board). Robust to out-of-order picks: this is a MAX over what's actually
 * filled, not "current_pick - 1". */
export function mostRecentPickNo(picks: DraftPick[] | undefined): number | null {
  let max: number | null = null;
  for (const p of picks ?? []) {
    if ((p.player_id || p.is_stub) && !p.voided) {
      if (max === null || p.pick_no > max) max = p.pick_no;
    }
  }
  return max;
}
