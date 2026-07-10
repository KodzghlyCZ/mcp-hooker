from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from mcp_hooker.settings import (
    cfg_bool,
    cfg_get,
    cfg_headers,
    cfg_optional_int,
    reload_settings,
)
from mcp_hooker.route_filters import build_route_map_fn, tools_filter_enabled
from mcp_hooker.spec_loader import load_openapi_spec, resolve_base_url, resolve_spec_location

logger = logging.getLogger(__name__)


@dataclass
class ServerState:
    mcp: FastMCP | None = None
    mcp_app: Any = None
    client: httpx.AsyncClient | None = None
    spec_source: str = ""
    base_url: str = ""
    reload_lock: asyncio.Lock | None = None
    reload_task: asyncio.Task[None] | None = None
    mcp_lifespan: AbstractAsyncContextManager[None] | None = field(default=None, repr=False)


state = ServerState()


def _server_name() -> str:
    return str(cfg_get("server.name", default="mcp-hooker"))


def _api_timeout() -> float:
    return float(cfg_get("api.timeout", default=30.0))


def _http_app_options() -> dict[str, Any]:
    allowed_hosts = cfg_get("server.allowed_hosts", default=[])
    if not isinstance(allowed_hosts, list):
        allowed_hosts = []
    return {
        "host_origin_protection": cfg_bool("server.host_origin_protection", default=False),
        "allowed_hosts": [str(host) for host in allowed_hosts],
    }


async def _stop_mcp_lifespan() -> None:
    if state.mcp_lifespan is None:
        return
    await state.mcp_lifespan.__aexit__(None, None, None)
    state.mcp_lifespan = None


async def _start_mcp_lifespan() -> None:
    if state.mcp_app is None:
        raise RuntimeError("MCP HTTP app is not initialized")
    if state.mcp_lifespan is not None:
        await _stop_mcp_lifespan()
    state.mcp_lifespan = state.mcp_app.router.lifespan_context(state.mcp_app)
    await state.mcp_lifespan.__aenter__()


def _warn_on_duplicate_base_path(base_url: str, spec: dict[str, Any]) -> None:
    """Warn when api.base_url repeats a path prefix already in every spec path.

    OpenAPI operation paths are joined onto the httpx client base_url. If the
    base_url already carries a path segment (e.g. ``/api/v1``) that every
    operation path also starts with, requests get a doubled prefix
    (``/api/v1/api/v1/...``) and 404. base_url should usually be host-only.
    """
    from urllib.parse import urlparse

    base_path = urlparse(base_url).path.strip("/")
    if not base_path:
        return

    paths = spec.get("paths")
    if not isinstance(paths, dict) or not paths:
        return

    prefix = f"/{base_path}/"
    if all(isinstance(p, str) and p.startswith(prefix) for p in paths):
        logger.warning(
            "api.base_url %r ends with '/%s', which every OpenAPI path already "
            "includes; upstream requests will double it (e.g. /%s/%s/...) and 404. "
            "Set api.base_url to the host only (e.g. %s://%s).",
            base_url,
            base_path,
            base_path,
            base_path,
            urlparse(base_url).scheme,
            urlparse(base_url).netloc,
        )


async def create_mcp_server() -> tuple[FastMCP, httpx.AsyncClient, dict[str, Any]]:
    spec = await load_openapi_spec()
    base_url = resolve_base_url(spec)
    _warn_on_duplicate_base_path(base_url, spec)
    client = httpx.AsyncClient(
        base_url=base_url,
        headers=cfg_headers(),
        timeout=_api_timeout(),
        follow_redirects=True,
    )
    validate_output = cfg_bool("openapi.validate_output", default=True)
    if not validate_output:
        logger.warning(
            "openapi.validate_output is false; FastMCP will not strictly validate tool outputs"
        )
    openapi_kwargs: dict[str, Any] = {
        "openapi_spec": spec,
        "client": client,
        "name": _server_name(),
        "validate_output": validate_output,
    }
    route_map_fn = build_route_map_fn()
    if route_map_fn is not None:
        openapi_kwargs["route_map_fn"] = route_map_fn
    elif tools_filter_enabled():
        logger.warning("openapi.tools_filter.enabled is true but no route_map_fn was built")
    mcp = FastMCP.from_openapi(**openapi_kwargs)
    return mcp, client, {"spec": spec, "base_url": base_url}


async def reload_server(*, reason: str = "manual", manage_lifespan: bool = True) -> dict[str, Any]:
    if state.reload_lock is None:
        state.reload_lock = asyncio.Lock()

    async with state.reload_lock:
        reload_settings()

        if manage_lifespan:
            await _stop_mcp_lifespan()

        if state.client is not None:
            await state.client.aclose()
            state.client = None

        mcp, client, meta = await create_mcp_server()
        state.mcp = mcp
        state.client = client
        state.mcp_app = mcp.http_app(**_http_app_options())
        state.spec_source = resolve_spec_location()
        state.base_url = meta["base_url"]

        if manage_lifespan:
            await _start_mcp_lifespan()

        info = await mcp.list_tools()
        tool_count = len(info)
        logger.info(
            "Reloaded MCP server (%s): spec=%s base_url=%s tools=%s",
            reason,
            state.spec_source,
            state.base_url,
            tool_count,
        )
        return {
            "status": "reloaded",
            "reason": reason,
            "spec": state.spec_source,
            "base_url": state.base_url,
            "tool_count": tool_count,
        }


async def _health(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "spec": state.spec_source,
            "base_url": state.base_url,
        }
    )


async def _reload(request: Request) -> JSONResponse:
    token = cfg_get("reload.token", default="")
    if token:
        auth = request.headers.get("authorization", "")
        expected = f"Bearer {token}"
        if auth != expected:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        payload = await reload_server(reason="http")
    except Exception as exc:
        logger.exception("Reload failed")
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(payload)


class _MCPASGIMiddleware:
    """Delegate ASGI traffic to the current FastMCP HTTP app."""

    async def __call__(self, scope, receive, send):
        if state.mcp_app is None:
            response = JSONResponse({"error": "MCP server not initialized"}, status_code=503)
            await response(scope, receive, send)
            return
        await state.mcp_app(scope, receive, send)


async def _reload_loop(stop_event: asyncio.Event) -> None:
    interval = cfg_optional_int("reload.interval_seconds")
    if not interval or interval <= 0:
        return

    logger.info("Automatic OpenAPI reload enabled every %s seconds", interval)
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break
        except TimeoutError:
            pass

        if stop_event.is_set():
            break

        try:
            await reload_server(reason="interval")
        except Exception:
            logger.exception("Periodic reload failed")


def _register_sighup() -> None:
    if not cfg_bool("reload.on_sighup", default=True):
        return

    loop = asyncio.get_running_loop()

    def _handle() -> None:
        logger.info("Received SIGHUP, scheduling reload")
        loop.create_task(reload_server(reason="sighup"))

    try:
        loop.add_signal_handler(signal.SIGHUP, _handle)
    except (NotImplementedError, RuntimeError):
        signal.signal(signal.SIGHUP, lambda _signum, _frame: _handle())


@asynccontextmanager
async def lifespan(_app: Starlette):
    state.reload_lock = asyncio.Lock()
    await reload_server(reason="startup")

    stop_event = asyncio.Event()
    state.reload_task = asyncio.create_task(_reload_loop(stop_event))
    _register_sighup()

    try:
        yield
    finally:
        stop_event.set()
        if state.reload_task is not None:
            state.reload_task.cancel()
            try:
                await state.reload_task
            except asyncio.CancelledError:
                pass
        await _stop_mcp_lifespan()
        if state.client is not None:
            await state.client.aclose()
            state.client = None


app = Starlette(
    routes=[
        Route("/health", _health, methods=["GET"]),
        Route("/admin/reload", _reload, methods=["POST"]),
        Mount("/", app=_MCPASGIMiddleware()),
    ],
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
    expose_headers=["mcp-session-id", "mcp-protocol-version", "last-event-id"],
)


def main() -> None:
    import uvicorn

    from mcp_hooker.settings import ensure_config_loaded

    logging.basicConfig(level=logging.INFO)
    ensure_config_loaded()
    host = str(cfg_get("server.host", default="0.0.0.0"))
    port = int(cfg_get("server.port", default=8000))
    uvicorn.run(
        "mcp_hooker.server:app",
        host=host,
        port=port,
        factory=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
