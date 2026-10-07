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
    DeviceMetricSchemaTool,
    DeviceMetricSqlTool,
    ProjectSnapshotTool,
    RealtimeMetricsTool,
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
            json={"code": 0, "data": {"rowCount": 1, "rows": [{"1#6003": 78}]}},
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
        'SELECT ts, "1#6003" FROM device_metrics '
        "ORDER BY ts DESC LIMIT 1"
    )

    with request_context(context):
        result = await tool.execute(project_id="project-7", sql=sql)

    assert not isinstance(result, ToolResult) or not result.is_error
    assert json.loads(str(result))["rows"][0]["1#6003"] == 78
    assert observed_request is not None
    assert observed_request.method == "POST"
    assert observed_request.url.path == "/internal-api/agent/projects/project-7/metric-query"
    assert json.loads(observed_request.content)["sql"] == sql


@pytest.mark.asyncio
async def test_schema_search_and_realtime_selection_preserve_identifiers_and_quality() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "data": {
            "pointTableVersion": "2026-10-07-v1",
            "metrics": {"3#551": {"rawValue": 650, "value": None,
                                   "quality": "conversion_unconfirmed", "unit": "‰"}},
        }})

    configure_ssrf_whitelist(["127.0.0.0/8"])
    client = MicrogridClient("http://127.0.0.1:48080", "service-secret",
                             transport=httpx.MockTransport(handler))
    context = RequestContext(channel="api", chat_id="direct",
                             session_key="api:microgrid:42:global:conversation-9")
    with request_context(context):
        await DeviceMetricSchemaTool(client).execute(project_id="project-7", search="3#551")
        result = await RealtimeMetricsTool(client).execute(
            project_id="project-7", identifiers=["3#551", "9#0x0024"])

    assert requests[0].url.params["search"] == "3#551"
    assert requests[1].url.params.get_list("identifiers") == ["3#551", "9#0x0024"]
    assert requests[1].headers["X-Microgrid-User-Id"] == "42"
    point = json.loads(str(result))["metrics"]["3#551"]
    assert point["value"] is None
    assert point["rawValue"] == 650
    assert point["unit"] == "‰"


@pytest.mark.asyncio
async def test_invalid_point_selection_does_not_contact_backend() -> None:
    client = MicrogridClient("http://127.0.0.1:48080", "service-secret")
    context = RequestContext(channel="api", chat_id="direct",
                             session_key="api:microgrid:42:global:conversation-9")
    with request_context(context):
        result = await RealtimeMetricsTool(client).execute(project_id="project-7", identifiers=[])
    assert isinstance(result, ToolResult) and result.is_error
