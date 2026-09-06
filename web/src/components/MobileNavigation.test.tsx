import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MobileNavigation } from "./MobileNavigation";

describe("MobileNavigation", () => {
  it("keeps the permitted primary destinations reachable and changes tabs", () => {
    const onTab = vi.fn();
    render(
      <MobileNavigation
        tab="chat"
        tabs={["chat", "explore", "review", "knowledge", "admin", "ontology"]}
        onTab={onTab}
      />,
    );

    expect(screen.getByRole("button", { name: "Ask", current: "page" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Explore" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Manage" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Schema" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Explore" }));
    expect(onTab).toHaveBeenCalledWith("explore");
  });
});
