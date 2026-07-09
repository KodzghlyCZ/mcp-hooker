from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from mcp_hooker.settings import cfg_get, primary_config_dir, project_root
from mcp_hooker.schema_sanitizer import sanitize_openapi_spec


def _looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _parse_spec_bytes(payload: bytes, source: str) -> dict[str, Any]:
    text = payload.decode("utf-8")
    stripped = text.lstrip()
    if not stripped:
        raise ValueError(f"OpenAPI spec at {source!r} is empty")

    if stripped[0] in "{[":
        data = json.loads(text)
    else:
        data = yaml.safe_load(text)

    if not isinstance(data, dict):
        raise ValueError(f"OpenAPI spec at {source!r} must be a JSON/YAML object")
    return data


def _read_local_spec(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"OpenAPI spec file not found: {path}")
    return _parse_spec_bytes(path.read_bytes(), str(path))


def _resolve_local_path(path_str: str) -> Path:
    path = Path(path_str)
    if not path.is_absolute():
        path = project_root() / path
    return path


def _resolve_patch_path(path_str: str) -> Path:
    path = Path(path_str)
    if not path.is_absolute():
        path = primary_config_dir() / path
    return path


def _deep_merge_openapi(base: Any, patch: Any) -> Any:
    """Merge OpenAPI-like objects, replacing lists predictably."""
    if isinstance(base, dict) and isinstance(patch, dict):
        merged = dict(base)
        for key, patch_value in patch.items():
            if key in merged:
                merged[key] = _deep_merge_openapi(merged[key], patch_value)
            else:
                merged[key] = patch_value
        return merged

    # Lists are replaced rather than merged; OpenAPI list merge semantics are ambiguous.
    if isinstance(patch, list):
        return list(patch)

    return patch


def _patch_file_paths() -> list[Path]:
    raw = cfg_get("openapi.patch_files", default=[]) or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    paths: list[Path] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            continue
        paths.append(_resolve_patch_path(entry.strip()))
    return paths


def _apply_openapi_patches(spec: dict[str, Any]) -> dict[str, Any]:
    patched = spec
    for path in _patch_file_paths():
        patch = _read_local_spec(path)
        patched = _deep_merge_openapi(patched, patch)
    return patched


async def _fetch_remote_spec(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        return _parse_spec_bytes(response.content, url)


def resolve_spec_location() -> str:
    return str(cfg_get("openapi.spec", required=True))


def resolve_base_url(spec: dict[str, Any]) -> str:
    configured = cfg_get("api.base_url", default="")
    if isinstance(configured, str) and configured.strip():
        return configured.rstrip("/")

    servers = spec.get("servers")
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict):
            url = first.get("url")
            if isinstance(url, str) and url.strip():
                return url.rstrip("/")

    raise ValueError(
        "api.base_url is not set and the OpenAPI spec does not define servers[0].url"
    )


async def load_openapi_spec() -> dict[str, Any]:
    location = resolve_spec_location()
    headers = {}
    timeout = float(cfg_get("openapi.fetch_timeout", default=30.0))
    sanitizer = cfg_get("openapi.sanitizer", default={}) or {}
    if not isinstance(sanitizer, dict):
        sanitizer = {}

    if _looks_like_url(location):
        spec = await _fetch_remote_spec(location, headers=headers, timeout=timeout)
        spec = _apply_openapi_patches(spec)
        return sanitize_openapi_spec(spec, sanitizer)

    path = _resolve_local_path(location)
    spec = _read_local_spec(path)
    spec = _apply_openapi_patches(spec)
    return sanitize_openapi_spec(spec, sanitizer)
