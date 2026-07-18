import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MemoryView } from "@/components/memory/MemoryView";
import {
  MemoryRequestError,
  type NanobotClient,
} from "@/lib/nanobot-client";

describe("MemoryView", () => {
  const getMemory = vi.fn();
  const updateMemory = vi.fn();
  const client = { getMemory, updateMemory } as unknown as NanobotClient;

  beforeEach(() => {
    getMemory.mockReset().mockResolvedValue({
      userName: "Alice",
      orgName: "Acme",
      documents: [
        { scopeType: "system", content: "system fact", version: 2, canEdit: false },
        { scopeType: "org", content: "org fact", version: 3, canEdit: false },
        { scopeType: "user", content: "private fact", version: 4, canEdit: true },
      ],
    });
    updateMemory.mockReset().mockResolvedValue({
      document: {
        scopeType: "user",
        content: "updated private fact",
        version: 5,
        canEdit: true,
      },
    });
  });

  it("shows all scopes and only saves an editable personal document", async () => {
    render(<MemoryView client={client} onToggleSidebar={vi.fn()} />);

    const editor = await screen.findByRole("textbox", { name: "Personal editor" });
    expect(editor).toHaveValue("private fact");
    fireEvent.change(editor, { target: { value: "updated private fact" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(updateMemory).toHaveBeenCalledWith({
        scopeType: "user",
        content: "updated private fact",
        expectedVersion: 4,
      });
    });
    expect(await screen.findByText("Saved")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Organization" }));
    expect(screen.getByRole("textbox", { name: "Organization editor" })).toHaveAttribute(
      "readonly",
    );
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
  });

  it("reloads the latest document when a save conflicts", async () => {
    updateMemory.mockRejectedValueOnce(new MemoryRequestError(409, "conflict"));
    render(<MemoryView client={client} onToggleSidebar={vi.fn()} />);

    const editor = await screen.findByRole("textbox", { name: "Personal editor" });
    fireEvent.change(editor, { target: { value: "stale edit" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Memory changed elsewhere and was reloaded."))
      .toBeInTheDocument();
    await waitFor(() => expect(getMemory).toHaveBeenCalledTimes(2));
  });
});
