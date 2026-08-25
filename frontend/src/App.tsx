import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import {
  addStub,
  correctPick,
  draftPick,
  getRecommendation,
  getState,
  openStateSocket,
  search as apiSearch,
  setClock,
  undo,
  undraftPick,
} from "./api";
import { CommandBar } from "./components/CommandBar";
import { DraftResultsTab } from "./components/DraftResultsTab";
import { Header } from "./components/Header";
import { HelpOverlay } from "./components/HelpOverlay";
import { PlayerActionPopover, type ActionMenuState } from "./components/PlayerActionPopover";
import { RecommendationPanel, type PlayerFlagInfo } from "./components/RecommendationPanel";
import { RosterPanel } from "./components/RosterPanel";
import { TeamNamesPanel } from "./components/TeamNamesPanel";
import { TierBoard } from "./components/TierBoard";
import { Ticker } from "./components/Ticker";
import { bindKeys, parseCommand } from "./keys";
import { buildPickNoIndex, mostRecentPickNo } from "./lib/pickUtil";
import {
  ALL_FILTER,
  BOARD_FILTERS,
  POSITIONS,
  type BoardFilter,
  type DraftState,
  type Recommendation,
  type SearchMatch,
} from "./types";

type Mode = "search" | "stub-name" | "stub-position" | "edit-pick-number" | "edit-pick-value" | "jump-clock";
type BoardView = "tiers" | "results";

const STUB_POSITION_KEYS: Record<string, string> = { q: "QB", r: "RB", w: "WR", t: "TE" };

export default function App() {
  const [state, setState] = useState<DraftState | null>(null);
  const [rec, setRec] = useState<Recommendation | null>(null);
  // Ledger #6: defaults to HIS pick, not the clock. `target=mine` collapses to the current
  // pick whenever it IS his turn, so this is strictly more useful at every moment of the draft
  // -- and before this batch it was the mode that returned nothing at all. F2 still flips to
  // whoever is on the clock when he wants to see what an opponent is facing.
  const [recMode, setRecMode] = useState<"clock" | "mine">("mine");
  // Ledger #2: ALL is the landing view. Marc: "especially early in the draft as we're thinking
  // about best available and I'm not focused on a position." Round 1 is a best-available problem,
  // so the board opens on the cross-position list rather than on whichever tab happened to be
  // first.
  const [posFilter, setPosFilter] = useState<BoardFilter>(ALL_FILTER);
  const [helpOpen, setHelpOpen] = useState(false);
  // The "elite QB grab" knob (fix "C"(b), CLAUDE.md/task spec: a visible control). Seeded from
  // the server's own default once state loads, so this UI never hardcodes the spec constant.
  const [eliteQbCutoff, setEliteQbCutoff] = useState<number | null>(null);

  const [mode, setMode] = useState<Mode>("search");
  const [inputValue, setInputValue] = useState("");
  const [matches, setMatches] = useState<SearchMatch[]>([]);
  const [highlighted, setHighlighted] = useState(0);
  const [pendingStubName, setPendingStubName] = useState("");
  const [pendingEditPickNo, setPendingEditPickNo] = useState<number | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // A3: which board view is showing in the center panel.
  const [boardView, setBoardView] = useState<BoardView>("tiers");
  // A1: the team-names setup panel is a modal, opened from the header.
  const [teamNamesOpen, setTeamNamesOpen] = useState(false);
  // A2: the one shared "draft this player" / "confirm undraft" popover, usable from anywhere a
  // player's name appears (tier board, search results, recommendation candidates, rosters).
  const [actionMenu, setActionMenu] = useState<ActionMenuState | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);

  // ---------------------------------------------------------------- initial load + live sync

  useEffect(() => {
    getState()
      .then((s) => {
        setState(s);
        setEliteQbCutoff((prev) => (prev === null ? s.elite_qb_rank_cutoff_default : prev));
      })
      .catch((e) => setErrorMsg(String(e)));
    const ws = openStateSocket(setState);
    return () => ws.close();
  }, []);

  useEffect(() => {
    if (!state || eliteQbCutoff === null) return;
    getRecommendation(recMode, eliteQbCutoff)
      .then(setRec)
      .catch((e) => setErrorMsg(String(e)));
    // Keyed on event_seq, NOT current_pick: a void or a correction changes who is available
    // (and every roster) without moving the clock, and the old key left a stale
    // recommendation on screen after exactly those actions (Codex 2026-08-18).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state?.event_seq, recMode, eliteQbCutoff]);

  // Flatten the tier board once per state update into a player_id -> flag lookup, so the
  // recommendation candidates can show the same disagreement/injury badges as the board
  // without recommend.py (owned by a concurrent stream) needing to carry them itself.
  const playerFlags = useMemo<Record<string, PlayerFlagInfo>>(() => {
    if (!state) return {};
    const out: Record<string, PlayerFlagInfo> = {};
    for (const rows of Object.values(state.tier_board)) {
      for (const r of rows) {
        out[r.player_id] = { disagreement_high: r.disagreement_high, injury_status: r.injury_status };
      }
    }
    return out;
  }, [state]);

  // ---------------------------------------------------------------- A2: click-anywhere draft/undraft

  // player_id -> pick_no over currently-filled picks, built from `all_picks` (A3's payload) so
  // any surface naming a player (tier board, search chips, recommendation candidates) can offer
  // "undraft" without every one of those payload shapes needing to carry pick_no itself.
  const pickNoByPlayerId = useMemo(() => buildPickNoIndex(state?.all_picks), [state]);
  const mostRecentPick = useMemo(() => mostRecentPickNo(state?.all_picks), [state]);
  // The full team list, by slot, for the draft-team and reassign-team pickers. `opponents`
  // already includes every slot (including mine, via is_mine) with name-aware labels -- see
  // DraftBoard.opponent_grid() -- so there is nothing else to derive here.
  const teamOptions = useMemo(
    () => (state ? state.opponents.map((o) => ({ team_slot: o.team_slot, team_label: o.team_label })) : []),
    [state],
  );

  const openDraftMenu = useCallback((e: ReactMouseEvent<HTMLElement>, playerId: string, playerName: string) => {
    e.preventDefault();
    e.stopPropagation();
    setErrorMsg(null);
    setActionMenu({ kind: "draft", playerId, playerName, x: e.clientX, y: e.clientY });
  }, []);

  const requestUndraft = useCallback(
    (e: ReactMouseEvent<HTMLElement>, pickNo: number, playerName: string) => {
      e.preventDefault();
      e.stopPropagation();
      setErrorMsg(null);
      // Confirm rule (A2): undrafting the MOST RECENT pick is instant (Ctrl+Z undoes it anyway).
      // Anything else confirms first, because it rewrites history mid-board.
      //
      // Both paths go through undraftPick, NOT voidPick: voiding the newest pick left the clock
      // advanced, so the replacement landed at the next pick number for the next team and the
      // whole board drifted one slot out of alignment (Codex 2026-08-21 finding 2).
      if (mostRecentPick !== null && pickNo === mostRecentPick) {
        undraftPick(pickNo).then(setState).catch((err) => setErrorMsg(String(err)));
        return;
      }
      setActionMenu({ kind: "confirm-void", pickNo, playerName, x: e.clientX, y: e.clientY });
    },
    [mostRecentPick],
  );

  function handleDraftFromMenu(playerId: string, teamSlot: number) {
    setErrorMsg(null);
    draftPick(playerId, { teamSlot })
      .then((s) => {
        setState(s);
        setActionMenu(null);
      })
      .catch((err) => setErrorMsg(String(err)));
  }

  /** Ledger #5: draft straight to the team on the clock, no popover and no confirm.
   *
   * Bound to double-click on a board row. The single-click popover stays for the cases that need
   * it (recording someone else's out-of-order pick, or a pick for a specific team), but the
   * common case -- 150 picks going to whoever is actually on the clock -- is now one gesture.
   * Closes any popover the first click of the double-click opened, so the board is not left with
   * a stale menu floating over it after the pick has already landed.
   */
  function handleDraftToClock(playerId: string, _playerName: string) {
    if (!state) return;
    setErrorMsg(null);
    setActionMenu(null);
    draftPick(playerId, { teamSlot: state.slot_on_clock })
      .then(setState)
      .catch((err) => setErrorMsg(String(err)));
  }

  // Ledger #8: Marc's own stated condition for when an IR stash is worth a late pick --
  // "obviously not when you have real needs". Derived from HIS words, not from a round number:
  // every dedicated starter slot AND the flex are full. Until then the hint stays off the board.
  const startersAllFilled = useMemo(() => {
    const fill = state?.my_starter_fill;
    if (!fill) return false;
    const dedicated = Object.values(fill.starters).every((s) => s.filled >= s.need);
    return dedicated && fill.flex.filled >= fill.flex.need;
  }, [state?.my_starter_fill]);

  function handleConfirmVoidFromMenu(pickNo: number) {
    setErrorMsg(null);
    undraftPick(pickNo)
      .then((s) => {
        setState(s);
        setActionMenu(null);
      })
      .catch((err) => setErrorMsg(String(err)));
  }

  // ---------------------------------------------------------------- search-as-you-type

  useEffect(() => {
    if (mode !== "search" && mode !== "edit-pick-value") {
      setMatches([]);
      return;
    }
    const { text, includeDrafted } = parseCommand(inputValue);
    if (!text) {
      setMatches([]);
      setHighlighted(0);
      return;
    }
    let cancelled = false;
    const handle = setTimeout(() => {
      apiSearch(text, { includeDrafted })
        .then((res) => {
          if (!cancelled) {
            setMatches(res.matches);
            setHighlighted(0);
          }
        })
        .catch(() => {
          if (!cancelled) setMatches([]);
        });
    }, 60);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [inputValue, mode]);

  // ---------------------------------------------------------------- stub position sub-flow
  // "Ctrl+U, then one key for position" -- a single non-Enter keypress, so it's a separate
  // listener scoped to this one mode rather than one of the fixed combos in keys.ts.

  useEffect(() => {
    if (mode !== "stub-position") return;
    function handler(e: KeyboardEvent) {
      if (e.key === "Escape") {
        resetToSearch();
        return;
      }
      const pos = STUB_POSITION_KEYS[e.key.toLowerCase()];
      if (!pos) return;
      e.preventDefault();
      addStub(pendingStubName, pos)
        .then((s) => {
          setState(s);
          resetToSearch();
        })
        .catch((err) => setErrorMsg(String(err)));
    }
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, pendingStubName]);

  function resetToSearch() {
    setMode("search");
    setInputValue("");
    setMatches([]);
    setPendingStubName("");
    setPendingEditPickNo(null);
    setHighlighted(0);
  }

  // ---------------------------------------------------------------- key bindings

  const onDraftHighlighted = useCallback(() => {
    setErrorMsg(null);
    if (mode === "search") {
      const { teamSlot } = parseCommand(inputValue);
      const match = matches[highlighted];
      if (!match) return;
      if (match.drafted) {
        setErrorMsg(`${match.name} is already drafted.`);
        return;
      }
      draftPick(match.player_id, { teamSlot: teamSlot ?? undefined, rawQuery: inputValue })
        .then((s) => {
          setState(s);
          setInputValue("");
          setMatches([]);
        })
        .catch((err) => setErrorMsg(String(err)));
      return;
    }
    if (mode === "stub-name") {
      const name = inputValue.trim();
      if (!name) return;
      setPendingStubName(name);
      setMode("stub-position");
      setInputValue("");
      return;
    }
    if (mode === "edit-pick-number") {
      const n = parseInt(inputValue.trim(), 10);
      if (!Number.isFinite(n) || n < 1) {
        setErrorMsg("Enter a valid pick number.");
        return;
      }
      setPendingEditPickNo(n);
      setMode("edit-pick-value");
      setInputValue("");
      return;
    }
    if (mode === "edit-pick-value") {
      const match = matches[highlighted];
      if (!match || pendingEditPickNo === null) return;
      correctPick(pendingEditPickNo, { playerId: match.player_id })
        .then((s) => {
          setState(s);
          resetToSearch();
        })
        .catch((err) => setErrorMsg(String(err)));
      return;
    }
    if (mode === "jump-clock") {
      const n = parseInt(inputValue.trim(), 10);
      if (!Number.isFinite(n) || n < 1) {
        setErrorMsg("Enter a valid pick number.");
        return;
      }
      setClock(n)
        .then((s) => {
          setState(s);
          resetToSearch();
        })
        .catch((err) => setErrorMsg(String(err)));
      return;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, inputValue, matches, highlighted, pendingEditPickNo]);

  const startJumpClock = useCallback(() => {
    setErrorMsg(null);
    setMode("jump-clock");
    setInputValue("");
  }, []);

  useEffect(() => {
    const unbind = bindKeys({
      onDraftHighlighted,
      onMoveHighlight: (delta) =>
        setHighlighted((h) => Math.min(Math.max(h + delta, 0), Math.max(0, matches.length - 1))),
      onEscape: () => {
        // Menus/modals take priority so Escape closes the topmost thing on screen first,
        // rather than racing a second listener registered inside the menu itself.
        if (actionMenu) {
          setActionMenu(null);
          return;
        }
        if (teamNamesOpen) {
          setTeamNamesOpen(false);
          return;
        }
        if (helpOpen) {
          setHelpOpen(false);
          return;
        }
        if (inputValue) {
          setInputValue("");
          setMatches([]);
          return;
        }
        if (mode !== "search") {
          resetToSearch();
        }
      },
      onCyclePositionFilter: (delta) => {
        // Cycles through ALL as well, so the keyboard reaches every tab the mouse can.
        const idx = BOARD_FILTERS.indexOf(posFilter);
        const next = (idx + delta + BOARD_FILTERS.length) % BOARD_FILTERS.length;
        setPosFilter(BOARD_FILTERS[next]);
      },
      onUndo: () => {
        setErrorMsg(null);
        undo()
          .then(setState)
          .catch((err) => setErrorMsg(String(err)));
      },
      onEditPastPick: () => {
        setErrorMsg(null);
        setMode("edit-pick-number");
        setInputValue("");
      },
      onJumpClock: startJumpClock,
      onAddStub: () => {
        setErrorMsg(null);
        setMode("stub-name");
        setInputValue("");
      },
      onHelpToggle: () => setHelpOpen((v) => !v),
      onRecommendationToggle: () => setRecMode((m) => (m === "clock" ? "mine" : "clock")),
    });
    return unbind;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onDraftHighlighted, helpOpen, inputValue, mode, posFilter, matches.length, actionMenu, teamNamesOpen]);

  if (!state) {
    return <div style={{ padding: 24, color: "#8fa1b3" }}>Loading draft state...</div>;
  }

  const modeLabels: Record<Mode, string> = {
    search: "",
    "stub-name": "ADD STUB — NAME",
    "stub-position": `ADD STUB — POSITION FOR "${pendingStubName}" (Q/R/W/T)`,
    "edit-pick-number": "EDIT PAST PICK — WHICH PICK #",
    "edit-pick-value": `EDIT PICK #${pendingEditPickNo ?? ""} — NEW PLAYER`,
    "jump-clock": "JUMP CLOCK — PICK #",
  };
  const placeholders: Record<Mode, string> = {
    search: "Type a name... @N=assign to team N, ~=include drafted, F1=help",
    "stub-name": "Player name, then Enter",
    "stub-position": "Press Q / R / W / T",
    "edit-pick-number": "Pick number, then Enter",
    "edit-pick-value": "New player name, then Enter",
    "jump-clock": "Pick number, then Enter",
  };

  return (
    <div className="app">
      <Header
        state={state}
        onOpenTeamNames={() => setTeamNamesOpen(true)}
        boardView={boardView}
        onToggleBoardView={() => setBoardView((v) => (v === "tiers" ? "results" : "tiers"))}
        onOpenHelp={() => setHelpOpen(true)}
        onState={setState}
        onError={setErrorMsg}
      />
      {state.board_source === "placeholder" && (
        <div className="placeholder-banner" title={state.value_note}>
          FALLBACK VALUES — the validated board could not be loaded; values are ADP placeholders
          and recommendations are NOT the validated model. Bookkeeping is unaffected.
        </div>
      )}
      <Ticker picks={state.upcoming_picks} />
      <RecommendationPanel
        rec={rec}
        mode={recMode}
        playerFlags={playerFlags}
        eliteQbCutoff={eliteQbCutoff ?? state.elite_qb_rank_cutoff_default}
        onEliteQbCutoffChange={setEliteQbCutoff}
        onOpenDraftMenu={openDraftMenu}
      />
      {boardView === "tiers" ? (
        <TierBoard
          board={state.tier_board}
          filter={posFilter}
          onSelectFilter={setPosFilter}
          pickNoByPlayerId={pickNoByPlayerId}
          onOpenDraftMenu={openDraftMenu}
          onDraftToClock={handleDraftToClock}
          onRequestUndraft={requestUndraft}
          slotOnClockLabel={
            state.upcoming_picks.find((u) => u.is_on_clock)?.team_label ??
            `Team ${state.slot_on_clock}`
          }
          stashHintActive={startersAllFilled}
        />
      ) : (
        <div className="board-panel panel">
          <div className="board-tabs">
            <span className="board-tabs-title">Draft Results</span>
          </div>
          <DraftResultsTab state={state} onState={setState} onError={setErrorMsg} teams={teamOptions} />
        </div>
      )}
      <RosterPanel state={state} onStartJumpClock={startJumpClock} onRequestUndraft={requestUndraft} />
      <CommandBar
        modeLabel={modeLabels[mode]}
        placeholder={placeholders[mode]}
        value={inputValue}
        onChange={setInputValue}
        matches={mode === "stub-position" ? [] : matches}
        highlightedIndex={highlighted}
        inputRef={inputRef}
        error={errorMsg}
        pickNoByPlayerId={pickNoByPlayerId}
        onOpenDraftMenu={openDraftMenu}
        onRequestUndraft={requestUndraft}
      />
      {helpOpen && <HelpOverlay onClose={() => setHelpOpen(false)} />}
      {teamNamesOpen && (
        <TeamNamesPanel
          state={state}
          onState={setState}
          onError={setErrorMsg}
          onClose={() => setTeamNamesOpen(false)}
        />
      )}
      {actionMenu && (
        <PlayerActionPopover
          menu={actionMenu}
          teams={teamOptions}
          defaultTeamSlot={state.slot_on_clock}
          onClose={() => setActionMenu(null)}
          onDraft={handleDraftFromMenu}
          onConfirmVoid={handleConfirmVoidFromMenu}
        />
      )}
    </div>
  );
}
