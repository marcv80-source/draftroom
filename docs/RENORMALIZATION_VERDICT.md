# Should we renormalize the receiving side to the team passing total?

Measured 2026-08-20. Tool: `tools/check_renormalization.py`
(`.venv\Scripts\python.exe tools\check_renormalization.py [--teams-detail]`).
Tests: `tests/test_renormalization.py` (22). Every number below is printed by that tool from
data already cached under `data/backtest/` and `data/raw/`. Nothing was fetched, nothing was
written, and no correction was applied to the production board.

The tool reuses `tools/backtest_sources.py`'s verified 2025 spine rather than building a second
one: the same hard stat-id gate, the same 449-player population with the same documented join
provenance, the same seeded bootstraps, the same `prep.scoring.score_statline` against
`data/league_manual.yaml`. It adds exactly one thing the backtest never needed — **team
attribution** — and that is where the whole measurement could have gone silently wrong.

---

## The answer, up front

**No. Do not renormalize.**

Three findings, in descending order of how much they should matter to the decision.

1. **Q1: neither side is guilty.** Renormalizing to the passing side assumes the passing side is
   right. On 2025 it was not. Against a complete actuals spine, Sleeper's projected passing came
   in **+4.8% above** its own neutral rushing baseline while its receiving came in **+2.0%** —
   i.e. the *passing* side was the more inflated one, the opposite of what the remedy assumes.
   ESPN's numbers point the other way (receiving −4.1% relative to rushing). And per team it is a
   coin flip: passing was over-projected on 23 of 32 teams, receiving on 23 of 32, and passing was
   the worse of the two sides on **15 of 32 teams for Sleeper and 17 of 32 for ESPN**. There is no
   systematically guilty side to renormalize toward.

2. **Q2: the version of the remedy that helps is not doing what it claims.** The one-sided
   correction (fix only the teams where receiving overshoots) does improve MAE, significantly:
   blend 37.14 → 36.04 overall (p = 0.000) and 56.35 → 53.68 on the 2025 ADP feed (p = 0.000).
   But a **flat haircut of exactly the same league-wide size, distributed uniformly and knowing
   nothing about which team violated anything, gets 36.42 and 54.68** — recovering roughly two
   thirds to three quarters of the gain with none of the machinery. Identity versus its own null:
   −0.38 (p = 0.128) overall, −1.01 (p = 0.089) on the ADP feed, and on ADP 1-60 the flat cut
   actually *wins*. **No identity-based remedy beats its level-matched null anywhere, in any
   population, at any significance.** The gain is a haircut on a board that
   `docs/SOURCE_BACKTEST.md` already measured as running +8 to +48 points hot. The accounting
   identity contributed nothing measurable.

3. **The literal proposal — scale receiving down, two-sided — is actively worse.** Blend MAE
   37.14 → 37.35 overall, and on the top 60 by ADP it costs **+4.9 points per player**
   (Sleeper +7.13, p = 0.072). The reason is mechanical: on the 2025 feeds a third of teams have
   receiving *below* their own passing, usually because the source published few receivers there,
   and a two-sided rule scales those receivers **up** — by as much as +58% (Sleeper) and +60%
   (ESPN). That is not a correction anyone asked for and it is the part that does the damage.

The one result that survives as interesting is not renormalization at all: **`pass_up` on the
top 24 by ADP** (blend 74.48 → 67.55, Sleeper 74.47 → 63.13). It is a QB-level effect on 24
players, and it too fails its null test (−2.78, p = 0.161). Do not act on it. Note also its
direction: raising a team's passing to meet its receiving *lowered* early-round error, which is
the opposite of the assumption behind renormalizing down.

**How much confidence does one season buy? Not much, and less than usual here.** See the last
section — the 2025 feeds are measurably *less* incoherent than the 2026 feeds this remedy would
be applied to, so this is a weaker test than a clean one-season test would be. But the direction
of the evidence is uniform across every cut, and the failure is not "the effect is small" — it is
"the effect is entirely explained by something else."

---

## What had to be got right first

### Team attribution: three different fields, two of them traps

`docs/PROJECTION_CHALLENGES.md` documents the trap — a player's current `proTeamId` is his 2026
team, so using it for 2025 credits every offseason mover's production to his new offense. This
work needs three attributions and hits the trap twice.

| What | Field used | Verified how |
|---|---|---|
| 2025 **actuals** | `proTeamId` on each **weekly** actual stat block | a mid-season trade splits across both offenses; tested |
| **ESPN** 2025 projection | player-level `proTeamId` **inside the 2025 payload** | agrees with the modal 2025 weekly team on **541/577 (93.8%)**; differs from the cached 2026 ESPN payload on **160 players** (A.J. Brown PHI not NE, Mike Evans TB not SF, Kyler Murray ARI not MIN), so the 2025 payload is genuinely 2025-vintage |
| **Sleeper** 2025 projection | the `team` on the **projection row** | agrees with ESPN's 2025 team **524/525 (99.8%)** |

The Sleeper one is the live trap. `row["player"]["team"]` — the embedded player object — is the
**current 2026 record**: it matches the cached 2026 Sleeper universe on 974 of 992 players and
agrees with ESPN's 2025 team on only **373/483 (77.2%)**. Reading it is the documented
offseason-mover error wearing a different key name. All three attributions are asserted on every
run, not warned about, and the assertion is tested against a synthetic 2026-vintage payload.

### The actuals spine had to be swapped, and this is load-bearing

The 2025 backtest payload is a **700-player** ESPN pull. It is fine for per-player scoring — the
backtest's 449 players all have blocks, and 12 of 12 sampled players match nflreadpy exactly —
but it is **not** fine for team totals, because it misses ~2% of the league's production and
misses it unevenly:

| spine | pass_yd vs nflreadpy | rush_yd | rec_yd | worst team `rec` vs `pass_cmp` |
|---|---|---|---|---|
| 2025 payload (700-player window) | 98.0% | 95.8% | 97.5% | **+22.97% (NYJ)** |
| cached 2026 payload's 2025 weekly blocks (1000) | **100.0%** | 100.1% | 100.6% | **−0.62% (NYG)** |

A 23% gap between New York's real receptions and its real completions is arithmetically
impossible; it is one missing quarterback. **A spine that cannot close the identity cannot be
used to decide which projected side violates it**, so Q1 uses the 1000-player spine, which closes
to 0.62% on receptions, 0.68% on yards, and matches nflreadpy's league yardage to 0.1%. The gate
raises rather than warns, and both spines are printed so the choice is visible. This is the same
aggregation `valuation/envelope.py` already validated.

Q2's per-player actuals are untouched: they remain the harness's own season blocks. Team totals
need completeness; per-player scoring needs verification. Different requirements, different
spines, stated.

### Coverage, handled three ways

Sources publish 12-15 receivers per team, so a projected team total can undershoot for no reason
but roster depth.

- **Depth-matched comparison.** If a source published K receivers for a team, its receiving sum
  is compared against that team's **top K** actual receivers. Every Q1 ratio is printed both raw
  and depth-matched, and the verdict never changes between them.
- **The rushing yardstick.** Rushing is not part of the identity, so a source's rushing ratio
  measures its general volume/availability optimism. Shared optimism cancels out of the
  passing-versus-receiving comparison, which is the entire point of Q1.
- **The passer-count confound**, measured rather than assumed. On the 2025 feeds it barely
  applies: only 1 team (Sleeper) and 0 teams (ESPN) had fewer than 2 projected passers. On the
  **2026** feeds it does apply — Sleeper's 2 thin-QB-room teams carry a median receiving gap of
  **+18.7%** against +5.9% for the rest, which is most of the headline TB +21.9%.

### Survivorship

Same rule as the harness: the 7 players with an **empty** 2025 actual block really did record
nothing and are kept, scored as a true zero, because they are exactly the rows that punish an
optimistic projection. **0** players had a projection and no actual block at all, so there is no
judgement call to make this year and the sensitivity is empty rather than argued.

---

## Q0. Is 2025 even a fair test of the 2026 problem?

Same method, both seasons, each source's whole published feed. `(receiving − passing) / passing`
summed per team.

| feed | stat | median | min | max | teams over +1% | teams under −1% |
|---|---|---|---|---|---|---|
| sleeper 2025 | `rec_yd` | **+0.90%** | −36.83% | +22.13% | 16 | 12 |
| sleeper 2025 | `rec` | +1.04% | −37.15% | +18.44% | 17 | 13 |
| espn 2025 | `rec_yd` | −0.17% | −37.56% | +97.62% | 12 | 10 |
| **sleeper 2026** | `rec_yd` | **+6.51%** | −10.94% | +21.89% | **26** | 5 |
| **sleeper 2026** | `rec` | +2.62% | −15.61% | +20.70% | 20 | 6 |
| **espn 2026** | `rec_yd` | **−0.00%** | −0.00% | +0.00% | **0** | 0 |

Three things follow, and all three matter.

**The 2025 Sleeper feed is much less incoherent than the 2026 one**: median gap +0.90% versus
+6.51%, 16 teams over 1% versus 26. And 2025's violations are **two-sided** (12 teams under −1%)
while 2026's are mostly one-directional (5). So 2025 is a partial proxy: it tests the remedy's
*mechanism* honestly, but on a season where there was less to fix and where the fixing cut both
ways. That is stated as a limitation, not smoothed over.

**ESPN's perfect reconciliation is a 2026 property, not a habit.** ESPN 2026 is exactly 0.00% on
all 32 teams; ESPN **2025** was not (median −0.17%, range −37.6% to +97.6%, 12 teams over 1%).
`docs/PROJECTION_CHALLENGES.md` reads ESPN's clean identity as evidence its projections are
"internally reconciled to a team passing budget". True of the current feed. It was not true one
year earlier, so it is not a durable property of the source, and it certainly is not evidence
ESPN's numbers are *right* — that document already says the identity check catches sloppiness and
never catches being wrong.

**ESPN 2025's extremes are a projected-side artifact.** Cleveland at **+97.6%** has ESPN
projecting its entire quarterback room for **310 pass attempts** against a league median of 561 —
an unsettled QB room, not a receiver problem. The same shape shows on the current feed at the
other end of the scale: Sleeper 2026's headline worst team, **TB at +21.9%, has exactly one
projected passer.** A per-team identity gap is a weak instrument precisely because the
denominator is often the thing that is missing.

---

## Q1. Which side is actually wrong?

The framing to answer is: *did projected receiving come in above actual while projected passing
matched actual (receiving guilty), or did projected passing come in below actual (the remedy
aimed at the wrong side)?* Neither. **Both sides came in above actual**, which is availability
optimism and says nothing about the identity — so the comparison that carries information is each
side against the source's own rushing ratio.

League-wide, depth-matched:

| source | stat | projected | actual | ratio | actual (depth) | ratio |
|---|---|---|---|---|---|---|
| sleeper | `pass_cmp` | 12,289 | 11,214 | 1.096 | 11,101 | 1.107 |
| sleeper | `pass_yd` | 134,554 | 122,366 | 1.100 | 121,343 | 1.109 |
| sleeper | `pass_td` | 859 | 811 | 1.059 | 806 | 1.066 |
| sleeper | `rec` | 11,880 | 11,209 | 1.060 | 11,106 | 1.070 |
| sleeper | `rec_yd` | 131,166 | 122,358 | 1.072 | 121,591 | 1.079 |
| sleeper | `rec_td` | 833 | 809 | 1.030 | 798 | 1.044 |
| sleeper | `rush_att` | 15,058 | 14,591 | 1.032 | 14,559 | 1.034 |
| sleeper | `rush_yd` | 67,039 | 63,634 | 1.054 | 63,660 | 1.053 |
| espn | `pass_cmp` | 11,824 | 11,214 | 1.054 | 10,805 | 1.094 |
| espn | `pass_yd` | 131,129 | 122,366 | 1.072 | 118,383 | 1.108 |
| espn | `pass_td` | 801 | 811 | 0.987 | 798 | 1.003 |
| espn | `rec` | 11,639 | 11,209 | 1.038 | 10,861 | 1.072 |
| espn | `rec_yd` | 128,678 | 122,358 | 1.052 | 119,543 | 1.076 |
| espn | `rec_td` | 785 | 809 | 0.971 | 809 | 0.971 |
| espn | `rush_att` | 15,237 | 14,591 | 1.044 | 14,206 | 1.073 |
| espn | `rush_yd` | 68,481 | 63,634 | 1.076 | 62,503 | 1.096 |

The decisive cut — each side against the source's own neutral rushing ratio:

| source | basis | pass ratio | rec ratio | rush ratio | pass/rush | rec/rush | verdict |
|---|---|---|---|---|---|---|---|
| sleeper | full | 1.085 | 1.054 | 1.043 | 1.040 | 1.011 | **PASSING is the outlier (+4.0%)** |
| sleeper | depth | 1.094 | 1.064 | 1.044 | 1.048 | 1.020 | **PASSING is the outlier (+4.8%)** |
| espn | full | 1.038 | 1.020 | 1.060 | 0.979 | 0.962 | RECEIVING is the outlier (−3.8%) |
| espn | depth | 1.068 | 1.040 | 1.084 | 0.986 | 0.959 | RECEIVING is the outlier (−4.1%) |

**Sleeper's passing side is the more inflated one, by 4.8 points of ratio.** That is the side the
remedy treats as the budget. Renormalizing Sleeper's receivers *down* to a passing total that is
itself the further-from-truth number moves the receivers away from the actuals, not toward them.
ESPN's ordering is the reverse, which is its own finding: **the two sources do not agree on which
side is guilty**, so no single-direction rule can be right for both.

And at team level there is nothing systematic to act on at all:

| source | teams | passing over-projected | receiving over-projected | passing the worse side |
|---|---|---|---|---|
| sleeper | 32 | 23 | 23 | **15** |
| espn | 32 | 23 | 22 | **17** |

15 of 32 and 17 of 32 is a coin flip. Whatever the identity violation is, it does **not** localise
to a side of the ball.

---

## Q2. Would the correction have improved 2025 player-level accuracy?

Five identity-based remedies plus two level-matched nulls, all applied per source and then blended
at the component-stat level (which is what the pipeline would actually do, per plan B1 — correct
each source, then average; never average and then correct).

| remedy | what it does |
|---|---|
| `rec_down` | scale receiving to the team's projected passing total — the literal proposal, both directions |
| `rec_down_over` | same, **only** where receiving exceeds passing — the faithful reading, since `valuation/envelope.py` treats only overages as violations |
| `pass_up` | scale passing to the team's projected receiving total |
| `pass_up_over` | same, overage teams only |
| `split` | move both sides to the midpoint, assuming neither is the truth |
| `rec_flat` | **null**: the same league-wide total removed from the same players' receiving, as one uniform multiplier — knows nothing about the identity |
| `pass_flat` | **null** for `pass_up`, same construction |

The nulls are the reason this section can conclude anything. `docs/SOURCE_BACKTEST.md` measured
both sources +8.1 and +9.0 points optimistic overall and +39.0 / +55.9 on the top 24 by ADP. Any
remedy whose net effect is to remove points will improve MAE for that reason alone. The nulls
remove exactly the same total from exactly the same players — verified in the tool's output
(`rec_down_over` mean change −2.17 pts/player, `rec_flat` −2.18) and asserted in a test.

### The size of each correction

| source | remedy | mean \|change\| | max \|change\| | mean change |
|---|---|---|---|---|
| blend | `rec_down` | 4.95 | 128.04 | **+1.24** |
| blend | `rec_down_over` | 2.15 | 55.69 | −2.15 |
| blend | `rec_flat` | 2.15 | 11.29 | −2.15 |
| blend | `pass_up` | 1.78 | 95.94 | −0.45 |
| blend | `pass_up_over` | 0.77 | 52.56 | +0.77 |
| blend | `pass_flat` | 0.44 | 7.39 | −0.44 |
| blend | `split` | 3.36 | 64.02 | +0.39 |

Note `rec_down`'s **positive** mean: on 2025 the two-sided rule is a net *increase* to the
receiving side, because a third of teams had receiving below their own passing. The per-team
multipliers run 0.82 to **1.58** for Sleeper and 0.51 to **1.60** for ESPN.

### MAE in season league points, no bonus (449 players)

| group | n | source | raw | `rec_down` | `rec_down_over` | `rec_flat` | `pass_up` | `pass_up_over` | `pass_flat` | `split` |
|---|---|---|---|---|---|---|---|---|---|---|
| all | 449 | sleeper | 37.47 | 38.20 | **36.64** | 36.80 | 37.26 | 37.71 | 37.38 | 37.40 |
| all | 449 | espn | 38.43 | 38.71 | **37.23** | 37.64 | 38.10 | 38.72 | 38.32 | 38.19 |
| all | 449 | blend | 37.14 | 37.35 | **36.04** | 36.42 | 36.82 | 37.37 | 37.05 | 36.91 |
| in ADP feed | 179 | sleeper | 56.21 | 56.99 | **54.13** | 54.67 | 55.02 | 56.66 | 55.97 | 55.27 |
| in ADP feed | 179 | espn | 58.67 | 59.09 | **55.76** | 56.77 | 57.36 | 59.08 | 58.37 | 57.94 |
| in ADP feed | 179 | blend | 56.35 | 56.26 | **53.68** | 54.68 | 55.11 | 56.82 | 56.11 | 55.42 |
| QB | 66 | blend | 54.55 | 54.55 | 54.55 | 54.55 | **52.36** | 56.10 | 53.95 | 53.25 |
| RB | 108 | blend | 40.94 | 41.55 | 40.79 | 40.79 | 40.94 | 40.94 | 40.94 | 41.15 |
| WR | 179 | blend | 35.87 | 35.98 | **33.53** | 34.39 | 35.87 | 35.87 | 35.87 | 35.72 |
| TE | 96 | blend | 23.25 | 23.34 | 22.63 | 22.82 | 23.25 | 23.25 | 23.25 | 23.14 |
| ADP 1-24 | 24 | blend | 74.48 | 78.69 | 73.08 | 73.10 | **67.55** | 74.70 | 73.14 | 72.75 |
| ADP 25-60 | 36 | blend | 74.40 | 79.80 | 72.93 | **71.73** | 73.72 | 73.92 | 74.63 | 76.46 |
| ADP 61-120 | 59 | blend | 49.46 | 45.36 | **45.07** | 47.59 | 48.63 | 51.05 | 49.14 | 46.68 |
| ADP 121+ | 60 | blend | 45.04 | 43.89 | **42.82** | 44.06 | 45.33 | 45.08 | 45.05 | 44.45 |
| not in 2025 ADP | 270 | blend | 24.40 | 24.81 | 24.35 | **24.31** | 24.69 | 24.47 | 24.41 | 24.64 |

With the per-game yardage bonus included the ordering is unchanged (blend all-449: raw 38.62,
`rec_down` 38.93, `rec_down_over` 37.44, `rec_flat` 37.83, `pass_up` 38.32, `split` 38.37).
Bonuses add 1.4 to 1.6 points of MAE to everything and move no verdict, the same result
`docs/SOURCE_BACKTEST.md` reports for the source question.

### Paired against the published projection

Negative = the remedy helped. Bootstrap 10,000 resamples, seed fixed, paired on the same players.

| population | n | source | remedy | MAE gap | 95% CI | p | read |
|---|---|---|---|---|---|---|---|
| all | 449 | blend | `rec_down` | +0.21 | −0.88 .. +1.39 | 0.714 | not distinguishable |
| all | 449 | blend | `rec_down_over` | **−1.10** | −1.68 .. −0.57 | **0.000** | HELPED |
| all | 449 | blend | `pass_up` | −0.32 | −1.16 .. +0.42 | 0.423 | not distinguishable |
| all | 449 | blend | `pass_up_over` | +0.23 | −0.20 .. +0.68 | 0.307 | not distinguishable |
| all | 449 | blend | `split` | −0.23 | −0.93 .. +0.49 | 0.522 | not distinguishable |
| in ADP feed | 179 | blend | `rec_down_over` | **−2.67** | −4.03 .. −1.46 | **0.000** | HELPED |
| in ADP feed | 179 | blend | `pass_up` | −1.24 | −3.20 .. +0.44 | 0.181 | not distinguishable |
| ADP 1-60 | 60 | sleeper | `rec_down` | **+7.13** | +0.30 .. +15.28 | 0.072 | HURT (marginal) |
| ADP 1-60 | 60 | blend | `rec_down` | +4.92 | −0.57 .. +11.46 | 0.124 | not distinguishable |
| ADP 1-60 | 60 | blend | `rec_down_over` | −1.43 | −3.45 .. +0.52 | 0.168 | not distinguishable |
| ADP 1-60 | 60 | blend | `pass_up` | −3.17 | −7.60 .. +0.05 | 0.115 | not distinguishable |
| WR | 179 | blend | `rec_down_over` | **−2.34** | −3.59 .. −1.23 | **0.000** | HELPED |
| TE | 96 | blend | `rec_down_over` | −0.61 | −1.83 .. +0.34 | 0.277 | not distinguishable |

So: the two-sided proposal never helps and marginally hurts where it matters most. The one-sided
version helps, clearly and repeatedly. Which is why the next table exists.

### The null test — and this is the whole verdict

Identity remedy versus a flat haircut of the same league-wide size. Negative = the per-team
identity beat the flat cut.

| population | n | source | comparison | MAE gap | 95% CI | p | read |
|---|---|---|---|---|---|---|---|
| all | 449 | sleeper | `rec_down_over` vs `rec_flat` | −0.17 | −0.59 .. +0.25 | 0.433 | not distinguishable |
| all | 449 | espn | `rec_down_over` vs `rec_flat` | −0.41 | −1.12 .. +0.22 | 0.239 | not distinguishable |
| all | 449 | blend | `rec_down_over` vs `rec_flat` | −0.38 | −0.88 .. +0.09 | 0.128 | not distinguishable |
| in ADP feed | 179 | blend | `rec_down_over` vs `rec_flat` | −1.01 | −2.21 .. +0.08 | 0.089 | not distinguishable |
| ADP 1-60 | 60 | sleeper | `rec_down_over` vs `rec_flat` | **+0.68** | −1.40 .. +2.57 | 0.510 | flat cut ahead |
| ADP 1-60 | 60 | espn | `rec_down_over` vs `rec_flat` | **+0.93** | −0.87 .. +2.66 | 0.318 | flat cut ahead |
| ADP 1-60 | 60 | blend | `rec_down_over` vs `rec_flat` | **+0.72** | −0.99 .. +2.40 | 0.418 | flat cut ahead |
| WR | 179 | blend | `rec_down_over` vs `rec_flat` | −0.85 | −1.94 .. +0.14 | 0.112 | not distinguishable |
| all | 449 | blend | `pass_up` vs `pass_flat` | −0.23 | −1.03 .. +0.48 | 0.543 | not distinguishable |
| in ADP feed | 179 | blend | `pass_up` vs `pass_flat` | −1.00 | −2.84 .. +0.60 | 0.254 | not distinguishable |
| ADP 1-60 | 60 | sleeper | `pass_up` vs `pass_flat` | −3.98 | −9.72 .. +0.86 | 0.147 | not distinguishable |

The table above is a readable slice; the full run makes **24 identity-versus-null comparisons
(4 populations x 3 sources x 2 nulls) and every single one reads "not distinguishable."**

**Twenty-four comparisons, zero wins.** The per-team identity information is worth −0.17 to −1.01
points of MAE against a flat cut of the same size, every interval contains zero, and on the
early-round players who actually decide the draft the flat cut is ahead. The identity told us
*that* two numbers disagreed; it did not tell us *which players* to move, and moving the ones it
named was no better than moving everyone a little.

That is a clean, interpretable negative result. It is not "renormalization is a rounding error" —
`rec_down_over` moves 2.15 points per player and up to 55.7 — it is "everything renormalization
bought, a haircut bought too."

---

## What this does and does not license

**Do not ship any of the five remedies.** The two-sided proposal is worse than doing nothing on
the part of the board that matters. The one-sided version is a level correction in disguise, and
its own gain is indistinguishable from a flat haircut.

**The identity check keeps its job, unchanged.** It stays exactly what
`docs/PROJECTION_CHALLENGES.md` said it was: the strongest *hygiene* signal in the repo, needing
no fitted band and no assumption, and the right thing to surface in Marc's review queue as
"these two numbers from this source cannot both be true." What this measurement rules out is
letting it move a number automatically. Surfacing an incoherence and knowing how to fix it are
different things, and only the first is supported.

**The level question is a separate, larger, already-decided question.** If the real content of
`rec_down_over` is a downward haircut, then it belongs in the same conversation as
`docs/SOURCE_BACKTEST.md` Part 2's per-position shrink — which is 3x the size, better identified,
corroborated by twelve independent seasons, and was **still declined** on one season of evidence.
Shipping a smaller, worse-identified version of the same correction under the name of an
accounting identity would be the inconsistency, not the caution.

**Two things worth carrying forward as findings in their own right.**

1. **ESPN's identity-clean 2026 feed was not identity-clean in 2025.** Anything that treats
   "reconciles its own arithmetic" as a durable source property should stop.
2. **Sleeper's 2026 receiving overage concentrates on thin-QB-room teams** (median +18.7% on the
   2 teams with fewer than 2 projected passers, versus +5.9% elsewhere). If the review queue wants
   a per-team flag, the passer count is the cheapest and most honest one to attach to it.

---

## How much confidence does one season buy?

Less than the p-values suggest, and this section is not boilerplate.

- **One season, one injury draw.** Same limitation as every other 2025 measurement in this repo.
- **The test season is the wrong shape.** The 2026 Sleeper feed's median team receiving overage is
  +6.51% and one-directional; 2025's was +0.90% and two-sided. The remedy is being judged on a
  season where there was less to fix. This cuts both ways honestly: it means the *positive* case
  was never given its best shot, and it also means the two-sided rule's damage on 2025 (from
  scaling thin receiving corps up) would be smaller in 2026, where only 5 teams sit below −1%.
- **FantasyPros is unmeasurable**, as `docs/SOURCE_BACKTEST.md` established, and
  `docs/PROJECTION_CHALLENGES.md` measured it as the **worst** identity violator (76 of 96
  checks). The source with the biggest violation is the one with no track record, so nothing here
  speaks to it at all. That is an argument against a source-wide rule, not for one.
- **What would change the answer.** An identity remedy that beat its level-matched null on a
  second season, or a per-team violation that predicted *which* receiver was wrong rather than
  just that the team's total was. Neither exists in this data. Re-run this tool each January
  alongside `backtest_sources.py`; both questions need the same accumulating seasons.

**What this run does buy:** the objection is now answered with a number rather than an argument,
and the remedy has a measured baseline to beat. Anything proposed here later has to clear its own
null, which is a bar nothing has cleared yet.

---

## Reproducing this

```
.venv\Scripts\python.exe tools\check_renormalization.py
.venv\Scripts\python.exe tools\check_renormalization.py --teams-detail
.venv\Scripts\python.exe -m pytest tests\test_renormalization.py -q
```

Read-only over `data/backtest/` (populated by `tools/backtest_sources.py --refresh`) and
`data/raw/`. Never calls `prep/fetch_all.py` — CLAUDE.md documents that a live fetch moves what
`load_latest_raw()` resolves to and breaks unrelated tests. Every bootstrap is seeded, so two
consecutive runs produce byte-identical output.

Gates that must pass before any number above is believed, all of which raise rather than warn:
ESPN stat-id identities in both projection and actual blocks (8 checks, all clean); 2025 team
attribution verified against the modal weekly team, against the 2026 payload, and across sources;
and the actuals spine closing the accounting identity to under 2%.
