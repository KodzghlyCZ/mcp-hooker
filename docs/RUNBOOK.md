# mcp-hooker — Operations Runbook

> Maintainer reference for **what** this service does, **how** it is configured, and **how** to operate it in production.
>
> Last updated: 2026-07-08

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

api:
  base_url: https://app.caflou.com
  headers:
    Authorization: "Bearer ${CAFLOU_API_TOKEN}"

reload:
  token: "${RELOAD_TOKEN}"
```

```bash
export CAFLOU_API_TOKEN="…"   # from Caflou Settings → Account settings → API
docker compose up -d --build
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

See [§6](#6-production-deployment-caflou-jbi-sv-00) for the live **mcp-hooker-caflou** stack on jbi-sv-00 (nginx → Kong → container).

---

## 6. Production deployment: Caflou (jbi-sv-00)

**Status:** Fixed 2026-07-08. MCP responds end-to-end (**477 tools**) at `https://mcp-hooker.catania-service.cz/caflou/mcp`.

Infra layout follows `deploy/mcp-hooker/instances/<name>/` (spliffy pattern). Live Caflou instance: Docker project `caflou`, container `caflou-app-1`, network `caflou_internal`.

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
