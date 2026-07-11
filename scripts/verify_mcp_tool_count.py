#!/usr/bin/env python3
"""Print tools/list count from a remote mcp-hooker MCP endpoint."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

MCP_URL = os.environ.get("MCP_URL", "https://mcp-hooker.catania-service.cz/caflou/mcp")
APIKEY = os.environ.get("MCP_APIKEY", "")
BEARER = os.environ.get("MCP_BEARER", "")


def _headers(session: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if APIKEY:
        headers["apikey"] = APIKEY
    if BEARER:
        headers["Authorization"] = f"Bearer {BEARER}"
    if session:
        headers["mcp-session-id"] = session
    return headers


def _parse_body(text: str) -> dict:
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return json.loads(text)


def _post(body: dict, session: str | None = None) -> tuple[int, str | None, dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(MCP_URL, data=data, headers=_headers(session), method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        session_id = resp.headers.get("mcp-session-id")
        payload = _parse_body(resp.read().decode())
        return resp.status, session_id, payload


def main() -> int:
    if not APIKEY and not BEARER:
        print("Set MCP_APIKEY or MCP_BEARER", file=sys.stderr)
        return 2

    try:
        status, session, init = _post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "verify_mcp_tool_count", "version": "1"},
                },
            }
        )
    except urllib.error.HTTPError as exc:
        print(f"initialize failed: HTTP {exc.code}", file=sys.stderr)
        print(exc.read().decode(), file=sys.stderr)
        return 1

    if status != 200 or not session:
        print(f"initialize failed: status={status} session={session}", file=sys.stderr)
        print(json.dumps(init, indent=2), file=sys.stderr)
        return 1

    _post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, session)
    _, _, listed = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session)
    tools = listed.get("result", {}).get("tools", [])
    names = sorted(tool["name"] for tool in tools)
    print(f"tool_count={len(names)}")
    print(f"url={MCP_URL}")
    if names:
        print("sample=", ", ".join(names[:8]), "...")
    suspicious = [name for name in names if any(x in name.lower() for x in ("dashboard", "chat", "settings"))]
    if suspicious:
        print("ui_like_tools=", ", ".join(suspicious[:10]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
