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
| `server.host` | Bind address |
| `server.port` | Bind port |
| `openapi.spec` | Remote URL or local path to OpenAPI JSON/YAML |
| `openapi.fetch_timeout` | Timeout when downloading remote specs (seconds) |
| `api.base_url` | Upstream API base URL (falls back to `servers[0].url` in the spec) |
| `api.timeout` | httpx timeout for tool calls |
| `api.headers` | Extra request headers; values support `${ENV_VAR}` |
| `reload.token` | If set, `POST /admin/reload` requires `Authorization: Bearer <token>` |
| `reload.interval_seconds` | Auto-reload interval (`0` = off) |
| `reload.on_sighup` | Reload on `SIGHUP` (default `true`) |

Example `api.headers` with a secret from the environment:

```yaml
api:
  headers:
    Authorization: "Bearer ${API_TOKEN}"
```

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
