import type { DraftState, Recommendation, SearchResponse } from "./types";

// Same-origin only, by design: the server binds 127.0.0.1 and this bundle is served from it.
// No absolute URL, no CDN, nothing that could reach past localhost.

async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return (await res.json()) as T;
}

async function postJSON<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error((detail as { detail?: string }).detail ?? `${url} -> ${res.status}`);
  }
  return (await res.json()) as T;
}

export function getState(): Promise<DraftState> {
  return getJSON<DraftState>("/api/state");
}

export function search(query: string, opts: { includeDrafted?: boolean; limit?: number } = {}): Promise<SearchResponse> {
  const params = new URLSearchParams({ q: query });
  if (opts.limit) params.set("limit", String(opts.limit));
  if (opts.includeDrafted) params.set("include_drafted", "true");
  return getJSON<SearchResponse>(`/api/search?${params.toString()}`);
}

export function draftPick(playerId: string, opts: { pickNo?: number; teamSlot?: number; rawQuery?: string } = {}): Promise<DraftState> {
  return postJSON<DraftState>("/api/pick", {
    player_id: playerId,
    pick_no: opts.pickNo ?? null,
    team_slot: opts.teamSlot ?? null,
    raw_query: opts.rawQuery ?? "",
  });
}

export function addStub(name: string, pos: string, opts: { pickNo?: number; teamSlot?: number } = {}): Promise<DraftState> {
  return postJSON<DraftState>("/api/stub", {
    name,
    pos,
    pick_no: opts.pickNo ?? null,
    team_slot: opts.teamSlot ?? null,
  });
}

export function undo(): Promise<DraftState> {
  return postJSON<DraftState>("/api/undo", {});
}

export function correctPick(pickNo: number, fix: { playerId?: string; stubName?: string; stubPos?: string }): Promise<DraftState> {
  return postJSON<DraftState>("/api/correct", {
    pick_no: pickNo,
    player_id: fix.playerId ?? null,
    stub_name: fix.stubName ?? null,
    stub_pos: fix.stubPos ?? null,
  });
}

export function voidPick(pickNo: number): Promise<DraftState> {
  return postJSON<DraftState>("/api/void", { pick_no: pickNo });
}

export function setClock(pickNo: number): Promise<DraftState> {
  return postJSON<DraftState>("/api/clock", { pick_no: pickNo });
}

export function getRecommendation(target: "clock" | "mine" = "clock"): Promise<Recommendation> {
  return getJSON<Recommendation>(`/api/recommendation?target=${target}`);
}

export function openStateSocket(onMessage: (state: DraftState) => void): WebSocket {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${window.location.host}/ws`);
  ws.onmessage = (evt) => {
    try {
      onMessage(JSON.parse(evt.data) as DraftState);
    } catch {
      // A malformed push shouldn't take the socket down; the next /api/state poll (or the
      // next successful push) will resync.
    }
  };
  return ws;
}
