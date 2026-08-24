# Playing-time overrides

The one place a human can set a player's expected games. Built 2026-08-24 to close the gap
`injury_vs_expected_games` could only complain about.

## The gap this closes

`injury_status` was carried on `PoolPlayer` and rendered as a badge, and it touched **nothing**
in the valuation. So whether a will-not-play designation reached `expected_games` depended
entirely on whether ESPN happened to price it in, which is accidental rather than principled.
Measured on the ranked pool 2026-08-20:

| Player | Designation | ADP | ESPN games | Board credited | Healthy-rank curve |
|---|---|---|---|---|---|
| Alec Pierce (WR IND) | PUP | 70.3 | **17.0** | **15.50** | 15.50 (WR30) |
| Brandon Aiyuk (WR SF) | DNR | 148.7 | — | **8.46** | 8.46 (WR78) |
| George Kittle (TE SF) | PUP | — | 15.0 | discounted | — |
| Zach Charbonnet (RB SEA) | PUP | — | 11.0 | discounted | — |

Kittle and Charbonnet came out right. Pierce and Aiyuk got the figure for a player about whom
nothing player-specific is known, at ADPs where that matters. Ricky Pearsall (IR) came out right
only by luck: he is off the board because Sleeper happened to zero his stat line, not because
anything read his status.

The review queue could see all of this and could do nothing about it, which is why those rows are
marked `actionable: false`. Its only lever is `blend_statlines(rejected=...)`, and **rejecting a
source cannot change an availability figure**. No source is even at fault: ESPN's 17.0 is an
ordinary if-healthy projection, and "projections are not expectations" is a measured finding in
this repo. The missing lever was a playing-time file. This is it.

## The file

`data/playing_time.json`. Modelled on `data/projection_decisions.json`: checked first, permanent,
auditable, hand-editable without running anything.

```json
{
  "schema": 1,
  "overrides": [
    {"player_id": "8142", "player_name": "Alec Pierce", "games": 11.0,
     "designation": "PUP",
     "reason": "not expected back before ~week 5",
     "date": "2026-08-24"}
  ]
}
```

Required: `player_id`, `games`, `reason`, `date`. Optional: `player_name` (human-facing only,
never used for matching) and `designation` (audit trail, never affects the number). A bare list
is accepted too, because that is what a hand-edit eventually looks like.

**`player_id` is never null.** `decisions.py` gives `null` a documented meaning — the decision
applies source-wide. Availability has no such grain: it is a fact about one player. The same
shape here is a mistake, so it is refused rather than reinterpreted.

## The rule

```
expected_games = min(the human's figure, curve(pos, rank-by-ppg))
```

The override **replaces** whatever games figure the active source published — including the
`None` that FantasyPros and FantasySharks leave behind, which makes an override the only thing
that can turn an implicit "let the fitted prior decide" into an explicit number. The same
rank-conditional availability curve that already caps every other player then clamps it.

The clamp is load-bearing in both directions, and it is derived rather than chosen:

- **Downward passes straight through.** Bad news is the point. An override of 11 for a PUP player
  lands at 11, because the curve's 15.50 was never a claim about *him*.
- **Upward stops at the curve.** The curve is fitted actual availability at that positional rank.
  You can say "he is fully cleared, ignore the source's 11" and get him back to the healthy-rank
  figure. You cannot push him past it, because that is claiming better-than-typical durability
  for a rank on the strength of a press report — and it is the one direction whose error is
  expensive, since it inflates a player you would then draft at full value.

The clamp is also why this feature **weakens no gate to admit itself**.
`check_expected_games_capped_by_curve` stays true by construction rather than by exemption. An
override mechanism that had to loosen an invariant would be indistinguishable from the bug that
invariant exists to catch. Pinned in `tests/test_playing_time_wiring.py`.

**PPG is never touched.** This moves the games VOLUME a per-game rate is credited for, exactly as
the curve cap already did. A view that a player will be *worse per game* is a projection question
and belongs in the review queue.

## No threshold is invented, here or anywhere

Nothing in this repo asserts what a PUP/IR/DNR designation costs in games, and this file does not
change that. The reason is in `candidates.NO_EMPIRICAL_DESIGNATION_FIT`: Sleeper's designation is
current-year while the only per-player games history in the cache is 2025 actuals, so **no
games-missed figure is derivable from this repo's data**. Asserting one would be exactly the
arbitrary rule the null-test standard exists to stop.

So the loader validates only that `games` is a non-negative real number. It deliberately enforces
**no maximum** — the fitted curve supplies the ceiling per player, and a second hardcoded ceiling
would be a number nobody derived.

## Fails closed

Same asymmetry as `decisions.load_decisions`, for the same reason:

- **Missing file** → no overrides. The ordinary state; the board builds normally.
- **Present but empty, or malformed** → `PlayingTimeFileError`, naming the offending entry, and
  it propagates through the board build uncaught.

Every other optional input in `validate/board.py` degrades to "this source contributes nothing",
because a missing FantasyPros CSV has nothing to do with whether the board is sound. A bad
overrides file is the opposite: degrading would silently stop applying a judgement about a player
Marc knows something about, and the board would look fine while ignoring him.

## What you see on the board

A `NN.NG` badge next to the player's name, in the accent colour rather than REJ's red — a
different kind of decision, so a different badge. The tooltip states the reason, the date, what
the pipeline would have used, and whether the curve clamped the figure.

**Only overrides that actually MOVED a number are badged.** Same rule as REJ. An override the
curve clamped back to the figure the board already had changed nothing, and a badge on it would
point at a decision that did nothing. Those are still loaded, still on file, and logged at
WARNING by the board build — an inert override is usually a sign the note is not doing what its
author thinks.

The `was` figure behind that comparison is the **no-override counterfactual**, and it took two
goes to get right. Both mistakes produced a badge for a change that never happened:

- Comparing against the **raw** source figure instead of the already-capped one. Josh Allen's
  source says 17.0 against a 16.6 curve, so an absurd override clamped to 16.6 read as
  "17.0 → 16.6". Caught by its own test during the build.
- Carrying **`None`** through for a source with no games column, and treating that as
  unconditionally "moved". For those players the fitted prior supplies the volume, so the
  **curve is** their no-override figure: an override of 99 on a FantasyPros board clamps to the
  curve, leaves EVoB byte-identical, and was still badged and pulled out of the injury queue.
  Caught by Codex, finding 2. `was` is now always a real number, and whether it came from a
  source or from the prior lives in its own field, `source_published_games`.

## An overridden player leaves the review queue, but only for the designation he answered

Once a player has an override that moved his number, `injury_vs_expected_games` stops firing for
him. The gap it exists to surface is closed for that player: the board's figure is now a human
judgement rather than an unexamined default, which is the only thing the check was complaining
about. Handing the decision back as a fresh candidate would be noise, and the `hygiene` wording
("a source did price in N games") would be actively false — no source did.

**The suppression is scoped to the designation the override recorded**, and that scoping is the
subtle part. Suppressing on the mere existence of an override let a figure written for one
situation silently absorb a later, different one: record 12 games for a suspension, then have a
refresh mark the player IR, and he sits on the board at a stale number with no row asking about
it. That is the most expensive thing this detector could get wrong, because an overridden player
with no row reads as "somebody looked at this". Codex found it (finding 3).

So: the row is suppressed only when the override's recorded `designation` matches the player's
current one. A mismatch leaves the row standing, and the reason says an override is in force and
which designation it was written for. An override that recorded **no** designation answers none
of them and never suppresses — this repo does not guess what a human meant.

The disappearance is **reported, not silent**: suppressed players land in
`ReviewQueue.settled_by_override`, with a note in the queue and a line in the HTML page. That map
is intersected with currently-designated players for the same reason, or an override on a
perfectly healthy player would be reported as a vanished injury row.

## Where it lives

| Path | Role |
|---|---|
| `backend/draftroom/valuation/playing_time.py` | the file format, the fail-closed loader, `bind()` |
| `backend/draftroom/validate/board.py` | loads it; applies it inside `_cap_expected_games_by_curve` |
| `backend/draftroom/valuation/candidates.py` | `ImpactEngine` carries it through counterfactuals; the injury detector defers to it |
| `backend/draftroom/live_data.py` | `_playing_time_payload` → `PoolPlayer.playing_time` |
| `backend/draftroom/server.py` | the board row's `playing_time` key |
| `frontend/src/components/TierBoard.tsx` | `PlayingTimeBadge` |
| `tests/test_playing_time.py` | the file and the clamp rule in isolation (53 tests) |
| `tests/test_playing_time_wiring.py` | the real cached board, end to end (16 tests) |

`ImpactEngine` needs the overrides in both the baseline and every counterfactual. The baseline
seasons already carry the applied figure (re-applying is idempotent, since the override is
already curve-clamped), but `_rebuild_season` derives `expected_games` from the statline and
would otherwise hand an overridden player his source's figure back mid-recomputation, making the
review queue's impact column wrong for exactly the players a human has looked at.

## Fails closed AT EVERY LAYER, not just the board build

`build_real_board` letting `PlayingTimeFileError` escape is necessary but not sufficient.
`live_data._real_board_enrichment` wraps that call in a broad `except Exception` that degrades to
ADP-placeholder mode, and it re-raised only `DecisionsFileError` — so a truncated overrides file
raised correctly, got swallowed one layer up, and draft mode booted on placeholder values with
`/healthz` returning 200. The failure would have read as "the cache is stale" rather than "your
availability judgements stopped applying". Codex found it as the batch blocker (finding 1); it is
the same bug that had already been fixed once for the decisions file and then reintroduced here.

Both exceptions are now re-raised together, and the regression test goes through
`load_player_pool()` rather than stopping at `build_real_board()` — testing only the board is
precisely what let this through. **If a fifth human-decision file is ever added, it belongs in
that tuple on day one.**

## Validation rejects, never coerces

Two shapes that a hand-edited file produces, both of which used to pass and then quietly do
nothing or do something unauditable (Codex finding 4):

- `"player_id": "   "` — emptiness was checked *before* stripping, so whitespace became an id,
  matched no board player, and degraded to an unmatched-override warning. The judgement was
  never applied. Non-scalar ids are refused too: a float `8142.0` stringifies to `"8142.0"` and
  matches nothing.
- `"reason": null` — `str(None)` is the **nonempty** string `"None"`, so a null passed the
  emptiness check and applied a valuation change with an unusable audit trail. `reason` and
  `date` must now be actual strings.

A literal `null` `player_id` is checked before the type test, so it gets the error that explains
*why* it has no meaning here rather than a generic type complaint. Copying a source-wide line out
of `projection_decisions.json` is the likeliest way to write this file wrong.

The one thing the loader cannot catch is a **valid id pointing at the wrong player** — it applies
cleanly and badges cleanly. The board build catches that, because names are in hand there: a
`player_name` that disagrees with the board's name for that id is logged loudly. A warning rather
than an error, since names legitimately differ on suffixes and punctuation, and refusing the
build would make the file brittle.
