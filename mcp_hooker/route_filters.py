from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml
from fastmcp.server.providers.openapi import MCPType
from fastmcp.utilities.openapi import HTTPRoute

from mcp_hooker.settings import cfg_get, primary_config_dir

logger = logging.getLogger(__name__)

_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"})


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _as_str_set(value: Any) -> set[str]:
    return set(_as_str_list(value))


def _compile_patterns(patterns: list[str], *, label: str) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for raw in patterns:
        try:
            compiled.append(re.compile(raw))
        except re.error as exc:
            raise ValueError(f"Invalid openapi.tools_filter {label} regex {raw!r}: {exc}") from exc
    return compiled


def _resolve_filter_file(path_str: str) -> Path:
    path = Path(path_str)
    if not path.is_absolute():
        path = primary_config_dir() / path
    return path


def _read_filter_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(
            f"openapi.tools_filter.file not found: {path} "
            f"(resolved relative to config directory {primary_config_dir()})"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"openapi.tools_filter.file must be a YAML mapping: {path}")
    return data


def _merge_filter_dict(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Shallow merge; overlay keys replace base keys (including lists)."""
    merged = dict(base)
    for key, value in overlay.items():
        if key in {"enabled", "file"}:
            continue
        merged[key] = value
    return merged


def tools_filter_config() -> dict[str, Any]:
    raw = cfg_get("openapi.tools_filter", default={}) or {}
    if not isinstance(raw, dict):
        return {"enabled": False}
    return raw


def tools_filter_enabled() -> bool:
    return bool(tools_filter_config().get("enabled", False))


def resolve_tools_filter_rules() -> dict[str, Any]:
    """Load inline openapi.tools_filter rules plus optional external YAML file."""
    config = tools_filter_config()
    inline = {key: value for key, value in config.items() if key not in {"enabled", "file"}}

    file_ref = config.get("file")
    if not file_ref:
        return inline

    file_rules = _read_filter_file(_resolve_filter_file(str(file_ref)))
    return _merge_filter_dict(inline, file_rules)


class ToolsFilter:
    """Config-driven OpenAPI operation filter for FastMCP route_map_fn."""

    def __init__(self, rules: dict[str, Any]) -> None:
        self.include_tags = _as_str_set(rules.get("include_tags"))
        self.exclude_tags = _as_str_set(rules.get("exclude_tags"))
        self.include_methods = {m.upper() for m in _as_str_list(rules.get("include_methods"))}
        self.exclude_methods = {m.upper() for m in _as_str_list(rules.get("exclude_methods"))}
        self.include_operation_ids = _as_str_set(rules.get("include_operation_ids"))
        self.exclude_operation_ids = _as_str_set(rules.get("exclude_operation_ids"))

        self.include_path_patterns = _compile_patterns(
            _as_str_list(rules.get("include_path_patterns")),
            label="include_path_patterns",
        )
        self.exclude_path_patterns = _compile_patterns(
            _as_str_list(rules.get("exclude_path_patterns")),
            label="exclude_path_patterns",
        )

        self.tag_path_rules = _parse_tag_path_rules(rules.get("tag_path_rules"))

    def decide(self, route: HTTPRoute) -> MCPType | None:
        operation_id = route.operation_id or ""
        method = route.method.upper()
        path = route.path
        tags = set(route.tags)

        if self.include_operation_ids and operation_id not in self.include_operation_ids:
            return MCPType.EXCLUDE
        if operation_id and operation_id in self.exclude_operation_ids:
            return MCPType.EXCLUDE

        if self.include_methods and method not in self.include_methods:
            return MCPType.EXCLUDE
        if method in self.exclude_methods:
            return MCPType.EXCLUDE

        for rule in self.tag_path_rules:
            if not (tags & rule.tags):
                continue
            if rule.keep_path_patterns:
                if any(pattern.search(path) for pattern in rule.keep_path_patterns):
                    return MCPType.TOOL
                return MCPType.EXCLUDE

        if self.include_tags and not (tags & self.include_tags):
            return MCPType.EXCLUDE
        if tags & self.exclude_tags:
            return MCPType.EXCLUDE

        if self.include_path_patterns and not any(
            pattern.search(path) for pattern in self.include_path_patterns
        ):
            return MCPType.EXCLUDE
        if any(pattern.search(path) for pattern in self.exclude_path_patterns):
            return MCPType.EXCLUDE

        return None


class _TagPathRule:
    __slots__ = ("tags", "keep_path_patterns")

    def __init__(self, tags: set[str], keep_path_patterns: list[re.Pattern[str]]) -> None:
        self.tags = tags
        self.keep_path_patterns = keep_path_patterns


def _parse_tag_path_rules(raw: Any) -> list[_TagPathRule]:
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError("openapi.tools_filter.tag_path_rules must be a list")

    rules: list[_TagPathRule] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"openapi.tools_filter.tag_path_rules[{index}] must be a mapping")
        tags = _as_str_set(entry.get("tags"))
        if not tags:
            raise ValueError(f"openapi.tools_filter.tag_path_rules[{index}] requires tags")
        keep_patterns = _compile_patterns(
            _as_str_list(entry.get("keep_path_patterns")),
            label=f"tag_path_rules[{index}].keep_path_patterns",
        )
        if not keep_patterns:
            raise ValueError(
                f"openapi.tools_filter.tag_path_rules[{index}] requires keep_path_patterns"
            )
        rules.append(_TagPathRule(tags=tags, keep_path_patterns=keep_patterns))
    return rules


def build_route_map_fn():
    """Return a FastMCP route_map_fn driven by openapi.tools_filter config."""
    if not tools_filter_enabled():
        return None

    rules = resolve_tools_filter_rules()
    if not rules:
        logger.warning(
            "openapi.tools_filter.enabled is true but no rules are configured; "
            "set exclude_tags, exclude_path_patterns, include_tags, or tools_filter.file"
        )
        return None

    tools_filter = ToolsFilter(rules)
    logger.info(
        "OpenAPI tools_filter enabled: include_tags=%s exclude_tags=%s "
        "include_paths=%s exclude_paths=%s tag_path_rules=%s",
        len(tools_filter.include_tags),
        len(tools_filter.exclude_tags),
        len(tools_filter.include_path_patterns),
        len(tools_filter.exclude_path_patterns),
        len(tools_filter.tag_path_rules),
    )

    def route_map_fn(route: HTTPRoute, mcp_type: MCPType) -> MCPType | None:
        return tools_filter.decide(route)

    return route_map_fn
