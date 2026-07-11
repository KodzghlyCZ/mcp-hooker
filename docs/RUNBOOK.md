# mcp-hooker — Operations Runbook

> Maintainer reference for **what** this service does, **how** it is configured, and **how** to operate it in production.
>
> Last updated: 2026-07-09

---

## Table of contents

1. [Executive summary](#1-executive-summary)
2. [Architecture](#2-architecture)
3. [Configuration](#3-configuration)
4. [Authentication](#4-authentication)
5. [Backend example: Caflou](#5-backend-example-caflou)
6. [Production deployment: Caflou (jbi-sv-00)](#6-production-deployment-caflou-jbi-sv-00)
7. [Deployment](#7-deployment)
8. [Reload semantics](#8-reload-semantics)
9. [Observability](#9-observability)
10. [Security](#10-security)
11. [FastMCP & OpenAPI background](#11-fastmcp--openapi-background)
12. [Alternatives & tool landscape](#12-alternatives--tool-landscape)
13. [Multi-server topology & reverse proxy](#13-multi-server-topology--reverse-proxy)
14. [Project context & naming](#14-project-context--naming)
15. [OpenAPI spec quality](#15-openapi-spec-quality)
16. [Troubleshooting](#16-troubleshooting)
17. [Decision log](#17-decision-log)
18. [Lessons learned & incident playbook](#18-lessons-learned--incident-playbook)

---

## 1. Executive summary

**mcp-hooker** is a thin bridge:

1. Read YAML config ([yayaya](https://pypi.org/project/yayaya/))
2. Load an OpenAPI document (HTTP(S) URL or local JSON/YAML file)
3. Build a [FastMCP](https://gofastmcp.com/) server via `FastMCP.from_openapi()`
4. Expose it over **streamable HTTP** on a configurable port

Use it when you want an LLM/MCP client to call an existing REST API without hand-writing one MCP tool per endpoint.

**mcp-hooker is not an auth service.** It forwards HTTP calls to `api.base_url` using whatever headers you configure (or none, if an API gateway injects credentials upstream).

**Scope (v0.1):** one config → one OpenAPI spec → one MCP server per process. Multiple APIs = multiple mcp-hooker instances (typically one container per subdomain or path prefix). See [§6](#6-production-deployment-caflou-jbi-sv-00) for the live Caflou stack and [§13](#13-multi-server-topology--reverse-proxy) for general routing patterns.

**Production (jbi-sv-00):** `https://mcp-hooker.catania-service.cz/caflou/mcp` — nginx → Kong (`key-auth` + ACL + CORS) → `caflou-app-1:8000`.

**Debugging reference:** [§18 Lessons learned & incident playbook](#18-lessons-learned--incident-playbook) — consolidated trial-and-error from production bring-up.

---

## 2. Architecture

### Data flow

```
┌─────────────────────────────────────────────────────────────┐
│ config.yaml (+ optional overlays via MCP_HOOKER_CONFIG_FILES)│
│   yayaya: merge, dot-path get(), ${ENV} expansion           │
│   MCP_HOOKER_ROOT=/app in Docker                            │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ spec_loader                                                  │
│   remote URL ──► httpx GET ──► parse JSON/YAML              │
│   local path ──► read file ──► parse JSON/YAML              │
│   openapi.patch_files ──► deep-merge overlays                │
│   schema_sanitizer ──► fix response schemas for FastMCP      │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ FastMCP.from_openapi(spec, httpx.AsyncClient(base_url))     │
│   each OpenAPI operation → MCP Tool                          │
│   inner app lifespan started explicitly (session manager)    │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ Starlette app (uvicorn)                                      │
│   GET  /health                                               │
│   POST /admin/reload                                         │
│   *    /mcp  (streamable HTTP MCP)                           │
└─────────────────────────────────────────────────────────────┘
```

### Production layout with API gateway (recommended)

```
 MCP client (Cursor, Inspector, …)
        │
        │  streamable HTTP  /mcp
        ▼
 ┌──────────────┐     optional: gateway auth on /mcp
 │ API gateway  │     (TLS, rate limits, IP allowlist)
 │ or reverse   │
 │ proxy        │
 └──────┬───────┘
        │
        ▼
 ┌──────────────┐
 │  mcp-hooker  │  api.headers: {}  (no upstream secrets)
 └──────┬───────┘
        │
        │  plain HTTP to gateway upstream route
        ▼
 ┌──────────────┐     injects Authorization: Bearer <token>
 │ API gateway  │     (or mTLS, API key, etc.)
 │ upstream     │
 └──────┬───────┘
        │
        ▼
   Upstream API (e.g. app.caflou.com)
```

**Process model:** single Python process, single uvicorn worker. Reload stops the inner FastMCP lifespan, rebuilds the OpenAPI provider, and starts a new lifespan. MCP clients with open sessions should reconnect after reload.

---

## 3. Configuration

### File resolution

Config root is resolved in this order:

1. `MCP_HOOKER_ROOT` env var (set to `/app` in Docker)
2. Source checkout (directory containing `pyproject.toml` next to the package — local editable installs)
3. Current working directory

Config files:

1. If `MCP_HOOKER_CONFIG_FILES` is set → comma-separated list, merged left → right.
2. Else → `config.yaml`, then `config.local.yaml` if present.

Relative paths in `MCP_HOOKER_CONFIG_FILES` and `openapi.spec` resolve against the config root above — **not** against the Python package install path in `site-packages` (a common Docker pitfall).

### Server keys

| Key | Default | Notes |
|-----|---------|-------|
| `server.name` | `mcp-hooker` | MCP server display name |
| `server.host` | `0.0.0.0` | Bind address |
| `server.port` | `8000` | Bind port |
| `server.host_origin_protection` | `false` | FastMCP Host/Origin guard; `true` → only loopback Host headers unless `allowed_hosts` set |
| `server.allowed_hosts` | `[]` | Extra Host header patterns when protection is on |

### OpenAPI / API keys

| Key | Notes |
|-----|-------|
| `openapi.spec` | URL or filesystem path (required) |
| `openapi.fetch_timeout` | Seconds when downloading remote specs |
| `openapi.patch_files` | List of local YAML/JSON overlays deep-merged into the parsed spec after fetch; paths relative to the **primary config file directory** (see below) |
| `openapi.validate_output` | When `false`, disable FastMCP strict output-schema validation on tool responses (default `true`). Caflou production uses `false` because live responses often diverge from the published spec |
| `openapi.tools_filter.enabled` | When `true`, apply `openapi.tools_filter` rules before FastMCP builds tools |
| `openapi.tools_filter.file` | Optional YAML file (path relative to primary config dir) with filter rules; overlays inline keys |
| `openapi.tools_filter.include_tags` | Allowlist: only operations with any listed OpenAPI tag are exposed |
| `openapi.tools_filter.exclude_tags` | Denylist: drop operations with any listed tag |
| `openapi.tools_filter.include_path_patterns` | Allowlist: path must match at least one regex |
| `openapi.tools_filter.exclude_path_patterns` | Denylist: drop when path matches any regex |
| `openapi.tools_filter.include_methods` / `exclude_methods` | Optional HTTP method allow/deny lists |
| `openapi.tools_filter.include_operation_ids` / `exclude_operation_ids` | Optional `operationId` allow/deny lists |
| `openapi.tools_filter.tag_path_rules` | Per-tag path allowlists — see [tools filter](#openapi-tools-filter) |
| `openapi.sanitizer.enabled` | Preprocess **response schemas** before FastMCP ingests the spec |
| `openapi.sanitizer.on_unresolved` | `preserve` (log warning) or `replace_generic` (swap unresolved local refs for `{type: object}`) |
| `openapi.sanitizer.paginated_lists.enabled` | Rewrite GET list endpoints (`page`/`per` query params) from bare JSON arrays to paginated object envelopes |
| `openapi.sanitizer.paginated_lists.items_key` | Array field name in paginated responses (default `results`; Caflou uses `results`) |
| `api.base_url` | Upstream base URL; **host only** when spec paths already include `/api/v1/...` (see [§18](#18-lessons-learned--incident-playbook)) |
| `api.timeout` | httpx timeout for tool calls |
| `api.headers` | Extra request headers; values support `${ENV_VAR}` via yayaya |

**`api.base_url` rule of thumb:** httpx joins `base_url` + operation `path`. If every path in the spec already starts with `/api/v1/`, set `base_url` to `https://app.caflou.com` — **not** `https://app.caflou.com/api/v1`. mcp-hooker logs a startup/reload `WARNING` when it detects a doubled prefix.

### OpenAPI patch files

Upstream specs are often incomplete or wrong. Layer local corrections without forking the remote file:

```yaml
openapi:
  spec: https://app.caflou.com/api/v1/i/docs/openapi/v1/openapi.yaml
  patch_files:
    - examples/caflou.accounts.patch.yaml
```

- Patches are **deep-merged** into the downloaded spec (mappings recurse; lists are replaced wholesale).
- Relative paths resolve against the directory of the **primary** config file (the first entry in `MCP_HOOKER_CONFIG_FILES`).
- In production, mount the whole instance config directory — not just `config.yaml` — so patch files are visible inside the container. See [§18 — Instance config vs runtime data](#instance-config-vs-runtime-data-docker).

Example: Caflou's published spec includes `GET /api/v1/accounts` but with minimal metadata (no `operationId`, bare `200` response). A patch adds stable `operationId: List_Accounts`, a clearer description, and a safe output schema for MCP clients. See `examples/caflou.accounts.patch.yaml`.

**Caflou production (Profile A):** `openapi.tools_filter.enabled: true` with instance file `tools_filter.yaml` trims **477 → ~212** tools. See `examples/tools_filter.example.yaml` for the generic schema.

### OpenAPI tools filter

Large OpenAPI specs can exceed MCP client tool limits (e.g. Claude web ~256). mcp-hooker can drop or allowlist operations **before** FastMCP builds tools. Rules are **per instance** — nothing is hardcoded per upstream API.

```yaml
openapi:
  tools_filter:
    enabled: true
    file: tools_filter.yaml   # optional; path relative to config dir
    # or inline any of the keys below (file overlays inline)
```

**Evaluation order** (first match wins):

1. `include_operation_ids` / `exclude_operation_ids`
2. `include_methods` / `exclude_methods`
3. `tag_path_rules` — if operation has a listed tag, keep only when path matches `keep_path_patterns`
4. `include_tags` — allowlist by tag
5. `exclude_tags` — denylist by tag
6. `include_path_patterns` — allowlist by path regex
7. `exclude_path_patterns` — denylist by path regex

Reference: `examples/tools_filter.example.yaml`.

### Response schema sanitizer

Caflou's OpenAPI has several problems that break or confuse FastMCP:

| Problem | Symptom | Sanitizer fix |
|---------|---------|---------------|
| List endpoints declare `type: array` but API returns `{page, results, ...}` | FastMCP output validation fails on tool calls | `paginated_lists.enabled: true` rewrites array → object envelope |
| Recursive/unresolved `#/components/schemas/...` refs in responses | Tool load warnings or client errors | `on_unresolved: preserve` or `replace_generic` |
| JSON Schema union types (`type: ["integer", "null"]`) | FastMCP spec parser rejects schema **at startup** (container never healthy) | Sanitizer uses OpenAPI 3.0-only syntax; envelope uses `additionalProperties: true` for extra pagination fields — see [§18](#paginated-lists-three-layer-mismatch-2026-07-09) |

Production Caflou config:

```yaml
openapi:
  validate_output: false
  sanitizer:
    enabled: true
    on_unresolved: preserve
    paginated_lists:
      enabled: true
      items_key: results
```

The sanitizer only touches **response** schemas, not request bodies or paths.

### Reload keys

| Key | Notes |
|-----|-------|
| `reload.token` | If set, `POST /admin/reload` requires `Authorization: Bearer <token>` |
| `reload.interval_seconds` | Auto-reload interval (`0` = off) |
| `reload.on_sighup` | Reload on `SIGHUP` (default `true`) |

### Example: internal API with local spec

```yaml
server:
  name: billing-mcp
  host: 0.0.0.0
  port: 8080
  host_origin_protection: false

openapi:
  spec: ./specs/billing.openapi.yaml

api:
  base_url: https://gateway.internal.example/billing
  headers: {}   # gateway injects credentials

reload:
  token: "${RELOAD_TOKEN}"
  interval_seconds: 300
  on_sighup: true
```

### Example: dev without gateway (secrets in env)

```yaml
api:
  base_url: https://api.example.com
  headers:
    Authorization: "Bearer ${API_TOKEN}"
```

---

## 4. Authentication

There are **two separate auth boundaries**. Do not conflate them.

| Layer | Who authenticates | Typical approach |
|-------|-------------------|------------------|
| **Client → mcp-hooker** | MCP client / human | Reverse proxy or gateway in front of `/mcp`; optional `reload.token` for `/admin/reload` |
| **mcp-hooker → upstream API** | mcp-hooker (as HTTP client) | **Prefer API gateway** that injects credentials; fallback: `api.headers` with `${ENV}` |

### Prefer gateway for upstream auth (production)

**Why:**

- mcp-hooker stays free of long-lived API secrets
- Token rotation happens in one place (gateway / secret store)
- Centralized audit, rate limiting, and policy

**mcp-hooker config behind a gateway:**

```yaml
api:
  base_url: https://your-gateway.example/upstream-name
  headers: {}
```

The gateway terminates trust on the private network and adds `Authorization`, API keys, or mTLS when calling the real API.

### When `api.headers` is acceptable

- Local development
- No gateway available yet
- Short-lived prototypes

```yaml
api:
  headers:
    Authorization: "Bearer ${UPSTREAM_API_TOKEN}"
```

Never commit tokens in YAML. Use yayaya `${ENV}` expansion and inject secrets via environment, Docker secrets, or orchestrator.

### MCP client URL

Clients should use a **real hostname**, not the bind address:

| URL | Works? |
|-----|--------|
| `http://localhost:8000/mcp` | Yes |
| `http://127.0.0.1:8000/mcp` | Yes |
| `http://0.0.0.0:8000/mcp` | No (Host header rejected when `host_origin_protection: true`; confusing even when false) |
| `http://192.168.x.x:8000/mcp` | Yes if `host_origin_protection: false` or host is in `allowed_hosts` |

### Kong gateway (client → mcp-hooker) — production pattern

On **jbi-sv-00**, MCP clients do **not** talk to mcp-hooker directly. Traffic flows:

```
MCP client  →  nginx :443  →  Kong :8040  →  mcp-hooker container :8000
```

**Kong plugins on the route** (OSS 3.4 — all free):

| Plugin | Purpose | Notes |
|--------|---------|-------|
| **key-auth** | Who is calling? | Header `apikey: <key>` (default key name) |
| **acl** | What may they access? | Per-route `allow` group; consumer must have matching ACL credential |
| **cors** | Browser clients (MCP Inspector) | Required for `localhost:6274` → public HTTPS |

**Setting up a consumer (Kong Manager):**

1. **Consumers** → create consumer (e.g. `mcp-hooker-caflou`).
2. **Credentials → Key Auth** → add API key (or let Kong generate one).
3. **Credentials → ACL** → add group matching the route's ACL plugin `allow` list (e.g. `mcp-hooker-caflou`).
4. On the **Route** (not just the Service): enable **key-auth** then **acl** (`allow: ["mcp-hooker-caflou"]`).

`key-auth` identifies the caller; `acl` restricts which route that key may use. A valid key for route A must **not** work on route B — assign different ACL groups per instance/route.

**Testing auth locally (Kong proxy port 8040):**

```bash
# Expect 401 — no key
curl -I http://localhost:8040/mcp_hooker_caflou/mcp

# Expect 403 — valid key but wrong/missing ACL group on consumer
curl -I -H "apikey: YOUR_KEY" http://localhost:8040/mcp_hooker_caflou/mcp

# Expect 200 (or MCP body) — key + ACL group match
curl -s -X POST http://localhost:8040/mcp_hooker_caflou/mcp \
  -H "apikey: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}'
```

**CORS plugin (for MCP Inspector / browser clients):**

Add **CORS** on the same Kong route:

| Field | Value |
|-------|-------|
| origins | `http://localhost:6274` (no trailing slash) |
| methods | `GET`, `POST`, `OPTIONS` |
| headers | `apikey`, `Content-Type`, `Accept` |
| preflight_continue | `true` |

**Client config (Cursor / Claude / Inspector):**

```json
{
  "mcpServers": {
    "caflou": {
      "url": "https://mcp-hooker.catania-service.cz/caflou/mcp",
      "headers": {
        "apikey": "YOUR_KONG_KEY"
      }
    }
  }
}
```

Use the **public** URL including `/mcp` — not `/caflou` alone. Kong `strip_path=true` maps `/mcp_hooker_caflou/...` to upstream `/...`; the app serves MCP at `/mcp`.

Bearer tokens in `Authorization` are an alternative if you configure Kong `key-auth` with a custom header name — `apikey` is what we use on jbi-sv-00.

### Registering a remote MCP server in Cursor

Remote streamable-HTTP MCP servers go in **`~/.cursor/mcp.json`** (user-level, not workspace):

```json
{
  "mcpServers": {
    "mcp-hooker-caflou": {
      "url": "https://mcp-hooker.catania-service.cz/caflou/mcp",
      "headers": {
        "apikey": "YOUR_KONG_KEY"
      }
    }
  }
}
```

**Checklist:**

| Step | Detail |
|------|--------|
| URL | Must include `/mcp` — not `/caflou` alone |
| Header name | Must match Kong `key-auth` `key_names` (default `apikey`, not `Authorization`) |
| Save + reload | Save `mcp.json`, then **reload Cursor window** or restart Cursor |
| New chat | Start a **new agent/chat session** after reload — existing sessions keep the old MCP tool list |
| Verify | Ask the agent which MCP tools it can access; `mcp-hooker-caflou` should appear alongside other servers |

**Common registration mistakes:**

- Entry is valid JSON but Cursor was not reloaded → server never attaches to new sessions.
- API key in `mcp.json` is correct but Kong consumer lacks the matching **ACL group** → 403 on connect.
- Using `http://localhost:8040/mcp_hooker_caflou/mcp` for local Kong testing works, but production clients should use the public HTTPS URL through nginx.
- Storing the Kong key in git — keep it in `mcp.json` locally or use env substitution if your Cursor version supports it; treat like a password.

Contrast with **stdio** servers (e.g. `zpl-mcp`, `ElevenLabs`) which use `command` + `args` instead of `url`.

---

## 5. Backend example: Caflou

Primary reference: [Caflou API Postman docs](https://documenter.getpostman.com/view/4786951/RWMFrTQC).

OpenAPI spec (same API): `https://app.caflou.com/api/v1/i/docs/openapi/v1/openapi.yaml`

### Upstream authentication

The Caflou OpenAPI spec applies **global Bearer auth** to all operations:

```yaml
security:
  - bearer_auth: []
```

Scheme: `Authorization: Bearer <token>`.

**Exception:** `POST /api/v1/login` uses **HTTP Basic** (email + password, optional OTP query param) to **obtain** a token. Every other endpoint expects the Bearer token on **each request**. There is no cookie/session model in the spec — you do not re-login per call, but you do send the token every time.

**Obtain a token (recommended for integrations):**

1. Caflou → **Settings → Account settings → API**
2. **Add token** → name, validity, copy token

Guide: [How to obtain access token for API, Integromat or Zapier](https://www.caflou.com/education/how-to-obtain-access-token-for-api-integromat-or-zapier)

### Account ID in paths

Nearly all Caflou routes are under `/api/v1/{account_id}/…`. The path parameter is documented as **Account ID or Token** — this is separate from the `Authorization` header. Tool calls typically need **both**:

- `Authorization: Bearer <token>` (header)
- `{account_id}` in the URL path

### Recommended mcp-hooker setup for Caflou

**Deployed on jbi-sv-00 (token injected in mcp-hooker via env):**

```yaml
server:
  name: caflou-mcp
  host: 0.0.0.0
  port: 8000
  host_origin_protection: false

openapi:
  spec: https://app.caflou.com/api/v1/i/docs/openapi/v1/openapi.yaml
  validate_output: false
  patch_files:
    - patch.yaml          # relative to config file dir (e.g. /app/instance-config/)
  sanitizer:
    enabled: true
    on_unresolved: replace_generic   # required for Cursor (List_TaskTodos $ref issue)
    paginated_lists:
      enabled: true
      items_key: results

api:
  base_url: https://app.caflou.com
  headers:
    Authorization: "Bearer ${CAFLOU_API_TOKEN}"

reload:
  token: "${RELOAD_TOKEN}"
```

```bash
export CAFLOU_API_TOKEN="…"   # from Caflou Settings → Account settings → API
docker compose up -d --force-recreate   # after image or mounted config/patch changes
```

Client auth (who may reach mcp-hooker) is handled separately by Kong `key-auth` + ACL ([§4](#4-authentication), [§6](#6-production-deployment-caflou-jbi-sv-00)).

**Alternative (upstream gateway injects Bearer token):**

```yaml
api:
  base_url: https://your-gateway.example/caflou   # proxies to https://app.caflou.com
  headers: {}
```

Use this when you want the long-lived Caflou token to live only in the gateway/secret store, not in the mcp-hooker environment.

### Caflou API notes

- Default list page size is 20; `per` query param can go up to 1000 (use carefully — rate limits apply).
- Filtering is supported via a `filter` object on list requests; see spec description for examples.
- Treat MCP tool access as **full API access** within the token's granted permissions.
- **Discover account ID first:** call `List_Accounts` (patched `operationId` on existing `GET /api/v1/accounts`) — returns `[{name, id, role}]`; use `id` as `account_id` in other tools.
- **MCP handshake ≠ upstream auth:** `initialize` and `tools/list` succeed even when `CAFLOU_API_TOKEN` is missing; only actual tool calls hit Caflou and will `401`.
- **Spec vs reality:** list endpoints return paginated objects (`{page, results, prev_page, ...}`) but the published spec often declares a bare array — enable `sanitizer.paginated_lists` and/or `validate_output: false`. Full trial-and-error narrative: [§18 — Paginated lists](#paginated-lists-three-layer-mismatch-2026-07-09).
- **Opaque tool names:** MCP tools use upstream `operationId` values (`List_21`, `Create_13`, …). See [§18 — tool name map](#caflou-mcp-tool-names-opaque-operationids) and [agent workflow recipes](#agent-workflow-recipes-verified-2026-07-09).
- **No reliable server-side text search via MCP today:** `Search_2` lacks query params in the tool schema — paginate list endpoints and filter client-side ([§18 — search limitations](#caflou-search-limitations)).

See [§6](#6-production-deployment-caflou-jbi-sv-00) for the live **mcp-hooker-caflou** stack on jbi-sv-00 (nginx → Kong → container).

---

## 6. Production deployment: Caflou (jbi-sv-00)

**Status:** Fixed 2026-07-08 (chain). **2026-07-09:** Cursor client loading fixed via response schema sanitizer (`replace_generic`); `List_Accounts` patch + instance config dir mount documented in [§18](#18-lessons-learned--incident-playbook). **2026-07-09 (later):** Caflou list-tool output validation — `paginated_lists` sanitizer + `validate_output: false`; full trial-and-error in [§18 — Paginated lists](#paginated-lists-three-layer-mismatch-2026-07-09). MCP responds at `https://mcp-hooker.catania-service.cz/caflou/mcp` (**477 tools** — count unchanged when patching existing paths).

Infra layout follows `deploy/mcp-hooker/instances/<name>/` (spliffy pattern). Live Caflou instance: Docker project `caflou`, container `caflou-app-1`, network `caflou_internal`.

**Config layout:** instance files (`config.yaml`, `patch.yaml`) bind to `/app/instance-config/`; runtime secrets (`.env`) stay in `/var/www/mcp-hooker/<project>/` → `/instance`. See [§18 — Instance config vs runtime data](#instance-config-vs-runtime-data-docker).

### Request chain

```
MCP client (Cursor, Inspector, …)
  │
  ▼
nginx :443                         vhost mcp-hooker.catania-service.cz
  │  location /caflou
  │  rewrite ^/caflou(.*)$ → /mcp_hooker_caflou$1
  ▼
Kong :8040 (proxy)                  route "mcp-hooker-caflou"
  │  path /mcp_hooker_caflou, strip_path=true
  │  plugins: key-auth + acl + cors
  ▼
caflou-app-1 :8000                  image mcp-hooker:latest, uvicorn
  │  MCP at /mcp  (serverInfo: caflou)
  │  injects Authorization: Bearer ${CAFLOU_API_TOKEN}
  ▼
Caflou REST API  (app.caflou.com)   authorized per request via Bearer token
```

| Layer | External | Internal |
|-------|----------|----------|
| Public URL | `https://mcp-hooker.catania-service.cz/caflou/mcp` | — |
| nginx rewrite target | — | `http://127.0.0.1:8040/mcp_hooker_caflou/mcp` |
| Kong route path | — | `/mcp_hooker_caflou` |
| Kong → container | — | `http://caflou-app-1:8000/mcp` |
| Host published port | `0.0.0.0:3003` | maps to container **8000** (not 3003) |

**Critical:** Kong talks to the **container internal port** (`8000`), not the host-mapped port (`3003`). `docker port caflou-app-1` shows `8000/tcp -> 0.0.0.0:3003`.

### nginx (`/etc/nginx/sites-enabled/mcp-hooker-caflou`)

External path `/caflou` → internal Kong path `/mcp_hooker_caflou`. The `location` block must live in the **`listen 443 ssl`** server block (not only port 80 — Certbot often splits these).

```nginx
server {
    listen 443 ssl;
    server_name mcp-hooker.catania-service.cz;

    ssl_certificate /etc/letsencrypt/live/mcp-hooker.catania-service.cz/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mcp-hooker.catania-service.cz/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    location ^~ /caflou {
        rewrite ^/caflou(.*)$ /mcp_hooker_caflou$1 break;
        proxy_pass http://127.0.0.1:8040;

        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 24h;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    server_name mcp-hooker.catania-service.cz;
    return 301 https://$host$request_uri;
}
```

### Kong route & service

| Item | Value |
|------|-------|
| Route name | `mcp-hooker-caflou` |
| Route path | `/mcp_hooker_caflou` |
| strip_path | `true` |
| Service host | `caflou-app-1` (container name, **not** `127.0.0.1`) |
| Service port | **8000** |
| Service protocol | `http` |
| Service id (Postgres) | `2abc28cf-7f85-4007-974f-48f96898f361` |
| Kong proxy (host) | `:8040` |
| Kong admin | `:8003` → container `8001` |
| Consumer | `mcp-hooker-caflou` |
| ACL group | `mcp-hooker-caflou` |
| Auth header | `apikey` |

**Plugins on the route:** `key-auth`, `acl` (allow `mcp-hooker-caflou`), `cors` (for MCP Inspector from `http://localhost:6274`).

Optional Kong env for streamable HTTP (if 502 "invalid response from upstream" persists):

```yaml
# kong-stack docker-compose.yml
kong:
  environment:
    KONG_NGINX_HTTP_CONFIGURATION_SNIPPET: |
      proxy_buffering off;
      proxy_cache off;
      proxy_request_buffering off;
      proxy_read_timeout 3600s;
      proxy_send_timeout 3600s;
```

Recreate Kong after changing: `docker compose up -d --force-recreate kong`.

### Docker networking

Kong and `caflou-app-1` must share a Docker network. Caflou uses **`caflou_internal`**; Kong defaults to **`kong-stack_default`**.

```bash
docker network connect caflou_internal kong
```

Verify from Kong (no curl/wget in image — use bash `/dev/tcp`):

```bash
docker exec -it kong bash -c "timeout 2 bash -c '</dev/tcp/caflou-app-1/8000' && echo Success || echo Failed"
```

List networks and members:

```bash
docker network inspect $(docker network ls -q) --format '{{.Name}}: {{range .Containers}}{{.Name}} {{end}}'
```

### Upstream authorization (mcp-hooker → Caflou)

Kong secures the **client → mcp-hooker** hop. Caflou itself requires a **Bearer token on every request** ([§5](#5-backend-example-caflou)), so mcp-hooker must add it to each upstream call. There is no upstream gateway on jbi-sv-00, so the token is injected by mcp-hooker directly.

**config.yaml** (baked/mounted into the image):

```yaml
api:
  base_url: https://app.caflou.com
  headers:
    Authorization: "Bearer ${CAFLOU_API_TOKEN}"
```

yayaya expands `${CAFLOU_API_TOKEN}` from the process environment at load time; the raw token is **never** committed. mcp-hooker builds one `httpx.AsyncClient(base_url=…, headers=…)` and reuses it for all tool calls, so every Caflou request carries the header. The client (and its headers) is rebuilt on each `/admin/reload`, so rotating the token = update env + reload.

**Wiring the env var:**

| Mechanism | How |
|-----------|-----|
| docker-compose | `environment: CAFLOU_API_TOKEN: ${CAFLOU_API_TOKEN:-}`; value from `.env` (gitignored) or host env |
| infra-files instance | put `CAFLOU_API_TOKEN=…` in the instance `runtime.env` / secret store |
| bare process | `export CAFLOU_API_TOKEN=…` before `mcp-hooker` |

**Verify the header is applied (no live API call needed):**

```bash
CAFLOU_API_TOKEN=xxx MCP_HOOKER_ROOT="$PWD" python -c "
from mcp_hooker.settings import cfg_headers, cfg_get
print(cfg_get('api.base_url'), cfg_headers())
"
# → https://app.caflou.com {'Authorization': 'Bearer xxx'}
```

**Notes:**

- The token is separate from the `{account_id}` path parameter — tool calls still need the correct account ID in the path ([§5](#5-backend-example-caflou)).
- A missing/empty token yields upstream `401`s on tool calls even though MCP handshake and `tools/list` succeed (those don't hit Caflou).
- Rotate: update env, then `POST /admin/reload` (or restart the container) to rebuild the httpx client.

### Root cause: 502 / "no Route matched" (2026-07-08 incident)

**Symptom:** With API key, Kong returned `404 "no Route matched"` or `502` with `Via: kong` and low `x-kong-upstream-latency` (~2 ms).

**Cause:** Kong service upstream was `host=127.17.0.1`, `port=8000`. The entire `127.0.0.0/8` range is loopback — Kong was forwarding to **itself**, not the app container. With `strip_path=true`, the looped request hit `/` and matched no route.

**Fix:**

```bash
curl -X PATCH "http://127.0.0.1:8003/services/2abc28cf-7f85-4007-974f-48f96898f361" \
  --data "host=caflou-app-1"
```

Use the **container name** (survives IP changes on restart), port **8000**.

**Red herring:** From LAN, curling the public IP `90.178.237.94:443` may **timeout** (NAT hairpin not supported on the router). External clients are fine. Validate locally with:

```bash
curl -k --resolve mcp-hooker.catania-service.cz:443:127.0.0.1 \
  -X POST https://mcp-hooker.catania-service.cz/caflou/mcp \
  -H "apikey: <KEY>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"v","version":"1"}}}'
```

### Validation checklist

| Step | Command / check | Expect |
|------|-------------------|--------|
| Container health | `curl http://127.0.0.1:3003/health` | `{"status":"ok",...}` |
| Kong → app TCP | `docker exec kong bash -c '</dev/tcp/caflou-app-1/8000'` | `Success` |
| Kong auth | `curl -I -H "apikey: …" http://localhost:8040/mcp_hooker_caflou/mcp` | not 401/403 |
| Full public chain | `curl -k --resolve …` (above) | HTTP 200, MCP initialize |
| Tool count | `tools/list` after initialize | 477 tools (Caflou spec) |

### Key IDs & hosts (jbi-sv-00)

| Item | Value |
|------|-------|
| Public host | `mcp-hooker.catania-service.cz` → `90.178.237.94` |
| Docker host | `10.0.1.132`, gateway `10.0.1.138` |
| nginx site | `/etc/nginx/sites-enabled/mcp-hooker-caflou` |
| Container | `caflou-app-1` |
| Shared network | `caflou_internal` |
| Registry image | `registry.gitlab.catania-service.cz/catania_dev/mcp-hooker:latest` |

### Path-prefix pattern for more instances

Same host, multiple APIs:

| External path | Kong route path | Container | ACL group |
|---------------|-----------------|-----------|-----------|
| `/caflou/mcp` | `/mcp_hooker_caflou` | `caflou-app-1` | `mcp-hooker-caflou` |
| `/edu-gov-cz/mcp` | `/mcp_hooker_edu_gov_cz` | `edu-gov-cz-app-1` | `mcp-hooker-edu-gov-cz` |

nginx: `rewrite ^/caflou(.*)$ /mcp_hooker_caflou$1 break;` per location block.

### Production bring-up chronology (2026-07-08)

Step-by-step record of what we tried, what failed, and what fixed it. Use as a checklist when adding the next instance.

| # | Symptom | Layer | Cause | Fix |
|---|---------|-------|-------|-----|
| 1 | `404` from `nginx/1.18` | nginx | `location /caflou` not in `listen 443 ssl` block (Certbot split HTTP/HTTPS) | Move location into SSL server block |
| 2 | Kong `404`, `X-Kong-Response-Latency: 0` | Kong route | Wrong path — route **name** ≠ route **path** | Hit `/mcp_hooker_caflou`, not `/mcp-hooker-caflou` |
| 3 | Kong `401` | Kong key-auth | No `apikey` header | Add Key Auth credential on consumer; send `apikey: <key>` |
| 4 | Kong `403` with valid key | Kong ACL | Consumer not in route's `allow` group | Add ACL credential on consumer matching route ACL plugin |
| 5 | Kong `502 Bad Gateway` | Kong → upstream | Wrong internal port (`3003` host port instead of `8000` container port) | Service port = **8000** |
| 6 | `Connection refused` from Kong | Docker network | `kong` and `caflou-app-1` on different networks | `docker network connect caflou_internal kong` |
| 7 | `Name or service not known` inside Kong | Docker DNS | Same as #6 | Connect containers to shared network |
| 8 | Kong `404 no Route matched` + `Via: kong`, ~2 ms upstream latency | Kong upstream | `host=127.17.0.1` (typo — entire `127.0.0.0/8` is loopback; Kong proxied to itself) | `PATCH` service → `host=caflou-app-1` |
| 9 | CORS error from MCP Inspector (`localhost:6274`) | Kong | Browser preflight blocked | Add Kong **CORS** plugin on route |
| 10 | Kong `502` / `invalid response from upstream` | Kong buffering | SSE/streamable HTTP buffered by Kong's internal nginx | `KONG_NGINX_HTTP_CONFIGURATION_SNIPPET` with `proxy_buffering off`; recreate container |
| 11 | Public IP timeout from LAN | Router NAT | Hairpin/loopback not supported (`90.178.237.94` from inside LAN) | Test with `--resolve …:127.0.0.1` or from external network |
| 12 | MCP client hits `/caflou` not `/caflou/mcp` | Path | FastMCP mounts at `/mcp`; Kong `strip_path` strips route prefix only | Client URL must end in `/mcp` |
| 13 | `tools/list` works, tool calls `401` | Upstream auth | `CAFLOU_API_TOKEN` missing/empty in container env | Set env + reload |
| 14 | Every tool call `404`, URL has `/api/v1/api/v1/` | `api.base_url` | Base URL included `/api/v1` but spec paths already do | `base_url: https://app.caflou.com` (host only) |
| 15 | Tool output validation errors on list endpoints | OpenAPI spec | Spec declares array; API returns paginated object | `sanitizer.paginated_lists.enabled: true` |
| 16 | Cursor agent can't see `mcp-hooker-caflou` tools | Client | `mcp.json` entry present but Cursor not reloaded / old chat session | Reload window + new chat |
| 17 | **Application startup failed** — hundreds of Pydantic errors on `prev_page` / `next_page` `type: ['integer', 'null']` | Sanitizer schema | First `paginated_lists` envelope used JSON Schema union types; FastMCP's **OpenAPI spec parser** rejects them at ingest (before any tool call) | Use OpenAPI 3.0-only envelope: `page` + `results` + `additionalProperties: true`; no `type: ["integer", "null"]` |
| 18 | List tools still fail after pagination fix: `None is not of type 'string'` or `{user object} is not of type 'string'` | Output validation | Pagination envelope OK; **item-level** schemas wrong (nullable fields, nested objects typed as strings) | `validate_output: false` (production); or patch/sanitize component schemas |
| 19 | `List_Accounts` tool not found in Cursor | Patch not mounted | `patch_files` set in config but `patch.yaml` not in container config dir | Mount instance config dir; see [§18 — Instance config](#instance-config-vs-runtime-data-docker) |
| 20 | Agent passes `account_id: "Token"` literal | Tool args | Path param docs say "Account ID or Token" but API expects real account hash | Call `List_Accounts` first; use returned `id` |
| 21 | Caflou `Search` / `Search_2` returns empty | OpenAPI + MCP | Search endpoints exist but MCP tool schema exposes only `account_id` — no `q`/`query` param wired | Paginate list endpoints and filter client-side; or extend patch/spec for search params |
| 22 | Created project under wrong company | Data model | Only one "CATANIA*" company in CRM (`CATANIA GROUP s.r.o.`) | Confirm `company_id` via `List_3` (companies) before `Create_13` |

**Kong consumer setup (OSS, free):** `key-auth` identifies *who*; `acl` restricts *what*. Add credentials on the **Consumer** (Key Auth + ACL group), not on the plugin. Apply both plugins on the **Route**, not just the Service. Plugin order: `key-auth` before `acl`.

**Debugging without curl inside Kong:** the official Kong image has neither `curl` nor `wget`. Use bash socket test:

```bash
docker exec -it kong bash -c "timeout 2 bash -c '</dev/tcp/caflou-app-1/8000' && echo Success || echo Failed"
```

**Auth test matrix (Kong port 8040):**

```bash
curl -I  http://localhost:8040/mcp_hooker_caflou/mcp                    # → 401
curl -I -H "apikey: KEY" http://localhost:8040/mcp_hooker_caflou/mcp   # → 403 if ACL wrong, else pass
```

---

## 7. Deployment

### Local / dev

```bash
pip install -e .
mcp-hooker
```

### Docker

```bash
docker build -t mcp-hooker .
docker run -d \
  --name mcp-hooker \
  -p 8000:8000 \
  -e MCP_HOOKER_ROOT=/app \
  -v /path/to/config.yaml:/app/config.yaml:ro \
  mcp-hooker
```

`MCP_HOOKER_ROOT=/app` is set in the Dockerfile. Compose also sets it explicitly.

### Docker Compose

```bash
docker compose up -d --build
```

Override port: `MCP_HOOKER_PORT=9000 docker compose up`.

Mount `config.yaml` at `/app/config.yaml`. Optional overlay: add `config.local.yaml` to the mount list and set `MCP_HOOKER_CONFIG_FILES=config.yaml,config.local.yaml`.

### GitLab CI/CD

Pipeline stage: **build**

- Runner tag: `mcp-hooker` (edit `.gitlab-ci.yml` if your fleet uses another tag)
- Builds and pushes `$CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA`
- On default branch, also pushes `:latest`

Same pattern as **zpl-mcp** (without the extra `zpl-adapter` build context).

### Reverse proxy / gateway in front of mcp-hooker

Terminate TLS at nginx, Traefik, Caddy, or your API gateway and forward to the container port. For **production on jbi-sv-00** (nginx → Kong → container, path prefixes), see [§6](#6-production-deployment-caflou-jbi-sv-00). For **multiple APIs on different subdomains**, see [§13](#13-multi-server-topology--reverse-proxy).

MCP streamable HTTP benefits from:

- Long-lived connections allowed
- WebSocket upgrade if your proxy version requires it for SSE/streamable transports
- Expose `mcp-session-id` if browser clients need it (mcp-hooker CORS already lists it)

Health check target: `GET /health` → `{"status":"ok",...}`.

---

## 8. Reload semantics

| Trigger | Behavior |
|---------|----------|
| Startup | Loads config + spec; starts inner FastMCP lifespan |
| `POST /admin/reload` | yayaya `reload_config()` → re-fetch spec → new FastMCP instance + new lifespan |
| `SIGHUP` | Same as HTTP reload when `reload.on_sighup: true` |
| `reload.interval_seconds > 0` | Background task repeats reload |

Reload steps:

1. Acquire async lock (concurrent reloads serialize)
2. Stop inner FastMCP lifespan (session manager task group)
3. Close previous `httpx.AsyncClient`
4. `reload_settings()` (yayaya re-reads YAML from disk)
5. Fetch/parse OpenAPI
6. `FastMCP.from_openapi(...)` → new `mcp_app`
7. Start inner FastMCP lifespan

**Caveat:** MCP clients holding a session across reload must reconnect.

Protect `POST /admin/reload` in production by setting `reload.token`.

**MCP discovery caveat:** clients (Cursor, Claude Desktop, Inspector) usually list tools **once per session**. After reload, reconnect the client to see added/removed/changed tools.

---

## 9. Observability

### Logs

Structured-ish INFO lines on reload:

```text
Reloaded MCP server (startup): spec=... base_url=... tools=N
```

Successful MCP initialize:

```text
Created new transport with session ID: …
```

Failures log stack traces at ERROR.

### Health

```bash
curl -s http://localhost:8000/health | jq
```

```json
{
  "status": "ok",
  "spec": "https://app.caflou.com/api/v1/i/docs/openapi/v1/openapi.yaml",
  "base_url": "https://app.caflou.com"
}
```

### Manual reload

```bash
curl -X POST http://localhost:8000/admin/reload
# with token:
curl -X POST -H "Authorization: Bearer $RELOAD_TOKEN" http://localhost:8000/admin/reload
```

---

## 10. Security

| Topic | Guidance |
|-------|----------|
| Upstream credentials | **Prefer gateway injection**; otherwise `${ENV}` in `api.headers` — never commit secrets |
| Reload endpoint | Set `reload.token` in production |
| MCP exposure | Protect `/mcp` at reverse proxy/gateway in production |
| Network | Private network between mcp-hooker and gateway/upstream |
| CORS | Permissive (`*`) for MCP Inspector — tighten at reverse proxy if needed |
| OpenAPI source | Only load specs from trusted URLs/paths (SSRF risk on remote `openapi.spec`) |
| `host_origin_protection` | Enable only when you control allowed Host headers; default `false` for Docker/LAN |

mcp-hooker executes whatever operations the OpenAPI spec describes against `api.base_url` — treat tool access like direct API access.

### Production security checklist

MCP is an open protocol — you are not "banned" for self-hosting, but you **can** compromise your own systems if the server is exposed carelessly:

| Risk | Mitigation |
|------|------------|
| Public `/mcp` without auth | Reverse proxy or gateway with API key / OAuth **before** traffic reaches mcp-hooker |
| Credential leakage | Never commit tokens in YAML; use `${ENV}`, gateway injection, or secret managers |
| Confused deputy | Gateway validates identity; do not blindly forward client-supplied credentials to upstream |
| Tool explosion | Large OpenAPI specs → hundreds of tools; prune or filter routes (see [§15](#15-openapi-spec-quality)) |
| Over-broad upstream tokens | Scope Caflou (and similar) API tokens to minimum permissions |
| No audit trail | Log tool calls at gateway or upstream API |

Run each instance in a **restricted container** without unnecessary host filesystem access. Containerize per [§7](#7-deployment).

---

## 11. FastMCP & OpenAPI background

Research and design notes (2026-07-07) that led to this repo.

### Can FastMCP load OpenAPI YAML directly?

**Yes.** Since FastMCP 2.0+, `FastMCP.from_openapi()` converts an OpenAPI document into MCP tools. mcp-hooker fetches remote JSON/YAML or reads a local file, parses it to a dict, and passes it to FastMCP.

Minimal pattern (what mcp-hooker automates):

```python
import httpx
from fastmcp import FastMCP

client = httpx.AsyncClient(base_url="https://api.example.com")
openapi_spec = httpx.get("https://api.example.com/openapi.yaml").json()  # or yaml.safe_load

mcp = FastMCP.from_openapi(
    openapi_spec=openapi_spec,
    client=client,
    name="My API Server",
)

if __name__ == "__main__":
    mcp.run()
```

### Default behaviour

| Topic | Behaviour |
|-------|-----------|
| Endpoint mapping | By default, **every** OpenAPI operation becomes an MCP **Tool** |
| Tool names | Usually derived from `operationId` in the spec |
| Custom mapping | `RouteMap` list → map routes to `TOOL`, `RESOURCE`, or exclude |
| Upstream auth | Configure on `httpx.AsyncClient` (headers, hooks) — or omit headers when a gateway injects them |
| Hand-curated vs auto | Auto-conversion is fast to bootstrap; complex APIs often benefit from a smaller, curated tool surface |

Docs: [FastMCP OpenAPI integration](https://gofastmcp.com/integrations/openapi)

### Periodic spec refresh

MCP clients typically **discover tools at connect time**. Updating the spec on a running server does not automatically push new tools to long-lived sessions.

**mcp-hooker approach (watcher pattern, in-process):**

- `reload.interval_seconds` — background poll
- `POST /admin/reload` — manual refresh
- `SIGHUP` — reload when `reload.on_sighup: true`
- On reload: stop inner FastMCP lifespan → re-fetch spec → rebuild tools → start new lifespan
- **Clients must reconnect** after reload to see tool changes

**Alternatives if you need gateway-managed refresh:**

- [AgentGateway OpenAPI → MCP](https://agentgateway.dev/docs/standalone/latest/mcp/connect/openapi/) — config reload on file change
- [PolicyLayer refresh-api-catalog](https://policylayer.com/tools/reapi-com-mcp-openapi/refresh-api-catalog) — MCP tool to refresh catalog

### One process vs orchestrator (`mount()`)

FastMCP supports **server composition** via `mcp.mount()` with optional namespaces (e.g. `billing.get_invoice`, `crm.get_contact`). That is a valid single-process "orchestrator" pattern.

**mcp-hooker deliberately chose one spec per process** because:

- Simpler ops: one `config.yaml`, one subdomain, one CI image
- Independent reload and failure domains per API
- Matches docker-compose / GitLab CI patterns used elsewhere in the fleet (zpl-mcp)

You can run multiple mcp-hooker containers behind a reverse proxy instead of one monolith with `mount()`.

---

## 12. Alternatives & tool landscape

Other projects solve OpenAPI → MCP. Use this table to decide whether mcp-hooker fits.

| Goal | Tool | Notes |
|------|------|-------|
| Python, configurable, self-hosted, spec reload | **mcp-hooker** (this repo) | yayaya config, Docker, GitLab CI, HTTP transport |
| Python, embed in your own app | [FastMCP](https://github.com/PrefectHQ/fastmcp) | `from_openapi()` directly; you own reload/proxy code |
| Zero-code CLI / proxy (.NET) | [ZeroMcp.Relay](https://github.com/ZeroMcp/ZeroMcp.Relay) | Point at OpenAPI URL; global dotnet tool |
| Managed gateway + observability | [TrueFoundry OpenAPI → MCP](https://www.truefoundry.com/docs/ai-gateway/mcp-server) | Upload spec; hosted |
| Node.js generator | [mcp-from-openapi](https://www.npmjs.com/package/mcp-from-openapi) | npm library |
| TypeScript utilities | [openapi2mcptools](https://mcp.so/server/openapi2mcptools) | Flexible HTTP client config |
| Go, multi-transport | [openapi-to-mcp](https://github.com/constellation39/openapi-to-mcp) | stdio, SSE, stream |
| Aggregate many MCP servers | [MCP Gateway](https://mcpservers.org/servers/lucky-aeon/mcp-gateway) | Single entry point, auth, translation |

**When mcp-hooker is the right fit:** you already use Python/yayaya in the stack, want YAML-driven config, scheduled or manual spec reload, and one MCP endpoint per upstream API behind your own reverse proxy.

---

## 13. Multi-server topology & reverse proxy

### jbi-sv-00 pattern: one host, path prefixes, Kong in the middle

Production on **jbi-sv-00** uses a **single public hostname** with path prefixes — not one subdomain per API. Full detail: [§6](#6-production-deployment-caflou-jbi-sv-00).

```
https://mcp-hooker.catania-service.cz/caflou/mcp
  → nginx rewrite /caflou → /mcp_hooker_caflou
  → Kong :8040 (key-auth + acl + cors)
  → caflou-app-1:8000/mcp
```

**Why Kong between nginx and mcp-hooker:**

- Central **client auth** (`key-auth` + per-route `acl`) — one key must not unlock every instance
- **CORS** for browser-based MCP Inspector
- Optional upstream Caflou Bearer injection at gateway (future)

**Do not** point nginx directly at the mcp-hooker container if you need Kong auth. nginx only rewrites the path and proxies to `127.0.0.1:8040`.

### Recommended production pattern (alternative: subdomains)

Run **one mcp-hooker instance per upstream API**, each on its own internal port. Map **one subdomain per instance** at the reverse proxy.

```
api-caflou.example.com  →  localhost:8001  →  mcp-hooker (Caflou spec)
api-other.example.com   →  localhost:8002  →  mcp-hooker (other spec)
```

This scales operationally: separate config, secrets, reload, and blast radius.

### Streamable HTTP proxy requirements

MCP streamable HTTP uses long-lived connections. Proxies must:

| Setting | Why |
|---------|-----|
| HTTP/1.1 keep-alive | Persistent MCP sessions |
| **Disable response buffering** | SSE / streamed responses stall if buffered |
| Long read/write timeouts | Sessions can run hours (e.g. 86400s) |
| TLS termination at proxy | mcp-hooker listens plain HTTP internally |

### Caddy example

```caddy
api-caflou.example.com {
    reverse_proxy localhost:8001 {
        flush_interval -1
    }
}

api-other.example.com {
    reverse_proxy localhost:8002 {
        flush_interval -1
    }
}
```

Caddy handles TLS certificates automatically.

### Nginx example

```nginx
server {
    server_name api-caflou.example.com;

    location / {
        proxy_pass http://localhost:8001;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 86400;
    }
}
```

### Docker Compose (multiple services)

```yaml
services:
  mcp-hooker-caflou:
    build: .
    ports: ["8001:8000"]
    environment:
      MCP_HOOKER_ROOT: /app
      MCP_HOOKER_CONFIG_FILES: config.caflou.yaml
    volumes:
      - ./config.caflou.yaml:/app/config.caflou.yaml:ro

  mcp-hooker-other:
    build: .
    ports: ["8002:8000"]
    environment:
      MCP_HOOKER_ROOT: /app
      MCP_HOOKER_CONFIG_FILES: config.other.yaml
    volumes:
      - ./config.other.yaml:/app/config.other.yaml:ro
```

Put Caddy/nginx in front of the host ports (or use a shared Docker network + internal DNS).

### MCP gateway vs raw reverse proxy

If subdomain sprawl becomes painful, an **MCP gateway** can aggregate upstream MCP servers behind one endpoint with centralized auth and observability. Trade-off: another component to operate. See [§12](#12-alternatives--tool-landscape).

### Spec refresh behind a proxy

With per-instance mcp-hooker, each service reloads its own spec (`reload.interval_seconds` or cron calling `/admin/reload`). The reverse proxy keeps accepting connections; **MCP clients reconnect** to pick up new tools after reload.

---

## 14. Project context & naming

### Why "mcp-hooker"

Codename chosen during early design (2026-07-07): the service **hooks** existing REST APIs into the MCP ecosystem. Memorable, short, matches the "attach to live APIs" idea.

Other names considered in brainstorming: `synapse-gate`, `mcp-tether`, `mcp-intercept`, `yayaya-relay`. **`mcp-hooker` stuck** as the repo name.

### Repo visibility & forking

- **Intent:** open for others to use if useful; unmaintained edgy name is acceptable to the author.
- **Policy:** if someone dislikes the name or design, they can fork and rename.
- **Practical note:** edgy repo names can trigger automated content filters on some platforms (search suppression, corporate dependency scanners flagging "offensive language" lists). Unlikely to cause account bans, but may reduce discoverability. A professional `README.md` matters more than the URL for adoption.

### Relationship to yayaya

Config loading uses [yayaya](https://pypi.org/project/yayaya/) (same author ecosystem as myskin). Layered YAML, dot-path keys, `${ENV}` expansion — keeps non-secrets in git and secrets in the environment.

### What this repo is not

- Not a multi-tenant MCP gateway (use gateway products or multiple instances)
- Not a replacement for hand-crafted MCP tools on complex APIs
- Not an auth broker (use API gateway for upstream credentials)

---

## 15. OpenAPI spec quality

Auto-generated MCP tools are only as good as the OpenAPI document.

| Spec quality | Effect on LLM |
|--------------|---------------|
| Missing `operationId` | Awkward or duplicated tool names |
| Vague `summary` / `description` | Model picks wrong endpoint |
| No parameter descriptions | Wrong query/path/body values |
| Huge spec (100+ operations) | Tool explosion, confusion, latency |

**Spend 5–10 minutes polishing** the spec (or a filtered subset) before pointing mcp-hooker at it:

- Clear `summary` and `description` on operations you care about
- Stable `operationId` values
- Document path/query/body parameters with examples where possible
- Consider trimming unused tags/paths from a vendored copy of the spec

For Caflou-sized APIs, expect **many** tools out of the box — plan gateway auth and least-privilege tokens accordingly.

When the upstream spec is wrong (missing endpoints, paginated lists declared as arrays, cyclic refs), use `openapi.patch_files` and `openapi.sanitizer` — see [§3](#3-configuration) and [§18](#18-lessons-learned--incident-playbook).

---

## 16. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `ConfigFileNotFoundError` … `site-packages/config.yaml` | Config root resolved to pip install path | Set `MCP_HOOKER_ROOT=/app` in Docker; mount config at `/app/config.yaml` |
| `api.base_url is not set...` | Spec lacks `servers` | Set `api.base_url` explicitly |
| `401` on tool calls | Missing upstream auth | Gateway not injecting token, or wrong `api.headers` / `${ENV}` |
| `421 Misdirected Request` on `/mcp` | FastMCP Host guard | Set `server.host_origin_protection: false`, or use `localhost` URL, or add host to `allowed_hosts` |
| `500` … `Task group is not initialized` | Inner FastMCP lifespan not running | Fixed in current code — rebuild image; ensure you are not on an old `mcp_hooker` wheel |
| `503 MCP server not initialized` | Startup reload failed | Check logs; fix spec URL or network |
| MCP client can't connect | Wrong URL | Use `http://localhost:8000/mcp` (not `0.0.0.0`) |
| Reload doesn't pick up changes | Wrong file mounted | Verify volume path; call `/admin/reload` |
| Empty tools after reload | Invalid/partial spec | Validate OpenAPI; check FastMCP logs |
| Caflou calls fail with 401 | Token missing/expired/wrong scope | Regenerate token in Caflou API settings; verify gateway injects Bearer header |
| Caflou 404 on resources | Wrong `account_id` in tool args | Pass correct account ID in path parameters |
| **Every Caflou tool call 404s with doubled `/api/v1/api/v1/...`** | `api.base_url` includes `/api/v1`, but spec paths already start with `/api/v1/{account_id}/...` | Set `api.base_url` to **host only** (`https://app.caflou.com`). mcp-hooker logs a `WARNING` at startup/reload when it detects this |
| Kong `404 no Route matched` + `Via: kong`, low upstream latency | Upstream host is loopback (`127.x.x.x`) — Kong proxies to itself | Set service host to **container name** on shared Docker network (e.g. `caflou-app-1`) |
| Kong `502` / `invalid response from upstream` | Wrong internal port, network isolation, or buffering | Use container port **8000** not host **3003**; `docker network connect caflou_internal kong`; disable `proxy_buffering` in Kong nginx snippet |
| Kong `401` with valid-looking setup | Missing `apikey` header | Clients must send `apikey: <key>` (default key-auth header name) |
| Kong `403` with valid key | Consumer not in route's ACL group | Add ACL credential on consumer matching route `allow` list |
| nginx `404` from `nginx/1.18` (not Kong) | `location /caflou` not in `listen 443 ssl` block | Move location into SSL server block; `sudo nginx -t && sudo systemctl reload nginx` |
| CORS error from MCP Inspector | Browser preflight blocked | Add Kong **CORS** plugin on route; allow `http://localhost:6274`, methods `GET,POST,OPTIONS`, headers `apikey,Content-Type,Accept` |
| Public IP timeout from LAN | NAT hairpin not supported | Test with `--resolve mcp-hooker.catania-service.cz:443:127.0.0.1` or from external network |
| `Connection refused` from Kong to app | Containers on different Docker networks | `docker network connect <app_network> kong`; verify with `</dev/tcp/container-name/8000>` inside Kong |
| MCP client connects to `/caflou` not `/caflou/mcp` | App mounts MCP at `/mcp`; Kong strips route prefix | Use full URL `…/caflou/mcp` in client config |
| `Not Acceptable: Client must accept text/event-stream` | Missing Accept header | Add `Accept: application/json, text/event-stream` (streamable HTTP) or `text/event-stream` (SSE) |
| FastMCP output validation error on list tool | Spec says array; API returns `{page, results}` | Enable `openapi.sanitizer.paginated_lists`; or `validate_output: false` |
| **`Application startup failed` with mass Pydantic errors on `prev_page`/`next_page` schemas** | Sanitizer used `type: ["integer", "null"]` (JSON Schema unions) | Use minimal OpenAPI 3.0 envelope only (`page`, `results`, `additionalProperties: true`); rebuild + recreate |
| List tool returns data in error text: `None is not of type 'string'` | Item schema says `string`; API sends `null` (e.g. `first_name`) | `validate_output: false` or relax nullable fields in patch/sanitizer |
| List tool: `{…user object…} is not of type 'string'` | `AccountUser.user` typed as `string` in spec; API returns nested object | `validate_output: false` |
| `List_Accounts` has awkward name or missing from Cursor | Upstream spec lacks `operationId`; or Cursor schema parse failed on another tool | Patch with `operationId: List_Accounts`; fix sanitizer (`replace_generic` for `TaskTodo` ref) |
| Unresolved `#/components/schemas/...` warnings at startup | Recursive or cyclic local refs in response schemas | `sanitizer.on_unresolved: preserve` or `replace_generic` |
| Cursor stuck on "loading tools" then **0 tools** | Cursor fails parsing tool offerings (strict JSON Schema resolver) | Check `~/.config/Cursor/logs/.../mcp-server-user-mcp-hooker-caflou.log` for `can't resolve reference`; enable sanitizer `replace_generic` |
| MCP Inspector lists tools but Cursor does not | Inspector tolerates broken `outputSchema` refs; Cursor does not | Same as above — server may be healthy while Cursor UI listing fails |
| Cursor agent doesn't list mcp-hooker tools | `mcp.json` not reloaded or old chat session | Reload Cursor window; start new chat; verify `url` ends in `/mcp` |
| Patch enabled in config but startup `FileNotFoundError: …/patch.yaml` | Only `config.yaml` mounted; patch file left on host | Mount instance config dir to `/app/instance-config`; set `MCP_HOOKER_CONFIG_FILES=/app/instance-config/config.yaml` |
| Sanitizer enabled but `List_TaskTodos` still has broken `$ref` | Old image running; `/admin/reload` does not load new Python code | Rebuild image, `docker pull`, `docker compose up -d --force-recreate` |
| Tool count still 477 after adding patch | Patch overlays existing path; does not add a new operation | Expected — verify patch via tool description/`operationId`, not count |
| `tools/list` shows 477 tools but calls fail | Separate issues: MCP layer OK, upstream broken | Check `CAFLOU_API_TOKEN`, `api.base_url`, `{account_id}` in args |
| Startup `WARNING` about doubled `/api/v1` | `api.base_url` has path prefix spec paths already include | Set host-only base URL; reload |
| Patch file not found in Docker | Path resolved relative to config dir; only `config.yaml` mounted | Mount `instances/<name>/` → `/app/instance-config`; set `MCP_HOOKER_CONFIG_FILES=/app/instance-config/config.yaml` |
| Kong route 404, latency 0 | Request never matched a route | Check route **path** field, not route name; check Host/header constraints |
| `502` after fixing upstream host | Kong buffering streamable HTTP | Add `proxy_buffering off` snippet; recreate Kong container |

### Validate OpenAPI locally

```bash
python -c "
import asyncio
from mcp_hooker.spec_loader import load_openapi_spec
print(asyncio.run(load_openapi_spec())['info'])
"
```

### Smoke-test MCP endpoint

```bash
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}'
```

Expect HTTP `200` and a session ID in logs.

---

## 17. Decision log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-07-07 | yayaya for config | Same pattern as myskin; layered YAML + env expansion |
| 2026-07-07 | FastMCP `from_openapi` | Native OpenAPI → MCP tool mapping; YAML spec support via parser |
| 2026-07-07 | In-process hot reload | Watcher pattern; avoids container restart for spec churn; restarts inner lifespan |
| 2026-07-07 | One spec per process (not `mount()` orchestrator) | Simpler ops, independent subdomains, matches zpl-mcp fleet patterns |
| 2026-07-07 | GitLab CI mirrors zpl-mcp | Consistent registry push workflow in catania_dev fleet |
| 2026-07-07 | Swagger Petstore default spec | Smoke-testable public OpenAPI + API |
| 2026-07-07 | `MCP_HOOKER_ROOT` for Docker | Config paths must not resolve under `site-packages` after `pip install` |
| 2026-07-07 | Explicit FastMCP inner lifespan | Required for streamable HTTP session manager when mounting ASGI app |
| 2026-07-07 | `host_origin_protection: false` default | Docker/LAN MCP clients send non-loopback Host headers |
| 2026-07-07 | Gateway for upstream auth | Keeps mcp-hooker free of long-lived API secrets (e.g. Caflou Bearer token) |
| 2026-07-07 | HTTP transport (not stdio) | Background reload tasks need a long-lived process |
| 2026-07-07 | Repo name `mcp-hooker` | Hooks APIs into MCP; author accepts discoverability trade-offs |
| 2026-07-08 | nginx → Kong → mcp-hooker on jbi-sv-00 | TLS at nginx; Kong key-auth + ACL + CORS; path prefix `/caflou` → `/mcp_hooker_caflou` |
| 2026-07-08 | Kong upstream = container name :8000 | `127.17.0.1` typo caused self-proxy loop; host port 3003 ≠ container port |
| 2026-07-08 | Client URL must include `/mcp` | FastMCP mounts at `/mcp`; Kong `strip_path` maps route prefix to upstream root |
| 2026-07-08 | Kong ACL per route (OSS) | Prevents one API key from accessing all mcp-hooker instances |
| 2026-07-08 | `CAFLOU_API_TOKEN` in mcp-hooker env | No upstream gateway on jbi-sv-00; Bearer injected via `api.headers` |
| 2026-07-08 | `api.base_url` host-only for Caflou | Spec paths include `/api/v1/`; doubled prefix caused 404 |
| 2026-07-09 | Response schema sanitizer | Caflou spec wrong on paginated lists and local refs; fix before FastMCP |
| 2026-07-09 | `validate_output: false` for Caflou | Live API responses diverge from published schemas too often for strict validation |
| 2026-07-09 | `paginated_lists` minimal OpenAPI 3.0 envelope | Over-specified envelope with JSON Schema unions crashed FastMCP at **spec ingest** |
| 2026-07-09 | `openapi.patch_files` for spec overlays | Upstream Caflou spec has bare `/api/v1/accounts`; patch adds `List_Accounts` + safe schema |
| 2026-07-09 | Patch paths relative to primary config file | Matches spliffy instance layout; avoids `MCP_HOOKER_ROOT` ambiguity |
| 2026-07-09 | `sanitizer.on_unresolved: replace_generic` for Cursor | Unresolved `#/$defs/TaskTodo` in `List_TaskTodos` broke Cursor tool discovery |
| 2026-07-09 | Instance config dir mount in Docker | `config.yaml` alone is insufficient when `patch_files` is set |
| 2026-07-09 | Startup warning on doubled base path | Catch `api.base_url` footgun before tool calls fail |
| 2026-07-10 | Generic `openapi.tools_filter` (`route_filters.py`) | Per-instance YAML rules; no upstream hardcoding; FastMCP `route_map_fn` |
| 2026-07-10 | Caflou Profile A `tools_filter.yaml` | **477 → 212** tools for Claude Web cap; exclude UI/email/chat/bank chrome |
| 2026-07-10 | Remove infra overlay bind-mounts | Overlay overrode image `server.py`; filter config alone was insufficient until image + compose sync |
| 2026-07-10 | `/health` `tool_count` as deploy gate | `curl 127.0.0.1:3003/health` must show 212 after Profile A deploy |
| 2026-07-10 | `scripts/caflou_sync_mcp_project.py` | Sync Caflou project `615010` tasks/progress from runbook knowledge |

---

## 18. Lessons learned & incident playbook

Consolidated knowledge from building and debugging **mcp-hooker-caflou** (2026-07-07 → 2026-07-09). Read this before standing up the next instance.

**Quick jump (2026-07-09 agent session):**

- [Paginated lists — three-layer mismatch & startup crash](#paginated-lists-three-layer-mismatch-2026-07-09)
- [Caflou MCP tool names (`List_21`, `Create_13`, …)](#caflou-mcp-tool-names-opaque-operationids)
- [Search limitations & client-side filtering](#caflou-search-limitations)
- [Verified agent workflows (users, tasks, projects)](#agent-workflow-recipes-verified-2026-07-09)
- [Profile A tools filter & overlay removal (2026-07-10)](#profile-a-tools-filter--overlay-removal-2026-07-10)

### Three auth layers (do not mix them up)

```
Layer 1: MCP client → Kong          apikey header (Kong key-auth + ACL)
Layer 2: Kong → mcp-hooker          (none today — trusted Docker network)
Layer 3: mcp-hooker → Caflou API    Authorization: Bearer ${CAFLOU_API_TOKEN}
```

Fixing layer 1 does not fix layer 3. `initialize` + `tools/list` only exercise layer 1–2; tool calls hit layer 3.

### URL construction (the `/api/v1` trap)

```
httpx final URL = api.base_url + operation.path
```

Caflou spec: `servers[0].url = https://app.caflou.com`, paths = `/api/v1/{account_id}/...`.

| `api.base_url` | Operation path | Result |
|----------------|----------------|--------|
| `https://app.caflou.com` | `/api/v1/123/foo` | ✅ `https://app.caflou.com/api/v1/123/foo` |
| `https://app.caflou.com/api/v1` | `/api/v1/123/foo` | ❌ `…/api/v1/api/v1/123/foo` |

**Symptom:** MCP handshake fine, every tool call 404. **Fix:** host-only `base_url`. mcp-hooker warns at startup if it detects the pattern.

### OpenAPI spec ≠ live API (Caflou)

| What the spec says | What the API does | mcp-hooker mitigation |
|--------------------|-------------------|----------------------|
| List response: `type: array` | Returns `{page, results, prev_page, total_results, …}` | `sanitizer.paginated_lists` |
| `GET /api/v1/accounts` present but bare (no `operationId`, weak schema) | Endpoint works; tool name/description poor for agents | `patch_files` → `examples/caflou.accounts.patch.yaml` |
| `List_TaskTodos` outputSchema has `#/$defs/TaskTodo` without `$defs` | Cursor stuck on "loading tools" or shows **0 tools** | `sanitizer.on_unresolved: replace_generic` |
| Strict output schemas | Extra fields in live responses | `validate_output: false` |
| Unresolved cyclic `#/components/schemas/...` refs | FastMCP warnings | `sanitizer.on_unresolved` |

**Lesson:** auto-generated tools from a remote spec are a bootstrap, not a guarantee. Budget time for patches + sanitizer tuning per upstream API.

### Kong + nginx + Docker (production pattern)

**External URL anatomy:**

```
https://mcp-hooker.catania-service.cz/caflou/mcp
  │                              │      └── FastMCP mount point (required)
  │                              └── nginx external prefix
  └── TLS termination at nginx
```

**Internal rewrite:**

```
/caflou/mcp  →  nginx rewrite  →  /mcp_hooker_caflou/mcp  →  Kong :8040
  →  strip_path  →  caflou-app-1:8000/mcp
```

**Rules learned the hard way:**

1. nginx `location` must be in the **`listen 443 ssl`** block.
2. Kong route **path** (`/mcp_hooker_caflou`) ≠ route **name** (`mcp-hooker-caflou`).
3. Kong service **host** = Docker container name on a **shared network**, never `127.x.x.x` or `localhost`.
4. Kong service **port** = container internal port (`8000`), not host-mapped port (`3003`).
5. Kong `strip_path=true` strips the route prefix; client must still include `/mcp` because that's the app mount, not the Kong route prefix.
6. Kong OSS **ACL** on the route prevents key reuse across instances.
7. Kong **CORS** required for browser-based MCP Inspector.
8. Kong image has no `curl`/`wget` — use `bash </dev/tcp/host/port>` for connectivity checks.
9. Recreate Kong after `KONG_NGINX_HTTP_CONFIGURATION_SNIPPET` changes — reload is not enough.

### MCP client registration (Cursor)

```json
"mcp-hooker-caflou": {
  "url": "https://mcp-hooker.catania-service.cz/caflou/mcp",
  "headers": { "apikey": "…" }
}
```

After editing `~/.cursor/mcp.json`: save → reload Cursor → **new chat**. Existing sessions keep the old tool list.

### Debugging order (when something breaks)

Work outside-in:

1. **Container alive?** `curl http://127.0.0.1:3003/health`
2. **MCP handshake?** `curl -X POST …/mcp` with `initialize` (direct or via Kong)
3. **Kong auth?** 401 → key missing; 403 → ACL wrong; 404 latency 0 → route mismatch
4. **Kong → container?** `docker exec kong bash -c '</dev/tcp/caflou-app-1/8000'`
5. **Upstream token?** `CAFLOU_API_TOKEN` set; `cfg_headers()` shows Bearer
6. **Upstream URL?** no doubled `/api/v1`; check startup WARNING
7. **Tool call args?** `{account_id}` present and correct
8. **Output validation?** sanitizer / `validate_output` settings

### Red herrings (don't waste time)

| Observation | Not actually broken | Real explanation |
|-------------|---------------------|------------------|
| Public IP timeout from LAN | Server down | NAT hairpin unsupported on router |
| 401 without key, 404 with key | Inconsistent Kong | 401 = key-auth stops before upstream loop; 404 with key = Kong self-proxy bug |
| `502` + low upstream latency (~2 ms) | Slow upstream | Kong reached itself or got immediate rejection |
| `tools/list` returns 477 tools | Upstream auth works | Tool list is built from OpenAPI spec, not live API calls |
| Direct `curl localhost:3003/mcp` without Accept header | Server broken | MCP requires `Accept: application/json, text/event-stream` |
| MCP Inspector shows 477 tools, Cursor shows 0 | Server down | Cursor failed during **UI tool listing** (`listOfferingsForUI`), often schema refs |
| Enabled sanitizer in config but no effect | Config not loaded | Container still on old **image**; `/admin/reload` does not hot-load new Python modules |
| Added `patch.yaml`, expected 478 tools | Patch not applied | `/api/v1/accounts` already in upstream spec — patch **overlays** metadata, count stays 477 |
| `docker pull` done but behavior unchanged | Latest image running | Running container still uses old image ID until `--force-recreate` |

### Cursor MCP client failures (2026-07-09 bring-up)

**Symptom:** Cursor connects to `mcp-hooker-caflou`, shows "loading tools", then **0 tools** (or errored). MCP Inspector on the same URL lists **477 tools** fine.

**Root cause:** Cursor's MCP client validates tool schemas when building the UI offering list. One tool — `List_TaskTodos` — had:

```json
"outputSchema": {
  "properties": {
    "result": {
      "items": { "$ref": "#/$defs/TaskTodo" }
    }
  }
}
```

FastMCP emitted a `$ref` to `#/$defs/TaskTodo` but did not include matching `$defs` in the tool schema. MCP Inspector tolerated it; Cursor did not.

**Cursor log location (Linux):**

```text
~/.config/Cursor/logs/<session>/mcp-server-user-mcp-hooker-caflou.log
~/.config/Cursor/logs/<session>/mcpprocess.log
```

**Smoking-gun line:**

```text
listOfferingsForUI sub-call failed: tools(...): can't resolve reference #/$defs/TaskTodo from id #
```

Earlier lines usually show `Successfully connected to streamableHttp server` — transport and Kong auth are fine; failure is **client-side schema parsing**, not connectivity.

**Fix that worked:**

```yaml
openapi:
  sanitizer:
    enabled: true
    on_unresolved: replace_generic
```

After deploy, `List_TaskTodos` `outputSchema` became a generic object and Cursor loaded all tools.

**Important:** changing sanitizer code or defaults requires a **new container image**. `POST /admin/reload` re-reads config and re-fetches the OpenAPI spec but does **not** reload Python modules already installed in the running image.

**Verify fix on live server** (no Cursor needed):

```bash
# initialize + tools/list, then inspect List_TaskTodos outputSchema
python - <<'PY'
import json, re, urllib.request
url = "https://mcp-hooker.catania-service.cz/caflou/mcp"
headers = {"apikey": "YOUR_KEY", "Content-Type": "application/json",
           "Accept": "application/json, text/event-stream"}
def post(p, sid=None):
    h = {**headers, **({"mcp-session-id": sid} if sid else {})}
    req = urllib.request.Request(url, data=json.dumps(p).encode(), method="POST", headers=h)
    with urllib.request.urlopen(req, timeout=60) as r:
        return dict(r.headers), r.read().decode()
_, _ = post({"jsonrpc":"2.0","id":1,"method":"initialize",
    "params":{"protocolVersion":"2024-11-05","capabilities":{},
              "clientInfo":{"name":"t","version":"1"}}})
sid = _[0]["mcp-session-id"]
_, body = post({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}, sid)
tools = json.loads(re.search(r"data: (\{.*\})", body, re.S).group(1))["result"]["tools"]
for t in tools:
    if t["name"] == "List_TaskTodos":
        print(json.dumps(t.get("outputSchema"), indent=2)); break
PY
```

Good: no `"$ref": "#/$defs/TaskTodo"`. Bad: still present → old image or sanitizer disabled.

### Instance config vs runtime data (Docker)

Production uses the spliffy-style split:

| What | Host path | Container path | Purpose |
|------|-----------|----------------|---------|
| **Runtime** (secrets, mutable) | `/var/www/mcp-hooker/<project>/` | `/instance` | `.env` with `CAFLOU_API_TOKEN`, `RELOAD_TOKEN` |
| **Instance config** (read-only) | `deploy/.../instances/<name>/` | `/app/instance-config` | `config.yaml`, `patch.yaml`, … |

**Do not** put `.env` in the instance config bind — keep it in the runtime dir loaded via `env_file`.

**Compose anchors (reference):**

```yaml
x-mcp-hooker-instance-volumes: &mcp-hooker-instance-volumes
  - ${MCP_HOOKER_RUNTIME_BASE:-/var/www/mcp-hooker}/${COMPOSE_PROJECT_NAME}:/instance:rw
  - type: bind
    source: ./instances/${COMPOSE_PROJECT_NAME}
    target: /app/instance-config
    read_only: true

x-app-env: &app-env
  MCP_HOOKER_ROOT: /app
  MCP_HOOKER_CONFIG_FILES: /app/instance-config/config.yaml
```

**Trial-and-error gotcha:** mounting only `config.yaml` → `/app/config.yaml` works until `openapi.patch_files` is set. With `patch_files: [patch.yaml]`, the app resolves `patch.yaml` relative to the config directory (`/app/` or `/app/instance-config/`). If the patch file is not mounted there, startup fails:

```text
FileNotFoundError: OpenAPI patch file not found: /app/patch.yaml
(resolved relative to config directory /app)
```

**Verify inside container:**

```bash
docker compose exec app ls -la /app/instance-config/
docker compose exec app sh -lc 'echo "$MCP_HOOKER_CONFIG_FILES"'
docker compose exec app cat /app/instance-config/config.yaml | grep -A2 patch_files
```

### OpenAPI patches: overlay vs new tool

**Expectation trap:** "I added a patch endpoint, tool count should go from 477 → 478."

**Reality:** Caflou's upstream spec **already includes** `GET /api/v1/accounts`. The patch deep-merges into that path — it does not create a second operation. Tool count stays **477**.

**What the patch still fixes:**

| Field | Upstream spec | After patch |
|-------|---------------|-------------|
| `operationId` | *(missing)* | `List_Accounts` |
| `summary` | `List` | `List accounts` |
| `description` | generic | agent-friendly text |
| `outputSchema` | bare `200 OK` | explicit `{result: [...]}` envelope |

**Verify patch applied** — compare live tool metadata, not count:

```bash
# tools/list → List_Accounts description should match patch.yaml, not upstream "Get list of..."
```

**Call patched tool** (discover `{account_id}` for all other tools):

```bash
# MCP tools/call
{"name": "List_Accounts", "arguments": {}}
```

Example response shape:

```json
{"result": [{"name": "…", "id": "958a30da…", "role": "collaborator"}]}
```

Use the returned `id` as `account_id` in paths like `List_ToDos`, `List_Projects`, etc.

### Deploy vs reload (when each is enough)

| Change | `/admin/reload` or SIGHUP | Rebuild image + recreate container |
|--------|---------------------------|-------------------------------------|
| `config.yaml` values | ✅ | optional |
| `patch.yaml` content (mounted volume) | ✅ | not needed |
| Remote OpenAPI spec updated | ✅ (re-fetches URL) | not needed |
| New Python in `mcp_hooker/` (sanitizer, patch loader, …) | ❌ | **required** |
| New pip dependency / FastMCP version | ❌ | **required** |

**Verify running image is actually new:**

```bash
docker inspect registry…/mcp-hooker:latest --format '{{.Id}} {{.Created}}'
docker inspect <container> --format '{{.Image}}'
```

IDs must match after `docker pull` **and** `docker compose up -d --force-recreate`. `docker pull` alone does not restart the running container.

### MCP client debugging order (Cursor-specific)

When Inspector works but Cursor does not:

1. Read `mcp-server-user-mcp-hooker-caflou.log` — look for `listOfferingsForUI` and `can't resolve reference`
2. Probe live `tools/list` over HTTP — confirm server returns tools
3. Inspect offending tool `outputSchema` (often `List_TaskTodos`)
4. Enable/fix sanitizer; **rebuild and recreate** container
5. Reload Cursor MCP server in settings; restart Cursor if cached error persists
6. Start a **new chat** — old sessions may retain stale tool snapshots

When both Inspector and Cursor fail at connection:

1. Kong `apikey` header present?
2. URL ends with `/caflou/mcp`?
3. Container healthy? `curl …/health`
4. Patch file mounted? (see above)

### Paginated lists: three-layer mismatch (2026-07-09)

Caflou list endpoints (`GET` with `page` / `per` query params) fail in **three stacked ways**. Fix them in order — fixing layer 1 alone still leaves layer 2 or 3 broken.

```
Layer A — Published OpenAPI spec
  200 response schema: type: array
  items: { $ref: '#/components/schemas/AccountUser' }

Layer B — FastMCP OpenAPI ingestion
  Wraps non-object response schemas for MCP:
  { "result": [ ... ], "x-fastmcp-wrap-result": true }

Layer C — Live Caflou API
  Returns paginated object:
  { "page": 1, "prev_page": null, "next_page": null,
    "results": [ ... ], "total_pages": 1, "unread_object_ids": [...] }
```

**Symptom at layer A→C (before sanitizer):**

```text
Output validation error: {'page': 1, 'results': [...], ...} is not of type 'array'
```

The error payload often **contains the real data** — the call succeeded upstream; only MCP output validation failed.

**Fix layer A→C:** `openapi.sanitizer.paginated_lists.enabled: true` rewrites eligible GET list responses from bare arrays to:

```yaml
type: object
additionalProperties: true
properties:
  page: { type: integer }
  results: { type: array, items: <original items schema> }
required: [page, results]
```

**Trial-and-error — v1 envelope broke startup (not tool calls):**

First implementation added explicit `prev_page` / `next_page` with JSON Schema union types:

```yaml
prev_page: { type: ["integer", "null"] }   # ❌ do not use
next_page: { type: ["integer", "null"] }   # ❌ do not use
```

**Result:** container never became healthy. FastMCP's OpenAPI parser (Pydantic) rejected the **entire spec at startup** — hundreds of errors like:

```text
Application startup failed. prev_page.type
  Input should be 'string', 'number', 'integer', 'boolean', 'object' or 'array'
  [type=literal_error, input_value=['integer', 'null'], input_type=list]
```

This is **not** a runtime tool-call failure; it happens during `FastMCP.from_openapi()` before any MCP session exists.

**Fix that worked (v2 minimal envelope):** only `page` + `results` in `properties`; set `additionalProperties: true` so live fields (`prev_page`, `next_page`, `total_pages`, `unread_object_ids`, …) pass validation without being declared. No JSON Schema union types anywhere in the sanitizer output.

**After pagination fix — layer B item schemas still fail:**

With `paginated_lists` only, envelope validation passes but **per-item** validation still fails on real data:

| Field / path | Spec says | API returns | Example error |
|--------------|-----------|-------------|---------------|
| `first_name` | `string` | `null` | `None is not of type 'string'` |
| `AccountUser.user` | `string` | nested `{id, name, …}` object | `{…} is not of type 'string'` |

**Pragmatic production fix:** `openapi.validate_output: false` (implemented in `mcp_hooker/server.py` → `FastMCP.from_openapi(validate_output=…)`). Logs a warning when disabled.

**Verification matrix (2026-07-09, account `958a30da1d8c2ccb0a3b6194`):**

| Tool | Before fixes | After `paginated_lists` only | After `validate_output: false` |
|------|--------------|------------------------------|--------------------------------|
| `Online` | ✅ | ✅ | ✅ |
| `Get_19` (single resource) | ✅ | ✅ | ✅ |
| `List_21` (users) | ❌ array vs object | ❌ field-level | ✅ |
| `List_AccountUsers` | ❌ array vs object | ❌ field-level | ✅ |

**Future hardening (optional):** extend sanitizer or patches for nullable strings and nested `$ref` objects so `validate_output` can be re-enabled.

**Red herring:** Cursor cached tool descriptors may still show `outputSchema` with `result` + `x-fastmcp-wrap-result` even after the sanitizer rewrites the underlying OpenAPI response schema. Trust live `tools/call` results and server logs, not stale client-side schema snapshots.

### Caflou MCP tool names (opaque `operationId`s)

The upstream Caflou OpenAPI spec assigns auto-generated `operationId` values (`List_21`, `Create_13`, …). MCP tool names match these exactly. There is **no** stable human-readable alias unless you patch `operationId` in `patch_files`.

**Commonly used tools (verified 2026-07-09):**

| MCP tool | HTTP (approx.) | Purpose |
|----------|----------------|---------|
| `List_Accounts` | `GET /api/v1/accounts` | Discover `account_id` — **requires** `patch.yaml` |
| `List_21` | `GET /api/v1/{account_id}/users` | List account users |
| `List_AccountUsers` | `GET /api/v1/{account_id}/account_users` | Account-user links (includes nested `user`) |
| `List_14` | `GET /api/v1/{account_id}/projects` | List projects (paginated) |
| `List_3` | `GET /api/v1/{account_id}/companies` | List companies |
| `List_15` | `GET /api/v1/{account_id}/tasks` | List tasks |
| `Create_13` | `POST /api/v1/{account_id}/projects` | Create project |
| `Create_14` | `POST /api/v1/{account_id}/tasks` | Create task |
| `Online` | `GET /api/v1/{account_id}/online` | Lightweight connectivity / auth smoke test |
| `Search_2` | search endpoint | **Broken for agents** — see below |

**Discover the full map:** `tools/list` on the MCP server (477 tools for Caflou). Cross-reference `summary` / `description` fields or the OpenAPI spec paths.

### Caflou search limitations

**Do not rely on `Search_2` (or other search tools) for agent text search today.**

- Caflou exposes search endpoints in the OpenAPI spec.
- The generated MCP tool schema often exposes only `account_id` — **no** `q`, `query`, or filter text parameter is wired through to the agent.
- Calling `Search_2` with just `account_id` returns **empty** results even when data exists.

**Workaround used in production debugging:**

1. Call the relevant list tool (`List_14` projects, `List_3` companies, `List_21` users, …).
2. Paginate with `page` and `per` (up to 1000; mind rate limits).
3. Filter matches **client-side** in the agent (name, description, custom fields).

Example: finding chatbot/voicebot projects — paginate `List_Projects`, filter `name` / `description` for `chatbot`, `voicebot`, `voice bot`, etc.

### Profile A tools filter & overlay removal (2026-07-10)

**Problem:** Claude Web indexes ~256 MCP tools. Caflou's patched spec still exposes **477** operations (business + UI chrome). Agents could not reliably find `List_Accounts` and similar tools buried late in the list.

**Solution:**

1. Generic engine: `mcp_hooker/route_filters.py` — reads `openapi.tools_filter` from config.
2. Instance rules: `instances/caflou/tools_filter.yaml` — Profile A trims to **212** business tools.
3. Verify: `GET /health` → `"tool_count": 212` on `127.0.0.1:3003`.

**Overlay incident:** Config had `tools_filter.enabled: true` but production still served 477 because `infra-files/overlay/mcp_hooker/server.py` bind-mounted over the Docker image and did not wire `route_filters`. **Fix:** remove overlay volumes; `docker compose pull && up -d --force-recreate`.

**Caflou project tracking:** project `615010` (*Caflou MCP*) — sync tasks/descriptions from runbooks:

```bash
export MCP_CAFOU_APIKEY='<kong-key>'   # or MCP_CAFOU_BEARER='<jwt>'
python3 scripts/caflou_sync_mcp_project.py
```

See also: `infra-files/docs/mcp-hooker/jbi-sv-00_caflou-mcp-tooling.md`.

### Agent workflow recipes (verified 2026-07-09)

Account used in all examples: **CATANIA GROUP s.r.o.** — `account_id: 958a30da1d8c2ccb0a3b6194`.

#### 0. Smoke test (no `account_id` needed for some tools)

```json
{"name": "Online", "arguments": {"account_id": "958a30da1d8c2ccb0a3b6194"}}
```

If this fails with `401`, fix `CAFLOU_API_TOKEN` before debugging list tools.

#### 1. Discover `account_id` (requires patch)

```json
{"name": "List_Accounts", "arguments": {}}
```

Example response:

```json
{"result": [{"name": "CATANIA GROUP s.r.o.", "id": "958a30da1d8c2ccb0a3b6194", "role": "collaborator"}]}
```

Without `patch.yaml` mounted, this tool is missing or has a useless name — you must recover `account_id` from Caflou UI, API settings, or a prior session.

**Nested route path params:** use OpenAPI names from the tool schema — e.g. `List_Tasks_To_Dos` needs `task_id`, not `task_id__path`. Wrong names are ignored by FastMCP; the URL keeps literal `{task_id}` and Caflou may return **429**. `List_Tasks` accepts optional `filter` (see `patch.yaml`); if omitted, paginate and filter `project_id` client-side.

#### 2. Find a user by name

```json
{"name": "List_21", "arguments": {"account_id": "958a30da1d8c2ccb0a3b6194", "page": 1, "per": 100}}
```

Filter `results` for display name or email. **Verified:** Karel Matějovský — `id: 68570`, `email: matejovsky@catania.cz`.

`List_AccountUsers` also works but returns join records with a nested `user` object (triggers field-level validation errors unless `validate_output: false`).

#### 3. List / filter projects

```json
{"name": "List_14", "arguments": {"account_id": "958a30da1d8c2ccb0a3b6194", "page": 1, "per": 100}}
```

Paginate until `next_page` is null. Filter client-side.

#### 4. Create a task

```json
{
  "name": "Create_14",
  "arguments": {
    "account_id": "958a30da1d8c2ccb0a3b6194",
    "body": {
      "name": "Testing task (MCP)",
      "project_id": 614979
    }
  }
}
```

**Verified:** task id `2165037` created via MCP (2026-07-09). Adjust `body` fields per OpenAPI `Create_14` request schema.

#### 5. Create a project under a company

1. Resolve `company_id` via `List_3` (filter for company name).
2. Create project:

```json
{
  "name": "Create_13",
  "arguments": {
    "account_id": "958a30da1d8c2ccb0a3b6194",
    "body": {
      "name": "Voicebot interní",
      "company_id": 1740315
    }
  }
}
```

**Verified:** project id `614979` under **CATANIA GROUP s.r.o.** (`company_id: 1740315`) — the only company matching `CATANIA*` in that account (2026-07-09).

**Trial-and-error:** do not guess `company_id` from account id — companies are a separate entity list.

#### 6. End-to-end agent checklist

1. `List_Accounts` → `account_id`
2. `Online` → token OK
3. Target list tool with `page`/`per` → client-side filter
4. Mutations (`Create_*`, `Update_*`) → read request schema from `tools/list` or OpenAPI spec
5. If output validation errors persist after deploy → confirm `validate_output: false` **and** new image running (`--force-recreate`)

### Minimal working Caflou config (copy-paste reference)

```yaml
server:
  name: caflou-mcp
  host_origin_protection: false

openapi:
  spec: https://app.caflou.com/api/v1/i/docs/openapi/v1/openapi.yaml
  validate_output: false
  patch_files:
    - patch.yaml          # relative to config file dir (e.g. /app/instance-config/)
  sanitizer:
    enabled: true
    on_unresolved: replace_generic   # required for Cursor (List_TaskTodos $ref issue)
    paginated_lists:
      enabled: true
      items_key: results

api:
  base_url: https://app.caflou.com
  headers:
    Authorization: "Bearer ${CAFLOU_API_TOKEN}"
```

```bash
export CAFLOU_API_TOKEN="…"
# Mount instances/caflou/ → /app/instance-config; see §18
docker compose up -d --force-recreate
curl -s http://localhost:8000/health | jq
```
