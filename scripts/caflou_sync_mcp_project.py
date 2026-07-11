#!/usr/bin/env python3
"""Sync Caflou MCP project (615010) with current mcp-hooker runbook knowledge.

Requires:
  MCP_CAFOU_APIKEY  — Kong apikey or Bearer (see infra runbooks)
  MCP_CAFOU_URL     — optional, default public caflou MCP endpoint

Dry-run: CAFLOU_SYNC_DRY_RUN=1
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

MCP_URL = os.environ.get(
    "MCP_CAFOU_URL", "https://mcp-hooker.catania-service.cz/caflou/mcp"
)
APIKEY = os.environ.get("MCP_CAFOU_APIKEY", "")
BEARER = os.environ.get("MCP_CAFOU_BEARER", "")
ACCOUNT_ID = os.environ.get("CAFLOU_ACCOUNT_ID", "958a30da1d8c2ccb0a3b6194")
COMPANY_ID = int(os.environ.get("CAFLOU_COMPANY_ID", "1740315"))
KAREL_USER_ID = int(os.environ.get("CAFLOU_USER_ID", "68570"))
PROJECT_ID = int(os.environ.get("CAFLOU_MCP_PROJECT_ID", "615010"))
DRY_RUN = os.environ.get("CAFLOU_SYNC_DRY_RUN", "").lower() in {"1", "true", "yes"}
SYNC_COMMENTS = os.environ.get("CAFLOU_SYNC_COMMENTS", "").lower() in {"1", "true", "yes"}

CAFLOU_BASE = "https://app.caflou.com/catania-group-s-r-o"

# Known cross-references (id → display name for link text)
LINK_PROJECTS: dict[int, str] = {
    615009: "Voicebot interní",
    615010: "Caflou MCP",
    615380: "Infra interní",
}
LINK_TASKS: dict[int, str] = {
    2165205: "12. Budoucí hardening",
    2166269: "Audit nginx sites — dead backends",
    2166547: "Validace nginx vhostů — gophish, gp, go, wazuh",
    2167020: "17. MCP instructions — HTML rich text",
}


def _link_project(project_id: int, name: str | None = None) -> str:
    label = name or LINK_PROJECTS.get(project_id, f"Projekt {project_id}")
    return f'<a href="{CAFLOU_BASE}/projects/{project_id}">{_esc(label)}</a>'


def _link_task(task_id: int, name: str | None = None) -> str:
    label = name or LINK_TASKS.get(task_id, f"Úkol {task_id}")
    return f'<a href="{CAFLOU_BASE}/tasks/{task_id}">{_esc(label)}</a>'


def _plain_p(text: str) -> str:
    text = text.strip()
    if text.startswith("<"):
        return text
    return f"<p>{_esc(text)}</p>"


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _comment_html(text: str) -> str:
    text = text.strip()
    if text.startswith("<"):
        return text
    return f"<p>{_esc(text)}</p>"


def normalize_task_name(name: str) -> str:
    m = re.match(r"^(\d+)\.\s*(.+)$", name.strip())
    if not m:
        return name
    return f"{int(m.group(1)):02d}. {m.group(2)}"


def _desc(header: str, body: str) -> str:
    """Convert a plain-text block to Caflou HTML (paragraphs + bullet lists)."""
    parts = [f"<p><strong>{_esc(header)}</strong></p>"]
    for block in body.strip().split("\n\n"):
        block = block.strip()
        if not block or block.startswith("━━"):
            continue
        lines = [ln.strip() for ln in block.split("\n") if ln.strip() and not ln.strip().startswith("━━")]
        if not lines:
            continue
        bullet_prefixes = ("• ", "- ", "⏳ ", "✅ ")
        if all(any(ln.startswith(p) for p in bullet_prefixes) for ln in lines):
            items = []
            for ln in lines:
                for p in bullet_prefixes:
                    if ln.startswith(p):
                        items.append(_esc(ln[len(p) :]))
                        break
                else:
                    items.append(_esc(ln))
            parts.append("<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>")
        elif len(lines) == 1:
            parts.append(f"<p>{_esc(lines[0])}</p>")
        else:
            bullets = []
            prose = []
            for ln in lines:
                if ln.startswith("• "):
                    bullets.append(_esc(ln[2:]))
                elif ln.startswith("- "):
                    bullets.append(_esc(ln[2:]))
                else:
                    prose.append(_esc(ln))
            if prose:
                parts.append(f"<p>{'<br>'.join(prose)}</p>")
            if bullets:
                parts.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
    return "".join(parts)


# --- Project metadata -------------------------------------------------------

PROJECT = {
    "name": "Caflou MCP",
    "description": (
        "<p>OpenAPI → MCP bridge (mcp-hooker) pro AI agenty nad Caflou ERP/PSA.</p>"
        "<p><strong>Veřejný endpoint</strong></p>"
        "<ul>"
        "<li>MCP: https://mcp-hooker.catania-service.cz/caflou/mcp</li>"
        "<li>Auth: OAuth Bearer (Claude Web) nebo header <strong>apikey</strong> (Cursor/služby)</li>"
        "<li>Health (host): http://127.0.0.1:3003/health → tool_count: 212</li>"
        "</ul>"
        "<p><strong>Stack (jbi-sv-00)</strong></p>"
        "<p>nginx TLS → Kong :8040 → caflou-app-1:8000 (Docker)<br>"
        "Keycloak: keycloak.catania-service.cz (Claude connector)</p>"
        "<p><strong>Repo &amp; dokumentace</strong></p>"
        "<ul>"
        "<li>App: git/mcp-hooker (server.py, route_filters.py)</li>"
        "<li>Deploy: infra-files/servers/jbi-sv-00/mcp-hooker/instances/caflou/</li>"
        "<li>Runbooky: infra-files/docs/mcp-hooker/</li>"
        "<li>Maintainer: docs/RUNBOOK.md §18</li>"
        "</ul>"
        "<p><strong>Stav k 2026-07-11</strong></p>"
        "<ul>"
        "<li>✅ 477 tools v OpenAPI → patch.yaml → stabilní operationId (List_Tasks, …)</li>"
        "<li>✅ Profile A tools_filter.yaml → 212 business tools (Claude web cap)</li>"
        "<li>✅ Sanitizer paginated_lists + validate_output: false</li>"
        "<li>✅ Claude Web OAuth + dual-auth Kong (apikey + Bearer)</li>"
        "<li>✅ Overlay odstraněn — funkce v Docker image, ne bind-mount</li>"
        "<li>✅ MCP server.instructions — HTML rich text pro task/project/todo/comment</li>"
        "<li>⏳ Hardening: nullable schemas, Search_2, validate_output zpět — viz "
        f"{_link_task(2165205, '12. Budoucí hardening')}</li>"
        "</ul>"
        f"<p>Projekt: {_link_project(615010)} | Assignee: Karel Matějovský (68570)</p>"
    ),
    "planned_hours": 170,
    "progress": 94,
    "start_date": "2026-06-22",
    "end_date": "2026-09-30",
}

# --- Tasks ------------------------------------------------------------------

TASKS: list[dict] = [
    {
        "name": "0. Přehled projektu — Caflou MCP",
        "planned_hours": 4,
        "progress": 95,
        "finished": False,
        "description": (
            _desc(
                "EXECUTIVE SUMMARY (pro reporting)",
                """
Cíl: Exponovat Caflou REST API jako kurátorovanou sadu MCP nástrojů pro AI agenty
(Claude Web, Cursor, interní automatizace).

Výsledek k 11. 7. 2026:
• Produkční MCP endpoint funguje end-to-end (OAuth + apikey)
• 212 business nástrojů (Profile A) — pod limitem Claude Web (~256)
• Kompletní runbooky v infra-files + maintainer RUNBOOK v repu
• Ověřeno: List_Accounts, List_Users, Create/Update_Task, správa projektů přes MCP

Odhad práce celkem: ~170 h | Skutečně dokončeno: ~160 h | Zbývá: ~10 h (hardening)

Dokumentace pro management:
→ infra-files/docs/mcp-hooker/jbi-sv-00_caflou-mcp-tooling.md
→ infra-files/docs/mcp-hooker/jbi-sv-00_claude-web-connector-runbook.md
                """,
            )
            + f"<p>HTML rich text pro popisy — viz {_link_task(2167020)}.</p>"
            + f"<p>Související projekt: {_link_project(615009)}.</p>"
        ),
        "subtasks": [],
        "comments": [
            "2026-07-11: Sync script + všechny task popisy převedeny na HTML.",
            "2026-07-10: Overlay odstraněn, tool_count=212 potvrzeno na /health.",
        ],
    },
    {
        "name": "1. Core mcp-hooker služba",
        "planned_hours": 24,
        "progress": 100,
        "finished": True,
        "description": _desc(
            "YAML konfigurace → OpenAPI → FastMCP → streamable HTTP /mcp",
            """
Moduly: settings.py (yayaya), spec_loader.py, schema_sanitizer.py, server.py
Hot reload: POST /admin/reload, SIGHUP, volitelný interval
/health vrací tool_count po každém reloadu

Dokončeno: 2026-07-07 | Odhad: 24 h
            """,
        ),
        "subtasks": [
            ("mcp_hooker Python moduly", "settings, spec_loader, schema_sanitizer, server, route_filters.", True),
            ("HTTP endpointy", "/health, /admin/reload, streamable HTTP /mcp + CORS.", True),
        ],
        "comments": ["Hotovo — základní služba stabilní od července 2026."],
    },
    {
        "name": "2. spec_loader + OpenAPI patches",
        "planned_hours": 16,
        "progress": 100,
        "finished": True,
        "description": _desc(
            "patch_files deep-merge, List_Accounts, generate-patch.py",
            """
• patch.yaml: 477 operationId (List_Tasks, Update_Task, …)
• generate-patch.py: regenerace při změně Caflou spec
• Varování na zdvojený api.base_url (/api/v1/api/v1)

Dokončeno: 2026-07-09 | Odhad: 16 h
            """,
        ),
        "subtasks": [],
        "comments": [],
    },
    {
        "name": "3. Response schema sanitizer",
        "planned_hours": 20,
        "progress": 100,
        "finished": True,
        "description": _desc(
            "paginated_lists + replace_generic",
            """
Tři vrstvy mismatch u list endpointů (spec array vs API {page,results}).
• paginated_lists: OpenAPI 3.0 envelope bez union types
• replace_generic: List_TaskTodos $ref fix pro Cursor

Dokončeno: 2026-07-09 | Odhad: 20 h
            """,
        ),
        "subtasks": [
            ("paginated_lists implementace", "Minimal envelope page+results+additionalProperties.", True),
            ("replace_generic pro Cursor", "Nerozpoznané $ref → generic object.", True),
        ],
        "comments": ["Kritický fix — bez sanitizeru container ani nenastartoval (Pydantic union types)."],
    },
    {
        "name": "4. validate_output konfigurace",
        "planned_hours": 4,
        "progress": 100,
        "finished": True,
        "description": (
            "<p><strong>validate_output: false pro field-level schema drift</strong></p>"
            "<p>Caflou live API vrací nullable pole a extra klíče mimo spec.<br>"
            "Strict validace blokovala List_Users aj.</p>"
            f"<p>Dokončeno: 2026-07-09 | Odhad: 4 h<br>"
            f"TODO: částečně zapnout po nullable sanitizeru ({_link_task(2165205)})</p>"
        ),
        "subtasks": [],
        "comments": [],
    },
    {
        "name": "5. Docker + GitLab CI",
        "planned_hours": 12,
        "progress": 100,
        "finished": True,
        "description": _desc(
            "Image registry.gitlab.catania-service.cz/catania_dev/mcp-hooker",
            """
• Dockerfile python:3.11-slim, MCP_HOOKER_ROOT=/app
• GitLab CI: build + push :latest na main
• Od 2026-07-10: route_filters + instructions přímo v image (bez overlay)

Dokončeno: 2026-07-10 | Odhad: 12 h
            """,
        ),
        "subtasks": [],
        "comments": ["Overlay bind-mount odstraněn 2026-07-10 po ověření image."],
    },
    {
        "name": "6. Produkční deploy jbi-sv-00",
        "planned_hours": 12,
        "progress": 100,
        "finished": True,
        "description": _desc(
            "instances/caflou → caflou-app-1, port 3003",
            """
Mount: /app/instance-config/ (config.yaml, patch.yaml, tools_filter.yaml)
MCP_HOOKER_CONFIG_FILES=/app/instance-config/config.yaml
Deploy: cd instances/caflou && docker compose pull && up -d --force-recreate
Verify: curl http://127.0.0.1:3003/health | jq .tool_count  # → 212

Dokončeno: 2026-07-10 | Odhad: 12 h
            """,
        ),
        "subtasks": [
            ("Instance config bind-mount", "config + patch + tools_filter.yaml v jedné složce.", True),
            ("force-recreate po změně compose", "Remount volumes — restart nestačí.", True),
        ],
        "comments": [],
    },
    {
        "name": "7. Kong integrace (auth + routing)",
        "planned_hours": 20,
        "progress": 100,
        "finished": True,
        "description": _desc(
            "Dual-auth: OAuth Bearer + apikey na stejné URL",
            """
• Route mcp-hooker-caflou, strip_path → caflou-app-1:8000
• Fix loopback: host=caflou-app-1 (ne 127.x)
• Kong consumer + ACL per instance
• OAuth route: mcp-hooker-caflou (JWT)
• API-key route: mcp-hooker-caflou-apikey

Dokončeno: 2026-07-09 | Odhad: 20 h
            """,
        ),
        "subtasks": [
            ("Fix Kong upstream loopback", "Kong self-proxy na 127.17.0.1 — upstream na Docker DNS.", True),
            ("Dual-auth nginx routing", "Claude Bearer vs apikey header → různé Kong routes.", True),
        ],
        "comments": [],
    },
    {
        "name": "8. nginx TLS reverse proxy",
        "planned_hours": 6,
        "progress": 100,
        "finished": True,
        "description": _desc(
            "mcp-hooker.catania-service.cz",
            """
Rewrite /caflou → /mcp_hooker_caflou (Kong)
TLS na hostu, proxy na 127.0.0.1:8040

Dokončeno: 2026-07-08 | Odhad: 6 h
            """,
        ),
        "subtasks": [],
        "comments": [],
    },
    {
        "name": "9. Cursor MCP client kompatibilita",
        "planned_hours": 8,
        "progress": 100,
        "finished": True,
        "description": _desc(
            "Cursor tool discovery + mcp.json",
            """
• List_TaskTodos unresolved $ref → 0 tools v Cursoru
• replace_generic fix
• mcp.json: url + apikey header

Dokončeno: 2026-07-09 | Odhad: 8 h
            """,
        ),
        "subtasks": [],
        "comments": [],
    },
    {
        "name": "10. Dokumentace RUNBOOK.md",
        "planned_hours": 16,
        "progress": 100,
        "finished": True,
        "description": _desc(
            "Maintainer + infra runbooky",
            """
• mcp-hooker/docs/RUNBOOK.md (§18 incident playbook)
• infra-files/docs/mcp-hooker/ (Kong, Claude Web, tooling)
• instances/README.md — deploy checklist

Aktualizováno: 2026-07-10 (tools_filter, overlay removal)

Dokončeno: 2026-07-10 | Odhad: 16 h
            """,
        ),
        "subtasks": [
            ("docs/RUNBOOK.md §18", "Paginated lists, tool names, agent workflows.", True),
            ("infra-files mcp-hooker runbooky", "Kong, Claude OAuth, Caflou tooling.", True),
            ("tools_filter + Profile A docs", "477→212, generic route_filters.py.", True),
        ],
        "comments": ["Runbooky synchronizovány s produkčním stavem 2026-07-10."],
    },
    {
        "name": "11. Ověření Caflou API přes MCP agenty",
        "planned_hours": 12,
        "progress": 100,
        "finished": True,
        "description": (
            "<p><strong>End-to-end agent workflows ověřeny</strong></p>"
            "<ul>"
            "<li>List_Accounts → account_id</li>"
            "<li>List_Users → Karel 68570</li>"
            "<li>List_Tasks / Update_Task (nested task body)</li>"
            f"<li>Create_Projects / Create_Tasks — projekty {_link_project(615009)} + "
            f"{_link_project(615010)}</li>"
            f"<li>HTML rich text popisy — ověřeno {_link_task(2167020)}</li>"
            "</ul>"
            "<p>Dokončeno: 2026-07-11 | Odhad: 12 h</p>"
        ),
        "subtasks": [],
        "comments": ["2026-07-11: HTML formatting pro MCP task descriptions ověřeno v produkci."],
    },
    {
        "name": "12. Budoucí hardening",
        "planned_hours": 12,
        "progress": 20,
        "finished": False,
        "description": _desc(
            "TODO — zbývající práce (~12 h)",
            """
⏳ Nullable field sanitizer (first_name null → string|nullable)
⏳ Search_2 / globální search — patch nebo exclude (Profile A ho filtruje)
⏳ Postupné zapnutí validate_output na vybraných tazích
⏳ CI image tag pinning místo :latest v produkci
⏳ Rotace Kong apikey / Keycloak client secret audit

Odhad dokončení: 2026-08-15
            """,
        ),
        "subtasks": [
            ("Nullable schema sanitizer", "Odhad 6 h — střední priorita.", False),
            ("validate_output selective", "Odhad 4 h — po nullable fix.", False),
            ("Image tag pinning", "Odhad 2 h — ops.", False),
        ],
        "comments": [],
    },
    {
        "name": "13. Generic openapi.tools_filter (route_filters.py)",
        "planned_hours": 10,
        "progress": 100,
        "finished": True,
        "description": _desc(
            "Konfigurovatelný filtr — žádný Caflou hardcode v Pythonu",
            """
mcp_hooker/route_filters.py:
• exclude_tags, include_tags
• exclude_path_patterns, tag_path_rules
• FastMCP route_map_fn → MCPType.EXCLUDE

Generické pravidlo v config.yaml, instance-specific v tools_filter.yaml.

Dokončeno: 2026-07-10 | Odhad: 10 h
            """,
        ),
        "subtasks": [
            ("route_filters.py + tests", "tests/test_route_filters.py — 9 testů.", True),
            ("Wire do server.py", "build_route_map_fn() při create_mcp_server.", True),
        ],
        "comments": ["Commit 32aede7 Added tools filtering."],
    },
    {
        "name": "14. Caflou Profile A — tools_filter.yaml (477→212)",
        "planned_hours": 8,
        "progress": 100,
        "finished": True,
        "description": _desc(
            "Kurátorovaná sada pro Claude Web",
            """
instances/caflou/tools_filter.yaml:
• exclude_tags: Settings, Chat, Email, Uploads, BankAccounts, …
• exclude_path_patterns: /search$, kanban, dashboard, …
• tag_path_rules Users: jen 6 user endpointů

Výsledek: tool_count 212 (verify /health)
Bez filtru: 477 tools (~191 KB tools/list)

Dokončeno: 2026-07-10 | Odhad: 8 h
            """,
        ),
        "subtasks": [
            ("Profile A rules", "123 řádků exclude_tags + path patterns.", True),
            ("Produkční ověření", "curl 127.0.0.1:3003/health → 212.", True),
        ],
        "comments": ["Řeší Claude Web ~256 tool cap bez ztráty business CRUD."],
    },
    {
        "name": "15. Claude Web OAuth + Cursor IDE auth",
        "planned_hours": 24,
        "progress": 100,
        "finished": True,
        "description": _desc(
            "Keycloak + Kong JWT + MCP PRM metadata",
            """
• Keycloak client: mcp-hooker-caflou-claude
• Kong JWT plugin + pre-function (Lua)
• nginx dual-auth routing
• PRM: /.well-known/oauth-protected-resource

Bez custom login UI — standardní Claude Connect flow.

Dokončeno: 2026-07-09 | Odhad: 24 h
Runbook: jbi-sv-00_claude-web-connector-runbook.md
            """,
        ),
        "subtasks": [
            ("Keycloak connector setup", "setup-claude-connector.py", True),
            ("Kong JWT route", "setup-claude-connector.sh", True),
        ],
        "comments": [],
    },
    {
        "name": "16. Overlay removal — image-native deploy",
        "planned_hours": 4,
        "progress": 100,
        "finished": True,
        "description": _desc(
            "Cleanup: bind-mount overlay → Docker image",
            """
Problém: infra overlay přepisoval server.py v image → filtr neběžel i při enabled config.
Řešení:
• Funkce přesunuty do mcp-hooker repo (už byly)
• Odstraněny overlay volumes z docker-compose.yml
• docker compose pull + --force-recreate

Dokončeno: 2026-07-10 | Odhad: 4 h
            """,
        ),
        "subtasks": [],
        "comments": ["Po cleanup: /health tool_count=212 bez overlay mountů."],
    },
    {
        "name": "17. MCP instructions — HTML rich text",
        "planned_hours": 2,
        "progress": 100,
        "finished": True,
        "description": (
            _desc(
                "Rich text pro Caflou UI — task / project / todo / comment",
                """
Caflou UI ukládá HTML, ne plain text. MCP zápisy s \\n nebo • se v UI zobrazí jako jeden řádek.

Opraveno Jul 2026:
• server.instructions v instances/caflou/config.yaml
• Runbook jbi-sv-00_caflou-mcp-tooling.md § Rich text
• caflou_sync_mcp_project.py generuje HTML pro všechny popisy

Vzor HTML:
• Odstavec: <p>…</p>
• Seznam: <ul><li>…</li></ul>
• Tučně: <strong>…</strong>

Dokončeno: 2026-07-10 | Odhad: 2 h
                """,
            )
            + "<p>Referenční úkoly:</p><ul>"
            f"<li>{_link_task(2167020)} (tento projekt)</li>"
            f"<li>{_link_task(2166547)} ({_link_project(615380)})</li>"
            "</ul>"
        ),
        "subtasks": [
            (
                "server.instructions v config.yaml",
                _desc(
                    "MCP initialize guidance",
                    "Rich text sekce pro agenty — task/project/todo/comment fields.",
                ),
                True,
            ),
            (
                "Runbook + sync script HTML",
                _desc(
                    "Dokumentace + automatický sync",
                    "jbi-sv-00_caflou-mcp-tooling.md + caflou_sync_mcp_project.py _desc().",
                ),
                True,
            ),
        ],
        "comments": ["2026-07-10: Ověřeno Update_Task s HTML — UI renderuje odstavce a seznamy."],
    },
]


class McpClient:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.req_id = 0

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if BEARER:
            h["Authorization"] = f"Bearer {BEARER}"
        elif APIKEY:
            h["apikey"] = APIKEY
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        return h

    def post(self, payload: dict) -> dict:
        self.req_id += 1
        payload = {**payload, "jsonrpc": "2.0", "id": self.req_id}
        req = urllib.request.Request(
            MCP_URL,
            data=json.dumps(payload).encode(),
            method="POST",
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                if not self.session_id:
                    self.session_id = resp.headers.get("mcp-session-id")
                body = resp.read().decode()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode()}") from e

        m = re.search(r"data:\s*(\{.*\})\s*$", body, re.S)
        if not m:
            raise RuntimeError(f"No SSE data in response: {body[:500]}")
        data = json.loads(m.group(1))
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data.get("result", data)

    def init(self) -> None:
        self.post(
            {
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "caflou-sync", "version": "1"},
                },
            }
        )

    def call(self, name: str, arguments: dict) -> object:
        if DRY_RUN:
            print(f"  [dry-run] {name} {json.dumps(arguments, ensure_ascii=False)[:120]}...")
            return {}
        result = self.post(
            {"method": "tools/call", "params": {"name": name, "arguments": arguments}}
        )
        content = result.get("content", [])
        if not content:
            return result
        text = content[0].get("text", "")
        if not text:
            return result
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


def list_project_tasks(client: McpClient, project_id: int) -> list[dict]:
    tasks: list[dict] = []
    page = 1
    while True:
        result = client.call(
            "List_Tasks",
            {
                "account_id": ACCOUNT_ID,
                "page": page,
                "per": 100,
                # Optional: Caflou supports filter when exposed in MCP schema (see patch.yaml).
                # Client-side filter below is the reliable fallback.
                "filter": {"project_id": project_id},
            },
        )
        if isinstance(result, dict):
            batch = result.get("results", [])
            batch = [t for t in batch if t.get("project_id") == project_id]
            tasks.extend(batch)
            if not result.get("next_page"):
                break
        else:
            break
        page += 1
    return tasks


def list_subtasks(client: McpClient, task_id: int) -> list[dict]:
    result = client.call(
        "List_Tasks_To_Dos",
        {"account_id": ACCOUNT_ID, "task_id": str(task_id), "page": 1, "per": 100},
    )
    if isinstance(result, dict):
        return result.get("results", [])
    return []


def update_project(client: McpClient) -> None:
    args = {
        "account_id": ACCOUNT_ID,
        "id": str(PROJECT_ID),
        "project": {
            "name": PROJECT["name"],
            "description": PROJECT["description"],
            "planned_hours": PROJECT["planned_hours"],
            "progress": PROJECT["progress"],
            "start_date": PROJECT["start_date"],
            "end_date": PROJECT["end_date"],
        },
        "user_id": KAREL_USER_ID,
    }
    client.call("Update_Project", args)
    print(f"  updated project {PROJECT_ID}")


def upsert_task(client: McpClient, spec: dict, existing_by_name: dict[str, dict]) -> int:
    name = normalize_task_name(spec["name"])
    spec = {**spec, "name": name}
    base_args = {
        "account_id": ACCOUNT_ID,
        "name": name,
        "description": spec["description"],
        "project_id": PROJECT_ID,
        "company_id": COMPANY_ID,
        "user_id": KAREL_USER_ID,
        "target_user_id": KAREL_USER_ID,
        "progress": spec["progress"],
        "finished": spec["finished"],
        "planned_hours": spec.get("planned_hours"),
    }

    if name in existing_by_name:
        tid = existing_by_name[name]["id"]
        is_new = False
        client.call(
            "Update_Task",
            {
                "account_id": ACCOUNT_ID,
                "id": str(tid),
                "task": {
                    "name": name,
                    "description": spec["description"],
                    "progress": spec["progress"],
                    "finished": spec["finished"],
                    "planned_hours": spec.get("planned_hours"),
                    "user_id": KAREL_USER_ID,
                    "target_user_id": KAREL_USER_ID,
                },
            },
        )
        print(f"    updated task {name[:50]!r} id={tid}")
    else:
        is_new = True
        result = client.call("Create_Tasks", base_args)
        tid = result["id"] if isinstance(result, dict) else 0
        print(f"    created task {name[:50]!r} id={tid}")

    existing_subs = {s["name"]: s for s in list_subtasks(client, tid)}
    for sub_name, sub_desc, sub_done in spec.get("subtasks", []):
        if not isinstance(sub_desc, str):
            sub_desc = str(sub_desc)
        if not sub_desc.strip().startswith("<"):
            sub_desc = _plain_p(sub_desc.strip())
        if sub_name in existing_subs:
            sid = existing_subs[sub_name]["id"]
            client.call(
                "Update_ToDo",
                {
                    "account_id": ACCOUNT_ID,
                    "id": str(sid),
                    "to_do": {
                        "name": sub_name,
                        "description": sub_desc,
                        "finished": sub_done,
                    },
                },
            )
        else:
                client.call(
                    "Create_Tasks_To_Dos",
                    {
                        "account_id": ACCOUNT_ID,
                        "task_id": str(tid),
                        "name": sub_name,
                    "description": sub_desc,
                    "finished": sub_done,
                    "user_id": KAREL_USER_ID,
                },
            )

    if is_new or SYNC_COMMENTS:
        for comment_text in spec.get("comments", []):
            try:
                client.call(
                    "Create_Comments",
                    {
                        "account_id": ACCOUNT_ID,
                        "commented_type": "Task",
                        "commented_id": tid,
                        "text": _comment_html(comment_text),
                    },
                )
            except RuntimeError as exc:
                print(f"      comment skip: {exc}")

    return int(tid)


def main() -> int:
    if not APIKEY and not BEARER:
        print("Set MCP_CAFOU_APIKEY or MCP_CAFOU_BEARER", file=sys.stderr)
        return 2

    client = McpClient()
    print("Initializing MCP session...")
    client.init()

    if not DRY_RUN:
        listed = client.post({"method": "tools/list", "params": {}})
        tools = listed.get("tools", [])
        print(f"  MCP tools available: {len(tools)}")

    print(f"\nSyncing project {PROJECT_ID}...")
    update_project(client)

    existing = {normalize_task_name(t["name"]): t for t in list_project_tasks(client, PROJECT_ID)}
    print(f"  existing tasks: {len(existing)}")

    for spec in TASKS:
        upsert_task(client, spec, existing)

    final = list_project_tasks(client, PROJECT_ID)
    expected = {normalize_task_name(t["name"]) for t in TASKS}
    found = {normalize_task_name(t["name"]) for t in final}
    missing = expected - found
    done = sum(1 for t in final if t.get("finished"))
    print(f"\n=== DONE ===")
    print(f"  tasks: {len(final)} (expected {len(TASKS)})")
    print(f"  finished: {done}/{len(final)}")
    if missing:
        print(f"  MISSING: {sorted(missing)}")
        return 1
    print(f"  project URL: https://app.caflou.com/catania-group-s-r-o/projects/{PROJECT_ID}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
