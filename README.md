# mcp-hooker

Configurable [FastMCP](https://gofastmcp.com/) server that turns any **OpenAPI** spec (local or remote, JSON or YAML) into an MCP tool surface over HTTP.

Configuration is YAML via [yayaya](https://pypi.org/project/yayaya/) — layered files, dot-path lookups, and `${ENV_VAR}` expansion for secrets.

## Project layout

```text
mcp-hooker/
  pyproject.toml
  config.yaml           # default config (safe to commit)
  config.local.yaml     # optional overlay (gitignored)
  Dockerfile
  docker-compose.yml
  .gitlab-ci.yml
  docs/RUNBOOK.md
  mcp_hooker/
    settings.py         # yayaya loader
    spec_loader.py      # fetch/parse OpenAPI
    route_filters.py    # openapi.tools_filter → FastMCP route_map_fn
    server.py           # reloadable HTTP MCP server
```

## Quick start

```bash
cd mcp-hooker
python -m venv .venv
source .venv/bin/activate
pip install -e .

# Edit config.yaml (openapi.spec + api.base_url), then:
mcp-hooker
```

The server listens on `server.host` / `server.port` from config (default `0.0.0.0:8000`).

## Configuration

Loaded from `config.yaml`, with optional `config.local.yaml` merged on top. Override paths with:

```bash
export MCP_HOOKER_CONFIG_FILES=config.yaml,config.docker.yaml
```

### Keys

| Path | Description |
|------|-------------|
| `server.name` | MCP server display name |
| `server.instructions` | Optional MCP `initialize` instructions string (injected into LLM context) |
| `server.website_url` | Optional MCP server website URL |
| `server.version` | Optional MCP server version string |
| `server.host` | Bind address |
| `server.port` | Bind port |
| `openapi.spec` | Remote URL or local path to OpenAPI JSON/YAML |
| `openapi.fetch_timeout` | Timeout when downloading remote specs (seconds) |
| `openapi.patch_files` | Local YAML/JSON overlays deep-merged into the parsed OpenAPI spec, resolved relative to the primary config file |
| `openapi.validate_output` | When `false`, disable FastMCP strict output-schema validation on tool responses (default `true`) |
| `openapi.tools_filter` | Per-instance OpenAPI operation filter (`enabled`, optional `file`, or inline rules). See `examples/tools_filter.example.yaml` |
| `openapi.sanitizer.enabled` | Inline local response-schema refs before FastMCP conversion |
| `openapi.sanitizer.on_unresolved` | `preserve` or `replace_generic` when local refs still cannot be resolved |
| `openapi.sanitizer.paginated_lists.enabled` | Rewrite GET list endpoints (`page`/`per` params) from bare arrays to paginated object envelopes |
| `openapi.sanitizer.paginated_lists.items_key` | Name of the array field in paginated responses (default `results`) |
| `api.base_url` | Upstream API base URL (falls back to `servers[0].url` in the spec) |
| `api.timeout` | httpx timeout for tool calls |
| `api.headers` | Extra request headers; values support `${ENV_VAR}` |
| `reload.token` | If set, `POST /admin/reload` requires `Authorization: Bearer <token>` |
| `reload.interval_seconds` | Auto-reload interval (`0` = off) |
| `reload.on_sighup` | Reload on `SIGHUP` (default `true`) |

Example `api.headers` with a secret from the environment (this is how the
Caflou instance authorizes upstream requests):

```yaml
api:
  base_url: https://app.caflou.com
  headers:
    Authorization: "Bearer ${CAFLOU_API_TOKEN}"
```

Example sanitizer settings for problematic OpenAPI response schemas:

```yaml
openapi:
  sanitizer:
    enabled: true
    on_unresolved: replace_generic
    paginated_lists:
      enabled: true
      items_key: results
```

The sanitizer only touches response schemas. It inlines local
`#/components/schemas/...` refs before handing the spec to FastMCP. If recursive
or otherwise unresolved local refs remain, `replace_generic` swaps the affected
response schema for a generic object so MCP clients can still load the tool.

When `paginated_lists.enabled` is true, GET operations that accept `page` or
`per` query parameters and declare a bare JSON array response are rewritten to
a minimal object envelope (`page`, `results`, `additionalProperties: true`).
Extra pagination fields from the live API (`prev_page`, `total_results`, etc.)
are allowed via `additionalProperties` and do not need to be listed explicitly.
The envelope uses OpenAPI 3.0 syntax only (no JSON Schema union types such as
`type: ["integer", "null"]`, which FastMCP's spec parser rejects).

### OpenAPI patch files

If the upstream spec is incomplete or needs local corrections, you can layer one
or more patch files on top of the downloaded spec before FastMCP ingests it:

```yaml
openapi:
  spec: https://app.caflou.com/api/v1/i/docs/openapi/v1/openapi.yaml
  patch_files:
    - examples/caflou.accounts.patch.yaml
```

Patch files are parsed as YAML/JSON objects and deep-merged into the OpenAPI
document. Mappings are merged recursively; lists are replaced in full. This
keeps the server spec-driven even when the upstream file is re-downloaded
periodically. Relative patch paths are resolved against the directory of the
primary config file (the first file in `MCP_HOOKER_CONFIG_FILES`, or
`config.yaml` by default).

In Docker, mount patch files into that same directory inside the container. If
`config.yaml` is mounted at `/app/config.yaml`, then `patch.yaml` must also be
available at `/app/patch.yaml`:

```yaml
volumes:
  - ./config.yaml:/app/config.yaml:ro
  - ./patch.yaml:/app/patch.yaml:ro
```

Example Caflou patch for the missing `GET /api/v1/accounts` endpoint:

```yaml
paths:
  /api/v1/accounts:
    get:
      tags:
        - Accounts
      operationId: List_Accounts
      summary: List accounts
      responses:
        "200":
          description: Accounts visible to the current token.
          content:
            application/json:
              schema:
                type: object
                properties:
                  result:
                    type: array
                    items:
                      type: object
                      additionalProperties: true
                required:
                  - result
                x-fastmcp-wrap-result: true
```

The repository includes that example as
`examples/caflou.accounts.patch.yaml`. After reload, FastMCP should expose a
generated `List_Accounts` MCP tool if the upstream API accepts the request.

Set the value via the environment (never commit it). With Docker Compose, copy
`.env.example` to `.env` and fill in `CAFLOU_API_TOKEN`; the token is passed
into the container and injected on every upstream call. See `docs/RUNBOOK.md`
§4 (auth) and §6 (production deployment).

## HTTP endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Liveness + current spec/base URL |
| `/admin/reload` | POST | Re-read config and OpenAPI spec |
| `/mcp` | * | Streamable HTTP MCP (FastMCP default) |

## Reloading

- **HTTP:** `curl -X POST http://localhost:8000/admin/reload`
- **Signal:** `kill -HUP <pid>` when `reload.on_sighup` is true
- **Interval:** set `reload.interval_seconds` to poll remote specs

Each reload closes the previous httpx client, re-parses config via yayaya, fetches the spec, and rebuilds the FastMCP OpenAPI provider.

## Docker

```bash
docker build -t mcp-hooker .
docker run --rm -p 8000:8000 -v "$PWD/config.yaml:/app/config.yaml:ro" mcp-hooker
```

Or with Compose:

```bash
docker compose up --build
```

## GitLab CI/CD

`.gitlab-ci.yml` mirrors [zpl-mcp](https://gitlab.catania-service.cz/catania_dev/zpl-mcp): build on a tagged runner, push `$CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA`, and tag `latest` on the default branch.

Register a runner with tag `mcp-hooker` (or change the tag in `.gitlab-ci.yml` to match your fleet).

## Cursor / MCP client

Point your client at the streamable HTTP URL, for example:

```json
{
  "mcpServers": {
    "my-api": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

## Development

```bash
pip install -e ".[dev]"
ruff check mcp_hooker
```

## Operations

See [docs/RUNBOOK.md](docs/RUNBOOK.md) for architecture, deployment notes, and troubleshooting.

## License

MIT
