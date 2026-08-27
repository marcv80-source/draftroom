# draftroom

Personal fantasy football draft model + live draft assistant for Marc's league. Draft is **2026-09-08**, in person, stickers on a board. This tool is the only live record of what's gone.

## The league (drives everything)

Yahoo-hosted snake draft, in person. **Two mandatory starting QBs. No kickers. No defenses. Short bench.**
Exact team count, roster slots, scoring, and draft slot are **never hardcoded** — `LeagueConfig` is
constructed at runtime from `data/league_manual.yaml`, which holds settings read off the Yahoo web UI
by hand. **That file is the source of truth — read it, don't paraphrase it here.**

**YAHOO API ACCESS IS DEAD AND IS NOT COMING (Marc, 2026-08-25).** Developer access was never granted
and he is not pursuing it. Everything the API was going to provide is either already in
`league_manual.yaml` (settings, scoring, roster, team names, all read by hand) or is simply not
available (prior-season pick-by-pick). Do NOT write a Yahoo client, do NOT plan around access
landing, and do not treat a Yahoo-shaped absence as a temporary gap. One thing IS still on the table:
Marc can export Yahoo's own player projections from the web UI by hand, 25 rows at a time, pasted
into Excel. That is a possible FIFTH projection source on the same manual path `prep/manual_csv.py`
already serves for FantasyPros. He will do the export closer to the draft; nothing is built for it
yet.

**CONFIRMED 2026-08-17** from the Yahoo Scoring & Settings page: **10 teams**, starters
`{QB:2, RB:2, WR:3, TE:1, FLEX(RB/WR/TE):1}`, **bench 6**, 15 rounds, 17 weeks, `pass_int -2`
(the league overrode Yahoo's default of -1). Draft slot still unknown until draft night.
Earlier drafts of this file said 12 teams / bench 5 / int -1. All three were wrong.

**Why this matters:** 10 teams x 2 QB x 17 weeks = 340 QB-games of demand, so replacement-level QB is
**QB22**, not ~QB11. Every public ranking is built for 1-QB leagues with K and DST and is wrong here.
That gap is the entire edge. If a number looks like a normal league's number, something is broken.

**One deliberate 12-team exception:** the FFC ADP feed is fetched at `teams=12` because FFC publishes
2QB ADP only in that format (`prep/ffc_client.py`). It is a proxy for this 10-team room, not a
description of it, and the pick-number mapping is therefore approximate. Never "fix" it to 10 — the
endpoint returns nothing there. **Nothing corrects for the mismatch, and that is deliberate.** The
`adp_scale` factor computed for it belonged to the opponent-calibration work that LOST to plain ADP
in leave-one-manager-out validation, so production runs `LeagueCalibration.national_only()` and uses
national ADP raw. ADP is a pick NUMBER and is roughly team-count invariant; what team count changes
is round boundaries, how many picks pass before your next turn, and positional demand - and all three
already read the real 10-team config. This line previously said correcting the mismatch was "the
opponent model's job", which implied a correction that does not happen and should not.

## Two phases — the load-bearing architecture idea

- **PREP** (online, run often): fetch -> resolve IDs -> score -> composite -> model. Writes
  timestamped raw payloads under `data/raw/<source>/`.
- **DRAFT** (offline, draft night): installs and VERIFIES a socket guard (any outbound non-localhost
  connect raises) before the server binds, rebuilds the board from the cached payloads, and writes
  only to a separate append-only draft event log.

**THERE IS NO SNAPSHOT ARTIFACT, AND THIS FILE USED TO CLAIM THERE WAS** (corrected 2026-08-25 by
audit). No snapshot module exists, no build or load path exists, `data/snapshots/` is empty, and
`--draft` gates on nothing: it installs the socket guard and calls `create_app()`, which resolves
whatever file is NEWEST in each `data/raw/<source>/` directory. The two-phase split is real and the
offline guarantee is real and enforced at runtime; the *freeze* is not. Consequences worth knowing
rather than rediscovering:

  - Nothing seals the board, so the board verified the night before is not PROVABLY the board
    drafted on. On the night itself wifi is off, so nothing can move underneath it.
  - An interrupted fetch leaving a truncated file with the newest timestamp WOULD be loaded. That is
    exactly the failure a sealed, checksummed snapshot exists to prevent.
  - The mitigation that actually protects draft night is operational: after final prep, STOP
    FETCHING. Do not run `fetch_all` between the last verification and the draft.

Building the real thing is POST-DRAFT work. Do not start it in the two weeks before 2026-09-08: it
touches draft-phase startup, the one code path whose regression cost is the draft itself.

Draft night must work with wifi physically off. No live network call may exist on any draft-phase code path.

## Canonical stat vocabulary (single source of truth)

Every source adapter emits **component stats, never fantasy points**. Points are computed only by applying
the league's own Yahoo `stat_modifiers`. The canonical names:

```
pass_att pass_cmp pass_yd pass_td pass_int pass_2pt
rush_att rush_yd rush_td rush_2pt
rec rec_tgt rec_yd rec_td rec_2pt
fum_lost
games
```

Any source field maps into this vocabulary **at ingest**. A Yahoo `stat_id` present in the league's
modifiers but missing from the stat map is a **hard pipeline failure**, never a silent skip.

## Non-negotiable gates

1. **Scoring reconciliation - SPECIFIED BUT NEVER IMPLEMENTED, and this file used to list it as an
   active gate** (corrected 2026-08-25). The design was: re-score ~12 players' actual 2025 stats with
   the engine and last season's modifiers, and assert within 1.0 point of Yahoo's recorded season
   totals. It needs Yahoo's recorded totals, Yahoo access was never granted, and no code or test for
   it exists anywhere in the tree. **Two gates run, not three - say two.** If Marc ever hand-copies
   about a dozen players' 2025 Yahoo season totals into a fixture this becomes cheap to build and is
   worth doing, because the whole gate needs roughly twelve numbers.
2. **Crosswalk completeness.** Zero unresolved players inside the top 200 by ADP. Deeper unresolved players
   are dropped with a warning.
3. **Sanity invariants** on every data refresh: the top QB must rank strictly HIGHER under this
   league's 2-QB rules than under 1-QB rules (#9 vs #18 on the real 2026 board) — this REPLACED a
   hardcoded "top QB lands top-8 overall" on 2026-08-20, which was never derived from anything.
   Measured, the QB curve's one dominant cliff is immediately after **QB1** (40.9 pts on the
   composite, the largest gap on the whole board under every source, range 32.4–45.7), with QB1 at
   overall #9 and QB2 at overall #22 — so rank 8 sat in empty space inside a 13-slot chasm, and the
   check's verdict tracked the active projection source (#7 Sleeper, #8 ESPN, #9 blend, #12
   FantasyPros) rather than the model. It also FAILS rather than passing vacuously if ever run on a
   1-QB league; the 2-QB shift is
   directional — scoring the SAME board under 1-QB rules and this league's 2-QB rules must put
   strictly more QBs in the top 30 under the 2-QB rules (replaces an earlier fixed "10-14 QBs in
   the top 30" band, which assumed 12 teams and broke on 2026's legitimately compressed QB curve —
   an absolute count can never be immune to a given year's projection spread); baselines move
   monotonically with team count and starter slots; survival is monotone and `S(n0)/S(n0)==1`;
   per-game fixture (high-PPG/few-games beats low-PPG/many-games at equal season totals); no ranked
   player's expected_games equals the full season by default, and every real-board expected_games
   respects the `min(source, availability-curve)` cap.

Never present a number that hasn't passed these. State which checks ran.

## Conventions

- Python 3.12. Windows. Paths contain spaces elsewhere on this machine but **this repo lives at `C:\dev\draftroom`** — never under OneDrive.
- Secrets in `%LOCALAPPDATA%\draftroom\` (`secrets.json`). **Never in the repo.** No Yahoo token is
  stored, because there is no Yahoo client. The OAuth token-rotation and JSON-transliteration
  conventions this file used to carry were removed 2026-08-25: they were guidance for an integration
  that will never be built.
- Raw fetches cache to `data/raw/<source>/<UTC-timestamp>.json`. Never re-fetch in a test.
- **Running `prep/fetch_all.py` live has a side effect on the test suite.** It writes new timestamped files
  into `data/raw/`, which moves what `load_latest_raw()` resolves to, which breaks any test that reads
  cached raw data (`test_live_model.py`, `test_bonuses.py`). Hit for real 2026-08-17. If you run a live
  fetch to smoke-test something, either delete the files you created afterwards or expect red tests that
  have nothing to do with your change.
- **There is no SQLite and no snapshot store** (this line used to claim both; corrected 2026-08-25).
  Draft state is an append-only JSONL event log, fsync'd before the UI acknowledges a pick, and
  everything else is rebuilt in memory from the cached raw payloads. See the two-phase section.
- Attribution in the UI must name the sources actually used. It read "Fantasy data provided by Yahoo
  Fantasy", which was **false**: no player data comes from Yahoo. Corrected 2026-08-25 to credit
  Sleeper, ESPN, FantasyPros and FantasySharks for projections and Fantasy Football Calculator for
  ADP, and to say league settings were read from Yahoo's own pages by hand.

## Data sources

| Source | Auth | Gives | Notes |
|---|---|---|---|
| Sleeper | none | player universe + IDs, season projections | `api.sleeper.app/v1/players/nfl` (~5MB, cache daily). Projections on `api.sleeper.com` are undocumented and may break. |
| Fantasy Football Calculator | none | **2QB ADP + std_dev** | `/api/v1/adp/2qb?teams=12&year=2026`. Free for personal use, refreshes daily. The std_dev is what the survival model needs. |
| FantasyPros | **none — manual CSV** | raw stat-line projections | **Decided 2026-08-17: no API, no subscription.** Marc downloads the Half-PPR projections tables by hand into `data/manual/`; `prep/manual_csv.py` ingests them. See `docs/MANUAL_PROJECTIONS.md`. No targets column. |
| ESPN | none | stat-line projections **incl. targets** | `prep/espn_client.py`. `lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/<yr>/segments/0/leaguedefaults/3?view=kona_player_info` + `X-Fantasy-Filter` header. 461 players. Omitting `sortDraftRanks` from the filter returns HTTP 400. Past seasons work by swapping `<yr>`, and `statSourceId` 1 = projection, 0 = actuals (`statSplitTypeId` 0 = season total) — that is how the 2025 backtest is possible. |
| FantasySharks | none | stat-line projections **incl. targets**, plus projected counts of games clearing each yardage threshold | `prep/fantasysharks_client.py`, 516 players. **Verified independent 2026-08-20** against a positive control (ESPN vs Clay reproduced at 99.8% agreement; Sharks agrees with all three others at 0.0–0.2%). `Segment` changes yearly and must be read from the page's own `<select>` — never hardcode it. `Position=4` is WR, not 3. |
| DynastyProcess `ff_playerids` | none | cross-source ID crosswalk | The join key for everything. |
| Yahoo | **none - dead** | nothing | Developer access never granted, not being pursued (2026-08-25). Settings live in `data/league_manual.yaml`, read by hand. Yahoo's own projections are manually exportable 25 rows at a time and are a possible fifth source; nothing is built for it. |

`nfl_data_py` is **dead** (archived Sep 2025). Use **nflreadpy** for historical stats.

**There is NO single "source of record" for projections — the COMPOSITE is.** This file previously
called ESPN the source of record while `build_real_board` in fact used Sleeper alone, and neither
was right. Since 2026-08-20 the default board is the **equal-weight component-stat composite**
(`valuation/composite.py`, `DEFAULT_BOARD_SOURCE = "blend"`), and `RealBoard.source` /
`active_source` always name the board actually being served. Equal weighting is **measured, not
assumed**: the 2025 backtest (`docs/archive/SOURCE_BACKTEST.md`) put the best-weight bootstrap interval at
0.30–1.00, with the in-sample optimum worth ≤0.22 points per player against an 86.5-point outcome
spread. Do not reweight without multi-season evidence.

**Sleeper's `games` is the constant 18.0 for all 3,111 records** — one distinct value, not a
forecast, and one MORE than this league's 17 weeks. ESPN's is a real per-player figure (7 distinct
values); FantasyPros and FantasySharks publish no games column at all. A source's `games` therefore
contributes to the blend **only if it varies within that source** (`varying_games_sources()`), or
Sleeper's constant would average away the only genuine durability signal in the pipeline.

**Do not re-litigate the FantasyPros decision.** Checked 2026-08-17: premium API keys require a $22.99/mo
HOF subscription (the often-quoted $8.99 is the MVP tier, which grants **no** API access at all); the free
API tier is documented as "non-production, sample data" and may not return a full player universe. More
decisively, **FantasyPros projections carry no targets column** — WR columns are `REC, YDS, TDS, ATT, YDS,
TDS, FL, FPTS`. Buy nothing; the manual CSV path covers it.

**Projected targets are SOLVED — they come from ESPN** (`rec_tgt`, stat id 58), 342 players. Sleeper has
none under any field name (checked across all 3,111 records). Earlier notes pointing at nflreadpy history
for targets are superseded: use nflreadpy for weekly *dispersion* (the bonus model), ESPN for projected
targets.

**ESPN's API and Mike Clay's PDF are ONE SOURCE, not two.** Verified 2026-08-17 by comparing both
extractions field-by-field: **411 of 411 overlapping players agree on every stat, max difference 0.50**
(pure integer rounding — the PDF is Clay's numbers rounded, the API is the same numbers with decimals).
Clay is ESPN's projections analyst. **Never count them as two independent sources** — doing so in any
cross-source variance/consensus measure makes disagreement look artificially small, because it is Clay
agreeing with himself. Honest independent families: **Sleeper, FantasyPros (itself a pre-averaged expert
consensus), ESPN/Clay.** Repro: `tools/` comparison in the 2026-08-17 session scratchpad.

**The `espn-api` PyPI library's stat-id table is WRONG for id 22** — it labels it a passing-yards
duplicate; the live value is passing yards *per game*. Its `POSITION_MAP` also does not match
`defaultPositionId` (real map: 1=QB, 2=RB, 3=WR, 4=TE, 5=K, 16=D/ST). Verify every ESPN stat id against
ESPN's own derived fields, which is how these were caught: id 21 must equal cmp/att, id 60 must equal
rec_yd/rec, id 73 must equal ints + fumbles. A wrong id produces plausible-looking numbers in the wrong
field, which nothing downstream will catch.

## Modeling decisions already made (don't relitigate)

- **EVERY PROPOSED CORRECTION MUST BEAT A DUMB NULL OF EQUAL MAGNITUDE BEFORE IT SHIPS.** Not "is it
  better than doing nothing" — "is it better than the crudest possible intervention of the same
  size." This is the general form of Marc's own objection to arbitrary thresholds, and **two
  corrections have already been declined on it** (2026-08-20). Per-position calibration shrink: all
  four slopes below 1.0 and QB worst, corroborated by 12 independent seasons, but our own WR slope
  (0.69) flatly contradicts the reference (0.85), the QB coefficient spans 0.24–0.97, and the
  parameter that would do the most damage to this league's thesis is the one least identified.
  Team-identity renormalization: improves 2025 MAE 37.14 → 36.04 (p=0.000), but a **flat haircut
  removing the same league-wide total from the same players** gets 36.42, all 24 identity-vs-null
  comparisons read "not distinguishable," the flat cut WINS on the top 60 by ADP, and rank ordering
  (the only thing a board consumes) got slightly worse. See `docs/archive/RENORMALIZATION_VERDICT.md`,
  `docs/archive/SOURCE_BACKTEST.md`.
- **Rejecting a projection is a REVIEW QUEUE, not a rule.** Marc adjudicates; nothing auto-rejects.
  Candidates are surfaced with every source's number and the board impact, decisions persist in
  `data/projection_decisions.json`, and an applied decision is always visible on the board. At a
  small number of correlated sources, distance-based auto-rejection is not sound (the smallest
  well-defined symmetric trim of three forecasts is just the median), so the trigger is
  **contamination** — a failed accounting identity, a bad join, a unit error, a constant posing as a
  projection — never distance from the other sources.
- **Projections are not expectations, and this is measured.** ESPN puts 89.9% of drafted players at
  exactly 17.0 projected games; they averaged 13.7. Among players who actually played 15–17 games
  Sleeper's bias is NEGATIVE 9.5 and 57.8% beat their number. They are conditional-on-role,
  if-healthy scenarios with the availability discount applied elsewhere — which is why summing them
  over a team does not reconcile, and why forcing it to reconcile was rejected above.
- **Per-game valuation**, not season totals: `EVoB = (PPG - baseline_PPG) * expected_games`.
- **Man-games replacement level**, parameterized by roster rules, with greedy flex allocation.
- **Tiers recomputed after every pick** on the remaining pool — a tier is defined by who's left.
- **The ALL board ranks by BEST PICK NOW, and three things about that are counterintuitive enough
  that they have each been got wrong once** (ledger #12, 2026-08-26). (1) **VONA does not lift the
  quarterback.** On the real 2026 board RB's VONA is **74.5** against QB's **58.7**, so ranking by
  cost-of-waiting alone moves Josh Allen from 9th to *eleventh*. What puts him first in the panel is
  a **hard gate** — the elite-QB grab (a top-3 board QB available with 0 of 2 rostered) — not any
  continuous price. Do not describe the QB's position as VONA-driven. (2) **`value + VONA` is not an
  identity for `utility`.** It reproduced the ordering for all 16 candidates at one board state,
  which is a measurement, not a guarantee: at a back-to-back turn the panel optimises a *pair*, and
  mid-round `utility` carries a candidate-specific continuation and risk term. The board therefore
  reuses the panel's own order (`Candidate.gate_priority`, published for exactly this) for gated
  players, and uses `value + VONA` only for the remainder the panel does not rank. (3) **Never gate
  the board on `forced_positions`.** It names a whole POSITION, while the panel's gate covers only
  candidates past feasibility and the per-position top-N cut — so a QB scarcity floor would hoist
  every remaining QB, QB23 and below included, above every RB/WR/TE. Gate on candidate ids.
  Also: **at a back-to-back turn every VONA is legitimately 0.0** (nothing can be taken in a gap of
  zero picks), so a pick-now column there is *correctly* equal to draft value — say so rather than
  rendering a column of duplicated numbers.
- **`tier_board()` serves each position in ADP ORDER, not value order.** Deriving a positional rank
  from a row's index in that list therefore yields an *ADP* rank, which is wrong beside the value
  columns and visibly so: by ADP Lamar Jackson is the third QB and Drake Maye the second, while by
  draft value it is the reverse. Sort by value explicitly. (Cost a build on 2026-08-26.)
- **RESEARCH THAT IS NOT IN THE NUMBERS GETS ITS OWN BADGE, and it inverts every other badge on
  the row** (ledger #10, 2026-08-27). `REJ` and `NN.NG` both mean *a value on this row MOVED*.
  `RISK` / `-NG?` mean the opposite: something is known and the value does **not** include it.
  Three things follow that are easy to get backwards. (1) **`games_missed: null` is UNPRICED and
  is NOT `0`.** Zero is a positive claim that he plays a full season; null is the absence of any
  claim, which is the only honest shape for an open disciplinary review. Collapsing them one way
  invents a number, the other way loses the finding entirely -- which is what the code did before
  this existed, because `tools/injury_sweep.py` can only act on a finding that carries a NUMBER.
  (2) **An applied playing-time override SUPPRESSES the note**, because `NN.NG` says what the
  research cost rather than merely that research exists. A note is therefore expected to disappear
  when an override lands, and that is a stronger badge replacing a weaker one, not a regression.
  (3) **Suspension risk has ZERO sources in this pipeline** and no source will ever add one, so
  this badge is the only place such a finding can surface. Measured before choosing it: a 4-game
  haircut would have moved Puka Nacua from board #4 to #11 on a number nobody has.
- **`data/injury_research.json` is the THIRD fail-closed human-decision file**, alongside
  `projection_decisions.json` and `playing_time.json`, and `InjuryResearchError` is in
  `live_data.py`'s fail-closed tuple. Same asymmetry as its siblings: a MISSING file means nothing
  was researched; an existing-but-empty or malformed one RAISES, because an empty file is what a
  truncated write looks like and degrading to placeholder mode reads as "the cache is stale"
  rather than "your findings stopped being shown". Its `player_id` is the **Sleeper** id.
- **Survival conditioned on the player still being on the board**: `P = S(N)/S(n0)`. Unconditioned is wrong and always too pessimistic.
- **Opponents are modeled as herding** (research shows managers demonstrably herd off the previous pick);
  **our recommendations never herd** (the same research shows herding doesn't correlate with winning).
- **Never reach to complete a QB/WR stack.** Correlation is a best-ball/DFS tool. Accept a stack that falls
  to you on value; flag shared bye weeks loudly — with two QB slots and a short bench, a shared bye can leave
  an unfillable lineup.
- **The sim/tournament objective fills a truly unfilled mandatory slot with the best undrafted waiver
  player.** That fill is generous (no acquisition cost, no availability risk, and real QBs ranked 25-40 play
  <=12 games 62.5% of the time), so `best_value` topping the pooled tournament table while rostering zero
  QB2s is an artifact of the objective's generosity, NOT a punt-QB endorsement. Do not cite that row as a
  strategy result.
- **Predicting opponent behavior beyond plain ADP is dead — it failed its validation gate three separate
  times** (2026-08: calibrated per-position offsets, a room QB-timing curve, a satiation damper; all lost
  to plain ADP in leave-one-manager-out on the 2025 draft). What works and ships is COUNTING: open starter
  slots, demand before the next turn, the shared scarcity trigger in `draft/scarcity.py`. Do not rebuild
  prediction without multi-season room data. Measured-only artifacts stay parked in `data/` (e.g.
  `opponent_calibration_2025.json`, which ships with EMPTY offsets and is what
  `TestShippedCalibrationFile` guards). The tools that produced these were deleted 2026-08-25;
  `opponents.py` still carries their now-inert fitting functions, and excising those is POST-DRAFT
  work because they sit inside the engine that runs in the room.

## The player pool is TWO TIERS (don't collapse them)

`live_data.load_player_pool()` returns **980** players, not the ~189 in the ADP feed:

- **`is_ranked=True`** (~189) — in the FFC ADP feed. Real ADP, std_dev, and a placeholder `value`.
  These are the only players that get tiered, valued, or recommended.
- **`is_ranked=False`** (~790) — every other active skill-position player on an NFL roster, from the
  cached Sleeper universe. `value=0.0`, never recommended. **They exist so the board can RECORD them.**

Why: bookkeeping is this tool's *first* job. A 150-pick draft against a 189-player pool means the late
rounds are full of names the board cannot see, and each one becomes a manual write-in. Marc asked for
every draftable name listed "even if we don't have projections" — tracking needs the name, recommending
needs the projection, and those are different requirements. A `value` of 0.0 is **not** an evaluation;
the UI must render unranked players as "no projection". Bye weeks are a **team** property, derived for
all 32 teams from the ADP feed (Sleeper carries `bye_week` for 0 of 988 records).

## Tone of the output

The engine **informs, never insists**. Every recommendation ends with the fallback. Marc is in the room and
knows things the model doesn't. Explanations are 2-3 scannable bullets, each backed by one computed number,
never a wall of tables.

## Feedback goes in the ledger, and "done" means verified against the running app

`FEEDBACK_LEDGER.md` at the repo root is the single record of Marc's feedback on this tool. **One
ledger per repo, forever** -- rounds are groupings inside it, never new files. Item numbers are
permanent and never reused, so an item that regresses keeps its number and the history shows it
broke once.

The rules that make it worth having, learned on other projects where items silently vanished
between builds:

- **Capture before analysis.** Every distinct item gets a number the moment it arrives, even the
  ones that turn out to be non-issues. Over-split rather than under-split: merging two complaints
  into one item is how one of them gets lost.
- **Nothing exits silently.** `deferred` and `dropped` carry a reason and keep appearing in the
  round summary. Item #3 of round 1 is `dropped` (Marc answered it himself mid-sentence) and is
  still in the file, on purpose.
- **VERIFIED requires the RUNNING app**, with the date and the method recorded. Code merging is
  `implemented`, not `VERIFIED`. "The code looks right" is not a status.
- **Investigate before planning.** Three of round 1's eight items were questions of fact, and
  measuring them changed the fix in two cases: the source toggle was working (real consensus at the
  top: mean rank move 1.0-1.6 in the top 12, rising to ~13 by rank 97), and click-to-draft already
  existed but its only affordance was an underline that was transparent until hover.

## Ship config (used by /ship)
- remote: https://github.com/marcv80-source/draftroom.git
- review-script: reviews/run-codex-review.ps1
- agents-md: AGENTS.md (copy of this file for Codex — refresh when this file changes)
- tests: `.venv\Scripts\python.exe -m pytest -q` all green; `.venv\Scripts\python.exe tools\run_invariants.py` gate PASS; `cd frontend && npm run build` clean (tsc catches what vitest misses; compiled assets are gitignored)
- local-link: http://127.0.0.1:8484 (only while the server is running — offline personal tool, no deployed instance)
- verify-live: rebuild frontend if src changed, launch `--draft --my-slot <N>`, GET /healthz = 200, and confirm one concrete marker from the batch in a served payload
- publish: none — the GitHub push is the delivery

## Draft-night operational hazards (learned the hard way)

- **The event log is NOT cleared between test runs.** On 2026-08-20 the live `data/drafts/draft.jsonl`
  still held four smoke-test picks from two days earlier; launching on it would have opened the draft
  at pick 5 with Josh Allen, Jaxson Dart, Sam Darnold and C.J. Stroud already gone, and the only
  symptom would have been a board that looked subtly wrong in a room full of people.
  `_announce_existing_draft` now prints a loud startup summary (pick count, opening pick, recent
  players BY NAME, and the archive command). It never refuses, because a non-empty log is also
  exactly what crash recovery looks like, and those two states are indistinguishable from outside.
  Archive with `move data/drafts/draft.jsonl data/drafts/draft.jsonl.archived`; never delete history.
- **The projection source is resumed from the log.** `source_changed` is replayed by `create_app`, so
  a relaunch mid-draft comes back on the board Marc chose rather than silently reverting to the
  default. A resume that cannot rebuild its board falls back LOUDLY and leaves `active_source` at the
  default, so the header never claims a source whose values failed to load.
- **Real 2026 team names are in `data/league_manual.yaml`** (`team_names:`), read off the Yahoo league
  page PDF. Marc's team is **Country Club Boys**; the app is titled **CC Boys Draft Room**.
  Name-to-slot mapping is UNKNOWN until the draw on draft night and is assigned in the UI.
- **`injury_status` NEVER touches the valuation on its own; a human override is the only lever.**
  The field is carried on `PoolPlayer` and rendered as a badge, and that is all it does. Whether a
  designation reduces a player's `expected_games` otherwise depends entirely on whether ESPN
  happened to price it in, which is accidental: on 2026-08-20 ESPN discounted Kittle (15 games)
  and Charbonnet (11) for PUP but projected Alec Pierce (PUP, ADP 70) for a full 17, so the board
  credited him the healthy-rank curve figure. Ricky Pearsall (IR) was excluded correctly but only
  by luck -- Sleeper happened to zero his stat line; nothing read his status. Nothing anywhere
  asserts what a designation COSTS -- no empirical figure is derivable from the cache, because
  Sleeper's designation is current-year and the only per-player games history is 2025 actuals.
- **Playing time is set by hand or not at all** (`valuation/playing_time.py`,
  `data/playing_time.json`, `docs/PLAYING_TIME.md`, added 2026-08-24). The rule is
  **`expected_games = min(the human's figure, curve(pos, rank))`**: the override replaces the
  active source's games figure -- including the `None` FantasyPros and FantasySharks leave, which
  makes it the only way to reach a player on those boards -- and the same fitted availability
  curve then clamps it. Downward passes straight through, because bad news is the point; **upward
  stops at the curve**, because claiming better-than-typical durability for a rank off a press
  report is the one error direction that inflates a player Marc then drafts at full value. That
  clamp is also why `check_expected_games_capped_by_curve` stays true BY CONSTRUCTION -- this
  feature loosened no gate to admit itself, and any future one must not either. **PPG is never
  touched**: an availability judgement moves the games VOLUME, and a "worse per game" view is a
  projection question for the review queue. The loader validates only that games is a
  non-negative real number and deliberately enforces **no maximum** -- the curve is the ceiling,
  and a second hardcoded one would be a number nobody derived. Fails closed exactly like the
  decisions file (missing = none; empty or malformed = raise, uncaught through the board build),
  and `player_id` may never be `null`: `decisions.py` gives null a real meaning (source-wide) and
  availability has no such grain, so the same shape here is refused rather than reinterpreted.
  Only overrides that actually MOVED a number are badged, and the "before" in that comparison is
  the **already-capped counterfactual**, not the raw source figure -- comparing against the raw
  number badged every override on a player the curve had already cut down (Josh Allen: source
  17.0, curve 16.6, so a clamped override read as a 17.0 -> 16.6 change it never made). An
  overridden player stops carrying an `injury_vs_expected_games` row and moves to
  `ReviewQueue.settled_by_override`, because handing Marc his own decision back as an open
  question is noise -- but the disappearance is reported, never silent.
- **`my_slot` is MOVABLE and his own seat is ALWAYS marked** (ledger #4, 2026-08-25). The draw
  happens at the table, so the slot arrives after launch: `POST /api/my-slot` appends a replayed
  `my_slot_set` event, and a relaunch comes back on the slot he SET rather than the one the command
  line carried. Two rules that must not regress. (1) `DraftBoard.my_slot` is a PROPERTY reading
  through to the replayed state, never a stored copy -- a copy would leave `is_mine` and
  `my_roster` pointing at the old seat while the state said otherwise. (2) `team_label` appends
  ` (YOU)` to his own slot when that slot also has a name; the old name-then-YOU precedence meant
  naming his own team ERASED the only marker saying which of the ten seats was his, and on draft
  night all ten seats have names. Three tests pinned the old rule and were deliberately rewritten.
- **A recommendation can be asked about ANY pick, and a preview must say so** (ledger #6). The
  engine reads `state.current_pick` and refuses when it is not Marc's turn, so
  `_call_recommend_engine(for_pick=N)` hands it a COPY of the state with the clock moved. The copy
  is deep enough to be harmless -- `dataclasses.replace` is SHALLOW, so it also copies the `picks`
  dict and every `Pick` in it; `recommend()` is read-only today, and the isolation must not depend
  on it staying that way mid-draft. Every off-clock answer carries `preview_for_pick` and is
  labelled on screen: it is computed against who is available NOW and does not simulate the
  intervening picks, so presenting it as live would be worse than the silence it replaced.
- **Dry runs go to a THROWAWAY log: `--log-path data/drafts/dryrun-<date>.jsonl`.** Never practise
  on the default `data/drafts/draft.jsonl`. `DraftNight.bat` deliberately uses the default, so it is
  the wrong launcher for a rehearsal -- a dry run through it leaves fake picks in the file the real
  draft opens against, and the only symptom is a board that looks subtly wrong in a room full of
  people.
- **`pkill -f draftroom.server` DOES NOT WORK on this machine.** It reports success, kills nothing,
  and the next launch fails to bind with `[Errno 10048]` buried in the log -- so curl keeps
  answering from the OLD build and verification silently tests stale code. This cost two wasted
  verification passes on 2026-08-25. Use PowerShell:
  `Get-NetTCPConnection -LocalPort 8484 -State Listen | Stop-Process -Id $_.OwningProcess -Force`,
  then confirm the new log has no `10048` before trusting anything it serves.
- **A NEW frontend served by an OLD backend is the nastier half of that trap, and it looks like your
  change did nothing.** Static assets are read from disk per request, so a killed-but-still-listening
  Python process serves the freshly built JS quite happily while running last build's Python — the
  new bundle hash verifies, the served markers verify, and the new API fields come back **empty**.
  Hit for real 2026-08-26 (`vona_by_pos: {}` on a correct implementation). **Verifying the bundle
  hash is not sufficient: check the LISTENING PROCESS'S START TIME** and confirm it is after your
  edit — `Get-NetTCPConnection -LocalPort 8484 -State Listen | %{ (Get-Process -Id
  $_.OwningProcess).StartTime }`. Launching the venv python spawns a child on the base interpreter
  and the CHILD holds the port; two rows for one launch is normal, not a duplicate.
- **A PowerShell tool call that times out kills the server it just launched**, even via
  `Start-Process ... -WindowStyle Hidden`. Launch the server in its own short call and poll health
  in a separate one; never bundle a launch with a 12-second sleep and a verification block that can
  run past the timeout. (Cost a restart cycle 2026-08-26.)
- **Re-run prep within a day or two of the draft, and run the AVAILABILITY JOB with it.** The
  injury picture is the fastest-decaying field in the whole pipeline and preseason cuts/IR
  designations land right up to kickoff. A pool cached three weeks before draft night will misvalue
  several players and give no sign of it. **`docs/FINAL_PREP.md` is the runbook** -- seven steps,
  exact commands, built 2026-08-25. `tools/injury_worklist.py` computes WHO to research (designated,
  source-implied-undesignated, ADP movers, blind top-N) and `tools/injury_sweep.py` turns cited
  external findings into overrides and contamination rejections. Do not rebuild either; run them.
  Availability has only ONE source (ESPN publishes the only per-player games figure), and suspension
  risk has none, so the news is the only way to see either.
- **TWO DIFFERENT ID SPACES ARE BOTH CALLED `player_id`, and confusing them fails SILENTLY.**
  `PoolPlayer.player_id` is FFC-derived; the crosswalk, `data/playing_time.json`,
  `data/projection_decisions.json`, `data/injury_research.json` and everything under `ReviewInputs`
  use the **Sleeper** id. Alec Pierce is `5641` in the first space and `8142` in the second, and
  `5641` in Sleeper's space is a teamless linebacker named Chris Worley. An override or rejection
  written against the wrong id binds to nobody, changes nothing, and looks like it worked.
  `tools/injury_worklist.py` prints the correct one; when hand-editing, resolve it from the Sleeper
  universe by name first.
- **A rejection badges only players whose OWN number changed, and the asymmetry is deliberate.**
  Filter `decisions_for(pid)` through `BlendProvenance.rejected_applied`, and match on
  `Decision.stats` (which expands the `"*"` sentinel) rather than `Decision.stat`. Two traps, both
  hit for real: badging every player a source-wide rule is *in force* for put a REJ on all 188 rows,
  and matching the raw `stat` silently missed whole-statline decisions entirely (13.4 dv of movement,
  no badge). Note that MORE players move than are badged -- removing a source shifts the positional
  replacement baseline, so players that source never published still nudge; badging them would point
  at a decision that did nothing to them. Pinned in `tests/test_decisions_wiring.py`.
- **The DECISIONS FILE FAILS CLOSED, and only a MISSING file means "no decisions."** An existing
  `data/projection_decisions.json` that is empty raises rather than reading as nothing rejected --
  an empty file is what a truncated write looks like, and failing open there silently un-applies
  every rejection Marc made while the board keeps looking fine. `player_id` is a REQUIRED key even
  when its value is `null` (source-wide): the file is hand-editable by design, and a dropped line
  used to promote one player's rejection into a rejection for every player that source publishes.
  `DecisionsFileError` propagates all the way through `live_data` -- it must never degrade to
  placeholder mode, which reads as "the cache is stale" rather than "your decisions stopped
  applying" (Codex 2026-08-21).
- **A source whose board did not build is REFUSED, never served under its own name.** A failed
  board build degrades to an ADP-placeholder pool, so the paths that make a source *active* (the
  toggle and the mid-draft resume) use `sources.pool_for_source_strict`, which raises
  `SourceUnavailable` when a pool valued nobody. `available_sources()` keeps the lenient accessor --
  describing broken sources is its whole job. `source_changed` is appended and fsync'd BEFORE the
  in-memory pool moves, so a failed disk write can never leave the running app on one source while
  replay rebuilds another.
- **`out_of_order` is COMPUTED at replay, never trusted from the payload.** It means "this pick did
  not go to the team on the clock" -- `team_slot != snake.slot_on_clock(teams, pick_no)`.
  Click-anywhere drafting always supplies a slot (the picker defaults to whoever is on the clock),
  so the old `out_of_order = team_slot is not None` badged every ordinary pick OOO.
- **"Undraft" is TWO acts and the newest pick gets the other one.** `/api/undraft` appends an `undo`
  when the target is the newest pick event, so replay drops it and the clock returns on its own;
  for an older pick it appends `pick_voided` and leaves a hole `gaps()` reports. Voiding the newest
  pick instead left the clock advanced, so the replacement landed at the next pick number for the
  next team and the whole board drifted one slot against the physical one, silently. Always ONE
  appended event -- a void+clock_set pair can be torn in half by a crash. Reassigning ownership is
  its own `pick_reassigned` event for the same reason: `pick_corrected` carries identity, and
  reusing it meant a reassign-only request was rejected 422.
- **The session scratchpad is SHARED across parallel agents.** Two agents both writing `probe7.py`
  cost a 9-minute stall on 2026-08-20. Prefix scratch filenames per agent.
- **A stale-team field is a recurring trap.** A player's `team` in any current-season payload is his
  CURRENT team, not the team he played for in a past season (Mike Evans reads SF, played 2025 for TB).
  Sleeper hides this twice over: the 2025 projection row's own team field is right, while the embedded
  `row["player"]["team"]` is the live 2026 record and agrees with the real 2025 team only 77.2% of the
  time. Any team-level aggregation of a past season needs per-week attribution. This has produced a
  confident, completely wrong answer at least twice.
