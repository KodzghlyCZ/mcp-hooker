# Voicebot ↔ Caflou MCP — integration notes

> **Caflou project:** [Voicebot interní](https://app.caflou.com/catania-group-s-r-o/projects/615009) (`615009`)  
> **Last updated:** 2026-07-11

How mcp-hooker relates to the ElevenLabs voicebot and Caflou CRM callback logging feature.

---

## Two MCP audiences

| Audience | Endpoint | Tool count | Purpose |
|----------|----------|------------|---------|
| **Cursor / Claude / automation** | `https://mcp-hooker.catania-service.cz/caflou/mcp` | ~212 (Profile A) | Full business CRUD for text agents |
| **ElevenLabs voice (planned)** | `https://mcp-hooker.catania-service.cz/voicebot/mcp` (TBD) | 1–2 | Curated callback logging only |

Do **not** attach the full `caflou` MCP server to a voice agent.

ElevenLabs supports MCP (SSE / HTTP streamable) — see https://elevenlabs.io/docs/eleven-agents/customization/tools/mcp

---

## What OpenAPI patch can do

Patch files (`instances/caflou/patch.yaml`) are deep-merged into the Caflou OpenAPI spec **before** FastMCP builds tools.

| Capability | Supported? | Notes |
|------------|------------|-------|
| Rename `operationId` (`List_21` → `List_Tasks`) | ✅ | Primary use today |
| Improve descriptions / schemas for agents | ✅ | e.g. nested `Update_Task` body |
| Overlay existing Caflou route metadata | ✅ | Tool **count unchanged** |
| Add new path to OpenAPI spec | ✅ | Tool count increases |
| Implement composite server logic | ❌ | No Python handlers in mcp-hooker |
| Proxy to a different backend | ❌ | Single `api.base_url` per instance |

Every tool = HTTP call to `api.base_url` + OpenAPI path.

Example: patching `/api/v1/{account_id}/voicebot/log_callback` creates an MCP tool named `Log_Voicebot_Callback`, but Caflou returns **404** unless that route exists on Caflou's API.

**Composite actions** (lookup contact + create task + comment) require a real HTTP handler — implement in **voicebot-core**, then optionally expose via a **separate mcp-hooker instance**.

---

## Option A — Filtered Caflou instance (`caflou-voice`)

For prototyping: expose a small subset of raw Caflou tools to ElevenLabs.

**New instance:** `infra-files/servers/jbi-sv-00/mcp-hooker/instances/caflou-voice/`

```yaml
# tools_filter_voice.yaml
include_operation_ids:
  - List_Contacts
  - Create_Contact
  - Create_Tasks
  - Create_Comments
```

Same `api.base_url: https://app.caflou.com`, same Caflou token — different filter + shorter `server.instructions`.

Patch the four tools with voice-oriented descriptions (HTML reminder, `commented_type` for comments).

**Pros:** Reuses mcp-hooker; ElevenLabs fine-grained tool approval works tool-by-tool.  
**Cons:** Agent still orchestrates 2–4 MCP calls; prompt needs some Caflou rules.

---

## Option B — Thin MCP on voicebot-core (recommended production)

```
ElevenLabs agent
    → MCP: mcp-hooker voicebot instance (~1 tool)
    → HTTP: voicebot-core POST /log-callback
    → Caflou REST
```

Steps:

1. Implement `POST /log-callback` in voicebot-core (see `voicebot-core/docs/ARCHITECTURE.md`).
2. Write minimal OpenAPI spec (single path) — e.g. `instances/voicebot/openapi.yaml`.
3. Deploy mcp-hooker instance:
   - `api.base_url: http://voicebot-core:8000`
   - `openapi.spec: openapi.yaml` (local file, not Caflou)
4. Kong route: `/voicebot/mcp` → voicebot-app container.
5. Register MCP server in ElevenLabs; attach to agent.

Alternative transport: ElevenLabs **webhook tool** directly to voicebot-core (skip mcp-hooker) — simpler, equally valid.

---

## Option C — Patch + nginx split (not recommended)

Patch a fictional path on the Caflou spec and route only that path in Kong to voicebot-core while other paths go to Caflou. Possible but fragile — avoid.

---

## Caflou write reference (for voicebot-core)

Hardcode in backend config:

| Constant | Value |
|----------|-------|
| `account_id` | `958a30da1d8c2ccb0a3b6194` |
| `company_id` | `1740315` |
| `project_id` | `615009` |
| `assignee` | `68570` |

**HTML** required for task descriptions and comments. Patterns in `instances/caflou/config.yaml` `server.instructions`.

Reference code: `scripts/caflou_sync_mcp_project.py` (`Create_Comments`, `_desc()`, nested `Update_Task`).

---

## ElevenLabs dynamic variables for tools

| Variable | Use |
|----------|-----|
| `system__caller_id` | Caller phone → pass to tool param |
| `system__conversation_id` | Audit link in Caflou task |
| `system__agent_id` | Which agent handled the call |

---

## Sync Voicebot interní Caflou project

```bash
export MCP_CAFOU_APIKEY=...
python scripts/caflou_sync_voicebot_project.py
```

---

## Related docs

| Doc | Path |
|-----|------|
| ElevenLabs agent architecture | `elevenlabs/docs/voicebot-architecture.md` |
| voicebot-core design | `voicebot-core/docs/ARCHITECTURE.md` |
| Telephony integration overview | `infra-files/docs/telephony/voicebot-caflou-integration.md` |
| Caflou MCP runbook | `docs/RUNBOOK.md`, `infra-files/docs/mcp-hooker/` |
| Patch overlay vs new tool | `docs/RUNBOOK.md` § OpenAPI patches |
