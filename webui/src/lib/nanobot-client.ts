import type {
  ConnectionStatus,
  InboundEvent,
  Outbound,
  OutboundCliAppMention,
  OutboundMcpPresetMention,
  OutboundMedia,
  GoalStateWsPayload,
  ManagedMemoryUpdatePayload,
  MemoryManagementPayload,
  MemoryScopeType,
  SkillOperationPayload,
  SkillsUpdatedEvent,
  SkillUpdatePolicy,
  WorkspaceScopePayload,
} from "./types";
import { createHostWebSocket } from "./runtime";

/** WebSocket readyState constants, referenced by value to stay portable
 * across runtimes that don't expose a global ``WebSocket`` (tests, SSR). */
const WS_OPEN = 1;
const WS_CLOSING = 2;
const HOST_SOCKET_URL_PREFIX = "nanobot-host://";

function createDefaultSocket(url: string): WebSocket {
  if (url.startsWith(HOST_SOCKET_URL_PREFIX)) {
    return createHostWebSocket(url);
  }
  return new WebSocket(url);
}

/** Inbound WebSocket ``console.log`` / parse-failure ``console.warn``.
 *
 * - **Dev** (non-production bundle): **on by default** — messages appear at default log level.
 * - **Production**: off unless ``localStorage.setItem('nanobot_debug_ws','1')`` (or ``true``).
 * - **Silence anywhere**: ``localStorage.setItem('nanobot_debug_ws','0')`` (or ``false`` / ``off``).
 * Values are read on every frame; no reload needed.
 */
function wsInboundDebugEnabled(): boolean {
  if (typeof globalThis === "undefined") return false;
  try {
    if (import.meta.env.MODE === "test") return false;
    const ls = (globalThis as unknown as { localStorage?: Storage }).localStorage;
    const raw = ls?.getItem("nanobot_debug_ws")?.trim().toLowerCase() ?? "";
    if (raw === "0" || raw === "false" || raw === "off" || raw === "no") {
      return false;
    }
    if (raw === "1" || raw === "true" || raw === "on" || raw === "yes") {
      return true;
    }
    return !import.meta.env.PROD;
  } catch {
    return !import.meta.env.PROD;
  }
}

/** Shorten streaming text fields so logging stays usable for huge deltas. */
function summarizeInboundWsPayload(ev: InboundEvent): unknown {
  const kind = (ev as { event?: string }).event;
  if (kind === "memory_result") {
    const row = ev as Extract<InboundEvent, { event: "memory_result" }>;
    return { event: row.event, request_id: row.request_id, payload: "[redacted]" };
  }
  if (kind === "skill_operation_result") {
    const row = ev as Extract<InboundEvent, { event: "skill_operation_result" }>;
    return { event: row.event, request_id: row.request_id, payload: "[redacted]" };
  }
  if (kind !== "delta" && kind !== "reasoning_delta") return ev;
  const row = { ...(ev as object) } as Record<string, unknown>;
  const text = typeof row.text === "string" ? row.text : "";
  const max = 240;
  if (text.length > max) {
    row.text = `${text.slice(0, max)}… (${text.length} chars)`;
  }
  return row;
}

type Unsubscribe = () => void;
type EventHandler = (ev: InboundEvent) => void;
type StatusHandler = (status: ConnectionStatus) => void;
type RuntimeModelHandler = (modelName: string | null, modelPreset?: string | null) => void;
type SessionUpdateScope = "metadata" | "thread" | string;
type SessionUpdateHandler = (
  chatId: string,
  scope?: SessionUpdateScope,
  workspaceScope?: WorkspaceScopePayload,
) => void;
type RunStatusHandler = (chatId: string, startedAt: number | null) => void;
type SkillsUpdatedHandler = (event: SkillsUpdatedEvent) => void;
type SkillRequestFrame = Extract<
  Outbound,
  {
    type:
      | "skill_install"
      | "skill_update"
      | "skill_rollback"
      | "skill_uninstall"
      | "skill_set_update_policy"
      | "skill_sync_now";
  }
>;

/** Structured errors surfaced to the UI.
 *
 * Most entries are transport-level or protocol-level faults. Workspace scope
 * rejections are server application errors promoted here because they affect
 * controls outside the message stream and must be visible immediately.
 */
export type StreamError =
  /** Server rejected the inbound frame as too large (WS close code 1009).
   * Typically means the user attached images whose base64 size exceeded
   * ``maxMessageBytes`` on the server. */
  | { kind: "message_too_big" }
  | { kind: "workspace_scope_rejected"; reason?: string; chatId?: string };

type ErrorHandler = (error: StreamError) => void;

interface PendingNewChat {
  resolve: (chatId: string) => void;
  reject: (err: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

interface PendingTranscription {
  resolve: (text: string) => void;
  reject: (err: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

interface PendingMemoryRequest {
  resolve: (payload: unknown) => void;
  reject: (err: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

interface PendingSkillRequest {
  resolve: (payload: unknown) => void;
  reject: (err: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

export class MemoryRequestError extends Error {
  constructor(
    public readonly status: number,
    detail: string,
  ) {
    super(detail);
    this.name = "MemoryRequestError";
  }
}

export class SkillRequestError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    public readonly retryable: boolean,
    detail: string,
  ) {
    super(detail);
    this.name = "SkillRequestError";
  }
}

export interface NanobotClientOptions {
  url: string;
  reconnect?: boolean;
  /** Called when a connection drops so the app can refresh its token. */
  onReauth?: () => Promise<string | null>;
  /** Inject a custom WebSocket factory (used by unit tests). */
  socketFactory?: (url: string) => WebSocket;
  /** Delay-cap for reconnect backoff (ms). */
  maxBackoffMs?: number;
}

/**
 * Singleton WebSocket client that multiplexes chat streams.
 *
 * One socket carries many chat_ids: the server tags every outbound event with
 * ``chat_id``, and this class fans those events out to handlers registered
 * per chat. Reconnects are transparent and re-attach every known chat_id.
 */
export class NanobotClient {
  private socket: WebSocket | null = null;
  private statusHandlers = new Set<StatusHandler>();
  private runtimeModelHandlers = new Set<RuntimeModelHandler>();
  private sessionUpdateHandlers = new Set<SessionUpdateHandler>();
  private runStatusHandlers = new Set<RunStatusHandler>();
  private skillsUpdatedHandlers = new Set<SkillsUpdatedHandler>();
  private errorHandlers = new Set<ErrorHandler>();
  // chat_id -> handlers listening on it
  private chatHandlers = new Map<string, Set<EventHandler>>();
  /** Inbound frames received while no subscriber is registered (e.g. user switched away). */
  private pendingInboundByChat = new Map<string, InboundEvent[]>();
  private static readonly PENDING_INBOUND_MAX = 2000;
  // chat_ids we've attached to since connect; re-attached after reconnects
  private knownChats = new Set<string>();
  /** Wall-clock run strip: updated from ``goal_status`` even with no ``onChat`` subscriber. */
  private runStartedAtByChatId = new Map<string, number>();
  /** Latest ``goal_state`` snapshot per ``chat_id`` (multi-session isolation). */
  private goalStateByChatId = new Map<string, GoalStateWsPayload>();
  private pendingNewChat: PendingNewChat | null = null;
  private pendingTranscriptions = new Map<string, PendingTranscription>();
  private pendingMemoryRequests = new Map<string, PendingMemoryRequest>();
  private pendingSkillRequests = new Map<string, PendingSkillRequest>();
  // Frames queued while the socket is not yet OPEN
  private sendQueue: Outbound[] = [];
  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly shouldReconnect: boolean;
  private readonly maxBackoffMs: number;
  private socketFactory: (url: string) => WebSocket;
  private currentUrl: string;
  private status_: ConnectionStatus = "idle";
  private readyChatId: string | null = null;
  // Set by ``close()`` so the onclose handler knows the drop was intentional
  // and must not schedule a reconnect or flip status back to "reconnecting".
  private intentionallyClosed = false;

  constructor(private options: NanobotClientOptions) {
    this.shouldReconnect = options.reconnect ?? true;
    this.maxBackoffMs = options.maxBackoffMs ?? 15_000;
    this.socketFactory = options.socketFactory ?? createDefaultSocket;
    this.currentUrl = options.url;
  }

  get status(): ConnectionStatus {
    return this.status_;
  }

  get defaultChatId(): string | null {
    return this.readyChatId;
  }

  /** Swap the URL (e.g. after fetching a fresh token) then reconnect. */
  updateUrl(url: string, socketFactory?: (url: string) => WebSocket): void {
    this.currentUrl = url;
    if (socketFactory) {
      this.socketFactory = socketFactory;
    }
  }

  onStatus(handler: StatusHandler): Unsubscribe {
    this.statusHandlers.add(handler);
    handler(this.status_);
    return () => {
      this.statusHandlers.delete(handler);
    };
  }

  onRuntimeModelUpdate(handler: RuntimeModelHandler): Unsubscribe {
    this.runtimeModelHandlers.add(handler);
    return () => {
      this.runtimeModelHandlers.delete(handler);
    };
  }

  onSessionUpdate(handler: SessionUpdateHandler): Unsubscribe {
    this.sessionUpdateHandlers.add(handler);
    return () => {
      this.sessionUpdateHandlers.delete(handler);
    };
  }

  onRunStatus(handler: RunStatusHandler): Unsubscribe {
    this.runStatusHandlers.add(handler);
    for (const [chatId, startedAt] of this.runStartedAtByChatId) {
      handler(chatId, startedAt);
    }
    return () => {
      this.runStatusHandlers.delete(handler);
    };
  }

  onSkillsUpdated(handler: SkillsUpdatedHandler): Unsubscribe {
    this.skillsUpdatedHandlers.add(handler);
    return () => {
      this.skillsUpdatedHandlers.delete(handler);
    };
  }

  /** Subscribe to transport-level faults (see :type:`StreamError`). */
  onError(handler: ErrorHandler): Unsubscribe {
    this.errorHandlers.add(handler);
    return () => {
      this.errorHandlers.delete(handler);
    };
  }

  /** Last ``goal_status`` ``started_at`` (unix sec) for *chatId*, if the turn is running. */
  getRunStartedAt(chatId: string): number | null {
    const v = this.runStartedAtByChatId.get(chatId);
    return v === undefined ? null : v;
  }

  /** Last ``goal_state`` payload for *chatId*, if any frame has arrived this connection. */
  getGoalState(chatId: string): GoalStateWsPayload | undefined {
    return this.goalStateByChatId.get(chatId);
  }

  private recordGoalStatusForRunStrip(chatId: string, ev: InboundEvent): void {
    if (ev.event === "turn_end") {
      if (this.runStartedAtByChatId.has(chatId)) {
        this.runStartedAtByChatId.delete(chatId);
        this.emitRunStatus(chatId, null);
      }
      return;
    }
    if (ev.event !== "goal_status") return;
    if (ev.status === "running" && typeof ev.started_at === "number") {
      const previous = this.runStartedAtByChatId.get(chatId);
      this.runStartedAtByChatId.set(chatId, ev.started_at);
      if (previous !== ev.started_at) this.emitRunStatus(chatId, ev.started_at);
    } else if (this.runStartedAtByChatId.has(chatId)) {
      this.runStartedAtByChatId.delete(chatId);
      this.emitRunStatus(chatId, null);
    }
  }

  private recordGoalStateSnapshot(chatId: string, ev: InboundEvent): void {
    if (ev.event === "goal_state") {
      this.goalStateByChatId.set(chatId, ev.goal_state);
      return;
    }
    if (ev.event === "turn_end" && ev.goal_state != null && typeof ev.goal_state === "object") {
      this.goalStateByChatId.set(chatId, ev.goal_state);
    }
  }

  /** Subscribe to events for a given chat_id. Auto-attaches on the next open. */
  onChat(chatId: string, handler: EventHandler): Unsubscribe {
    let handlers = this.chatHandlers.get(chatId);
    if (!handlers) {
      handlers = new Set();
      this.chatHandlers.set(chatId, handlers);
    }
    handlers.add(handler);
    const pending = this.pendingInboundByChat.get(chatId);
    if (pending !== undefined && pending.length > 0) {
      const flushed = pending.splice(0);
      this.pendingInboundByChat.delete(chatId);
      for (const ev of flushed) {
        handler(ev);
      }
    }
    this.attach(chatId);
    return () => {
      const current = this.chatHandlers.get(chatId);
      if (!current) return;
      current.delete(handler);
      if (current.size === 0) this.chatHandlers.delete(chatId);
    };
  }

  connect(): void {
    if (this.socket && this.socket.readyState < WS_CLOSING) return;
    this.intentionallyClosed = false;
    this.setStatus("connecting");
    const sock = this.socketFactory(this.currentUrl);
    this.socket = sock;
    sock.onopen = () => this.handleOpen();
    sock.onmessage = (ev) => this.handleMessage(ev);
    sock.onerror = () => this.setStatus("error");
    sock.onclose = (ev) => this.handleClose(ev);
  }

  close(): void {
    this.intentionallyClosed = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    const sock = this.socket;
    this.socket = null;
    try {
      sock?.close();
    } catch {
      // ignore
    }
    this.setStatus("closed");
  }

  /** Ask the server to provision a new chat_id; resolves with the assigned id. */
  newChat(timeoutMs: number = 5_000, workspaceScope?: WorkspaceScopePayload | null): Promise<string> {
    if (this.pendingNewChat) {
      return Promise.reject(new Error("newChat already in flight"));
    }
    return new Promise<string>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingNewChat = null;
        reject(new Error("newChat timed out"));
      }, timeoutMs);
      this.pendingNewChat = { resolve, reject, timer };
      this.queueSend({
        type: "new_chat",
        ...(workspaceScope ? { workspace_scope: workspaceScope } : {}),
      });
    });
  }

  transcribeAudio(
    dataUrl: string,
    options?: { durationMs?: number; timeoutMs?: number },
  ): Promise<string> {
    const requestId = crypto.randomUUID();
    const timeoutMs = options?.timeoutMs ?? 120_000;
    return new Promise<string>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingTranscriptions.delete(requestId);
        reject(new Error("transcription timed out"));
      }, timeoutMs);
      this.pendingTranscriptions.set(requestId, { resolve, reject, timer });
      this.queueSend({
        type: "transcribe_audio",
        request_id: requestId,
        data_url: dataUrl,
        ...(options?.durationMs !== undefined ? { duration_ms: options.durationMs } : {}),
      });
    });
  }

  getMemory(timeoutMs: number = 20_000): Promise<MemoryManagementPayload> {
    return this.requestMemory<MemoryManagementPayload>(
      { type: "memory_get", request_id: crypto.randomUUID() },
      timeoutMs,
    );
  }

  updateMemory(
    values: {
      scopeType: MemoryScopeType;
      content: string;
      expectedVersion: number;
    },
    timeoutMs: number = 20_000,
  ): Promise<ManagedMemoryUpdatePayload> {
    return this.requestMemory<ManagedMemoryUpdatePayload>(
      {
        type: "memory_update",
        request_id: crypto.randomUUID(),
        ...values,
      },
      timeoutMs,
    );
  }

  installSkill(
    skillId: string,
    options: {
      version?: string;
      updatePolicy?: SkillUpdatePolicy;
      expectedRowVersion?: number;
      timeoutMs?: number;
    } = {},
  ): Promise<SkillOperationPayload> {
    return this.requestSkill({
      type: "skill_install",
      request_id: crypto.randomUUID(),
      skillId,
      ...(options.version ? { version: options.version } : {}),
      ...(options.updatePolicy ? { updatePolicy: options.updatePolicy } : {}),
      expectedRowVersion: options.expectedRowVersion ?? 0,
    }, options.timeoutMs);
  }

  updateSkill(
    skillId: string,
    options: {
      version?: string;
      updatePolicy?: SkillUpdatePolicy;
      expectedRowVersion?: number;
      timeoutMs?: number;
    } = {},
  ): Promise<SkillOperationPayload> {
    return this.requestSkill({
      type: "skill_update",
      request_id: crypto.randomUUID(),
      skillId,
      ...(options.version ? { version: options.version } : {}),
      ...(options.updatePolicy ? { updatePolicy: options.updatePolicy } : {}),
      ...(options.expectedRowVersion !== undefined
        ? { expectedRowVersion: options.expectedRowVersion }
        : {}),
    }, options.timeoutMs);
  }

  rollbackSkill(
    skillId: string,
    version: string,
    options: { expectedRowVersion?: number; timeoutMs?: number } = {},
  ): Promise<SkillOperationPayload> {
    return this.requestSkill({
      type: "skill_rollback",
      request_id: crypto.randomUUID(),
      skillId,
      version,
      ...(options.expectedRowVersion !== undefined
        ? { expectedRowVersion: options.expectedRowVersion }
        : {}),
    }, options.timeoutMs);
  }

  uninstallSkill(
    skillId: string,
    options: { expectedRowVersion?: number; timeoutMs?: number } = {},
  ): Promise<SkillOperationPayload> {
    return this.requestSkill({
      type: "skill_uninstall",
      request_id: crypto.randomUUID(),
      skillId,
      ...(options.expectedRowVersion !== undefined
        ? { expectedRowVersion: options.expectedRowVersion }
        : {}),
    }, options.timeoutMs);
  }

  setSkillUpdatePolicy(
    skillId: string,
    updatePolicy: SkillUpdatePolicy,
    options: { version?: string; expectedRowVersion?: number; timeoutMs?: number } = {},
  ): Promise<SkillOperationPayload> {
    return this.requestSkill({
      type: "skill_set_update_policy",
      request_id: crypto.randomUUID(),
      skillId,
      updatePolicy,
      ...(options.version ? { version: options.version } : {}),
      ...(options.expectedRowVersion !== undefined
        ? { expectedRowVersion: options.expectedRowVersion }
        : {}),
    }, options.timeoutMs);
  }

  syncSkills(timeoutMs?: number): Promise<SkillOperationPayload> {
    return this.requestSkill({
      type: "skill_sync_now",
      request_id: crypto.randomUUID(),
    }, timeoutMs);
  }

  /** Ask the server to create a non-destructive fork before a user-message index. */
  forkChat(
    sourceChatId: string,
    beforeUserIndex: number,
    title?: string,
    timeoutMs: number = 5_000,
  ): Promise<string> {
    if (this.pendingNewChat) {
      return Promise.reject(new Error("newChat already in flight"));
    }
    return new Promise<string>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingNewChat = null;
        reject(new Error("forkChat timed out"));
      }, timeoutMs);
      this.pendingNewChat = { resolve, reject, timer };
      this.queueSend({
        type: "fork_chat",
        source_chat_id: sourceChatId,
        before_user_index: beforeUserIndex,
        ...(title?.trim() ? { title: title.trim() } : {}),
      });
    });
  }

  attach(chatId: string): void {
    this.knownChats.add(chatId);
    if (this.socket?.readyState === WS_OPEN) {
      this.queueSend({ type: "attach", chat_id: chatId });
    }
  }

  sendMessage(
    chatId: string,
    content: string,
    media?: OutboundMedia[],
    options?: {
      cliApps?: OutboundCliAppMention[];
      mcpPresets?: OutboundMcpPresetMention[];
      workspaceScope?: WorkspaceScopePayload | null;
      turnId?: string;
    },
  ): void {
    this.knownChats.add(chatId);
    const frame: Outbound = {
      type: "message",
      chat_id: chatId,
      content,
      ...(media && media.length > 0 ? { media } : {}),
      ...(options?.cliApps?.length ? { cli_apps: options.cliApps } : {}),
      ...(options?.mcpPresets?.length ? { mcp_presets: options.mcpPresets } : {}),
      ...(options?.workspaceScope ? { workspace_scope: options.workspaceScope } : {}),
      ...(options?.turnId ? { turn_id: options.turnId } : {}),
      webui: true,
    };
    this.queueSend(frame);
  }

  setWorkspaceScope(chatId: string, workspaceScope: WorkspaceScopePayload): void {
    this.knownChats.add(chatId);
    this.queueSend({
      type: "set_workspace_scope",
      chat_id: chatId,
      workspace_scope: workspaceScope,
    });
  }

  // -- internals ---------------------------------------------------------

  private setStatus(status: ConnectionStatus): void {
    if (this.status_ === status) return;
    this.status_ = status;
    for (const handler of this.statusHandlers) handler(status);
  }

  private clearRunStatusesForReconnect(): void {
    if (this.runStartedAtByChatId.size === 0) return;
    const chatIds = [...this.runStartedAtByChatId.keys()];
    this.runStartedAtByChatId.clear();
    for (const chatId of chatIds) this.emitRunStatus(chatId, null);
  }

  private handleOpen(): void {
    this.setStatus("open");
    this.reconnectAttempts = 0;
    // Re-attach every known chat_id so deliveries continue routing after a drop.
    for (const chatId of this.knownChats) {
      this.rawSend({ type: "attach", chat_id: chatId });
    }
    // Flush anything queued during reconnect.
    const queued = this.sendQueue.splice(0);
    for (const frame of queued) this.rawSend(frame);
  }

  private handleMessage(ev: MessageEvent): void {
    let parsed: InboundEvent;
    try {
      parsed = JSON.parse(typeof ev.data === "string" ? ev.data : "") as InboundEvent;
    } catch {
      if (wsInboundDebugEnabled()) {
        const raw = typeof ev.data === "string" ? ev.data : String(ev.data);
        console.warn(
          "[nanobot ws inbound] invalid JSON",
          raw.length > 400 ? `${raw.slice(0, 400)}… (${raw.length} chars)` : raw,
        );
      }
      return;
    }

    if (wsInboundDebugEnabled()) {
      console.log("[nanobot ws inbound]", summarizeInboundWsPayload(parsed));
    }

    if (parsed.event === "ready") {
      this.readyChatId = parsed.chat_id;
      this.knownChats.add(parsed.chat_id);
      return;
    }

    if (parsed.event === "attached") {
      this.knownChats.add(parsed.chat_id);
      if (this.pendingNewChat) {
        clearTimeout(this.pendingNewChat.timer);
        this.pendingNewChat.resolve(parsed.chat_id);
        this.pendingNewChat = null;
      }
      this.dispatch(parsed.chat_id, parsed);
      return;
    }

    if (parsed.event === "runtime_model_updated") {
      this.emitRuntimeModelUpdate(parsed.model_name || null, parsed.model_preset ?? null);
      return;
    }

    if (parsed.event === "transcription_result") {
      this.resolveTranscription(parsed.request_id, parsed.text);
      return;
    }

    if (parsed.event === "transcription_error") {
      this.rejectTranscription(parsed.request_id, parsed.detail || "error");
      return;
    }

    if (parsed.event === "memory_result") {
      this.resolveMemoryRequest(parsed.request_id, parsed.payload);
      return;
    }

    if (parsed.event === "memory_error") {
      this.rejectMemoryRequest(
        parsed.request_id,
        parsed.status,
        parsed.detail || "memory request failed",
      );
      return;
    }

    if (parsed.event === "skill_operation_result") {
      this.resolveSkillRequest(parsed.request_id, parsed.payload);
      return;
    }

    if (parsed.event === "skill_operation_error") {
      this.rejectSkillRequest(
        parsed.request_id,
        parsed.status,
        parsed.code,
        parsed.retryable === true,
        parsed.detail || "skill operation failed",
      );
      return;
    }

    if (parsed.event === "skills_updated") {
      for (const handler of this.skillsUpdatedHandlers) handler(parsed);
      return;
    }

    if (parsed.event === "session_updated") {
      this.emitSessionUpdate(parsed.chat_id, parsed.scope, parsed.workspace_scope);
      return;
    }

    if (parsed.event === "error" && parsed.detail === "workspace_scope_rejected") {
      this.emitError({
        kind: "workspace_scope_rejected",
        reason: parsed.reason,
        chatId: parsed.chat_id,
      });
      if (this.pendingNewChat) {
        clearTimeout(this.pendingNewChat.timer);
        this.pendingNewChat.reject(new Error(`workspace_scope_rejected:${parsed.reason || ""}`));
        this.pendingNewChat = null;
      }
    }

    if (parsed.event === "error" && this.pendingNewChat) {
      clearTimeout(this.pendingNewChat.timer);
      const detail = typeof parsed.detail === "string" ? parsed.detail : "server error";
      const reason = typeof parsed.reason === "string" && parsed.reason ? `:${parsed.reason}` : "";
      this.pendingNewChat.reject(new Error(`${detail}${reason}`));
      this.pendingNewChat = null;
    }

    const chatId = (parsed as { chat_id?: string }).chat_id;
    if (chatId) {
      this.recordGoalStatusForRunStrip(chatId, parsed);
      this.recordGoalStateSnapshot(chatId, parsed);
      this.dispatch(chatId, parsed);
    }
  }

  private emitRuntimeModelUpdate(modelName: string | null, modelPreset?: string | null): void {
    for (const handler of this.runtimeModelHandlers) {
      handler(modelName, modelPreset);
    }
  }

  private emitSessionUpdate(
    chatId: string,
    scope?: SessionUpdateScope,
    workspaceScope?: WorkspaceScopePayload,
  ): void {
    for (const handler of this.sessionUpdateHandlers) {
      handler(chatId, scope, workspaceScope);
    }
  }

  private emitRunStatus(chatId: string, startedAt: number | null): void {
    for (const handler of this.runStatusHandlers) {
      handler(chatId, startedAt);
    }
  }

  private dispatch(chatId: string, ev: InboundEvent): void {
    const handlers = this.chatHandlers.get(chatId);
    if (handlers !== undefined && handlers.size > 0) {
      for (const h of handlers) {
        h(ev);
      }
      return;
    }
    let q = this.pendingInboundByChat.get(chatId);
    if (!q) {
      q = [];
      this.pendingInboundByChat.set(chatId, q);
    }
    q.push(ev);
    const over = q.length - NanobotClient.PENDING_INBOUND_MAX;
    if (over > 0) {
      q.splice(0, over);
    }
  }

  private handleClose(event?: { code?: number }): void {
    this.socket = null;
    if (this.pendingNewChat) {
      clearTimeout(this.pendingNewChat.timer);
      this.pendingNewChat.reject(new Error("socket closed"));
      this.pendingNewChat = null;
    }
    this.rejectAllTranscriptions("socket closed");
    this.rejectAllMemoryRequests(503, "socket closed");
    this.rejectAllSkillRequests(503, "SOCKET_CLOSED", true, "socket closed");
    // Surface structured reasons *before* reconnect logic so the UI can
    // display the error even while the client transparently reconnects.
    // Browsers populate ``CloseEvent.code`` with the wire-level close code;
    // 1009 = Message Too Big (server's max frame guard).
    if (event?.code === 1009) {
      this.emitError({ kind: "message_too_big" });
    }
    if (this.intentionallyClosed || !this.shouldReconnect) {
      this.setStatus("closed");
      return;
    }
    this.scheduleReconnect();
  }

  private emitError(error: StreamError): void {
    // Isolate subscribers so a throwing handler cannot abort the surrounding
    // ``handleClose`` flow (which still owes us a reconnect decision + status
    // update). We deliberately swallow here: error reporting is best-effort
    // and must never be allowed to compound the failure it's reporting.
    for (const handler of this.errorHandlers) {
      try {
        handler(error);
      } catch {
        // best-effort: subscriber fault must not stall transport bookkeeping
      }
    }
  }

  private resolveTranscription(requestId: string, text: string): void {
    const pending = this.pendingTranscriptions.get(requestId);
    if (!pending) return;
    clearTimeout(pending.timer);
    this.pendingTranscriptions.delete(requestId);
    pending.resolve(text);
  }

  private rejectTranscription(requestId: string | undefined, detail: string): void {
    if (!requestId) {
      this.rejectAllTranscriptions(detail);
      return;
    }
    const pending = this.pendingTranscriptions.get(requestId);
    if (!pending) return;
    clearTimeout(pending.timer);
    this.pendingTranscriptions.delete(requestId);
    pending.reject(new Error(detail));
  }

  private rejectAllTranscriptions(detail: string): void {
    for (const [requestId, pending] of this.pendingTranscriptions) {
      clearTimeout(pending.timer);
      pending.reject(new Error(detail));
      this.pendingTranscriptions.delete(requestId);
    }
  }

  private requestMemory<T>(
    frame: Extract<Outbound, { type: "memory_get" | "memory_update" }>,
    timeoutMs: number,
  ): Promise<T> {
    const requestId = frame.request_id;
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingMemoryRequests.delete(requestId);
        reject(new MemoryRequestError(504, "memory request timed out"));
      }, timeoutMs);
      this.pendingMemoryRequests.set(requestId, {
        resolve: (payload) => resolve(payload as T),
        reject,
        timer,
      });
      this.queueSend(frame);
    });
  }

  private resolveMemoryRequest(requestId: string, payload: unknown): void {
    const pending = this.pendingMemoryRequests.get(requestId);
    if (!pending) return;
    clearTimeout(pending.timer);
    this.pendingMemoryRequests.delete(requestId);
    pending.resolve(payload);
  }

  private rejectMemoryRequest(
    requestId: string | undefined,
    status: number,
    detail: string,
  ): void {
    if (!requestId) {
      this.rejectAllMemoryRequests(status, detail);
      return;
    }
    const pending = this.pendingMemoryRequests.get(requestId);
    if (!pending) return;
    clearTimeout(pending.timer);
    this.pendingMemoryRequests.delete(requestId);
    pending.reject(new MemoryRequestError(status, detail));
  }

  private rejectAllMemoryRequests(status: number, detail: string): void {
    for (const [requestId, pending] of this.pendingMemoryRequests) {
      clearTimeout(pending.timer);
      pending.reject(new MemoryRequestError(status, detail));
      this.pendingMemoryRequests.delete(requestId);
    }
  }

  private requestSkill<T extends SkillOperationPayload>(
    frame: SkillRequestFrame,
    timeoutMs: number = 60_000,
  ): Promise<T> {
    const requestId = frame.request_id;
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingSkillRequests.delete(requestId);
        this.removeQueuedSkillFrames(requestId);
        reject(new SkillRequestError(504, "TIMEOUT", true, "skill request timed out"));
      }, timeoutMs);
      this.pendingSkillRequests.set(requestId, {
        resolve: (payload) => resolve(payload as T),
        reject,
        timer,
      });
      this.queueSend(frame);
    });
  }

  private resolveSkillRequest(requestId: string, payload: unknown): void {
    const pending = this.pendingSkillRequests.get(requestId);
    if (!pending) return;
    clearTimeout(pending.timer);
    this.pendingSkillRequests.delete(requestId);
    this.removeQueuedSkillFrames(requestId);
    pending.resolve(payload);
  }

  private rejectSkillRequest(
    requestId: string | undefined,
    status: number,
    code: string,
    retryable: boolean,
    detail: string,
  ): void {
    if (!requestId) {
      this.rejectAllSkillRequests(status, code, retryable, detail);
      return;
    }
    const pending = this.pendingSkillRequests.get(requestId);
    if (!pending) return;
    clearTimeout(pending.timer);
    this.pendingSkillRequests.delete(requestId);
    this.removeQueuedSkillFrames(requestId);
    pending.reject(new SkillRequestError(status, code, retryable, detail));
  }

  private rejectAllSkillRequests(
    status: number,
    code: string,
    retryable: boolean,
    detail: string,
  ): void {
    for (const [requestId, pending] of this.pendingSkillRequests) {
      clearTimeout(pending.timer);
      this.removeQueuedSkillFrames(requestId);
      pending.reject(new SkillRequestError(status, code, retryable, detail));
      this.pendingSkillRequests.delete(requestId);
    }
  }

  private removeQueuedSkillFrames(requestId: string): boolean {
    let removed = false;
    for (let index = this.sendQueue.length - 1; index >= 0; index -= 1) {
      const frame = this.sendQueue[index];
      if (this.isSkillRequestFrame(frame) && frame.request_id === requestId) {
        this.sendQueue.splice(index, 1);
        removed = true;
      }
    }
    return removed;
  }

  private isSkillRequestFrame(frame: Outbound): frame is SkillRequestFrame {
    return frame.type === "skill_install"
      || frame.type === "skill_update"
      || frame.type === "skill_rollback"
      || frame.type === "skill_uninstall"
      || frame.type === "skill_set_update_policy"
      || frame.type === "skill_sync_now";
  }

  private scheduleReconnect(): void {
    this.clearRunStatusesForReconnect();
    this.setStatus("reconnecting");
    const attempt = this.reconnectAttempts++;
    // Exponential backoff: 0.5s, 1s, 2s, 4s, capped.
    const delay = Math.min(500 * 2 ** attempt, this.maxBackoffMs);
    this.reconnectTimer = setTimeout(async () => {
      this.reconnectTimer = null;
      if (this.options.onReauth) {
        try {
          const refreshed = await this.options.onReauth();
          if (refreshed) this.currentUrl = refreshed;
        } catch {
          // fall through to retry with current URL
        }
      }
      this.connect();
    }, delay);
  }

  private queueSend(frame: Outbound): void {
    if (this.socket?.readyState === WS_OPEN) {
      this.rawSend(frame);
    } else {
      this.sendQueue.push(frame);
    }
  }

  private rawSend(frame: Outbound): void {
    if (!this.socket) return;
    try {
      this.socket.send(JSON.stringify(frame));
    } catch {
      // Send failure will materialize as a close; queue the frame for retry.
      this.sendQueue.push(frame);
    }
  }
}
