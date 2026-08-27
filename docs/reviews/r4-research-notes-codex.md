# Codex review — round 4, research notes (ledger #10)

**Date:** 2026-08-27 · **Verdict on arrival: "Do not ship."** 3 × P1, 1 × P2. **All four fixed,
none deferred.** Suite 793 → **834**, invariant gate 8/8, verified on a fresh server process
(PID started 10:10:04, bundle `index-CEUSOFGz.js`).

Codex earned its place again. Two of the three P1s were failure modes I had reasoned about in the
module docstring and then not actually defended against, which is the most dangerous shape a bug
can take here: the comment says the right thing, so nobody re-checks the code.

---

## P1 — `NaN` and `Infinity` pass validation and become a zero-games override

`json.loads` **accepts** `NaN`, `Infinity` and `-Infinity`, and none of them satisfies `raw < 0`,
because every comparison against `NaN` is `False`. A `NaN` therefore parsed cleanly, reached
`max(0.0, weeks - NaN)` in the sweep, and that returns **`0.0`** — so `injury_sweep.py --apply`
would have written a **zero-games override for a healthy player**, zeroing his value on the board
with a badge claiming a human decided it.

This is the worst thing this file could do, and it needed one line: `math.isfinite`.

Fixed in `injury_research.py`, pinned by four parametrized cases written through a real file
(a dict literal cannot carry `NaN` in from JSON, so testing the parser alone would have missed it).

## P1 — the loader wrapped only `JSONDecodeError`, so two real failures fell open

`load_research` caught bad JSON and nothing else. Two ways a present file fails to read escaped as
generic exceptions, landed in `live_data`'s broad `except Exception`, and **degraded the app to
ADP-placeholder mode with `/healthz` still returning 200**:

- an interrupted write ending inside a multibyte character → `UnicodeDecodeError`
- a locked or permission-denied file → `OSError`

That is exactly the failure the fail-closed rule exists to prevent, and it reads as "the cache is
stale" rather than "your researched findings stopped being shown". It has now been fixed once per
decision file — decisions (Codex 2026-08-21), playing time (Codex 2026-08-24), research (here).
**The pattern is the finding**: three modules, three independent regressions of the same rule.

Now wrapped: `OSError`, `UnicodeError`, and an explicit whitespace-only check, since a zero-byte
file is the classic truncated write and deserves a sentence rather than a parser error.

## P1 — research joined by id with no name check, so a valid-but-wrong id binds silently

An entry naming Puka Nacua but carrying Josh Jacobs' valid id `5850` would cleanly put Nacua's
risk badge on Jacobs' row and leave Nacua unbadged. Nothing would say so.

**Implemented differently from what Codex prescribed, deliberately.** Codex asked for a raise. The
board already runs this exact check for playing-time overrides ten lines above and makes it a
**warning**, with a stated reason: *"names legitimately differ on suffixes and punctuation, so
refusing the build would make the file brittle."* That reasoning applies here unchanged, and two
things make a warning more defensible here rather than less:

- a research note **moves no number at all**, where an override moves a real one
- `player_name` now travels in the payload, so the badge itself flags a mismatch (`RISK?!` plus a
  loud tooltip) rather than the mismatch living only in a log nobody reads in a room

A file that refuses to build the board over a punctuation difference gets edited around, which is
worse than a loud line. The check exists now; the severity matches its sibling.

## P2 — the declared `schema` version was ignored

A file declaring an unsupported schema still loaded under schema-1 assumptions. `SCHEMA_VERSION`
added and enforced, matching `playing_time.py`.

---

## What Codex explicitly cleared

> The core `null` handling is otherwise correct: `None` survives parsing, all sweep comparisons are
> guarded by `is_unpriced`, apply cannot write an unpriced override, and React distinguishes it
> with `=== null`. I found no new network call, and the numbered-finding sweep path otherwise
> preserves its prior arithmetic.

That last clause was the thing I most wanted checked: the diff deletes ~100 lines of parser from
`tools/injury_sweep.py` and rewires it to the new module, and a regression there would stay
invisible until the final-prep run on 2026-09-06.

## Note

Codex could not run pytest — it reported the venv's `home` pointing at a missing interpreter. The
venv works fine from PowerShell (834 passed), so this is a sandbox artifact, not a repo problem.
Worth knowing so the next reviewer does not chase it.
