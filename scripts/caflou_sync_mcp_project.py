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

# --- Project metadata -------------------------------------------------------

PROJECT = {
    "name": "Caflou MCP",
    "description": (
        "OpenAPI → MCP bridge (mcp-hooker) pro AI agenty nad Caflou ERP/PSA.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "VEŘEJNÝ ENDPOINT\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "• MCP: https://mcp-hooker.catania-service.cz/caflou/mcp\n"
        "• Auth: OAuth Bearer (Claude Web) NEBO header apikey (Cursor/služby)\n"
        "• Health (host): http://127.0.0.1:3003/health → tool_count: 212\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "STACK (jbi-sv-00)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "nginx TLS → Kong :8040 → caflou-app-1:8000 (Docker)\n"
        "Keycloak: keycloak.catania-service.cz (Claude connector)\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "REPO & DOKUMENTACE\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "• App: git/mcp-hooker (mcp_hooker/server.py, route_filters.py)\n"
        "• Deploy: infra-files/servers/jbi-sv-00/mcp-hooker/instances/caflou/\n"
        "• Runbooky: infra-files/docs/mcp-hooker/\n"
        "• Maintainer: docs/RUNBOOK.md §18\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "STAV K 2026-07-10\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ 477 tools v OpenAPI → patch.yaml → stabilní operationId (List_Tasks, …)\n"
        "✅ Profile A tools_filter.yaml → 212 business tools (Claude web cap)\n"
        "✅ Sanitizer paginated_lists + validate_output: false\n"
        "✅ Claude Web OAuth + dual-auth Kong (apikey + Bearer)\n"
        "✅ Overlay odstraněn — funkce v Docker image, ne bind-mount\n"
        "⏳ Hardening: nullable schemas, Search_2, validate_output zpět\n\n"
        "Caflou project ID: 615010 | Assignee: Karel Matějovský (68570)"
    ),
    "planned_hours": 168,
    "progress": 93,
    "start_date": "2026-06-22",
    "end_date": "2026-09-30",
}

# --- Tasks ------------------------------------------------------------------

def _desc(header: str, body: str) -> str:
    return f"{header}\n\n{body.strip()}"


TASKS: list[dict] = [
    {
        "name": "0. Přehled projektu — Caflou MCP",
        "planned_hours": 4,
        "progress": 95,
        "finished": False,
        "description": _desc(
            "EXECUTIVE SUMMARY (pro reporting)",
            """
Cíl: Exponovat Caflou REST API jako kurátorovanou sadu MCP nástrojů pro AI agenty
(Claude Web, Cursor, interní automatizace).

Výsledek k 10. 7. 2026:
• Produkční MCP endpoint funguje end-to-end (OAuth + apikey)
• 212 business nástrojů (Profile A) — pod limitem Claude Web (~256)
• Kompletní runbooky v infra-files + maintainer RUNBOOK v repu
• Ověřeno: List_Accounts, List_Users, Create/Update_Task, správa projektů přes MCP

Odhad práce celkem: ~168 h | Skutečně dokončeno: ~156 h | Zbývá: ~12 h (hardening)

Dokumentace pro management:
→ infra-files/docs/mcp-hooker/jbi-sv-00_caflou-mcp-tooling.md
→ infra-files/docs/mcp-hooker/jbi-sv-00_claude-web-connector-runbook.md
            """,
        ),
        "subtasks": [],
        "comments": [
            "2026-07-10: Overlay odstraněn, tool_count=212 potvrzeno na /health.",
            "2026-07-09: Claude Web OAuth connector nasazen (Keycloak + Kong JWT).",
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
        "description": _desc(
            "validate_output: false pro field-level schema drift",
            """
Caflou live API vrací nullable pole a extra klíče mimo spec.
Strict validace blokovala List_Users aj.

Dokončeno: 2026-07-09 | Odhad: 4 h
TODO: částečně zapnout po nullable sanitizeru (úkol 12)
            """,
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
        "description": _desc(
            "End-to-end agent workflows ověřeny",
            """
• List_Accounts → account_id
• List_Users → Karel 68570
• List_Tasks / Update_Task (nested task body)
• Create_Projects / Create_Tasks — tento projekt v Caflou vytvořen přes MCP
• Projekty Voicebot 615009 + Caflou MCP 615010

Dokončeno: 2026-07-09 | Odhad: 12 h
            """,
        ),
        "subtasks": [],
        "comments": ["Projekt 615010 synchronizován skriptem caflou_sync_mcp_project.py."],
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
        "name": "15. Claude Web OAuth connector",
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
                "filter": {"project_id": project_id},
            },
        )
        if isinstance(result, dict):
            batch = result.get("results", [])
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
        {"account_id": ACCOUNT_ID, "task_id__path": str(task_id), "page": 1, "per": 100},
    )
    if isinstance(result, dict):
        return result.get("results", [])
    return []


def update_project(client: McpClient) -> None:
    args = {
        "account_id": ACCOUNT_ID,
        "id": str(PROJECT_ID),
        **PROJECT,
        "user_id": KAREL_USER_ID,
    }
    client.call("Update_Project", args)
    print(f"  updated project {PROJECT_ID}")


def upsert_task(client: McpClient, spec: dict, existing_by_name: dict[str, dict]) -> int:
    name = spec["name"]
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
        result = client.call("Create_Tasks", base_args)
        tid = result["id"] if isinstance(result, dict) else 0
        print(f"    created task {name[:50]!r} id={tid}")

    existing_subs = {s["name"]: s for s in list_subtasks(client, tid)}
    for sub_name, sub_desc, sub_done in spec.get("subtasks", []):
        if sub_name in existing_subs:
            sid = existing_subs[sub_name]["id"]
            client.call(
                "Update_Tasks_To_Do",
                {
                    "account_id": ACCOUNT_ID,
                    "task_id__path": str(tid),
                    "id": str(sid),
                    "name": sub_name,
                    "description": sub_desc,
                    "finished": sub_done,
                },
            )
        else:
            client.call(
                "Create_Tasks_To_Dos",
                {
                    "account_id": ACCOUNT_ID,
                    "task_id__path": str(tid),
                    "name": sub_name,
                    "description": sub_desc,
                    "finished": sub_done,
                    "user_id": KAREL_USER_ID,
                },
            )

    for comment_text in spec.get("comments", []):
        try:
            client.call(
                "Create_Comments",
                {
                    "account_id": ACCOUNT_ID,
                    "commentable_type": "Task",
                    "commentable_id": tid,
                    "text": comment_text,
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

    existing = {t["name"]: t for t in list_project_tasks(client, PROJECT_ID)}
    print(f"  existing tasks: {len(existing)}")

    for spec in TASKS:
        upsert_task(client, spec, existing)

    final = list_project_tasks(client, PROJECT_ID)
    expected = {t["name"] for t in TASKS}
    found = {t["name"] for t in final}
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
