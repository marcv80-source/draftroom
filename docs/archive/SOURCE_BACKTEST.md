# Which projection source was actually more accurate in 2025?

Measured 2026-08-20. Tool: `tools/backtest_sources.py` (`.venv\Scripts\python.exe tools/backtest_sources.py`).
Every number below is printed by that tool from data it pulled; nothing here is estimated or
carried over from another document.

**Correction applied 2026-08-21, numbers re-verified and unchanged.** The tool's local
`blend_statlines` was averaging Sleeper's `games` into the blend, and Sleeper publishes a blanket
18.0 for every player in 2025 as well as 2026 -- a constant, not a forecast -- so an ESPN 11-game
projection blended to 14.5 (Codex review finding 9, `docs/reviews/ui-rebuild-four-source-composite-codex.md`).
It now applies the same rule production does: a source contributes to `games` only if its `games`
column varies within that source, measured over the pool rather than declared, and printed in the
report header. **Every figure in this document was re-run after the fix and reproduces exactly**,
because the season-total MAE tables that carry the verdicts score without the bonus term and
`games` only enters the bonus-scored and PPG views. Those secondary views are now correct; no
conclusion here changed.

**The answer, up front.** Over 449 players scored in this league's own currency, Sleeper's 2025
preseason projections beat ESPN's by 0.95 points of MAE overall and 2.5 points on the 179 players
who actually had a 2025 ADP. Neither gap survives a paired test (p = 0.36 and p = 0.18). Sweeping
the Sleeper weight from 0 to 1 moves MAE by 0.2 points per player and the bootstrap interval for
the best weight is 0.40 to 1.00. **Equal weight is not just the default any more, it is the
measured answer.** The one result that does clear significance is that the blend beats ESPN alone
(p = 0.014), which is the standard forecast-averaging result and an argument for the composite
itself, not for tilting it.

The genuinely actionable finding is not about weights at all. It is that **both sources were
badly optimistic about early-round players, and ESPN much more so**: on the top 24 by ADP, ESPN
projected 55.9 points more than players delivered and Sleeper 39.0 more. That is a bias to correct
in one place, not a reason to prefer one feed. **Part 2 of this document takes that finding
apart**: per-position calibration slopes, whether the tier bias is anything other than plain
regression to the mean, and what a shrink would do to the QB premium on the live board.

---

## What could and could not be measured

| Source | 2025 preseason projections | In this backtest |
|---|---|---|
| Sleeper (the numbers are Rotowire's; `company: "rotowire"`) | retrievable, 3,115 records | yes |
| ESPN / Mike Clay | retrievable, 700-player window; 457 of the 630 skill-position players carry a 2025 projection block (522 including kickers and defenses) | yes |
| FantasyPros | **not retrievable** | no |

FantasyPros cannot be backtested here. Our CSVs are 2026 only and the historical download sits
behind the HOF subscription `CLAUDE.md` says not to buy. That is itself a result worth stating
plainly: **a source with no measurable track record cannot earn a weight above equal.** Whatever
this document concludes about Sleeper and ESPN, FantasyPros stays at its equal share by default,
because there is no evidence to move it either way.

## Both projection sets really are preseason

The obvious failure mode is scoring a season-end restatement against the season it restates, which
would show implausibly small errors. Neither source is doing that, and the tool re-checks it by
content on every run rather than trusting a timestamp:

| Player | Games actually played, 2025 | ESPN projected | Sleeper projected |
|---|---|---|---|
| Brandon Aiyuk | 0 | 12.0 games, 578 total yards | 630 total yards |
| MarShawn Lloyd | 0 | 13.0 games, 276 total yards | 160 total yards |
| Joe Burrow | 8 | 17.0 games, 4,844 total yards | 4,686 total yards |

Sleeper's 2025 records carry `last_modified` of 2026-01-04, after the season. That is a bulk
re-write of the store, not a re-forecast: the content above is preseason, and a restatement would
have put ~0 on the two players who never took a snap.

## One actuals spine, checked against a second

Both sources are scored against the **same** actuals: ESPN's own 2025 actual stat blocks
(`statSourceId == 0`) from the same payload as its projections (`statSourceId == 1`). Scoring
Sleeper against nflreadpy and ESPN against ESPN would have measured the actuals, not the
projections.

Those ESPN actuals were cross-checked against the cached nflreadpy 2025 weekly history, read via
`load_latest_weekly_history()` so the newest file wins (the older file in
`data/raw/nflreadpy_weekly/` still contains postseason weeks 19-22, which would inflate every
season total). **Twelve of twelve sampled players agree exactly on passing, rushing and receiving
yards: max |difference| = 0 yards.** Josh Allen's games count differs by one (ESPN 17, nflreadpy
16 rows), which is a games-appearance convention, not a stat disagreement.

## The stat-id gate

`CLAUDE.md` warns that the `espn-api` PyPI stat-id table is wrong for id 22 and that a wrong id
produces plausible numbers in the wrong field. Every id this backtest reads is re-derived from
ESPN's own ratio fields, in **both** the projection and the actual blocks, and a mismatch raises
rather than warns:

```
projection id21 == pass_cmp/pass_att    66/66 agree
projection id60 == rec_yd/rec           379/379 agree
projection id39 == rush_yd/rush_att     240/240 agree
projection id73 == pass_int + fum_lost  448/448 agree
actual     id21 == pass_cmp/pass_att    72/72 agree
actual     id60 == rec_yd/rec           402/402 agree
actual     id39 == rush_yd/rush_att     286/286 agree
actual     id73 == pass_int + fum_lost  157/157 agree
```

The 2025 **actual** blocks also carry stat ids that never appear in a projection block (52, 54,
55, 155, 156, 158, 175-186, 32, 179-182). Every one of them is a granular count or bonus bucket
with no canonical stat; none is a scored stat arriving under a second id, which the identity
checks above confirm by reconciling the scored fields exactly.

## Population, and what was dropped

449 players: an ESPN 2025 projection, a Sleeper 2025 projection, and observed 2025 production.

- 157 joined by Sleeper's own `espn_id`, 292 by normalized name plus position. The `espn_id`
  index is built from the **full** cached Sleeper universe, not `filter_active_skill_players`,
  because filtering on `active` would restrict a 2025 backtest to players who survived to 2026.
- **7 players kept whose ESPN actual block was empty** (Joe Mixon, Brandon Aiyuk, MarShawn Lloyd,
  Jermaine Burton, Trey Palmer, Sam Howell, Gavin Bartholomew). They played and recorded nothing,
  or never played at all. Both sources projected real production for them, so that is forecast
  error and it belongs in the error. Dropping them would have flattered whichever source was more
  optimistic, which is exactly the survivorship trap.
- **0 players held out for unobserved production.** Every projected player had an actual block, so
  the survivorship sensitivity is empty this year rather than something that had to be judged.
- 8 players never entered the table because Sleeper published no 2025 projection to pair with.
  They are fullbacks and camp bodies (Kyle Juszczyk, Patrick Ricard, C.J. Ham, Alec Ingold, and
  four similar), not a value-relevant exclusion.
- 0 ambiguous name-plus-position matches. An ambiguous join is left unmatched, never guessed.
- 179 of the 449 carry a 2025 preseason 2QB ADP from Fantasy Football Calculator (feed window
  2025-08-22 to 2025-09-01, 3,317 drafts, 215 players).

Scale, so the MAEs below can be judged against something: actual 2025 league points averaged
146.8 with a standard deviation of **86.5** across the players in the ADP feed, and 82.5 with a
standard deviation of 82.6 across all 449.

---

## A. League points, no per-game bonus

MAE, RMSE and bias in season league points. Bias is projected minus actual, so positive means the
source was optimistic.

| Group | n | Sleeper MAE | ESPN MAE | Blend MAE | Sleeper bias | ESPN bias | Sleeper corr | ESPN corr | Blend corr |
|---|---|---|---|---|---|---|---|---|---|
| All matched | 449 | **37.5** | 38.4 | **37.1** | +8.1 | +9.0 | 0.803 | 0.804 | 0.809 |
| In 2025 ADP feed | 179 | **56.2** | 58.7 | 56.3 | +27.8 | +37.0 | 0.652 | 0.673 | 0.673 |

By position (all matched):

| Position | n | Sleeper MAE | ESPN MAE | Blend MAE | Sleeper bias | ESPN bias |
|---|---|---|---|---|---|---|
| QB | 66 | **53.7** | 56.1 | 54.5 | +12.5 | +10.3 |
| RB | 108 | 41.9 | 42.3 | **40.9** | +8.0 | +11.9 |
| WR | 179 | 36.8 | 37.4 | **35.9** | +12.8 | +12.3 |
| TE | 96 | **22.6** | 23.9 | 23.2 | -3.4 | -1.2 |

By 2025 ADP tier:

| Tier | n | Sleeper MAE | ESPN MAE | Blend MAE | Sleeper bias | ESPN bias | Sleeper corr | ESPN corr |
|---|---|---|---|---|---|---|---|---|
| ADP 1-24 | 24 | **74.5** | 76.4 | 74.5 | +39.0 | +55.9 | -0.081 | 0.026 |
| ADP 25-60 | 36 | **70.8** | 78.3 | 74.4 | +36.6 | +52.8 | 0.579 | 0.484 |
| ADP 61-120 | 59 | **47.2** | 53.4 | 49.5 | +29.8 | +42.1 | 0.572 | 0.549 |
| ADP 121+ | 60 | 49.0 | **45.0** | 45.0 | +16.0 | +15.0 | 0.224 | 0.521 |
| Not in 2025 ADP | 270 | 25.1 | 25.0 | **24.4** | -4.9 | -9.6 | 0.604 | 0.541 |

## B. League points including the per-game yardage bonus

Projections get the modelled bonus (`expected_bonus` over the fitted Tier 1 curves); actuals get
`actual_bonus` over ESPN's real weekly yardage, so the ground truth stays ground truth with no
model in it. Sleeper reports a blanket `gp` of 18.0 for **every** player in both 2025 and 2026 --
a constant, not a forecast, and impossible in a 17-week season -- so the bonus term caps games at
the league's own `weeks`, or this table would become a comparison of two games figures, one of
which is not a projection.

| Group | n | Sleeper MAE | ESPN MAE | Blend MAE |
|---|---|---|---|---|
| All matched | 449 | **38.9** | 40.0 | **38.6** |
| In 2025 ADP feed | 179 | **59.1** | 62.0 | 59.4 |
| QB | 66 | **55.9** | 58.8 | 57.1 |
| RB | 108 | 43.5 | 44.0 | **42.5** |
| WR | 179 | 38.5 | 39.3 | **37.7** |
| TE | 96 | **22.9** | 24.1 | 23.4 |
| ADP 1-24 | 24 | **79.3** | 82.6 | 80.0 |
| ADP 25-60 | 36 | **76.3** | 83.5 | 79.7 |

Bonuses add roughly 1.5 to 3 points of MAE to every source and **change no ordering anywhere**.
The accuracy question is the same in both currencies, which is worth knowing: the bonus model can
be argued about on its own merits without it dragging the source question along with it.

## D. Per-game view (players who played at least 8 games)

Season totals mix forecasting the rate with forecasting availability. `valuation/evob.py` consumes
PPG, so this is the view closest to how the number is actually used.

| Group | n | Sleeper | ESPN | Blend |
|---|---|---|---|---|
| MAE, points per game | 371 | 2.1 | 2.1 | **2.0** |
| RMSE | 371 | 2.9 | 2.9 | **2.8** |
| Correlation | 371 | 0.856 | 0.864 | **0.867** |

On rate alone the two sources are indistinguishable (MAE gap 0.01 points per game, p = 0.94). The
blend edges Sleeper by 0.08 PPG (p = 0.048) and ESPN by 0.07 (p = 0.14).

---

## Is any of it real, or is it noise?

Paired on the same players, comparing absolute errors. Negative gap means the first source is
closer. Bootstrap CI is 10,000 resamples, seed fixed.

| Population | n | Comparison | MAE gap | 95% CI | p | Read |
|---|---|---|---|---|---|---|
| All matched | 449 | Sleeper vs ESPN | -0.95 | -2.99 .. +1.03 | 0.355 | indistinguishable |
| All matched | 449 | Blend vs Sleeper | -0.34 | -1.41 .. +0.77 | 0.545 | indistinguishable |
| All matched | 449 | Blend vs ESPN | **-1.29** | -2.35 .. -0.24 | **0.018** | blend wins |
| In ADP feed | 179 | Sleeper vs ESPN | -2.46 | -5.96 .. +1.16 | 0.178 | indistinguishable |
| In ADP feed | 179 | Blend vs Sleeper | +0.14 | -1.90 .. +2.09 | 0.891 | indistinguishable |
| In ADP feed | 179 | Blend vs ESPN | **-2.32** | -4.11 .. -0.48 | **0.014** | blend wins |
| ADP 1-60 | 60 | Sleeper vs ESPN | -5.22 | -11.01 .. +0.50 | 0.081 | indistinguishable |
| ADP 1-60 | 60 | Blend vs ESPN | **-3.07** | -6.05 .. -0.18 | **0.046** | blend wins |
| QB | 66 | Sleeper vs ESPN | -2.44 | -11.52 .. +5.35 | 0.569 | indistinguishable |
| RB | 108 | Sleeper vs ESPN | -0.43 | -5.27 .. +4.41 | 0.862 | indistinguishable |
| WR | 179 | Sleeper vs ESPN | -0.53 | -3.07 .. +2.08 | 0.688 | indistinguishable |
| TE | 96 | Sleeper vs ESPN | -1.31 | -3.16 .. +0.54 | 0.174 | indistinguishable |

Sleeper was closer on 253 of 449 players (56%), and on 111 of 179 in the ADP feed (62%). A
consistent lean, never a significant one. Note what the significant rows all have in common: the
blend beating **ESPN**. On season points the blend never significantly beats Sleeper (its only
edge over Sleeper anywhere is the per-game view, at p = 0.048), and Sleeper never significantly
beats ESPN. That is the signature of two forecasts of similar quality where
averaging removes some of the worse one's error.

## E. What weight would 2025 actually choose?

MAE as a function of the Sleeper weight, ESPN taking the rest.

| w (Sleeper) | MAE, all 449 | MAE, ADP feed (179) |
|---|---|---|
| 0.00 (ESPN only) | 38.43 | 58.67 |
| 0.20 | 37.74 | 57.46 |
| 0.40 | 37.25 | 56.54 |
| **0.50 (equal)** | **37.14** | **56.35** |
| 0.60 | 37.08 | 56.26 |
| 0.70 | **37.06** | 56.17 |
| 0.80 | 37.12 | **56.13** |
| 1.00 (Sleeper only) | 37.47 | 56.21 |

The best weight on 2025 is 0.70 on the full population and 0.80 on the ADP feed. What it buys is
**0.08 and 0.22 points per player** against equal weight, on projections whose errors average 37
and 56 points and whose outcomes have a standard deviation of 86.5. And that optimum is not
stable: resampling the players puts the best weight anywhere in **0.30 to 1.00** (full population)
or **0.40 to 1.00** (ADP feed), landing on a corner solution of 0 or 1 in 41% of draws on the ADP
feed. An in-sample optimum always exists. This one is indistinguishable from flat.

---

## Should the composite weight these equally?

**Yes. Ship equal weight, and now say it is measured rather than assumed.**

1. **Sleeper vs ESPN is a coin flip in 2025.** Sleeper leads on every headline cut, by 1 to 2.5
   points of MAE, winning 56-62% of individual players. Nothing reaches significance and the
   confidence intervals comfortably contain zero. One season, one injury pattern.
2. **The blend beats ESPN alone, significantly, on three separate populations.** That is the
   forecast-averaging result the plan bet on, and it holds here rather than being assumed. It
   justifies having a composite at all.
3. **The blend does not beat Sleeper alone** on season points, and only marginally on PPG
   (0.08 points per game, p = 0.048). So this evidence is a case for averaging, not for knowing
   which member to favour.
4. **No positional reweighting is supported.** Sleeper's QB edge (53.7 vs 56.1 MAE) is the
   position where it matters most in a 2QB league, and it is also the position with the widest
   confidence interval in the whole table: -11.52 to +5.35. In a room where replacement-level QB
   is QB22, 66 quarterbacks is not enough evidence to tilt QB projections toward one feed. The
   one place ESPN is clearly better is ADP 121+ (45.0 vs 49.0, correlation 0.52 vs 0.22), the
   deepest tier, where a projection edge is worth the least.
5. **FantasyPros keeps its equal share by necessity.** It is unmeasurable, and unmeasurable is not
   the same as bad. Down-weighting it for lack of evidence would be a bias dressed as a finding.

If a weight ever moves, the honest version is a mild Sleeper tilt in the 0.5 to 0.8 band that
2025 cannot distinguish from 0.5 -- worth at most 0.2 points per player, which is under a tenth of
a standard deviation of the thing being forecast. It is not worth the complexity or the risk of
fitting one season's noise.

### The finding that IS worth acting on: both sources are optimistic about early picks

| Tier | Sleeper bias | ESPN bias |
|---|---|---|
| ADP 1-24 | +39.0 | +55.9 |
| ADP 25-60 | +36.6 | +52.8 |
| ADP 61-120 | +29.8 | +42.1 |
| ADP 121+ | +16.0 | +15.0 |
| Not in 2025 ADP | -4.9 | -9.6 |

Every projection of a drafted player ran hot in 2025, and the overage scales with draft position:
+39 points on the top 24 for Sleeper, +56 for ESPN. Averaging the two does **not** fix it -- the
blend's top-24 bias is +47.7, exactly between them, because a shared bias survives averaging. That
is a level effect on the early board, and it is much larger than any gap between the sources: the
whole Sleeper-vs-ESPN argument is worth 2.5 points, while shared optimism on the top 60 is worth
40 to 55.

Two caveats before anyone treats it as a haircut to apply. It is one season, and 2025's injury
draw is inside that number. And it partly reflects selection: high-ADP players are high-ADP
because everyone was optimistic, so the population is defined by the thing being measured.
Whatever gets built on this needs its own validation gate, exactly like the opponent-prediction
work that failed three times.

One related observation, offered with the same caution: within the top 24 by ADP, projection
correlation with actual outcome was **-0.08 (Sleeper) and +0.03 (ESPN)**. Inside that tier the
projections carried no information about who outperformed. Some of that is range restriction on 24
players in a narrow band, so it is not evidence that projections are worthless at the top -- but
it does argue against treating small projection deltas as a tiebreaker among early-round players.

## How much confidence does one season buy?

Not much, and the tool says so on every run. The measurement is sound -- one actuals spine,
verified stat ids, exact agreement with an independent actuals source, no players quietly dropped
-- but it is a **single draw**. 2025 had one injury pattern, one set of coaching changes, one crop
of rookies. A source can win a season on luck, and the sizes here are exactly the sizes luck
produces: a 1 to 2.5 point MAE gap against a spread of 86.5 points.

What this does buy is a floor. **Equal weighting is now an empirically supported choice rather
than a fallback**, which is worth more than it sounds: it retires the open question in the plan's
"Backtest: verify before promising" section, and any future proposal to reweight has to beat a
measured baseline instead of a default. Reweighting on accuracy needs several seasons per source,
and Sleeper serves current-year projections only, so the practical path is to run this tool every
January and accumulate seasons.

---

## Reproducing this

```
.venv\Scripts\python.exe tools/backtest_sources.py            # offline, from data/backtest/
.venv\Scripts\python.exe tools/backtest_sources.py --refresh  # re-pull the three 2025 payloads
.venv\Scripts\python.exe -m pytest tests/test_backtest_sources.py -q
```

Sections of that output: A/B accuracy by position and ADP tier, C survivorship sensitivity,
D per-game view, E weight sweep, **F calibration slopes, G the ADP-tier decomposition, H the
board-shrink consequence, I bonus versus shrink**. Section H imports `build_real_board` and
`compute_draft_values` read-only, re-values a shrunk COPY, and writes nothing -- no shrink is
applied to the shipped board. If those modules cannot be imported it prints a SKIPPED line rather
than failing the run, and the end-to-end test asserts the run it checks was not the degraded one.
Every bootstrap is seeded, so two consecutive runs produce byte-identical output.

The three payloads cache to **`data/backtest/`** under fixed names, deliberately not
`data/raw/<source>/`: a new timestamped file there moves what `load_latest_raw()` resolves to and
breaks tests that have nothing to do with this work (`CLAUDE.md`, hit for real 2026-08-17). This
tool never calls `prep/fetch_all.py`, and it only ever reads from `data/raw/`.

Endpoints, exactly as used:

- ESPN: `lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2025/segments/0/leaguedefaults/3?view=kona_player_info`
  with `X-Fantasy-Filter: {"players": {"limit": 700, "sortDraftRanks": {"sortPriority": 100, "sortAsc": true, "value": "STANDARD"}}}`.
  **`sortDraftRanks` is required** -- the `sortPercOwned` filter `prep/espn_client.py` uses for the
  current season returns HTTP 400 on a past season.
- Sleeper: `api.sleeper.com/projections/nfl/2025?season_type=regular&position[]=QB&position[]=RB&position[]=WR&position[]=TE&order_by=adp_half_ppr`
- FFC: `fantasyfootballcalculator.com/api/v1/adp/2qb?teams=12&year=2025`

## Two side findings for whoever owns the board

Neither is touched by this work; both were turned up by it.

1. **Sleeper's `gp` is a blanket 18.0 for every player, in 2025 and in 2026.** It is not a
   projection of games, and `prep/sleeper_client.py` maps it to canonical `games`. Anything reading
   `games` off a Sleeper statline is reading a constant that is also one game longer than this
   league's season. This backtest caps it at `cfg.weeks` for the bonus term; the live board's
   handling is a separate question.
2. **ESPN's `games` (stat id 210) is a real per-player figure**, including in a past season: of the
   projected players, 413 sit at 17.0 and the rest spread across 4 through 16.5. If a per-player
   projected-games figure is wanted anywhere, ESPN is the source that has one.

---

# Part 2: calibration slopes, and what a shrink would cost the QB premium

Added 2026-08-20, same tool, same 449-player spine, sections F through I of its output.

**The answer, up front.** Every position's slope is below 1.0 in every cut, so the direction of
the finding holds. Our 2025 ordering only half agrees with Fantasy Football Analytics' 12-season
ordering: both put QB at or near the bottom, but FFA has WR as the best-calibrated position
(0.85) while our season-total fit has WR as the *worst* (0.69). The ADP-tier bias from Part 1 is
**not** a separate effect from regression to the mean; it is the same finding written a different
way, and the decomposition shows nothing left over. And the coordinator's board estimate is
**verified, not refuted**: under FFA's slopes Josh Allen goes from 108.75 to 72.87 and from
overall #9 to #11, out of the top 10. Under the slope basis that is most defensible for this
board he falls to #24 and the board holds one QB in its top 30 instead of five.

That last number is the reason not to ship it yet. The QB slope is simultaneously the most
consequential parameter and the worst-identified one in the whole exercise: 30 quarterbacks, a
bootstrap interval of 0.24 to 0.97, and a value that flips with the source you fit
(ESPN 0.86, Sleeper 0.43).

## 1. Are our own slopes below 1.0? Do they corroborate FFA?

Regressing actual on projected: a slope of 1.0 is a perfectly calibrated forecast, and below 1.0
says shrink toward the positional mean. Blend projections, 2025 season league points, all 449
players (section F1). `*` marks a bootstrap CI that excludes 1.0.

| Position | n | Slope | 95% CI | Intercept | R² | = r | x sd ratio | FFA (12 seasons) |
|---|---|---|---|---|---|---|---|---|
| QB | 66 | **0.71** * | 0.57 .. 0.85 | +28.9 | 0.66 | 0.81 | 0.88 | 0.67 |
| RB | 108 | 0.89 | 0.74 .. 1.02 | +1.2 | 0.65 | 0.81 | 1.10 | 0.79 |
| WR | 179 | **0.69** * | 0.58 .. 0.80 | +13.2 | 0.58 | 0.76 | 0.91 | 0.85 |
| TE | 96 | **0.81** * | 0.68 .. 0.95 | +12.0 | 0.67 | 0.82 | 0.99 | 0.72 |
| All | 449 | **0.77** * | 0.69 .. 0.84 | +12.2 | 0.66 | 0.81 | 0.95 | -- |

Per source, same population: Sleeper QB 0.74 / RB 0.95 / WR 0.70 / TE 0.83; ESPN QB 0.67 /
RB 0.79 / WR 0.65 / TE 0.77. ESPN's slopes are lower across the board, which is the same
optimism Part 1 measured, now expressed as a slope.

**Corroboration, honestly scored.** Two things agree and one disagrees.

- **Agrees:** all four positions below 1.0, in both datasets. One season and twelve seasons
  reaching the same qualitative conclusion is the part worth trusting.
- **Agrees:** QB is badly calibrated. FFA has it worst at 0.67; we have it at 0.71 on season
  totals and 0.53 on the rate basis, worst or joint-worst in every cut we ran.
- **Disagrees, and it is the consequential one:** **WR**. FFA has WR as the best-calibrated
  position at 0.85. We have WR at 0.69, statistically indistinguishable from our QB and the
  lowest of our four. RB is the mirror image: FFA 0.79, us 0.89. Our own CIs overlap so heavily
  (RB 0.74-1.02 against WR 0.58-0.80) that we cannot resolve the middle ordering at all.

Why the WR disagreement matters more than it looks: a per-position shrink only changes the board
through the *differences* between slopes. If QB and WR shrink by the same amount, quarterbacks do
not move relative to receivers at all. FFA's numbers produce a QB-down-WR-up rotation; ours
produce a nearly uniform compression. Section 3 shows both.

## 2. Is the ADP-tier bias distinguishable from regression to the mean?

**No, and not because the data is too thin. Because they are the same statement.** Fit
`actual = a + b*proj` per position, and a tier's mean bias decomposes exactly:

```
mean(proj - actual)  =  (1-b)*mean(proj)   -   a    -   mean(residual)
                        spread term          level     anything ADP-specific
```

Blend, no bonus, per-position lines fitted on all 449 (section G):

| Tier | n | Raw bias | Spread term | Level term | Residual | t | p |
|---|---|---|---|---|---|---|---|
| ADP 1-24 | 24 | +47.7 | +67.0 | -15.9 | -3.4 | +0.21 | 0.839 |
| ADP 25-60 | 36 | +45.1 | +56.1 | -13.8 | +2.8 | -0.21 | 0.839 |
| ADP 61-120 | 59 | +36.4 | +42.5 | -12.1 | +6.0 | -0.84 | 0.405 |
| ADP 121+ | 60 | +15.8 | +24.9 | -10.3 | +1.2 | -0.19 | 0.852 |
| Not in ADP | 270 | -6.4 | +7.7 | -12.4 | -1.7 | +0.80 | 0.424 |

The residual is the only column that could be an ADP-specific effect, and it is never
distinguishable from zero. So **none of the +39.0 / +55.9 / +47.7 top-24 bias survives as
something separate**: all of it is what a slope below 1 mechanically produces at that projection
level. Conditioning on high ADP is conditioning on high projection, and the tier table was a
restatement of the slope table all along. Caveat, stated plainly: the line is fitted in sample on
these same players, so this is a decomposition rather than an out-of-sample test. It shows the two
explanations are one explanation. It does not prove the slope is right.

**A sharper finding sits underneath this, and it changes the story.** The slope identity
`slope = r x sd(actual)/sd(projected)` says a slope below 1 can come from over-dispersion (sd
ratio below 1) or from weak correlation (r below 1). FFA's framing is the first. **Our data is
almost entirely the second.**

| Basis | QB | RB | WR | TE |
|---|---|---|---|---|
| sd ratio, season totals, all 449 | 0.88 | 1.10 | 0.91 | 0.99 |
| sd ratio, draftable players only | 1.31 | 1.37 | 1.35 | 1.52 |
| r, season totals, all 449 | 0.81 | 0.81 | 0.76 | 0.82 |
| r, draftable players only | 0.45 | 0.74 | 0.58 | 0.46 |

Read the second row. Among drafted players, **actual outcomes were more spread out than the
projections were, at every position** -- the projections were under-dispersed, the opposite of
"too spread out". The slope falls below 1 because the correlation is weak (0.45 to 0.74), not
because the range is too wide.

That does not overturn the prescription: shrinking toward the mean is the MSE-optimal response to
a weakly-correlated forecast, which is why the arithmetic is the same either way. But it changes
what the finding *is*. It is not "our sources exaggerate the spread between players". It is
"outcomes among drafted players are only loosely predictable, and a projection stated as a point
estimate implies more knowledge than the source has". Those lead to different remedies: the first
argues for rescaling the numbers, the second argues for widening the uncertainty around them and
leaning on structure (roster slots, replacement level, ADP) rather than on small numeric gaps.

## 3. What would a per-position shrink do to the QB premium?

**Verified. The sign is right and the magnitude is right under the slopes the estimate assumed.**
Board built read-only from the live pipeline (188 players, blend source), each position's PPG
compressed toward its own positional mean by that position's slope, then re-valued through the
real `compute_draft_values` with baselines recomputed. Nothing was applied to the shipped board.

| Slope basis | QB / RB / WR / TE | Josh Allen DV | His overall rank | QBs in top 10 | QBs in top 30 |
|---|---|---|---|---|---|
| **current board, no shrink** | -- | **108.75** | **#9** | 1 | 5 |
| FFA, 12 seasons | 0.67 / 0.79 / 0.85 / 0.72 | 72.87 | #11 | 0 | 3 |
| ours, season totals (F1) | 0.71 / 0.89 / 0.69 / 0.81 | 77.71 | #10 | 1 | 3 |
| ours, PPG, draftable (F5) | 0.53 / 0.93 / 0.97 / 1.01 | **57.76** | **#24** | 0 | 1 |
| ours, PPG, all >=8 games (F4) | 0.38 / 0.92 / 0.83 / 0.99 | 41.47 | #32 | 0 | 0 |

On the coordinator's own basis: **108.75 to 72.87, from #9 to #11, out of the top 10.** "About 107
to the low 70s and out of the top 10 entirely" is correct on all three counts.

Two things the estimate did not anticipate, one in each direction.

- **Under our own season-total slopes the relative move nearly vanishes.** Allen loses 31 points
  of value but only one rank spot (#9 to #10), because our WR slope (0.69) is as harsh as our QB
  slope (0.71) and receivers fall with him. The QB-versus-WR rotation is entirely a consequence of
  FFA's WR 0.85, which our season is the one dataset that disagrees with.
- **Under the basis most defensible for this board it is far worse than estimated.** Allen falls
  to #24 and the board keeps one QB in the top 30 instead of five.

The mechanism is exact, not fitted: **every position's mean EVoB scales by that position's slope,
to two decimals** (QB 0.53x at slope 0.53, RB 0.93x at 0.93, WR 0.97x at 0.97, TE 1.01x at 1.01).
Under a mean-preserving shrink the player and the replacement level both move toward the same
positional mean, so the gap between them -- which *is* EVoB -- scales by exactly `b`. Which means
a per-position shrink is not a projection correction at all in effect. **It is a cross-position
reweighting of the board**, and one that never changes the order within a position, only across
positions.

### Which slope basis is the defensible one, and why it is also the shakiest

The board values in points per game and models availability separately, through the fitted
rank-conditional games curve. A slope fitted on *season totals* has availability error baked into
it, so applying that slope to PPG charges the board twice for the same missed games. The PPG fit
on draftable players (F5) is therefore the right basis in principle. It is also the weakest
evidence in the document:

- **n = 30 quarterbacks.** CI 0.24 to 0.97.
- **It flips with the source.** ESPN's QB rate slope is 0.86 (its QB projections have a narrow
  spread, sd 2.19); Sleeper's is 0.43 (sd 3.93). The blend's 0.53 is not a property of "QB
  projections", it is a property of *which* QB projections.
- **Its population excludes everyone who missed the season**, because an actual PPG needs a
  denominator. That is a real selection effect and it flatters both sources.
- **Its whole-pool cousin (F4) is contaminated** by a divisor artifact: a backup projected for a
  near-zero season total over a 17-game divisor lands at a projected PPG near zero (Mariota 0.47,
  Cousins 0.41, Mac Jones 0.39) and then plays 10-14 real games at 10-12 PPG. That is what drags
  F4's QB slope to 0.38, and it is not a rate miss.

So the parameter that would do the most damage to the league's central thesis is the one this
season identifies least well. That is the worst possible combination for acting.

## 4. The integrity point: the board applies one adjustment and not the other

Both adjustments were measured in the same currency, on the same players (section I; 2025 season
league points, blend projection, shrink using the F1 season-total slopes):

| Tier | n | Bonus adds | Shrink would remove | Ratio |
|---|---|---|---|---|
| ADP 1-24 | 24 | +14.0 | -39.6 | 2.8x |
| ADP 25-60 | 36 | +9.5 | -30.6 | 3.2x |
| ADP 61-120 | 59 | +5.3 | -20.2 | 3.8x |
| ADP 121+ | 60 | +1.6 | -4.2 | 2.7x |

**The omitted adjustment is roughly three times the size of the applied one, at every tier.** In
draft-value terms the gap is wider still: `tools/compare_bonus_effect.py` measures the bonus as
+1.6 DV for the top QB (87.6 to 89.2) and +6.1 for the top RB (137.4 to 143.5), while the shrink
removes 31 to 51 points from Allen.

So the concern is legitimate in structure: the pipeline ships the adjustment that raises the top
of the board and not the one that would lower it, and the one it skipped is the larger. But it is
**not** a thumb on the scale for quarterbacks. The bonus adds +0.10 PPG of top-QB-to-replacement
spread against +0.36 for RB, so it already moves value *away* from QB, in the same direction the
shrink would. There is no scenario in this data where the current configuration flatters
quarterbacks relative to a fully-adjusted board.

## 5. What we recommend, and what we are not recommending

**Do not apply a per-position shrink on this evidence.** Not because the finding is wrong -- the
direction is corroborated by twelve seasons of independent work -- but because:

1. The consequential parameter is unidentified. QB at 0.53 with a CI of 0.24 to 0.97, from 30
   players, flipping to 0.86 if you fit ESPN alone.
2. The *relative* effect depends on the one number where our data and FFA's disagree most
   (WR: 0.69 versus 0.85), and relative is all that matters for a board.
3. A one-season slope applied to a 15-round draft is the same class of error as reweighting
   sources on a one-season MAE gap, which Part 1 declined to do. The consistency cuts both ways.
4. The shrink would be a cross-position reweighting dressed as a projection fix, and it would
   attack the league's 2QB thesis on weaker evidence than the thesis itself rests on. The 2QB
   edge comes from replacement level -- 10 teams x 2 QB means QB22 is the baseline -- which is a
   *counting* fact about roster slots, not a projection-accuracy claim. A shrink does not touch
   the counting; it compresses the gap above it.

**What the evidence does support right now:**

- Treat point estimates at the top of the board as less informative than their precision
  suggests, especially at QB. Part 1 already found projection-to-outcome correlation of -0.08 and
  +0.03 inside the top 24 by ADP; this part adds that among drafted players outcomes are *more*
  dispersed than projections at every position. Small numeric gaps between early-round players
  are not signal.
- Run this tool every January and accumulate seasons. Both the source-weight question and the
  calibration question need the same thing, and neither can be settled from one draw.
- If a shrink is ever applied, apply it to PPG (not season totals, which double-counts
  availability), fit it on the draftable population, and require multiple seasons per position
  before letting the QB coefficient differ from the others. And say on screen that it is on.

**One season, 66 quarterbacks, 30 of them drafted.** Everything in this part is one draw of the
season, and the QB numbers rest on the smallest sample in the document.
