# Per-game yardage bonuses: the plan

## The problem in one line

The league pays bonuses for crossing a yardage threshold **in a single game**, but every projection
source in existence publishes **season totals**, and a season total cannot tell you how the yards were
distributed across weeks.

Confirmed bonus schedule (Yahoo Scoring & Settings, league #180947):

| | +3 | +1 more | +1 more |
|---|---|---|---|
| Passing yards | 300 | 400 | 500 |
| Rushing yards | 100 | 150 | 200 |
| Receiving yards | 100 | 150 | 200 |

Two backs both projected for 1,000 rushing yards:

- **Bell-cow**: ten 100-yard games and seven quiet ones → **+30 bonus points**
- **Committee back**: seventeen 59-yard games → **+0 bonus points**

Identical projection. A 30-point gap, which at this league's replacement levels is worth roughly two
and a half rounds of draft capital. Our current engine scores both identically.

## Why this matters more than it looks, especially at quarterback

Passing yards are worth **0.04 points each** (25 yards per point), so the +3 bonus at 300 yards is
worth **75 passing yards** on its own. For a quarterback who clears 300 six or seven times, that is
roughly **20 points of season value that our model currently does not see at all**.

In a two-QB league where replacement is QB22, 20 points is not a rounding error. And it is *unevenly*
distributed: it accrues to high-ceiling passers and almost not at all to game managers with the same
season total. The bonus schedule is, in effect, a variance subsidy the league pays quietly.

Same logic at 100 rushing/receiving yards: the +3 is worth 30 yards, a **30% uplift on that game**.

**Direction of the current error:** every projection we produce is biased LOW, and biased low most for
exactly the players most worth drafting. That is the worst shape of bias to leave in.

## The approach

The bonus is an expectation over a distribution, not a function of the mean:

```
E[season bonus] = games × Σ_k  bonus_k × P(single-game yards ≥ threshold_k)
```

So the entire problem reduces to estimating **P(a given player exceeds a threshold in one game)**.
Everything below is about that one quantity.

Note the structure: because thresholds stack (+3 at 100, +1 at 150, +1 at 200), the marginal bonuses
are independent tail probabilities and simply sum. No special handling needed for the tiers.

## Estimating the per-game distribution — three tiers, ship in order

### Tier 1 — Empirical hit-rate curves (build this first)

The most defensible version, and it needs no distributional assumption at all.

Pull weekly player data for 2019-2025 via **nflreadpy** (`nfl_data_py` is dead; see CLAUDE.md). For
every player-season, compute yards per game and the actual count of games clearing each threshold.
Then, **per position**, bin players by yards-per-game and compute the empirical rate:

```
P(game ≥ 100 | RB averaging 75 ypg)  = (100-yd games by RBs in that bin) / (total games in that bin)
```

That produces a lookup curve per position per threshold. Given a player's projected yards per game,
read the hit rate off the curve and multiply.

**Why this first:** it is simply counting. It cannot be wrong about the shape of reality because it
*is* reality, and it is trivially auditable — anyone can check "RBs averaging 75 ypg cleared 100 in
X% of games" against the raw data.

**Weakness:** it assigns every player at the same yards-per-game the same hit rate, which is exactly
the boom/bust distinction we are trying to capture. Tier 3 fixes that.

### Tier 2 — Parametric fit for smoothness and extrapolation

Bins are noisy at the edges and cannot extrapolate past the observed range. Fit a **Gamma
distribution** per position (right-skewed, non-negative, two parameters, well-behaved for this kind of
count-like yardage data), with the mean pinned to the player's projected yards per game and the shape
parameter fitted from history.

Then `P(Y ≥ threshold)` is a closed-form survival function rather than a bin lookup.

**Validation gate:** the parametric curve must reproduce the Tier 1 empirical rates within a stated
tolerance across the well-populated bins. If Gamma does not fit, try lognormal, and if neither fits,
keep Tier 1 and say so. **We do not ship a smooth curve that disagrees with the counted reality.**

### Tier 3 — Player-specific dispersion (the actual edge)

Two receivers averaging 70 yards per game are not the same. A deep threat with a 15-yard average depth
of target has a fat right tail and clears 100 often; a slot receiver on eight short targets rarely
does, despite an identical average.

Estimate each player's **coefficient of variation** of weekly yardage from his own history, then
**shrink it toward the positional mean** by sample size:

```
cv_player = w · cv_observed + (1 − w) · cv_position,     w = n_games / (n_games + 16)
```

A player with 16 games of history gets 50% weight on his own dispersion. Rookies and role-changers
get the positional prior, which is the honest answer for someone with no history.

Feed the player-specific dispersion into the Tier 2 distribution.

**Flag it honestly:** the shrinkage constant of 16 is a judgment call, chosen so one full season buys
half the weight. It is exposed as a parameter, not buried.

## Validation — how we know it works

This is testable against ground truth, which is unusual and worth exploiting.

1. **Backtest on 2025.** For every relevant player, take their *actual* 2025 season totals, run them
   through the model, and predict bonus points. Compare against the **actual** bonus points they
   earned, computed from real weekly game logs. Report MAE and bias, by position.
   **Pass bar:** mean bias under 2 points per player-season and no systematic sign by position. A
   model that is unbiased on average but wrong per player is still a large improvement on zero.
2. **The bell-cow vs committee fixture.** Two synthetic players with identical season yards and
   different dispersion must receive materially different bonus estimates. This is the whole point,
   and it becomes a unit test.
3. **Sanity bound.** Predicted bonus can never exceed `games × (3+1+1)`, and must be zero for a player
   projected far below the first threshold.
4. **Order preservation.** Adding bonuses must not reorder players *within* a position more than a
   stated amount without a human looking at it. A bonus model that shuffles the board wholesale is
   more likely to be broken than insightful.

## The interaction nobody should miss

Bonuses **reward variance**. The short bench **rewards floor**. These pull in opposite directions and
must not be assumed to cancel.

Concretely: our risk knob λ currently penalises high-variance players. The bonus model will hand those
same players extra expected points. That is not a contradiction — it is the correct accounting of two
real, opposing effects — but if both are tuned independently and carelessly we could double-count in
either direction.

**Rule:** the bonus goes into the **expected points** (the mean), and λ continues to penalise
**dispersion of outcomes**. They operate on different moments of the same distribution and stay
separate. This gets a comment in the code and a test asserting that turning bonuses on does not
silently change the λ penalty.

## Implementation

New module `backend/draftroom/valuation/bonuses.py`:

- `WeeklyDistribution` — per-position parameters plus optional player dispersion override
- `fit_empirical_curves(weekly_df)` → Tier 1 lookup tables, cached to `data/bonus_curves.json`
- `fit_parametric(weekly_df)` → Tier 2 Gamma parameters, gated against Tier 1
- `expected_bonus(stat_line, cfg, dist)` → expected season bonus points, itemised by stat so the UI
  can explain "+18 of his projection is yardage bonuses"
- Wired into `prep/scoring.py` as an **additive term after** the linear dot product, never inside it.
  The linear engine stays a pure dot product; that is what makes the reconciliation gate meaningful.

New `tools/fetch_weekly_history.py` to pull and cache nflreadpy weekly data (prep phase only, never
draft night).

**Ordering:** Tier 1 plus the backtest is the whole minimum viable fix and captures most of the value.
Tier 3 is where the differentiation lives. Tier 2 exists mainly to make Tier 3 possible.

## What could go wrong

- **nflreadpy weekly data may not carry per-game yardage in the shape we need.** Verify before
  building on it; if it does not, the fallback is play-by-play aggregation, which is heavier.
- **The bonus applies to the player's game, and our projections are season totals that already embed
  an injury/games assumption.** Do not double-discount for missed games: bonus is computed per game
  played and multiplied by expected games, using the same `expected_games` the valuation engine uses.
- **Return touchdowns and offensive fumble return TDs** also score in this league (6 points each) and
  no source projects them. They are genuinely unforecastable and will be left at zero, which is a
  small, unbiased-in-expectation omission. Stated here so it is a decision rather than an oversight.

## Honest summary of confidence

Tier 1 is counting and I have high confidence in it. Tier 2 is a standard distributional fit with a
real validation gate. Tier 3 is the part that is genuinely modelling rather than measuring, and its
shrinkage constant is a judgment call. The backtest against 2025 actuals is what makes the whole thing
falsifiable, and it should be built at the same time as Tier 1, not afterwards.
