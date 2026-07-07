from __future__ import annotations

import os
from pathlib import Path

from yayaya import contains, get, init, reload_config

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOADED = False


def project_root() -> Path:
    return _PROJECT_ROOT


def _resolve_paths() -> list[str]:
    explicit = os.environ.get("MCP_HOOKER_CONFIG_FILES", "").strip()
    if explicit:
        paths: list[Path] = []
        for raw in explicit.split(","):
            entry = raw.strip()
            if not entry:
                continue
            path = Path(entry)
            paths.append(path if path.is_absolute() else _PROJECT_ROOT / path)
        return [str(p) for p in paths]

    paths = [_PROJECT_ROOT / "config.yaml"]
    local_overlay = _PROJECT_ROOT / "config.local.yaml"
    if local_overlay.is_file():
        paths.append(local_overlay)
    return [str(p) for p in paths]


def ensure_config_loaded() -> None:
    global _LOADED
    if _LOADED:
        return
    paths = _resolve_paths()
    if not paths:
        raise FileNotFoundError("No mcp-hooker config files configured")
    if not Path(paths[0]).is_file():
        raise FileNotFoundError(f"mcp-hooker config not found: {paths[0]}")
    init(paths)
    _LOADED = True


def reload_settings() -> None:
    global _LOADED
    ensure_config_loaded()
    reload_config()
    _LOADED = True


def cfg_get(path: str, default=None, *, required: bool = False):
    ensure_config_loaded()
    return get(path, default=default, required=required)


def cfg_bool(path: str, default: bool) -> bool:
    value = cfg_get(path, default=default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return bool(value)


def cfg_optional_float(path: str) -> float | None:
    ensure_config_loaded()
    if not contains(path):
        return None
    value = get(path, default=None)
    if value is None or value == "":
        return None
    return float(value)


def cfg_optional_int(path: str) -> int | None:
    value = cfg_optional_float(path)
    if value is None:
        return None
    return int(value)


def cfg_headers() -> dict[str, str]:
    raw = cfg_get("api.headers", default={}) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}
