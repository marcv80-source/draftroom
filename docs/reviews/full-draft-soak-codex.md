# Codex review triage — `full-draft-soak` (2026-08-24)

Raw output: `reviews/_full-draft-soak_raw_output.txt` · prompt:
`reviews/_full-draft-soak_prompt.txt` · exit 0.

Verdict as delivered: **"request changes"** — 1 blocker, 2 should-fix, 1 nit. **All four were
verified and all four were fixed.** Nothing deferred.

This was a review of a TEST-ONLY change, and it earned its keep. The review of a test file is
easy to treat as a formality; here it caught a defect that would have made the whole file
worthless outside this one machine, and a second that would have let the file pass while checking
far less than it claimed.

---

## 1 · BLOCKER — all 13 tests could skip in a clean checkout · FIXED

**Claim.** The module-scoped pool fixture skipped when the cache was absent, `data/raw/` is
gitignored, and every test depended on that fixture. A fresh clone or CI runner would report
`13 skipped`, exit 0, and leave the coverage gap wide open while looking closed.

**Verified.** Correct, and worse than it sounds. The file's entire reason for existing is to close
a coverage gap; a version of it that silently evaporates on any machine but Marc's closes nothing
and actively misleads, because "846 tests pass" would now include a soak that never ran. This is
the same failure mode the repo already refuses everywhere else — `settled_by_override`,
`suppressed_by_injury`, the draft-log startup announcement — all exist because *a suppression
nobody can see is indistinguishable from a detector that stopped working*. I applied that
standard to the product and not to the test.

**Fix.** The bookkeeping tests now build their own deterministic 265-player two-tier pool and
**cannot skip**. This is the right split on the merits, not just a workaround: pick mechanics, the
clock, the event log and replay have nothing to do with whether a projection is any good — they
need enough PLAYERS, not real ones. `LeagueConfig.from_yaml()` is still the real 10-team league,
because `data/league_manual.yaml` is tracked.

The real cached pool survives in ONE additive test at the bottom, for end-to-end confidence with
real data. It may skip, and nothing the file guarantees depends on it.

Codex's narrower skip points were also all taken:
- "no unranked players" can no longer skip — the synthetic pool is built with them, and the test
  asserts they exist.
- An empty or tiny loaded cache now **fails** rather than skipping: that is a broken cache, not an
  absent one, and skipping there would hide a data-layer regression. Only `FileNotFoundError`
  skips.
- The last remaining skip is "only one projection source offered", which is inherent to the
  source-toggle test and cannot be manufactured.

---

## 2 · SHOULD-FIX — `_assert_consistent` did not enforce what it advertised · FIXED

**Claim.** The helper checked the clock, `all_picks`, duplicate ids, count, gaps and pick numbers,
but never reconciled live picks against `tier_board` rows or the `opponents` rosters. The plain
soak checked only the player just drafted. And the final "full roster" assertion was computed from
`taken` — the test's own record of what it requested — rather than from the server payload.

**Verified.** All correct. The roster point is the sharpest: computing the expected rosters from
my own request list and then asserting they are full is the test grading its own homework. It
would pass no matter what the server said.

**Fix.** `_assert_consistent` now reconciles the whole board on every call:
- every live pick against its board row, `drafted` **and** `owner_team_slot`;
- the reverse direction too — no row may claim to be drafted with no live pick behind it, which is
  the check that catches a void that failed to free its player;
- `out_of_order == (team_slot != slot_on_clock(...))` for **every** live pick, not only the ones
  deliberately misfiled;
- rosters counted from the server's own `opponents` payload.

**This was measurably not cosmetic.** Re-running the two mutation tests against the stricter
helper: the `out_of_order` bug went from failing **1 test to failing 12**, because the flag is now
checked on every pick on every call rather than once in one dedicated test.

**And writing it strictly surfaced a real property of the system.** The first strict version failed
the source-toggle test with "pick 1 names a player absent from the board" — because a source
change REBUILDS the pool, so a player drafted under the old board legitimately has no row on the
new one. The picks are unaffected (`all_picks` is replayed from the event log, not read off the
pool), which is exactly why bookkeeping survives a mid-draft source change. So invariant 3 is
conditional on a constant pool, and that is now explicit: `allow_unpooled=True` is passed by the
source-toggle test and nowhere else, and the tests that do not pass it additionally assert the
skip count is **zero**, so the flag cannot quietly hollow out the check.

---

## 3 · SHOULD-FIX — the source-toggle test proved less than its name claimed · FIXED

**Claim.** After switching, the test never asserted `active_source` changed, never checked the
logged event's key, and relaunched with an injected pool (which ignores the logged source). So it
would pass against an `/api/source` that appended an event and switched nothing, or that recorded
the wrong key entirely.

**Verified.** Correct on every point. My docstring's claim that it proved "the toggle itself is
durable" was too strong, exactly as Codex said.

**Fix.** It now asserts `payload["active_source"] == chosen`, that exactly one `source_changed`
event was logged, and that the event carries the requested key. Renamed to
`test_a_source_change_can_be_interleaved_without_damaging_the_draft`, which is what it actually
establishes, and the docstring now states plainly that the board-rebuild half is out of scope
because an injected pool always wins over the log.

**Codex also corrected my cross-reference and was right:** resume rebuilding lives in
`tests/test_server.py` (`test_create_app_resumes_logged_source`,
`test_source_changed_is_fsynced_before_the_served_pool_moves`,
`test_injected_pool_never_resumes_from_log`), not `tests/test_sources.py`. Verified before copying.

---

## 4 · NIT — "cold replay equals the live board exactly" overstated it · FIXED

**Claim.** The comparison covered clock, count, player id and team slot. Replay could reconstruct
wrong `out_of_order` flags and the "exact" claim would still pass.

**Verified.** Correct, and the flag is the one to worry about precisely because replay **derives**
`out_of_order` from `(team_slot, pick_no)` rather than reading it out of the event — so it is the
field most able to come back wrong while identity looks fine.

**Fix.** Rather than soften the wording, I widened the comparison: `_core_pick_state` now includes
`voided` and `out_of_order` alongside player and owner, on both sides. The prose says "core pick
state" and then lists exactly what that means.

---

## Codex's requested challenges — its answers, checked

- **`_best_available` realism:** agreed adequate; it deterministically consumes 150 of ~240 ranked
  players. Its suggestion to add fixed late-round unranked picks was **taken** — the interleaved
  soak now drafts unranked players at picks 131 and 145.
- **Shared module fixture:** agreed safe. `PoolPlayer` instances are shared but each app gets its
  own list, and board construction only reads them. No deep copy needed.
- **Payload brittleness:** agreed the asserted fields are real API/UI contracts, and the raw JSONL
  event type/count is an intentional durability contract.
- **Determinism:** agreed the fixed perturbation schedule is right, and that coverage should grow
  by adding fixed cases rather than by introducing randomness.

## Note on the review environment

Codex could not run the suite (its sandbox venv points at a missing Python) and had no `git`, so
every finding came from static tracing — the same limitation as the previous round, and the second
time in a row that a static reader beat a green suite. Both times for the same underlying reason:
the tests were checking the wrong LAYER, which no amount of running them reveals.

## Deferred

None.
