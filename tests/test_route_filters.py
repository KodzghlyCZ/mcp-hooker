from __future__ import annotations

from unittest.mock import patch

import pytest
from fastmcp.server.providers.openapi import MCPType
from fastmcp.utilities.openapi import HTTPRoute

from mcp_hooker.route_filters import ToolsFilter, build_route_map_fn


def _route(
    path: str,
    *,
    tags: list[str] | None = None,
    method: str = "GET",
    operation_id: str | None = None,
) -> HTTPRoute:
    return HTTPRoute(
        path=path,
        method=method,  # type: ignore[arg-type]
        operation_id=operation_id,
        summary=None,
        description=None,
        tags=tags or [],
        parameters=[],
        request_body=None,
        responses={},
        request_schemas={},
        response_schemas={},
        extensions={},
        openapi_version="3.0.0",
        flat_param_schema={},
        parameter_map={},
    )


SAMPLE_RULES = {
    "exclude_tags": ["Settings", "BankConnections"],
    "exclude_path_patterns": [r"/search$", r"kanban"],
    "tag_path_rules": [
        {
            "tags": ["Users"],
            "keep_path_patterns": [r"/users$", r"/users/\{id\}$"],
        }
    ],
}


@pytest.fixture
def sample_filter():
    return ToolsFilter(SAMPLE_RULES)


def test_disabled_filter_returns_none():
    with patch("mcp_hooker.route_filters.tools_filter_config", return_value={"enabled": False}):
        assert build_route_map_fn() is None


def test_enabled_without_rules_returns_none():
    with patch("mcp_hooker.route_filters.tools_filter_config", return_value={"enabled": True}):
        assert build_route_map_fn() is None


def test_excludes_tag(sample_filter):
    route = _route("/api/v1/settings/units", tags=["Settings"])
    assert sample_filter.decide(route) == MCPType.EXCLUDE


def test_keeps_unmatched_tag(sample_filter):
    route = _route("/api/v1/tasks", tags=["Tasks"], method="POST")
    assert sample_filter.decide(route) is None


def test_tag_path_rule_keep_and_drop(sample_filter):
    keep = _route("/api/v1/users", tags=["Users"])
    drop = _route("/api/v1/users/add_dashboard", tags=["Users"], method="POST")
    assert sample_filter.decide(keep) == MCPType.TOOL
    assert sample_filter.decide(drop) == MCPType.EXCLUDE


def test_excludes_path_pattern(sample_filter):
    search = _route("/api/v1/search", tags=["Index"])
    kanban = _route("/api/v1/tasks/kanban", tags=["Tasks"])
    assert sample_filter.decide(search) == MCPType.EXCLUDE
    assert sample_filter.decide(kanban) == MCPType.EXCLUDE


def test_include_tags_allowlist():
    filt = ToolsFilter({"include_tags": ["Projects", "Tasks"]})
    assert filt.decide(_route("/api/v1/tasks", tags=["Tasks"])) is None
    assert filt.decide(_route("/api/v1/invoices", tags=["Invoices"])) == MCPType.EXCLUDE


def test_exclude_operation_ids():
    filt = ToolsFilter({"exclude_operation_ids": ["purge_all"]})
    assert filt.decide(_route("/api/v1/purge", operation_id="purge_all")) == MCPType.EXCLUDE
    assert filt.decide(_route("/api/v1/items", operation_id="list_items")) is None


def test_build_route_map_fn_from_inline_rules():
    with patch(
        "mcp_hooker.route_filters.tools_filter_config",
        return_value={"enabled": True, **SAMPLE_RULES},
    ):
        fn = build_route_map_fn()
    assert fn is not None
    assert fn(_route("/api/v1/search", tags=["Index"]), MCPType.TOOL) == MCPType.EXCLUDE
