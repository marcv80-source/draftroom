# draftroom

Personal fantasy football draft model + live draft assistant for Marc's league. Draft is **2026-09-08**, in person, stickers on a board. This tool is the only live record of what's gone.

## The league (drives everything)

Yahoo-hosted snake draft, in person. **Two mandatory starting QBs. No kickers. No defenses. Short bench.**
Exact team count, roster slots, scoring, and draft slot come from the Yahoo API and are **never hardcoded** —
`LeagueConfig` is constructed at runtime. Until Yahoo access lands, `data/league_manual.yaml` holds
hand-entered settings read off the Yahoo web UI.

Working assumption until confirmed: 12 teams, starters `{QB:2, RB:2, WR:3, TE:1, FLEX(RB/WR/TE):1}`, bench 5, 17 weeks.

**Why this matters:** 12 teams x 2 QB x 17 weeks = 408 QB-games of demand, so replacement-level QB is
around QB26-28, not ~QB17. Every public ranking is built for 1-QB leagues with K and DST and is wrong here.
That gap is the entire edge. If a number looks like a normal league's number, something is broken.

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
3. **Sanity invariants** on every data refresh: top QB lands top-8 overall; 10-14 QBs in the top 30;
   baselines move monotonically with team count and starter slots; survival is monotone and `S(n0)/S(n0)==1`;
   per-game fixture (high-PPG/few-games beats low-PPG/many-games at equal season totals).

Never present a number that hasn't passed these. State which checks ran.

## Conventions

- Python 3.12. Windows. Paths contain spaces elsewhere on this machine but **this repo lives at `C:\dev\draftroom`** — never under OneDrive.
- Secrets in `%LOCALAPPDATA%\draftroom\` (`secrets.json`, `yahoo_token.json`). **Never in the repo.**
- Yahoo rotates the refresh token on **every** refresh. Persist the new one atomically (temp file + `os.replace`) or you get locked out.
- Yahoo JSON is a literal XML transliteration: collections arrive as `{"0":{...},"1":{...},"count":2}`. Normalize at the boundary; no Yahoo shape leaks past `prep/yahoo_normalize.py`.
- Raw fetches cache to `data/raw/<source>/<UTC-timestamp>.json`. Never re-fetch in a test.
- SQLite everywhere. Snapshots are immutable; draft state is an append-only JSONL event log, fsync'd before the UI acknowledges.
- Attribution required in the UI footer and on the cheat sheet: "Fantasy data provided by Yahoo Fantasy", plus Fantasy Football Calculator for ADP.

## Data sources

| Source | Auth | Gives | Notes |
|---|---|---|---|
| Sleeper | none | player universe + IDs, season projections | `api.sleeper.app/v1/players/nfl` (~5MB, cache daily). Projections on `api.sleeper.com` are undocumented and may break. |
| Fantasy Football Calculator | none | **2QB ADP + std_dev** | `/api/v1/adp/2qb?teams=12&year=2026`. Free for personal use, refreshes daily. The std_dev is what the survival model needs. |
| FantasyPros | `x-api-key` | raw stat-line projections + consensus ECR | premium $8.99/mo, one month. |
| DynastyProcess `ff_playerids` | none | cross-source ID crosswalk | The join key for everything. |
| Yahoo | OAuth2 `fspt-r` | league settings, scoring, rosters, **prior-season draft pick-by-pick** | Access gated by manual application. |

`nfl_data_py` is **dead** (archived Sep 2025). Use **nflreadpy** for historical stats.

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

## Tone of the output

The engine **informs, never insists**. Every recommendation ends with the fallback. Marc is in the room and
knows things the model doesn't. Explanations are 2-3 scannable bullets, each backed by one computed number,
never a wall of tables.
