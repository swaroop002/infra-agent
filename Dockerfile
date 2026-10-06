FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[anthropic,openai,aws,azure,gcp,teams,sqlite-memory,postgres]"

COPY config ./config
COPY knowledge ./knowledge

RUN useradd --uid 10001 --create-home agent && mkdir -p /app/data && chown -R agent /app/data
USER agent

EXPOSE 8080
ENV INFRA_AGENT_CONFIG=/app/config/agent.yaml LOG_LEVEL=INFO
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["infra-agent", "serve"]
