# Kangaroo account authentication

Nanobot provides a native Kangaroo account login and can also exchange an existing Kangaroo access
token for a one-time browser handoff. Password login is handled by the nanobot gateway; the browser
never connects to `quotation-god`. Nanobot never stores passwords. It encrypts the current access and
refresh tokens in the active instance's runtime directory so unattended tasks survive restarts.

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
        "llmProxyUrl": "https://agent.example.com/nanobot/llm/stream",
        "userInfoPath": "api/auth/userInfo",
        "loginPath": "/api/auth/login",
        "logoutPath": "/api/auth/logout",
        "upstreamLoginPath": "api/auth/oauth/login",
        "upstreamRefreshPath": "api/auth/oauth/token",
        "exchangePath": "/api/auth/exchange",
        "handoffTtlS": 60,
        "requestTimeoutS": 10,
        "refreshSkewS": 300,
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

Open the nanobot WebUI directly. When Kangaroo authentication is enabled and the encrypted vault has
no usable instance identity, the WebUI displays a Kangaroo account and password form, calls the
same-origin nanobot login route, and enters the verified account runtime. Once native login has stored
a refreshable identity, a local browser can recover it after a gateway restart or an expired browser
transport token. Remote and LAN callers still require a verified account handoff or API token.

The gateway's current HTTP transport accepts GET requests only. Login credentials are therefore
sent in a standard `Authorization: Basic ...` request header to `/api/auth/login`; they are never
placed in the URL. Production deployments must expose this route through HTTPS.

Nanobot applies the upstream Kangaroo login contract, including its historical password digest,
then calls `api/auth/userInfo` before creating a trusted identity. The raw password and Kangaroo
tokens are never returned to the browser. The access and refresh tokens are stored in an encrypted,
permission-restricted vault at `<runtimeRoot>/.kangaroo-credentials.enc`; its generated key file is
permission-restricted as well. Set `NANOBOT_KANGAROO_CREDENTIAL_KEY` to a Fernet key when deployment
policy requires the encryption key to come from a secret manager instead of a local key file.

At gateway startup and every minute while it is running, nanobot refreshes the bound instance
credential when either its access or refresh token expires within `refreshSkewS`. Model and memory
requests perform the same check, and concurrent requests for the same user share one refresh
operation. If the model proxy rejects a token with 401, nanobot refreshes and retries the complete
request once. A transient account-service failure keeps the encrypted credential for the next
attempt; an explicit 401 or 403 from the refresh endpoint clears it and requires login. The browser's
short-lived `nbwt_*` API and WebSocket credentials remain an independent transport layer: localhost
bootstrap replaces them from the persisted instance identity, while remote callers cannot use this
recovery path.

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

An access-token-only handoff cannot be refreshed because it does not provide a refresh token. Use
native WebUI login for unattended work that must outlive the supplied access token.

## Model proxy

When Kangaroo authentication is enabled, `llmProxyUrl` is required. Nanobot ignores legacy provider
API keys and endpoints for chat turns and sends each request to this URL with the current user's
Kangaroo access token. The proxy verifies the token again, derives `userId` and `orgId` from
`api/auth/userInfo`, and calls the server-managed model. Nanobot does not issue a separate AI
session token.

Signing out through the WebUI calls `/api/auth/logout`, removes the persisted OAuth bundle, and
revokes all outstanding nanobot browser and WebSocket grants for that identity.

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
