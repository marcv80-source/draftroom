// Every keybinding draftroom implements, in one place, per the project spec: "implement all,
// in one src/keys.ts". This module only recognizes the fixed combos below and calls back into
// App -- it knows nothing about search results, roster state, or what "highlighted" means.
//
// The command bar's text input stays focused at all times (Marc never touches the mouse), so
// these listen at the window/capture phase rather than on a specific element: a plain <input>
// does not swallow Enter/Escape/arrows/F-keys, and capture phase lets us preventDefault()
// browser defaults (Ctrl+Z undo-in-field, Ctrl+G's browser meaning, etc.) before anything else
// sees them. Some OS/browser-level shortcuts (Ctrl+W, Ctrl+N, Ctrl+T) cannot be intercepted by
// any web page; running the app in "app mode" (no tabs/chrome) sidesteps most of that.
export interface KeyActions {
  /** Enter -- draft the currently highlighted match to whoever is on the clock. */
  onDraftHighlighted: () => void;
  /** Up/Down -- move the highlighted match in the command bar's result list. */
  onMoveHighlight: (delta: 1 | -1) => void;
  /** Esc -- clear the command bar if it has text, else close any open modal/overlay. */
  onEscape: () => void;
  /** Tab / Shift+Tab -- cycle the tier board's position filter. */
  onCyclePositionFilter: (delta: 1 | -1) => void;
  /** Ctrl+Z -- undo the most recent pick-ish event. */
  onUndo: () => void;
  /** Ctrl+E -- open the "edit a past pick" flow. */
  onEditPastPick: () => void;
  /** Ctrl+G -- open the "jump clock to pick number" flow. */
  onJumpClock: () => void;
  /** Ctrl+U -- start "add unknown player as stub" (name, then one key for position). */
  onAddStub: () => void;
  /** F1 -- toggle the key-help overlay. */
  onHelpToggle: () => void;
  /** F2 -- toggle the recommendation panel between "my next pick" and "team on the clock". */
  onRecommendationToggle: () => void;
}

export function bindKeys(actions: KeyActions): () => void {
  function handler(e: KeyboardEvent) {
    // Ctrl-chord bindings first -- these must win over the browser's own meaning.
    if (e.ctrlKey && !e.altKey && !e.metaKey) {
      const key = e.key.toLowerCase();
      if (key === "z") {
        e.preventDefault();
        actions.onUndo();
        return;
      }
      if (key === "e") {
        e.preventDefault();
        actions.onEditPastPick();
        return;
      }
      if (key === "g") {
        e.preventDefault();
        actions.onJumpClock();
        return;
      }
      if (key === "u") {
        e.preventDefault();
        actions.onAddStub();
        return;
      }
    }

    switch (e.key) {
      case "Enter":
        e.preventDefault();
        actions.onDraftHighlighted();
        return;
      case "ArrowUp":
        e.preventDefault();
        actions.onMoveHighlight(-1);
        return;
      case "ArrowDown":
        e.preventDefault();
        actions.onMoveHighlight(1);
        return;
      case "Escape":
        e.preventDefault();
        actions.onEscape();
        return;
      case "Tab":
        e.preventDefault();
        actions.onCyclePositionFilter(e.shiftKey ? -1 : 1);
        return;
      case "F1":
        e.preventDefault();
        actions.onHelpToggle();
        return;
      case "F2":
        e.preventDefault();
        actions.onRecommendationToggle();
        return;
      default:
        return;
    }
  }

  window.addEventListener("keydown", handler, true);
  return () => window.removeEventListener("keydown", handler, true);
}

/** Parse the `@N` out-of-order suffix and the `~` include-drafted prefix out of a raw query. */
export function parseCommand(raw: string): { text: string; teamSlot: number | null; includeDrafted: boolean } {
  let text = raw.trim();
  let includeDrafted = false;
  if (text.startsWith("~")) {
    includeDrafted = true;
    text = text.slice(1);
  }
  let teamSlot: number | null = null;
  const m = text.match(/@(\d+)\s*$/);
  if (m) {
    teamSlot = parseInt(m[1], 10);
    text = text.slice(0, m.index).trim();
  }
  return { text, teamSlot, includeDrafted };
}
