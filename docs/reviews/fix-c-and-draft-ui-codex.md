# Codex review triage — fix-c-and-draft-ui (2026-08-18)

Raw output: `reviews/_fix-c-and-draft-ui_raw_output.txt`. Verdict: "do not ship yet."
8 findings. Triage below. FIX-NOW = must land before commit; DEFER = recorded, not dropped.

## CRITICAL

1. **Draft mode bypasses the snapshot/valuation pipeline.** `create_app()` builds the board from
   `live_data.load_player_pool()` with `dv = 300 - ADP` placeholder and `dv_sd = 0.0`; bonus scoring,
   games curves, EVoB, and sigma never reach draft night, and all 980 players (incl. unranked) enter
   `recommend()` candidate generation. **FIX-NOW.** Server must build its board via
   `validate.board.build_real_board()` (cached files only — offline-safe), fall back to the placeholder
   ONLY with a loud startup warning + payload flag, and `recommend()` must only ever see
   `is_ranked=True` players. This was pre-existing architecture, but fix "C" made dv move rankings,
   which upgraded it from cosmetic to critical.

## HIGH

2. **Invariant gate "weakened".** Partly a stale-contract problem: replacing the 10-14 QB band with the
   comparative 1QB-vs-2QB test was explicitly approved (handoff Decisions #8) because the band came from
   the abandoned 12-team assumption. **FIX-NOW (docs + strengthen):** update CLAUDE.md's gate text to the
   comparative test so contract and code agree, AND make the expected-games invariant run against the
   real board (not only the synthetic pool) so it can catch finding #3-class bypasses.
3. **`build_real_board()` bypasses the expected-games curves** — it sets `expected_games` straight from
   Sleeper's per-player games, so the rank-conditional curves are inert on the real board. **FIX-NOW.**
   Policy: `expected_games = min(source_games, curve(pos, rank))` — the curve is fitted actual
   availability and acts as a cap; a source projecting BELOW the curve (suspension, known injury) is
   trusted. Re-run sims after, since this moves the board.
4. **Mutation endpoints can fsync invalid events** (`correct` accepts unknown/already-drafted players,
   `pick` accepts unknown player or occupied pick_no and silently replaces on replay, `clock` accepts
   zero/negative/post-draft picks → replay crash). **FIX-NOW.** Validate BEFORE append on all three;
   an event that would corrupt replay must be rejected with a 4xx, never written.
5. **Scarcity floor can force a QB while supply is fine** — trigger assumes every intervening pick
   consumes supply while demand stays constant, and defines "startable" as `dv > 0` (placeholder makes
   nearly all QBs positive), diverging from the tournament's cutoff. **FIX-NOW.** Trigger becomes:
   force when `startable_supply − teams_needing_position_before_next_turn < my_unfilled_slots_at_position`,
   with "startable" defined by the same replacement-level cutoff the tournament used. Re-verify vs sims.

## MEDIUM

6. **Recommendation panel goes stale after void/correct** (refetch keyed only on `current_pick`).
   **FIX-NOW:** key the fetch on an event-log version counter from the payload.
7. **Demand clock shows zero opponent picks when Marc is on the clock** (`next_pick == current_pick`).
   **FIX-NOW:** when on the clock, the window is current+1 → following turn; fix the test that
   enshrines the zero.
8. **VONA survival off-by-one:** conditions from `current_pick`, pricing an opponent taking a player
   during Marc's own pick; at back-to-back turns partner survival must be exactly 1.0. **FIX-NOW:**
   condition from `current_pick + 1`. Now decision-affecting since VONA moves rankings.

## LOW

9. **Disagreement join key `name|team` can silently collide.** **FIX-NOW (cheap):** include position,
   assert uniqueness at build time.
10. **DraftNight.bat expands raw prompt input as batch syntax** before validation. **FIX-NOW (cheap):**
    delayed expansion so metacharacters can't execute. **DEFER:** the 1-10 range is hardcoded while the
    contract says team count comes from config — acceptable for a personal launcher; noted here so it
    isn't lost.

## Deferred summary
- DraftNight.bat hardcoded 10-team slot range (LOW; personal launcher, league is 10 teams this season).

## Post-fix gates required before commit
pytest all green · invariants gate PASS (now including the real-board games check) · mock-draft sim
re-run on the fixed board (bar: 65th pct from most slots) · npm build clean · dry run incl. new
validation rejections (invalid clock/pick/correct must 4xx and NOT append).
