"""Nanobot Tool implementations for read-only microgrid analysis."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from nanobot.agent.tools.base import Tool, ToolResult

from nanobot_microgrid_tools.client import (
    MicrogridClient,
    MicrogridContextError,
    MicrogridRequestContext,
)


class _MicrogridReadTool(Tool):
    _plugin_discoverable = False
    resource = ""

    def __init__(self, client: MicrogridClient | None = None) -> None:
        self.client = client or MicrogridClient.from_env()

    @classmethod
    def enabled(cls, _ctx: Any) -> bool:
        return bool(
            os.environ.get("MICROGRID_BACKEND_URL", "").strip()
            and os.environ.get("MICROGRID_AGENT_SERVICE_TOKEN", "").strip()
        )

    @classmethod
    def create(cls, _ctx: Any) -> Tool:
        return cls(MicrogridClient.from_env())

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Authorized project ID returned by list_microgrid_projects",
                    "minLength": 1,
                    "maxLength": 128,
                }
            },
            "required": ["project_id"],
            "additionalProperties": False,
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, project_id: str, **_kwargs: Any) -> str | ToolResult:
        try:
            context = MicrogridRequestContext.current()
            result = await self.client.get(self.resource, context, project_id)
            return json.dumps(result, ensure_ascii=False, default=str)
        except MicrogridContextError as exc:
            return ToolResult.error(f"Microgrid context error: {exc}")
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            return ToolResult.error(f"Microgrid data service returned HTTP {status}")
        except (httpx.HTTPError, ValueError) as exc:
            return ToolResult.error(f"Microgrid data unavailable: {exc}")


class AuthorizedProjectsTool(_MicrogridReadTool):
    """List every microgrid project authorized for the current user."""

    _plugin_discoverable = True
    name = "list_microgrid_projects"
    description = (
        "List all microgrid projects authorized for the current user, including project IDs, "
        "names, codes, and addresses. Call this before selecting a project for any other "
        "microgrid tool. Never invent or reuse a project ID that is not in this result."
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, **_kwargs: Any) -> str | ToolResult:
        try:
            context = MicrogridRequestContext.current()
            result = await self.client.list_projects(context)
            return json.dumps(result, ensure_ascii=False, default=str)
        except MicrogridContextError as exc:
            return ToolResult.error(f"Microgrid context error: {exc}")
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            return ToolResult.error(f"Microgrid data service returned HTTP {status}")
        except (httpx.HTTPError, ValueError) as exc:
            return ToolResult.error(f"Microgrid data unavailable: {exc}")


class ProjectSnapshotTool(_MicrogridReadTool):
    """Read one authorized project's metadata and installed energy assets."""

    _plugin_discoverable = True
    name = "get_project_snapshot"
    description = (
        "Get one authorized microgrid project's identity, location, operating mode, "
        "and installed load, photovoltaic, storage, transformer, and rated capacities. "
        "Use list_microgrid_projects first, then pass its exact project ID."
    )
    resource = "snapshot"


class RealtimeMetricsTool(_MicrogridReadTool):
    """Read the current project's latest telemetry summary."""

    _plugin_discoverable = True
    name = "get_realtime_metrics"
    description = (
        "Get one authorized microgrid project's latest telemetry, including battery "
        "SOC and power, grid/load/PV power, electrical measurements, temperatures, energy "
        "totals, and grid-connected state when available. Use this before making any claim "
        "about current operating conditions. Use list_microgrid_projects first."
    )
    resource = "metrics"


class RecentAlarmsTool(_MicrogridReadTool):
    """Read recent alarm records for the current project."""

    _plugin_discoverable = True
    name = "get_recent_alarms"
    description = (
        "Get up to 20 most recent alarm records for one authorized microgrid project. "
        "These are historical records; do not call them active or unresolved unless the data "
        "explicitly says so. Use list_microgrid_projects first."
    )
    resource = "alarms"


class DeviceMetricSchemaTool(_MicrogridReadTool):
    """List queryable metric columns for the current project's device."""

    _plugin_discoverable = True
    name = "get_device_metric_schema"
    description = (
        "Get one authorized microgrid device's queryable metric column names and SQL "
        "types. Call this before writing a device metric SQL query unless the relevant exact "
        "column names were already returned in this conversation. SQL queries must use the "
        "virtual table name device_metrics; never use a physical database table name."
    )
    resource = "metric-schema"


class DeviceMetricSqlTool(_MicrogridReadTool):
    """Execute project-scoped read-only SQL over device telemetry."""

    _plugin_discoverable = True
    name = "query_device_metrics_sql"
    description = (
        "Run a read-only SQL SELECT against one authorized project's device telemetry. "
        "Always query the virtual table device_metrics. The backend validates and binds the "
        "project and device, caps results at 200 rows, and rejects writes, joins, subqueries, "
        "parameters, unsafe functions, and other tables. Use get_device_metric_schema first to "
        "discover exact column names. Include a time filter and ORDER BY ts for time-series data."
    )
    resource = "metric-query"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": (
                        "One PostgreSQL SELECT statement over device_metrics, for example: "
                        'SELECT ts, "battery_soc" FROM device_metrics '
                        "WHERE ts >= CURRENT_TIMESTAMP - INTERVAL '1 hour' "
                        "ORDER BY ts DESC LIMIT 100"
                    ),
                    "maxLength": 4000,
                },
                "project_id": {
                    "type": "string",
                    "description": "Authorized project ID returned by list_microgrid_projects",
                    "minLength": 1,
                    "maxLength": 128,
                },
            },
            "required": ["project_id", "sql"],
            "additionalProperties": False,
        }

    async def execute(self, project_id: str, sql: str, **_kwargs: Any) -> str | ToolResult:
        if not isinstance(sql, str) or not sql.strip():
            return ToolResult.error("Device metric SQL must be a non-empty string")
        try:
            context = MicrogridRequestContext.current()
            result = await self.client.post(self.resource, context, {"sql": sql}, project_id)
            return json.dumps(result, ensure_ascii=False, default=str)
        except MicrogridContextError as exc:
            return ToolResult.error(f"Microgrid context error: {exc}")
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            return ToolResult.error(f"Microgrid data service returned HTTP {status}")
        except (httpx.HTTPError, ValueError) as exc:
            return ToolResult.error(f"Microgrid data unavailable: {exc}")
