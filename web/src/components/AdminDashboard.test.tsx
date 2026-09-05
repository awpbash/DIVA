import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { Account } from "../api";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, signOutEverywhere: vi.fn() };
});

import { signOutEverywhere } from "../api";
import { AccountManager } from "./AdminDashboard";

const mockSignOut = vi.mocked(signOutEverywhere);

const ACCOUNTS: Account[] = [
  { email: "person@example.com", name: "Person One", title: "Engineer", role: "default", zone: "z" },
];

beforeEach(() => {
  mockSignOut.mockReset();
});

describe("AccountManager: sign out everywhere", () => {
  it("calls signOutEverywhere with the account's email and shows the resulting note", async () => {
    mockSignOut.mockResolvedValue({ cleared: 2 });
    render(<AccountManager accounts={ACCOUNTS} onChanged={() => {}} />);

    // open the inline editor for the row, then trigger the sign-out action
    fireEvent.click(screen.getByText("Person One"));
    fireEvent.click(screen.getByText("Sign out everywhere"));

    await waitFor(() => expect(mockSignOut).toHaveBeenCalledWith("person@example.com"));
    expect(await screen.findByText(/Signed out everywhere \(2 sessions cleared\)/))
      .toBeInTheDocument();
  });
});
