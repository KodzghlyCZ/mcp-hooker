from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from mcp_hooker.settings import cfg_get, project_root


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

    if _looks_like_url(location):
        return await _fetch_remote_spec(location, headers=headers, timeout=timeout)

    path = Path(location)
    if not path.is_absolute():
        path = project_root() / path
    return _read_local_spec(path)
