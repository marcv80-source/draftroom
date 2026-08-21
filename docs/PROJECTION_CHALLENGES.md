# Challenging the projections: team envelopes (B3) and TD regression (B4)

Built 2026-08-20 against `docs/PLAN_2026-08-20.md` items B3 and B4. Everything below was
computed from files already cached under `data/raw/`. No network call, no `prep/fetch_all.py`
run, no invented number.

Reproduce with:

```
.venv\Scripts\python.exe tools\check_envelopes.py        [--teams-detail]
.venv\Scripts\python.exe tools\check_td_regression.py    [--quantile 0.99] [--all]
```

Modules: `backend/draftroom/valuation/envelope.py`, `backend/draftroom/valuation/td_regression.py`.
Tests: `tests/test_envelope.py` (24), `tests/test_td_regression.py` (25).

---

## What the cached data actually supports, and what it does not

The plan said "two seasons of cached weekly data sit in `data/raw/nflreadpy_weekly/`". Both
halves of that need correcting, in opposite directions.

**There are seven seasons, not two.** `data/raw/nflreadpy_weekly/` holds two FILES, each
covering 2019-2025. They are not interchangeable: the older one
(`2026-08-17T19-33-04Z.csv`, weeks up to 22) includes NFL **postseason** games and the newer one
(`2026-08-17T19-34-43Z.csv`, weeks up to 18) does not, because `_REGULAR_SEASON_ONLY` was added
to `tools/fetch_weekly_history.py` between the two fetches. Season totals fitted from the older
file would be 21-week totals. `envelope.load_weekly_history_rows()` picks the newest file (the
correct one) and **raises** if any season in it carries a week above 18, because the cached CSV
drops the `season_type` column, so contamination can be detected but never filtered out after
the fact.

**But those seven seasons carry only three stats.** The cached columns are
`season, week, player_id, player_display_name, position, pass_yd, rush_yd, rec_yd`. There is no
team column, no attempts, no targets, no touchdowns. So the file cannot produce a team-season
total of anything, and cannot fit a TD relationship at all.

**The team-level history came from somewhere else, and this is the load-bearing discovery.** The
cached ESPN payload (`data/raw/espn/`, 37 MB) carries, for each player, one stat block per game
played in 2025 (`statSourceId == 0`, `statSplitTypeId == 1`) with the **`proTeamId` the player
was on that week**. That per-week team id is what makes team aggregation possible; the player
object's own `proTeamId` is his *current* team, so using it would credit every offseason mover's
2025 production to his 2026 offense. Aggregating those blocks gives 32 team-seasons of 2025
actuals across the full canonical vocabulary — attempts, targets, receptions, yards, touchdowns.

Two checks confirm that aggregation is complete, neither of them assumed:

| check | result |
|---|---|
| accounting identity on real actuals: team `rec` vs team `pass_cmp` | closes to **under 1% on all 32 teams** (exactly 0.0% on 24) |
| ESPN team-mean yardage vs the independent nflreadpy cache, 2025 | `pass_yd` 3824 vs 3824 (**+0.0%**), `rush_yd` 1988 vs 1986 (+0.1%), `rec_yd` 3824 vs 3802 (+0.6%) |

Two unrelated providers agreeing on 2025 league yardage to within 0.6%, and an exact accounting
identity closing to under 1%, is about as much validation as an offline fit can get.

**What is still not fittable.** One season of team-seasons (n=32). The band therefore has a real
*cross-team* spread and no measured *year-to-year* component. The seven-season yardage cache
supplies that missing piece for three stats only: the league mean moved
`pass_yd -0.0%/+8.5%`, `rush_yd -9.4%/+3.4%`, `rec_yd -0.0%/+8.5%` relative to 2025 across
2019-2025. For attempts, targets and touchdowns nothing cached measures it, so the widest
measured yardage drift (`-9.4%/+8.5%`) is **transported in as a stated proxy** and every such
`Band` carries `drift_measured=False`. That is an assumption, not a fit, and the tool prints both
the widened and un-widened verdicts so the cost of the assumption is visible.

---

## B3: the team-envelope validator

Two checks, deliberately separate because they turned out to have wildly different strength.

### The fitted bands

| stat | 2025 min | median | max | band low | band high | drift |
|---|---|---|---|---|---|---|
| `pass_att` | 422 | 554 | 648 | 383 | 703 | proxy |
| `pass_yd` | 2781 | 3939 | 4735 | 2781 | 5135 | measured |
| `rush_att` | 366 | 464 | 547 | 332 | 594 | proxy |
| `rush_yd` | 1314 | 2020 | 2714 | 1191 | 2807 | measured |
| `rec` | 278 | 344 | 427 | 252 | 463 | proxy |
| `rec_tgt` | 408 | 525 | 617 | 370 | 670 | proxy |
| `rec_yd` | 2781 | 3939 | 4735 | 2781 | 5138 | measured |
| `total_td` | 24 | 42 | 63 | 22 | 68 | proxy |

`total_td` is `pass_td + rush_td`. A passing TD and a receiving TD are the same touchdown;
summing all three TD stats would report ~1.6x a real offense and make every band look busted.

Note the honest answer to the plan's "a real offense throws roughly 550-600 targets a year": in
2025 it was **408 to 617, median 525**. The 550-600 figure is not what the data says.

### The coverage question, and why the answer changed the design

The plan worried that "the ranked pool is ~189 players across 32 teams, roughly 6 per team", so
a partial roster would always undershoot. That worry does not apply once you stop using the
ranked pool. Each source publishes 14-19 skill players per team, and measured against the fitted
2025 median the team sums come out at:

| source | pass_att | rush_att | rec | rec_yd | total_td |
|---|---|---|---|---|---|
| sleeper | 0.97 | 0.98 | 1.06 | 1.04 | 0.99 |
| espn | 1.02 | 0.99 | 1.06 | 1.02 | 0.99 |
| fantasypros | 1.02 | **1.07** | **1.10** | **1.09** | 1.05 |
| blend | 1.00 | 1.05 | 1.10 | 1.07 | 1.00 |

These are essentially whole offenses. So the band check *is* applicable, and the FantasyPros row
is already a finding on its own: its entire board runs 5-10% above the 2025 median on volume.

Even so, only **overages** are treated as violations. An undershoot is still ambiguous (a
missing receiver and an under-projected offense look identical), and being wrong about that
direction is what produces a validator that flags all 32 teams and means nothing.

### Band-check result: almost nothing

Against the honestly-widened bands:

- **sleeper: 0 overages.** ESPN: **0 overages.** FantasyPros: **1** — BAL `rush_yd` 2849 vs a
  high of 2807, +1.5% (Derrick Henry 1568, Lamar Jackson 640).

Against the un-widened 2025 observed max (dropping the transported proxy):

- sleeper: 1 (DAL `rec_yd` 4771 vs 4735, +0.8% — Lamb 1249, Pickens 1211)
- espn: 0
- blend: 8
- **fantasypros: 14**, and they cluster: `rush_att` on CHI 588, SF 587, MIA 575, NO 572, PHI 568,
  CAR 568, BAL 559 against a 2025 max of 547; `rec`/`rec_yd` on NO (458 / 5052 against 427 /
  4735), CIN (446 / 4789), SF (4929); `rush_yd` on BAL 2849 and MIA 2727.

So the band mechanism, done honestly, is close to inert. Widened by measured drift it catches one
marginal violation in 96 team-stat checks (0 on the blend). That is a real result, not a failure: it says that at
the team level all three sources are within the range of a plausible NFL offense, and any claim
to the contrary would have required inventing a tighter band than the data supports.

### Identity-check result: this is where all the signal is

Every completed pass is exactly one reception. Summed over a team, `rec == pass_cmp`,
`rec_yd == pass_yd`, `rec_td == pass_td` — exactly, not approximately. The tolerance is fitted,
not chosen: the largest deviation seen in the real 2025 actuals, which is 0.62% on receptions,
0.68% on yards and 6.67% on touchdowns (small integer counts, so one TD on a 15-TD team).

| source | identity violations (of 96 checks) | worst |
|---|---|---|
| **espn** | **0** | max deviation −5.6% (SF), all negative and all explained by 6 players the crosswalk dropped |
| **sleeper** | **59** | TB `rec_yd` +806 (+21.9%), JAX `rec` +72 (+20.6%), JAX `rec_td` +6 (+22.2%) |
| **fantasypros** | **76** | NO `rec_yd` +909 (+21.9%), NO `rec` +76 (+20.0%), LV `rec_td` +7 (+31.1%) |
| **blend** (the default board) | **63** | median rec-vs-cmp gap +4.74%, worst +19.4% |

The **equal-weight blend** — the default board — inherits the problem, because averaging two
incoherent sources with one coherent one leaves two thirds of the incoherence: **63 of 96
identity violations**, median rec-vs-cmp gap **+4.74%**, worst +19.4%. That is the single most
decision-relevant number in this document, and it is why the tools now carry a `blend` column.

ESPN's projections are internally reconciled to a team passing budget — 24 of 32 teams come out
at exactly 0.0%, and the only non-zero cells are negative and match the six ESPN rows the
crosswalk failed to resolve. Sleeper and FantasyPros are not reconciled at all: their receivers
collectively catch up to 21% more balls than their own quarterbacks are projected to complete.

The bust is spread across a whole receiving corps rather than sitting on one player, which is the
signature of players projected independently and never added back up. FantasyPros NO: Tyler
Shough 366 completions and Spencer Rattler 15, against Chris Olave 94, Juwan Johnson 65, Jordyn
Tyson 63, Travis Etienne 45, Devaughn Vele 39, Bub Means 38 and more.

### The one honest objection, measured

If a source publishes a team's starter and no backup quarterback, the passing side is short and
the receiving side looks inflated for no good reason. That confound is real and the tool now
measures it on every run:

| source | 1 passer | 2 passers | 3 passers | 4 passers | corr | mean team `pass_att` |
|---|---|---|---|---|---|---|
| sleeper | +13.2% (n=2) | +3.9% (n=22) | −2.7% (n=6) | +2.0% (n=2) | −0.354 | 541 |
| espn | — | −0.5% (n=31) | −0.0% (n=1) | — | +0.073 | 558 |
| fantasypros | — | +7.4% (n=20) | +3.4% (n=9) | −0.5% (n=3) | −0.461 | 562 |

The confound explains part of the effect and not the effect. The decisive number is the last
column: the real 2025 league mean team pass attempts was **545**. FantasyPros projects **562**,
*above* the real mean, and its receiving side is still another 7.4% above its own completions on
teams with a full two-man quarterback room. A passing side that were genuinely incomplete would
show up as team pass attempts well below 545. It does not.

---

## B4: the TD-regression flag

### The fit

`td = slope x predictor`, through the origin, per position group, fitted on 639 player-seasons of
2025 actuals from the same cached ESPN weekly blocks.

| group | predictor | n | slope /100 | R² | resid sd | dispersion | usage floor | \|z\| p90 | p95 | p99 |
|---|---|---|---|---|---|---|---|---|---|---|
| QB/`pass_td` | `pass_yd` | 37 | 0.679 | **0.833** | 3.54 | 0.61 | 1238 | 1.73 | 1.83 | 2.74 |
| QB/`rush_td` | `rush_att` | 39 | 5.176 | 0.484 | 2.19 | 1.82 | 18 | 1.18 | 1.61 | 2.21 |
| RB/`rush_td` | `rush_yd` | 71 | 0.762 | 0.606 | 2.46 | 1.16 | 187 | 1.44 | 1.77 | 2.59 |
| RB/`rec_td` | `rec_yd` | 61 | 0.614 | 0.519 | 1.19 | 0.91 | 96 | 1.38 | 1.60 | 2.56 |
| WR/`rec_td` | `rec_yd` | 109 | 0.616 | 0.536 | 1.96 | 1.01 | 225 | 1.45 | 1.82 | 3.62 |
| TE/`rec_td` | `rec` | 62 | 8.124 | **0.395** | 1.99 | 1.16 | 14 | 1.44 | 2.22 | 2.62 |

Nothing here is a chosen number. The predictor is whichever candidate scores the highest R².
The usage floor is the median of that predictor's non-zero values in the sample. The variance
model is `dispersion x expected` (the Poisson shape) with `dispersion` measured off the
residuals. The flag threshold is a fitted quantile of |z| in the sample, so "outlier" means
"further from its own yardage than 95% of real 2025 player-seasons were".

**Two implementation choices worth knowing, because getting either wrong produced a false
finding during the build.**

1. **The slope is the pooled rate `sum(y)/sum(x)`, not OLS.** This is forced by the variance
   model: if `var(td) = c x slope x x`, the maximum-likelihood through-origin slope is exactly
   the pooled rate, while OLS assumes constant variance and over-weights the highest-volume
   players, who score at a higher rate than everyone else. Measured on the 2025 actuals, OLS ran
   1.7% hot on QB passing TDs, 8.7% on RB receiving TDs and **15.7% on QB rushing TDs** — enough
   to make all three sources look like they were under-projecting quarterback rushing touchdowns
   by 20-30%. They were not. `ols_slope` is kept on every model so that stays reproducible.
2. **Targets are excluded as a predictor.** Allowed as a candidate, `rec_tgt` wins on R² for all
   three receiving groups — and only ESPN publishes targets, so the flag then goes completely
   blind on Sleeper and FantasyPros. It wins by 0.006 of R² (WR: 0.542 vs 0.536). Trading two
   thirds of the board's coverage for that is not a trade.
   `CANDIDATE_PREDICTORS_WITH_TARGETS` reproduces the alternative.

### Per-player flags: 9 in total, across 1,529 statlines

At the fitted p95 threshold, every flag inside the ADP pool:

| source | player | stat | projected | expected | z (threshold) |
|---|---|---|---|---|---|
| sleeper | Josh Allen (QB, adp 1.5) | `rush_td` | 11.0 | 5.7 | +1.65 (1.61) |
| espn | Malik Willis (QB, adp 76) | `pass_td` | 13.2 | 24.2 | **−2.86** (1.83) |
| espn | Cam Ward (QB, adp 95) | `pass_td` | 16.6 | 25.5 | −2.26 (1.83) |
| espn | Jacoby Brissett (QB, adp 107) | `pass_td` | 14.9 | 22.8 | −2.11 (1.83) |
| espn | Deshaun Watson (QB, adp 127) | `pass_td` | 8.3 | 13.9 | −1.90 (1.83) |
| espn | Fernando Mendoza (QB, adp 129) | `pass_td` | 14.2 | 20.8 | −1.85 (1.83) |
| espn | Josh Allen (QB, adp 1.5) | `rush_td` | 12.5 | 6.0 | +1.94 (1.61) |
| fantasypros | Davante Adams (WR, adp 53) | `rec_td` | 10.2 | 5.7 | +1.87 (1.82) |
| fantasypros | Josh Allen (QB, adp 1.5) | `rush_td` | 11.8 | 6.1 | +1.70 (1.61) |

That is a very sparse flag, and the sparsity is structural rather than a bug. The threshold is
the dispersion of *realised* outcomes, while a projection is an expectation and should be less
dispersed than reality. Judging one against the other under-flags by construction. The only
player flagged by all three sources is Josh Allen's rushing touchdowns, and he is a genuine
outlier, not a projection error — which is the honest way to read a mechanism that flags him.

### Aggregate TD level: the same fit, summed, and this one has teeth

| source | group | n | projected | expected | ratio | agg z |
|---|---|---|---|---|---|---|
| sleeper | QB/`pass_td` | 34 | 768.0 | 764.4 | 1.005 | +0.16 |
| sleeper | WR/`rec_td` | 113 | 499.0 | 464.0 | 1.075 | +1.62 |
| sleeper | TE/`rec_td` | 64 | 198.0 | 217.5 | 0.910 | −1.23 |
| **espn** | **QB/`pass_td`** | 34 | 738.7 | 836.4 | **0.883** | **−4.32** |
| **espn** | **TE/`rec_td`** | 56 | 168.0 | 209.6 | **0.801** | **−2.67** |
| espn | RB/`rec_td` | 57 | 85.9 | 98.1 | 0.876 | −1.29 |
| fantasypros | QB/`pass_td` | 34 | 800.9 | 822.6 | 0.974 | −0.97 |
| fantasypros | WR/`rec_td` | 120 | 536.7 | 506.3 | 1.060 | +1.35 |
| blend | QB/`pass_td` | 34 | 769.2 | 807.8 | 0.952 | −1.74 |
| blend | TE/`rec_td` | 64 | 194.4 | 219.3 | 0.886 | −1.56 |

(Full table in the tool output.) One clear signal: **ESPN projects 11.7% fewer passing
touchdowns than its own passing yardage implies at the 2025 rate**, z −4.32. In a league with two
mandatory QB slots that is not a rounding difference — it moves the whole quarterback board.
ESPN's tight ends are 20% light on the same basis.

The blend dilutes that: ESPN's −11.7% becomes −4.8% at z −1.74, which is no longer a signal.
Equal weighting is doing its job here — one source's level bias gets averaged down by two that
do not share it.

### A real calibration backtest — for ESPN only

The plan's backtest section asked whether 2025 preseason projections are retrievable per source.
For ESPN they are already cached: the payload carries ESPN's own 2025 season projection
(`statSourceId == 1`) alongside the 2025 actuals, on the same player ids. Nothing under
`data/raw/sleeper_projections/` is 2025 and the FantasyPros CSVs are 2026 exports, so **from
this repo's cache only ESPN can be backtested** — a finding here is about ESPN and says nothing
comparative.

Extending it is a prep fetch, not a modelling problem. `docs/SOURCE_BACKTEST.md` (built in
parallel) establishes that Sleeper's 2025 preseason projections **are** retrievable, 3,115
records, verified preseason by content. Cache those and this table covers two of the three
families. FantasyPros stays unmeasurable either way.

Rates, not totals: projected totals overshoot every year simply because projections do not know
who gets hurt, so a total-vs-total ratio measures availability optimism, not TD calibration.

| group | predictor | n | ESPN's projected rate | actual 2025 rate | ratio |
|---|---|---|---|---|---|
| QB/`pass_td` | `pass_yd` | 34 | 0.613 /100 | 0.683 /100 | **0.897** |
| QB/`rush_td` | `rush_att` | 35 | 4.737 /100 | 5.371 /100 | 0.882 |
| RB/`rec_td` | `rec_yd` | 54 | 0.528 /100 | 0.673 /100 | **0.784** |
| RB/`rush_td` | `rush_yd` | 71 | 0.757 /100 | 0.739 /100 | 1.024 |
| TE/`rec_td` | `rec` | 52 | 6.779 /100 | 7.727 /100 | 0.877 |
| WR/`rec_td` | `rec_yd` | 105 | 0.610 /100 | 0.624 /100 | 0.978 |

ESPN under-projected the 2025 passing-TD rate by 10.3% and then projected 2026 at 11.7% below
the 2025 rate. Same direction, near-identical magnitude, one year apart. That converts "ESPN
differs from 2025" into "ESPN has a persistent low-TD-rate habit", verified against the season it
was projecting.

Caveat: it is unknown whether that `statSourceId == 1` block is ESPN's *preseason* projection or
one updated during 2025. If it was updated in-season it should have been closer to the actuals,
which would make the finding stronger, not weaker — but it cannot be verified from the cache.

---

## What each mechanism is strong enough for

Marc decides; this is the evidence.

**The accounting identity is the strongest thing in either module.** It needs no fitted band, no
history, and no assumption — `rec == pass_cmp` is arithmetic. It found a 20%+ internal
contradiction in two of the three sources and exactly zero in the third. The confound (a missing
backup quarterback) was measured and does not explain it. This is the one piece of evidence here
that could justify a rejection rule.

But the rejection rule the composite exposes is per `(source, stat)` and would drop that source's
number for *every* player at that stat, and the violation localises to a **team**, not to a
source-wide stat. Dropping Sleeper's `rec_yd` league-wide because 26 of 32 teams are internally
inconsistent throws away Sleeper on the 6 teams where it is fine, and does nothing about the fact
that within a busted team we do not know *which* receiver is over-projected. Two better shapes,
if Marc wants this to act rather than annotate: renormalise the receiving side to the team's own
passing budget (which keeps every player's share and fixes only the level), or reject at
`(source, stat, team)` grain, which the composite's `rejected` container does not currently
express. Either is a design decision, not a build detail.

**The fitted band is not strong enough for automatic rejection.** Honestly widened it fires once
in 96 checks, at +1.5%. Un-widened it fires 14 times on FantasyPros, but the widening it drops is
the honest acknowledgment that one season of team-seasons cannot see year-to-year league drift.
The band's real value is the *coverage* row it produces as a by-product: FantasyPros' board
running 5-10% above the 2025 median on volume across every stat is a genuine, quantified
finding, and it is a level statement about the whole source, which is exactly what a weight
could act on.

**The per-player TD flag is not strong enough for anything but a badge.** R² is 0.40-0.61 for
everything except QB passing yards, and on 1,529 real statlines it produces 9 flags of which the
most consistent is a player who really does score 12 rushing touchdowns. Keep it visible; do not
let it move a number.

**The aggregate TD level is a different and better mechanism than the plan asked for.** ESPN at
z −4.32 on quarterback passing touchdowns, corroborated by ESPN's own 2025 miss in the same
direction at the same magnitude, is a real measured bias in the source whose projections
`CLAUDE.md` still calls "source of record". It also lines up with what
`docs/SOURCE_BACKTEST.md` found independently: on the top 24 by ADP, ESPN over-projected 2025
by 55.9 points per player against Sleeper's 39.0 — optimistic on volume, pessimistic on the TD
rate, which is a coherent pair of habits rather than two unrelated errors. It is also the right *shape* for the composite: a
source-wide level bias on one stat is precisely what a per-`(source, stat)` weight or rejection
can fix. It is one season of history and one source that can be backtested, so it is evidence
for down-weighting ESPN's `pass_td`, not proof.

**One thing worth not forgetting.** The disagreement caveat in `valuation/disagreement.py`
applies to all of this in reverse. These checks pass ESPN on both mechanisms and fail the other
two — but ESPN passing the identity check only proves ESPN reconciles its own arithmetic, which
is a hygiene property, not accuracy. A source can be perfectly self-consistent about an offense
that never happens. The identity check catches sloppiness; nothing here catches being wrong
together.
