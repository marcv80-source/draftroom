import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
} from "./api";
import { CommandBar } from "./components/CommandBar";
import { HelpOverlay } from "./components/HelpOverlay";
import { RecommendationPanel, type PlayerFlagInfo } from "./components/RecommendationPanel";
import { RosterPanel } from "./components/RosterPanel";
import { TierBoard } from "./components/TierBoard";
import { Ticker } from "./components/Ticker";
import { bindKeys, parseCommand } from "./keys";
import { POSITIONS, type DraftState, type Position, type Recommendation, type SearchMatch } from "./types";

type Mode = "search" | "stub-name" | "stub-position" | "edit-pick-number" | "edit-pick-value" | "jump-clock";

const STUB_POSITION_KEYS: Record<string, string> = { q: "QB", r: "RB", w: "WR", t: "TE" };

export default function App() {
  const [state, setState] = useState<DraftState | null>(null);
  const [rec, setRec] = useState<Recommendation | null>(null);
  const [recMode, setRecMode] = useState<"clock" | "mine">("clock");
  const [posFilter, setPosFilter] = useState<Position>("QB");
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
        const idx = POSITIONS.indexOf(posFilter);
        const next = (idx + delta + POSITIONS.length) % POSITIONS.length;
        setPosFilter(POSITIONS[next]);
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
  }, [onDraftHighlighted, helpOpen, inputValue, mode, posFilter, matches.length]);

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
      />
      <TierBoard board={state.tier_board} filter={posFilter} onSelectFilter={setPosFilter} />
      <RosterPanel
        state={state}
        onState={setState}
        onError={setErrorMsg}
        onStartJumpClock={startJumpClock}
      />
      <CommandBar
        modeLabel={modeLabels[mode]}
        placeholder={placeholders[mode]}
        value={inputValue}
        onChange={setInputValue}
        matches={mode === "stub-position" ? [] : matches}
        highlightedIndex={highlighted}
        inputRef={inputRef}
        error={errorMsg}
      />
      {helpOpen && <HelpOverlay onClose={() => setHelpOpen(false)} />}
    </div>
  );
}
