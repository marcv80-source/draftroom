# Draftroom

A personal fantasy football draft assistant for one non-standard league.

**Status:** in development, for a private draft on September 8, 2026. Not distributed, not published
to any app store, no other users, no commercial use.

---

## Why this exists

My fantasy league uses rules that public rankings do not account for:

- **Two mandatory starting quarterbacks** (not superflex, two dedicated QB slots)
- **No kickers**
- **No team defenses**
- **A short bench**

Every published ranking, cheat sheet, and draft tool is built for one-quarterback leagues that start a
kicker and a defense. In this 10-team league starting two quarterbacks, 20 QBs must be started
every week across 17 weeks, so replacement-level quarterback sits at QB22 rather than around QB11. That single
difference re-prices the entire draft board, and no off-the-shelf list reflects it.

The league also drafts in person, on a board, with physical stickers. There is no synced draft room to
read from, so the tool doubles as the live record of who has been taken.

## What it does

1. **Reads the league's own rules** (scoring modifiers, roster positions) and applies them to player
   statistical projections, so player values are derived from this league's actual point values rather
   than borrowed from a generic ranking.
2. **Blends four independent projection sources** into an equal-weight composite, on component
   stats rather than on points. Equal weight is measured, not assumed: a 2025 backtest put the
   best-weight interval at 0.30-1.00, so no reweighting is justified on one season
   (`docs/archive/SOURCE_BACKTEST.md`).
3. **Derives rankings from projections**, and recomputes them as players come off the board. A ranking
   is treated as an output, not an input.
4. **Estimates availability** using average draft position and its standard deviation, so the question
   "can I wait a round on this position?" gets a probability rather than a guess.
5. **Explains its recommendations** in plain language, each point backed by a computed number, and
   always offers the fallback options. It informs; it does not insist.

## Design constraints

- **Runs entirely locally.** The draft is in a room with unreliable wifi, so the tool separates an
  online preparation phase from an offline draft phase. On draft night it rebuilds the board from
  the newest cached source payloads under `data/raw/` and makes no network call at all -- a socket
  guard is installed at startup and verified to block outbound connections before the server binds.
  There is deliberately no snapshot artifact: see "Known gaps" below.
- **Crash-safe.** Draft state is an append-only event log, flushed to disk before the interface
  acknowledges a pick. A crash mid-draft recovers by replay in a few seconds.
- **Keyboard driven.** Recording a pick is a few characters and Enter, because the operator is also a
  participant in the draft.
- **Gated, with one gap stated openly.** The crosswalk-completeness gate and the sanity invariants
  both run and both must pass before a number is shown (`tools/run_invariants.py`). The third gate
  originally planned -- reconciling this engine's scoring against Yahoo's own recorded point totals
  -- was never implementable, because Yahoo developer access was never granted. See "Known gaps".

## Data sources

| Source | Use |
|---|---|
| Sleeper | Player universe, identifiers, statistical projections |
| Fantasy Football Calculator | Average draft position and its standard deviation, in two-quarterback format |
| FantasyPros | Statistical projections and expert consensus (manual CSV export, no API) |
| ESPN | Statistical projections including receiving targets |
| FantasySharks | Statistical projections including targets and per-game yardage-threshold counts |
| DynastyProcess | Cross-source player identifier crosswalk |

League settings, roster rules and scoring were read by hand from the league's own Yahoo settings
page (`data/league_manual.yaml`); there is no Yahoo API integration.

## Non-goals

This is not a product. It is not published, not monetized, has a single user, and redistributes no
data from any provider. It exists to prepare for one draft.

## Technical notes

Python 3.12 backend, local web interface, and an append-only JSONL event log for live draft state.
Credentials are stored outside the repository and are never committed.

## Known gaps

Stated here rather than left to be discovered, both found by an audit on 2026-08-25:

- **There is no frozen snapshot artifact.** The two-phase split is real -- prep writes timestamped
  payloads to `data/raw/`, draft mode reads them offline behind a verified socket guard -- but draft
  mode resolves whatever file is *newest* at launch rather than opening a sealed, checksummed
  snapshot. On draft night with wifi off nothing can move underneath it. The residual risk is narrow:
  an interrupted fetch that leaves a truncated file with the newest timestamp would be loaded, and
  the board verified the night before is not *provably* the board drafted on. Mitigated
  operationally: after final prep, stop fetching.
- **The scoring-reconciliation gate does not exist.** It was specified as re-scoring real players
  against Yahoo's own recorded season totals. Yahoo developer access was never granted, so there is
  nothing to reconcile against and the gate was never written.
