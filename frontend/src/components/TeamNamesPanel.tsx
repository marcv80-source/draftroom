import { useEffect, useState } from "react";
import { setTeamName, setTeamNames } from "../api";
import type { DraftState } from "../types";

// The candidate league names come from the SERVER (`state.team_name_candidates`, read from
// data/league_manual.yaml), never from a copy in here. A hardcoded list in the frontend meant
// editing the yaml -- the documented source of truth -- changed nothing on screen, and the two
// could drift silently (Codex 2026-08-21 finding 10).
//
// "Fill known names" seeds the blank inputs at once so the table-side work on draft night is
// pure reordering, not retyping ten names under time pressure. Slot assignment is unknown until
// the draw, so the order they land in carries no meaning.

export function TeamNamesPanel({
  state,
  onState,
  onError,
  onClose,
}: {
  state: DraftState;
  onState: (s: DraftState) => void;
  onError: (msg: string | null) => void;
  onClose: () => void;
}) {
  const [names, setNames] = useState<string[]>([]); // index 0 -> slot 1
  const candidates = state.team_name_candidates ?? [];
  // Two slots sharing a name makes every by-name reading of the board ambiguous, so it is shown
  // rather than silently allowed -- the server accepts it (a real league could genuinely have
  // near-identical names) but Marc should see it.
  const duplicates = (() => {
    const seen = new Map<string, number>();
    for (const n of names) {
      const key = n.trim();
      if (!key) continue;
      seen.set(key, (seen.get(key) ?? 0) + 1);
    }
    return [...seen.entries()].filter(([, c]) => c > 1).map(([n]) => n);
  })();

  useEffect(() => {
    const arr: string[] = [];
    for (let slot = 1; slot <= state.teams; slot++) {
      arr.push(state.team_names[String(slot)] ?? "");
    }
    setNames(arr);
    // Only re-seed from the server when the team count or the server's own map changes -- not
    // on every keystroke, which lives in local state until blur/swap commits it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.teams, state.team_names]);

  function commitOne(idx: number) {
    const slot = idx + 1;
    const current = state.team_names[String(slot)] ?? "";
    const next = names[idx] ?? "";
    if (next === current) return;
    onError(null);
    setTeamName(slot, next)
      .then(onState)
      .catch((err) => onError(String(err)));
  }

  function swap(idx: number, delta: -1 | 1) {
    const j = idx + delta;
    if (j < 0 || j >= names.length) return;
    const next = names.slice();
    [next[idx], next[j]] = [next[j], next[idx]];
    setNames(next);
    const bulk: Record<string, string> = {};
    next.forEach((n, i) => {
      bulk[String(i + 1)] = n;
    });
    onError(null);
    setTeamNames(bulk)
      .then(onState)
      .catch((err) => onError(String(err)));
  }

  function fillKnownNames() {
    // Never clobbers a slot that already has a name -- only fills blanks, in order -- AND never
    // hands out a name that is already sitting in another slot. Filling from the top of the list
    // regardless put "Country Club Boys" into slot 2 when it was already in slot 1, i.e. the
    // one button whose whole job is to save typing created a duplicate to hunt down (Codex
    // 2026-08-21 finding 10).
    const next = names.slice();
    const taken = new Set(next.map((n) => n.trim()).filter(Boolean));
    const remaining = candidates.filter((n) => !taken.has(n.trim()));
    let i = 0;
    for (let idx = 0; idx < next.length && i < remaining.length; idx++) {
      if (!next[idx].trim()) {
        next[idx] = remaining[i];
        i++;
      }
    }
    setNames(next);
    const bulk: Record<string, string> = {};
    next.forEach((n, idx) => {
      bulk[String(idx + 1)] = n;
    });
    onError(null);
    setTeamNames(bulk)
      .then(onState)
      .catch((err) => onError(String(err)));
  }

  return (
    <div className="overlay-backdrop" onClick={onClose}>
      <div className="help-card team-names-card" onClick={(e) => e.stopPropagation()}>
        <h2>Team Names</h2>
        <p className="footer-note team-names-hint">
          Slot-to-name is unknown until the draw. Type or fill in the ten names now, then use the
          arrows to reorder as slots get assigned at the table -- every edit saves immediately.
        </p>
        <button
          className="control-btn"
          onClick={fillKnownNames}
          disabled={candidates.length === 0}
          title={
            candidates.length === 0
              ? "This server build sends no team_name_candidates (data/league_manual.yaml team_names:)"
              : `Fill blanks from the ${candidates.length} names in data/league_manual.yaml`
          }
        >
          Fill known names into blanks
        </button>
        {duplicates.length > 0 && (
          <p className="footer-note">
            Two slots share a name: {duplicates.join(", ")}. Allowed, but every by-name reading of
            the board is ambiguous until it's resolved.
          </p>
        )}
        <div className="team-names-list">
          {names.map((n, idx) => {
            const slot = idx + 1;
            return (
              <div className="team-name-row" key={slot}>
                <span className="team-name-slot">{slot}</span>
                <input
                  className="team-name-input"
                  value={n}
                  placeholder={`Team ${slot}`}
                  maxLength={40}
                  onChange={(e) => {
                    const next = names.slice();
                    next[idx] = e.target.value;
                    setNames(next);
                  }}
                  onBlur={() => commitOne(idx)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                  }}
                />
                {slot === state.my_slot && (
                  <span className="you-badge" title="Your draft slot">
                    YOU
                  </span>
                )}
                <div className="team-name-reorder">
                  <button
                    className="reorder-btn"
                    disabled={idx === 0}
                    onClick={() => swap(idx, -1)}
                    title="Move up"
                    aria-label="Move up"
                  >
                    &#9650;
                  </button>
                  <button
                    className="reorder-btn"
                    disabled={idx === names.length - 1}
                    onClick={() => swap(idx, 1)}
                    title="Move down"
                    aria-label="Move down"
                  >
                    &#9660;
                  </button>
                </div>
              </div>
            );
          })}
        </div>
        <button className="control-btn" onClick={onClose} style={{ marginTop: 12 }}>
          Done
        </button>
      </div>
    </div>
  );
}
