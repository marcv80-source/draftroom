import { useEffect, useMemo, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import { correctPick, search as apiSearch, reassignPick, undraftPick } from "../api";
import { mostRecentPickNo } from "../lib/pickUtil";
import type { DraftPick, DraftState, SearchMatch } from "../types";
import { ContextMenu } from "./ContextMenu";

type RowMenu =
  | { mode: "actions"; pick: DraftPick; x: number; y: number }
  | { mode: "replace"; pick: DraftPick; x: number; y: number }
  | { mode: "reassign"; pick: DraftPick; x: number; y: number }
  | { mode: "confirm-remove"; pick: DraftPick; x: number; y: number };

export interface ResultsTeamOption {
  team_slot: number;
  team_label: string;
}

/** Plan A3: the draft in pick order, grouped by round, reading `all_picks` off the state
 * payload (no extra fetch). Right-click OR the "..." button opens a context menu: Remove,
 * Replace with..., Reassign to team.... Voided picks stay visible, struck through -- this tab
 * is the audit trail, not a filtered view. */
export function DraftResultsTab({
  state,
  onState,
  onError,
  teams,
}: {
  state: DraftState;
  onState: (s: DraftState) => void;
  onError: (msg: string | null) => void;
  teams: ResultsTeamOption[];
}) {
  const picks = state.all_picks;
  const [menu, setMenu] = useState<RowMenu | null>(null);
  const [replaceQuery, setReplaceQuery] = useState("");
  const [replaceMatches, setReplaceMatches] = useState<SearchMatch[]>([]);

  const mostRecent = useMemo(() => mostRecentPickNo(picks), [picks]);

  useEffect(() => {
    if (menu?.mode !== "replace") {
      setReplaceMatches([]);
      return;
    }
    const q = replaceQuery.trim();
    if (!q) {
      setReplaceMatches([]);
      return;
    }
    let cancelled = false;
    const handle = setTimeout(() => {
      apiSearch(q, { includeDrafted: false })
        .then((r) => {
          if (!cancelled) setReplaceMatches(r.matches);
        })
        .catch(() => {
          if (!cancelled) setReplaceMatches([]);
        });
    }, 60);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [replaceQuery, menu?.mode]);

  function closeMenu() {
    setMenu(null);
    setReplaceQuery("");
    setReplaceMatches([]);
  }

  function openRowMenu(e: ReactMouseEvent<HTMLElement>, pick: DraftPick) {
    e.preventDefault();
    e.stopPropagation();
    onError(null);
    setMenu({ mode: "actions", pick, x: e.clientX, y: e.clientY });
  }

  function doRemove(pick: DraftPick) {
    onError(null);
    // undraftPick, not voidPick: removing the newest pick must also rewind the clock, or the
    // replacement lands at the next pick number for the next team (Codex 2026-08-21 finding 2).
    undraftPick(pick.pick_no)
      .then((s) => {
        onState(s);
        closeMenu();
      })
      .catch((err) => onError(String(err)));
  }

  function requestRemove(menuState: { pick: DraftPick; x: number; y: number }) {
    if (mostRecent !== null && menuState.pick.pick_no === mostRecent) {
      doRemove(menuState.pick);
      return;
    }
    setMenu({ mode: "confirm-remove", pick: menuState.pick, x: menuState.x, y: menuState.y });
  }

  function doReplace(pick: DraftPick, playerId: string) {
    onError(null);
    correctPick(pick.pick_no, { playerId })
      .then((s) => {
        onState(s);
        closeMenu();
      })
      .catch((err) => onError(String(err)));
  }

  function doReassign(pick: DraftPick, teamSlot: number) {
    onError(null);
    // Dedicated endpoint. Routing this through correctPick sent player_id: null with no
    // stub_name, which /api/correct rejects 422 -- so "Reassign to team..." never once worked
    // from this menu (Codex 2026-08-21 finding 3).
    reassignPick(pick.pick_no, teamSlot)
      .then((s) => {
        onState(s);
        closeMenu();
      })
      .catch((err) => onError(String(err)));
  }

  if (!picks) {
    return (
      <div className="board-list">
        <div className="empty-hint">
          Draft Results needs `all_picks` in the state payload, which this server build doesn't
          send yet. Bookkeeping is unaffected -- use the tier board and roster panels meanwhile.
        </div>
      </div>
    );
  }

  const rounds = new Map<number, DraftPick[]>();
  for (const p of picks) {
    if (!rounds.has(p.round)) rounds.set(p.round, []);
    rounds.get(p.round)!.push(p);
  }
  const sortedRounds = [...rounds.entries()].sort((a, b) => a[0] - b[0]);

  return (
    <div className="board-list results-list">
      {sortedRounds.map(([round, rows]) => (
        <div key={round}>
          <div className="tier-separator">Round {round}</div>
          {rows.map((p) => (
            <div
              key={p.pick_no}
              className={`results-row ${p.voided ? "voided" : ""} ${p.is_mine ? "mine" : ""}`}
              onContextMenu={(e) => openRowMenu(e, p)}
            >
              <span className="row-adp">{p.pick_label}</span>
              <span className="row-name">
                {p.pos && <span className={`pos-badge ${p.pos}`}>{p.pos}</span>}{" "}
                {p.name ?? <span className="empty-hint">(empty)</span>}
                {p.out_of_order && (
                  <span className="tier-badge" title="Drafted out of snake order">
                    OOO
                  </span>
                )}
              </span>
              <span className="row-team">
                {p.team}
                {p.bye ? ` bye ${p.bye}` : ""}
              </span>
              <span className="row-owner">{p.team_label}</span>
              <button
                className="results-menu-btn"
                title="Row actions (or right-click the row)"
                aria-label={`Actions for pick ${p.pick_label}`}
                onClick={(e) => openRowMenu(e, p)}
              >
                &#8942;
              </button>
            </div>
          ))}
        </div>
      ))}
      {picks.length === 0 && <div className="empty-hint">No picks recorded yet.</div>}

      {menu?.mode === "actions" && (
        <ContextMenu x={menu.x} y={menu.y} onClose={closeMenu}>
          <div className="menu-title">
            Pick #{menu.pick.pick_label} &mdash; {menu.pick.name ?? "(empty)"}
          </div>
          <button className="menu-item danger" onClick={() => requestRemove(menu)}>
            Remove
          </button>
          <button
            className="menu-item"
            onClick={() => setMenu({ mode: "replace", pick: menu.pick, x: menu.x, y: menu.y })}
          >
            Replace with...
          </button>
          <button
            className="menu-item"
            onClick={() => setMenu({ mode: "reassign", pick: menu.pick, x: menu.x, y: menu.y })}
          >
            Reassign to team...
          </button>
        </ContextMenu>
      )}

      {menu?.mode === "confirm-remove" && (
        <ContextMenu x={menu.x} y={menu.y} onClose={closeMenu}>
          <div className="menu-title">Remove pick #{menu.pick.pick_label}?</div>
          <div className="menu-note">
            {menu.pick.name} returns to the pool. This isn't the most recent pick, so it rewrites
            history mid-board.
          </div>
          <div className="menu-actions">
            <button className="menu-item danger" onClick={() => doRemove(menu.pick)}>
              Confirm remove
            </button>
            <button className="menu-item" onClick={closeMenu}>
              Cancel
            </button>
          </div>
        </ContextMenu>
      )}

      {menu?.mode === "replace" && (
        <ContextMenu x={menu.x} y={menu.y} onClose={closeMenu}>
          <div className="menu-title">Replace pick #{menu.pick.pick_label} with...</div>
          <input
            autoFocus
            className="menu-search-input"
            placeholder="Player name"
            value={replaceQuery}
            onChange={(e) => setReplaceQuery(e.target.value)}
          />
          <div className="menu-scroll">
            {replaceMatches.map((m) => (
              <button key={m.player_id} className="menu-item" onClick={() => doReplace(menu.pick, m.player_id)}>
                <span className={`pos-badge ${m.pos}`}>{m.pos}</span> {m.name}{" "}
                <span className="command-hint">{m.team}</span>
              </button>
            ))}
            {replaceQuery.trim() && replaceMatches.length === 0 && <div className="menu-note">No matches.</div>}
          </div>
        </ContextMenu>
      )}

      {menu?.mode === "reassign" && (
        <ContextMenu x={menu.x} y={menu.y} onClose={closeMenu}>
          <div className="menu-title">Reassign pick #{menu.pick.pick_label} to...</div>
          <div className="menu-scroll">
            {teams
              .slice()
              .sort((a, b) => a.team_slot - b.team_slot)
              .map((t) => (
                <button
                  key={t.team_slot}
                  className={`menu-item ${t.team_slot === menu.pick.team_slot ? "suggested" : ""}`}
                  disabled={t.team_slot === menu.pick.team_slot}
                  onClick={() => doReassign(menu.pick, t.team_slot)}
                >
                  {t.team_label}
                  {t.team_slot === menu.pick.team_slot ? " (current)" : ""}
                </button>
              ))}
          </div>
        </ContextMenu>
      )}
    </div>
  );
}
