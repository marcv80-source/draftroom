r"""The projection review page: every flagged outlier, ranked by board impact, keep or reject.

PREP-TIME ONLY. Draft night opens a frozen snapshot read-only, asserts no outbound network, and
must never depend on this tool or on anything it writes. Adjudicating a projection is a prep
activity, and the draft phase has no code path into this file.

Marc's decision, ``docs/archive/PLAN_2026-08-20.md`` ("Marc's decisions, round 2"): *"I'd like to have
outliers brought to me and highlighted and then we make decisions around whether to boot it or
not."* So this generates ONE self-contained HTML page -- one row per pending decision, every
source's number side by side with the flagged one marked, why it fired, what it would do to the
board, and a keep/reject control that **defaults to keep**. Opening the page and clicking nothing
changes nothing. A button copies the decisions Marc actually touched to the clipboard as JSON,
which ``--apply`` then merges into ``data/projection_decisions.json``.

Reads only cached files under ``data/raw/`` and ``data/manual/``. No network call. Never run
``prep/fetch_all.py`` to "refresh" for this -- CLAUDE.md documents that it moves what
``load_latest_raw()`` resolves to and breaks unrelated tests.

Run:
    C:\dev\draftroom\.venv\Scripts\python.exe tools\review_outliers.py
    ... --limit 300              render more rows (default 150 of however many exist)
    ... --json queue.json        also dump the whole queue as JSON (the server payload shape)
    ... --no-open                do not launch a browser
    ... --apply pasted.json      merge a pasted clipboard export into data/projection_decisions.json
    ... --detectors distance     run only these detector groups (repeatable)
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from draftroom.valuation import candidates as C  # noqa: E402
from draftroom.valuation import decisions as D  # noqa: E402
from draftroom.valuation.disagreement import DISAGREEMENT_CAVEAT  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "data" / "review"

#: Rows rendered by default. The queue is ranked by board impact, so the cut falls on the rows
#: that cannot move a pick -- but the count of what was cut is printed on the page, never hidden.
DEFAULT_LIMIT = 150

#: However tight ``--limit`` is, render at least this many of EVERY detector's own top rows.
#: Without it a flooding detector could push a smaller detector's findings off the page entirely,
#: and "no rows" would be indistinguishable from "the detector never ran".
PER_DETECTOR_FLOOR = 8

ATTRIBUTION = (
    "Fantasy data provided by Yahoo Fantasy. ADP and its standard deviation from Fantasy "
    "Football Calculator (2QB, published at 12 teams -- a proxy for this 10-team room, not a "
    "description of it)."
)


# ---------------------------------------------------------------------------
# Row selection
# ---------------------------------------------------------------------------


def select_rows(
    queue: C.ReviewQueue, *, limit: int, per_detector_floor: int = PER_DETECTOR_FLOOR
) -> tuple[list[C.Candidate], int]:
    """``(rows to render, how many were left out)``, impact-ranked with a per-detector floor."""
    ordered = list(queue.candidates)
    keep: dict[str, C.Candidate] = {}
    seen_per_detector: dict[str, int] = {}
    for c in ordered:
        n = seen_per_detector.get(c.detector, 0)
        if n < per_detector_floor:
            seen_per_detector[c.detector] = n + 1
            keep[c.row_id()] = c
    for c in ordered:
        if len(keep) >= limit:
            break
        keep.setdefault(c.row_id(), c)
    rows = [c for c in ordered if c.row_id() in keep]
    return rows, len(ordered) - len(rows)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _fmt(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "&mdash;"
    return f"{value:,.{digits}f}"


def _auto_reason(c: C.Candidate) -> str:
    """A prefilled, editable reason. Every decision needs one; this makes the common case free."""
    dets = "+".join(c.detectors or (c.detector,))
    if c.player_id is None:
        return f"{dets}: {c.source} {c.stat} source-wide"
    return f"{dets}: {c.source} {c.stat} for {c.player_name}"


def _impact_cell(c: C.Candidate) -> str:
    imp = c.impact
    if imp is None:
        return '<span class="dim">not computed</span>'
    if not imp.computable:
        return f'<span class="dim" title="{_e(imp.note)}">n/a</span>'
    if imp.drops_from_board:
        return (
            f'<span class="bad" title="{_e(imp.note)}">falls off the board</span>'
            f'<div class="sub">was dv {_fmt(imp.dv_before)}, rank {imp.rank_before}</div>'
        )
    if imp.scope == "source":
        cls = "warn" if imp.n_players_moved else "dim"
        return (
            f'<span class="{cls}">{imp.n_players_moved} player(s) move</span>'
            f'<div class="sub" title="{_e(imp.note)}">worst {imp.dv_delta:+.1f} dv '
            f"({_e(imp.worst_player)})</div>"
        )
    cls = "bad" if imp.dv_delta < 0 else "good"
    rank = ""
    if imp.rank_before and imp.rank_after:
        arrow = "&#8594;"
        rank = f'<div class="sub">rank {imp.rank_before} {arrow} {imp.rank_after}</div>'
    return f'<span class="{cls}">{imp.dv_delta:+.1f} dv</span>{rank}'


def _values_cell(c: C.Candidate, sources: tuple[str, ...]) -> str:
    if not c.values_by_source:
        return '<span class="dim">&mdash;</span>'
    cells = []
    for s in sources:
        value = c.values_by_source.get(s)
        if s in c.unpublished_by:
            body = '<span class="dim" title="this source publishes no such column">no col</span>'
        elif value is None:
            body = '<span class="dim" title="this source has no row for this player">no row</span>'
        else:
            body = _fmt(value)
        flagged = " flagged" if s == c.source else ""
        cells.append(
            f'<div class="v{flagged}"><span class="vs">{_e(s)}</span>'
            f'<span class="vv">{body}</span></div>'
        )
    return '<div class="values">' + "".join(cells) + "</div>"


def _control_cell(c: C.Candidate, index: int) -> str:
    """The keep/reject control. ``index`` names the radio group.

    A row id ("fantasysharks|pass_td|4046") is a fine attribute value but a poor radio ``name``
    -- radio exclusivity is an exact string match on that attribute, and a source-wide row's id
    carries spaces, parentheses and an asterisk. A plain integer cannot be got wrong.
    """
    if not c.actionable:
        return (
            '<div class="noctl">no keep/reject &mdash; the number is missing, not wrong. '
            "Fix with a <code>data/overrides.csv</code> entry.</div>"
        )
    rid = _e(c.row_id())
    return f"""<div class="ctl" data-row="{rid}">
  <label class="opt keep"><input type="radio" name="v{index}" value="keep" checked> keep</label>
  <label class="opt rej"><input type="radio" name="v{index}" value="reject"> reject</label>
  <input class="reason" type="text" value="{_e(_auto_reason(c))}" placeholder="reason (required)">
</div>"""


def _row_html(c: C.Candidate, sources: tuple[str, ...], index: int) -> str:
    adp = f"{c.adp:.1f}" if c.adp is not None else "&mdash;"
    who = (
        f'<div class="name">{_e(c.player_name)}</div>'
        f'<div class="sub">{_e(c.pos)} {_e(c.team)} &middot; ADP {adp}</div>'
    )
    payload = {
        "source": c.source,
        "stat": c.stat,
        "player_id": c.player_id,
        "player_name": c.player_name,
        "detector": "+".join(c.detectors or (c.detector,)),
    }
    return f"""<tr class="row sev-{_e(c.severity)}" data-severity="{_e(c.severity)}"
    data-detectors="{_e(' '.join(c.detectors or (c.detector,)))}"
    data-source="{_e(c.source)}"
    data-search="{_e((c.player_name + ' ' + c.source + ' ' + c.stat).lower())}"
    data-payload='{_e(json.dumps(payload))}'>
  <td class="c-who">{who}</td>
  <td class="c-what">
    <div class="pill src">{_e(c.source)}</div>
    <div class="pill stat">{_e(c.stat)}</div>
    <div class="sub">{_e(c.value_label)}</div>
  </td>
  <td class="c-values">{_values_cell(c, sources)}</td>
  <td class="c-why">
    <div class="dets">{''.join(f'<span class="pill det {_e(d)}">{_e(d)}</span>'
                               for d in (c.detectors or (c.detector,)))}</div>
    <div class="reason-text">{_e(c.reason)}</div>
  </td>
  <td class="c-impact">{_impact_cell(c)}</td>
  <td class="c-ctl">{_control_cell(c, index)}</td>
</tr>"""


def _summary_html(queue: C.ReviewQueue, rows: list[C.Candidate], omitted: int) -> str:
    chips = "".join(
        f'<button class="chip" data-filter="{_e(d)}">{_e(d)} '
        f'<span class="n">{n}</span></button>'
        for d, n in sorted(queue.counts_by_detector.items(), key=lambda kv: -kv[1])
    )
    sev = " ".join(
        f'<span class="pill sev-{_e(s)}">{_e(s)} {n}</span>'
        for s, n in sorted(
            queue.counts_by_severity.items(), key=lambda kv: C.SEVERITIES.index(kv[0])
        )
    )
    notes = "".join(f"<li>{_e(n)}</li>" for n in queue.notes)
    suppressed = "".join(
        f"<li><b>{_e(d)}</b>: {n} row(s) explained away by a will-not-play designation</li>"
        for d, n in sorted(getattr(queue, "suppressed_by_injury", {}).items())
    ) + "".join(
        # A player whose games figure Marc set by hand no longer carries an injury row. Listed
        # for the same reason the injury suppressions are: a row that vanished because the
        # question was ANSWERED must not look like a detector that stopped working.
        f"<li><b>settled by your playing-time override</b>: {_e(desc)}</li>"
        for _, desc in sorted(getattr(queue, "settled_by_override", {}).items())
    )
    skipped = "".join(
        f"<li><b>{_e(k)}</b> did not run: {_e(v)}</li>" for k, v in sorted(queue.skipped.items())
    )
    return f"""<section class="summary">
  <div class="cards">
    <div class="card"><div class="k">pending decisions</div><div class="val">{len(queue.candidates)}</div>
      <div class="sub">{queue.n_findings} detector findings, merged one row per (source, stat, player)</div></div>
    <div class="card"><div class="k">rendered here</div><div class="val">{len(rows)}</div>
      <div class="sub">{omitted} lower-impact row(s) omitted &mdash; raise <code>--limit</code></div></div>
    <div class="card"><div class="k">board</div><div class="val">{_e(queue.board_source)}</div>
      <div class="sub">{queue.n_board_players} valued players &middot; {len(queue.sources)} sources</div></div>
    <div class="card"><div class="k">severity mix</div><div class="val small">{sev}</div>
      <div class="sub">severity does NOT order this page &mdash; board impact does</div></div>
  </div>
  <div class="filters">
    <button class="chip active" data-filter="all">all <span class="n">{len(rows)}</span></button>
    {chips}
    <input id="search" type="search" placeholder="filter by player, source or stat">
  </div>
  {'<ul class="notes">' + notes + '</ul>' if notes else ''}
  {'<ul class="notes">' + suppressed + '</ul>' if suppressed else ''}
  {'<ul class="notes warn">' + skipped + '</ul>' if skipped else ''}
</section>"""


def render_page(queue: C.ReviewQueue, *, limit: int = DEFAULT_LIMIT) -> tuple[str, int]:
    """``(html, rows rendered)``. Self-contained: no CDN, no remote font, no external asset."""
    rows, omitted = select_rows(queue, limit=limit)
    sources = queue.sources
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    source_wide = [c for c in rows if c.player_id is None and c.actionable]
    playing_time = [c for c in rows if c.detector == "injury_vs_expected_games"]
    joins = [
        c for c in rows if not c.actionable and c.detector != "injury_vs_expected_games"
    ]
    players = [c for c in rows if c.player_id is not None and c.actionable]

    index = {c.row_id(): i for i, c in enumerate(rows)}

    def table(section_rows: list[C.Candidate], sid: str) -> str:
        if not section_rows:
            return '<p class="dim">none</p>'
        body = "".join(_row_html(c, sources, index[c.row_id()]) for c in section_rows)
        return f"""<div class="tablewrap">
  <table id="{sid}">
    <thead><tr>
      <th>player</th><th>flagged</th><th>every source's number</th>
      <th>why it fired</th><th>board impact<div class="sub">if rejected</div></th><th>decision</th>
    </tr></thead>
    <tbody>{body}</tbody>
  </table>
</div>"""

    return (
        f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Projection review queue &middot; CC Boys Draft Room</title>
<style>
:root {{
  --bg: #07090d; --panel: #0f141a; --panel-raised: #171f28; --border: #26313d;
  --text: #e8edf2; --text-dim: #8fa1b3; --text-faint: #5a6b7b;
  --accent: #f5c542; --accent-dim: #8a6f1f; --danger: #ff5c5c; --danger-bg: #3a1414;
  --ok: #4ade80; --warn: #fb923c;
  --font: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  --mono: ui-monospace, "Cascadia Mono", Consolas, monospace;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; }}
body {{
  background: var(--bg); color: var(--text); font-family: var(--font); font-size: 15px;
  line-height: 1.45; overflow-x: hidden;
}}
.wrap {{ width: 100%; padding: 18px 22px 60px; }}   /* NO max-width: full width on a monitor */
h1 {{ font-size: 22px; margin: 0 0 4px; letter-spacing: -0.2px; }}
h2 {{ font-size: 15px; margin: 30px 0 10px; text-transform: uppercase;
     letter-spacing: 1px; color: var(--text-dim); font-weight: 600; }}
a {{ color: var(--accent); }}
code {{ font-family: var(--mono); font-size: 0.9em; color: var(--accent); }}
.lede {{ color: var(--text-dim); margin: 0 0 16px; max-width: 90ch; }}
.dim {{ color: var(--text-faint); }}
.good {{ color: var(--ok); }}
.bad {{ color: var(--danger); }}
.warn {{ color: var(--warn); }}
.sub {{ font-size: 11.5px; color: var(--text-faint); }}

.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
          gap: 10px; margin-bottom: 14px; }}
.card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 6px;
         padding: 10px 12px; }}
.card .k {{ font-size: 11px; text-transform: uppercase; letter-spacing: 1px;
            color: var(--text-faint); }}
.card .val {{ font-size: 26px; font-weight: 600; line-height: 1.1; }}
.card .val.small {{ font-size: 13px; font-weight: 400; }}

.filters {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 12px 0; }}
.chip {{ background: var(--panel); color: var(--text-dim); border: 1px solid var(--border);
         border-radius: 20px; padding: 5px 11px; font: inherit; font-size: 12.5px;
         cursor: pointer; }}
.chip:hover {{ border-color: var(--accent-dim); color: var(--text); }}
.chip.active {{ background: var(--accent); color: #1a1400; border-color: var(--accent);
                font-weight: 600; }}
.chip .n {{ opacity: 0.7; margin-left: 3px; }}
#search {{ background: var(--panel); border: 1px solid var(--border); color: var(--text);
           border-radius: 20px; padding: 6px 12px; font: inherit; font-size: 12.5px;
           min-width: 220px; flex: 1 1 220px; }}

ul.notes {{ margin: 6px 0 0; padding-left: 20px; color: var(--text-dim); font-size: 13px; }}
ul.notes.warn li {{ color: var(--warn); }}

.tablewrap {{ overflow-x: auto; border: 1px solid var(--border); border-radius: 6px;
              background: var(--panel); }}
table {{ border-collapse: collapse; width: 100%; min-width: 980px; }}
th {{ text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 1px;
      color: var(--text-faint); font-weight: 600; padding: 9px 10px;
      border-bottom: 1px solid var(--border); background: var(--panel-raised);
      position: sticky; top: 0; z-index: 2; }}
td {{ padding: 9px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }}
tr.row:hover td {{ background: #121a22; }}
tr.row.touched td {{ background: #141d17; }}
tr.row.rejected td {{ background: #1d1113; }}
.name {{ font-weight: 600; }}
.c-who {{ min-width: 150px; }}
.c-impact {{ min-width: 120px; white-space: nowrap; }}
.c-ctl {{ min-width: 210px; }}
.reason-text {{ font-size: 12.5px; color: var(--text-dim); max-width: 62ch; }}

.pill {{ display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 10px;
         border: 1px solid var(--border); color: var(--text-dim); margin: 0 3px 3px 0;
         font-family: var(--mono); }}
.pill.src {{ color: var(--accent); border-color: var(--accent-dim); }}
.pill.stat {{ color: var(--text); }}
.pill.sev-defect {{ color: var(--danger); border-color: var(--danger); }}
.pill.sev-distance {{ color: var(--warn); border-color: var(--warn); }}
.pill.sev-hygiene {{ color: #60a5fa; border-color: #2c4a70; }}
.pill.sev-badge {{ color: var(--text-faint); }}
tr.sev-defect .c-who .name {{ border-left: 3px solid var(--danger); padding-left: 6px;
                              margin-left: -9px; }}
tr.sev-distance .c-who .name {{ border-left: 3px solid var(--warn); padding-left: 6px;
                                margin-left: -9px; }}

.values {{ display: flex; flex-wrap: wrap; gap: 4px; }}
.v {{ border: 1px solid var(--border); border-radius: 4px; padding: 2px 6px;
      font-family: var(--mono); font-size: 11.5px; min-width: 96px; }}
.v .vs {{ color: var(--text-faint); display: block; font-size: 10px; }}
.v .vv {{ color: var(--text); }}
.v.flagged {{ border-color: var(--danger); background: var(--danger-bg); }}
.v.flagged .vs {{ color: var(--danger); }}

.ctl {{ display: flex; flex-direction: column; gap: 4px; }}
.opt {{ font-size: 12.5px; color: var(--text-dim); cursor: pointer; user-select: none; }}
.opt.rej {{ color: var(--danger); }}
.ctl .reason {{ background: var(--bg); border: 1px solid var(--border); color: var(--text);
                border-radius: 4px; padding: 4px 6px; font: inherit; font-size: 11.5px;
                width: 100%; }}
.noctl {{ font-size: 11.5px; color: var(--text-faint); max-width: 30ch; }}

.actionbar {{ position: sticky; bottom: 0; margin-top: 18px; padding: 12px 14px;
              background: var(--panel-raised); border: 1px solid var(--border);
              border-radius: 6px; display: flex; flex-wrap: wrap; gap: 12px;
              align-items: center; z-index: 5; }}
button.primary {{ background: var(--accent); color: #1a1400; border: 0; border-radius: 5px;
                  padding: 9px 16px; font: inherit; font-weight: 700; cursor: pointer; }}
button.ghost {{ background: transparent; color: var(--text-dim); border: 1px solid var(--border);
                border-radius: 5px; padding: 9px 14px; font: inherit; cursor: pointer; }}
#status {{ font-size: 13px; color: var(--text-dim); }}
#dump {{ width: 100%; min-height: 120px; background: var(--bg); color: var(--text);
         border: 1px solid var(--border); border-radius: 5px; font-family: var(--mono);
         font-size: 12px; padding: 8px; display: none; }}
footer {{ margin-top: 28px; padding-top: 14px; border-top: 1px solid var(--border);
          color: var(--text-faint); font-size: 12px; }}
footer p {{ max-width: 100ch; }}

@media (max-width: 720px) {{
  .wrap {{ padding: 12px 12px 60px; }}
  h1 {{ font-size: 19px; }}
  .card .val {{ font-size: 21px; }}
  table {{ min-width: 760px; }}
  #search {{ min-width: 100%; }}
  .actionbar {{ flex-direction: column; align-items: stretch; }}
  button.primary, button.ghost {{ width: 100%; padding: 12px; }}
}}
</style>
</head>
<body>
<div class="wrap">
<h1>Projection review queue</h1>
<p class="lede">
  Every outlier the pipeline found, ranked by <b>board impact</b> &mdash; what happens to that
  player's draft value if the flagged source were dropped for that stat. <b>Nothing is rejected
  automatically and every control starts on KEEP</b>: open this, click nothing, and no number
  changes. Generated {generated} against the <code>{_e(queue.board_source)}</code> board from
  cached data only.
</p>

{_summary_html(queue, rows, omitted)}

<h2>Source-wide decisions &mdash; one click, whole board</h2>
<p class="lede">A defect or a level bias in a source's entire file. These are the highest-leverage
rows on the page: the decision is <code>(source, stat)</code> with no player attached, which is
the grain <code>blend_statlines(rejected=...)</code> accepts natively.</p>
{table(source_wide, "t-source")}

<h2>Playing time &mdash; a designation the valuation never read</h2>
<p class="lede">An injury designation that sits next to a healthy player's playing time.
<b>Not a keep/reject:</b> no source's number is wrong (an ESPN 17.0 is an ordinary if-healthy
projection), and dropping a source cannot change an availability figure &mdash; so the fix is a
playing-time override. Each row prints the games the board credited and the rank-conditional
availability curve figure for that positional rank, which is the figure for a player nothing
player-specific is known about. <b>No games-missed number is asserted for any designation</b>,
because none is fittable from this repo's cache; the two numbers are shown so the judgment is
yours. Short-term game-status tags (Questionable, Doubtful) never appear here.</p>
{table(playing_time, "t-injury")}

<h2>Per-player decisions</h2>
{table(players, "t-players")}

<h2>Join failures &mdash; not a keep/reject</h2>
<p class="lede">A crosswalk miss means the number is <b>missing, not wrong</b>, so rejecting it is
a no-op. The fix is a line in <code>data/overrides.csv</code>, which is checked first on every
run and is permanent.</p>
{table(joins, "t-joins")}

<div class="actionbar">
  <button class="primary" id="copy">Copy decisions (<span id="count">0</span>)</button>
  <button class="ghost" id="show">Show JSON</button>
  <span id="status">Untouched rows are not exported &mdash; a keep you never considered is not a
    decision. Paste the JSON back and apply it with
    <code>tools\\review_outliers.py --apply &lt;file&gt;</code>.</span>
  <textarea id="dump" readonly spellcheck="false"></textarea>
</div>

<footer>
<p><b>Read the numbers with these caveats attached.</b></p>
<p>{_e(DISAGREEMENT_CAVEAT)}</p>
<p><b>The team accounting identity is a hygiene flag, not a correction.</b> One-sided
renormalization improved 2025 error and then failed to beat a flat haircut of identical magnitude
(p=0.128 overall, the flat cut ahead on the top 60 by ADP), and ordering &mdash; the only thing a
board consumes &mdash; got slightly worse. The honest per-team signal is <b>passer count, not
catcher count</b>: Sleeper's overage runs a median +18.7% on teams listing fewer than 2 projected
quarterbacks against +5.9% elsewhere, so every identity row carries its team's passer count.</p>
<p><b>The TD flag is a badge.</b> R&sup2; near 0.5 outside QB passing yards, and the most
consistent flag it produces is a player who genuinely does score 12 rushing touchdowns. The
aggregate source-level TD row is the stronger of the two and is the one shaped like a decision.</p>
<p>{_e(ATTRIBUTION)}</p>
</footer>
</div>
<script>
(function () {{
  var rows = Array.prototype.slice.call(document.querySelectorAll("tr.row"));
  var countEl = document.getElementById("count");
  var dump = document.getElementById("dump");
  var today = new Date().toISOString().slice(0, 10);

  function touched() {{
    return rows.filter(function (r) {{ return r.classList.contains("touched"); }});
  }}
  function refresh() {{
    countEl.textContent = touched().length;
    if (dump.style.display === "block") {{ dump.value = payload(); }}
  }}
  function payload() {{
    var out = touched().map(function (r) {{
      var base = JSON.parse(r.getAttribute("data-payload"));
      var ctl = r.querySelector(".ctl");
      var picked = ctl.querySelector("input[type=radio]:checked");
      var reason = ctl.querySelector(".reason").value.trim();
      return {{
        source: base.source, stat: base.stat, player_id: base.player_id,
        player_name: base.player_name, verdict: picked ? picked.value : "keep",
        reason: reason || (picked ? picked.value : "keep") + ": reviewed " + today,
        date: today, detector: base.detector
      }};
    }});
    return JSON.stringify({{ schema: 1, decisions: out }}, null, 2);
  }}

  rows.forEach(function (r) {{
    var ctl = r.querySelector(".ctl");
    if (!ctl) return;
    ctl.addEventListener("change", function (ev) {{
      r.classList.add("touched");
      var picked = ctl.querySelector("input[type=radio]:checked");
      r.classList.toggle("rejected", !!picked && picked.value === "reject");
      refresh();
    }});
    ctl.querySelector(".reason").addEventListener("input", function () {{
      if (r.classList.contains("touched")) refresh();
    }});
  }});

  document.getElementById("copy").addEventListener("click", function () {{
    var text = payload();
    var done = function () {{
      var s = document.getElementById("status");
      s.textContent = "Copied " + touched().length +
        " decision(s). Save to a file and run: tools\\\\review_outliers.py --apply <file>";
    }};
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      navigator.clipboard.writeText(text).then(done, function () {{ fallback(text, done); }});
    }} else {{ fallback(text, done); }}
  }});
  function fallback(text, done) {{
    dump.style.display = "block";
    dump.value = text;
    dump.removeAttribute("readonly");
    dump.select();
    try {{ document.execCommand("copy"); }} catch (e) {{}}
    dump.setAttribute("readonly", "readonly");
    done();
  }}
  document.getElementById("show").addEventListener("click", function () {{
    dump.style.display = dump.style.display === "block" ? "none" : "block";
    dump.value = payload();
  }});

  var chips = Array.prototype.slice.call(document.querySelectorAll(".chip"));
  var search = document.getElementById("search");
  var active = "all";
  function apply() {{
    var q = search.value.trim().toLowerCase();
    rows.forEach(function (r) {{
      var okFilter = active === "all" ||
        (" " + r.getAttribute("data-detectors") + " ").indexOf(" " + active + " ") >= 0;
      var okSearch = !q || r.getAttribute("data-search").indexOf(q) >= 0;
      r.style.display = (okFilter && okSearch) ? "" : "none";
    }});
  }}
  chips.forEach(function (c) {{
    c.addEventListener("click", function () {{
      chips.forEach(function (o) {{ o.classList.remove("active"); }});
      c.classList.add("active");
      active = c.getAttribute("data-filter");
      apply();
    }});
  }});
  search.addEventListener("input", apply);
  refresh();
}})();
</script>
</body>
</html>
""",
        len(rows),
    )


# ---------------------------------------------------------------------------
# The JSON payload (also the shape the server should serve -- docs/REVIEW_QUEUE.md)
# ---------------------------------------------------------------------------


def queue_as_json(queue: C.ReviewQueue, *, limit: int | None = None) -> dict:
    """The queue as plain JSON. Same shape ``docs/REVIEW_QUEUE.md`` specifies for the server."""
    rows = queue.candidates if limit is None else queue.candidates[:limit]
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "board_source": queue.board_source,
        "n_board_players": queue.n_board_players,
        "sources": list(queue.sources),
        "n_pending": len(queue.candidates),
        "n_findings": queue.n_findings,
        "counts_by_detector": dict(queue.counts_by_detector),
        "counts_by_severity": dict(queue.counts_by_severity),
        "flooded": list(queue.flooded),
        "skipped": dict(queue.skipped),
        "suppressed_by_injury": dict(getattr(queue, "suppressed_by_injury", {})),
        "settled_by_override": dict(getattr(queue, "settled_by_override", {})),
        "notes": list(queue.notes),
        "candidates": [
            {
                "row_id": c.row_id(),
                "source": c.source,
                "stat": c.stat,
                "player_id": c.player_id,
                "player_name": c.player_name,
                "pos": c.pos,
                "team": c.team,
                "adp": c.adp,
                "values_by_source": dict(c.values_by_source),
                "unpublished_by": list(c.unpublished_by),
                "value_label": c.value_label,
                "detector": c.detector,
                "detectors": list(c.detectors or (c.detector,)),
                "severity": c.severity,
                "reason": c.reason,
                "actionable": c.actionable,
                "detail": {k: dict(v) for k, v in c.detail.items()},
                "impact": (
                    None
                    if c.impact is None
                    else {
                        "scope": c.impact.scope,
                        "computable": c.impact.computable,
                        "note": c.impact.note,
                        "dv_before": c.impact.dv_before,
                        "dv_after": c.impact.dv_after,
                        "dv_delta": c.impact.dv_delta,
                        "ppg_before": c.impact.ppg_before,
                        "ppg_after": c.impact.ppg_after,
                        "points_before": c.impact.points_before,
                        "points_after": c.impact.points_after,
                        "rank_before": c.impact.rank_before,
                        "rank_after": c.impact.rank_after,
                        "rank_delta": c.impact.rank_delta,
                        "drops_from_board": c.impact.drops_from_board,
                        "n_players_moved": c.impact.n_players_moved,
                        "worst_player": c.impact.worst_player,
                        "magnitude": c.impact.magnitude,
                    }
                ),
            }
            for c in rows
        ],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def apply_pasted(path: Path, *, decisions_path: Path | None = None) -> int:
    """Merge a pasted clipboard export into ``data/projection_decisions.json``."""
    incoming = D.parse_decisions(path.read_text(encoding="utf-8"))
    existing = D.load_decisions(decisions_path)
    merged = D.merge_decisions(existing, incoming)
    written = D.save_decisions(merged, decisions_path)
    rejects = sum(1 for d in merged if d.is_reject)
    print(f"applied {len(incoming)} decision(s) from {path}")
    print(f"  {len(existing)} existing -> {len(merged)} total, {rejects} of them rejections")
    print(f"  written: {written}")
    for d in incoming:
        print(f"    {d.describe()}")
    if rejects:
        print(
            "\nEvery rejection must remain VISIBLE on the board -- see docs/REVIEW_QUEUE.md for "
            "the payload contract the UI badge reads."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=None, help="where to write the HTML")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="rows to render")
    parser.add_argument("--json", type=Path, default=None, help="also dump the queue as JSON")
    parser.add_argument("--no-open", action="store_true", help="do not launch a browser")
    parser.add_argument(
        "--apply", type=Path, default=None,
        help="merge a pasted clipboard export into data/projection_decisions.json and exit",
    )
    parser.add_argument(
        "--decisions", type=Path, default=None,
        help="the decisions file --apply merges into (default: data/projection_decisions.json)",
    )
    parser.add_argument(
        "--detectors", action="append", default=None,
        help=f"run only these detector groups (repeatable): {sorted(C.DETECTOR_GROUPS)}",
    )
    parser.add_argument(
        "--distance-rel-min", type=float, default=C.DEFAULT_DISTANCE_REL_MIN,
        help="how far from the other sources' median a value must sit to be SHOWN",
    )
    parser.add_argument(
        "--impact-budget", type=int, default=None,
        help="cap how many rows get the real board-impact column (default: all of them)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("draftroom.prep.crosswalk", "draftroom.prep.sleeper", "draftroom.prep.espn"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    if args.apply is not None:
        return apply_pasted(args.apply, decisions_path=args.decisions)

    print("loading cached sources and building the blend board ...")
    inputs = C.load_review_inputs()
    print(
        f"  sources: {', '.join(inputs.sources)}   ranked players: {len(inputs.adp_of)}   "
        f"board: {len(inputs.board.players)} valued"
    )
    queue = C.collect_candidates(
        inputs,
        distance_rel_min=args.distance_rel_min,
        impact_budget=args.impact_budget,
        include=args.detectors,
    )

    print(f"\n{len(queue.candidates)} pending decision(s) from {queue.n_findings} findings")
    for detector, n in sorted(queue.counts_by_detector.items(), key=lambda kv: -kv[1]):
        print(f"  {detector:<30} {n:>5}")
    for name, why in sorted(queue.skipped.items()):
        print(f"  SKIPPED {name}: {why}")
    for note in queue.notes:
        print(f"  NOTE: {note}")

    print("\ntop 10 by board impact:")
    for i, c in enumerate(queue.candidates[:10], 1):
        imp = c.impact.describe() if c.impact else "n/a"
        who = c.player_name if c.player_id else "(source-wide)"
        print(f"  {i:>2}. {who:<22} {c.source}/{c.stat:<9} {'+'.join(c.detectors):<28} {imp}")

    page, n_rows = render_page(queue, limit=args.limit)
    out = args.out
    if out is None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        out = OUT_DIR / f"projection_review_{stamp}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(queue_as_json(queue), indent=2), encoding="utf-8")
        print(f"\nqueue JSON: {args.json.resolve()}")

    print(f"\n{n_rows} row(s) rendered; page saved to:\n  {out.resolve()}")
    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
