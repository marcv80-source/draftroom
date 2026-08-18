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
3. **Sanity invariants** on every data refresh: top QB lands top-8 overall; the 2-QB shift is
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
| ESPN | none | stat-line projections **incl. targets** | `prep/espn_client.py`. `lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/<yr>/segments/0/leaguedefaults/3?view=kona_player_info` + `X-Fantasy-Filter` header. 461 players. **Source of record for projections.** |
| Mike Clay PDF | none | same numbers as ESPN, rounded | `prep/clay_pdf.py` over the staged draft-kit PDF in `data/manual/`. Keep it **only** as the offline fallback — see the same-source warning below. |
| DynastyProcess `ff_playerids` | none | cross-source ID crosswalk | The join key for everything. |
| Yahoo | OAuth2 `fspt-r` | league settings, scoring, rosters, **prior-season draft pick-by-pick** | Access gated by manual application. |

`nfl_data_py` is **dead** (archived Sep 2025). Use **nflreadpy** for historical stats.

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
