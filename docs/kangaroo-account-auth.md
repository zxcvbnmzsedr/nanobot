# Kangaroo account authentication

Nanobot provides a native Kangaroo account login and can also exchange an existing Kangaroo access
token for a one-time browser handoff. Password login is handled by the nanobot gateway; the browser
never connects to `quotation-god`, and nanobot does not store the password or Kangaroo access and
refresh tokens.

## Gateway configuration

Configure the WebSocket channel in `~/.nanobot/config.json`:

```json
{
  "channels": {
    "websocket": {
      "enabled": true,
      "host": "0.0.0.0",
      "port": 8765,
      "path": "/ws",
      "kangarooAuth": {
        "enabled": true,
        "apiBase": "https://api.example.com/",
        "userInfoPath": "api/auth/userInfo",
        "loginPath": "/api/auth/login",
        "upstreamLoginPath": "api/auth/oauth/login",
        "exchangePath": "/api/auth/exchange",
        "handoffTtlS": 60,
        "requestTimeoutS": 10,
        "allowedUserIds": [],
        "runtimeRoot": "~/.nanobot/tenants"
      }
    }
  }
}
```

An empty `allowedUserIds` accepts every valid account. Set it to explicit Kangaroo user IDs
when the rollout must be restricted.

When `kangarooAuth.enabled` is true, `websocketRequiresToken` must remain true. Nanobot's legacy
gateway-key authentication (`token`, `tokenIssuePath`, and `tokenIssueSecret`) has been removed;
the only remote WebUI entry is a verified Kangaroo account.

## Native WebUI login

Open the nanobot WebUI directly. When Kangaroo authentication is enabled, localhost bootstrap is
disabled. The WebUI displays a Kangaroo account and password form, calls the same-origin nanobot
login route, and enters the verified account runtime.

The gateway's current HTTP transport accepts GET requests only. Login credentials are therefore
sent in a standard `Authorization: Basic ...` request header to `/api/auth/login`; they are never
placed in the URL. Production deployments must expose this route through HTTPS.

Nanobot applies the upstream Kangaroo login contract, including its historical password digest,
then calls `api/auth/userInfo` before creating a trusted identity. The raw password and Kangaroo
access/refresh tokens are not written to disk or returned to the browser. The browser receives only
short-lived nanobot credentials and refreshes those while the page session remains active.

## Existing-token handoff

The bearer-token exchange remains available for trusted clients that already own a Kangaroo access
token:

```http
GET /api/auth/exchange
Authorization: Bearer <kangaroo-access-token>
```

The response contains a short-lived, one-time `handoff_code`. Open the WebUI with the code in
the URL fragment so it isn't sent in the initial HTTP request or proxy logs:

```text
https://nanobot.example.com/#/new?handoff=<handoff_code>
```

The WebUI removes the code from the address bar immediately and exchanges it for identity-bound
API and WebSocket tokens. The Kangaroo access token never enters WebUI storage.

## Isolation layout

Authenticated runtimes use deterministic, hashed directory keys:

```text
<runtimeRoot>/
  users/<user-scope>/
    workspace/
      memory/MEMORY.md
    media/
    webui/
  organizations/<org-scope>/
    memory/MEMORY.md
```

The agent sees memory in this order:

1. System memory from the main nanobot workspace.
2. Organization memory selected from the server-verified `orgId`.
3. Private user memory selected from the server-verified `userId`.

The account workspace is always restricted. Account sessions cannot change it to another local
path. Conversation consolidation and history archive writes go to the user's private memory;
organization memory is read-only from ordinary account sessions.

## Security boundary

This first phase enforces tenancy inside nanobot: trusted identity, session ownership, fixed
workspace scope, private persistence paths, and account-scoped automation access. It is not an
OS-level sandbox boundary. For production workloads that run untrusted shell commands or code,
run each tenant with a Linux sandbox such as `bwrap` or an isolated container runtime.

The organization memory file is currently published by an administrator and read-only to normal
accounts. Candidate submission, reviewer authorization, approval, publishing, and audit APIs are
not part of this phase.
