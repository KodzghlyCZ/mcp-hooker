# syntax=docker/dockerfile:1.4
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

ENV MCP_HOOKER_ROOT=/app

COPY pyproject.toml README.md config.yaml /app/
RUN pip install --no-cache-dir --upgrade pip \
    && python -c "import subprocess, tomllib; \
deps = tomllib.load(open('/app/pyproject.toml', 'rb'))['project']['dependencies']; \
subprocess.check_call(['pip', 'install', '--no-cache-dir', *deps])" \
    && rm -rf /root/.cache/pip

COPY mcp_hooker /app/mcp_hooker
RUN pip install --no-cache-dir --no-deps /app \
    && rm -rf /root/.cache/pip

EXPOSE 8000
CMD ["mcp-hooker"]
