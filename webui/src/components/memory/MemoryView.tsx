import { useCallback, useEffect, useMemo, useState } from "react";
import { Database, LockKeyhole, PanelLeft, RefreshCw, Save } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { MemoryRequestError, type NanobotClient } from "@/lib/nanobot-client";
import type {
  ManagedMemoryDocument,
  MemoryManagementPayload,
  MemoryScopeType,
} from "@/lib/types";
import { cn } from "@/lib/utils";

const SCOPE_ORDER: MemoryScopeType[] = ["system", "org", "user"];

function emptyDocument(scopeType: MemoryScopeType): ManagedMemoryDocument {
  return { scopeType, content: "", version: 0, canEdit: false };
}

export function MemoryView({
  client,
  onToggleSidebar,
}: {
  client: NanobotClient;
  onToggleSidebar: () => void;
}) {
  const { t } = useTranslation();
  const [payload, setPayload] = useState<MemoryManagementPayload | null>(null);
  const [activeScope, setActiveScope] = useState<MemoryScopeType>("user");
  const [drafts, setDrafts] = useState<Record<MemoryScopeType, string>>({
    system: "",
    org: "",
    user: "",
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const next = await client.getMemory();
      setPayload(next);
      setDrafts({
        system: next.documents.find((item) => item.scopeType === "system")?.content ?? "",
        org: next.documents.find((item) => item.scopeType === "org")?.content ?? "",
        user: next.documents.find((item) => item.scopeType === "user")?.content ?? "",
      });
    } catch {
      setError(t("memory.errors.load"));
    } finally {
      setLoading(false);
    }
  }, [client, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const documents = useMemo(() => {
    const byScope = new Map(payload?.documents.map((item) => [item.scopeType, item]));
    return SCOPE_ORDER.map((scope) => byScope.get(scope) ?? emptyDocument(scope));
  }, [payload]);
  const activeDocument = documents.find((item) => item.scopeType === activeScope)
    ?? emptyDocument(activeScope);
  const draft = drafts[activeScope];
  const changed = draft !== activeDocument.content;

  const save = useCallback(async () => {
    if (!activeDocument.canEdit || !changed || saving) return;
    setSaving(true);
    setSaved(false);
    setError("");
    try {
      const result = await client.updateMemory({
        scopeType: activeScope,
        content: draft,
        expectedVersion: activeDocument.version,
      });
      setPayload((current) => current ? {
        ...current,
        documents: current.documents.map((item) =>
          item.scopeType === activeScope ? result.document : item
        ),
      } : current);
      setSaved(true);
    } catch (caught) {
      if (caught instanceof MemoryRequestError && caught.status === 409) {
        await load();
        setError(t("memory.errors.conflict"));
      } else if (caught instanceof MemoryRequestError && caught.status === 403) {
        setError(t("memory.errors.permission"));
      } else {
        setError(t("memory.errors.save"));
      }
    } finally {
      setSaving(false);
    }
  }, [
    activeDocument.canEdit,
    activeDocument.version,
    activeScope,
    changed,
    draft,
    load,
    saving,
    t,
  ]);

  const scopeName = t(`memory.scopes.${activeScope}.name`);
  const owner = activeScope === "org"
    ? payload?.orgName
    : activeScope === "user"
      ? payload?.userName
      : "";

  return (
    <section className="flex h-full min-h-0 flex-col bg-background" aria-label={t("memory.title")}>
      <header className="flex h-14 shrink-0 items-center gap-3 border-b px-4 sm:px-6">
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={onToggleSidebar}
          aria-label={t("thread.header.toggleSidebar")}
          className="h-8 w-8"
        >
          <PanelLeft className="h-4 w-4" />
        </Button>
        <Database className="h-4 w-4 text-muted-foreground" />
        <h1 className="text-sm font-semibold">{t("memory.title")}</h1>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={() => void load()}
          disabled={loading || saving}
          aria-label={t("memory.refresh")}
          className="ml-auto h-8 w-8"
        >
          <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
        </Button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex min-h-full w-full max-w-5xl flex-col px-4 py-5 sm:px-6 sm:py-7">
          <div
            className="grid h-10 w-full max-w-xl grid-cols-3 rounded-md bg-muted p-1"
            role="tablist"
            aria-label={t("memory.scopePicker")}
          >
            {documents.map((document) => (
              <button
                key={document.scopeType}
                type="button"
                role="tab"
                aria-selected={activeScope === document.scopeType}
                onClick={() => {
                  setActiveScope(document.scopeType);
                  setError("");
                  setSaved(false);
                }}
                className={cn(
                  "min-w-0 rounded px-2 text-sm font-medium transition-colors",
                  activeScope === document.scopeType
                    ? "bg-background text-foreground shadow-sm"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                <span className="block truncate">
                  {t(`memory.scopes.${document.scopeType}.name`)}
                </span>
              </button>
            ))}
          </div>

          <div className="mt-6 flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <h2 className="text-base font-semibold">{scopeName}</h2>
              <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
                {t(`memory.scopes.${activeScope}.description`)}
              </p>
              {owner ? (
                <p className="mt-1 truncate text-xs text-muted-foreground">{owner}</p>
              ) : null}
            </div>
            <span className="shrink-0 text-xs text-muted-foreground">
              {t("memory.version", { version: activeDocument.version })}
            </span>
          </div>

          <div className="mt-4 flex min-h-[22rem] flex-1 flex-col overflow-hidden rounded-md border bg-background">
            {loading ? (
              <div className="flex min-h-[22rem] items-center justify-center text-sm text-muted-foreground">
                {t("memory.loading")}
              </div>
            ) : (
              <Textarea
                value={draft}
                onChange={(event) => {
                  const value = event.target.value;
                  setDrafts((current) => ({ ...current, [activeScope]: value }));
                  setSaved(false);
                }}
                readOnly={!activeDocument.canEdit}
                maxLength={64_000}
                spellCheck={false}
                aria-label={t("memory.editorLabel", { scope: scopeName })}
                placeholder={t("memory.empty")}
                className="min-h-[22rem] flex-1 resize-none rounded-none border-0 bg-transparent p-4 font-mono text-sm leading-6 shadow-none focus-visible:ring-0"
              />
            )}
            <div className="flex min-h-12 shrink-0 items-center gap-3 border-t px-3 py-2">
              {activeDocument.canEdit ? (
                <span className="text-xs text-muted-foreground">
                  {draft.length.toLocaleString()} / 64,000
                </span>
              ) : (
                <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
                  <LockKeyhole className="h-3.5 w-3.5 shrink-0" />
                  <span className="truncate">{t("memory.readOnly")}</span>
                </span>
              )}
              {error ? <span className="ml-auto text-xs text-destructive">{error}</span> : null}
              {!error && saved ? (
                <span className="ml-auto text-xs text-muted-foreground">{t("memory.saved")}</span>
              ) : null}
              {activeDocument.canEdit ? (
                <Button
                  type="button"
                  size="sm"
                  onClick={() => void save()}
                  disabled={!changed || saving || loading}
                  className={cn(!error && !saved && "ml-auto")}
                >
                  <Save className="h-4 w-4" />
                  {saving ? t("memory.saving") : t("memory.save")}
                </Button>
              ) : null}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
