from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)

_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
_UNRESOLVED_MARKER = "x-mcp-hooker-unresolved-local-ref"


def sanitize_openapi_spec(spec: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Sanitize OpenAPI response schemas before FastMCP ingests them."""
    config = config or {}
    if not config.get("enabled", False):
        return spec

    sanitized = copy.deepcopy(spec)
    components = _component_schemas(sanitized)
    unresolved_action = str(config.get("on_unresolved", "preserve")).strip().lower()
    paginated_lists = config.get("paginated_lists", {}) or {}
    paginated_lists_enabled = bool(paginated_lists.get("enabled", False))
    paginated_items_key = str(paginated_lists.get("items_key", "results")).strip() or "results"

    for path, path_item in sanitized.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            _sanitize_operation_responses(
                operation,
                components,
                unresolved_action=unresolved_action,
                op_name=f"{method.upper()} {path}",
                paginated_lists_enabled=paginated_lists_enabled,
                paginated_items_key=paginated_items_key,
            )

    return sanitized


def _component_schemas(spec: dict[str, Any]) -> dict[str, Any]:
    components = spec.get("components")
    if not isinstance(components, dict):
        return {}
    schemas = components.get("schemas")
    return schemas if isinstance(schemas, dict) else {}


def _sanitize_operation_responses(
    operation: dict[str, Any],
    components: dict[str, Any],
    *,
    unresolved_action: str,
    op_name: str,
    paginated_lists_enabled: bool = False,
    paginated_items_key: str = "results",
) -> None:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return

    for response in responses.values():
        if not isinstance(response, dict):
            continue
        content = response.get("content")
        if not isinstance(content, dict):
            continue
        for media_type, media in content.items():
            if not isinstance(media, dict):
                continue
            schema = media.get("schema")
            if not isinstance(schema, dict):
                continue

            sanitized_schema = _inline_local_refs(schema, components, stack=())
            unresolved_refs = sorted(_collect_unresolved_refs(sanitized_schema))

            if unresolved_refs and unresolved_action == "replace_generic":
                media["schema"] = {
                    "type": "object",
                    "additionalProperties": True,
                    "description": "Response schema replaced due to unresolved local references.",
                }
                logger.warning(
                    "Replaced response schema for %s (%s) due to unresolved local refs: %s",
                    op_name,
                    media_type,
                    ", ".join(unresolved_refs),
                )
                continue

            if unresolved_refs:
                logger.warning(
                    "Preserving unresolved response schema refs for %s (%s): %s",
                    op_name,
                    media_type,
                    ", ".join(unresolved_refs),
                )

            final_schema = _strip_markers(sanitized_schema)
            if paginated_lists_enabled and _operation_has_pagination_params(operation):
                rewritten = _rewrite_paginated_list_schema(
                    final_schema,
                    items_key=paginated_items_key,
                )
                if rewritten is not None:
                    logger.debug("Rewrote paginated list response schema for %s", op_name)
                    final_schema = rewritten

            media["schema"] = final_schema


def _inline_local_refs(value: Any, components: dict[str, Any], *, stack: tuple[str, ...]) -> Any:
    if isinstance(value, list):
        return [_inline_local_refs(item, components, stack=stack) for item in value]

    if not isinstance(value, dict):
        return value

    ref = value.get("$ref")
    if isinstance(ref, str):
        component_name = _local_component_name(ref)
        if component_name is not None:
            if component_name in stack:
                return {
                    **copy.deepcopy(value),
                    _UNRESOLVED_MARKER: ref,
                }
            target = components.get(component_name)
            if isinstance(target, dict):
                merged = _merge_ref_target(
                    _inline_local_refs(target, components, stack=(*stack, component_name)),
                    value,
                )
                return _inline_local_refs(merged, components, stack=stack)

    return {
        key: _inline_local_refs(item, components, stack=stack)
        for key, item in value.items()
    }


def _local_component_name(ref: str) -> str | None:
    prefix = "#/components/schemas/"
    if ref.startswith(prefix):
        return ref[len(prefix) :]
    return None


def _merge_ref_target(target: Any, source: dict[str, Any]) -> Any:
    if not isinstance(target, dict):
        return copy.deepcopy(target)
    merged = copy.deepcopy(target)
    for key, value in source.items():
        if key == "$ref":
            continue
        merged[key] = copy.deepcopy(value)
    return merged


def _collect_unresolved_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, list):
        for item in value:
            refs.update(_collect_unresolved_refs(item))
        return refs
    if not isinstance(value, dict):
        return refs

    marker = value.get(_UNRESOLVED_MARKER)
    if isinstance(marker, str):
        refs.add(marker)

    ref = value.get("$ref")
    if isinstance(ref, str) and (
        ref.startswith("#/components/schemas/") or ref.startswith("#/$defs/")
    ):
        refs.add(ref)

    for item in value.values():
        refs.update(_collect_unresolved_refs(item))
    return refs


def _operation_has_pagination_params(operation: dict[str, Any]) -> bool:
    names: set[str] = set()
    for param in operation.get("parameters", []):
        if not isinstance(param, dict):
            continue
        if param.get("in") != "query":
            continue
        name = param.get("name")
        if isinstance(name, str):
            names.add(name)
    return "page" in names or "per" in names


def _rewrite_paginated_list_schema(
    schema: dict[str, Any],
    *,
    items_key: str = "results",
) -> dict[str, Any] | None:
    """Rewrite a bare array response schema into a paginated object envelope."""
    if schema.get("type") != "array":
        return None

    items = schema.get("items", {})
    if not isinstance(items, dict):
        items = {}

    # Keep the envelope minimal and OpenAPI 3.0-compatible. FastMCP's spec parser
    # rejects JSON Schema union types (e.g. type: ["integer", "null"]); nullable
    # page fields and extra totals are covered by additionalProperties.
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "page": {"type": "integer"},
            items_key: {
                "type": "array",
                "items": copy.deepcopy(items),
            },
        },
        "required": ["page", items_key],
    }


def _strip_markers(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_markers(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: _strip_markers(item)
        for key, item in value.items()
        if key != _UNRESOLVED_MARKER
    }
