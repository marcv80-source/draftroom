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
kicker and a defense. In a 12-team league starting two quarterbacks, 24 QBs must be rostered as
starters every week, so replacement-level quarterback sits around QB27 rather than QB14. That single
difference re-prices the entire draft board, and no off-the-shelf list reflects it.

The league also drafts in person, on a board, with physical stickers. There is no synced draft room to
read from, so the tool doubles as the live record of who has been taken.

## What it does

1. **Reads the league's own rules** (scoring modifiers, roster positions) and applies them to player
   statistical projections, so player values are derived from this league's actual point values rather
   than borrowed from a generic ranking.
2. **Blends multiple projection sources** into a weighted composite, because published accuracy
   studies show no single source is most accurate across all positions.
3. **Derives rankings from projections**, and recomputes them as players come off the board. A ranking
   is treated as an output, not an input.
4. **Estimates availability** using average draft position and its standard deviation, so the question
   "can I wait a round on this position?" gets a probability rather than a guess.
5. **Explains its recommendations** in plain language, each point backed by a computed number, and
   always offers the fallback options. It informs; it does not insist.

## Design constraints

- **Runs entirely locally.** The draft is in a room with unreliable wifi, so the tool separates an
  online preparation phase from an offline draft phase. On draft night it reads a frozen local
  snapshot and makes no network calls at all.
- **Crash-safe.** Draft state is an append-only event log, flushed to disk before the interface
  acknowledges a pick. A crash mid-draft recovers by replay in a few seconds.
- **Keyboard driven.** Recording a pick is a few characters and Enter, because the operator is also a
  participant in the draft.
- **Nothing ships unverified.** Scoring is reconciled against the league host's own recorded point
  totals before any ranking is trusted, and the model carries sanity invariants that halt the pipeline
  rather than emit a number that fails them.

## Data sources

| Source | Use |
|---|---|
| Yahoo Fantasy Sports API | League scoring settings, roster positions, teams, prior-season draft results (read only) |
| Sleeper | Player universe, identifiers, statistical projections |
| Fantasy Football Calculator | Average draft position and its standard deviation, in two-quarterback format |
| FantasyPros | Statistical projections and expert consensus |
| DynastyProcess | Cross-source player identifier crosswalk |

Fantasy data provided by Yahoo Fantasy.

## Non-goals

This is not a product. It is not published, not monetized, has a single user, and redistributes no
data from any provider. It exists to prepare for one draft.

## Technical notes

Python 3.12 backend, local web interface, SQLite for snapshots and an append-only JSONL log for live
draft state. Credentials are stored outside the repository and are never committed.
