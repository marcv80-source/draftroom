# draftroom

Personal fantasy football draft model + live draft assistant for Marc's league. Draft is **2026-09-08**, in person, stickers on a board. This tool is the only live record of what's gone.

## The league (drives everything)

Yahoo-hosted snake draft, in person. **Two mandatory starting QBs. No kickers. No defenses. Short bench.**
Exact team count, roster slots, scoring, and draft slot come from the Yahoo API and are **never hardcoded** —
`LeagueConfig` is constructed at runtime. Until Yahoo access lands, `data/league_manual.yaml` holds
hand-entered settings read off the Yahoo web UI. **That file is the source of truth — read it, don't
paraphrase it here.**

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
endpoint returns nothing there. Correcting for the mismatch is the opponent model's job.

## Two phases — the load-bearing architecture idea

- **PREP** (online, run often): fetch -> resolve IDs -> score -> composite -> model -> freeze a **snapshot** -> regenerate cheat sheet.
- **DRAFT** (offline, draft night): opens the newest snapshot **read-only**, refuses to start if the
  reconciliation gate failed, asserts no outbound network. Writes only to a separate draft event log.

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

1. **Scoring reconciliation.** Re-score ~12 players' actual 2025 stats with the engine + last season's
   modifiers; assert within 1.0 point of Yahoo's recorded season totals. A snapshot that fails is unloadable.
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
- Secrets in `%LOCALAPPDATA%\draftroom\` (`secrets.json`, `yahoo_token.json`). **Never in the repo.**
- Yahoo rotates the refresh token on **every** refresh. Persist the new one atomically (temp file + `os.replace`) or you get locked out.
- Yahoo JSON is a literal XML transliteration: collections arrive as `{"0":{...},"1":{...},"count":2}`. Normalize at the boundary; no Yahoo shape leaks past `prep/yahoo_normalize.py`.
- Raw fetches cache to `data/raw/<source>/<UTC-timestamp>.json`. Never re-fetch in a test.
- **Running `prep/fetch_all.py` live has a side effect on the test suite.** It writes new timestamped files
  into `data/raw/`, which moves what `load_latest_raw()` resolves to, which breaks any test that reads
  cached raw data (`test_live_model.py`, `test_bonuses.py`). Hit for real 2026-08-17. If you run a live
  fetch to smoke-test something, either delete the files you created afterwards or expect red tests that
  have nothing to do with your change.
- SQLite everywhere. Snapshots are immutable; draft state is an append-only JSONL event log, fsync'd before the UI acknowledges.
- Attribution required in the UI footer and on the cheat sheet: "Fantasy data provided by Yahoo Fantasy", plus Fantasy Football Calculator for ADP.

## Data sources

| Source | Auth | Gives | Notes |
|---|---|---|---|
| Sleeper | none | player universe + IDs, season projections | `api.sleeper.app/v1/players/nfl` (~5MB, cache daily). Projections on `api.sleeper.com` are undocumented and may break. |
| Fantasy Football Calculator | none | **2QB ADP + std_dev** | `/api/v1/adp/2qb?teams=12&year=2026`. Free for personal use, refreshes daily. The std_dev is what the survival model needs. |
| FantasyPros | **none — manual CSV** | raw stat-line projections | **Decided 2026-08-17: no API, no subscription.** Marc downloads the Half-PPR projections tables by hand into `data/manual/`; `prep/manual_csv.py` ingests them. See `docs/MANUAL_PROJECTIONS.md`. No targets column. |
| ESPN | none | stat-line projections **incl. targets** | `prep/espn_client.py`. `lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/<yr>/segments/0/leaguedefaults/3?view=kona_player_info` + `X-Fantasy-Filter` header. 461 players. Omitting `sortDraftRanks` from the filter returns HTTP 400. Past seasons work by swapping `<yr>`, and `statSourceId` 1 = projection, 0 = actuals (`statSplitTypeId` 0 = season total) — that is how the 2025 backtest is possible. |
| FantasySharks | none | stat-line projections **incl. targets**, plus projected counts of games clearing each yardage threshold | `prep/fantasysharks_client.py`, 516 players. **Verified independent 2026-08-20** against a positive control (ESPN vs Clay reproduced at 99.8% agreement; Sharks agrees with all three others at 0.0–0.2%). `Segment` changes yearly and must be read from the page's own `<select>` — never hardcode it. `Position=4` is WR, not 3. |
| Mike Clay PDF | none | same numbers as ESPN, rounded | `prep/clay_pdf.py` over the staged draft-kit PDF in `data/manual/`. Keep it **only** as the offline fallback — see the same-source warning below. |
| DynastyProcess `ff_playerids` | none | cross-source ID crosswalk | The join key for everything. |
| Yahoo | OAuth2 `fspt-r` | league settings, scoring, rosters, **prior-season draft pick-by-pick** | Access gated by manual application. |

`nfl_data_py` is **dead** (archived Sep 2025). Use **nflreadpy** for historical stats.

**There is NO single "source of record" for projections — the COMPOSITE is.** This file previously
called ESPN the source of record while `build_real_board` in fact used Sleeper alone, and neither
was right. Since 2026-08-20 the default board is the **equal-weight component-stat composite**
(`valuation/composite.py`, `DEFAULT_BOARD_SOURCE = "blend"`), and `RealBoard.source` /
`active_source` always name the board actually being served. Equal weighting is **measured, not
assumed**: the 2025 backtest (`docs/SOURCE_BACKTEST.md`) put the best-weight bootstrap interval at
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
  (the only thing a board consumes) got slightly worse. See `docs/RENORMALIZATION_VERDICT.md`,
  `docs/SOURCE_BACKTEST.md`.
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
  `room_priors_2025.json`, `opponent_calibration_2025.json`).

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
- **`injury_status` is INFORMATIONAL ONLY -- it never touches the valuation.** It is carried on
  `PoolPlayer` and rendered as a badge, and that is all. Whether a designation reduces a player's
  `expected_games` depends entirely on whether ESPN happened to price it in, which is accidental:
  on 2026-08-20 ESPN discounted Kittle (15 games) and Charbonnet (11) for PUP but projected Alec
  Pierce (PUP, ADP 70) for a full 17, so the board credited him the healthy-rank curve figure.
  Ricky Pearsall (IR) was excluded correctly but only by luck -- Sleeper happened to zero his stat
  line; nothing read his status. `candidates.py`'s `injury_vs_expected_games` detector now flags
  the inconsistency as non-actionable (the fix is a playing-time override, not a source rejection),
  and asserts NOTHING about what a designation costs -- no empirical figure is derivable from the
  cache, because Sleeper's designation is current-year and the only per-player games history is
  2025 actuals.
- **Re-run prep within a day or two of the draft.** The injury picture is the fastest-decaying
  field in the whole pipeline and preseason cuts/IR designations land right up to kickoff. A pool
  cached three weeks before draft night will misvalue several players and give no sign of it.
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
