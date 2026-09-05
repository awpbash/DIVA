import { useCallback, useEffect, useRef, useState } from "react";
import { AssistantTurn, ChatMessage } from "../types";
import { getThreadStore, putThreadStore } from "../api";

/** One conversation thread. Titles are derived from the first user turn. */
export interface Thread {
  id: string;
  title: string;
  createdAt: number;
  updatedAt: number;
  userHistory: ChatMessage[];
  turns: AssistantTurn[];
}

interface PersistedShape {
  version: 4;
  activeId: string | null;
  threads: Thread[];
}

// Bump to invalidate persisted threads (they cache citation bbox geometry). v4 = the
// server-backed, per-account era: history persists in the app DB (keyed to the login) with
// a per-account localStorage cache for instant paint + offline. The email is stable for the
// hook's lifetime — MainApp remounts on each login — so no mid-life account switching.
const SCHEMA_VERSION = 4 as const;
const cacheKey = (email: string | null) => `verbatim.threads.v${SCHEMA_VERSION}.${email || "anon"}`;
// Pre-v4 caches were not keyed by account, so they can be named and swept. The
// per-account v4 entries under the old prefix are simply orphaned: threads live
// server-side in the app DB, so a lost local cache costs one extra fetch.
const LEGACY_KEYS = ["engineering_agent.threads.v3", "engineering_agent.threads.v2",
                     "engineering_agent.threads.v1"];
const MAX_THREADS = 40;

function emptyState(): PersistedShape {
  return { version: SCHEMA_VERSION, activeId: null, threads: [] };
}

function loadCache(email: string | null): PersistedShape {
  try { for (const k of LEGACY_KEYS) localStorage.removeItem(k); } catch { /* no storage */ }
  try {
    const raw = localStorage.getItem(cacheKey(email));
    if (!raw) return emptyState();
    const parsed = JSON.parse(raw);
    if (parsed.version !== SCHEMA_VERSION || !Array.isArray(parsed.threads)) return emptyState();
    return parsed;
  } catch { return emptyState(); }
}

function saveCache(email: string | null, s: PersistedShape) {
  try { localStorage.setItem(cacheKey(email), JSON.stringify(s)); } catch { /* quota */ }
}

function newThread(): Thread {
  return {
    id: `t-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
    title: "New chat", createdAt: Date.now(), updatedAt: Date.now(),
    userHistory: [], turns: [],
  };
}

export interface UseThreadsResult {
  list: Thread[];
  active: Thread | null;
  setActive: (id: string) => void;
  startNew: () => Thread;
  update: (id: string, patch: Partial<Thread> | ((t: Thread) => Thread)) => void;
  remove: (id: string) => void;
  clearAll: () => void;
}

export function useThreads(email: string | null): UseThreadsResult {
  const [state, setState] = useState<PersistedShape>(() => {
    // Same rule as the server hydrate below: history yes, stale pointer no.
    const cached = loadCache(email);
    return { ...cached, activeId: null, threads: cached.threads.filter(t => t.turns.length > 0) };
  });
  const saveTimer = useRef<number | null>(null);

  // Pull this account's history from the server once on mount (authoritative over the
  // local cache — so the same account's history follows them to another browser).
  useEffect(() => {
    if (!email) return;
    let cancelled = false;
    getThreadStore().then(data => {
      if (cancelled || !data) return;
      try {
        const parsed = JSON.parse(data);
        if (parsed && Array.isArray(parsed.threads)) {
          // Restore the history but NOT the stale active pointer — a fresh
          // login lands on a new chat, so typing never silently appends to
          // whichever thread happened to be open last session.
          const serverThreads = (parsed.threads as Thread[]).filter(t => t.turns.length > 0);
          setState(prev => {
            // Merge rather than replace: the server fetch can take a while
            // (a cold-started instance shows its own "waking up" notice for
            // up to ~30s), and a message the user sent locally in that
            // window would otherwise vanish the moment this response lands,
            // since the thread it created doesn't exist on the server yet.
            const serverIds = new Set(serverThreads.map(t => t.id));
            const localOnly = prev.threads.filter(
              t => t.turns.length > 0 && !serverIds.has(t.id));
            const threads = [...localOnly, ...serverThreads]
              .sort((a, b) => b.updatedAt - a.updatedAt);
            return { version: SCHEMA_VERSION, activeId: null, threads };
          });
        }
      } catch { /* ignore malformed */ }
    });
    return () => { cancelled = true; };
  }, [email]);

  // Always have an active thread to write into.
  useEffect(() => {
    setState(prev => {
      if (prev.activeId && prev.threads.find(t => t.id === prev.activeId)) return prev;
      const fresh = newThread();
      return { version: SCHEMA_VERSION, activeId: fresh.id, threads: [fresh, ...prev.threads] };
    });
  }, [state.activeId, state.threads.length]);

  // Persist on change: cache immediately, server debounced.
  useEffect(() => {
    saveCache(email, state);
    if (!email) return;
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => { putThreadStore(JSON.stringify(state)); }, 700);
  }, [state, email]);

  const setActive = useCallback((id: string) => setState(prev => ({ ...prev, activeId: id })), []);

  const startNew = useCallback((): Thread => {
    const t = newThread();
    setState(prev => {
      const trimmed = prev.threads.filter(x => x.turns.length > 0 || x.id === prev.activeId);
      let next = [t, ...trimmed];
      if (next.length > MAX_THREADS) next = next.slice(0, MAX_THREADS);
      return { version: SCHEMA_VERSION, activeId: t.id, threads: next };
    });
    return t;
  }, []);

  const update = useCallback(
    (id: string, patch: Partial<Thread> | ((t: Thread) => Thread)) => {
      setState(prev => {
        const idx = prev.threads.findIndex(t => t.id === id);
        if (idx < 0) return prev;
        const cur = prev.threads[idx];
        const updated = typeof patch === "function" ? patch(cur) : { ...cur, ...patch };
        updated.updatedAt = Date.now();
        if ((!updated.title || updated.title === "New chat") && updated.userHistory[0]) {
          const t = updated.userHistory[0].content.replace(/\s+/g, " ").trim();
          updated.title = t.slice(0, 48) + (t.length > 48 ? "…" : "");
        }
        const nextThreads = [...prev.threads];
        nextThreads[idx] = updated;
        nextThreads.sort((a, b) => b.updatedAt - a.updatedAt);
        return { ...prev, threads: nextThreads };
      });
    }, []);

  const remove = useCallback((id: string) => {
    setState(prev => {
      const nextThreads = prev.threads.filter(t => t.id !== id);
      const nextActive = prev.activeId === id ? nextThreads[0]?.id ?? null : prev.activeId;
      return { version: SCHEMA_VERSION, activeId: nextActive, threads: nextThreads };
    });
  }, []);

  const clearAll = useCallback(() => {
    try { localStorage.removeItem(cacheKey(email)); } catch { /* no storage */ }
    const fresh = newThread();
    setState({ version: SCHEMA_VERSION, activeId: fresh.id, threads: [fresh] });
  }, [email]);

  const active = state.threads.find(t => t.id === state.activeId) ?? null;
  const list = state.threads.filter(t => t.turns.length > 0 || t.id === state.activeId);

  return { list, active, setActive, startNew, update, remove, clearAll };
}
