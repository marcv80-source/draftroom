# Codex review triage — `ui-rebuild-four-source-composite`

**Reviewed:** 2026-08-21 · **Raw output:** `reviews/_ui-rebuild-four-source-composite_raw_output.txt`
**Prompt:** `reviews/_ui-rebuild-four-source-composite_prompt.txt` (211 KB: inlined diff of the
high-risk backend and frontend pick logic, full text of the risky new modules, and a pointer list
for the rest, which Codex read from disk)

The batch under review: the draft-night UI rebuild (click-anywhere drafting, undraft, Draft Results
tab with right-click actions, team names, projection-source toggle) plus the projection layer
rebuild (equal-weight four-source composite, FantasySharks as the fourth family, the human review
queue).

**10 findings: 2 CRITICAL, 3 HIGH, 5 MEDIUM. 9 fixed, 1 deferred.** Every finding was verified
against the actual code before being acted on; none were taken on the report's word.

Codex's own summary opened "this tree is not safe for draft night yet," on the strength of
findings 1 and 2. Finding 2 is fixed. Finding 1 is real, is architectural, and is **not** a
regression from this batch — see the deferred section.

---

## Fixed

### 1. (CRITICAL → fixed) Undrafting the newest pick left the clock advanced

`frontend/src/App.tsx`, `components/DraftResultsTab.tsx`, `draft/state.py`, `server.py`

The worst class of bug this tool can have: bookkeeping that drifts silently. The `x` on a row and
the Draft Results "Remove" both called `/api/void`, which marks the pick void but does **not**
rewind `current_pick`. Sequence: picks 1–5 recorded, clock at 6; click `x` on pick 5; pick 5 is
void but the clock stays 6; the replacement player is recorded at **pick 6 for slot 6**. Pick 5 is
left empty and every subsequent pick on the physical board is attributed one slot off. Nothing on
screen said so. The App.tsx comment even read "Ctrl+Z undoes it anyway" — the author intended undo
semantics and wired void.

Fixed with a new atomic `POST /api/undraft` and `DraftSession.undraft_pick`, which decides
server-side:

- target is the newest pick event → append `undo`, so replay drops the pick and the clock returns
  on its own;
- target is older → append `pick_voided`, leaving a hole that `gaps()` already reports.

**One appended event either way.** Deliberately not a `void` + `clock_set` pair, per Codex's own
note: a crash landing between the two halves would leave the log describing a draft that never
happened. The response carries `last_undraft: {pick_no, mode}` so the UI can say which happened.

Pinned by 5 tests in `tests/test_server.py`, including the one-event count and a relaunch that
replays the rewound clock.

### 2. (HIGH → fixed) Reassign-to-team was non-functional from the shipped UI

`frontend/src/api.ts`, `components/DraftResultsTab.tsx`, `draft/events.py`, `draft/state.py`, `server.py`

The Draft Results context menu's "Reassign to team…" sent `{pick_no, team_slot}` through
`correctPick`, which serializes the identity fields as `null`. `/api/correct` requires a
`player_id` or a `stub_name`, so every reassign returned **422**. The feature had never worked.

The existing tests passed because they resend `player_id` — they exercised the endpoint, not the
UI contract. That gap is why the new test is written against the exact payload the frontend sends.

Fixed with a dedicated `pick_reassigned` event and `POST /api/reassign` that carries ownership
only. Replay changes `team_slot` and nothing else, so player identity, stub identity, void state
and correction history all survive; `out_of_order` is recomputed, because "did this pick go to the
team on the clock" is a fact about the new owner. Simply dropping the `/api/correct` guard would
have cleared the pick's identity on the way through, which is the trap Codex flagged.

### 3. (HIGH → fixed) Decisions were not fail-closed end to end

`valuation/decisions.py`, `live_data.py`

Three problems that compounded:

- **An existing but empty decisions file returned no decisions.** A truncated write or an
  interrupted hand-edit silently un-applied every rejection Marc had made. Now only a *missing*
  path means "no decisions"; an existing empty file raises. The asymmetry is the safety property.
- **An omitted `player_id` was read as source-wide.** The old reasoning was sound — `null` is a
  meaningful value, so absence cannot be distinguished from a typo — but it resolved the ambiguity
  the expensive way round. This file is hand-editable by design, and a dropped line promoted one
  player's rejection into a rejection for every player that source publishes. `player_id` is now
  required; source-wide is written `"player_id": null`. `Decision.as_json` already emitted it, and
  a test now pins that round trip.
- **`_load_real_board_by_key` caught `DecisionsFileError` and degraded to placeholder mode**,
  defeating the deliberate decision to let it escape `build_real_board`. A truncated decisions file
  presented as "the cache is stale" rather than "your rejections stopped applying." It now
  propagates.

Codex agreed the uncaught-in-`build_real_board` design is correct; the broad wrapper downstream was
what defeated it.

### 4. (HIGH → fixed) An unavailable source could be selected and durably recorded as active

`sources.py`, `server.py`, `components/SourceToggle.tsx`, `types.ts`

`load_player_pool` degrades a failed board build to an ADP-placeholder pool, so `pool_for_source`
never raised: the switch "succeeded", the header said ESPN, a `source_changed` event recorded ESPN,
and a relaunch resumed that placeholder pool under the ESPN label. Picks were fine; **the record of
which board they were made against was a fiction.** `available_sources()` computed
`available: false` correctly, but `SourceInfo` in TypeScript dropped the field, so the UI rendered
every source as selectable.

Fixed with `SourceUnavailable` and `pool_for_source_strict`, used on the two paths that make a
source active (the toggle and the mid-draft resume) while `available_sources` keeps the lenient
accessor — its entire job is to describe broken sources without breaking. `available` is now in the
TS type and unavailable choices are disabled with the reason in the tooltip.

Also fixed the write ordering Codex flagged in the same finding: `switch_source` mutated the pool
and `active_source` *before* fsyncing `source_changed`, so a failed disk write left the running app
on one source while replay would rebuild another. The event is now appended first, after the pool
is built and validated. A test injects an append failure and asserts the served source did not move.

### 5. (MEDIUM → fixed) The source label went stale outside the window that changed it

`components/SourceToggle.tsx`, `components/Header.tsx`, `App.tsx`, `types.ts`

`SourceToggle` read the active source once from `/api/sources` and updated it only when that same
component made the change. The server had always sent `active_source` in the state payload;
`DraftState` didn't declare it. The toggle is now a controlled component driven by server state,
with the `/api/sources` value as a startup fallback only, and `choose()` no longer writes a local
copy from its own request argument.

### 6. (MEDIUM → fixed) Every click-anywhere pick was flagged out of order

`draft/state.py`

`out_of_order = team_slot is not None` was harmless while the only way to name a slot was the
explicit out-of-turn command. Click-anywhere drafting always sends a slot — the picker defaults to
whoever is on the clock — so every ordinary pick rendered an `OOO` badge. On a tool whose first job
is bookkeeping, a flag that fires on everything is worse than no flag.

`out_of_order` is now computed at replay from `team_slot != snake.slot_on_clock(teams, pick_no)`,
and the command layer no longer writes the flag into the payload at all, so the log cannot carry a
flag that contradicts its own numbers. Recomputing also corrects picks already recorded under the
old rule; a test hand-writes the pre-fix payload shape and asserts replay ignores it.

### 7. (MEDIUM → fixed) Review-queue impact numbers could restore earlier rejections

`valuation/candidates.py`, `valuation/decisions.py`

`ImpactEngine._blend` rebuilt each statline from the raw source lines with only the newly proposed
rejection, so `rejected=()` meant "nothing was ever rejected" rather than "the board as it is
today". Reviewing a second candidate for a player whose Sleeper number Marc had already thrown out
showed that Sleeper number **coming back**, and attributed the movement to the new decision. The
delta was real; the cause named on the page was not.

`ReviewInputs` now carries the standing `RejectedIndex` (read one call away from the board's own),
and `_blend` unions it with every hypothetical. Added `RejectedIndex.empty()` so callers never need
a null check. This one matters immediately: it is the arithmetic behind the two decisions currently
waiting in the queue.

### 8. (MEDIUM → fixed) The backtest's blend averaged Sleeper's constant games figure

`tools/backtest_sources.py`, `tools/check_renormalization.py`

The backtest's deliberately-local `blend_statlines` admitted every positive `games` value. Sleeper
publishes a blanket **18.0 for every player in 2025 as well as 2026** — one distinct value, so a
constant rather than a forecast — so Sleeper 18 and an ESPN 11-game projection became **14.5**,
exactly the information-destroying case production's `varying_games_sources()` exists to prevent.
The 17-week cap in `league_points` does not catch it, because 14.5 is already under the cap. This
sat in the tool whose numbers are the evidence for equal weighting.

Fixed by adding the same rule, measured rather than declared: `measure_games_variation()` computes
per-source distinct-games counts over the whole joined pool, and `games_varies` is a **required**
argument on the blend — no default, because a default would re-admit the constant the moment a
caller forgot. The report now prints the measurement
(`espn=yes, sleeper=NO (constant)`). `check_renormalization.py` uses the same blend and was fixed
with it.

**One process note worth keeping.** The first cut cached the measurement in a module global. The
suite went green, but only because of test ordering: the blend tests passed when some earlier test
had populated the global, and running one in isolation raised. A green suite that depends on test
order is worse than a red one, so the global was removed and the mask threaded explicitly through
all nine call sites.

**Both documented verdicts re-verified after the fix, and both hold unchanged:**

| Figure | Documented | Re-run 2026-08-21 |
|---|---|---|
| 2025 MAE, all 449 — Sleeper / ESPN / blend | 37.5 / 38.4 / 37.1 | 37.5 / 38.4 / 37.1 |
| blend vs ESPN | significant, p=0.018 | significant, p=0.018 |
| best-weight bootstrap interval | 0.30 – 1.00 | 0.30 – 1.00 |
| in-sample optimum worth | ≤0.22 pts/player | 0.08 (all) / 0.22 (ADP feed) |
| outcome spread (sd) | 86.5 | 86.5 |
| top-24 projection-to-outcome corr, Sleeper | −0.08 | −0.081 |
| renormalization: raw → identity → flat null | 37.14 → 36.04 vs 36.42 | 37.14 → 36.04 vs 36.42 |

The reason nothing moved: the season-total MAE tables that carry both verdicts score through
`score_statline` without the bonus term, and `games` only enters the bonus-scored and PPG views. So
the fix corrects the secondary views and leaves the primary evidence untouched. **No documented
conclusion was revised, and none needed to be.**

### 9. (MEDIUM → fixed) "Fill known names" could duplicate an assigned team, and bypassed the YAML

`components/TeamNamesPanel.tsx`

The panel hardcoded the ten 2026 league names even though the server already sends
`team_name_candidates` from `data/league_manual.yaml` — so editing the documented source of truth
changed nothing on screen, and the two could drift. It also filled blanks from the top of the list
without excluding names already assigned, so with "Country Club Boys" in slot 1 and slot 2 blank,
the one button whose job is to save typing put "Country Club Boys" in slot 2 as well.

Now reads `state.team_name_candidates`, subtracts names already in use, and surfaces a duplicate
warning (the server still accepts duplicates — a real league could have near-identical names — but
Marc should see it). The button disables itself, with the reason, when the server sends no
candidates.

---

## Deferred — one finding, and it is the important one

### (CRITICAL, deferred) Draft mode does not load a frozen, gate-passed snapshot

`server.py`, `live_data.py`, `validate/board.py`

`--draft` installs the socket guard, then `create_app()` rebuilds the board directly from whichever
raw/manual files are currently newest. There is no immutable snapshot load, and **no reconciliation,
crosswalk-completeness, or invariant result is checked before startup.** A partial prep refresh, or
caches from inconsistent timestamps, and `python -m draftroom.server --draft --my-slot 7` starts
anyway and values that data. It can even start in placeholder mode.

This is a real gap against this project's own stated architecture — `CLAUDE.md` says draft phase
"opens the newest snapshot **read-only**, refuses to start if the reconciliation gate failed."
Codex is right that the code does not do that.

**Why it is deferred rather than fixed here:**

1. **It is not a regression from this batch.** The snapshot layer has never existed — there is no
   `snapshot.py`, and no commit ever added one. This batch did not make it worse.
2. **It is a design task, not a fix.** Doing it properly means deciding what a snapshot contains
   (resolved pools, all five source boards, config, gate results), how it is written and versioned,
   and what "abort" looks like on draft night when the alternative is no tool at all. That is a
   batch of its own and it wants Marc's input, particularly on the refuse-to-start policy.
3. **Deferring does not make anything worse than the current `origin/main`.** The working tree is
   strictly better than what is committed; leaving it uncommitted to preserve a snapshot gap that
   exists in both is the wrong trade.

**This is the top recommended next batch, and it has a deadline: the draft is 2026-09-08.** It also
composes with the already-open task of re-running prep close to the draft — a snapshot is the
natural artifact for that run to freeze.

## Checked and dismissed

- **The same constant-games exposure in `tools/check_envelopes.py` and
  `tools/check_td_regression.py`.** Both call production's `blend_statlines` without
  `games_sources`, which does mean the blended `games` figure admits Sleeper's constant. But
  neither tool reads `games` anywhere (0 references in each), so nothing consumes the corrupted
  value. Theoretical, not a defect. Noted here so it is not re-derived.
- **Offline guarantee.** Codex found no network call reachable from source switching or board
  construction; the FantasySharks fetch functions are prep-only and the socket guard installs
  before `create_app()`. Its one offline-related complaint is finding 1 (the missing snapshot gate),
  not network isolation.
- **Security.** No path traversal and no served-payload injection; the review HTML escapes source
  text consistently. Codex explicitly reported nothing at LOW severity rather than padding.
- **Composite missing-vs-zero handling** — the question the prompt asked first. Confirmed correct:
  a missing source is excluded from `offered`, a structurally unpublished stat is skipped, and a
  published zero still contributes its denominator. Position-specific publish tables are in use
  because `build_real_board` passes `pos`.

## Caveat on the review itself

Codex ran read-only and **could not execute the tests** — the repo venv points at a Python 3.12
executable it could not resolve — so its review is static and it said so rather than implying a
green suite. The gates below were run here.

## Gates

- `pytest -q` — **763 passed** (745 before; +18 net from the new tests and the rewrites)
- `tools/run_invariants.py` — **8/8 PASS**
- `cd frontend && npm run build` — clean, exit 0
- `tools/backtest_sources.py` — exit 0, verdict table above
- `tools/check_renormalization.py` — exit 0, verdict unchanged
