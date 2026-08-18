# Manual FantasyPros projections -- weekly download steps

FantasyPros API access needs manual approval and a premium key costs $22.99/mo, so this pipeline
does not use the API and does not scrape the site. Instead, once a week (and again close to draft
night), download four CSVs by hand from the public projections pages and drop them in one folder.
No renaming, no editing.

## The four downloads

For each position, open the URL, click **Export to CSV**, and save the file straight into
**`C:\dev\draftroom\data\manual\`**. Do not rename the downloaded file. Do not change the scoring
dropdown; every URL below already has it set to **Half PPR**, which is this league's format.

| Position | URL |
|---|---|
| QB | https://www.fantasypros.com/nfl/projections/qb.php?week=draft&scoring=HALF |
| RB | https://www.fantasypros.com/nfl/projections/rb.php?week=draft&scoring=HALF |
| WR | https://www.fantasypros.com/nfl/projections/wr.php?week=draft&scoring=HALF |
| TE | https://www.fantasypros.com/nfl/projections/te.php?week=draft&scoring=HALF |

Each page has an **Export to CSV** button below the table. Click it, and your browser downloads
the file with FantasyPros' own name -- leave that name exactly as-is:

```
FantasyPros_Fantasy_Football_Projections_QB.csv
FantasyPros_Fantasy_Football_Projections_RB.csv
FantasyPros_Fantasy_Football_Projections_WR.csv
FantasyPros_Fantasy_Football_Projections_TE.csv
```

Move (or save directly) all four into `C:\dev\draftroom\data\manual\`. That's it -- no renaming
step, because a rename is exactly the thing to forget at 11pm before a draft. The next `fetch_all`
run picks up whichever file is newest for each position automatically.

**Total time: under 3 minutes** -- four page loads, four clicks, one folder.

## If the export button ever misbehaves

These pages sometimes show only a partial table until you scroll or a script finishes loading.
The adapter checks for this (see "What gets checked automatically" below) and will refuse a file
that looks truncated, so a partial grab won't quietly poison the rankings -- but if you hit that
error, try again after the page has fully loaded, or add `&print=true` to the URL first (unverified
as of 2026-08-17 -- if the export button itself ever stops giving the full list, this print view is
the fallback worth trying).

## What gets checked automatically -- you don't have to think about these

- **Wrong file / no file:** if a position's file is missing, that position is skipped with a clear
  note; the other three still load normally.
- **Stale file:** if the newest file for a position is more than 10 days old (by the file's own
  download date), loading it fails loudly rather than quietly using old data. Re-run the download
  above if you see this.
- **Too few rows:** if a position's file comes in far shorter than a normal export (e.g. under 24
  QBs), loading it fails loudly rather than silently thinning the player pool -- re-export it.
- **Column layout drift:** if FantasyPros ever changes their export's columns, loading fails loudly
  instead of silently mis-mapping stats (this is what actually matters -- see the note on rushing
  vs. receiving below).

## Why this matters: rushing vs. receiving yards look identical in the header

The RB and WR exports both have two columns literally labeled `YDS` and two labeled `TDS` --
one pair is rushing, one pair is receiving, and RB and WR are in the OPPOSITE order from each
other (RB: rushing columns first, then receiving; WR: receiving first, then rushing). The
adapter resolves this by fixed column position, verified against the real files, not by the
header text -- so you never have to worry about it. It's flagged here only so you understand why
"the export changed" is treated as a hard failure rather than a best-effort guess.

## Discarded on purpose

The `FPTS` column in every export is read and thrown away. This pipeline re-scores every player
from the raw stats (yards, touchdowns, completions, etc.) using the league's own scoring settings,
so FantasyPros' own point total is never used. None of the four exports include a targets column
either -- that's normal, not a missing download; targets come from a different source.
