# The outlier review queue

Built 2026-08-20 against `docs/archive/PLAN_2026-08-20.md`, "Marc's decisions, round 2". His words:

> I'd like to have outliers brought to me and highlighted and then we make decisions around
> whether to boot it or not.

**Nothing is ever rejected automatically.** The pipeline surfaces candidates, Marc adjudicates,
his decisions persist in `data/projection_decisions.json`, and that file feeds
`blend_statlines(rejected=...)`. Every number that leaves the composite left because a human
wrote a line saying so.

That is not a stylistic choice. Distance-based auto-rejection is not statistically sound at a
small number of correlated sources (the smallest well-defined symmetric trim of three forecasts
is just the median; Stock & Watson 2004 measured a "drop the worst source" screening rule doing
*worse* than plain averaging in three of six cases). And this repo has already declined two
proposed automatic corrections — the per-position calibration shrink (`docs/archive/SOURCE_BACKTEST.md`)
and identity renormalization (`docs/archive/PLAN_2026-08-20.md`, VERDICT section) — for failing to beat a
dumb null of the same magnitude. A human deciding case by case is subject to neither objection,
because no rule is being fitted.

Reproduce everything below with:

```
.venv\Scripts\python.exe tools\review_outliers.py [--limit 300] [--json queue.json] [--no-open]
.venv\Scripts\python.exe tools\review_outliers.py --apply <pasted.json>
```

Modules: `backend/draftroom/valuation/candidates.py` (detection + aggregation),
`backend/draftroom/valuation/decisions.py` (persistence).
Tests: `tests/test_candidates.py` (49), `tests/test_decisions.py` (24).

**PREP time only.** Draft night opens a frozen snapshot read-only and asserts no outbound
network; nothing on a draft-phase code path touches this tool. Adjudicating a projection at the
table is the wrong moment anyway.

---

## The loop

1. **Detect.** Seven detectors, none of which can do anything but flag. Every one already existed
   before this queue did; the queue's job is to normalise their very different outputs into one
   candidate type at one grain.
2. **Rank by board impact** — what actually happens to that player's draft value if the flagged
   source were dropped for that stat. Not by statistical extremity. A 40% disagreement on the
   180th player is not worth Marc's attention; a 6% disagreement on a third-rounder is.
3. **Present** in one self-contained HTML page, keep/reject per row, **every control defaulting
   to keep**. Open it, click nothing, and no number changes.
4. **Persist** the touched rows to `data/projection_decisions.json` via `--apply`.
5. **Apply** as the `rejected` set the composite already accepts, and render every applied
   decision on the board as a visible badge. Never silently folded in.

---

## What the detectors found on the real cached board (2026-08-20)

`1,171` findings, merged to **1,152 pending decisions** (one row per `(source, stat, player)` —
two rows for one number would invite two contradictory decisions about it). Whole run: **2.0 s**,
every row carrying a real board revaluation.

| detector | rows | severity | what it is |
|---|---:|---|---|
| `distance` | 601 | distance | one source far from the median of the others, on one stat |
| `identity_hygiene` | 495 | hygiene | a team catching more passes than it threw |
| `td_regression` | 43 | badge | projected TDs outside the fitted historical dispersion |
| `band_hygiene` | 18 | hygiene | a team sum above the fitted plausible band |
| `crosswalk_missing_source` | 5 | defect | a top-200-ADP player a source has no row for |
| `td_source_bias` | 4 | distance | a source's whole-board TD level against its own yardage |
| `injury_vs_expected_games` | 4 | defect / hygiene | a will-not-play designation the valuation never read |
| `contamination_constant` | 1 | defect | a constant published as a projection |
| `contamination_zero_statline` | **0** | defect | an all-zero statline carried with positive games |
| `crosswalk_unresolved` | **0** | defect | an FFC row resolving to no player at all |

Suppressed by injury status, counted and named rather than silent: **1**
`contamination_zero_statline` row and **5** `crosswalk_missing_source` rows.

`crosswalk_unresolved` at zero is CLAUDE.md gate #2 passing: no unresolved skill-position player
inside the top 200 by ADP. The Kenny Gainwell case from earlier the same day (ADP 132.8,
FantasySharks publishing a fuller first name that scored 86.7 against the crosswalk's 90.0 floor)
is already fixed by a `data/overrides.csv` entry, so this detector correctly finds nothing — and
would find it again if the override were removed.

### Two detectors flood, and that is the finding

`distance` (601) and `identity_hygiene` (495) are both over the queue's flood threshold, and the
queue says so in its own `notes` rather than quietly truncating. Neither count is a bug:

* **`distance` at 601** is 189 ranked players x 16 canonical stats x four sources, filtered to the
  rows where one source sits at least 30% from the other three's median. The 30% is a **display**
  threshold, adjustable with `--distance-rel-min`, and it selects what is looked at, never what is
  dropped. Board-impact ranking is what makes 601 rows usable: the top 20 are almost all one
  source and one stat, and the tail cannot move a pick.
* **`identity_hygiene` at 495** is ~165 team-level identity violations across four sources, each
  naming the top 3 players contributing to that team's total. The violation localises to a
  **team**, so the players are how a decision could act on it at all.

**How the page handles it:** rows are sorted by absolute movement in draft value, descending; the
page renders the top 150 by default (`--limit`), guarantees at least 8 rows from *every* detector
so a flooding detector cannot push a smaller one off the page entirely, and prints how many rows
were omitted. Filter chips carry each detector's full count.

### Top 10 by board impact

| # | player | flagged | detectors | board impact if rejected |
|---:|---|---|---|---|
| 1 | **Alec Pierce** (WR, ADP 70.3) | playing_time / `games` | injury_vs_expected_games | not a delta — see below |
| 2 | Brandon Aiyuk (WR, ADP 148.7) | playing_time / `games` | injury_vs_expected_games | not a delta — see below |
| 3 | Jacoby Brissett (QB, ADP 106.8) | fantasysharks / `pass_td` | distance + td_regression | dv −27.7 → −41.3 (−13.6), rank 143 → 164 |
| 4 | Kyler Murray (QB, ADP 56.0) | fantasysharks / `pass_td` | distance + td_regression | dv 15.1 → 2.8 (−12.4), rank 76 → 93 |
| 5 | Bo Nix (QB, ADP 30.9) | fantasysharks / `pass_td` | td_regression | dv 40.1 → 27.9 (−12.2), rank 46 → 59 |
| 6 | Caleb Williams (QB, ADP 22.9) | fantasysharks / `pass_td` | td_regression | dv 39.5 → 27.7 (−11.7), rank 47 → 59 |
| 7 | Cam Ward (QB, ADP 94.6) | fantasysharks / `pass_td` | distance + td_regression | dv −15.5 → −26.7 (−11.2), rank 119 → 140 |
| 8 | Patrick Mahomes (QB, ADP 19.7) | fantasysharks / `pass_td` | td_regression | dv 42.7 → 31.9 (−10.8), rank 43 → 58 |
| 9 | Aaron Rodgers (QB, ADP 112.4) | fantasysharks / `pass_td` | distance + td_regression | dv −23.5 → −34.1 (−10.6), rank 138 → 152 |
| 10 | Malik Willis (QB, ADP 76.2) | fantasysharks / `pass_td` | distance | dv −2.5 → −12.9 (−10.5), rank 104 → 115 |

Two things fall out of that table.

**The top two rows are not valuation disagreements at all** — they are players whose playing time
the pipeline never discounted for a designation it was already carrying. See the playing-time
section below; that detector exists because of them.

**FantasySharks' passing touchdowns are the biggest thing on this board.** The aggregate row is
unambiguous, and it reproduces from the same fitted 2025 model `docs/archive/PROJECTION_CHALLENGES.md`
used:

| source | n QBs | QB `pass_td` summed above the usage floor | fitted expectation from its own `pass_yd` | ratio | aggregate z |
|---|---:|---:|---:|---:|---:|
| sleeper | 34 | 768.0 | 764.4 | 1.005 | +0.16 |
| espn | 34 | 738.7 | 836.4 | 0.883 | **−4.32** |
| fantasypros | 34 | 800.9 | 822.6 | 0.974 | −0.97 |
| **fantasysharks** | 33 | **1,084.5** | 786.3 | **1.379** | **+13.59** |

(Reproduce the full table, including the rows below the display threshold, with
`detect_td_source_bias(inputs, z_min=0.0)`. The sleeper, espn and fantasypros rows match
`docs/archive/PROJECTION_CHALLENGES.md` figure for figure, which is the check that this module is reading
the same fit and not a new one.)

FantasySharks projects **35% more quarterback passing touchdowns than the next-highest source**
and 37.9% more than its own passing yardage buys at the 2025 rate. Per player that reads as Jared
Goff 36.9, Joe Burrow 42.6, Jacoby Brissett 37.5 on 3,504 passing yards. The column mapping is
*not* the culprit: FantasySharks' own distance buckets (`0-9`, `10-19`, ... `50+ Pass TDs`) sum to
that same figure on every row checked, so the parser is reading the column the source publishes.
It is the source's number.

Two corroborating angles that make this a level bias rather than a parsing artifact. Over
FantasySharks' whole published QB table (78 quarterbacks, 1,224.1 passing TDs on 127,937 passing
yards) the rate is **0.957 TD per 100 passing yards** against the fitted 2025 rate of **0.679**.
And the ESPN row in the same table reproduces the documented −4.32 exactly, which is what says the
machinery is wired to the same fit rather than to something new. **Not a recommendation** —
it is one review row, and Marc decides. But it is the row to look at first, and rejecting
`(fantasysharks, pass_td)` source-wide is a single click that moves 36 players.

### The playing-time gap (`injury_vs_expected_games`)

`injury_status` is carried on `live_data.PoolPlayer` and emitted in the server payload, and until
this detector existed it **never touched the valuation** — grep it and it appears in
`live_data.py` (carrying it) and `server.py` (the payload) and nowhere else. So whether a
will-not-play designation reached `expected_games` depended entirely on whether ESPN happened to
price it in. Accidental, not principled.

Measured on the ranked pool. Five players carry a long-term designation; the fitted
rank-conditional availability curve figure is what the board credits a player nobody knows
anything about:

| player | ADP | designation | ESPN games | board credited | healthy-rank curve | verdict |
|---|---:|---|---:|---:|---:|---|
| **Alec Pierce** | **70.3** | **PUP** | **17.0** | **15.50** | **15.50 (WR30)** | **defect — zero discount** |
| Brandon Aiyuk | 148.7 | DNR | no row | 8.46 | 8.46 (WR78) | defect — zero discount |
| George Kittle | 125.7 | PUP | 15.0 | 15.00 | 15.94 (TE4) | hygiene — 0.94 games priced in |
| Zach Charbonnet | 137.0 | PUP | 11.0 | 11.00 | 14.77 (RB26) | hygiene — 3.77 games priced in |
| Ricky Pearsall | 118.0 | IR | no row | — | — | off the board; no row, see below |

Alec Pierce is the live one and he is **row 1 of the queue**: a top-75 receiver on PUP credited
with 15.50 of 17 games purely because the availability curve cannot read a designation and ESPN
projected him for a full season.

Three design constraints, each of which is a property of the code and not a promise:

* **It is not a `(source, stat)` rejection and does not pretend to be.** No source's number is
  wrong here — ESPN's 17.0 is an ordinary if-healthy projection — and
  `blend_statlines(rejected=...)` cannot express "the availability figure is wrong" at all. So
  `actionable: false`, the row's `source` is the pseudo-source `playing_time`, and the reason says
  the fix is a playing-time override. The strongest form of that: `decisions.parse_decisions`
  **refuses** an entry naming `playing_time`, because it is not a known composite source. There is
  no way to record a rejection here even by hand-editing the file.
* **No threshold is invented.** Nothing asserts what a PUP designation costs in games, and it is
  not fittable from this repo's cache: Sleeper's `injury_status` is the CURRENT (2026 preseason)
  designation, the only cached per-player games history is 2025 actuals from the ESPN payload, and
  matching a 2026 designation to a 2025 games count measures nothing about either. The cached
  nflreadpy weekly CSVs carry no injury column at all. Said plainly in
  `NO_EMPIRICAL_DESIGNATION_FIT`, carried on every row's `detail`, and quoted in every reason
  sentence. What the detector does instead is a **consistency comparison between two numbers the
  pipeline already produced** — the games the board credited, and the healthy-rank curve figure —
  both printed in the reason, so Marc judges the two numbers rather than a verdict. Severity
  splits on that comparison alone: `defect` at exactly zero discount, `hygiene` when a source
  priced something in and the only open question is whether it priced in *enough*.
* **Ranked honestly.** Impact is `computable: false` with a note, because nothing is being
  dropped, so these rows use the same machinery as the not-on-the-board rows and sort by **ADP**
  among them. A PUP player at ADP 70 sorts above one at ADP 180, and both sort above nothing that
  has a measured `dv` delta at all — which is why Pierce is row 1 and Kittle (ADP 125.7, already
  discounted, hygiene) is row 1039.

Short-term game-status tags never fire. 28 of the 33 designated players in the ranked pool carry
`Questionable` in August — Puka Nacua, Christian McCaffrey, Patrick Mahomes among them — and a
detector that fires on those is noise with a severity label on it. Sleeper's full observed
vocabulary in the cached universe is `DNR, IR, NA, Out, PUP, Questionable, Sus`. An
**unrecognised** code is surfaced (the safe direction for a row that only ever informs) but is
never allowed to excuse missing data anywhere else.

### Injury status now gates the two contamination detectors

Four of the previous top ten rows were Ricky Pearsall, one per source, and every one was a false
positive once Marc supplied what the data could not: **he is out for the season.** An all-zero
statline for a healthy starter is contamination; for a player on IR it is the truth. A source
declining to publish him is a source being right.

So `contamination_zero_statline` and `crosswalk_missing_source` both skip players carrying a
recognised long-term designation. On the current board that removes 1 + 5 rows, and
`crosswalk_missing_source` drops from 10 to 5. Without the gate every IR player generates four
top-ranked false alarms and crowds out real findings.

Two guardrails so the gate cannot hide anything:

* **Suppressions are counted**, per detector, on `ReviewQueue.suppressed_by_injury`, in the JSON
  payload, in a queue `note`, and in the page summary. A suppression Marc cannot see is
  indistinguishable from a detector that stopped working.
* **A designated player who is off the board entirely is named in a note.** Pearsall now appears
  nowhere as a row — correctly, since nothing needs deciding — so the queue says so explicitly:
  *"designated and OFF the valued board entirely… Ricky Pearsall (IR, ADP 118.0). Nothing needs
  deciding for them… their exclusion from the board is now EXPLAINED rather than lucky."* That
  sentence is the real change: his exclusion used to be luck.

### What is deliberately weak, and labelled as such

* **The team accounting identity is a hygiene flag with no remedy.** `docs/archive/PLAN_2026-08-20.md`'s
  VERDICT settled this: one-sided renormalization improves 2025 MAE (37.14 → 36.04) and then
  fails to beat a flat haircut of identical magnitude (36.42, p=0.128 overall, the flat cut
  *ahead* on the top 60 by ADP), while ordering — the only thing a board consumes — got slightly
  worse (Spearman 0.7777 → 0.7765). So every identity row carries `no_correction_warranted:
  true`, says "HYGIENE FLAG ONLY" in its reason, and attaches its team's **projected passer
  count**, because that is the honest per-team signal the arbitration found: Sleeper's overage
  runs a median +18.7% on teams listing fewer than 2 projected quarterbacks against +5.9%
  elsewhere.
* **The per-player TD flag is a badge.** R² near 0.5 outside QB passing yards, and the most
  consistent flag it produces is a player who genuinely does score 12 rushing touchdowns.
* **The fitted band is near-inert by construction** — honestly widened it fires about once in 96
  team-stat checks. It is kept because its absence would be indistinguishable from not running it.
* **Low disagreement is not a safety signal.** The mandated caveat from
  `valuation/disagreement.py` is reproduced verbatim in the page footer: four notionally
  independent families can lean on the same beat-reporter depth chart and be wrong together, and
  a fourth forecast raises the count of opinions, not the count of independent looks at the
  season.

---

## The decisions file

`data/projection_decisions.json`, modelled on `data/overrides.csv`'s discipline: checked first,
permanent, auditable, hand-editable without running anything. JSON rather than CSV because a
decision carries a free-text reason, and a reason with a comma in it is how a hand-edited CSV
silently loses a column.

```json
{
  "schema": 1,
  "_note": "Marc's adjudicated projection decisions ... NOTHING is ever added here automatically",
  "decisions": [
    {
      "source": "fantasysharks",
      "stat": "pass_td",
      "player_id": null,
      "player_name": "(every player)",
      "verdict": "reject",
      "reason": "37.9% above its own passing yardage at the fitted 2025 rate, z +13.6",
      "date": "2026-08-20",
      "detector": "td_source_bias"
    }
  ]
}
```

| field | required | meaning |
|---|---|---|
| `source` | yes | must be a known composite source; an unknown name raises |
| `stat` | yes | a canonical stat, or `"*"` for every stat from that source for that player |
| `player_id` | no | the crosswalk pid. `null` (or omitted) = the whole `(source, stat)`, every player |
| `verdict` | yes | `"keep"` or `"reject"`. A `keep` changes no number, ever |
| `reason` | yes | free text. Empty raises — a decision with no stated reason is not auditable |
| `date` | yes | ISO date |
| `player_name` | no | human-facing only; matching is on `player_id` |
| `detector` | no | audit trail of which detector surfaced it |

Rules, each with a test:

* A **bare top-level list** is also accepted, because that is what a hand-edit and the page's
  clipboard export produce. Both shapes round-trip into the object form.
* **A missing file means no decisions** and must never break a board build.
* **A malformed file fails loudly**, naming the entry by index and printing it. Silently skipping
  a bad entry would mean a rejection Marc made was quietly not applied, with nothing on the board
  to reveal it.
* **Later entries win on a shared key.** Re-deciding is legitimate (reject in August, keep in
  September) and the file never holds two contradictory lines for one key.
* **A `keep` materialises nothing.** It exists so a reviewed-and-accepted outlier stops coming
  back to the top of the queue.
* Written atomically (temp file + `os.replace`), the way this repo writes any state.

### Two widenings of the grain, both explicit in the file

* `"player_id": null` — the whole `(source, stat)`. This is the grain `blend_statlines` accepts
  natively and the right one for a source-wide defect or level bias.
* `"stat": "*"` — every canonical stat for that player, i.e. "do not use this source for this
  player at all". The right grain for an all-zero statline, where no single stat is the problem.

---

## Wiring it in (what I have NOT done, and the exact contract)

I own only `candidates.py`, `decisions.py`, `tools/review_outliers.py` and their tests. The board,
the server payload and the UI badge are somebody else's files. Here is what to change.

### 1. The board reads the decisions

`build_real_board()` currently calls:

```python
blended, provenance = blend_statlines(by_source, pos=pos, games_sources=games_sources)
```

It needs one argument added, and one object built once before the loop:

```python
from draftroom.valuation.decisions import load_decisions, rejected_index

rejections = rejected_index(load_decisions())          # once, before the player loop
...
blended, provenance = blend_statlines(
    by_source, pos=pos, games_sources=games_sources,
    rejected=rejections.for_player(pid),               # <- the only change in the loop
)
```

`for_player` returns a plain `frozenset[tuple[str, str]]` — the exact type that parameter already
accepts, with `"*"` pre-expanded and the source-wide decisions merged in. Nothing else in the
board changes.

Two things worth doing at the same time:

* **`RealBoard` should carry the applied decisions**, so a UI can render a badge without
  re-reading the file: `applied_decisions: Mapping[str, tuple[Decision, ...]]` keyed by pid,
  from `rejections.decisions_for(pid)`, plus `n_rejections: int`. `BlendProvenance` already
  records `rejected_applied` per player, so the badge can also be driven from provenance alone —
  but provenance carries the pairs, not the reason and date, and a badge with no reason is a
  silent edit with a light on it.
* **A decisions file that fails to load must fail the build loudly**, not degrade to "no
  decisions". `DecisionsFileError` should propagate. Degrading would mean Marc's rejections
  silently stopped applying, which is the one failure mode this design exists to prevent.

### 2. The server payload

Two additions. First, on the existing state/board payload, per player, so the board UI can badge
a row:

```ts
export interface TierRow {
  // ... existing fields
  projection_decisions: AppliedDecision[] | null;   // null = none applied to this player
}

export interface AppliedDecision {
  source: string;        // "fantasysharks"
  stat: string;          // "pass_td", or "*" for the whole statline
  verdict: "reject";     // keeps never appear here -- they change nothing
  reason: string;
  date: string;          // ISO
  detector: string;      // "" when hand-written
}
```

Rendering rule, which is the whole point of the field: a player whose value was changed by a
decision must show a visible badge — suggested `REJ` in the same slot as the existing `DISAGREE`
badge, in `--danger`, with the reason and date on hover. **Never silently folded in.** A count of
applied rejections belongs in the header next to the source toggle, because it is part of "which
board am I looking at".

Second, the queue itself, if the review page is ever to live in the app rather than as a file:

```
GET /api/review-queue  ->  the exact object tools/review_outliers.py --json writes
```

That shape is produced by `tools.review_outliers.queue_as_json(queue)` and is stable:

```jsonc
{
  "generated_utc": "2026-08-20T18:15:19+00:00",
  "board_source": "blend",
  "n_board_players": 188,
  "sources": ["sleeper", "espn", "fantasypros", "fantasysharks"],
  "n_pending": 1154,
  "n_findings": 1173,
  "counts_by_detector": {"distance": 601, "identity_hygiene": 495, "...": 0},
  "counts_by_severity": {"defect": 12, "distance": 605, "hygiene": 511, "badge": 26},
  "flooded": ["distance", "identity_hygiene"],
  "skipped": {},
  "suppressed_by_injury": {"contamination_zero_statline": 1, "crosswalk_missing_source": 5},
  "notes": ["FLOODED detector(s): ..."],
  "candidates": [
    {
      "row_id": "fantasysharks|pass_td|3257",
      "source": "fantasysharks",
      "stat": "pass_td",
      "player_id": "3257",
      "player_name": "Jacoby Brissett",
      "pos": "QB", "team": "ARI", "adp": 106.8,
      "values_by_source": {"sleeper": 15.0, "espn": 14.90802945, "fantasypros": 19.7,
                           "fantasysharks": 37.5},
      "unpublished_by": [],
      "value_label": "pass_td",
      "detector": "distance",
      "detectors": ["distance", "td_regression"],
      "severity": "distance",
      "reason": "one plain sentence per detector that fired, concatenated",
      "actionable": true,
      "detail": {
        "distance": {"others_median": 15.0, "deviation": 22.5, "deviation_rel": 0.6,
                     "n_contributing": 4},
        "td_regression": {"z": 3.587, "threshold": 1.834, "expected_td": 23.808,
                          "predictor": "pass_yd", "predictor_value": 3504.0,
                          "model_r2": 0.833, "quantile": 0.95}
      },
      "impact": {
        "scope": "player", "computable": true, "note": "",
        "dv_before": -27.667, "dv_after": -41.3, "dv_delta": -13.6,
        "ppg_before": 0.0, "ppg_after": 0.0,          // real PPG figures, elided here
        "points_before": 0.0, "points_after": 0.0,    // league-scored season points
        "rank_before": 143, "rank_after": 164, "rank_delta": 21,
        "drops_from_board": false, "n_players_moved": 1, "worst_player": "",
        "magnitude": 13.6
      }
    }
  ]
}
```

Field notes that matter to a consumer:

* `values_by_source[s] === null` means **that source has no row for this player**;
  `s in unpublished_by` means **that source publishes no such column at all**. Two different
  facts, and they must not render the same. Neither is a zero.
* `actionable: false` means keep/reject is the wrong response. Two cases, and their fixes are
  different: a **join failure** is a *missing* number (fix: a `data/overrides.csv` entry), and a
  **playing-time row** (`source: "playing_time"`, `detector: "injury_vs_expected_games"`) is a
  wrong availability assumption that no source rejection can reach (fix: a playing-time
  override). Render both without a control. `decisions.parse_decisions` refuses either
  pseudo-source outright, so neither can enter the decisions file even by hand.
* `suppressed_by_injury` counts rows a will-not-play designation explained away. Show it: a
  suppression nobody can see is indistinguishable from a detector that stopped working. The
  matching `notes` entries name the players.
* `impact.computable: false` means there was nothing to recompute; `impact.note` says why in
  plain words and should be shown, not swallowed.
* `impact.scope: "source"` rows carry the aggregate: `n_players_moved`, and `dv_delta` is the
  largest **single-player** move (`worst_player`), not a total.
* `detectors` can hold more than one name. That is normal, not a bug.
* Sorting is already board-impact-descending with unmeasurable defects first. Preserve it, or
  state the new order on screen.

### 2b. One bug in the badge filter, found reading the wiring

`validate/board.py` now badges with:

```python
applied_here = frozenset(provenance.rejected_applied)
applied = [d for d in rejections.decisions_for(pid) if (d.source, d.stat) in applied_here]
```

Filtering `decisions_for(pid)` down to what actually applied is exactly right — a source-wide
rejection is in force for all 188 players and only changes the ~35 with that stat, and badging all
188 would train Marc to ignore the badge. But the comparison misses a `stat: "*"` decision.
`rejected_index` expands `"*"` to every canonical stat before it reaches the composite, so
`provenance.rejected_applied` holds **concrete** pairs like `("sleeper", "rec_yd")` while the
`Decision` still carries `stat == "*"`. `(d.source, "*")` is never in that set, so a
whole-statline rejection changes the value and shows **no badge** — the one failure mode the badge
exists to prevent.

Measured, not just read. Same player (Jordyn Tyson, pid 13281), same source, one decision each,
`build_real_board(source="blend")` both times:

| decision | dv moved | badge shown |
|---|---:|---|
| `stat: "rec_yd"` | 10.1 | **yes** |
| `stat: "*"` | 13.4 | **no** |

`Decision.stats` already expands the sentinel, so the fix is one line:

```python
applied = [
    d for d in rejections.decisions_for(pid)
    if any((d.source, s) in applied_here for s in d.stats)
]
```

Worth a test with a `"*"` decision on a player who has two or more sources, asserting the badge
appears. I have not touched `board.py`.

### 3. Do not put this on a draft-night path

The queue needs the whole four-source resolution and a board build (2.9 s + 0.6 s from cold
cache). The draft phase is read-only over a frozen snapshot with no network. If the review page
ever moves into the app, gate it to prep mode explicitly.

---

## Cost and honesty of the impact column

The column is a real revaluation, not an estimate: the player's blended statline is rebuilt with
`blend_statlines(rejected=...)`, re-scored through `score_statline_with_bonus` with the same bonus
model the board uses, re-capped by the fitted availability curve, and re-valued through
`compute_draft_values` — calling `validate/board.py`'s own helpers rather than a second copy of
them. `compute_draft_values` over the 188-player board is **1.1 ms**, so all 1,154 rows get the
real number and the whole run is 2.0 s.

The baseline is asserted equal to the board's own `dv` for every player, on real cached data, in
`tests/test_candidates.py::test_real_board_impact_baseline_matches_the_board_itself`. A drift
between this column and the board is therefore a red test rather than a wrong number on a page.

One simplification, stated rather than hidden: `sigma_ppg` is carried over unchanged from the
baseline season. Dropping a source really would change the cross-source spread, but the board runs
at `lam = 0`, where `dv == evob` and sigma cannot move a value at all — so recomputing it would
add a number to the page that provably changes nothing.

A `--impact-budget` cap exists for a much wider run than this one. With a cap set, rows are
screened on a cheap proxy first (the change in league-scored season points for that one player)
and only the top N are revalued; the proxy and the real column move in the same direction but are
not identical, because the real one subtracts a positional baseline and multiplies by expected
games. Any capped run says so on the page.

---

## Gates that ran

* `pytest -q` — **744 passed** (49 in `test_candidates.py`, 24 in `test_decisions.py`).
* `tools/run_invariants.py` — **GATE: PASS**, 8/8.
* Round trip, in tests: candidates → decisions JSON → the `rejected` set → a board built with it.
  A rejection provably raises `WR1`'s value and moves nobody else's; an empty file and a
  keep-only file leave every value bit-for-bit identical.
* Round trip, on the **real cached board**, via `tools/review_outliers.py --apply`:

  | decisions file | result |
  |---|---|
  | empty | 188 players valued, **0 differ** from `build_real_board`'s own `dv` |
  | 3 rejections from the queue | **37 of 188** players change `dv` |

  The three were the source-wide `fantasysharks / pass_td`, `fantasypros / rec_yd` for Jordyn
  Tyson, and `sleeper / *` for Ricky Pearsall. Tyson lands at **dv 21.1 → 11.0 (−10.1)**, which is
  the number the queue predicted for that row before any decision was made — an independent check
  that the impact column is the real thing. Josh Allen goes **106.6 → 113.0** (his own passing
  touchdowns are unaffected, so dropping the inflated source lifts him relative to the QBs it was
  inflating). Pearsall's rejection changes nothing, correctly: he is not on the board to move.
* Playing time, in tests: the designation vocabulary split (long-term vs weekly tag); a
  zero-discount row is a `defect` and an already-discounted one is `hygiene`; the reason prints
  both compared numbers and the ADP; no games-missed figure is asserted; a weekly tag never
  fires; the row is never offered as a keep/reject **and** `decisions.parse_decisions` refuses an
  entry naming `playing_time`; a designated player already off the board gets no row; and — on
  real cached data — `effective_games_by_pid` equals the games the board actually valued every
  one of the 188 players at, Alec Pierce is queue row 1 at ADP 70.3 with zero discount, and the
  Pearsall rows are suppressed while he stays named in a note.
* Real cached data, in tests: Sleeper's constant `games` = 18.0 across 3,111 records; the
  Pearsall placeholder; ESPN's documented `pass_td` ratio 0.883 at z −4.32; the queue's own flood
  reporting; every identity row carrying its passer count and its no-correction disclaimer.
