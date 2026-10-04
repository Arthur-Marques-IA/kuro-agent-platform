# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.33 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Extras opcionais, ex.: `--build-arg KURO_EXTRAS="--extra observability"` para
# incluir o exportador do Langfuse. Vazio = imagem enxuta.
ARG KURO_EXTRAS=""

# Instala dependências primeiro (cache separado do código-fonte). O cache do uv
# fica num mount do BuildKit: acelera o próximo build sem entrar na imagem, onde
# ocupava o mesmo tanto que os pacotes instalados.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev $KURO_EXTRAS

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev $KURO_EXTRAS

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# Chama o uvicorn do venv diretamente: `uv run` re-sincronizaria o
# ambiente contra o lockfile a cada start (lento e sem necessidade, já
# que a imagem já foi montada com `uv sync` acima).
CMD ["uvicorn", "agent_service.main:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
