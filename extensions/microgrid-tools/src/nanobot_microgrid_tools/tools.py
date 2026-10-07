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
        parameters = {
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
        if self.resource == "metric-schema":
            parameters["properties"]["search"] = {
                "type": "string", "maxLength": 100,
                "description": "Filter point definitions by identifier or name substring, e.g. SOC",
            }
        if self.resource == "metrics":
            parameters["properties"]["identifiers"] = {
                "type": "array", "minItems": 1, "maxItems": 50, "uniqueItems": True,
                "items": {"type": "string"},
                "description": "Exact identifiers from the current schema; select relevant points",
            }
        return parameters

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, project_id: str, **_kwargs: Any) -> str | ToolResult:
        try:
            context = MicrogridRequestContext.current()
            params: dict[str, Any] = {}
            if self.resource == "metric-schema" and _kwargs.get("search") is not None:
                search = _kwargs["search"]
                if not isinstance(search, str) or len(search) > 100:
                    return ToolResult.error("Point search must be a string, at most 100 characters")
                params["search"] = search
            if self.resource == "metrics" and _kwargs.get("identifiers") is not None:
                identifiers = _kwargs["identifiers"]
                if (
                    not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 50
                    or any(not isinstance(value, str) or not value.strip() for value in identifiers)
                ):
                    return ToolResult.error("Select 1 to 50 identifiers from the latest schema")
                params["identifiers"] = identifiers
            result = await self.client.get(
                self.resource, context, project_id, params=params or None,
            )
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
        "Read latest sampled points for one authorized project using the current point table. "
        "Use list_microgrid_projects and get_device_metric_schema first; select relevant "
        "identifiers to keep results small. Metrics include name, rawValue, value, unit, "
        "sampledAt, quality and freshness. Null value is unavailable or unconfirmed, never zero. "
        "Do not convert scaleHint yourself or decode status bits from name order. Stale or "
        "freshness-unknown readings cannot establish current conditions. Do not infer "
        "device roles or merge same-named points."
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
        "Get current point definitions, names, units, source rows, conversionStatus, divisors "
        "and queryable flags for one authorized project. Optional search filters names or "
        "identifiers. Refresh each turn, even if old definitions exist in conversation history. "
        "Only query fields with queryable=true; missing, undefined and reserved points do not "
        "provide business conclusions. scaleHint does not confirm a conversion. Multiple SOC "
        "or total-power points belong to distinct sources until roles are confirmed. "
        "SQL uses device_metrics, never a physical table name."
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
        "discover exact identifiers. Select explicit columns; SELECT * and undefined fields "
        "are forbidden. Results include point definitions and stored_raw values. Interpret "
        "units only with confirmed conversion rules. Calculations require confirmed "
        "engineering values (divisor=1); otherwise query "
        "raw time series. Never SUM cumulative energy or treat a power target as measured power. "
        "Include a time filter and ORDER BY ts for time-series data."
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
                        'SELECT ts, "1#6003" FROM device_metrics '
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
