# WebSocket Server Channel

Nanobot can act as a WebSocket server, allowing external clients (web apps, CLIs, scripts) to interact with the agent in real time via persistent connections.

## Features

- Bidirectional real-time communication over WebSocket
- Streaming support — receive agent responses token by token
- Kangaroo account authentication with identity-bound, short-lived session tokens
- Multi-chat multiplexing — one connection can run many concurrent `chat_id`s
- TLS/SSL support (WSS) with enforced TLSv1.2 minimum
- Client allow-list via `allowFrom`
- Auto-cleanup of dead connections

## Quick Start

### 1. Configure

The WebSocket channel is enabled by default. Add only the fields you want to
override under `channels.websocket`:

```json
{
  "channels": {
    "websocket": {
      "host": "127.0.0.1",
      "port": 8765,
      "path": "/",
      "websocketRequiresToken": true,
      "kangarooAuth": {
        "enabled": true,
        "apiBase": "https://accounts.example.com"
      },
      "allowFrom": ["*"],
      "streaming": true
    }
  }
}
```

### 2. Start nanobot

```bash
nanobot gateway
```

You should see:

```text
WebSocket server listening on ws://127.0.0.1:8765/
```

### 3. Connect

Open `http://127.0.0.1:8765` and sign in with a Kangaroo account. The WebUI obtains a one-time
handoff and exchanges it for identity-bound WebSocket and API tokens. See
[Kangaroo account authentication](./kangaroo-account-auth.md) for programmatic handoff details.

## Connection URL

```text
ws://{host}:{port}{path}?client_id={id}&token={token}
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `client_id` | No | Identifier for `allowFrom` authorization. Auto-generated as `anon-xxxxxxxxxxxx` if omitted. Truncated to 128 chars. |
| `token` | Conditional | One-time, short-lived nanobot token returned by authenticated bootstrap. Required when `websocketRequiresToken` is `true`. |

## Wire Protocol

All frames are JSON text. Each message has an `event` field.

### Server → Client

**`ready`** — sent immediately after connection is established:

```json
{
  "event": "ready",
  "chat_id": "uuid-v4",
  "client_id": "alice"
}
```

**`message`** — full agent response:

```json
{
  "event": "message",
  "chat_id": "uuid-v4",
  "text": "Hello! How can I help?",
  "media": ["/tmp/image.png"],
  "reply_to": "msg-id"
}
```

`media` and `reply_to` are only present when applicable.

**`delta`** — streaming text chunk (only when `streaming: true`):

```json
{
  "event": "delta",
  "chat_id": "uuid-v4",
  "text": "Hello",
  "stream_id": "s1"
}
```

**`stream_end`** — signals the end of a streaming segment:

```json
{
  "event": "stream_end",
  "chat_id": "uuid-v4",
  "stream_id": "s1"
}
```

**`reasoning_delta`** — incremental model reasoning / thinking chunk for the active assistant turn. Mirrors `delta` but targets the reasoning bubble above the answer rather than the answer body:

```json
{
  "event": "reasoning_delta",
  "chat_id": "uuid-v4",
  "text": "Let me decompose ",
  "stream_id": "r1"
}
```

**`reasoning_end`** — close marker for the active reasoning stream. WebUI uses this to lock the in-place bubble and switch from the shimmer header to a static collapsed state:

```json
{
  "event": "reasoning_end",
  "chat_id": "uuid-v4",
  "stream_id": "r1"
}
```

Reasoning frames only flow when the channel's `showReasoning` is `true` (default) and the model returns reasoning content (DeepSeek-R1 / Kimi / MiMo / OpenAI reasoning models, Anthropic extended thinking, or inline `<think>` / `<thought>` tags). Models without reasoning produce zero `reasoning_delta` frames.

**`runtime_model_updated`** — broadcast when the gateway runtime model changes, for example after `/model <preset>`:

```json
{
  "event": "runtime_model_updated",
  "model_name": "openai/gpt-4.1-mini",
  "model_preset": "fast"
}
```

`model_preset` is omitted when no named preset is active. WebUI clients use this event to keep the displayed model badge in sync across slash commands, config reloads, and settings changes.

**`attached`** — confirmation for `new_chat` / `attach` inbound envelopes (see [Multi-chat multiplexing](#multi-chat-multiplexing)):

```json
{"event": "attached", "chat_id": "uuid-v4"}
```

**`error`** — soft error for malformed inbound envelopes. The connection stays open:

```json
{"event": "error", "detail": "invalid chat_id"}
```

### Client → Server

**Legacy (default chat):** send a plain string, or a JSON object with a recognized text field:

```json
"Hello nanobot!"
```

```json
{"content": "Hello nanobot!"}
```

Recognized fields: `content`, `text`, `message` (checked in that order). Invalid JSON is treated as plain text. These frames route to the connection's default `chat_id` (the one announced in `ready`).

**Typed envelopes (multi-chat):** any JSON object with a string `type` field is a typed envelope:

| `type` | Fields | Effect |
|--------|--------|--------|
| `new_chat` | — | Server mints a new `chat_id`, subscribes this connection, replies with `attached`. |
| `attach` | `chat_id` | Subscribe to an existing `chat_id` (e.g. after a page reload). Replies with `attached`. |
| `message` | `chat_id`, `content` | Send `content` on `chat_id`. First use auto-attaches; no explicit `attach` needed. |

See [Multi-chat multiplexing](#multi-chat-multiplexing) for the full flow.

## Configuration Reference

All fields go under `channels.websocket` in `config.json`.

### Connection

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Enable the WebSocket server. Set to `false` only when you intentionally do not want the bundled WebUI/WebSocket surface. |
| `host` | string | `"127.0.0.1"` | Bind address. Use `"0.0.0.0"` to accept external connections. |
| `port` | int | `8765` | Listen port. |
| `path` | string | `"/"` | WebSocket upgrade path. Trailing slashes are normalized (root `/` is preserved). |
| `maxMessageBytes` | int | `37748736` | Maximum inbound message size in bytes (1 KB – 40 MB). Default (36 MB) is sized to accept up to 4 base64-encoded image attachments at 8 MB each; lower it if the channel only carries text. |

### Authentication

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `websocketRequiresToken` | bool | `true` | Require a valid short-lived token issued by authenticated bootstrap. Set to `false` only for same-machine development. |
| `tokenTtlS` | int | `300` | Time-to-live for issued tokens in seconds (30 – 86,400). |
| `kangarooAuth` | object | disabled | Kangaroo login, identity verification, handoff, and tenant runtime configuration. See [Kangaroo account authentication](./kangaroo-account-auth.md). |

### Access Control

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `allowFrom` | list of string | `["*"]` | Allowed `client_id` values. `"*"` allows all; `[]` denies all. |

### Streaming

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `streaming` | bool | `true` | Enable streaming mode. The agent sends `delta` + `stream_end` frames instead of a single `message`. |

### Keep-alive

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `pingIntervalS` | float | `20.0` | WebSocket ping interval in seconds (5 – 300). |
| `pingTimeoutS` | float | `20.0` | Time to wait for a pong before closing the connection (5 – 300). |

### TLS/SSL

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `sslCertfile` | string | `""` | Path to the TLS certificate file (PEM). Both `sslCertfile` and `sslKeyfile` must be set to enable WSS. |
| `sslKeyfile` | string | `""` | Path to the TLS private key file (PEM). Minimum TLS version is enforced as TLSv1.2. |

## Account Session Tokens

Nanobot no longer accepts static gateway keys and no longer exposes a general token-issue route.
The only remote issuance path starts with a verified Kangaroo identity:

1. The account logs in through `/api/auth/login`, or a trusted client submits an existing
   Kangaroo access token to `/api/auth/exchange`.
2. Nanobot returns a short-lived, single-use handoff code.
3. `/webui/bootstrap` consumes the handoff and returns separate WebSocket and REST API tokens.
4. The WebSocket token is consumed during the handshake and carries the verified `userId/orgId`.

### Limits

- Issued tokens are single-use — each token can only complete one handshake.
- Outstanding tokens are capped at 10,000. Requests beyond this return HTTP 429.
- Expired tokens are purged lazily on issuance or validation.

## Multi-chat multiplexing

A single WebSocket can carry many concurrent chats. The server tracks `chat_id -> {connections}` as a fan-out set, so the same chat can also be mirrored across multiple connections (e.g. two browser tabs).

### Typical flow (web UI with a sidebar)

```text
client                                server
  | --- connect -------------------->  |
  | <-- {"event":"ready",              |
  |      "chat_id":"d3..."}   (default)|
  |                                     |
  | --- {"type":"new_chat"} --------->  |
  | <-- {"event":"attached",            |
  |      "chat_id":"a1..."}             |
  |                                     |
  | --- {"type":"message",              |
  |      "chat_id":"a1...",             |
  |      "content":"hi"} ------------>  |
  | <-- {"event":"delta", ...}          |
  | <-- {"event":"stream_end", ...}     |
  |                                     |
  | --- {"type":"attach",               |  # after page reload
  |      "chat_id":"a1..."} --------->  |
  | <-- {"event":"attached", ...}       |
```

### Rules

- Every outbound event carries `chat_id`. Clients must dispatch by that field.
- `chat_id` format: `^[A-Za-z0-9_:-]{1,64}$`. Non-matching values return `error`.
- `message` auto-attaches on first use — no separate `attach` is required for chats the server minted (`new_chat`) on the same connection.
- Errors (invalid envelope, unknown `type`, bad `chat_id`) are soft: the server replies with `{"event":"error","detail":"..."}` and keeps the connection open.

### Backward compatibility

Legacy clients that only send plain text or `{"content": ...}` keep working unchanged: those frames route to the connection's default `chat_id` (the one from `ready`). No config flag is needed.

### Security boundary

Account runtimes namespace and validate chat IDs against the verified Kangaroo principal. A caller
cannot use another account's chat ID to attach to its conversation.

## Security Notes

- **Defense in depth**: `allowFrom` is checked at both the HTTP handshake level and the message level.
- **Tenant ownership**: account WebSocket and REST requests are bound to server-verified `userId/orgId` values.
- **TLS enforcement**: When SSL is enabled, TLSv1.2 is the minimum allowed version.
- **Default-secure**: `websocketRequiresToken` defaults to `true`. Explicitly set it to `false` only on trusted networks.

## Media Files

Outbound `message` events may include a `media` field containing local filesystem paths. Remote clients cannot access these files directly — they need either:

- A shared filesystem mount, or
- An HTTP file server serving the nanobot media directory

## Common Patterns

### Same-machine development (no account login)

```json
{
  "channels": {
    "websocket": {
      "host": "127.0.0.1",
      "port": 8765,
      "websocketRequiresToken": false,
      "allowFrom": ["*"],
      "streaming": true
    }
  }
}
```

### Public endpoint with Kangaroo accounts

```json
{
  "channels": {
    "websocket": {
      "host": "0.0.0.0",
      "port": 8765,
      "path": "/ws",
      "websocketRequiresToken": true,
      "kangarooAuth": {
        "enabled": true,
        "apiBase": "https://accounts.example.com"
      },
      "sslCertfile": "/etc/ssl/certs/server.pem",
      "sslKeyfile": "/etc/ssl/private/server-key.pem",
      "allowFrom": ["*"]
    }
  }
}
```

### Custom path

```json
{
  "channels": {
    "websocket": {
      "path": "/chat/ws",
      "allowFrom": ["*"]
    }
  }
}
```

Clients connect to `ws://127.0.0.1:8765/chat/ws?client_id=...`. Trailing slashes are normalized, so `/chat/ws/` works the same.
