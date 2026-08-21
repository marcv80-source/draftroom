# FantasySharks: endpoint, field mapping, independence verdict

Adapter: `backend/draftroom/prep/fantasysharks_client.py`
Verification: `tools/verify_fantasysharks.py`
Tests: `tests/test_fantasysharks_client.py` (49 tests, no network)
Fixture: `tests/fixtures/fantasysharks/projections_trimmed.json`

Everything below was measured live on **2026-08-20**. Authorised by "FantasySharks: add it" in
`docs/PLAN_2026-08-20.md`.

**Status: independent, adapter built and verified, and WIRED IN on 2026-08-20.** It is the
fourth family in `valuation/composite.COMPOSITE_SOURCES`, the fourth in
`valuation/disagreement.INDEPENDENT_SOURCES`, a board key (`sources.SOURCE_KEYS`), and the
external reference behind `tools/validate_bonus_vs_sharks.py`. See the checklist at the bottom
for the one item still open (`prep/fetch_all.py`).

---

## Why this source, and not just a fourth vote

`CLAUDE.md` is blunt that a fourth *correlated* source adds little to an average. Two specific
gains justify this one, and they are the two things to check are still true if it is ever
re-evaluated:

1. **Targets go from one source to two.** `rec_tgt` came from ESPN alone, so the composite could
   not average it and the team-envelope validator had nothing to cross-check it against.
   FantasySharks publishes a `Tgt` column for RB, WR and TE: **427 of 516 players** carry a
   nonzero projected target figure, against ESPN's ~360. Going 1 -> 2 on a stat is a different
   move from going 3 -> 4 on an average.
2. **Projected counts of games clearing each yardage threshold.** This is exactly what
   `valuation/bonuses.py` estimates from fitted hit-rate curves, and it previously had **no
   external reference of any kind**. Coverage is partial and the detail matters - see
   [Bonus threshold coverage](#bonus-threshold-coverage).

---

## Endpoint

```
GET https://www.fantasysharks.com/apps/bert/forecasts/projections.php
    ?League=-1&Position=<N>&scoring=18&Segment=<S>&uid=4
```

Free, no auth, no key. Returns server-rendered HTML (no JS shell), ~200-350 KB per position.
Four requests cover the whole offensive universe; the adapter makes exactly four, reusing the
bootstrap page for the first position.

### `Segment` changes every year and is never hardcoded

`Segment` is FantasySharks' internal period id and carries no meaning in the number. On
2026-08-20 the `<select name="Segment">` served:

| value | label |
|---|---|
| **874** | **2026 NFL Season** |
| 877 | 2026 Rest of Year |
| 878 | 2026 Playoffs |
| 883-901 | Week 1 - Week 18, then the playoff rounds |
| 906 | 2027 NFL Season |
| 938 / 970 | 2028 / 2029 NFL Season |
| 99200 | Rest of Career |

A hardcoded `874` would, a year later, quietly serve a prior season's projections: plausible
numbers, wrong year, and nothing downstream could catch it. `discover_segment()` therefore
fetches without a `Segment` parameter, reads the page's own select, and matches the option
labelled **exactly** `"<season> NFL Season"`. Substring matching is not enough - three other
options mention 2026 and all three are partial periods. No option for the requested season
raises `SegmentNotFoundError`, listing every option it did see.

`tests/test_fantasysharks_client.py::test_segment_is_read_from_the_page_not_hardcoded` parses the
module's AST and fails if `874` ever appears as a literal constant (prose in a docstring is fine,
a value is not).

### `Position=4` is WIDE RECEIVER

The ids are not 1/2/3/4. Confirmed against the returned player names, not read off a table:

| id | label | top rows returned | count |
|---|---|---|---|
| 1 | Quarterback | Josh Allen, Lamar Jackson | 78 |
| 2 | Running Back | Jahmyr Gibbs, Bijan Robinson | 136 |
| **4** | **Wide Receiver** | Ja'Marr Chase, Puka Nacua | 187 |
| 5 | Tight End | Trey McBride, Brock Bowers | 115 |

Id 3 is not a skill position in their numbering. Every fetch re-checks the `selected` option of
`<select name="Position">` against the position requested, so a renumbering upstream fails the
fetch rather than mislabelling 187 receivers.

**516 offensive players total** (78/136/187/115). The scouting note said 583 (88/154/212/129);
the live tables are smaller. Counts are reported by the tool, not asserted anywhere.

### `scoring=18`

A FantasySharks scoring preset. It changes only the page's "Points Awarded" row and the `Pts`
column, **both of which this adapter discards**. It does not move a single component-stat
projection.

---

## Served-HTML artifacts, all handled

| Artifact | Handling |
|---|---|
| Header row repeats every 16 data rows | Skipped, but **validated against the layout first** - a mismatch raises `ColumnLayoutError` |
| A `"Points Awarded"` scoring row after each header | Skipped and never ingested. It is points-per-unit under a foreign preset |
| Rookies marked with a `<sup>R</sup>` **tag** on the player link | Read as a separate tag and exposed as `FantasySharksRow.rookie` (55 of 516 rows). Naive tag-stripping concatenates it and produces "Mendoza, FernandoR" |
| Names are `"Last, First"` | Reversed on the first comma only, so `"Washington Jr., Mike"` -> `"Mike Washington Jr."` and `"Gore Jr., Frank"` -> `"Frank Gore Jr."` |
| Team codes are FantasySharks' own | Mapped by `FS_TEAM_MAP`. Nine differ from the Sleeper spine: `GBP->GB, JAC->JAX, KCC->KC, LVR->LV, NEP->NE, NOS->NO, SFO->SF, TBB->TB`. An unmapped code **raises** - a blank team downgrades the crosswalk join to name+position and can pick the wrong player of a shared name |
| Player ids in `playerpage.php?id=NNNNN` | Used as `source_key`. They appear in **no** ID crosswalk (see [Resolution](#crosswalk-resolution)) |

### The duplicate-header trap

The **RB table carries the header text `">= 50 yd"` and `">= 100 yd"` twice each** - once for
rushing, once for receiving. This is the same trap `prep/manual_csv.py` documents for
FantasyPros, in a worse form, because the collision is *within one row*. Header text is
therefore useless for disambiguation and every row is read **positionally** against
`POSITION_LAYOUTS`. The header is parsed only as a drift detector.

Jahmyr Gibbs is the worked example: rushing `>=50` = 13.4 games and `>=100` = 2.9, receiving
`>=50` = 2.4 and `>=100` = 1.2. A header-keyed parse would have silently produced one pair twice.

---

## Field mapping

Canonical vocabulary per `CLAUDE.md`; enforced by `prep/schema.py`'s `CANONICAL_STATS`
assertion. **Component stats only, never fantasy points.**

### QB (24 columns)

| # | Column | Maps to |
|---|---|---|
| 3 | `Att` | `pass_att` |
| 4 | `Comp` | `pass_cmp` |
| 5 | `Pass Yds` | `pass_yd` |
| 6 | `Pass TDs` | `pass_td` |
| 13 | `Int` | `pass_int` |
| 15-17 | `>= 250 / 300 / 350 yd` | threshold counts on `pass_yd` |
| 18 | `Rush` | `rush_att` |
| 19 | `Rsh Yds` | `rush_yd` |
| 20 | `Rsh TDs` | `rush_td` |
| 21 | `Fum` | `fum_lost` |

### RB (25 columns)

| # | Column | Maps to |
|---|---|---|
| 3 | `Rush` | `rush_att` |
| 4 | `Rsh Yds` | `rush_yd` |
| 5 | `Rsh TDs` | `rush_td` |
| 12-13 | `>= 50 / 100 yd` | threshold counts on `rush_yd` |
| 14 | `Tgt` | `rec_tgt` |
| 16 | `Rec` | `rec` |
| 17 | `Rec Yds` | `rec_yd` |
| 18 | `Rec TDs` | `rec_td` |
| 19-20 | `>= 50 / 100 yd` (the duplicate headers) | threshold counts on `rec_yd` |
| 22 | `Fum` | `fum_lost` |

### WR and TE (24 columns, column-for-column identical)

| # | Column | Maps to |
|---|---|---|
| 3 | `Tgt` | `rec_tgt` |
| 5 | `Rec` | `rec` |
| 6 | `Rec Yds` | `rec_yd` |
| 7 | `Rec TDs` | `rec_td` |
| 14-17 | `>= 50 / 100 / 150 / 200 yd` | threshold counts on `rec_yd` |
| 18 | `Rsh Yds` | `rush_yd` |
| 19 | `Rsh TDs` | `rush_td` |
| 21 | `Fum` | `fum_lost` |

### Columns deliberately NOT mapped - decisions, not silent drops

An import-time assertion in the adapter refuses any column that is neither mapped nor carries a
written reason, so this list cannot rot.

| Column | Why not mapped |
|---|---|
| `0-9 / 10-19 / 20-29 / 30-39 / 40-49 / 50+ <Pass\|Rsh\|Rec> TDs` | TD-by-distance buckets that **sum to the total TD column already mapped** (Gibbs: 7.8+2.9+0.4+0.4+1.4+1.3 = 14.2 vs `Rsh TDs` 14.3). Mapping both double-counts |
| `RZ Tgt` | Red-zone targets: a subset of `Tgt`, no canonical stat |
| `Sck` (QB) | Times sacked: no canonical stat, and this league does not score it |
| `Kick Ret Yds` | Return production has no canonical stat here (no K, no DST, no return scoring) - the same treatment `prep/espn_client.py` gives ids 101-119. **The label is also doubtful**: the values are 0 for every high-usage receiver and rise as the projection falls (Chase 0, Iosivas 17, Tinsley 217, Colbie Young 601, Myles Price 1116), which is rank-shaped, not yardage-shaped. Unmapped either way, so the ambiguity costs nothing |
| `Opp` | An undocumented numeric column. What could be measured was: it scales with the segment's week count (Chase 1.4 for Week 1, 24.2 for the full season), it is larger for QBs than receivers, and it is **not** part of the published `Pts` total. Unnamed and unverifiable means unmapped |
| `Pts` | **Fantasy points**, discarded on every row. Points are computed only by applying this league's own modifiers to component stats. Ingesting a foreign preset's total would be a second, wrong scoring engine hiding inside a projection |
| `#`, `Player`, `Tm` | Rank, identity, team - not stats |

Every unmapped numeric column except `Pts` is retained on
`FantasySharksRow.extras` (keyed by its served header) so it can be inspected later without a
re-fetch.

### What is STRUCTURALLY unpublished

`StatLine` has no `None`, so a stat a source never published looks exactly like a projected
zero. That is the trap `valuation/composite.py` exists to avoid, and it is handled here by
declaring the published set per position (`PUBLISHED_STATS_BY_POS`, the same shape composite
already consumes for FantasyPros via `SOURCE_PUBLISHES_BY_POS`).

| Position | Published (count) | NOT published - must never be averaged in as zero |
|---|---|---|
| QB | 9 | `games`, `pass_2pt`, `rush_2pt`, `rec_2pt`, all receiving |
| RB | 8 | `games`, all 2pt, all passing |
| WR | 7 | `games`, all 2pt, all passing, **`rush_att`** |
| TE | 7 | `games`, all 2pt, all passing, **`rush_att`** |

Two absences to note when wiring: **no two-point-conversion column exists anywhere**, and
**WR/TE publish rushing yards and TDs with no attempt count**. A test asserts that every stat
any parsed row reports as nonzero is declared published at that position.

---

## `games`: there is no column, and that is measured

**FantasySharks publishes no games column on any of the four position tables.**
`games_report()` re-derives that from the parsed pages on every run rather than trusting a
docstring - it scans each table's header for a games-shaped word and counts distinct positive
`games` values in the parsed statlines. Output:

```
QB: 24 header columns, games-shaped headers: NONE
RB: 25 header columns, games-shaped headers: NONE
WR: 24 header columns, games-shaped headers: NONE
TE: 24 header columns, games-shaped headers: NONE
games columns found across all four tables : NONE
DISTINCT positive `games` values published  : 0  []   (over 516 parsed players)
```

A test doctors a `<th>Games</th>` into a fixture page and asserts the measurement changes its
answer, so this is a measurement and not a hardcoded claim.

The same measurement across every source is the context that makes the number mean something:

| Source | Distinct positive `games` values | Reading |
|---|---|---|
| ESPN | 7 | Real per-player variation |
| Sleeper | **1** | A blanket constant (18.0 for all 3,111 records) - and 18 exceeds this league's own 17 weeks |
| FantasyPros | 0 | No games column at all |
| **FantasySharks** | **0** | No games column at all |

So `StatLine.games` is `0.0` here, meaning **unknown**: downstream applies the positional
availability prior, and must never read it as a projection of zero games. Stated plainly: a
source with no games column and a source publishing one constant are equally uninformative about
durability. The only difference is that the constant looks like a forecast.

---

## Bonus threshold coverage

Against `data/league_manual.yaml`'s `scoring_bonuses` (read via
`valuation/bonuses.load_bonus_schedule()`, passed *into* the adapter so a prep module never
reaches into `valuation`):

| Bonus stat | League tiers | Covered | Not covered |
|---|---|---|---|
| `pass_yd` | 300 (+3), 400 (+1), 500 (+1) | **300** (QB) | 400, 500 |
| `rush_yd` | 100 (+3), 150 (+1), 200 (+1) | **100** (RB) | 150, 200 |
| `rec_yd` | 100 (+3), 150 (+1), 200 (+1) | **100** (RB, WR, TE), **150** (WR, TE), **200** (WR, TE) | none |

The plan's expectation - "rushing and passing match only the +3 tier while receiving matches all
three" - is confirmed, **with one qualification the report must not blur**: receiving covers all
three tiers only for **WR and TE**. The RB table's receiving threshold columns stop at 100, so an
RB's receiving 150/200 tiers have no external reference. Given that a running back clearing 150
receiving yards in a game is rare, that is a small gap, but it is a gap.

Extra thresholds published that this league does not pay: `pass_yd` 250 and 350 (QB), `rush_yd`
50 (RB), `rec_yd` 50 (RB, WR, TE). Not waste - they constrain the shape of the same per-game
distribution `valuation/bonuses.py` is estimating, from four points on the curve instead of one.

`ThresholdProjection.get(stat, threshold)` returns **`None`**, never `0.0`, for an unpublished
tier. Returning zero would assert the player never clears it, which is a projection this source
never made.

---

## Crosswalk resolution

`Crosswalk.resolve_fantasysharks_row()` (additive, in `prep/crosswalk.py`). It takes **no
`extra_ids`** argument, deliberately: FantasySharks' own player ids appear in **no** ID
crosswalk - not in Sleeper's cross-ID fields, and not in any DynastyProcess column (checked
against the header: mfl / sportradar / fantasypros / gsis / pff / nfl / espn / yahoo /
fleaflicker / cbs / pfr / cfbref / rotowire / rotoworld / ktc / stats / stats_global /
fantasy_data / swish - there is no fantasysharks column). Stage 1 (`direct_id`) is therefore
structurally unavailable and every row resolves on name+team+pos or fuzzy. Feeding the
FantasySharks id in as some other source's id field would be worse than useless: it would
collide with real ids in that field's index and hand back a confidently wrong player.

Measured 2026-08-20:

| | |
|---|---|
| `exact_name_team_pos` | 502 (97.3%) |
| `exact_name_pos` | 6 (1.2%) |
| `override` | 2 (0.4%) |
| `unresolved` | 6 (1.2%) |
| **Resolved** | **510 of 516 (98.8%)** |

**Gate #2 (zero unresolved inside the top 200 by ADP): PASS.** Of the 189 top-200-by-ADP players
the crosswalk resolves, **186 (98.4%) are covered** by FantasySharks and **0 are failed joins**.
The three uncovered are players FantasySharks simply does not publish: Ricky Pearsall (ADP 118.0),
Carson Beck (142.6), Theo Wease (143.6).

Two real join failures were found and fixed. Both were nickname mismatches scoring 86.7 against
the crosswalk's fuzzy threshold of 90:

- `Kenneth Gainwell` (FS id 15255) vs Sleeper's `Kenny Gainwell` - **inside the ADP window
  (132.8), so a genuine gate failure before the fix**
- `Mitchell Tinsley` (FS id 16403) vs Sleeper's `Mitch Tinsley`

Both are pinned in `data/overrides.csv` under `source=fantasysharks`, rather than by adding
`kenny`/`mitch` to `schema._NICKNAME_FOLD`: an override is scoped to one source row, a fold rule
changes joins for every source.

The remaining 6 unresolved rows are **all fullbacks** - Adam Prentice, Alec Ingold, Hunter
Luepke, Kyle Juszczyk, Michael Burton, Patrick Ricard. FantasySharks lists them under Running
Back; Sleeper classifies them `FB`, which the crosswalk spine filters out (`SKILL_POSITIONS` is
QB/RB/WR/TE). That is a scope fact, not a crosswalk defect, and the verification tool now labels
it as such. **Every joinable player resolves.**

---

## Independence verdict: INDEPENDENT

`tools/verify_fantasysharks.py`. This check is a **precondition** for using FantasySharks in the
composite, the disagreement badge, the envelope validator or the review queue.

### Why there are two controls

A correlation of 0.97 means nothing on its own - every projection source is forecasting the same
football season. So the same machinery runs over a pair known to be **one** source and a pair
known to be **two**, and FantasySharks is read against those, not against an invented threshold.

**Positive control - ESPN API vs Mike Clay PDF** (`CLAUDE.md`: one source, not two):

| stat | n | max&#124;d&#124; | mean&#124;d&#124; | corr |
|---|---|---|---|---|
| pass_yd | 40 | 0.498 | 0.288 | 1.0000 |
| rush_yd | 215 | 0.496 | 0.252 | 1.0000 |
| rec_tgt | 340 | 0.498 | 0.258 | 1.0000 |
| rec_yd | 340 | 0.497 | 0.259 | 1.0000 |
| games | 416 | 2.000 | 0.005 | 0.9978 |

**415 of 416 players (99.8%) agree on every compared stat within rounding**, max difference 0.50
on every stat. This reproduces `CLAUDE.md`'s 411/411 finding, which is what makes the verdict
machinery trustworthy. (Getting this control right required using Clay's *own* published set -
his PDF carries no 2pt and no fumbles columns. Comparing those against ESPN's structural zeros
dropped the control to 68.5% and would have mis-calibrated the entire report.)

**Negative control - Sleeper vs ESPN** (accepted as two independent families): **0 of 422
players (0.0%)** agree on every stat within rounding; median per-stat mean&#124;d&#124; is
**21.3%** of the stat's own level; median correlation **0.961**.

### FantasySharks

| Pair | players compared | agree on EVERY stat within rounding | median mean&#124;d&#124; / level | median corr | verdict |
|---|---|---|---|---|---|
| FantasySharks vs Sleeper | 454 | 0 (**0.0%**) | 27.0% | 0.936 | **INDEPENDENT** |
| FantasySharks vs ESPN | 423 | 0 (**0.0%**) | 25.1% | 0.933 | **INDEPENDENT** |
| FantasySharks vs FantasyPros | 453 | 1 (**0.2%**) | 23.4% | 0.949 | **INDEPENDENT** |
| *(reference)* Sleeper vs ESPN | 422 | 0 (0.0%) | 21.3% | 0.961 | independent |
| *(reference)* ESPN vs Clay | 416 | 415 (**99.8%**) | 0.8% | 1.000 | **re-publication** |

Selected per-stat detail, FantasySharks vs ESPN - the pair that matters most, because ESPN was
the sole source of targets:

| stat | n | max&#124;d&#124; | mean&#124;d&#124; | mean&#124;d&#124; / level | corr |
|---|---|---|---|---|---|
| `rec_tgt` | 359 | 78.169 | 12.059 | **25.0%** | 0.9242 |
| `rec_yd` | 356 | 549.100 | 94.087 | 26.7% | 0.9312 |
| `rec` | 358 | 36.827 | 7.970 | 25.1% | 0.9290 |
| `rec_td` | 345 | 6.364 | 0.984 | 44.6% | 0.8493 |
| `pass_yd` | 62 | 1091.415 | 293.644 | 14.3% | 0.9773 |
| `rush_yd` | 319 | 422.479 | 43.432 | 21.3% | 0.9748 |

**Verdict: FantasySharks is INDEPENDENT of Sleeper, ESPN and FantasyPros.** It disagrees with all
three at roughly the same magnitude they disagree with each other (23-27% of level vs the
21.3% Sleeper/ESPN baseline), and it sits nowhere near the re-publication signature. It is a
genuine fourth family.

The `rec_tgt` line is the specific win: FantasySharks and ESPN differ by a mean of 12 targets
(25% of level, r = 0.924) on 359 shared players. A stat that had one source now has two that
actually disagree, which is what makes averaging it meaningful and what gives the envelope
validator something to cross-check.

---

## Caching and phase discipline

Raw pulls cache to `data/raw/fantasysharks/<UTC-timestamp>.json` as **one** payload holding the
season, resolved segment and label, scoring preset, each URL, and each page's full HTML. A new
directory, so it cannot move what `load_latest_raw()` resolves to for any existing source.
`data/raw/` is gitignored.

`prep/fetch_all.py` was **never** run - `CLAUDE.md` documents that it writes new timestamped
files into `data/raw/` for existing sources and breaks unrelated tests.

Fetching is PREP-phase only. Draft night reads a frozen snapshot with the wifi off, and
`install_socket_guard` enforces it.

---

## Gates run

| Gate | Result |
|---|---|
| `pytest -q` | **621 passed** (49 new, all pre-existing green) |
| `tools/run_invariants.py` | **GATE: PASS**, 8/8 checks |
| `tools/verify_fantasysharks.py` against real fetched data | ran; positive control reproduces `CLAUDE.md`'s ESPN/Clay finding at 99.8% |
| Tests hit the network | No. Fixture only; the one `fetch_projections` test monkeypatches the transport |
| Crosswalk completeness, top 200 by ADP | 0 failed joins |

---

## Wiring checklist -- state as of 2026-08-20

1. **DONE** `valuation/composite.py`: `"fantasysharks"` is in `COMPOSITE_SOURCES`, with entries
   in `SOURCE_PUBLISHES` (the union, from `PUBLISHED_STATS`) and `SOURCE_PUBLISHES_BY_POS` (from
   `PUBLISHED_STATS_BY_POS`). Position-keyed, as required. Re-verified against the real cached
   payload by the same presence-vs-nonzero method used for Sleeper and ESPN, and the check now
   runs as a test: every declared stat is nonzero on at least one row of its position and no
   undeclared stat is ever nonzero. `rush_att` is nonzero on 78 of 78 QB rows and 0 of 187 WR
   rows while those WR rows carry `rush_yd` on 130 of them.
2. **DONE, confirmed by measurement** `games` contributes nothing, via
   `varying_games_sources` reporting 0 distinct positive values over the resolved pool - no
   special case anywhere. Two tests: one on the real resolved pool, one on fabricated pools.
3. **DONE** `validate/board.py::_resolve_fantasysharks_statlines(cw)`, mirroring the ESPN one and
   degrading with a warning on a missing cache OR a `ColumnLayoutError` from drifted markup.
4. **STILL OPEN** `prep/fetch_all.py`: add the fetch so a refresh picks it up. Not done here -
   `prep/` was outside the wiring change's scope. Until it is done, the cached payload under
   `data/raw/fantasysharks/` is the only one that exists and a refresh will not renew it.
5. **DONE** `valuation/disagreement.py`: in `INDEPENDENT_SOURCES`, and `DISAGREEMENT_CAVEAT`
   updated to four families with its substance intact (HIGH disagreement is the signal; LOW is
   not a safety signal, and a fourth forecast does not change that). Re-run
   `tools/verify_fantasysharks.py` if the source is ever re-fetched after a site change.
6. **DONE, as a validator and nothing else** The threshold projections are consumed by
   `tools/validate_bonus_vs_sharks.py` only. They are not canonical component stats and are
   never blended.

### What wiring it in moved

`DISAGREEMENT_CV_THRESHOLD` was re-derived, because a fourth independent family shifts the
distribution the old constant was read off. Rule (unchanged, now stated): the **80th percentile
of the measured CV distribution** - the badge marks the noisiest fifth of the board, which is a
proportion and therefore a quantile. Measured on the four-source board: p50 0.066, **p80 0.141**,
p90 0.229, max 0.540, against three-source p50 0.045 / p80 0.100 / max 0.359. So 0.141, flagging
38 of 188 (20.2%). The old 0.10 flagged 19.9% of the three-source board and would flag 29.3% of
this one.

Board movement, three sources to four, in draft-value rank terms over the 188 shared players:
134 move more than 1 spot, 54 more than 5, 20 more than 12, 1 more than 24; mean |move| 4.7,
max 33. Coverage did not change (188 valued either way; Ricky Pearsall is the one exclusion, and
FantasySharks does not publish him either).

Per-source invariants, each board independently: blend 8/8, sleeper 8/8, espn 8/8,
**fantasysharks 8/8**, fantasypros 7/8 (the known, accepted `qb_count_in_top30` failure - see the
"UNRELIABLE STANDALONE" note in `sources.py`). Nothing was relaxed.
