import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { TFunction } from "i18next";
import {
  ArrowUpCircle,
  Brain,
  Check,
  CircleAlert,
  Download,
  KeyRound,
  Loader2,
  LockKeyhole,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Terminal,
  Trash2,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { builtinSkillPresentation } from "@/components/settings/builtinSkillMetadata";
import { publishedRollbackVersions } from "@/components/settings/skillVersions";
import {
  fetchInstalledSkills,
  fetchSkillDetail,
  fetchSkillMarket,
  fetchSkillMarketDetail,
  fetchSkillMarketStatus,
} from "@/lib/api";
import { SkillRequestError } from "@/lib/nanobot-client";
import type {
  SkillDetail,
  SkillInventoryPayload,
  SkillMarketItem,
  SkillMarketStatusPayload,
  SkillSummary,
  SkillUpdatePolicy,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

type CatalogView = "discover" | "installed" | "updates";
type SelectedSkill =
  | { kind: "local"; skill: SkillSummary }
  | { kind: "market"; skill: SkillMarketItem };

const UPDATE_POLICIES: SkillUpdatePolicy[] = ["manual", "notify", "auto_stable", "pinned"];

export function SkillsCatalogSettings({ skills }: { skills: SkillSummary[] }) {
  const { client, token } = useClient();
  const { t } = useTranslation();
  const [view, setView] = useState<CatalogView>("discover");
  const [catalog, setCatalog] = useState<SkillMarketItem[]>([]);
  const [inventory, setInventory] = useState<SkillInventoryPayload>({ skills: [] });
  const [marketStatus, setMarketStatus] = useState<SkillMarketStatusPayload | null>(null);
  const [marketLoading, setMarketLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [selected, setSelected] = useState<SelectedSkill | null>(null);
  const catalogRef = useRef<SkillMarketItem[]>([]);
  const inventoryRef = useRef<SkillInventoryPayload>({ skills: [] });
  const refreshGenerationRef = useRef(0);

  const refresh = useCallback(async () => {
    const generation = ++refreshGenerationRef.current;
    const [catalogResult, inventoryResult, statusResult] = await Promise.allSettled([
      fetchSkillMarket(token),
      fetchInstalledSkills(token),
      fetchSkillMarketStatus(token),
    ]);
    if (generation !== refreshGenerationRef.current) return;

    const nextCatalog = catalogResult.status === "fulfilled"
      ? catalogResult.value.skills
      : catalogRef.current;
    const nextInventory = inventoryResult.status === "fulfilled"
      ? inventoryResult.value
      : inventoryRef.current;
    if (catalogResult.status === "fulfilled") {
      catalogRef.current = nextCatalog;
      setCatalog(nextCatalog);
    }
    if (inventoryResult.status === "fulfilled") {
      inventoryRef.current = nextInventory;
      setInventory(nextInventory);
    }
    if (statusResult.status === "fulfilled") setMarketStatus(statusResult.value);
    if (statusResult.status === "rejected" && catalogResult.status === "rejected") {
      setMarketStatus(null);
    }
    const refreshedSkills = mergeCatalogInventory(nextCatalog, nextInventory.skills);
    setSelected((current) => {
      if (current?.kind !== "market") return current;
      const refreshed = refreshedSkills.find((item) => item.skillId === current.skill.skillId);
      return refreshed
        ? { kind: "market", skill: { ...current.skill, ...refreshed } }
        : current;
    });
    setMarketLoading(false);
  }, [token]);

  useEffect(() => {
    let active = true;
    const load = () => {
      if (active) void refresh();
    };
    load();
    const unsubscribe = client.onSkillsUpdated(load);
    return () => {
      active = false;
      refreshGenerationRef.current += 1;
      unsubscribe();
    };
  }, [client, refresh]);

  const marketSkills = useMemo(
    () => mergeCatalogInventory(catalog, inventory.skills),
    [catalog, inventory.skills],
  );
  const visibleMarketSkills = view === "discover"
    ? marketSkills
    : view === "installed"
      ? marketSkills.filter((skill) => skill.installed)
      : marketSkills.filter((skill) => skill.installed && skill.updateAvailable);
  const showLocalSkills = catalog.length === 0 && inventory.skills.length === 0;
  const count = showLocalSkills ? skills.length : visibleMarketSkills.length;

  const handleSync = async () => {
    setSyncing(true);
    setSyncError(null);
    try {
      const result = await client.syncSkills();
      const failed = failedOperationMessage(result, t);
      if (failed) setSyncError(failed);
    } catch (reason) {
      setSyncError(operationErrorMessage(reason, t));
    } finally {
      await refresh();
      setSyncing(false);
    }
  };

  return (
    <div className="space-y-5">
      <section className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="max-w-[680px] text-[13px] leading-5 text-muted-foreground">
            {t("settings.skills.marketDescription", {
              defaultValue: "Discover and manage verified instruction skills for this organization.",
            })}
          </p>
          {marketStatus?.lastSyncedAt ? (
            <p className="mt-1 text-[11px] text-muted-foreground/75">
              {t("settings.skills.lastSynced", {
                time: marketStatus.lastSyncedAt,
                defaultValue: "Last synced {{time}}",
              })}
            </p>
          ) : null}
        </div>
        <Button
          type="button"
          size="icon"
          variant="ghost"
          title={t("settings.skills.syncNow", { defaultValue: "Sync skills" })}
          aria-label={t("settings.skills.syncNow", { defaultValue: "Sync skills" })}
          disabled={syncing || marketStatus?.enabled === false}
          onClick={() => void handleSync()}
        >
          <RefreshCw className={cn("h-4 w-4", syncing && "animate-spin")} aria-hidden />
        </Button>
      </section>

      {syncError || marketStatus?.available === false || marketStatus?.stale ? (
        <div role="status" className="flex items-start gap-2 rounded-md bg-amber-500/10 px-3 py-2 text-[12px] text-amber-800 dark:text-amber-200">
          <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          <span className="min-w-0 break-words">
            {syncError
              || marketStatus?.lastErrorCode
              || t("settings.skills.statusUnavailable", { defaultValue: "Unavailable" })}
          </span>
        </div>
      ) : null}

      <div
        role="tablist"
        aria-label={t("settings.skills.views", { defaultValue: "Skill marketplace views" })}
        className="inline-grid h-9 grid-cols-3 rounded-md bg-muted p-1"
      >
        {(["discover", "installed", "updates"] as CatalogView[]).map((item) => (
          <button
            key={item}
            type="button"
            role="tab"
            aria-selected={view === item}
            onClick={() => setView(item)}
            className={cn(
              "min-w-[6.5rem] rounded px-3 text-[12px] font-medium transition-colors",
              view === item
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {viewLabel(item, t)}
          </button>
        ))}
      </div>

      <section>
        <div className="flex items-center justify-between border-b border-border/45 px-1 pb-3">
          <h2 className="text-[13px] font-semibold text-foreground/85">{viewLabel(view, t)}</h2>
          <span className="rounded-full bg-muted px-2.5 py-1 text-[12px] font-medium text-muted-foreground">
            {count}
          </span>
        </div>

        {marketLoading && count === 0 ? (
          <div className="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            {t("settings.skills.loadingMarket", { defaultValue: "Loading skill marketplace..." })}
          </div>
        ) : showLocalSkills && skills.length ? (
          <div className="grid gap-x-10 gap-y-1 py-3 md:grid-cols-2">
            {skills.map((skill) => (
              <LocalSkillRow
                key={`${skill.source}:${skill.name}`}
                skill={skill}
                onSelect={(next) => setSelected({ kind: "local", skill: next })}
              />
            ))}
          </div>
        ) : visibleMarketSkills.length ? (
          <div className="grid gap-x-10 gap-y-1 py-3 md:grid-cols-2">
            {visibleMarketSkills.map((skill) => (
              <MarketSkillRow
                key={skill.skillId}
                skill={skill}
                onSelect={(next) => setSelected({ kind: "market", skill: next })}
              />
            ))}
          </div>
        ) : (
          <div className="px-3 py-12 text-center text-sm text-muted-foreground">
            {view === "updates"
              ? t("settings.skills.noUpdates", { defaultValue: "All installed skills are current." })
              : t("settings.skills.empty", { defaultValue: "No skills are available." })}
          </div>
        )}
      </section>

      {selected?.kind === "local" ? (
        <LocalSkillDetailSheet
          skill={selected.skill}
          open
          onOpenChange={(open) => !open && setSelected(null)}
        />
      ) : null}
      {selected?.kind === "market" ? (
        <MarketSkillDetailSheet
          skill={selected.skill}
          open
          onOpenChange={(open) => !open && setSelected(null)}
          onChanged={refresh}
        />
      ) : null}
    </div>
  );
}

function MarketSkillRow({
  skill,
  onSelect,
}: {
  skill: SkillMarketItem;
  onSelect: (skill: SkillMarketItem) => void;
}) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      aria-label={t("settings.skills.openDetails", {
        name: skillTitle(skill),
        defaultValue: "Open details for {{name}}",
      })}
      onClick={() => onSelect(skill)}
      className="group flex min-w-0 items-center gap-3 rounded-md px-3 py-3 text-left transition-colors hover:bg-muted/45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-md bg-muted/70 text-muted-foreground">
        <Brain className="h-5 w-5" strokeWidth={1.8} aria-hidden />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <h3 className="truncate text-[15px] font-semibold leading-5 text-foreground">
            {skillTitle(skill)}
          </h3>
          {skill.required ? (
            <LockKeyhole className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-label={t("settings.skills.required", { defaultValue: "Required" })} />
          ) : null}
        </div>
        <p className="mt-1 line-clamp-2 text-[13px] leading-5 text-muted-foreground">
          {skill.summary || skill.description || skill.skillId}
        </p>
      </div>
      <span className={cn(
        "hidden shrink-0 rounded-full px-2 py-1 text-[11px] font-medium sm:inline-flex",
        skill.updateAvailable
          ? "bg-amber-500/10 text-amber-700 dark:text-amber-300"
          : skill.installed
            ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
            : "bg-muted text-muted-foreground",
      )}>
        {skill.updateAvailable
          ? t("settings.skills.updateAvailable", { defaultValue: "Update" })
          : skill.installed
            ? t("settings.skills.installed", { defaultValue: "Installed" })
            : skill.latestVersion || skill.version || t("settings.skills.available", { defaultValue: "Available" })}
      </span>
    </button>
  );
}

function LocalSkillRow({ skill, onSelect }: { skill: SkillSummary; onSelect: (skill: SkillSummary) => void }) {
  const { t, i18n } = useTranslation();
  const presentation = builtinSkillPresentation(skill, i18n.resolvedLanguage ?? i18n.language);
  const StatusIcon = skill.available ? Check : CircleAlert;
  return (
    <button
      type="button"
      aria-label={t("settings.skills.openDetails", {
        name: presentation.name,
        defaultValue: "Open details for {{name}}",
      })}
      onClick={() => onSelect(skill)}
      className={cn(
        "group flex min-w-0 items-center gap-3 rounded-md px-3 py-3 text-left transition-colors hover:bg-muted/45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        !skill.available && "opacity-65",
      )}
    >
      <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-md bg-muted/70 text-muted-foreground">
        <Brain className="h-5 w-5" strokeWidth={1.8} aria-hidden />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <h3 className="truncate text-[15px] font-semibold">{presentation.name}</h3>
          <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold text-muted-foreground">
            {skillSourceLabel(skill.source, t)}
          </span>
        </div>
        <p className="mt-1 line-clamp-2 text-[13px] leading-5 text-muted-foreground">{presentation.description}</p>
        {!skill.available && skill.unavailable_reason ? (
          <p className="mt-1 truncate text-[12px] text-muted-foreground/80">
            {t("settings.skills.unavailableReason", { reason: skill.unavailable_reason, defaultValue: "Missing: {{reason}}" })}
          </p>
        ) : null}
      </div>
      <StatusIcon className="hidden h-4 w-4 shrink-0 text-muted-foreground sm:block" aria-hidden />
    </button>
  );
}

function MarketSkillDetailSheet({
  skill,
  open,
  onOpenChange,
  onChanged,
}: {
  skill: SkillMarketItem;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChanged: () => Promise<void>;
}) {
  const { client, token } = useClient();
  const { t } = useTranslation();
  const [detail, setDetail] = useState(skill);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rollbackVersion, setRollbackVersion] = useState(
    publishedRollbackVersions(skill)[0] || "",
  );
  const detailRef = useRef(skill);
  const detailGenerationRef = useRef(0);

  const applyDetail = useCallback((next: SkillMarketItem, resetRollback: boolean = true) => {
    detailRef.current = next;
    setDetail(next);
    if (resetRollback) setRollbackVersion(publishedRollbackVersions(next)[0] || "");
  }, []);

  const refreshDetail = useCallback(async () => {
    const generation = ++detailGenerationRef.current;
    const [detailResult, inventoryResult] = await Promise.allSettled([
      fetchSkillMarketDetail(token, skill.skillId),
      fetchInstalledSkills(token),
    ]);
    if (generation !== detailGenerationRef.current) return;
    if (detailResult.status === "rejected" && inventoryResult.status === "rejected") return;

    let next = detailResult.status === "fulfilled"
      ? { ...skill, ...detailResult.value }
      : detailRef.current;
    if (inventoryResult.status === "fulfilled") {
      const inventorySkill = inventoryResult.value.skills.find(
        (item) => item.skillId === skill.skillId,
      );
      next = inventorySkill
        ? { ...next, ...inventorySkill, installed: inventorySkill.installed === true }
        : { ...next, installed: false };
    }
    applyDetail(next);
  }, [applyDetail, skill, token]);

  useEffect(() => {
    void refreshDetail();
    return () => {
      detailGenerationRef.current += 1;
    };
  }, [refreshDetail]);

  const run = async (
    operation: string,
    action: () => Promise<unknown>,
    rollbackOptimistic?: () => void,
  ) => {
    setBusy(operation);
    setError(null);
    try {
      let failed: string | null = null;
      try {
        const result = await action();
        failed = failedOperationMessage(result, t);
      } catch (reason) {
        failed = operationErrorMessage(reason, t);
      }
      if (failed) {
        rollbackOptimistic?.();
        setError(failed);
      }
      await Promise.allSettled([onChanged()]);
      await refreshDetail();
    } finally {
      setBusy(null);
    }
  };

  const canManage = detail.canManage !== false;
  const uninstallLocked = detail.required || detail.canUninstall === false || !canManage;
  const publisher = typeof detail.publisher === "string"
    ? detail.publisher
    : detail.publisher?.name || t("settings.skills.unknownPublisher", { defaultValue: "Unknown publisher" });
  const signatureVerified = detail.signature?.verified === true || detail.signature?.status === "verified";

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-[min(36rem,calc(100vw-1rem))] max-w-none gap-0 overflow-hidden p-0 sm:max-w-none">
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
          <div className="flex items-start gap-3 pr-8">
            <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-md bg-muted/70 text-muted-foreground">
              <Brain className="h-5 w-5" aria-hidden />
            </div>
            <div className="min-w-0">
              <SheetTitle className="truncate text-[20px] font-semibold">{skillTitle(detail)}</SheetTitle>
              <SheetDescription className="mt-1 line-clamp-2">{detail.summary || detail.description}</SheetDescription>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {detail.required ? <Pill>{t("settings.skills.organizationRequired", { defaultValue: "Organization required" })}</Pill> : null}
                {detail.installed ? <Pill tone="success">{t("settings.skills.installed", { defaultValue: "Installed" })}</Pill> : null}
              </div>
            </div>
          </div>

          <div className="mt-7 space-y-6">
            <div className="grid grid-cols-2 gap-2">
              <MetaItem label={t("settings.skills.publisher", { defaultValue: "Publisher" })} value={publisher} />
              <MetaItem
                label={t("settings.skills.signature", { defaultValue: "Signature" })}
                value={signatureVerified
                  ? t("settings.skills.signatureVerified", { defaultValue: "Verified" })
                  : detail.signature?.status || t("settings.skills.signatureUnknown", { defaultValue: "Unknown" })}
                icon={signatureVerified ? <ShieldCheck className="h-3.5 w-3.5" aria-hidden /> : undefined}
              />
              <MetaItem
                label={t("settings.skills.currentVersion", { defaultValue: "Current version" })}
                value={detail.installedVersion || t("settings.skills.notInstalled", { defaultValue: "Not installed" })}
              />
              <MetaItem
                label={t("settings.skills.latestVersion", { defaultValue: "Latest version" })}
                value={detail.latestVersion || detail.version || "-"}
              />
            </div>

            {detail.description ? (
              <DetailSection title={t("settings.skills.descriptionTitle", { defaultValue: "Description" })}>
                <p className="text-[14px] leading-6 text-muted-foreground">{detail.description}</p>
              </DetailSection>
            ) : null}

            {detail.changelog ? (
              <DetailSection title={t("settings.skills.changelog", { defaultValue: "Changelog" })}>
                <p className="whitespace-pre-wrap text-[13px] leading-5 text-muted-foreground">{detail.changelog}</p>
              </DetailSection>
            ) : null}

            {detail.installed ? (
              <DetailSection title={t("settings.skills.updatePolicy", { defaultValue: "Update policy" })}>
                <select
                  aria-label={t("settings.skills.updatePolicy", { defaultValue: "Update policy" })}
                  value={detail.updatePolicy || "manual"}
                  disabled={busy !== null || detail.required || !canManage}
                  onChange={(event) => {
                    const updatePolicy = event.target.value as SkillUpdatePolicy;
                    const previous = detailRef.current;
                    applyDetail({ ...previous, updatePolicy }, false);
                    void run("policy", () => client.setSkillUpdatePolicy(
                      previous.skillId,
                      updatePolicy,
                      {
                        expectedRowVersion: previous.rowVersion,
                        ...(updatePolicy === "pinned" && previous.installedVersion
                          ? { version: previous.installedVersion }
                          : {}),
                      },
                    ), () => applyDetail(previous, false));
                  }}
                  className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus:ring-2 focus:ring-ring disabled:opacity-55"
                >
                  {UPDATE_POLICIES.map((policy) => (
                    <option key={policy} value={policy}>{policyLabel(policy, t)}</option>
                  ))}
                </select>
              </DetailSection>
            ) : null}

            {detail.installed && rollbackVersion ? (
              <DetailSection title={t("settings.skills.rollback", { defaultValue: "Rollback" })}>
                <div className="flex gap-2">
                  <select
                    aria-label={t("settings.skills.rollbackVersion", { defaultValue: "Rollback version" })}
                    value={rollbackVersion}
                    disabled={busy !== null || !canManage}
                    onChange={(event) => setRollbackVersion(event.target.value)}
                    className="h-9 min-w-0 flex-1 rounded-md border border-input bg-background px-3 text-sm outline-none focus:ring-2 focus:ring-ring"
                  >
                    {publishedRollbackVersions(detail).map((version) => <option key={version} value={version}>{version}</option>)}
                  </select>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={busy !== null || !canManage}
                    onClick={() => void run("rollback", () => client.rollbackSkill(
                      detail.skillId,
                      rollbackVersion,
                      { expectedRowVersion: detail.rowVersion },
                    ))}
                  >
                    <RotateCcw className="mr-2 h-4 w-4" aria-hidden />
                    {t("settings.skills.rollback", { defaultValue: "Rollback" })}
                  </Button>
                </div>
              </DetailSection>
            ) : null}

            {error ? <p role="alert" className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</p> : null}

            <div className="flex flex-wrap justify-end gap-2 border-t border-border/45 pt-4">
              {!detail.installed ? (
                <Button
                  type="button"
                  size="sm"
                  disabled={busy !== null || !canManage}
                  onClick={() => void run("install", () => client.installSkill(detail.skillId, {
                    updatePolicy: detail.updatePolicy || "manual",
                    expectedRowVersion: detail.rowVersion ?? 0,
                  }))}
                >
                  {busy === "install" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden /> : <Download className="mr-2 h-4 w-4" aria-hidden />}
                  {t("settings.skills.install", { defaultValue: "Install" })}
                </Button>
              ) : (
                <>
                  {detail.updateAvailable ? (
                    <Button
                      type="button"
                      size="sm"
                      disabled={busy !== null || !canManage}
                      onClick={() => void run("update", () => client.updateSkill(detail.skillId, {
                        updatePolicy: detail.updatePolicy || "manual",
                        ...(detail.updatePolicy === "pinned" && detail.latestVersion
                          ? { version: detail.latestVersion }
                          : {}),
                        expectedRowVersion: detail.rowVersion,
                      }))}
                    >
                      {busy === "update" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden /> : <ArrowUpCircle className="mr-2 h-4 w-4" aria-hidden />}
                      {t("settings.skills.update", { defaultValue: "Update" })}
                    </Button>
                  ) : null}
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    title={detail.required ? t("settings.skills.requiredCannotRemove", { defaultValue: "Required skills are managed by your organization." }) : undefined}
                    disabled={busy !== null || uninstallLocked}
                    onClick={() => void run("uninstall", () => client.uninstallSkill(detail.skillId, {
                      expectedRowVersion: detail.rowVersion,
                    }))}
                  >
                    <Trash2 className="mr-2 h-4 w-4" aria-hidden />
                    {t("settings.skills.uninstall", { defaultValue: "Uninstall" })}
                  </Button>
                </>
              )}
            </div>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function LocalSkillDetailSheet({ skill, open, onOpenChange }: { skill: SkillSummary; open: boolean; onOpenChange: (open: boolean) => void }) {
  const { token } = useClient();
  const { t, i18n } = useTranslation();
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const presentation = builtinSkillPresentation(
    detail ?? skill,
    i18n.resolvedLanguage ?? i18n.language,
  );

  useEffect(() => {
    let active = true;
    setLoading(true);
    setFailed(false);
    void fetchSkillDetail(token, skill.name)
      .then((payload) => active && setDetail(payload))
      .catch(() => active && setFailed(true))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [skill.name, token]);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-[min(34rem,calc(100vw-1rem))] max-w-none gap-0 overflow-hidden p-0 sm:max-w-none">
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
          <SheetTitle className="truncate pr-8 text-[20px] font-semibold">{presentation.name}</SheetTitle>
          <SheetDescription className="mt-1">{presentation.description}</SheetDescription>
          {loading ? (
            <div className="mt-8 flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" aria-hidden />{t("settings.skills.loadingDetail", { defaultValue: "Loading skill details..." })}</div>
          ) : failed ? (
            <p className="mt-8 text-sm text-destructive">{t("settings.skills.loadFailed", { defaultValue: "Could not load skill details." })}</p>
          ) : detail ? (
            <div className="mt-7 space-y-6">
              <div className="grid grid-cols-2 gap-2">
                <MetaItem label={t("settings.skills.source", { defaultValue: "Source" })} value={skillSourceLabel(detail.source, t)} />
                <MetaItem label={t("settings.skills.status", { defaultValue: "Status" })} value={detail.available ? t("settings.skills.statusAvailable", { defaultValue: "Available" }) : t("settings.skills.statusUnavailable", { defaultValue: "Unavailable" })} />
              </div>
              {!detail.available && detail.unavailable_reason ? (
                <DetailSection title={t("settings.skills.unavailableReasonLabel", { defaultValue: "Unavailable reason" })}>
                  <p className="text-[13px] text-destructive">{detail.unavailable_reason}</p>
                </DetailSection>
              ) : null}
              <RequirementsSection detail={detail} />
              <RawInstructionsBlock markdown={detail.raw_markdown} />
            </div>
          ) : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function RawInstructionsBlock({ markdown }: { markdown: string }) {
  const { t } = useTranslation();
  return (
    <details className="rounded-md border border-border/45 bg-muted/20 px-3 py-3">
      <summary className="cursor-pointer text-[13px] font-medium">{t("settings.skills.rawInstructions", { defaultValue: "Raw SKILL.md" })}</summary>
      <pre className="mt-3 max-h-[42vh] overflow-auto whitespace-pre-wrap break-words rounded bg-background/70 px-3 py-3 font-mono text-[12px] leading-6 text-foreground/65">
        {markdown || t("settings.skills.rawInstructionsEmpty", { defaultValue: "No raw instructions." })}
      </pre>
    </details>
  );
}

function RequirementsSection({ detail }: { detail: SkillDetail }) {
  const { t } = useTranslation();
  const { bins, env, missing_bins, missing_env } = detail.requirements;
  if (!bins.length && !env.length) {
    return <DetailSection title={t("settings.skills.requirements", { defaultValue: "Requirements" })}><p className="text-[13px] text-muted-foreground">{t("settings.skills.noRequirements", { defaultValue: "No explicit requirements." })}</p></DetailSection>;
  }
  return (
    <DetailSection title={t("settings.skills.requirements", { defaultValue: "Requirements" })}>
      <div className="space-y-3">
        {missing_bins.length ? <RequirementLine title={t("settings.skills.missingCommands", { defaultValue: "Missing CLI" })} items={missing_bins} icon={<Terminal className="h-3.5 w-3.5" aria-hidden />} danger /> : null}
        {missing_env.length ? <RequirementLine title={t("settings.skills.missingEnvironment", { defaultValue: "Missing ENV" })} items={missing_env} icon={<KeyRound className="h-3.5 w-3.5" aria-hidden />} danger /> : null}
        {bins.length ? <RequirementLine title={t("settings.skills.commands", { defaultValue: "Commands" })} items={bins} icon={<Terminal className="h-3.5 w-3.5" aria-hidden />} /> : null}
        {env.length ? <RequirementLine title={t("settings.skills.environment", { defaultValue: "Environment variables" })} items={env} icon={<KeyRound className="h-3.5 w-3.5" aria-hidden />} /> : null}
      </div>
    </DetailSection>
  );
}

function RequirementLine({ title, items, icon, danger = false }: { title: string; items: string[]; icon: ReactNode; danger?: boolean }) {
  return <div><div className={cn("mb-1.5 flex items-center gap-1.5 text-[12px]", danger ? "text-destructive" : "text-muted-foreground")}>{icon}{title}</div><div className="flex flex-wrap gap-1.5">{items.map((item) => <Pill key={item}>{item}</Pill>)}</div></div>;
}

function DetailSection({ title, children }: { title: string; children: ReactNode }) {
  return <section><h3 className="mb-2 text-[12px] font-medium text-muted-foreground">{title}</h3>{children}</section>;
}

function MetaItem({ label, value, icon }: { label: string; value: string; icon?: ReactNode }) {
  return <div className="rounded-md bg-muted/35 px-3 py-2.5"><div className="text-[11px] text-muted-foreground">{label}</div><div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-[13px] font-medium">{icon}<span className="truncate">{value}</span></div></div>;
}

function Pill({ children, tone = "muted" }: { children: ReactNode; tone?: "muted" | "success" }) {
  return <span className={cn("inline-flex max-w-full items-center rounded-full px-2 py-0.5 text-[11px] font-medium", tone === "success" ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" : "bg-muted text-muted-foreground")}>{children}</span>;
}

function mergeCatalogInventory(catalog: SkillMarketItem[], inventory: SkillMarketItem[]): SkillMarketItem[] {
  const installed = new Map(inventory.map((skill) => [skill.skillId, skill]));
  const merged = catalog.map((skill) => {
    const active = installed.get(skill.skillId);
    return active ? { ...skill, ...active, installed: true } : skill;
  });
  const known = new Set(merged.map((skill) => skill.skillId));
  return [...merged, ...inventory.filter((skill) => !known.has(skill.skillId)).map((skill) => ({ ...skill, installed: true }))];
}

function skillTitle(skill: SkillMarketItem): string {
  return skill.displayName || skill.name || skill.skillKey || skill.skillId;
}

function skillOperationErrorCode(value: unknown): string | null {
  if (!value || typeof value !== "object") return null;
  const payload = value as Record<string, unknown>;
  const raw = payload.errorCode ?? payload.error_code ?? payload.code;
  return typeof raw === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$/.test(raw)
    ? raw
    : null;
}

function operationErrorMessage(reason: unknown, t: TFunction): string {
  const code = reason instanceof SkillRequestError
    ? skillOperationErrorCode({ code: reason.code })
    : skillOperationErrorCode(reason);
  return code
    ? t("settings.skills.operationFailedWithCode", {
      code,
      defaultValue: "Skill operation failed ({{code}}). The latest state has been reloaded.",
    })
    : t("settings.skills.operationFailed", {
      defaultValue: "Skill operation failed. The latest state has been reloaded.",
    });
}

function failedOperationMessage(payload: unknown, t: TFunction): string | null {
  if (!payload || typeof payload !== "object") return null;
  if ((payload as Record<string, unknown>).status !== "failed") return null;
  return operationErrorMessage(payload, t);
}

function viewLabel(view: CatalogView, t: TFunction): string {
  if (view === "installed") return t("settings.skills.installedView", { defaultValue: "Installed" });
  if (view === "updates") return t("settings.skills.updatesView", { defaultValue: "Updates" });
  return t("settings.skills.discoverView", { defaultValue: "Discover" });
}

function policyLabel(policy: SkillUpdatePolicy, t: TFunction): string {
  const labels: Record<SkillUpdatePolicy, string> = {
    manual: t("settings.skills.policyManual", { defaultValue: "Manual" }),
    notify: t("settings.skills.policyNotify", { defaultValue: "Notify me" }),
    auto_stable: t("settings.skills.policyAutoStable", { defaultValue: "Auto-update stable" }),
    pinned: t("settings.skills.policyPinned", { defaultValue: "Pinned version" }),
  };
  return labels[policy];
}

function skillSourceLabel(source: string, t: TFunction): string {
  if (source === "workspace") return t("settings.skills.sourceWorkspace", { defaultValue: "Custom" });
  if (source === "builtin") return t("settings.skills.sourceBuiltin", { defaultValue: "Built-in" });
  if (source === "managed") return t("settings.skills.sourceManaged", { defaultValue: "Managed" });
  return source;
}
