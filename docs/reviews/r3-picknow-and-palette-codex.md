# Codex review — `r3-picknow-and-palette` (2026-08-26)

Batch: ledger #12 (ALL ranks by best pick now), #13 (positional-rank chips), #14 (Clubhouse
palette), #15 (board rank column). Raw transcript is gitignored by design; this is the record.

Verdict on arrival: **"Do not ship this batch yet"** — 3 × P1, 2 × P2, 2 × P3. All seven addressed,
none deferred. Codex could not execute the suite in its sandbox (no venv, no Node), so it reviewed
statically; the suite and gate were run here.

## P1 — fixed

**1. ALL did not reproduce the panel's comparator; it reconstructed it.**
The board sorted by `(boolean gate, value + VONA, ADP)`. The engine sorts by
`(gate_priority, utility)`, and `utility` is not `value + VONA`: at a back-to-back turn the panel
optimises a **pair**, and mid-round `utility` carries a candidate-specific continuation and risk
term. The 16-of-16 agreement I measured was **one board state, not an identity** — a fair hit, and
the correction is in the code comments as well as the code.
*Fix:* `Candidate.gate_priority` is now published (2 = scarcity floor, 1 = elite grab, 0 = value
alone), and the board slices the leading gated run off the panel's own ordered candidate list and
preserves that order exactly. `value + VONA` now ranks only the ungated remainder, which the panel
does not rank at all, and is documented as an approximation rather than an identity.

**2. A scarcity floor would have hoisted every remaining player at the position.**
`isGated()` tested `forced_positions`, which names a whole **position**, while the panel's gate
covers only candidates that passed feasibility and the per-position top-N cut. Since "startable"
is a man-games rank cutoff (QB22), many QB23-and-lower players can remain — so a QB floor would
have put every one of them above every RB/WR/TE. That is precisely the large-block failure I had
told myself the design avoided, and my code comment asserted it "can never hoist a large block."
It could. *Fix:* the gate is keyed on candidate IDs only; `forced_positions` is explanatory text
and never a sort key. Pinned by `TestGateCannotHoistAWholeBlock`.

**3. A stale recommendation could sort a new board.**
Fetches were async, unsequenced, and a **failure left the previous payload in place**. After a pick
the board would keep ordering itself by the old VONA and old gates — indefinitely, if a request
failed. Tolerable when the payload only fed a panel; not once it drives a sort. *Fix:* responses
carry a monotonic request id (older arrivals discarded) and the `event_seq` they were computed for;
`recIsCurrent` gates every pick-now input, and a failure clears rather than keeps. The board falls
back to plain draft value whenever the recommendation does not describe the board on screen.

## P2 — fixed

**4. Empty VONA did not produce the documented fallback.** The NOW column checked `hasVona` but the
gate did not, so the final round (VONA empty, gates still live) would have re-ordered the board with
no NOW column and no explanation. *Fix:* one `pickNowReady` flag drives sort, column, badges and
note together.

**5. Two small-text contrast failures in the new palette.** `--danger` on `--danger-bg` measured
4.28:1 in 10–13px badges (the old pair was 5.38:1), and the new 9.5px column header measured
3.82:1. Both under 4.5:1. *Fix:* `--danger` lightened to `#ff86a6`, `--danger-bg` deepened to
`#2d0f1b`, header moved to `--text-dim`. Codex also caught that `--rb` and `--accent` are the same
lime while the comment claimed all position colours differ from the accent. The colour is what Marc
approved and stays; **the false comment was corrected** and flags `--rb` as the token to move if the
chips ever fight the active-control lime on screen.

## P3 — fixed

**6. Header could drift from the rows when the scrollbar appeared** — the header is a sibling of the
scrolling `.board-list`, not a child. *Fix:* `scrollbar-gutter: stable`.

**7. Old-palette leftovers and stale wording.** The loading state hardcoded the old dim grey
`#8fa1b3`; nine dead `var(--token, #oldcolor)` fallbacks remained; and the ALL tab tooltip still
said "ranked by draft value," which the change had made false. All three fixed — the tooltip
mattered most, since Marc reads tooltips.

## Confirmed correct by the review

- Allen is lifted by the gate, not by VONA — the two are not conflated.
- `filter().sort()` does not mutate the shared board payload.
- Positional ranks are value-ordered and exclude unranked / no-projection players.
- Drafted rows cannot retain a REC badge.
- A null or fully empty recommendation degrades to draft value cleanly.
- Header and row column counts match in both modes.

## Found here, not by Codex

**At a back-to-back turn every VONA is legitimately 0.0**, and the keys are still present — so
`pickNowReady` was true and the NOW column rendered exactly DV on all 199 rows, reading as a broken
column rather than as the real fact that waiting costs nothing when you pick twice in a row. Only
visible on the live board with Marc at slot 1 previewing pick 20. Readiness now requires a non-zero
price, and the at-the-turn case says so in words.

## Verification

- New tests: `tests/test_picknow_board.py`, 11 cases. **Mutation-verified** — unstamping
  `gate_priority` fails 2 of them.
- Suite **793 passed** (782 → 793). Invariant gate **8/8 PASS**. Frontend build clean under `tsc`.
- Live: bundle `index-CB5ZDYga.js` / `index-B1r45zuU.css` confirmed served from a freshly started
  process, checked against real drafted state (5 picks in the dry-run log, slot 1, at the turn).
  Board top 3 == panel top 3.
