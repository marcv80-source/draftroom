# Yahoo API access — do this today

This is the only item on the critical path whose timing isn't in our control. Everything about modeling
*your specific room* (how early this league takes QBs, which managers reach) depends on getting last year's
draft results, and that needs API access.

## Step 1 — Apply for access (~5 minutes)

Go to **https://sports.yahoo.com/developer/access/**

Yahoo now manually reviews these. The form asks for:

| Field | What to put |
|---|---|
| Product description | "A personal draft-preparation tool for my own fantasy football league. It reads my league's scoring settings and my prior-season draft results to build custom player rankings, because my league uses two starting quarterbacks and no kickers or defenses, so public rankings don't apply." |
| Data needed | League settings and scoring, roster positions, teams, and prior-season draft results. **Read only.** |
| Intended user base | Choose the **personal / single league use** option. |
| Expected users, 3-6 months | **Small (under 1,000)** — it's one person. |

Be concrete and modest. Incomplete submissions get closed without a reply.

## Step 2 — Create the app (after approval)

Go to **https://developer.yahoo.com/apps/create/** (requires Yahoo login).

- **Application Type:** `Installed Application`
- **Redirect URI:** `https://localhost:8080`
- **API Permissions:** check **Fantasy Sports**, and select **Read** only.

You'll get a **Client ID (Consumer Key)** and **Client Secret (Consumer Secret)**.

## Step 3 — Give them to the tool

Put them in `%LOCALAPPDATA%\draftroom\secrets.json` (this path is outside the repo on purpose — repos acquire
remotes, and secrets must never travel with one):

```json
{
  "yahoo_client_id": "...",
  "yahoo_client_secret": "...",
  "fantasypros_api_key": "..."
}
```

Then run `python -m draftroom.prep.yahoo_auth`. It opens your browser once, you click Approve, and it stores
the token. You never do it again.

## Also today: FantasyPros

Request a key at **https://secure.fantasypros.com/api-keys/request/** and subscribe to premium ($8.99/month —
cancel after the season). That's the whole data budget. Drop the key into the same `secrets.json`.

---

## If approval hasn't landed by Aug 25

We switch to the manual path and lose nothing that matters on draft night. You'll paste two pages from the
Yahoo site you're already logged into:

1. **League Settings** — the roster slots and every scoring value are displayed as plain text.
2. **Draft Results** from last season — the full pick-by-pick board with manager names.

A parser fills the same tables and the model runs identically. The only thing that degrades is the automatic
scoring-reconciliation check, which drops to spot-checking a handful of players' season totals by hand.

Draft order is announced before draft night regardless, so the snake logic is never at risk.

---

## Two technical notes for whoever maintains this

- **Yahoo rotates the refresh token on every refresh.** Each refresh call returns a *new* refresh token and
  invalidates the old one. If the new one isn't persisted atomically, the tool locks itself out and needs a
  full re-auth. `prep/yahoo_client.py` writes to a temp file and `os.replace`s it for exactly this reason.
- **Prior seasons** are reached by reading the current league's `renew` field, which holds the previous
  season's `league_key`. Yahoo keeps three seasons of renewal history.
