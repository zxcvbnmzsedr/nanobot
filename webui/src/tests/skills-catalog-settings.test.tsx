import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SkillsCatalogSettings } from "@/components/settings/SkillsCatalogSettings";
import {
  fetchInstalledSkills,
  fetchSkillDetail,
  fetchSkillMarket,
  fetchSkillMarketDetail,
  fetchSkillMarketStatus,
} from "@/lib/api";
import { SkillRequestError, type NanobotClient } from "@/lib/nanobot-client";
import type {
  SkillInventoryPayload,
  SkillMarketItem,
  SkillMarketPayload,
  SkillMarketStatusPayload,
  SkillsUpdatedEvent,
} from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", () => ({
  fetchInstalledSkills: vi.fn(),
  fetchSkillDetail: vi.fn(),
  fetchSkillMarket: vi.fn(),
  fetchSkillMarketDetail: vi.fn(),
  fetchSkillMarketStatus: vi.fn(),
}));

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

function marketItem(overrides: Partial<SkillMarketItem> = {}): SkillMarketItem {
  return {
    skillId: "sales-helper",
    displayName: "Sales Helper",
    summary: "Keep sales work consistent.",
    latestVersion: "3.0.0",
    rowVersion: 1,
    canManage: true,
    canUninstall: true,
    ...overrides,
  };
}

describe("SkillsCatalogSettings state consistency", () => {
  const fetchMarketMock = vi.mocked(fetchSkillMarket);
  const fetchInventoryMock = vi.mocked(fetchInstalledSkills);
  const fetchStatusMock = vi.mocked(fetchSkillMarketStatus);
  const fetchDetailMock = vi.mocked(fetchSkillMarketDetail);
  const fetchLocalDetailMock = vi.mocked(fetchSkillDetail);
  const installSkillMock = vi.fn();
  const updateSkillMock = vi.fn();
  const rollbackSkillMock = vi.fn();
  const uninstallSkillMock = vi.fn();
  const setPolicyMock = vi.fn();
  const syncSkillsMock = vi.fn();
  let client: NanobotClient;
  let skillUpdatedHandlers: Set<(event: SkillsUpdatedEvent) => void>;

  beforeEach(() => {
    fetchMarketMock.mockReset();
    fetchInventoryMock.mockReset();
    fetchStatusMock.mockReset();
    fetchDetailMock.mockReset();
    fetchLocalDetailMock.mockReset();
    installSkillMock.mockReset().mockResolvedValue({ status: "applied" });
    updateSkillMock.mockReset().mockResolvedValue({ status: "applied" });
    rollbackSkillMock.mockReset().mockResolvedValue({ status: "applied" });
    uninstallSkillMock.mockReset().mockResolvedValue({ status: "applied" });
    setPolicyMock.mockReset().mockResolvedValue({ status: "applied" });
    syncSkillsMock.mockReset().mockResolvedValue({ status: "applied" });
    skillUpdatedHandlers = new Set();
    client = {
      onSkillsUpdated: (handler: (event: SkillsUpdatedEvent) => void) => {
        skillUpdatedHandlers.add(handler);
        return () => skillUpdatedHandlers.delete(handler);
      },
      installSkill: installSkillMock,
      updateSkill: updateSkillMock,
      rollbackSkill: rollbackSkillMock,
      uninstallSkill: uninstallSkillMock,
      setSkillUpdatePolicy: setPolicyMock,
      syncSkills: syncSkillsMock,
    } as unknown as NanobotClient;
  });

  function renderCatalog() {
    return render(
      <ClientProvider client={client} token="tok">
        <SkillsCatalogSettings skills={[]} />
      </ClientProvider>,
    );
  }

  function emitSkillsUpdated() {
    const event: SkillsUpdatedEvent = { event: "skills_updated", reason: "assignment" };
    for (const handler of skillUpdatedHandlers) handler(event);
  }

  it("keeps the newest catalog, inventory, and status refresh when requests finish out of order", async () => {
    const oldCatalog = deferred<SkillMarketPayload>();
    const newCatalog = deferred<SkillMarketPayload>();
    const oldInventory = deferred<SkillInventoryPayload>();
    const newInventory = deferred<SkillInventoryPayload>();
    const oldStatus = deferred<SkillMarketStatusPayload>();
    const newStatus = deferred<SkillMarketStatusPayload>();
    fetchMarketMock.mockReturnValueOnce(oldCatalog.promise).mockReturnValueOnce(newCatalog.promise);
    fetchInventoryMock
      .mockReturnValueOnce(oldInventory.promise)
      .mockReturnValueOnce(newInventory.promise);
    fetchStatusMock.mockReturnValueOnce(oldStatus.promise).mockReturnValueOnce(newStatus.promise);

    renderCatalog();
    expect(skillUpdatedHandlers.size).toBe(1);
    act(() => emitSkillsUpdated());

    await act(async () => {
      newCatalog.resolve({ skills: [marketItem({ skillId: "new-skill", displayName: "New Skill" })] });
      newInventory.resolve({ skills: [] });
      newStatus.resolve({ enabled: true, available: true, lastSyncedAt: "new-sync" });
      await newCatalog.promise;
    });
    expect(await screen.findByRole("button", { name: "Open details for New Skill" })).toBeInTheDocument();
    expect(screen.getByText(/new-sync/)).toBeInTheDocument();

    await act(async () => {
      oldCatalog.resolve({ skills: [marketItem({ skillId: "old-skill", displayName: "Old Skill" })] });
      oldInventory.resolve({
        skills: [marketItem({ skillId: "old-installed", displayName: "Old Installed", installed: true })],
      });
      oldStatus.resolve({
        enabled: true,
        available: false,
        stale: true,
        lastErrorCode: "OLD_REFRESH",
      });
      await oldCatalog.promise;
    });

    expect(screen.queryByText("Old Skill")).not.toBeInTheDocument();
    expect(screen.queryByText("OLD_REFRESH")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open details for New Skill" })).toBeInTheDocument();
  });

  it("ignores stale detail responses and uses the row version received while the sheet is open", async () => {
    let authority = 1;
    const oldDetail = deferred<SkillMarketItem>();
    const newDetail = deferred<SkillMarketItem>();
    fetchMarketMock.mockImplementation(async () => ({
      skills: [marketItem({ rowVersion: authority, installed: true, installedVersion: "2.0.0" })],
    }));
    fetchInventoryMock.mockImplementation(async () => ({
      skills: [marketItem({
        rowVersion: authority,
        installed: true,
        installedVersion: "2.0.0",
        updatePolicy: authority === 1 ? "notify" : "pinned",
      })],
    }));
    fetchStatusMock.mockResolvedValue({ enabled: true, available: true });
    fetchDetailMock.mockReturnValueOnce(oldDetail.promise).mockReturnValueOnce(newDetail.promise);

    renderCatalog();
    fireEvent.click(await screen.findByRole("button", { name: "Open details for Sales Helper" }));
    await waitFor(() => expect(fetchDetailMock).toHaveBeenCalledTimes(1));

    authority = 2;
    act(() => emitSkillsUpdated());
    await waitFor(() => expect(fetchDetailMock).toHaveBeenCalledTimes(2));
    await act(async () => {
      newDetail.resolve(marketItem({
        rowVersion: 2,
        installed: true,
        installedVersion: "2.0.0",
        updatePolicy: "pinned",
      }));
      await newDetail.promise;
    });
    await act(async () => {
      oldDetail.resolve(marketItem({
        rowVersion: 1,
        installed: true,
        installedVersion: "2.0.0",
        updatePolicy: "notify",
      }));
      await oldDetail.promise;
    });

    const policy = await screen.findByRole("combobox", { name: "Update policy" });
    expect(policy).toHaveValue("pinned");
    fireEvent.change(policy, { target: { value: "manual" } });
    await waitFor(() => expect(setPolicyMock).toHaveBeenCalledWith(
      "sales-helper",
      "manual",
      { expectedRowVersion: 2 },
    ));
  });

  it("treats failed result payloads as failures and reloads a usable row version", async () => {
    let authority = 3;
    fetchMarketMock.mockImplementation(async () => ({
      skills: [marketItem({ rowVersion: authority, installed: false })],
    }));
    fetchInventoryMock.mockResolvedValue({ skills: [] });
    fetchStatusMock.mockResolvedValue({ enabled: true, available: true });
    fetchDetailMock.mockImplementation(async () => marketItem({
      rowVersion: authority,
      installed: false,
    }));
    installSkillMock
      .mockImplementationOnce(async () => {
        authority = 9;
        return {
          status: "failed",
          errorCode: "ROW_VERSION_CONFLICT",
          message: "private upstream exception",
        };
      })
      .mockResolvedValueOnce({ status: "applied" });

    renderCatalog();
    fireEvent.click(await screen.findByRole("button", { name: "Open details for Sales Helper" }));
    fireEvent.click(await screen.findByRole("button", { name: "Install" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("ROW_VERSION_CONFLICT");
    expect(alert).not.toHaveTextContent("private upstream exception");
    await waitFor(() => expect(screen.getByRole("button", { name: "Install" })).not.toBeDisabled());
    fireEvent.click(screen.getByRole("button", { name: "Install" }));
    await waitFor(() => expect(installSkillMock).toHaveBeenLastCalledWith(
      "sales-helper",
      { updatePolicy: "manual", expectedRowVersion: 9 },
    ));
  });

  it("rolls back an optimistic policy after a CAS error and refreshes before retry", async () => {
    let authority = 7;
    fetchMarketMock.mockImplementation(async () => ({
      skills: [marketItem({ rowVersion: authority, installed: true, installedVersion: "2.0.0" })],
    }));
    fetchInventoryMock.mockImplementation(async () => ({
      skills: [marketItem({
        rowVersion: authority,
        installed: true,
        installedVersion: "2.0.0",
        updatePolicy: authority === 7 ? "notify" : "manual",
      })],
    }));
    fetchStatusMock.mockResolvedValue({ enabled: true, available: true });
    fetchDetailMock.mockImplementation(async () => marketItem({
      rowVersion: authority,
      installed: true,
      installedVersion: "2.0.0",
      updatePolicy: authority === 7 ? "notify" : "manual",
    }));
    setPolicyMock
      .mockImplementationOnce(async () => {
        authority = 8;
        throw new SkillRequestError(409, "ROW_VERSION_CONFLICT", false, "private conflict");
      })
      .mockResolvedValueOnce({ status: "applied" });

    renderCatalog();
    fireEvent.click(await screen.findByRole("button", { name: "Open details for Sales Helper" }));
    const policy = await screen.findByRole("combobox", { name: "Update policy" });
    await waitFor(() => expect(policy).toHaveValue("notify"));
    fireEvent.change(policy, { target: { value: "pinned" } });

    expect(policy).toHaveValue("pinned");
    expect(await screen.findByRole("alert")).toHaveTextContent("ROW_VERSION_CONFLICT");
    await waitFor(() => expect(policy).toHaveValue("manual"));
    fireEvent.change(policy, { target: { value: "auto_stable" } });
    await waitFor(() => expect(setPolicyMock).toHaveBeenLastCalledWith(
      "sales-helper",
      "auto_stable",
      { expectedRowVersion: 8 },
    ));
  });

  it("recomputes the rollback target from the refreshed installed version after an operation", async () => {
    let installedVersion = "3.0.0";
    let rowVersion = 7;
    const detailForAuthority = () => marketItem({
      rowVersion,
      installed: true,
      installedVersion,
      updatePolicy: "manual",
      versions: [
        { version: "4.0.0", status: "published" },
        { version: "2.0.0", status: "published" },
        { version: "1.0.0", status: "published" },
      ],
    });
    fetchMarketMock.mockImplementation(async () => ({ skills: [detailForAuthority()] }));
    fetchInventoryMock.mockImplementation(async () => ({ skills: [detailForAuthority()] }));
    fetchStatusMock.mockResolvedValue({ enabled: true, available: true });
    fetchDetailMock.mockImplementation(async () => detailForAuthority());
    rollbackSkillMock.mockImplementationOnce(async () => {
      installedVersion = "2.0.0";
      rowVersion = 8;
      return { status: "applied" };
    });

    renderCatalog();
    fireEvent.click(await screen.findByRole("button", { name: "Open details for Sales Helper" }));
    const rollback = await screen.findByRole("combobox", { name: "Rollback version" });
    await waitFor(() => expect(rollback).toHaveValue("2.0.0"));
    expect(screen.queryByRole("option", { name: "4.0.0" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Rollback" }));

    await waitFor(() => expect(rollbackSkillMock).toHaveBeenCalledWith(
      "sales-helper",
      "2.0.0",
      { expectedRowVersion: 7 },
    ));
    await waitFor(() => expect(rollback).toHaveValue("1.0.0"));
    expect(screen.queryByRole("option", { name: "4.0.0" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "2.0.0" })).not.toBeInTheDocument();
  });
});
