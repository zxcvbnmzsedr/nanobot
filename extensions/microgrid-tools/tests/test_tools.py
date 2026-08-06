from __future__ import annotations

import json

import httpx
import pytest
from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.security.network import configure_ssrf_whitelist

from nanobot_microgrid_tools.client import MicrogridClient, MicrogridRequestContext
from nanobot_microgrid_tools.tools import (
    AuthorizedProjectsTool,
    DeviceMetricSqlTool,
    ProjectSnapshotTool,
)


def test_request_context_is_derived_from_trusted_session_key() -> None:
    context = RequestContext(
        channel="api",
        chat_id="direct",
        session_key="api:microgrid:42:global:conversation-9",
    )
    with request_context(context):
        resolved = MicrogridRequestContext.current()

    assert resolved.user_id == "42"


@pytest.mark.asyncio
async def test_projects_tool_calls_authorized_catalog_endpoint() -> None:
    observed_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_request
        observed_request = request
        return httpx.Response(
            200,
            json={"code": 0, "data": {"count": 1, "projects": [{"id": "project-7"}]}},
        )

    configure_ssrf_whitelist(["127.0.0.0/8"])
    client = MicrogridClient(
        "http://127.0.0.1:48080",
        "service-secret",
        transport=httpx.MockTransport(handler),
    )
    tool = AuthorizedProjectsTool(client)
    context = RequestContext(
        channel="api",
        chat_id="direct",
        session_key="api:microgrid:42:global:conversation-9",
    )

    with request_context(context):
        result = await tool.execute()

    assert not isinstance(result, ToolResult) or not result.is_error
    assert json.loads(str(result))["projects"][0]["id"] == "project-7"
    assert observed_request is not None
    assert observed_request.url.path == "/internal-api/agent/projects/catalog"
    assert observed_request.headers["X-Microgrid-User-Id"] == "42"


@pytest.mark.asyncio
async def test_snapshot_tool_calls_fixed_project_endpoint() -> None:
    observed_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_request
        observed_request = request
        return httpx.Response(
            200,
            json={"code": 0, "data": {"project": {"name": "Demo Microgrid"}}},
        )

    configure_ssrf_whitelist(["127.0.0.0/8"])
    client = MicrogridClient(
        "http://127.0.0.1:48080",
        "service-secret",
        transport=httpx.MockTransport(handler),
    )
    tool = ProjectSnapshotTool(client)
    context = RequestContext(
        channel="api",
        chat_id="direct",
        session_key="api:microgrid:42:global:conversation-9",
    )

    with request_context(context):
        result = await tool.execute(project_id="project-7")

    assert not isinstance(result, ToolResult) or not result.is_error
    assert json.loads(str(result))["project"]["name"] == "Demo Microgrid"
    assert observed_request is not None
    assert observed_request.url.path == "/internal-api/agent/projects/project-7/snapshot"
    assert observed_request.headers["X-Microgrid-User-Id"] == "42"
    assert observed_request.headers["X-Microgrid-Agent-Token"] == "service-secret"


@pytest.mark.asyncio
async def test_tool_rejects_non_microgrid_session() -> None:
    client = MicrogridClient("https://example.com", "service-secret")
    tool = ProjectSnapshotTool(client)
    context = RequestContext(channel="api", chat_id="direct", session_key="api:other")

    with request_context(context):
        result = await tool.execute(project_id="project-7")

    assert isinstance(result, ToolResult)
    assert result.is_error
    assert "context" in str(result).lower()


@pytest.mark.asyncio
async def test_metric_sql_tool_posts_sql_to_scoped_project_endpoint() -> None:
    observed_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_request
        observed_request = request
        return httpx.Response(
            200,
            json={"code": 0, "data": {"rowCount": 1, "rows": [{"battery_soc": 78}]}},
        )

    configure_ssrf_whitelist(["127.0.0.0/8"])
    client = MicrogridClient(
        "http://127.0.0.1:48080",
        "service-secret",
        transport=httpx.MockTransport(handler),
    )
    tool = DeviceMetricSqlTool(client)
    context = RequestContext(
        channel="api",
        chat_id="direct",
        session_key="api:microgrid:42:global:conversation-9",
    )
    sql = (
        'SELECT ts, "battery_soc" FROM device_metrics '
        "ORDER BY ts DESC LIMIT 1"
    )

    with request_context(context):
        result = await tool.execute(project_id="project-7", sql=sql)

    assert not isinstance(result, ToolResult) or not result.is_error
    assert json.loads(str(result))["rows"][0]["battery_soc"] == 78
    assert observed_request is not None
    assert observed_request.method == "POST"
    assert observed_request.url.path == "/internal-api/agent/projects/project-7/metric-query"
    assert json.loads(observed_request.content)["sql"] == sql
