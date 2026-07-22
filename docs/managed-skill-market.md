# Managed Skill marketplace

The managed Skill marketplace lets a Kangaroo organization browse, install, update, pin, roll back,
and remove signed instruction-only Skills. Platform or organization policy can require, disable, or
remove a Skill. The control plane owns desired state; nanobot verifies and activates an immutable
local snapshot for each organization.

Managed Skills do not install code or dependencies. A release may contain only:

```text
SKILL.md
references/**/*.md
references/**/*.txt
manifest.json
signature.ed25519
```

`SKILL.md` frontmatter may contain only `name` and `description`. The model cannot install, update,
or remove a Skill. It receives a summary and can read the turn-pinned content only through the
read-only `read_skill` tool.

## Deployment order

Keep both feature flags off until every prerequisite is ready.

1. Apply the `ai_skill`, `ai_skill_version`, `ai_skill_subscription`, `ai_skill_assignment`,
   `ai_skill_revision`, and `ai_skill_sync_audit` migration from
   `kangaroo-sql/branch/develop_10.0.0/kangaroo-answer.sql`.
2. Generate an Ed25519 key pair. Put the private key only in the control-plane secret store and
   distribute the raw public key to nanobot through a separate trusted configuration channel.
3. Deploy `kangaroo-ai-agent-v2` with the market disabled and confirm its existing database schema
   checks still pass.
4. Configure every nanobot instance with the API URL and pinned public key, but leave
   `skillMarketEnabled` false.
5. Enable the control plane, exercise catalog and manifest reads with a test organization, then
   enable one canary nanobot instance.
6. Publish and install a harmless canary Skill. Verify the local snapshot, sync audit, WebUI update
   event, restart recovery, and offline last-known-good behavior.
7. Roll out the nanobot flag by organization. Keep rollback access to the previous application
   versions and retain all public keys needed by published Skill releases.

The backend feature flag defaults to false, and nanobot rejects an enabled marketplace without an
absolute HTTP(S) API URL and at least one pinned public key.

## Generate signing keys

The backend expects a base64-encoded raw 32-byte Ed25519 private key. Nanobot accepts the matching
raw 32-byte public key as base64, hex, or PEM. This example prints values for a secret manager and
client configuration; do not store the private value in source control or shell history:

```python
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

private_key = Ed25519PrivateKey.generate()
private_raw = private_key.private_bytes(
    serialization.Encoding.Raw,
    serialization.PrivateFormat.Raw,
    serialization.NoEncryption(),
)
public_raw = private_key.public_key().public_bytes(
    serialization.Encoding.Raw,
    serialization.PublicFormat.Raw,
)
print("private:", base64.b64encode(private_raw).decode("ascii"))
print("public:", base64.b64encode(public_raw).decode("ascii"))
```

Use a stable, non-secret key ID such as `skill-prod-2026-01`.

## Control-plane configuration

Configure `kangaroo-ai-agent-v2` after the migration is present:

```dotenv
NANOBOT_DATABASE_ENABLED=true
NANOBOT_SKILL_MARKET_ENABLED=true
NANOBOT_SKILL_SIGNING_KEY_ID=skill-prod-2026-01
NANOBOT_SKILL_SIGNING_PRIVATE_KEY=<base64-raw-private-key>
NANOBOT_SKILL_POLL_AFTER_SECONDS=300
NANOBOT_SKILL_MANIFEST_TTL_SECONDS=86400
```

The service fails requests closed when the repository or signer is unavailable. Artifact bodies and
sync report bodies are excluded from access logs.

## Nanobot configuration

The market uses the same verified Kangaroo identity and encrypted refreshable credential as the
model and memory services:

```json
{
  "channels": {
    "websocket": {
      "enabled": true,
      "websocketRequiresToken": true,
      "kangarooAuth": {
        "enabled": true,
        "apiBase": "https://api.example.com",
        "llmProxyUrl": "https://agent.example.com/nanobot/llm/stream",
        "memoryApiUrl": "https://agent.example.com",
        "skillMarketEnabled": true,
        "skillMarketApiUrl": "https://agent.example.com",
        "skillMarketPollIntervalS": 300,
        "skillMarketPublicKeys": {
          "skill-prod-2026-01": "<base64-raw-public-key>"
        },
        "runtimeRoot": "/var/lib/nanobot/tenants"
      }
    }
  }
}
```

`skillMarketApiUrl` is an origin without a path. When it is omitted, nanobot uses the `apiBase`
origin. Prefer an explicit origin when model, memory, and market traffic have different routing. The
runtime directory must be durable and private to the nanobot service account.

## Publish a Skill

All API calls use `Authorization: Bearer <kangaroo-access-token>`. Creating a Skill and publishing a
release require a platform administrator account.

Create the catalog entry:

```http
POST /nanobot/admin/skills
Content-Type: application/json

{
  "skillKey": "parts-lookup",
  "displayName": "Parts lookup",
  "summary": "Standard lookup workflow for parts staff",
  "description": "Approved organization workflow",
  "category": "operations",
  "tags": ["parts", "lookup"]
}
```

Publish an immutable release. The server validates the paths and frontmatter, creates the canonical
manifest and restricted ZIP, signs the manifest, and stores the artifact:

```http
POST /nanobot/admin/skill-releases
Content-Type: application/json

{
  "skillKey": "parts-lookup",
  "version": "1.0.0",
  "changelog": "Initial approved workflow",
  "minRuntimeVersion": "0.1.0",
  "stable": true,
  "files": {
    "SKILL.md": "---\nname: parts-lookup\ndescription: Standard parts lookup workflow\n---\n\n# Workflow\n...",
    "references/checklist.md": "# Checklist\n..."
  }
}
```

A published version cannot be overwritten. Publish a new semantic version for every change.

## Assign policy

Policy precedence is platform assignment, then organization assignment, then the organization's
self-service subscription. `require` installs the selected or latest stable release and prevents
uninstall; `disable` removes it from the active snapshot and blocks self-service management;
`remove` forces it out of the active snapshot.

Create a platform-wide required assignment:

```http
POST /nanobot/admin/skill-assignments
Content-Type: application/json

{
  "skillKey": "parts-lookup",
  "action": "require",
  "target": "platform",
  "expectedRevision": 0,
  "reason": "Approved standard workflow"
}
```

A platform administrator can target one organization with `"target": "org"` and
`"targetOrgId": "<org-id>"`. An organization main account can create an organization assignment
only for its own verified organization. To pin a required release, include `"version": "1.0.0"`.

Assignment changes use compare-and-swap revisions. Revoke the current assignment with its key and
revision:

```http
DELETE /nanobot/admin/skill-assignments/<assignment-key>?expectedRevision=1&reason=retired
```

## Self-service update policies

Organization main accounts manage eligible Skills from **Settings > Skills** in the WebUI:

| Policy | Behavior |
| --- | --- |
| `manual` | Keep the selected release until an administrator clicks Update or Roll back. |
| `notify` | Keep the selected release and surface that a newer stable release is available. |
| `auto_stable` | Follow the control plane's current stable release automatically. |
| `pinned` | Keep an exact version until the pin changes. |

Install, update, policy, rollback, and uninstall writes use authenticated WebSocket operations and a
subscription `rowVersion` compare-and-swap value. Reads use same-origin authenticated HTTP routes.
The WebUI distinguishes remote desired state from the locally activated snapshot.

## Revoke a release

Use emergency revocation when content must never execute again:

```http
POST /nanobot/admin/skill-releases/<release-id>/revoke
Content-Type: application/json

{"reason": "Security review failed"}
```

Revoked digests are included in the signed manifest and persisted in each organization's local deny
list before artifact work or snapshot activation. A revoked release is blocked even for a turn that
was pinned before the next snapshot became active. Use an assignment or subscription change for a
temporary disable; it does not add the release digest to the permanent revocation list.

## Runtime and recovery semantics

- The manifest is bound to `org:<verified-org-id>`, signed with Ed25519, time-bounded, and identified
  by monotonic global and organization generations. Nanobot rejects another organization's manifest,
  expired data, reused generations, and generation rollback.
- Nanobot uses `ETag`/`If-None-Match`; it periodically forces a full signed response and also does so
  before cached validity expires. A `304` without a recoverable local snapshot triggers a full fetch.
- Every artifact is downloaded from a same-origin path and checked for archive size, expanded size,
  file count, per-file size, compression ratio, duplicate paths, links, traversal, digest, manifest,
  signature, runtime compatibility, and built-in Skill name collision.
- Releases are immutable. Nanobot stages and validates all missing releases before atomically moving
  `CURRENT` to the new organization snapshot. `PREVIOUS` is retained for crash recovery.
- Each agent turn pins one snapshot. Subagents inherit it. A later update becomes visible on the next
  turn, except emergency revocation, which fails closed immediately.
- If the control plane is unavailable, the last validated local snapshot keeps working. Inventory and
  status expose remote availability, last success, manifest expiry, and last-known-good staleness.
- Sync audit delivery is best effort and idempotent by `eventId`; audit failure never blocks a valid
  activation. Audit rows contain provenance and bounded errors, not Skill bodies or credentials.

## Key rotation

1. Add the new public key ID to every nanobot instance while retaining the old key.
2. Wait until the client configuration rollout is complete.
3. Switch the backend signing key ID and private key.
4. Publish new releases with the new key and verify one canary organization.
5. Retain the old public key while any desired or rollback-eligible release uses it.
6. Revoke or retire those releases, complete the rollout, then remove the old public key.

Never deliver a replacement public key in the manifest it is supposed to authenticate.

## Operational checks

For one canary organization, verify all of the following before broad rollout:

- catalog and detail are visible only after Kangaroo authentication;
- installation creates a local `CURRENT` snapshot and an `ai_skill_sync_audit` success row;
- an update received during a running turn is visible only on the next turn;
- a bad signature, unsafe ZIP, runtime mismatch, or cross-organization manifest is quarantined or
  rejected without changing `CURRENT`;
- restart and control-plane outage continue from the same last-known-good snapshot;
- release revocation blocks `read_skill` and removes the release from the next snapshot;
- CAS conflicts return a retryable UI error instead of overwriting another administrator's change.
