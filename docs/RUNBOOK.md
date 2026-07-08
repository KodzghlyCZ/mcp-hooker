# mcp-hooker — Operations Runbook

> Maintainer reference for **what** this service does, **how** it is configured, and **how** to operate it in production.
>
> Last updated: 2026-07-07

---

## Table of contents

1. [Executive summary](#1-executive-summary)
2. [Architecture](#2-architecture)
3. [Configuration](#3-configuration)
4. [Authentication](#4-authentication)
5. [Backend example: Caflou](#5-backend-example-caflou)
6. [Deployment](#6-deployment)
7. [Reload semantics](#7-reload-semantics)
8. [Observability](#8-observability)
9. [Security](#9-security)
10. [FastMCP & OpenAPI background](#10-fastmcp--openapi-background)
11. [Alternatives & tool landscape](#11-alternatives--tool-landscape)
12. [Multi-server topology & reverse proxy](#12-multi-server-topology--reverse-proxy)
13. [Project context & naming](#13-project-context--naming)
14. [OpenAPI spec quality](#14-openapi-spec-quality)
15. [Troubleshooting](#15-troubleshooting)
16. [Decision log](#16-decision-log)

---

## 1. Executive summary

**mcp-hooker** is a thin bridge:

1. Read YAML config ([yayaya](https://pypi.org/project/yayaya/))
2. Load an OpenAPI document (HTTP(S) URL or local JSON/YAML file)
3. Build a [FastMCP](https://gofastmcp.com/) server via `FastMCP.from_openapi()`
4. Expose it over **streamable HTTP** on a configurable port

Use it when you want an LLM/MCP client to call an existing REST API without hand-writing one MCP tool per endpoint.

**mcp-hooker is not an auth service.** It forwards HTTP calls to `api.base_url` using whatever headers you configure (or none, if an API gateway injects credentials upstream).

**Scope (v0.1):** one config → one OpenAPI spec → one MCP server per process. Multiple APIs = multiple mcp-hooker instances (typically one container per subdomain). See [§12](#12-multi-server-topology--reverse-proxy) for routing. FastMCP `mount()` orchestration is documented in [§10](#10-fastmcp--openapi-background) but is **out of scope** for this repo today.

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
| `api.base_url` | Upstream base URL; required when spec has no usable `servers[0].url` |
| `api.timeout` | httpx timeout for tool calls |
| `api.headers` | Extra request headers; values support `${ENV_VAR}` via yayaya |

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

**Production (gateway injects Bearer token):**

```yaml
server:
  name: caflou-mcp
  host: 0.0.0.0
  port: 8000
  host_origin_protection: false

openapi:
  spec: https://app.caflou.com/api/v1/i/docs/openapi/v1/openapi.yaml

api:
  base_url: https://your-gateway.example/caflou   # proxies to https://app.caflou.com
  headers: {}

reload:
  token: "${RELOAD_TOKEN}"
```

**Development (direct to Caflou, token in env):**

```yaml
api:
  base_url: https://app.caflou.com
  headers:
    Authorization: "Bearer ${CAFLOU_API_TOKEN}"
```

```bash
export CAFLOU_API_TOKEN="…"
docker compose up --build
```

### Caflou API notes

- Default list page size is 20; `per` query param can go up to 1000 (use carefully — rate limits apply).
- Filtering is supported via a `filter` object on list requests; see spec description for examples.
- Treat MCP tool access as **full API access** within the token's granted permissions.

---

## 6. Deployment

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

Terminate TLS at nginx, Traefik, Caddy, or your API gateway and forward to the container port. For **multiple APIs on different subdomains**, see [§12](#12-multi-server-topology--reverse-proxy).

MCP streamable HTTP benefits from:

- Long-lived connections allowed
- WebSocket upgrade if your proxy version requires it for SSE/streamable transports
- Expose `mcp-session-id` if browser clients need it (mcp-hooker CORS already lists it)

Health check target: `GET /health` → `{"status":"ok",...}`.

---

## 7. Reload semantics

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

## 8. Observability

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

## 9. Security

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
| Tool explosion | Large OpenAPI specs → hundreds of tools; prune or filter routes (see [§14](#14-openapi-spec-quality)) |
| Over-broad upstream tokens | Scope Caflou (and similar) API tokens to minimum permissions |
| No audit trail | Log tool calls at gateway or upstream API |

Run each instance in a **restricted container** without unnecessary host filesystem access. Containerize per [§6](#6-deployment).

---

## 10. FastMCP & OpenAPI background

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

## 11. Alternatives & tool landscape

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

## 12. Multi-server topology & reverse proxy

### Recommended production pattern

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

If subdomain sprawl becomes painful, an **MCP gateway** can aggregate upstream MCP servers behind one endpoint with centralized auth and observability. Trade-off: another component to operate. See [§11](#11-alternatives--tool-landscape).

### Spec refresh behind a proxy

With per-instance mcp-hooker, each service reloads its own spec (`reload.interval_seconds` or cron calling `/admin/reload`). The reverse proxy keeps accepting connections; **MCP clients reconnect** to pick up new tools after reload.

---

## 13. Project context & naming

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

## 14. OpenAPI spec quality

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

---

## 15. Troubleshooting

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

## 16. Decision log

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
