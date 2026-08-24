# Codex review triage — `playing-time-overrides` (2026-08-24)

Raw output: `reviews/_playing-time-overrides_raw_output.txt` · prompt:
`reviews/_playing-time-overrides_prompt.txt` · exit 0.

Verdict as delivered: **"Do not ship as-is"** — 1 blocker, 3 should-fix, 0 nits. **All four were
verified against the working tree and all four were fixed.** Nothing deferred.

Codex could not run pytest (its sandbox venv points at a missing Python 3.12), so every finding
came from static tracing. All four were nonetheless real, and one of them was a genuine blocker
that the 50 tests in the batch did not catch. Worth remembering: a static reader beat the test
suite here because the suite tested the wrong LAYER, not the wrong logic.

---

## 1 · BLOCKER — `PlayingTimeFileError` swallowed by the live-board fallback · FIXED

**Claim.** `live_data._real_board_enrichment` re-raises only `DecisionsFileError`; the broad
`except Exception` immediately below turns everything else into `{}` = ADP-placeholder mode. So a
truncated `data/playing_time.json` raised correctly out of `build_real_board`, got swallowed one
layer up, and draft mode booted with `/healthz` at 200 on placeholder values.

**Verified.** Read `live_data.py:255` and `server.py:873`. Exactly as described. This is the same
bug that was found and fixed for the decisions file on 2026-08-21 (Codex finding 4 of that
round) and reintroduced here by adding a second exception type and not adding it to the handler.

**Why it matters more than it looks.** The stated fail-closed contract is not "the board build
raises", it is "the draft never runs on numbers that quietly ignore Marc's judgement". Placeholder
mode reads as *"the cache is stale"*, which is a completely different diagnosis from *"your
availability judgements stopped applying"* — and on draft night, with wifi off and a room full of
people, the wrong diagnosis is the whole cost.

**Fix.** `except (DecisionsFileError, PlayingTimeFileError): raise`, with a comment stating that
every human-decision file gets this treatment and a fifth one belongs in the tuple on day one.

**Test.** `test_a_malformed_overrides_file_also_fails_the_LIVE_POOL_not_just_the_board` goes
through `live_data.load_player_pool()`. The pre-existing test stopped at `build_real_board()`,
which is exactly why it passed while the bug was live.

---

## 2 · SHOULD-FIX — the `expected_games=None` counterfactual produced false "moved" badges · FIXED

**Claim.** `board.py` passed `None` as `Binding.was` for a source with no games column, and
`Binding.moved` treated `was is None` as unconditionally moved. So an override of 99 on a
FantasyPros board clamps to the curve, leaves EVoB byte-identical, and is still badged and pulled
out of the injury queue. The numeric counterfactual is `curve`, not `None`.

**Verified.** Correct, and confirmed against the repo's own existing convention: the invariant
docstring and `candidates.effective_games_by_pid` both already treat "no explicit
`expected_games`" as "the curve value". So the curve *is* the no-override figure for those
players, and my `None` was inventing a third state.

Notable: this is the **second** version of the same mistake in one batch. The first (comparing
against the raw source figure rather than the capped one) I caught myself via a failing test
during the build. Both had the identical symptom — a badge for a change that never happened —
which suggests the real lesson is that "what would the number have been?" deserved a named,
tested function from the start rather than being computed inline at the call site.

**Fix.** `bind` resolves `source_games=None` to `curve`; `was` is now always a real number;
`moved` is a pure numeric comparison. Provenance moved to its own field,
`source_published_games`, which drives the tooltip wording and never affects `moved`. Payload,
`types.ts` and the badge updated accordingly (`was: number`, not `number | null`).

**Tests.** `test_a_clamped_override_on_a_source_with_no_games_moved_nothing`,
`test_a_source_with_no_games_column_falls_back_to_the_curve_as_the_counterfactual`,
`test_source_published_games_distinguishes_the_two_ways_of_reaching_the_same_figure`.

---

## 3 · SHOULD-FIX — any prior override could hide a new or unrelated designation · FIXED

**Claim.** The detector skipped every player with an applied override, without checking what
designation the override was written for. Record 12 games for a suspension; a later refresh marks
the player IR; the row never fires. Also, `settled_by_override` reported healthy-player overrides
as vanished injury rows.

**Verified.** Both halves correct. My `settled` set was `set(applied_playing_time)` with no
reference to the designation at all.

**This is the most dangerous of the four**, and it is worth saying why plainly: a player carrying
an override but *no row* reads as "somebody looked at this". A stale override therefore does not
merely fail to help — it actively suppresses the signal that would have prompted a second look,
which is worse than never having built the suppression.

**Fix.** Suppression is scoped to the designation: skip only when the override's recorded
`designation` matches the player's current one. A mismatch leaves the row standing and the reason
now states that an override is in force and which designation it was written for. An override
recording **no** designation answers none of them and never suppresses — this repo does not guess
what a human meant. `settled_by_override` is built from the identical predicate, so the report and
the suppression cannot drift apart.

**Tests.** `test_an_override_for_a_DIFFERENT_designation_does_not_suppress_the_row`,
`test_an_override_with_no_recorded_designation_never_suppresses`,
`test_a_healthy_player_override_is_never_reported_as_a_settled_injury_row`. The pre-existing
settled-by-override test was updated to record a matching designation, which is now load-bearing
rather than decorative.

---

## 4 · SHOULD-FIX — malformed identity and audit fields coerced instead of rejected · FIXED

**Claim.** Emptiness was checked before stripping, so `"   "` and `" null "` became accepted ids;
and `str(None)` is the nonempty string `"None"`, so `"reason": null` / `"date": null` passed the
emptiness check and applied a valuation change with an unusable audit trail.

**Verified.** Both correct. The whitespace-id case is the nastier one: it does not raise, it
degrades to an unmatched-override warning, so the judgement is simply never applied while the
file looks fine.

**Fix.** Normalize first, then validate. `player_id` must be a string or integer (a float `8142.0`
stringifies to `"8142.0"` and matches nothing) and must not normalize to empty/`null`/`none`.
`reason` and `date` must be actual `str`. A literal `null` `player_id` is checked *before* the
type test so it gets the error that explains why it has no meaning here — copying a source-wide
line out of `projection_decisions.json` is the likeliest way to write this file wrong, and the
error message is the only place that can say so.

**Also adopted** Codex's optional suggestion about a mistargeted-but-valid id, which is the one
failure the loader structurally cannot see (it has no board). The board build now compares
`player_name` against the board's name for that id, via the crosswalk's existing
`normalize_name` rather than a second normalizer, and logs a warning. Warning and not error:
names legitimately differ on suffixes and punctuation, and refusing the build would make the file
brittle for no safety gain. Test: `test_a_name_that_disagrees_with_the_id_is_flagged`.

---

## Codex's requested checks — all confirmed clean

- All callers of `_cap_expected_games_by_curve` updated for the tuple return (2 production in
  `candidates.py`, 3 in tests).
- Non-overridden behaviour unchanged, including `None` and zero. Independently re-verified by a
  `git stash` A/B: with no overrides file, the board is **bit-identical to HEAD** — 188 players,
  0 draft values differing. Re-run after these four fixes; still 0.
- `ImpactEngine` re-application is idempotent (the override is already curve-clamped, so
  `min(override, cap)` a second time is a no-op).
- `RealBoard`'s new fields are appended last; `PoolPlayer.playing_time` likewise.

## Deferred

None.
