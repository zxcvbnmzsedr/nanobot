import { afterEach, describe, expect, it, vi } from "vitest";

import {
  clearSessionApiToken,
  consumeUrlHandoff,
  deriveWsUrl,
  fetchBootstrap,
  loginKangaroo,
  logoutKangaroo,
  readSessionApiToken,
  storeSessionApiToken,
} from "@/lib/bootstrap";

describe("bootstrap helpers", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it("shares the short-lived API token through localStorage", () => {
    storeSessionApiToken(" api-token ");

    expect(readSessionApiToken()).toBe("api-token");
    expect(window.localStorage.getItem("nanobot-webui.auth.api-token.v1")).toBe("api-token");
    expect(window.sessionStorage.getItem("nanobot-webui.auth.api-token.v1")).toBeNull();

    clearSessionApiToken();
    expect(readSessionApiToken()).toBe("");
  });

  it("prefers the server-provided websocket URL over the current dev host", () => {
    expect(deriveWsUrl("/", "tok en", "ws://127.0.0.1:8765/")).toBe(
      "ws://127.0.0.1:8765/?token=tok%20en",
    );
  });

  it("overrides the server-provided websocket URL when on dev server port 5173", () => {
    vi.stubGlobal("window", {
      location: {
        port: "5173",
        hostname: "192.168.1.100",
        protocol: "http:",
      },
    });
    expect(deriveWsUrl("/", "tok", "ws://127.0.0.1:8765/")).toBe(
      "ws://192.168.1.100:8765/?token=tok",
    );
  });

  it("preserves the host socket bridge URL", () => {
    expect(deriveWsUrl("/", "tok en", "nanobot-host://engine/")).toBe(
      "nanobot-host://engine/?token=tok%20en",
    );
  });

  it("falls back to the current window host for legacy bootstrap payloads", () => {
    expect(deriveWsUrl("/", "tok")).toBe(
      "ws://localhost:3000/?token=tok",
    );
  });

  it("times out when the bootstrap endpoint never responds", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));

    const pending = expect(fetchBootstrap("", 25)).rejects.toThrow(
      "Request timed out after 25ms",
    );
    await vi.advanceTimersByTimeAsync(25);

    await pending;
  });

  it("rejects bootstrap responses without an API token", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({ token: "ws-token", ws_path: "/", expires_in: 300 }),
      })),
    );

    await expect(fetchBootstrap()).rejects.toThrow("bootstrap response missing api_token");
  });

  it("consumes a one-time account handoff from the URL fragment", () => {
    window.history.replaceState(null, "", "/#/chat?handoff=nbho_code&keep=1");

    expect(consumeUrlHandoff()).toBe("nbho_code");
    expect(window.location.hash).toBe("#/chat?keep=1");
  });

  it("sends account credentials in bootstrap headers", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      token: "ws-token",
      api_token: "api-token",
      ws_path: "/ws",
      expires_in: 300,
    }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await fetchBootstrap("", undefined, "nbho_code", "old-api-token");

    expect(fetchMock).toHaveBeenCalledWith(
      "/webui/bootstrap",
      expect.objectContaining({
        headers: {
          "X-Nanobot-Handoff": "nbho_code",
          Authorization: "Bearer old-api-token",
        },
      }),
    );
  });

  it("logs in with Kangaroo credentials without putting them in the URL", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      handoff_code: "nbho_code",
      expires_in: 60,
      user: { userId: "101", orgId: "9001" },
    }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await loginKangaroo("13800138000", "secret-password");

    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/auth/login");
    expect(options).toMatchObject({ method: "GET", cache: "no-store" });
    expect(options.headers.Authorization).toMatch(/^Basic /);
    expect(String(url)).not.toContain("13800138000");
    expect(String(url)).not.toContain("secret-password");
  });

  it("logs out with the identity-bound API token", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true }), {
      status: 200,
    }));
    vi.stubGlobal("fetch", fetchMock);

    await logoutKangaroo("api-token");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/auth/logout",
      expect.objectContaining({
        method: "GET",
        headers: { Authorization: "Bearer api-token" },
      }),
    );
  });

  it("maps a rejected Kangaroo bootstrap to account authentication", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      error: "Kangaroo account authentication required",
      auth_mode: "kangaroo",
    }), { status: 401 })));

    await expect(fetchBootstrap()).rejects.toMatchObject({
      name: "BootstrapAuthRequiredError",
    });
  });
});
