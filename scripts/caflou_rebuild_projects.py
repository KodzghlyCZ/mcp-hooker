#!/usr/bin/env python3
"""Rebuild Voicebot + Caflou MCP projects in Caflou via mcp-hooker MCP API."""
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
ACCOUNT_ID = "958a30da1d8c2ccb0a3b6194"
COMPANY_ID = 1740315
KAREL_USER_ID = 68570

# Old projects to delete before recreate
OLD_PROJECT_IDS = [614979, 615001]

VOICEBOT_TASKS = [
    {
        "name": "0. Přehled projektu — Voicebot interní",
        "description": (
            "PŘEHLED PROJEKTU\n\n"
            "Cíl: Interní voicebot pro CATANIA GROUP — příchozí hovory na +420 371 585 438 "
            "směrovat na ElevenLabs Conversational AI agenta.\n\n"
            "Stack: jbi-sv-00 | Asterisk 21 | OpenSIPS 3.4 | O2 voipmenu.cz | ElevenLabs\n\n"
            "Dokumentace: infra-files/docs/telephony/jbi-sv-00_o2_elevenlabs_asterisk.md\n\n"
            "Stav k 2026-06-26:\n"
            "✅ O2 registrace, příchozí hovory, AI konverzace, audio obousměrně\n"
            "✅ OpenSIPS tel:→sip: REFER rewrite, Asterisk 202 Accepted\n"
            "❌ Dokončení přenosu na operátora — O2 vrací 403\n\n"
            "Otevřené: kontaktovat O2 — povolit SIP REFER a/nebo druhý kanál"
        ),
        "progress": 85,
        "finished": False,
        "subtasks": [],
    },
    {
        "name": "1. Architektura telefonie (Phase 2)",
        "description": (
            "Finální architektura: Asterisk :5060 UDP (O2) + OpenSIPS :5061 TLS (ElevenLabs) "
            "+ Asterisk :5071 TLS. Fáze 1 (OpenSIPS front pro vše) opuštěna."
        ),
        "progress": 100,
        "finished": True,
        "subtasks": [
            ("Fáze 1: OpenSIPS full front — opuštěno", "REGISTER smyčky, identify mismatch, Route loop.", True),
            ("Fáze 2: split roles — nasazeno", "Asterisk :5060 O2 přímý, OpenSIPS :5061 jen ElevenLabs.", True),
        ],
    },
    {
        "name": "2. O2 SIP trunk (voipmenu.cz)",
        "description": "Registrace na SBC 88.103.241.62, identify, outbound RURI @voipmenu.cz.",
        "progress": 100,
        "finished": True,
        "subtasks": [
            ("Oprava registrace na SBC IP", "server_uri=sip:88.103.241.62 (ne voipmenu.cz DNS).", True),
            ("Outbound RURI @voipmenu.cz + Route na SBC", "contact=sip:voipmenu.cz + outbound_proxy na endpointu.", True),
            ("PJSIP identify pro inbound", "match 88.103.241.62 a .253.", True),
        ],
    },
    {
        "name": "3. ElevenLabs Conversational AI",
        "description": "TLS trunk sip.rtc.elevenlabs.io, auth realm LiveKit, dial přes DID.",
        "progress": 100,
        "finished": True,
        "subtasks": [
            ("ElevenLabs trunk konfigurace", "TLS trunk a routing na importované DID.", True),
            ("TLS bind a Contact port", "bind 0.0.0.0:5071, external_signaling_port=5061.", True),
        ],
    },
    {
        "name": "4. OpenSIPS REFER rewrite (tel→sip)",
        "description": "OpenSIPS :5061 TLS, sipmsgops Refer-To rewrite, relay na Asterisk :5071.",
        "progress": 100,
        "finished": True,
        "subtasks": [
            ("Root cause: Asterisk tel: REFER", "res_pjsip_refer.c odmítá tel: — 400 před dialplanem.", True),
            ("sipmsgops Refer-To rewrite", "remove_hf + append_hf s \\r\\n (ne textops subst).", True),
        ],
    },
    {
        "name": "5. NAT/RTP a audio bridge",
        "description": "Router forward 10000-20000, SIP ALG off, Asterisk NAT fix — oprava 11s drop.",
        "progress": 100,
        "finished": True,
        "subtasks": [
            ("Router NAT a port forwarding", "UDP 10000-20000, vypnout SIP ALG.", True),
            ("Asterisk NAT fix", "external_media/signaling_address, strictrtp=seqno.", True),
        ],
    },
    {
        "name": "6. Dialplan a call routing",
        "description": "from-o2 → ElevenLabs, from-elevenlabs-trunk → Transfer na O2.",
        "progress": 100,
        "finished": True,
        "subtasks": [],
    },
    {
        "name": "7. Bezpečnost — Fail2Ban SIP",
        "description": "Fail2Ban jail pro Asterisk PJSIP, backend=auto.",
        "progress": 100,
        "finished": True,
        "subtasks": [],
    },
    {
        "name": "8. Dokumentace telephony runbooky",
        "description": "infra-files/docs/telephony/ — kompletní chronologie a do-not-do list.",
        "progress": 100,
        "finished": True,
        "subtasks": [
            ("jbi-sv-00_o2_elevenlabs_asterisk.md", "Hlavní telephony runbook (~1063 řádků).", True),
            ("gist-asterisk-elevenlabs-opensips.md", "Sanitizovaná veřejná edice tel: REFER problému.", True),
        ],
    },
    {
        "name": "9. Přenos hovoru na operátora — blokováno O2",
        "description": "REFER rewrite OK, O2 vrací 403 na transfer i druhý outbound INVITE.",
        "progress": 40,
        "finished": False,
        "subtasks": [
            ("Požadavek na O2 support", "Povolit SIP REFER / blind transfer na trunku.", False),
            ("Ověřeno: Dial() nefunguje concurrent", "Druhý outbound INVITE během hovoru → 403.", True),
        ],
    },
    {
        "name": "10. Provozní monitoring",
        "description": "Rutinní kontroly trunku, OpenSIPS, RTP, Fail2Ban.",
        "progress": 25,
        "finished": False,
        "subtasks": [],
    },
]

CAFLOU_MCP_TASKS = [
    {
        "name": "0. Přehled projektu — Caflou MCP",
        "description": (
            "PŘEHLED PROJEKTU\n\n"
            "Cíl: Exponovat Caflou REST API jako MCP nástroje pro AI agenty.\n\n"
            "Endpoint: https://mcp-hooker.catania-service.cz/caflou/mcp (477 tools)\n\n"
            "Stack: mcp-hooker → Kong → nginx → Docker caflou-app-1\n\n"
            "Stav k 2026-07-09: produkce funkční, sanitizer + validate_output."
        ),
        "progress": 90,
        "finished": False,
        "subtasks": [],
    },
    {
        "name": "1. Core mcp-hooker služba",
        "description": "YAML → OpenAPI → FastMCP → streamable HTTP /mcp.",
        "progress": 100,
        "finished": True,
        "subtasks": [
            ("mcp_hooker Python moduly", "settings, spec_loader, schema_sanitizer, server.", True),
            ("HTTP endpointy", "/health, /admin/reload, /mcp.", True),
        ],
    },
    {
        "name": "2. spec_loader + OpenAPI patches",
        "description": "patch_files deep-merge, List_Accounts patch, doubled /api/v1 warning.",
        "progress": 100,
        "finished": True,
        "subtasks": [],
    },
    {
        "name": "3. Response schema sanitizer",
        "description": "paginated_lists + replace_generic pro Cursor.",
        "progress": 100,
        "finished": True,
        "subtasks": [
            ("paginated_lists implementace", "Minimal OpenAPI 3.0 envelope, žádné union types.", True),
            ("replace_generic pro Cursor", "List_TaskTodos $ref fix.", True),
        ],
    },
    {
        "name": "4. validate_output konfigurace",
        "description": "validate_output: false pro field-level schema drift.",
        "progress": 100,
        "finished": True,
        "subtasks": [],
    },
    {
        "name": "5. Docker + GitLab CI",
        "description": "Dockerfile, compose, GitLab CI registry push.",
        "progress": 100,
        "finished": True,
        "subtasks": [],
    },
    {
        "name": "6. Produkční deploy jbi-sv-00",
        "description": "Docker instance caflou, caflou-app-1:8000, instance config mount.",
        "progress": 100,
        "finished": True,
        "subtasks": [],
    },
    {
        "name": "7. Kong integrace (auth + routing)",
        "description": "key-auth + ACL, upstream caflou-app-1 (ne loopback).",
        "progress": 100,
        "finished": True,
        "subtasks": [
            ("Fix Kong upstream loopback", "host=caflou-app-1 místo 127.x self-proxy.", True),
            ("Kong consumer credentials", "key-auth + ACL per instance.", True),
        ],
    },
    {
        "name": "8. nginx TLS reverse proxy",
        "description": "mcp-hooker.catania-service.cz, rewrite /caflou → Kong.",
        "progress": 100,
        "finished": True,
        "subtasks": [],
    },
    {
        "name": "9. Cursor MCP client kompatibilita",
        "description": "List_TaskTodos $ref — Cursor 0 tools fix.",
        "progress": 100,
        "finished": True,
        "subtasks": [],
    },
    {
        "name": "10. Dokumentace RUNBOOK.md",
        "description": "docs/RUNBOOK.md + infra-files mcp-hooker runbook.",
        "progress": 100,
        "finished": True,
        "subtasks": [
            ("docs/RUNBOOK.md", "Kompletní maintainer runbook včetně §18.", True),
            ("Kong/nginx deploy runbook", "infra-files/docs/mcp-hooker/jbi-sv-00_mcp-hooker-kong-caflou.md", True),
        ],
    },
    {
        "name": "11. Ověření Caflou API přes MCP agenty",
        "description": "List_21, Create_14, Create_13 ověřeno; tento projekt vytvořen přes MCP.",
        "progress": 100,
        "finished": True,
        "subtasks": [],
    },
    {
        "name": "12. Budoucí hardening",
        "description": "Nullable sanitizer, re-enable validate_output, Search_2 patch.",
        "progress": 15,
        "finished": False,
        "subtasks": [],
    },
]


class McpClient:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.req_id = 0

    def _headers(self) -> dict[str, str]:
        h = {
            "apikey": APIKEY,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
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
                    "clientInfo": {"name": "caflou-rebuild", "version": "1"},
                },
            }
        )
        # notifications/initialized is optional for some MCP servers; skip if rejected

    def call(self, name: str, arguments: dict) -> object:
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


def delete_project(client: McpClient, project_id: int) -> None:
    try:
        client.call("Delete_12", {"account_id": ACCOUNT_ID, "id": str(project_id)})
        print(f"  deleted project {project_id}")
    except RuntimeError as e:
        print(f"  skip delete project {project_id}: {e}")


def create_project(client: McpClient, name: str, description: str) -> int:
    result = client.call(
        "Create_13",
        {
            "account_id": ACCOUNT_ID,
            "name": name,
            "description": description,
            "company_id": COMPANY_ID,
            "user_id": KAREL_USER_ID,
        },
    )
    pid = result["id"]
    print(f"  created project {name!r} id={pid}")
    return pid


def create_task(
    client: McpClient,
    project_id: int,
    spec: dict,
) -> int:
    args = {
        "account_id": ACCOUNT_ID,
        "name": spec["name"],
        "description": spec["description"],
        "project_id": project_id,
        "company_id": COMPANY_ID,
        "user_id": KAREL_USER_ID,
        "target_user_id": KAREL_USER_ID,
        "progress": spec["progress"],
        "finished": spec["finished"],
    }
    result = client.call("Create_14", args)
    tid = result["id"]
    uid = result.get("user_id")
    if uid != KAREL_USER_ID:
        client.call(
            "Update_14",
            {
                "account_id": ACCOUNT_ID,
                "id": str(tid),
                "name": spec["name"],
                "user_id": KAREL_USER_ID,
                "target_user_id": KAREL_USER_ID,
            },
        )
    for sub_name, sub_desc, sub_done in spec.get("subtasks", []):
        client.call(
            "Create_17",
            {
                "account_id": ACCOUNT_ID,
                "task_id__path": str(tid),
                "name": sub_name,
                "description": sub_desc,
                "finished": sub_done,
                "user_id": KAREL_USER_ID,
            },
        )
    return tid


def list_project_tasks(client: McpClient, project_id: int) -> list[dict]:
    tasks = []
    page = 1
    while True:
        result = client.call(
            "List_15", {"account_id": ACCOUNT_ID, "page": page, "per": 100}
        )
        batch = [t for t in result.get("results", []) if t.get("project_id") == project_id]
        tasks.extend(batch)
        if not result.get("next_page"):
            break
        page += 1
    return tasks


def validate_project(client: McpClient, project_id: int, expected_names: list[str]) -> bool:
    tasks = list_project_tasks(client, project_id)
    found = {t["name"] for t in tasks}
    expected = set(expected_names)
    missing = expected - found
    extra = found - expected
    unassigned = [t for t in tasks if t.get("user_id") != KAREL_USER_ID]
    print(f"\n  project {project_id}: {len(tasks)} tasks (expected {len(expected_names)})")
    if missing:
        print(f"  MISSING ({len(missing)}): {sorted(missing)}")
    if extra:
        print(f"  EXTRA ({len(extra)}): {sorted(extra)}")
    if unassigned:
        print(f"  UNASSIGNED ({len(unassigned)}): {[t['name'] for t in unassigned]}")
    else:
        print(f"  all {len(tasks)} tasks assigned to user_id={KAREL_USER_ID}")
    return not missing and not unassigned


def main() -> int:
    if not APIKEY:
        print("Set MCP_CAFOU_APIKEY env var (Kong apikey)", file=sys.stderr)
        return 2
    client = McpClient()
    print("Initializing MCP session...")
    client.init()

    print("\nDeleting old projects...")
    for pid in OLD_PROJECT_IDS:
        delete_project(client, pid)

    print("\nCreating Voicebot interní...")
    vb_id = create_project(
        client,
        "Voicebot interní",
        "Interní voicebot O2 + Asterisk + ElevenLabs + OpenSIPS na jbi-sv-00.",
    )
    vb_task_ids = []
    for spec in VOICEBOT_TASKS:
        tid = create_task(client, vb_id, spec)
        vb_task_ids.append(tid)
        print(f"    task {spec['name'][:50]} id={tid}")

    print("\nCreating Caflou MCP...")
    mcp_id = create_project(
        client,
        "Caflou MCP",
        "MCP bridge mcp-hooker — Caflou OpenAPI → 477 MCP tools pro AI agenty.",
    )
    mcp_task_ids = []
    for spec in CAFLOU_MCP_TASKS:
        tid = create_task(client, mcp_id, spec)
        mcp_task_ids.append(tid)
        print(f"    task {spec['name'][:50]} id={tid}")

    print("\n=== VALIDATION ===")
    vb_ok = validate_project(client, vb_id, [t["name"] for t in VOICEBOT_TASKS])
    mcp_ok = validate_project(client, mcp_id, [t["name"] for t in CAFLOU_MCP_TASKS])

    summary = {
        "voicebot_project_id": vb_id,
        "caflou_mcp_project_id": mcp_id,
        "voicebot_task_ids": vb_task_ids,
        "caflou_mcp_task_ids": mcp_task_ids,
        "assigned_to_user_id": KAREL_USER_ID,
        "validation_ok": vb_ok and mcp_ok,
    }
    print("\n" + json.dumps(summary, indent=2))
    return 0 if summary["validation_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
