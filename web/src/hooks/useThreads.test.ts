import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import type { AssistantTurn } from "../types";
import type { Thread } from "./useThreads";

vi.mock("../api", () => ({
  getThreadStore: vi.fn(),
  putThreadStore: vi.fn(),
}));

import { getThreadStore, putThreadStore } from "../api";
import { useThreads } from "./useThreads";

const mockGetThreadStore = vi.mocked(getThreadStore);
const mockPutThreadStore = vi.mocked(putThreadStore);

const EMAIL = "user@example.com";
const CACHE_KEY = `verbatim.threads.v4.${EMAIL}`;

function makeTurn(id: string): AssistantTurn {
  return {
    id, question: "q", answer: "a", plan: null, citations: [], status: "done",
    trace: { steps: [], stepsUsed: 0, finishReason: null, nCitations: 0 },
  };
}

function makeThread(id: string, updatedAt: number, turns: AssistantTurn[]): Thread {
  return { id, title: id, createdAt: updatedAt, updatedAt, userHistory: [], turns };
}

beforeEach(() => {
  localStorage.clear();
  mockGetThreadStore.mockReset();
  mockPutThreadStore.mockReset();
  mockPutThreadStore.mockResolvedValue(undefined);
});

describe("useThreads: merging the server fetch with the local cache", () => {
  it("keeps a local-only thread with turns and does not duplicate a thread the server also knows", async () => {
    const localOnly = makeThread("local-only-1", 1000, [makeTurn("t1")]);
    const sharedLocal = makeThread("shared-1", 500, [makeTurn("t2-local")]);
    localStorage.setItem(CACHE_KEY, JSON.stringify({
      version: 4, activeId: null, threads: [localOnly, sharedLocal],
    }));

    const sharedServer = makeThread("shared-1", 900, [makeTurn("t2-server")]);
    const serverOnly = makeThread("server-1", 800, [makeTurn("t3")]);
    mockGetThreadStore.mockResolvedValue(JSON.stringify({ threads: [sharedServer, serverOnly] }));

    const { result } = renderHook(() => useThreads(EMAIL));

    await waitFor(() => {
      expect(result.current.list.some(t => t.id === "server-1")).toBe(true);
    });

    const byId = (id: string) => result.current.list.filter(t => t.id === id);
    // the local-only thread (unknown to the server) survives the merge
    expect(byId("local-only-1")).toHaveLength(1);
    expect(byId("local-only-1")[0].turns).toHaveLength(1);
    // the thread that exists on both sides appears exactly once
    expect(byId("shared-1")).toHaveLength(1);
    // the server-only thread is picked up too
    expect(byId("server-1")).toHaveLength(1);
  });
});
