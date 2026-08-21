import { useEffect, useRef, useState } from "react";
import { getSources, setSource } from "../api";
import type { DraftState, SourceInfo } from "../types";

/** Plan A5/B2: header control for the multi-source projection toggle. The active source is
 * always rendered on screen -- never implicit -- including in the disabled/unavailable state,
 * because "the toggle is silently missing" is itself a fact Marc needs to see, not hide. */
export function SourceToggle({
  state,
  onState,
  onError,
}: {
  // The served state. `state.active_source` is AUTHORITATIVE for the label: this component used
  // to keep its own copy and update it only when it made the change itself, so a source switched
  // anywhere else (another window, a mid-draft resume) left this header naming the old board
  // while the rows below it were the new one (Codex 2026-08-21 finding 6).
  state: DraftState | null;
  onState: (s: DraftState) => void;
  onError: (msg: string | null) => void;
}) {
  const [sources, setSources] = useState<SourceInfo[] | null>(null);
  const [fetchedActive, setFetchedActive] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    getSources()
      .then((r) => {
        setSources(r.sources);
        setFetchedActive(r.active);
        setUnavailable(false);
      })
      .catch(() => {
        // Covers the documented 503 (backend not ready) and any other failure alike: the
        // control degrades to a visible "unavailable" state rather than throwing, and every
        // other panel (board, search, roster) keeps working off whatever source the server
        // already loaded at startup.
        setUnavailable(true);
        setSources(null);
        setFetchedActive(null);
      });
  }, []);

  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    window.addEventListener("mousedown", onDown, true);
    window.addEventListener("keydown", onKey, true);
    return () => {
      window.removeEventListener("mousedown", onDown, true);
      window.removeEventListener("keydown", onKey, true);
    };
  }, [open]);

  function choose(key: string) {
    setOpen(false);
    onError(null);
    // No local `active` write here. The response is the new state, which carries
    // `active_source`; trusting the request's own argument instead is exactly how the label
    // could claim a source the server had refused (it 503s on an unavailable one).
    setSource(key)
      .then(onState)
      .catch((err) => onError(String(err)));
  }

  // Server state first, the /api/sources snapshot only as a startup fallback.
  const active = state?.active_source ?? fetchedActive;
  const activeLabel = sources?.find((s) => s.key === active)?.label ?? active;

  return (
    <div className="source-toggle" ref={ref}>
      <button
        className="source-toggle-btn"
        onClick={() => setOpen((v) => !v)}
        disabled={unavailable}
        title={
          unavailable
            ? "Source endpoint not available on this server build -- board is on its startup default"
            : "Choose the projection source"
        }
      >
        <span className="source-toggle-label">SOURCE</span>
        <span className="source-toggle-value">{unavailable ? "unavailable" : activeLabel ?? "..."}</span>
      </button>
      {open && sources && (
        <div className="source-toggle-menu">
          {sources.map((s) => (
            <button
              key={s.key}
              className={`source-toggle-item ${s.key === active ? "active" : ""}`}
              // A source whose board built but valued nobody serves ADP placeholders. The server
              // now refuses to make it active (503); disabling it here means Marc finds that out
              // by the choice being greyed with the reason in its tooltip, rather than by an
              // error toast after clicking (Codex 2026-08-21 finding 5).
              disabled={s.available === false}
              onClick={() => choose(s.key)}
              title={s.note}
            >
              <span>{s.label}</span>
              <span className="command-hint">
                {s.available === false ? "unavailable" : `${s.player_count} players`}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
