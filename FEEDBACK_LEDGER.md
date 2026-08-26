# Feedback ledger — draftroom

One ledger for the repo, forever. Rounds are groupings. Item numbers are permanent and never
reused. Nothing exits silently: `deferred` and `dropped` carry a reason and keep appearing in
round summaries until acknowledged.

Lifecycle: `open → planned → implemented → VERIFIED (date + how)`, with `regressed` sending an
item back to `open` while keeping its number.

Priorities: **P0** blocks draft night · **P1** hinders with a workaround · **P2** nice-to-have ·
**P3** someday.

**Draft night is 2026-09-08.** Any item not VERIFIED by 2026-09-07 is a thing Marc works around
in a live room, so the P0/P1 line is "would this cost him a pick or a minute at the table".

---

## Round 1 — Marc's own dry run, 2026-08-25

He drove the app on port 8484 against a throwaway log and talked through it. Eight distinct
items. Three turned out to be questions of fact that I measured rather than guessed at, and the
answers changed what the fix is in two cases.

---

### #1 — Source toggle looks inert at the top of the board, especially QB  [P2]
- **Source:** Marc, 2026-08-25 dry run · **Round:** R1
- **Status:** VERIFIED 2026-08-25 — **not a bug; it was a display gap.** Fixed by showing the spread on the
  row. live app on :8484, final build (`index-mRWMHVhT.js` / `index-C5_3T7r-.css`): the QB rows carry `±N%` (Allen ±6%, Burrow ±14%), `source-spread` present in
  the served JS and CSS. Measurement that settled it is below.
- **His words:** "when I click on source and I see the blend, I see the sleeper, when I click it
  they barely move at the top... I feel like they're not moving when I change it, it only is at
  the bottom."

**He is factually right, and the reason is real consensus.** Mean absolute rank movement between
the blend board and each single source, bucketed by the blend's own overall rank:

| Blend rank | vs Sleeper | vs ESPN | vs FantasySharks |
|---|---|---|---|
| 1–12 | **1.42** | **1.00** | **1.58** |
| 13–24 | 2.67 | 6.50 | 3.50 |
| 25–48 | 8.79 | 5.96 | 7.71 |
| 49–96 | 12.56 | 8.77 | 13.31 |
| 97–188 | 12.89 | 9.73 | 13.22 |

Josh Allen is QB1 under all five sources. The top of the board genuinely is agreed, and
disagreement grows monotonically with rank. So the toggle is working.

**But the VALUES move a lot where the ranks don't**, and that is what he cannot see. Top 12,
blend → Sleeper: Gibbs 169→144, Bijan 168→140, J.Taylor 137→105, Nacua 134→110. That is 15 to 32
points of draft value on players whose ORDER barely changed.

**And QB moves least in absolute terms, which is exactly the tab he was on.** Allen shifts only
6.6 points blend→Sleeper against Gibbs' 25.5. The full QB1 spread across sources is real though:
Allen is 122 under ESPN and 79 under FantasySharks.

**Fix is display, not model:** show what the toggle actually changed (a delta, or the per-source
figures inline) so "no rank movement" reads as agreement rather than as a dead button.

---

### #2 — No ALL / best-available view across positions  [P1]
- **Source:** Marc, 2026-08-25 dry run · **Round:** R1
- **Status:** VERIFIED 2026-08-25 — ALL is now the landing tab, value-sorted across positions.
  live app on :8484, final build (`index-mRWMHVhT.js` / `index-C5_3T7r-.css`): top 8 reads Gibbs 169.2 RB, Bijan 167.7 RB, J.Taylor 137.4 RB, Nacua 134.0 WR,
  Chase 133.7 WR, CMC 127.7 RB, JSN 116.0 WR, Cook 112.4 RB; all four positions appear in
  the top 25, so it is genuinely merged rather than one position's list. Tier separators
  are suppressed in this view (a tier is a within-position idea).
- **His words:** "I need an all button... especially early in the draft as we're thinking about
  best available and I'm not focused on a position... at all times it might be worth the risk to
  take someone who is of exceptional value."

Board tabs today are QB / RB / WR / TE only. Early rounds are a best-available problem, not a
positional one, and he wants the cross-position view permanently available rather than as a mode
he switches into.

He also asked the right follow-up himself: **"in the all, how do we rate them relative to what's
left on the demand clock?"** That is precisely what draft value (EVoB) already is — value above
the replacement player at that position, given this league's roster rules — so a single
value-sorted list across positions is already an apples-to-apples ranking. It has never been
shown as one. Related to #7: the ALL view is where the scarcity numbers earn their keep.

---

### #3 — Ability to name the teams  [P2]
- **Source:** Marc, 2026-08-25 dry run · **Round:** R1
- **Status:** **dropped — no action needed.** He found it mid-sentence ("which I get, actually I
  see now you have it, got it"). Ten real 2026 league names seed from
  `data/league_manual.yaml`. Kept in the ledger so the round summary is complete.

---

### #4 — Cannot set WHICH SLOT IS HIS from the UI  [P0]
- **Source:** Marc, 2026-08-25 dry run · **Round:** R1
- **Status:** VERIFIED 2026-08-25 — **clarified first; neither of my two guesses was right.**
  New `my_slot_set` event + `POST /api/my-slot` + a "this is me" button on every other
  row of the team panel. live app on :8484, final build (`index-mRWMHVhT.js` / `index-C5_3T7r-.css`): set slot 1, named it, label came back
  `Country Club Boys (YOU)` and `is_my_pick` flipped to true at pick 1. Relaunching with
  `--my-slot 4` still returns the slot from the log. Out-of-range returns 422 and appends
  nothing. Pinned in `tests/test_feedback_round1.py` (6 tests).
- **His words:** "so it looks like you locking me but I need to be able to move the order around."
- **Clarified:** *"All I meant is, at the beginning of the draft, I need to be able to set the
  order and make it clear which one is me. I couldn't figure out how to move the CC Boys away
  from whatever pick it defaulted to."*

Not about reordering names and not about reassigning picks. He is asking for the one thing that
genuinely does not exist: **`my_slot` is fixed at launch** (`--my-slot`, or `draft_slot` in the
yaml) and there is no endpoint and no control to change it. `create_app` reads it once. So when
the draw comes out at the table, the only way to move his seat is to restart the server.

Worse, and this is what actually confused him: naming a slot "Country Club Boys" does **not** move
his seat, because the two are unrelated. `team_label` precedence is name → "YOU" for `my_slot` →
"Team N", so **naming his own team REPLACES the "YOU" marker with the name** and then nothing on
the board says which of the ten teams is him. That behaviour is deliberate and tested
(`test_team_name_endpoint_own_slot_becomes_you_when_cleared_and_named_when_set`), and it is still
wrong for the moment it matters most.

**P0, reclassified up from P1.** Every turn-dependent number on the screen is computed from
`my_slot`: survival to his next pick, the gap to his next turn, the demand-clock window, upcoming
picks, the whole recommendation. If the seat is wrong, all of it is confidently wrong, and the
only current remedy is a restart in the middle of a live draft.

Needs: a slot control in the UI, an event so it survives a relaunch, and a persistent visual
marker for his own team that a name does not erase.

---

### #5 — Cannot draft from the board; thinks the command bar is the only way in  [P0]
- **Source:** Marc, 2026-08-25 dry run · **Round:** R1
- **Status:** VERIFIED 2026-08-25 — the feature existed and was invisible; now it is visible and has a
  one-action path. live app on :8484, final build (`index-mRWMHVhT.js` / `index-C5_3T7r-.css`): served CSS carries
  `.clickable-name.draftable{border-bottom-color:var(--accent-dim)}` (an underline at
  REST, not only on hover) and the bundle carries `onDoubleClick` wired to draft straight
  to the team on the clock. Single click still opens the team picker for the cases that
  need it.
- **His words:** "if I'm clicking on Allen and he's the first pick of the draft I want to be able
  to double click or do something to draft him. The only way to enter right now is that I have to
  go down into the bar at the bottom and type his name."

Click-anywhere drafting shipped 2026-08-20. `TierBoard.tsx` wires every undrafted player's name
to `onOpenDraftMenu`, which opens a team picker defaulting to whoever is on the clock. It works.

**Why he never found it:** the only affordance is
`.clickable-name { border-bottom: 1px dotted transparent }`. Transparent. It becomes visible, and
the text changes colour, **only on hover**. At rest there is no signal whatsoever that a name is
clickable, so he had no reason to try and fell back to typing.

**P0 because of draft night arithmetic:** 150 picks typed by name, under time pressure, in a room,
is the difference between keeping up and losing the board. The fix is small (make it look
clickable, and honour his actual ask of a one-action double-click that drafts to the team on the
clock without the confirm step) and the risk of leaving it is large.

---

### #6 — Recommendation panel says nothing  [P0]
- **Source:** Marc, 2026-08-25 dry run · **Round:** R1
- **Status:** VERIFIED 2026-08-25 — real gap, by design rather than broken, now fixed at the engine
  boundary. `_call_recommend_engine(for_pick=...)` hands `recommend()` a copy of the state
  with the clock moved, so `?target=mine` finally works; the panel defaults to his own
  pick. live app on :8484, final build (`index-mRWMHVhT.js` / `index-C5_3T7r-.css`): at pick 1 slot 4 it returns pick 4, `picks_away` 3, 16 candidates led by
  Josh Allen with "Last man in his tier. Next QB down is 37 points worse." Every preview
  is labelled (`preview_for_pick`) and asking a hypothetical appends no event and does not
  move the clock. Pinned in `tests/test_feedback_round1.py` (5 tests).
- **His words:** "The recommendation, I don't know, like why is it not giving a recommendation? If
  I had the number one pick and I'm going first, it should be making the case for why we should be
  picking Josh Allen or Jahmyr Gibbs or Puka Nacua."

Verified against the running app. `GET /api/recommendation` at pick 1 with slot 4 returns:

```json
{"pick_no": 1, "is_my_pick": false, "candidates": [],
 "warnings": ["Not on the clock -- no recommendation generated."]}
```

`recommend.recommend()` **only ever answers for whoever is on the clock right now** and returns an
explicit "not on the clock" placeholder otherwise. It takes no pick-number argument.
`GET /api/recommendation?target=mine` looks like the escape hatch and is **dead code**: the
endpoint computes his next pick, then hands it to `_recommendation_payload`, which passes it only
to the placeholder branch and throws it away before calling the engine. Confirmed by reading both
functions and by the identical empty response from `?target=mine`.

So the engine's entire output is unreachable until the instant it is his turn — which is the worst
possible moment to start reading, and the opposite of how he wants to use it ("at all times I want
to think about that"). Needs `recommend()` to answer for an arbitrary pick number.

---

### #7 — The demand clock is unintelligible  [P1]
- **Source:** Marc, 2026-08-25 dry run · **Round:** R1
- **Status:** VERIFIED 2026-08-25 — numbers were always correct; the words are now on the row instead of
  in a tooltip. live app on :8484, final build (`index-mRWMHVhT.js` / `index-C5_3T7r-.css`): renders "21 startable QBs left for 20 starting QB jobs -> 1 spare"
  and "10 startable TEs left for 10 starting TE jobs -> NO spare". `demand-clock-sentence`
  present in the served CSS.
- **His words:** "QB says 21 left, first 20 needed. I don't really understand that... I don't
  understand the rationale of what we're saying in that demand clock."

Live values at pick 1: **QB 21 left vs 20 needed (cushion 1)** · RB 32 vs 20 (12) · **TE 10 vs 10
(cushion 0)** · WR 36 vs 30 (6).

What the two numbers actually are:
- **left** = ranked, startable players still on the board at that position
- **needed** = unfilled STARTING slots across the whole league, all ten teams (QB: 10 × 2 = 20)

So "QB 21 vs 20" means *there are 21 startable quarterbacks left for 20 starting quarterback jobs
in this league.* That single line is the entire thesis of the tool — the 2-QB rule is why every
public ranking is wrong here — and it is being delivered as two bare numbers with no subject and
no verb. TE at cushion 0 is arguably just as loud and equally silent.

The tooltips do explain it. He was not hovering, and on draft night he will not hover.

**Fix is words, not maths.** Say it in a sentence.

---

### #8 — IR players are valuable late-round flyers  [P2]
- **Source:** Marc, 2026-08-25 dry run · **Round:** R1
- **Status:** VERIFIED 2026-08-25 — premise verified correct (`BN x6, IR x2`), surfaced as a hint and
  nothing revalued. live app on :8484, final build (`index-mRWMHVhT.js` / `index-C5_3T7r-.css`): `stash-badge` present in the served JS and CSS. Gated on Marc's
  OWN stated condition -- every starter slot filled -- rather than a round number, so it
  stays off the board while he still has real needs. **No valuation changed**, which keeps
  it clear of the beat-a-dumb-null rule; revaluing IR players would have to clear that and
  has not been attempted.
- **His words:** "anyone on injured reserve, as you get further along in the draft, is actually
  pretty valuable once you get to the later rounds, because you're really just going to move them
  to IR immediately and be able to pick up another person after. It's a helpful flyer, obviously
  not when you have real needs."

**Confirmed from `data/league_manual.yaml`: the league has `BN x6, IR x2`.** So the mechanic is
real — draft an IR player in round 14, move him to IR, and the bench spot is free again. Two extra
lottery tickets that cost no roster space.

The config already notes IR slots "cannot hold a healthy player and so add no roster demand",
which is correct for *demand* and is exactly why nothing in the model sees the *option value* he
is describing. Today an IR player is either excluded from the board (Pearsall, because Sleeper
zeroed his stat line) or valued as if he will play, with an injury badge and nothing else.

Note his own caveat, which is the whole scope: this is a LATE-ROUND effect and only when starters
are filled. A model that priced it earlier would be actively harmful. Also note the standing rule
that a correction must beat a dumb null of the same size — so the cheap version (surface it, let
him decide) should be tried before anything is valued differently.

---

## Round 1 status summary

| # | Item | P | Status |
|---|---|---|---|
| 5 | Draft from the board (invisible affordance) | P0 | **VERIFIED** 8/25 |
| 6 | Recommendation unreachable before his turn | P0 | **VERIFIED** 8/25 |
| 4 | Set which slot is his + a marker for it | P0 | **VERIFIED** 8/25 |
| 2 | ALL / best-available view | P1 | **VERIFIED** 8/25 |
| 7 | Demand clock wording | P1 | **VERIFIED** 8/25 |
| 1 | Source toggle looks inert | P2 | **VERIFIED** 8/25 (display fix) |
| 8 | IR late-round flyers | P2 | **VERIFIED** 8/25 (surfaced, nothing revalued) |
| 3 | Team naming | P2 | dropped, no action (he found it) |

Marc's call 2026-08-25: all three phases in one batch. **7 verified, 0 open, 1 dropped, 0
deferred.** Every verification was against the running app on :8484 serving the final build, not
against the code.

### Behaviour deliberately CHANGED, with the tests that pinned the old rule updated

`team_label` no longer lets a name replace the "YOU" marker on his own seat. Three tests pinned
the old rule and were rewritten rather than deleted, each carrying the reason:
`test_team_label_precedence_and_his_own_slot_is_ALWAYS_marked` (was
`..._name_wins_then_you_then_team_n`), `test_team_name_on_his_own_slot_keeps_the_YOU_marker`, and
the label assertion inside `test_all_picks_is_name_aware`.

### Found while building, not reported by Marc

`dataclasses.replace` is a SHALLOW copy, so the hypothetical state handed to the recommendation
engine shared the live `picks` dict. `recommend()` is read-only today, so nothing exploited it --
but the whole point of passing a copy is that the isolation should not depend on an engine keeping
a promise mid-draft. `_call_recommend_engine` now copies the dict and every `Pick` in it. Caught by
its own test (`test_a_hypothetical_state_shares_no_mutable_history_with_the_live_draft`), which
failed on the first attempt.

The `#1` spread was also nearly shipped in the wrong units: `value_by_source` carries league SEASON
POINTS while the column beside it shows DRAFT VALUE, so a raw spread of 22 next to a dv of 106.6
invited reading "106.6 ± 22" when Allen's sources actually span 358-380 points. It ships as a
percentage, which is scale-free and cannot be misread.

### Gates at close of round

861 tests pass (15 new in `tests/test_feedback_round1.py`) · invariant gate 8/8 PASS · `tsc` clean
· frontend build clean · live app serving the new bundle on :8484.

---

## Round 2 — pre-draft readiness and the injury-lag problem, 2026-08-25

Same day as round 1, later session. Marc asked what was left to be live if the draft were
tomorrow, then raised one substantive item off the back of the injury findings.

---

### #9 — Projection sources lag injury news INDEPENDENTLY, so a player can be out for the season while some sources still project a full one  [P0]
- **Source:** Marc, 2026-08-25 · **Round:** R2
- **Status:** implemented 2026-08-25, **partially verified**. See the honest verification note below.
- **His words:** "for all injuries you need to do your own research, kick off an agent to see
  current status of the player, this explains why rankings move and sometimes there is a lag so
  we need to have as up to date as possible... it's possible one of them could pick it up and
  then the others will be behind on that so we'll need to systematically override those cases or
  at a min highlight it... we need to make sure we do not draft a player out for the season as an
  example because 2 of the sources are behind." Then, on scope: "what we need is a job on how we
  want to do these searches as we get closer to draft day, it should be part of the final prep."

**He was right, and MEASURING it made the exposure worse than he stated.** It is not two of four:

- **AVAILABILITY has exactly ONE source.** Sleeper reports a blanket 18.0 games for every player,
  and FantasyPros and FantasySharks publish no games column at all
  (`composite.varying_games_sources`). ESPN is the only source with a real per-player games
  figure, so nothing can disagree with it when it lags.
- **The STATLINE has four**, any of which can keep a full season of yards alive for a dead player.
- **SUSPENSION risk has ZERO.** No feed we fetch prices it. Only reading the news finds it.

**The lag case was real and current.** Jayden Higgins tore his ACL on 2026-08-19 and is on
season-ending IR. The ESPN cache before this session was **2026-08-18**. Without the refresh the
board would have carried him healthy.

**Two failure modes, two existing levers, and the split is the design:**
- season-ending -> `playing_time` override to 0 games, PLUS a **contamination** rejection for any
  source still publishing production. That is a failed identity ("plays zero games, projects
  1,200 yards"), not a distance measure, so it needs no threshold and clears the beat-a-dumb-null
  rule by construction.
- partial absence -> games override only. **The statline is untouched**, because only the volume
  changed and PPG is never moved by an availability judgement.

**Shipped:** `tools/injury_worklist.py` (who to research: designated, source-implied-undesignated,
ADP movers, blind top-N), `tools/injury_sweep.py` (what to do with the answers, `--apply`,
`--only-severe`), `data/injury_research.json` (cited and dated, loader refuses an uncited entry),
`docs/FINAL_PREP.md` (the recurring job), `tests/test_injury_tools.py` (42 tests).

**Applied this round:** Ricky Pearsall and Jayden Higgins at `games: 0` (both season-ending IR,
two outlets each, GM on record for Pearsall).

**Verification, stated honestly:** the tools were run against real refreshed data, the invariant
gate held 8/8, and the 42 tests were mutation-tested (removing the asymmetry guard fails 2,
counting games as production fails 1, failing open on an empty research file fails 1). This is
NOT the ledger's "VERIFIED against the running app" standard, because these are prep-phase tools
with no UI surface. What has NOT been checked in the running app is whether the `NN.NG` playing-time
badge renders for these two players -- and it cannot be, because both are off the valued board
entirely (no source published them), so the overrides are **inert today** and the board logged
exactly that at WARNING. They are kept as standing guards: they bind the moment a later refresh
republishes a row. **A future round should verify the badge on a player who IS on the board.**

**Found while building, not reported:** two distinct ID spaces are both called `player_id`.
`PoolPlayer.player_id` is FFC-derived; the crosswalk, `playing_time.json`,
`projection_decisions.json` and `injury_research.json` all use the **Sleeper** id. Alec Pierce is
`5641` in the first and `8142` in the second, and `5641` in Sleeper's space is a teamless
linebacker named Chris Worley. An override against the wrong id binds to nobody and fails
**silently**. Caught before anything was written; the worklist now prints the correct id, joined
through the pipeline's own `normalize_name` so suffixed names ("Luther Burden III") resolve.

**Also corrected:** `playing_time.py`'s file note claimed "NOTHING is ever added here
automatically", which stopped being true the moment `injury_sweep --apply` existed. It now states
the two routes that write it and requires a citable basis in every reason.

**My own error, caught by the tool's own output:** the first version of the sweep proposed an
UPWARD override for Jordyn Tyson (research implied 12 games, ESPN already credited 10). Research
is now authoritative **downward only** -- a source more pessimistic than the beat writer is being
careful, not lagging, and raising a figure off a press report is the one error direction that
inflates a player Marc then drafts at full value. Pinned by two tests.

---

### #10 — Suspension and other non-injury availability risk is invisible to the whole pipeline  [P1]
- **Source:** surfaced by the R2 research, 2026-08-25 · **Round:** R2
- **Status:** **open — needs Marc's call.** Carried here rather than decided, because any number
  would be invented.

**Josh Jacobs** (ADP 34.6, dv 62.0) was arrested 2026-05-23; the DA's investigation is open and
Schefter reported 2026-08-11 that the NFL is weighing discipline **even if no charges are filed**.
**Puka Nacua** (ADP 4.8, dv 134.8, the #4 player on our board by value) is under open review over
a New Year's Eve incident, with Schefter saying a Week 1 absence is "within the realm of options";
his civil trial slipped to March 2028, which likely pushes discipline past this season.

The `playing_time` lever can EXPRESS this (games missed is games missed) but nothing DETECTS it,
and no source prices it. The choice is a games haircut on an invented number versus a badge and a
human decision in the room. Recommendation on file: badge.

---

### #11 — Availability research must be re-run in the days before the draft, not once in August  [P1]
- **Source:** Marc, 2026-08-25 ("it should be part of the final prep") · **Round:** R2
- **Status:** implemented as `docs/FINAL_PREP.md`; **the second run is still outstanding and is
  calendar-gated.**

The **53-man cutdown is Sunday 2026-08-30, 5pm CT**, nine days before the draft. Active/PUP either
clears or converts to reserve/PUP (a four-game minimum), which resolves Alec Pierce, Zach
Charbonnet and Jordyn Tyson. **Run the whole job again 2026-09-06 or 09-07.**

---

## Round 2 status summary

| # | Item | P | Status |
|---|---|---|---|
| 9 | Sources lag injury news independently | P0 | implemented, partially verified (see note) |
| 10 | Suspension risk invisible to the pipeline | P1 | **open — Marc's call** |
| 11 | Availability research as a recurring final-prep job | P1 | implemented; **second run due 09-06/07** |

**Deferred, still on file with citations, not applied:** Alec Pierce (-> 13.0 games) and Zach
Charbonnet (-> 7.0 games), both awaiting the 08-30 cutdown. Marc's call: "other ones will get
solved potentially over days before the draft."

**Carried over from earlier, still open:** six tests re-anchored by the data refresh are still
failing, and nothing since `28d7ff2` is committed.

---

## Round 3 — Marc's second click-through, 2026-08-25

He drove the freshly-relaunched app on :8484 (throwaway log, slot 5, empty board). Four items.
One is a real disagreement between two screens; one is a small display add; one is a positive
confirmation that closes an earlier item; one is a look-and-feel request he explicitly gated
behind mockups.

---

### #12 — ALL tab and the recommendation panel disagree about who the best pick is  [P1]
- **Source:** Marc, 2026-08-25 second click-through · **Round:** R3
- **Status:** **VERIFIED 2026-08-25** on the running app (:8484, bundle `index-DpWG3uDB.js` /
  `index-DghXfEEb.css`, pid confirmed fresh). Marc chose to make ALL the best-pick-now ranking.
  Board top 3 now reads Josh Allen / Lamar Jackson / Drake Maye and MATCHES the panel exactly;
  `vona_by_pos` = QB 58.7, RB 74.5, WR 58.3, TE 0.0 and `elite_player_ids` = Allen/Lamar/Maye
  are served and consumed. Original diagnosis stands: it was never a math bug.
- **His words:** "on the All tab, it ranks Josh Allen well below a bunch of running backs and
  wide receivers. The question is, why is that deliberate? When I go to the recommendations,
  it's got Josh Allen, Lamar Jackson, and Drake May as the top three before it gets to Jahmyr
  Gibbs and Bijan Robinson. I think there's something off with the logic of how All is
  happening. That should be corrected."

**Measured, from the running app on the blend board at pick 1.01:**

| ALL tab (sorts by `value` = DraftValue/EVoB) | | Recommendation (sorts by `utility`) |
|---|---|---|
| 1. Gibbs RB1 171.0 · 2. Bijan RB2 169.4 · 3. J.Taylor RB3 139.1 · 4. Nacua WR1 134.8 · 5. Chase WR2 134.5 · 6. CMC RB4 129.8 · 7. JSN WR3 117.3 · 8. Cook RB5 114.2 · **9. Josh Allen QB1 105.8** | | **1. Josh Allen** · 2. Lamar Jackson · 3. Drake Maye · 4. Gibbs · 5. Bijan · 6. J.Taylor |

**Why they differ.** The ALL tab sorts by season value above positional replacement. The
recommendation sorts by `utility = E[value] - lam*SD + VONA`. The term that flips the order is
**VONA**, the cost of waiting at that position: **Allen's VONA is 58.7**, the highest on the
board, because the QB he would get at his next turn is 58.7 points worse. The ALL tab does not
price waiting at all.

The supporting numbers all point the same way: Allen is alone in his tier (`tier_size_remaining: 1`),
the drop to Lamar is **36.7 points** (the QB1 cliff CLAUDE.md documents at ~40.9), his probability
of surviving to Marc's next turn at 2.10 is **2.7e-16**, and his ADP is **1.7** against our board
rank of 9. So the room takes him first overall while our board values him ninth, and the
recommendation says take him anyway because it is now or never.

**Allen at overall #9 is also the gate-asserted correct output**, not an accident: the sanity
invariant requires the top QB to rank strictly higher under 2-QB rules than 1-QB rules, and cites
**#9 vs #18 on this exact board**. Sorting ALL differently must not be described as fixing a
wrong number.

**So the defect is real but it is a labelling/affordance defect, not a math one:** the ALL tab's
own tooltip says "best available regardless of position," and by the app's own recommendation
engine that is not what it shows. Three ways to resolve, for Marc to pick:

- **(A) Sort ALL by the same utility the recommendation uses.** The two screens agree. Cost: utility
  is pick- and roster-specific and Monte-Carlo-based, so the board stops being a stable reference
  and becomes a second recommendation that re-orders every pick; it is currently computed for a
  16-candidate shortlist, not for all 199, so this is the expensive option.
- **(B) Keep the value sort, make the disagreement visible** — a VONA or "cost of waiting" column,
  and/or a REC badge on the players the engine is actually recommending. Cheap, board stays stable.
- **(C) Give ALL an explicit two-way sort: "Season value" vs "Best pick now."** He gets both, and the
  labels themselves teach the distinction that is currently invisible.
- **Recommendation on file: (C), with (B)'s cost-of-waiting column included**, defaulting to season
  value. The board's first job is bookkeeping and a stable reference; the pick-now question already
  has a panel, and what is missing is the word that says so.

---

### #13 — ALL tab shows only the overall number; it should show positional rank too  [P2]
- **Source:** Marc, 2026-08-25 second click-through · **Round:** R3
- **Status:** **VERIFIED 2026-08-25** on the running app. Chips render value-ordered: QB1 Allen,
  QB2 Lamar, QB3 Maye; RB1 Gibbs through RB7 Achane.
- **His words:** "On the All tab, it just has the ranking number on the left side. If we want it
  to have our RB1, our RB2, as in showing what their positional rank is, that could be helpful."

Positional rank is already derivable with no server change: the board payload is keyed by
position and each position's list is value-sorted, so the row's index within its own key IS its
positional rank. Renders as `RB1`, `QB1`, `WR3` beside the overall number.

**This is also the likely cause of the findability problem that opened the session.** Marc's first
words on the ALL tab were "I'm not seeing Josh Allen... actually, there it is." At rank 9 in a
merged list with no position label, the only QB in the top 15 was invisible to a scan. Position
labels fix the scan, independently of how #12 is resolved.

---

### #14 — Color scheme should match the Country Club Boys logo  [P2]
- **Source:** Marc, 2026-08-25 second click-through · **Round:** R3
- **Status:** **VERIFIED 2026-08-25.** Mockups built at `docs/feedback/r3/palette-mockups.html`
  (three variants on the real board); **Marc chose B / Clubhouse**. Applied and serving: the old
  ground `#07090d` and the old gold pulse `rgba(245,197,66)` are both gone from the served CSS,
  `#12201d` and `#dbfa5f` are present.
- **His words:** "could we get the color scheme to match better with the colors that are in the
  image or the icon of our team logo... why don't you come up with a couple of mockups and let me
  agree with it before we actually make any changes?"

The logo already ships in the app (`frontend/src/assets/cc-boys-logo.png`, 90x90, Yahoo-served)
and renders in the header, but nothing in the palette derives from it. Current tokens are a
near-black navy ground with a gold accent (`--bg: #07090d`, `--accent: #f5c542`).

**Dominant colors measured off the logo file** (12-bin median-cut quantization, share of pixels):
deep teal-green `#417169` 14.0%, bright mint `#5be1c6` 12.8%, dark navy `#212545` 12.8%, cream
`#f4fdef` 10.5%, lime `#dbfa5f` 9.5%, khaki `#94966f` 9.1%, wine `#95224d` 9.0%, mid-green
`#5eb47c` 6.2%, tan `#d0c18f` 6.2%.

Convenient: the logo's navy `#212545` is close to the existing ground, so a logo-faithful palette
can stay a dark draft-room UI rather than becoming a redesign. **No code change until Marc picks
a mockup.**

---

### #3 — Ability to name the teams  [P2]  — MARC-VERIFIED
- **Update, 2026-08-25 (R3):** Marc confirmed this himself on the running app: "team names, I see
  now how it does. This is coming along." Previously VERIFIED by me only; now verified by the
  person who asked for it. No further work.

---

## Round 3 status summary

| # | Item | P | Status |
|---|---|---|---|
| 12 | ALL tab vs recommendation disagree on best pick | P1 | **open — needs his call between A/B/C** (not a bug; measured) |
| 13 | ALL tab positional rank (RB1/QB1) | P2 | open — trivial, awaiting go-ahead |
| 14 | Palette from the CC Boys logo | P2 | open — **mockups first, his explicit gate** |
| 3 | Team naming | P2 | **VERIFIED by Marc himself** (was verified by me only) |

**Still open from earlier rounds, unchanged:** #10 (suspension risk, needs his call on badge vs
haircut) and #11 (availability job second run, calendar-gated to 2026-09-06/07).

---

## Round 3 close-out, 2026-08-25

All three actionable items VERIFIED against the running app, not against the code. Suite **782
passed**; invariant gate **8/8 PASS**; frontend build clean (tsc); served bundle confirmed to be
the one built, from a process confirmed newly started.

**What the investigation changed, and it changed twice.** My first read told Marc that VONA was
what flipped Josh Allen to first and that RB's VONA was about 16. **Both were wrong** -- 16 was the
counterfactual, RB's actual VONA is **74.5 against QB's 58.7**, and measuring it showed that ranking
purely by pick-now value moves Allen from 9th to **11th**, the opposite of what he wanted. What
actually puts Allen first is a **hard gate**, the elite-QB grab (a top-3 board QB is available and
he holds 0 of 2), which is why the shipped sort applies the gate and badges it `REC` rather than
relying on the VONA term alone.

**Two measurements worth keeping.** (1) `value + VONA(pos)` reproduces the engine's own `utility`
ordering EXACTLY -- all 16 candidates, because utility's continuation term is position-agnostic and
measured 93.1-96.7 across every one of them. That is why the board gets the panel's ranking without
running the panel's Monte Carlo over 199 players. (2) `tier_board()` serves each position in **ADP
order, not value order**, so deriving positional rank from list index produced an ADP rank next to
the value columns -- visible as Lamar reading QB3 above QB2 Maye, and Cook/Henry swapped. Caught on
the running app after the first build and fixed by sorting on value explicitly.

**Process note.** The first restart silently failed: the old process kept port 8484, the new one died
on `[Errno 10048]`, and because static assets are read from disk per request the **new frontend was
being served by the old backend** -- so `vona_by_pos` came back empty and the fix looked inert. This
is the CLAUDE.md hazard in its nastier direction. Verifying the listening process's start time is
what caught it, and it is now part of how this repo gets verified.

**Still open, unchanged:** #10 (suspension risk, needs Marc's call on badge vs games haircut) and
#11 (availability job second run, calendar-gated to 2026-09-06/07).

**Not yet committed.** Nothing from Round 3 has been through `/ship`.

---

### #15 — The left column on the ALL tab looks like random numbers; it should be our rank  [P1]
- **Source:** Marc, 2026-08-26, mid-session on the running app · **Round:** R3
- **Status:** **VERIFIED 2026-08-26** on the running app (bundle `index-CB5ZDYga.js`). The `#`
  column now runs 1, 2, 3 down the list, so the pick the engine leads with reads as **1**. ADP
  moved to its own labelled column. Verified against real drafted state: `#` reads 1-10 in order
  while ADP on those same rows reads 10, 6, 7, 9, 14, 11, 16, 22, 25, 29 -- which is exactly why
  the two could not share a cell.
- **His words:** "on the all tab - you have some random numbers on left of each name, they should
  be in number order, if josh allen is the pick then lets rank him 1!"

**He is right and the numbers were never random: that cell held ADP.** It had held ADP since the
board was built, and adding a column header in the same batch (labelling it `ADP`) explained the
number without fixing the real problem, which is that a board's leftmost column should be OUR rank.
A value-ordered board with an ADP-ordered left column reads as noise, correctly.

Both numbers are worth having on the row, and the GAP between them is the edge in this league, so
ADP was not dropped -- it is its own column now, deliberately the dimmest number on the row.

---

## Round 3 addendum — Codex review, 2026-08-26

`/ship` ran the Codex pass and it returned **"Do not ship this batch yet"**: 3 P1, 2 P2, 2 P3, all
seven fixed, none deferred. Full triage in `docs/reviews/r3-picknow-and-palette-codex.md`. The three
P1s were all real and all silent-in-a-room:

1. **The board reconstructed the panel's ranking instead of reusing it.** `value + VONA` does not
   reproduce `utility` in general -- at the turn the panel optimises a pair, mid-round utility
   carries candidate-specific terms. My 16-of-16 agreement was one board state, not an identity.
   The engine now publishes `Candidate.gate_priority` and the board reuses the panel's own order.
2. **A scarcity floor would have hoisted every remaining player at that position** -- QB23 and
   below above every RB/WR/TE -- because the gate was re-derived from `forced_positions`, which is
   a whole position. This is the exact failure my own code comment claimed was impossible.
3. **A stale or failed recommendation could sort a new board indefinitely**, because a failed fetch
   left the old payload in place. Fine when it fed a panel; not when it drives a sort.

Also found here, only on the live board: **at a back-to-back turn every VONA is legitimately 0.0**,
so the NOW column rendered exactly DV on all 199 rows. Readiness now requires a non-zero price and
the at-the-turn case is explained in words instead.

**Tests added:** `tests/test_picknow_board.py`, 11 cases, mutation-verified. Suite 782 -> **793**.
