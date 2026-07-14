import type { BootstrapResponse, KangarooLoginResponse } from "./types";
import { fetchWithTimeout } from "./http";

const URL_HANDOFF_PARAM = "handoff";

export class BootstrapAuthRequiredError extends Error {
  constructor(message = "Kangaroo account authentication required") {
    super(message);
    this.name = "BootstrapAuthRequiredError";
  }
}

function basicAuthorization(username: string, password: string): string {
  const bytes = new TextEncoder().encode(`${username}:${password}`);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return `Basic ${window.btoa(binary)}`;
}

export async function loginKangaroo(
  username: string,
  password: string,
  timeoutMs?: number,
): Promise<KangarooLoginResponse> {
  const res = await fetchWithTimeout("/api/auth/login", {
    method: "GET",
    cache: "no-store",
    credentials: "same-origin",
    headers: {
      Authorization: basicAuthorization(username.trim(), password),
    },
  }, timeoutMs);
  const body = await res.json().catch(() => ({})) as Partial<KangarooLoginResponse> & {
    error?: string;
  };
  if (!res.ok) {
    throw new Error(body.error?.trim() || `login failed: HTTP ${res.status}`);
  }
  if (!body.handoff_code || !body.user?.userId || !body.user?.orgId) {
    throw new Error("login response missing identity handoff");
  }
  return body as KangarooLoginResponse;
}

export function consumeUrlHandoff(): string {
  if (typeof window === "undefined") return "";
  const hash = window.location.hash || "";
  const queryStart = hash.indexOf("?");
  if (queryStart < 0) return "";

  const path = hash.slice(0, queryStart) || "#/";
  const params = new URLSearchParams(hash.slice(queryStart + 1));
  const handoff = params.get(URL_HANDOFF_PARAM)?.trim() || "";
  if (!handoff) return "";

  params.delete(URL_HANDOFF_PARAM);
  const nextQuery = params.toString();
  window.history.replaceState(
    null,
    "",
    `${window.location.pathname}${window.location.search}${path}${nextQuery ? `?${nextQuery}` : ""}`,
  );
  return handoff;
}

/**
 * Fetch a short-lived token + the WebSocket path from the gateway's
 * ``/webui/bootstrap`` endpoint.
 */
export async function fetchBootstrap(
  baseUrl: string = "",
  timeoutMs?: number,
  handoff: string = "",
  apiToken: string = "",
): Promise<BootstrapResponse> {
  const headers: Record<string, string> = {};
  if (handoff) {
    headers["X-Nanobot-Handoff"] = handoff;
  }
  if (apiToken) {
    headers.Authorization = `Bearer ${apiToken}`;
  }
  const res = await fetchWithTimeout(`${baseUrl}/webui/bootstrap`, {
    method: "GET",
    cache: "no-store",
    credentials: "same-origin",
    headers,
  }, timeoutMs);
  if (!res.ok) {
    const errorBody = await res.clone().json().catch(() => ({})) as {
      auth_mode?: string;
    };
    if (res.status === 401 && errorBody.auth_mode === "kangaroo") {
      throw new BootstrapAuthRequiredError(`bootstrap failed: HTTP ${res.status}`);
    }
    throw new Error(`bootstrap failed: HTTP ${res.status}`);
  }
  const body = (await res.json()) as BootstrapResponse;
  if (!body.token || !body.ws_path) {
    throw new Error("bootstrap response missing token or ws_path");
  }
  if (!body.api_token) {
    throw new Error("bootstrap response missing api_token");
  }
  return body;
}

/** Derive a WebSocket URL from the current window location and the server-provided path.
 *
 * Keeps the path segment exactly as the server registered it: the root ``/``
 * stays ``/`` and non-root paths are not given an extra trailing slash. This
 * matters because some WS servers dispatch handshakes based on the literal
 * path, not a normalised form.
 */
export function deriveWsUrl(
  wsPath: string,
  token: string,
  wsUrl?: string | null,
): string {
  const query = `?token=${encodeURIComponent(token)}`;
  const path = wsPath && wsPath.startsWith("/") ? wsPath : `/${wsPath || ""}`;
  if (typeof window !== "undefined" && window.location.port === "5173") {
    const host = window.location.hostname.includes(":")
      ? `[${window.location.hostname}]`
      : window.location.hostname;
    return `ws://${host}:8765${path}${query}`;
  }
  if (wsUrl && /^(wss?|nanobot-host):\/\//i.test(wsUrl)) {
    const join = wsUrl.includes("?") ? "&" : "?";
    return `${wsUrl}${join}token=${encodeURIComponent(token)}`;
  }
  if (typeof window === "undefined") {
    return `ws://127.0.0.1:8765${path}${query}`;
  }
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const host = window.location.host;
  return `${scheme}://${host}${path}${query}`;
}
