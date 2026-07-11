#!/usr/bin/env python3
"""Sync Voicebot interní project (615009) with architecture docs and task status.

Requires:
  MCP_CAFOU_APIKEY  — Kong apikey header
  MCP_CAFOU_BEARER  — OAuth Bearer (alternative)

Optional:
  CAFLOU_VOICEBOT_PROJECT_ID — default 615009
  CAFLOU_SYNC_DRY_RUN=1
  CAFLOU_SYNC_COMMENTS=1  — re-post comments on existing tasks

Docs synced by this script:
  elevenlabs/docs/voicebot-architecture.md
  voicebot-core/docs/ARCHITECTURE.md
  infra-files/docs/telephony/voicebot-caflou-integration.md
  mcp-hooker/docs/voicebot-caflou-mcp.md
"""
from __future__ import annotations

import caflou_sync_mcp_project as sync

# Override target project before main()
sync.PROJECT_ID = int(__import__("os").environ.get("CAFLOU_VOICEBOT_PROJECT_ID", "615009"))

_link = sync._link_project
_task = sync._link_task
_desc = sync._desc

sync.PROJECT = {
    "name": "Voicebot interní",
    "description": (
        "<p>Interní voicebot pro CATANIA GROUP — příchozí hovory na "
        "<strong>+420 371 585 438</strong> → O2 → Asterisk → ElevenLabs Conversational AI.</p>"
        "<p><strong>Stack (jbi-sv-00)</strong></p>"
        "<ul>"
        "<li>Telefonie: Asterisk 21, OpenSIPS 3.4, O2 voipmenu.cz</li>"
        "<li>AI: ElevenLabs (agent Test: <code>agent_2301k95k4p2je9sscxsh5gjpbmdb</code>)</li>"
        "<li>CRM integrace (plánováno): voicebot-core → Caflou REST</li>"
        "</ul>"
        "<p><strong>Dokumentace (2026-07-11)</strong></p>"
        "<ul>"
        "<li>Telefonie: infra-files/docs/telephony/jbi-sv-00_o2_elevenlabs_asterisk.md</li>"
        "<li>Caflou integrace: infra-files/docs/telephony/voicebot-caflou-integration.md</li>"
        "<li>ElevenLabs agenti: elevenlabs/docs/voicebot-architecture.md</li>"
        "<li>voicebot-core API: voicebot-core/docs/ARCHITECTURE.md</li>"
        "<li>mcp-hooker voice MCP: mcp-hooker/docs/voicebot-caflou-mcp.md</li>"
        "</ul>"
        "<p><strong>Stav k 2026-07-11</strong></p>"
        "<ul>"
        "<li>✅ O2 registrace, příchozí hovory, AI konverzace, audio obousměrně</li>"
        "<li>✅ OpenSIPS tel:→sip: REFER rewrite</li>"
        "<li>✅ Test agent workflow + end_call (script update_test_agent.py)</li>"
        "<li>✅ Architektura CRM callback logging zdokumentována</li>"
        "<li>✅ Přenos na operátora — O2 403 vyřešeno (2026-07)</li>"
        "<li>⏳ voicebot-core POST /log-callback — neimplementováno</li>"
        "<li>⏳ ElevenLabs webhook / thin MCP pro Caflou — neimplementováno</li>"
        "</ul>"
        f"<p>Související: {_link(615010, 'Caflou MCP')}</p>"
    ),
    "planned_hours": 120,
    "progress": 78,
    "start_date": "2026-06-01",
    "end_date": "2026-10-31",
}

sync.TASKS = [
    {
        "name": "00. Přehled projektu — Voicebot interní",
        "planned_hours": 4,
        "progress": 85,
        "finished": False,
        "description": (
            _desc(
                "EXECUTIVE SUMMARY",
                """
Cíl: Příchozí hovory na +420 371 585 438 směrovat na ElevenLabs AI agenta.
Plánované rozšíření: automatické logování zájmu o produkty/služby do Caflou včetně telefonu volajícího.

Stack: jbi-sv-00 | Asterisk | OpenSIPS | O2 | ElevenLabs | voicebot-core (plán)

Dokumentace:
• infra-files/docs/telephony/jbi-sv-00_o2_elevenlabs_asterisk.md
• infra-files/docs/telephony/voicebot-caflou-integration.md
• elevenlabs/docs/voicebot-architecture.md
                """,
            )
            + f"<p>Související MCP projekt: {_link(615010)}.</p>"
        ),
        "subtasks": [],
        "comments": ["2026-07-11: CRM callback architektura + sync script."],
    },
    {
        "name": "01. Architektura telefonie (Phase 2)",
        "planned_hours": 16,
        "progress": 100,
        "finished": True,
        "description": _desc(
            "Finální architektura",
            """
Asterisk :5060 UDP (O2) + OpenSIPS :5061 TLS (ElevenLabs) + Asterisk :5071 TLS.
Fáze 1 (OpenSIPS front pro vše) opuštěna.
            """,
        ),
        "subtasks": [
            ("Fáze 1: OpenSIPS full front — opuštěno", sync._plain_p("REGISTER smyčky, identify mismatch."), True),
            ("Fáze 2: split roles — nasazeno", sync._plain_p("Asterisk :5060 O2 přímý, OpenSIPS :5061 jen ElevenLabs."), True),
        ],
        "comments": [],
    },
    {
        "name": "02. O2 SIP trunk (voipmenu.cz)",
        "planned_hours": 12,
        "progress": 100,
        "finished": True,
        "description": sync._plain_p("Registrace na SBC 88.103.241.62, identify, outbound RURI @voipmenu.cz."),
        "subtasks": [
            ("Oprava registrace na SBC IP", sync._plain_p("server_uri=sip:88.103.241.62"), True),
            ("Outbound RURI @voipmenu.cz", sync._plain_p("contact + outbound_proxy na endpointu."), True),
        ],
        "comments": [],
    },
    {
        "name": "03. ElevenLabs Conversational AI",
        "planned_hours": 8,
        "progress": 100,
        "finished": True,
        "description": sync._plain_p("TLS trunk sip.rtc.elevenlabs.io, auth realm LiveKit, dial přes DID."),
        "subtasks": [
            ("ElevenLabs trunk konfigurace", sync._plain_p("TLS trunk a routing na importované DID."), True),
            ("TLS bind a Contact port", sync._plain_p("bind 0.0.0.0:5071, external_signaling_port=5061."), True),
        ],
        "comments": [],
    },
    {
        "name": "04. OpenSIPS REFER rewrite (tel→sip)",
        "planned_hours": 12,
        "progress": 100,
        "finished": True,
        "description": sync._plain_p("OpenSIPS :5061 TLS, sipmsgops Refer-To rewrite, relay na Asterisk :5071."),
        "subtasks": [
            ("Root cause: Asterisk tel: REFER", sync._plain_p("res_pjsip_refer.c odmítá tel: — 400."), True),
            ("sipmsgops Refer-To rewrite", sync._plain_p("remove_hf + append_hf s \\r\\n."), True),
        ],
        "comments": [],
    },
    {
        "name": "05. NAT/RTP a audio bridge",
        "planned_hours": 8,
        "progress": 100,
        "finished": True,
        "description": sync._plain_p("Router forward 10000-20000, SIP ALG off, Asterisk NAT fix — oprava 11s drop."),
        "subtasks": [],
        "comments": [],
    },
    {
        "name": "06. Dialplan a call routing",
        "planned_hours": 4,
        "progress": 100,
        "finished": True,
        "description": sync._plain_p("from-o2 → ElevenLabs, from-elevenlabs-trunk → Transfer na O2."),
        "subtasks": [],
        "comments": [],
    },
    {
        "name": "07. Bezpečnost — Fail2Ban SIP",
        "planned_hours": 4,
        "progress": 100,
        "finished": True,
        "description": sync._plain_p("Fail2Ban jail pro Asterisk PJSIP, backend=auto."),
        "subtasks": [],
        "comments": [],
    },
    {
        "name": "08. Dokumentace telephony runbooky",
        "planned_hours": 8,
        "progress": 100,
        "finished": True,
        "description": (
            "<p>infra-files/docs/telephony/ — kompletní chronologie a do-not-do list.</p>"
            "<ul>"
            "<li>jbi-sv-00_o2_elevenlabs_asterisk.md</li>"
            "<li>voicebot-caflou-integration.md (2026-07-11)</li>"
            "<li>gist-asterisk-elevenlabs-opensips.md</li>"
            "</ul>"
        ),
        "subtasks": [
            ("Hlavní telephony runbook", sync._plain_p("jbi-sv-00_o2_elevenlabs_asterisk.md"), True),
            ("Voicebot ↔ Caflou integrace", sync._plain_p("voicebot-caflou-integration.md"), True),
        ],
        "comments": [],
    },
    {
        "name": "09. Přenos hovoru na operátora",
        "planned_hours": 8,
        "progress": 100,
        "finished": True,
        "description": _desc(
            "Stav",
            """
SIP REFER přepojení na operátora/mobil funguje end-to-end.

Dříve (2026-06): O2 vracelo 403 na REFER i druhý outbound INVITE během aktivního hovoru.
Vyřešeno 2026-07 — provisioning u O2 / operátora.

Dokumentace: infra-files/docs/telephony/jbi-sv-00_o2_elevenlabs_asterisk.md (Current status)
            """,
        ),
        "subtasks": [
            ("O2 403 na REFER — vyřešeno", sync._plain_p("Transfer na cílové číslo funguje (2026-07)."), True),
            ("OpenSIPS tel:→sip: rewrite", sync._plain_p("Předpoklad pro Asterisk 202 Accepted."), True),
        ],
        "comments": ["2026-07-11: O2 403 vyřešeno — task uzavřen."],
    },
    {
        "name": "10. Provozní monitoring",
        "planned_hours": 8,
        "progress": 25,
        "finished": False,
        "description": sync._plain_p("Rutinní kontroly trunku, OpenSIPS, RTP, Fail2Ban."),
        "subtasks": [],
        "comments": [],
    },
    {
        "name": "11. ElevenLabs Test agent — workflow a end_call",
        "planned_hours": 6,
        "progress": 90,
        "finished": False,
        "description": _desc(
            "Test agent (inbound line)",
            """
Script: elevenlabs/scripts/update_test_agent.py
Agent ID: agent_2301k95k4p2je9sscxsh5gjpbmdb

Česká testovací linka — workflow nodes:
start → Přivítání → Menu → Technický test → Rozloučení → end

Povinný end_call system tool — bez něj SIP leg zůstane otevřený.
Asterisk: rtp_timeout=15 v pjsip-elevenlabs.conf.example

Dokumentace: elevenlabs/docs/voicebot-architecture.md
            """,
        ),
        "subtasks": [
            ("update_test_agent.py", sync._plain_p("Workflow + end_call konfigurace v repu."), True),
            ("Ověřit live agent vs script", sync._plain_p("First message a workflow na produkci."), False),
        ],
        "comments": ["2026-07-11: Architektura agenta zdokumentována."],
    },
    {
        "name": "12. CRM callback — architektura (Caflou logging)",
        "planned_hours": 8,
        "progress": 30,
        "finished": False,
        "description": _desc(
            "Cíl",
            """
Při dotazu na produkt/službu zalogovat zájem + telefon volajícího do Caflou pro zpětné volání.

Rozhodnutí 2026-07-11:
• ElevenLabs podporuje MCP (SSE/HTTP streamable)
• Voice agent volá JEDNU kurátorovanou akci — webhook nebo thin MCP
• NE plný Caflou MCP (~212 tools): latence, bezpečnost, prompt bloat
• Telefon: system__caller_id (ElevenLabs dynamic variable)
• mcp-hooker patch = OpenAPI proxy only — ne composite logika
• Interest capture workflow node — krátký prompt, ne Caflou API docs

Možnosti:
A) Webhook → voicebot-core (doporučeno)
B) Thin MCP (1 tool) → voicebot-core via mcp-hooker voicebot instance
C) Prototyp: caflou-voice instance (4 allowlisted Caflou tools)

Dokumentace:
→ infra-files/docs/telephony/voicebot-caflou-integration.md
→ elevenlabs/docs/voicebot-architecture.md
→ mcp-hooker/docs/voicebot-caflou-mcp.md
            """,
        ),
        "subtasks": [
            ("Architektura zdokumentována", sync._plain_p("MCP vs webhook vs patch limity."), True),
            ("ElevenLabs webhook / thin MCP", sync._plain_p("log_callback_request tool na agentovi."), False),
            ("Interest capture workflow node", sync._plain_p("override_agent node v ElevenLabs workflow."), False),
            ("Volitelně caflou-voice MCP", sync._plain_p("Filtered mcp-hooker instance pro prototyp."), False),
        ],
        "comments": ["2026-07-11: Kompletní architektonická analýza."],
    },
    {
        "name": "13. voicebot-core — log-callback API",
        "planned_hours": 16,
        "progress": 5,
        "finished": False,
        "description": _desc(
            "Plánované API",
            """
Repo: voicebot-core (zatím stub — pouze /health)

POST /log-callback:
• topic, notes, caller_name (od agenta)
• caller_phone ← system__caller_id
• conversation_id ← system__conversation_id

Backend flow:
1. List_Contacts — dedup podle telefonu
2. Create_Contact nebo update
3. Create_Tasks na projekt 615009
4. Create_Comments — HTML (<p>, <ul>)

Konstanty v backend config (ne v agent promptu):
account_id 958a30da1d8c2ccb0a3b6194, company_id 1740315, assignee 68570

Dokumentace: voicebot-core/docs/ARCHITECTURE.md
            """,
        ),
        "subtasks": [
            ("Caflou client + HTML builders", sync._plain_p("Vzor: caflou_sync_mcp_project.py"), False),
            ("POST /log-callback", sync._plain_p("FastAPI route + validace."), False),
            ("Deploy jbi-sv-00 + Kong", sync._plain_p("URL TBD"), False),
            ("ElevenLabs webhook tool napojení", sync._plain_p("Po deployi voicebot-core."), False),
        ],
        "comments": [],
    },
]


if __name__ == "__main__":
    raise SystemExit(sync.main())
