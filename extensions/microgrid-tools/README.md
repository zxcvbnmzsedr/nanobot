# Nanobot Microgrid Tools

This package exposes six read-only Nanobot tools backed by the Java service:

- `list_microgrid_projects`
- `get_project_snapshot`
- `get_realtime_metrics`
- `get_recent_alarms`
- `get_device_metric_schema`
- `query_device_metrics_sql`

Install it into the same environment as Nanobot:

```bash
pip install -e ./extensions/microgrid-tools
```

Runtime configuration:

```bash
export MICROGRID_BACKEND_URL=http://127.0.0.1:48080
export MICROGRID_AGENT_SERVICE_TOKEN=replace-with-a-long-random-value
```

The Docker image installs this extension automatically. The repository's
`docker-compose.yml` starts `nanobot-api` with the fail-closed
`deploy/microgrid-api-config.json` template and reads all credentials from the
environment:

```bash
export MICROGRID_AGENT_NANOBOT_API_KEY=replace-with-an-api-password
export MICROGRID_AGENT_SERVICE_TOKEN=replace-with-the-same-value-as-the-java-service
export MICROGRID_LLM_API_KEY=replace-with-the-model-provider-key
export MICROGRID_LLM_API_BASE=https://www.shiyitopo.tech/v1
export MICROGRID_AGENT_MODEL=gpt-5.6-terra
docker compose up --build nanobot-api
```

Do not put those secret values in `docker-compose.yml` or the JSON template.
Set `MICROGRID_AGENT_NANOBOT_API_KEY`, `MICROGRID_AGENT_SERVICE_TOKEN`, and
`MICROGRID_AGENT_MODEL` to the same values used by the Java service. By default,
the container reaches a Java service running on the Docker host at
`http://host.docker.internal:48080`; override `MICROGRID_BACKEND_URL` when both
services share a Docker network or the Java service runs elsewhere.

The default private CIDRs cover Docker's usual Linux and Docker Desktop host
gateways. In production, override `MICROGRID_BACKEND_PRIMARY_CIDR` and
`MICROGRID_BACKEND_SECONDARY_CIDR` with the narrowest ranges containing the Java
backend. These values only pass Nanobot's SSRF validation; the plugin still calls
the single fixed `MICROGRID_BACKEND_URL` and authenticates every request with the
service token.

Configure Nanobot's API authentication and private-backend allowlist in
`~/.nanobot/config.json`. The same API key is configured in the Java service as
`MICROGRID_AGENT_NANOBOT_API_KEY`:

```json
{
  "api": {
    "host": "127.0.0.1",
    "port": 8900,
    "apiKey": "${MICROGRID_AGENT_NANOBOT_API_KEY}",
    "allowCommands": false,
    "requireToolAllowlist": true,
    "toolAllowlist": [
      "list_microgrid_projects",
      "get_project_snapshot",
      "get_realtime_metrics",
      "get_recent_alarms",
      "get_device_metric_schema",
      "query_device_metrics_sql"
    ]
  },
  "tools": {
    "ssrfWhitelist": ["127.0.0.0/8"]
  }
}
```

`api.allowCommands: false` prevents API input such as `/restart`, `/trigger`, and
`/dream-restore` from reaching Nanobot's command router. `api.requireToolAllowlist`
makes the microgrid-facing API fail closed when the allowlist is empty.
`api.toolAllowlist` is the server-side tool execution boundary for both
`/v1/responses` and `/v1/chat/completions`; Nanobot also refuses to start if any
configured tool is missing. Keep all three settings on every microgrid deployment.

Use the narrowest CIDR containing the Java service in non-local environments. Nanobot's
SSRF guard rejects private addresses unless this allowlist explicitly permits them.

The Java gateway creates user-scoped session IDs in the form
`microgrid:<userId>:global:<conversationId>`. The project catalog is limited to projects
authorized for that user. Project data tools accept a project ID returned by that catalog,
and the Java backend validates project access again on every request.

Device metric SQL uses the virtual table `device_metrics`. The Java backend parses each query,
rewrites that virtual table to the selected authorized project's physical telemetry table, adds the
device filter, enforces a 200-row limit and 10-second statement timeout, and rejects writes, joins,
subqueries, comments, parameters, unsafe functions, and access to any other table.

Example:

```sql
SELECT ts, "battery_soc"
FROM device_metrics
WHERE ts >= CURRENT_TIMESTAMP - INTERVAL '1 hour'
ORDER BY ts DESC
LIMIT 100
```

This package contains no shell, file-write, command preparation, or device execution tool.
Natural-language control must be added later as a Java-validated, user-confirmed workflow; it
must not be implemented as a direct Nanobot execution tool.
