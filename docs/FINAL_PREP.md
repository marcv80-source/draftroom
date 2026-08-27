# Final prep — the availability job

**Draft: 2026-09-08.** This is the one part of prep that is genuinely calendar-gated, because it
depends on news that has not happened yet. Everything else in this repo can be run whenever.

Built 2026-08-25 after Marc's observation that sources lag injury news *independently*, so a
player can be out for the season while two of four sources still project a full one.

---

## Why this is a recurring job and not a task that got done in August

The exposure is not symmetric across the pipeline, and that is what makes it worth a runbook:

- **Availability has ONE source.** Sleeper reports a blanket `18.0` games for every player, and
  FantasyPros and FantasySharks publish no games column at all. **ESPN is the only source with a
  real per-player games figure.** If ESPN lags, nothing downstream notices, because there is no
  second opinion to disagree with it.
- **The statline has four sources**, any one of which can keep a full season of yards alive for a
  player who is done.
- **Suspension risk has zero sources.** No feed we fetch prices it. It is only ever found by
  reading the news.

And the news arrives on a schedule:

| Date | What happens | Why it matters here |
|---|---|---|
| **Sun Aug 30, 5pm CT** | 53-man cutdown | active/PUP either clears or converts to **reserve/PUP**, a four-game minimum. This single deadline resolves most PUP cases. |
| Aug 30 – Sep 7 | Final cuts, IR designations, preseason Week 3 | The highest-density window for exactly the news this job screens for. |
| **Sep 6 or 7** | **Run this whole job** | Late enough to catch the cutdown, early enough to act on it. |
| Sep 8 | Draft | Too late to research. |

**Run the job at least twice: once now, and again Sep 6 or 7.** A run before Aug 30 cannot know
how the PUP cases resolve, so its partial-absence numbers are provisional by construction.

---

## The job, in order

### 1. Refresh the data

```
Prep.bat
```

Marc's only manual step is inside it: the four FantasyPros CSVs are a hand download. See
`docs/MANUAL_PROJECTIONS.md`. Everything else is automatic.

**The suite must stay green through a refresh.** It used to red out on every fetch, because several
tests pinned live-feed values (an ADP, a record count) beside the properties they existed to check.
Those were re-anchored to structural assertions on 2026-08-25 and the settled-analysis tests that
could never survive a refresh were deleted. So a failure here is now a real failure: **read it, do
not wait for it to pass.** A refresh that reddens pytest is a bug in the test or the data, and the
previous version of this line told you to expect it, which is exactly how a real break would have
hidden.

### 2. Build the worklist

```
.venv\Scripts\python.exe tools\injury_worklist.py --top 60 --movers 15 --prompts --out data\review\worklist.txt
```

Four categories, none of which uses an invented threshold:

- **A — designated.** Every ranked player carrying an injury designation.
- **B — source-implied, undesignated.** No designation, but his games figure was still cut below
  the healthy-rank curve. Only ESPN can leak this, and a discount with nothing said out loud is
  the shape of news arriving early.
- **C — ADP movers.** The market, computed free from two cached FFC pulls. A player falling hard
  is a player the internet knows something about. On the Aug 14 → Aug 25 pair this caught Jordyn
  Tyson falling 11.9 spots while our own feed still called him merely "Doubtful".
- **D — blind top-N.** Because the designation feed lags too, the top of the board is checked
  whether or not anything flagged it. The catastrophic error is an early-round pick who is out.

`--top` and `--movers` are **depths, not cutoffs**, and the report prints what it did not examine.
A shallow run must never read as a clean sweep.

The worklist carries each player's **Sleeper `player_id`**. Use it. See the ID trap below.

### 3. Do the research

`--prompts` emits ready-to-paste prompts, batched. Run them as parallel subagents with web
access. Session model, not a cheap one: a wrong "he's fine" passes silently into a pick.

Rules baked into the prompt template, all of which earned their place:

- **"No recent reporting found" is a real answer.** Never let a model fill a gap from training
  knowledge, which is months stale.
- **Every status claim traces to a fetched URL with a date.**
- **Say when a tag is trivial.** Over-discounting a healthy star is its own expensive error, and
  in the Aug 25 run five of the flagged players were simply stale tags (Mahomes, McCaffrey,
  Metcalf, Judkins, Warren).
- **Flag suspension, free agency, cuts, and trades**, not just injuries.

### 4. Write the research file

`data/injury_research.json`. One entry per player, and the loader **refuses** an entry with no
`report_date` and no `citation` — an injury claim with no source is the unverifiable input this
whole file exists to keep out. `player_id` is required and may never be null.

**`games_missed: null` means UNPRICED, and it is not the same as `0`.** Use it when something is
known and no honest games figure exists for it -- an open disciplinary review has no timeline, a
camp battle has no date. `0` is a positive claim that he plays the full season; omitting the field
still means `0`. An unpriced finding changes **no number anywhere**: the sweep proposes nothing for
it, and the board renders it as a `RISK` badge carrying the citation, so the judgement is visible
in the room instead of sitting in this file. `season_ending: true` may never be unpriced -- that
finding IS a games figure (zero played). See `backend/draftroom/valuation/injury_research.py`.

This is the only lever for the category the table above says has **zero sources**. Suspension,
discipline, a roster decision that has not happened: research is the only way to see it, and
before 2026-08-27 there was nowhere to put the answer.

### 5. Sweep

```
.venv\Scripts\python.exe tools\injury_sweep.py --json data\review\sweep.json
```

Report only. It cross-checks every source against the research and proposes an action per player,
by arithmetic rather than by threshold:

- **Season-ending** → `games: 0`, which zeroes value while leaving the player on the board so he
  can still be *recorded*. Plus a **contamination** rejection for any source still publishing
  production, which is a failed identity ("plays zero games, projects 1,200 yards") rather than an
  outlier, and so needs no distance measure.
- **Partial absence** → `games: weeks - missed`, curve-clamped. **The statline is untouched**,
  because only the volume changed and PPG is never moved by an availability judgement.

### 6. Apply

```
.venv\Scripts\python.exe tools\injury_sweep.py --apply --only-severe    # the settled ones
.venv\Scripts\python.exe tools\injury_sweep.py --apply                  # everything
```

**Before Aug 30, use `--only-severe`.** A season-ending injury is settled fact the moment it is
reported; a PUP or soft-tissue timeline is still moving. Deferred rows stay in the research file
with their citations and are reported by name, so nothing exits silently.

### 7. Verify

```
.venv\Scripts\python.exe tools\run_invariants.py          # must be 8/8 PASS
.venv\Scripts\python.exe -m pytest -q
```

Then **look at the board**. An applied decision must be visible: a playing-time override badges
as `NN.NG`, a rejection as `REJ`, and a finding that produced NEITHER badges as `RISK` (unpriced)
or `-NG?` (a real figure nobody has applied yet -- deferred, or clamped away). And read the WARNING lines — the board build logs any override
that **matched nobody and did nothing**, which is the failure mode a silent success looks
identical to.

---

## The rules that keep this honest

**The research is authoritative DOWNWARD ONLY.** If a source already credits fewer games than the
reporting implies, it is not behind — it is being more careful than the beat writer, and raising
the figure to match a press report is the one error direction that inflates a player you then
draft at full price. The sweep refuses upward overrides and says so. This caught a real mistake on
2026-08-25: Tyson's research implied 12 games, ESPN already said 10, and the first version of the
tool proposed *raising* him.

**Availability and rate are different questions.** "He will miss four games" is a games override.
"He will be worse per game when he plays" is a projection question for the review queue. Tucker
Kraft in the Aug 25 run is the clean example: he will likely play all 17 at a suppressed
early-season snap share, so his games figure is right and his PPG is the thing that is wrong.

**Two ID spaces are both called `player_id`.** `PoolPlayer.player_id` is FFC-derived;
`injury_research.json`, `playing_time.json`, `projection_decisions.json` and the crosswalk all use
the **Sleeper** id. Alec Pierce is `5641` in the first space and `8142` in the second, and `5641`
in Sleeper's space is a teamless linebacker named Chris Worley. An override against the wrong id
binds to nobody and fails **silently**. The worklist prints the right one.

**An override on a player with no projection is inert but not useless.** Pearsall and Higgins are
off the valued board entirely because no source published them, so their `games: 0` changed
nothing on 2026-08-25 and the board said so at WARNING. Keep them: the override becomes
load-bearing the moment a later refresh re-publishes a row for them.

**Do not add an official-looking injury page as a source.** NFL injury reports do not begin until
the regular season, so sites rendering "no injuries to report" for all 32 teams in August are
showing an empty schema, not a clean bill of health.

**Never invent a games figure for a designation.** Nothing in this repo asserts what PUP or IR
costs, and it is not fittable from the cache (`candidates.NO_EMPIRICAL_DESIGNATION_FIT`). Every
number in `playing_time.json` traces to a dated citation about one player, or it should not be
there.

---

## Known open items as of 2026-08-25

- **Applied:** Pearsall and Higgins at `games: 0` (both season-ending IR, two outlets each).
- **Deferred to the Aug 30 cutdown:** Alec Pierce (→ 13.0) and Zach Charbonnet (→ 7.0). Both are
  active/PUP, so the cutdown decides whether they miss ~0 games or a four-game minimum.
- **Carried as a badge (decided 2026-08-27):** Josh Jacobs (ADP 34.6) and Puka Nacua (ADP 4.8) are
  both under open NFL disciplinary review. Nothing we fetch can see this. Marc chose a badge over a
  games haircut, because any haircut number would be invented -- measured, four games would have
  moved Nacua from #4 to #11. They are in the research file with `games_missed: null`, and the
  board shows them as `RISK`. **Both cite the ledger rather than a primary URL; replace those
  citations with real links on this run.**
- **Volume-not-availability:** Tucker Kraft. Review queue, not this job.
