# syntax=docker/dockerfile:1
#
# Dois estágios: o `builder` tem o compilador (para alguma dependência que não
# traga wheel pronta) e o `uv`; a imagem final só tem o Python e o venv já
# montado, sem `build-essential`, e roda com um usuário sem privilégio.
#
# Os dois estágios partem da mesma imagem base e montam o venv no mesmo caminho
# (`/app/.venv`): os scripts do venv (`uvicorn`, `kuro`, `kuro-mcp`) apontam para
# o Python por caminho absoluto, e o projeto é instalado em modo editável,
# apontando para `/app/src`. Mudar um sem o outro quebra a imagem.
#
# Bibliotecas de sistema em runtime: nenhuma além do que a `python:3.12-slim` já
# traz. O `psycopg[binary]` vem com a própria libpq, e `asyncpg`, `cryptography`,
# `uvloop` e companhia são wheels manylinux, que embutem o que precisam.

FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.33 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Extras opcionais, ex.: `--build-arg KURO_EXTRAS="--extra observability"` para
# incluir o exportador do Langfuse. Vazio = imagem enxuta.
ARG KURO_EXTRAS=""

# Instala dependências primeiro (cache separado do código-fonte)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev $KURO_EXTRAS

COPY src ./src
RUN uv sync --frozen --no-dev $KURO_EXTRAS


FROM python:3.12-slim AS runtime

# Usuário fixo (uid/gid 10001): numérico para o `runAsNonRoot` de um orquestrador
# conseguir conferir, e alto para não colidir com um usuário do host. O HOME é
# dele: o `kuro chat` rodado dentro do container (`docker compose exec`) guarda a
# sessão em `~/.kuro/sessions.json`.
RUN groupadd --system --gid 10001 kuro \
    && useradd --system --uid 10001 --gid kuro --home-dir /home/kuro --create-home --shell /usr/sbin/nologin kuro

# A imagem oficial do Python vem sem os `.pyc` da biblioteca padrão. Rodando como
# root, o Python os gravava no primeiro start; como `kuro` (sem escrita em
# /usr/local), cada processo — o serviço, o healthcheck, o `kuro` — recompilaria
# tudo a cada vez. Compila uma vez aqui, ainda como root.
RUN python -m compileall -q -j 0 /usr/local/lib/python3.12 || true

WORKDIR /app

# O código e o venv ficam do root e só para leitura de quem roda: o processo do
# serviço não tem por que reescrever o próprio código. O que ele escreve em disco
# é só o `/tmp` (arquivos de tools) e o HOME acima; os dados moram no Postgres.
COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/kuro

USER 10001:10001

EXPOSE 8000

# Chama o uvicorn do venv diretamente: `uv run` re-sincronizaria o
# ambiente contra o lockfile a cada start (lento e sem necessidade, já
# que a imagem já foi montada com `uv sync` no builder).
CMD ["uvicorn", "agent_service.main:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
