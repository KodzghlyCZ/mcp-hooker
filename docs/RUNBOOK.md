# mcp-hooker — Operations Runbook

> Maintainer reference for **what** this service does, **how** it is configured, and **how** to operate it in production.
>
> Last updated: 2026-07-07

---

## Table of contents

1. [Executive summary](#1-executive-summary)
2. [Architecture](#2-architecture)
3. [Configuration](#3-configuration)
4. [Deployment](#4-deployment)
5. [Reload semantics](#5-reload-semantics)
6. [Observability](#6-observability)
7. [Security](#7-security)
8. [Troubleshooting](#8-troubleshooting)
9. [Decision log](#9-decision-log)

---

## 1. Executive summary

**mcp-hooker** is a thin bridge:

1. Read YAML config ([yayaya](https://pypi.org/project/yayaya/))
2. Load an OpenAPI document (HTTP(S) URL or local JSON/YAML file)
3. Build a [FastMCP](https://gofastmcp.com/) server via `FastMCP.from_openapi()`
4. Expose it over **streamable HTTP** on a configurable port

Use it when you want an LLM/MCP client to call an existing REST API without hand-writing one MCP tool per endpoint.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ config.yaml (+ optional overlays via MCP_HOOKER_CONFIG_FILES)│
│   yayaya: merge, dot-path get(), ${ENV} expansion           │
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

**Process model:** single Python process, single uvicorn worker. Reload swaps the inner FastMCP ASGI app in place; in-flight MCP sessions may need to reconnect after a reload.

---

## 3. Configuration

### File resolution

1. If `MCP_HOOKER_CONFIG_FILES` is set → comma-separated list, merged left → right.
2. Else → `config.yaml`, then `config.local.yaml` if present.

Secrets belong in the environment and are referenced from YAML:

```yaml
api:
  headers:
    Authorization: "Bearer ${API_TOKEN}"
```

### Required fields

| Key | Notes |
|-----|-------|
| `openapi.spec` | URL or filesystem path |
| `api.base_url` | Required when the spec has no `servers[0].url` |

### Example: internal API with local spec

```yaml
server:
  name: billing-mcp
  host: 0.0.0.0
  port: 8080

openapi:
  spec: ./specs/billing.openapi.yaml

api:
  base_url: https://billing.internal.example
  headers:
    X-Api-Key: "${BILLING_API_KEY}"

reload:
  token: "${RELOAD_TOKEN}"
  interval_seconds: 300
  on_sighup: true
```

---

## 4. Deployment

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
  -v /path/to/config.yaml:/app/config.yaml:ro \
  -e API_TOKEN=secret \
  mcp-hooker
```

### Docker Compose

```bash
docker compose up -d --build
```

Override port: `MCP_HOOKER_PORT=9000 docker compose up`.

### GitLab CI/CD

Pipeline stage: **build**

- Runner tag: `mcp-hooker` (edit `.gitlab-ci.yml` if your fleet uses another tag)
- Builds and pushes `$CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA`
- On default branch, also pushes `:latest`

Same pattern as **zpl-mcp** (without the extra `zpl-adapter` build context).

### Reverse proxy

Terminate TLS in nginx/Traefik and forward to the container port. MCP streamable HTTP benefits from:

- Long-lived connections allowed
- WebSocket upgrade if your proxy version requires it for SSE/streamable transports

Health check target: `GET /health` → `{"status":"ok",...}`.

---

## 5. Reload semantics

| Trigger | Behavior |
|---------|----------|
| Startup | Always loads config + spec |
| `POST /admin/reload` | yayaya `reload_config()` → re-fetch spec → new FastMCP instance |
| `SIGHUP` | Same as HTTP reload when `reload.on_sighup: true` |
| `reload.interval_seconds > 0` | Background task repeats reload |

Reload steps:

1. Acquire async lock (concurrent reloads serialize)
2. Close previous `httpx.AsyncClient`
3. `reload_settings()` (yayaya re-reads YAML from disk)
4. Fetch/parse OpenAPI
5. `FastMCP.from_openapi(...)` and replace `state.mcp_app`

**Caveat:** MCP clients holding a session across reload should reconnect if tools disappear or change.

Protect `POST /admin/reload` in production by setting `reload.token`.

---

## 6. Observability

### Logs

Structured-ish INFO lines on reload:

```text
Reloaded MCP server (startup): spec=... base_url=... tools=N
```

Failures log stack traces at ERROR.

### Health

```bash
curl -s http://localhost:8000/health | jq
```

```json
{
  "status": "ok",
  "spec": "https://.../openapi.yaml",
  "base_url": "https://api.example.com"
}
```

### Manual reload

```bash
curl -X POST http://localhost:8000/admin/reload
# with token:
curl -X POST -H "Authorization: Bearer $RELOAD_TOKEN" http://localhost:8000/admin/reload
```

---

## 7. Security

| Topic | Guidance |
|-------|----------|
| Upstream credentials | Use `${ENV}` in `api.headers`; never commit secrets in YAML |
| Reload endpoint | Set `reload.token` in production |
| Network | Prefer private network between mcp-hooker and upstream API |
| CORS | Permissive (`*`) for MCP Inspector compatibility — tighten at reverse proxy if needed |
| OpenAPI source | Only load specs from trusted URLs/paths (SSRF risk on remote spec URL) |

mcp-hooker executes whatever operations the OpenAPI spec describes against `api.base_url` — treat tool access like direct API access.

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `ConfigFileNotFoundError` | Missing `config.yaml` | Mount/copy config in Docker; set `MCP_HOOKER_CONFIG_FILES` |
| `api.base_url is not set...` | Spec lacks `servers` | Set `api.base_url` explicitly |
| `401` on tool calls | Missing/wrong `api.headers` | Check `${ENV}` expansion and upstream auth |
| `503 MCP server not initialized` | Startup reload failed | Check logs; fix spec URL or network |
| MCP client can't connect | Wrong URL or port | Use `http://<host>:<port>/mcp` for streamable HTTP |
| Reload doesn't pick up changes | Wrong file mounted | Verify volume path; call `/admin/reload` |
| Empty tools after reload | Invalid/partial spec | Validate OpenAPI; check FastMCP logs |

### Validate OpenAPI locally

```bash
python -c "
import asyncio
from mcp_hooker.spec_loader import load_openapi_spec
print(asyncio.run(load_openapi_spec())['info'])
"
```

---

## 9. Decision log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-07-07 | yayaya for config | Same pattern as myskin; layered YAML + env expansion |
| 2026-07-07 | FastMCP `from_openapi` | Native OpenAPI → MCP tool mapping |
| 2026-07-07 | In-process hot reload | Avoid container restart for spec churn; good enough for config-driven hooks |
| 2026-07-07 | GitLab CI mirrors zpl-mcp | Consistent registry push workflow in catania_dev fleet |
| 2026-07-07 | Swagger Petstore default spec | Smoke-testable public OpenAPI + API |
