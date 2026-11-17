"""Falhas de tool, complexidade do run e tools com efeito colateral.

- `runs.tool_calls`, `runs.tool_failures`, `runs.complexity`: resumo das tools de
  cada run (ver `tools/failures.py`). O `status` do run não muda.
- `tool_definitions.side_effect`: a tool muda algo no sistema chamado (grava,
  cobra, transfere)? `NULL` = ainda não classificada — o console avisa.

Os runs já gravados são reclassificados aqui: uma tool `kind="api"` devolvia o
erro como texto ("HTTP 500: ...", "Falha ao chamar a API: ...") e a span ficava
como sucesso. Esses textos são gerados pelo próprio Kuro, então o prefixo diz o
que aconteceu. Só tools `kind="api"`: uma tool python pode devolver um texto
qualquer que comece igual.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-01
"""

import re
from collections import defaultdict

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_BATCH = 500
# Cópia de `tools/failures.py` no momento desta migração — ela não pode importar
# o código da aplicação, que muda depois.
_INTERNAL_TOOLS = {"update_user_memory", "search_knowledge_base"}
_HTTP = re.compile(r"^HTTP (\d{3}): ")
# 404 é registrado como aviso e não conta como falha (ver `tools/failures.py`).
_NOT_COUNTED = {"not_found"}


def _classify(output: object) -> tuple[str, int | None] | None:
    if not isinstance(output, str):
        return None
    if match := _HTTP.match(output):
        status = int(match.group(1))
        if status < 400:
            return None
        if status in (401, 403):
            return "auth", status
        if status == 404:
            return "not_found", status
        if status == 429 or status >= 500:
            return "unavailable", status
        return "invalid_arguments", status
    if output.startswith("Falha ao chamar a API"):
        return "unavailable", None
    if output.startswith(("Chamada recusada", "Erro de configuração")):
        return "config", None
    if output.startswith(("Erro: faltam parâmetros", "Erro ao montar a URL")):
        return "invalid_arguments", None
    return None


def _complexity(names: list[str]) -> int:
    distinct = {name for name in names if name not in _INTERNAL_TOOLS}
    if not distinct:
        return 1
    return 2 if len(distinct) <= 2 else 3


def upgrade() -> None:
    op.add_column("runs", sa.Column("tool_calls", sa.Integer, nullable=True))
    op.add_column("runs", sa.Column("tool_failures", sa.Integer, nullable=True))
    op.add_column("runs", sa.Column("complexity", sa.Integer, nullable=True))
    op.create_index("ix_runs_complexity", "runs", ["complexity"])
    op.add_column("tool_definitions", sa.Column("side_effect", sa.Boolean, nullable=True))

    conn = op.get_bind()
    tools = sa.table("tool_definitions", sa.column("tool_name", sa.String), sa.column("kind", sa.String))
    spans = sa.table(
        "run_spans",
        sa.column("id", sa.String),
        sa.column("run_id", sa.String),
        sa.column("type", sa.String),
        sa.column("name", sa.String),
        sa.column("level", sa.String),
        sa.column("status_message", sa.Text),
        sa.column("output", sa.JSON),
        sa.column("metadata", sa.JSON),
    )
    runs = sa.table(
        "runs",
        sa.column("run_id", sa.String),
        sa.column("tool_calls", sa.Integer),
        sa.column("tool_failures", sa.Integer),
        sa.column("complexity", sa.Integer),
    )

    # As tools de exemplo do seed só leem.
    seed = sa.table(
        "tool_definitions",
        sa.column("tool_name", sa.String),
        sa.column("is_seed", sa.Boolean),
        sa.column("side_effect", sa.Boolean),
    )
    conn.execute(
        seed.update()
        .where(seed.c.is_seed.is_(True), seed.c.tool_name.in_(["calculator", "hackernews", "cat_fact"]))
        .values(side_effect=False)
    )

    api_tools = {row.tool_name for row in conn.execute(sa.select(tools.c.tool_name).where(tools.c.kind == "api"))}

    by_run: dict[str, list[tuple[str, bool]]] = defaultdict(list)
    last_id = ""
    while True:
        batch = list(
            conn.execute(
                sa.select(spans.c.id, spans.c.run_id, spans.c.name, spans.c.level, spans.c.output, spans.c.metadata)
                .where(spans.c.type == "TOOL", spans.c.id > last_id)
                .order_by(spans.c.id)
                .limit(_BATCH)
            )
        )
        if not batch:
            break
        last_id = batch[-1].id
        for span in batch:
            failed = span.level == "ERROR"
            classified = _classify(span.output) if span.name in api_tools and not failed else None
            if classified is not None:
                kind, http_status = classified
                conn.execute(
                    spans.update()
                    .where(spans.c.id == span.id)
                    .values(
                        level="WARNING" if kind in _NOT_COUNTED else "ERROR",
                        status_message=span.output[:500],
                        metadata={
                            **(span.metadata or {}),
                            "failure": kind,
                            **({"http_status": http_status} if http_status else {}),
                        },
                    )
                )
                failed = kind not in _NOT_COUNTED
            elif failed and not (span.metadata or {}).get("failure"):
                conn.execute(
                    spans.update()
                    .where(spans.c.id == span.id)
                    .values(metadata={**(span.metadata or {}), "failure": "exception"})
                )
            by_run[span.run_id].append((span.name, failed))

    for run_id, calls in by_run.items():
        conn.execute(
            runs.update()
            .where(runs.c.run_id == run_id)
            .values(
                tool_calls=len(calls),
                tool_failures=sum(1 for _, failed in calls if failed),
                complexity=_complexity([name for name, _ in calls]),
            )
        )
    # Runs sem nenhuma tool.
    conn.execute(runs.update().where(runs.c.tool_calls.is_(None)).values(tool_calls=0, tool_failures=0, complexity=1))
