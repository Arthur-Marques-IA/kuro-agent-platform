"""Um turno de conversa com um agente procedural.

A ordem é o que garante as regras de `agents/procedural.py`:

1. carrega (ou cria) o estado da sessão em `procedure_runs` — e, se o agente foi
   editado desde a última mensagem, as confirmações caem;
2. **extrai** da mensagem os valores dos campos — uma chamada estruturada ao
   modelo, revalidada aqui;
3. **aplica** as regras de transição (código, não modelo) e grava o estado;
4. se a etapa atual é uma `action`, **reserva** a ação (grava `running` com a
   trava otimista), chama a tool e grava o resultado — tudo *antes* de responder,
   e protegido do cancelamento de quem chamou, para uma resposta que estoure o
   tempo ou um cliente que desconecte não apagarem um efeito que já aconteceu;
5. **responde**: na etapa `confirm`, com o texto montado dos dados; nas outras, o
   modelo redige a partir de `dependencies.procedimento`.

Uma conversa concluída fica concluída: não extrai nem executa mais nada, mesmo
que o agente ganhe etapas depois.

O turno inteiro tem um prazo só (`timeout_seconds`), repartido entre as fases.
Tudo isso é uma execução só no trace: a extração e a ação entram como spans do
mesmo run, e a etapa em que a conversa ficou vai em `metadata.procedure_stage`.
"""

import asyncio
import dataclasses
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import datetime, timedelta, timezone
from typing import Any

from agno.agent import Agent
from agno.run.agent import RunEvent
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from agent_service.agents import procedural, procedure_store
from agent_service.agents.base import build_agent
from agent_service.agents.procedure_store import ProcedureConflictError
from agent_service.models.crypto import EncryptionNotConfiguredError
from agent_service.models.provider import ProviderNotConfiguredError
from agent_service.observability.run_store import PROCEDURE_STATE_SPAN, SpanRecord
from agent_service.observability.tracing import (
    RUN_FAILED,
    RunContext,
    RunTimeoutError,
    _model_spans,
    record_run_without_model,
    traced_run_events,
)
from agent_service.tools.invoke import ToolUnavailableError, invoke_tool

logger = logging.getLogger(__name__)

STALE_ACTION = timedelta(minutes=10)
"""Uma ação `running` há mais que isso foi interrompida (o processo caiu no meio)."""

_extractors: dict[str, tuple[Any, Agent]] = {}


class ProcedureBusyError(RuntimeError):
    """Outra mensagem da mesma sessão está sendo processada — 409 para quem chama."""


class ProcedureModelError(RuntimeError):
    """A extração falhou no provedor do modelo — 502, como uma falha do modelo no `/chat`."""


def _busy(session_id: str) -> ProcedureBusyError:
    return ProcedureBusyError(
        f"Outra mensagem da sessão {session_id!r} está sendo processada. Espere a resposta dela e envie de novo."
    )


def is_procedural(run: RunContext) -> bool:
    return bool(run.definition) and (run.definition.get("kind") or "conversational") == "procedural"


class _Deadline:
    """O prazo do turno inteiro. Cada fase (extração, ação, resposta) usa o que
    sobrou — sem isto, um turno podia levar três vezes o `timeout_seconds`, e o
    timeout do cliente documentado (`timeout_seconds + 5`) estouraria antes."""

    def __init__(self, seconds: float | None) -> None:
        self.seconds = seconds
        self._end = time.monotonic() + seconds if seconds else None

    def remaining(self, phase: str) -> float | None:
        if self._end is None:
            return None
        left = self._end - time.monotonic()
        if left <= 0:
            raise RunTimeoutError(f"Tempo limite de {self.seconds:g}s excedido ({phase}).")
        return left


# -- extração --------------------------------------------------------------------------


def _extractor_agent(definition: dict[str, Any]) -> Agent:
    """Um agente one-shot com o schema da extração, no mesmo modelo do agente.
    Em cache pela definição: muda quando as etapas ou o modelo mudam."""
    key = definition["agent_type"]
    stamp = definition.get("updated_at")
    cached = _extractors.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    agent = build_agent(
        agent_id=f"{key}-extracao",
        name=f"{definition['name']} (extração)",
        instructions=procedural.EXTRACTION_INSTRUCTIONS,
        model_provider=definition.get("model_provider"),
        model_id=definition.get("model_id"),
        model_credential_id=definition.get("model_credential_id"),
        model_params=definition.get("model_params"),
        kind="analysis",
        output_schema=procedural.extraction_model(key, definition.get("stages") or []),
    )
    _extractors[key] = (stamp, agent)
    return agent


async def extract(
    definition: dict[str, Any], state: dict[str, Any], message: str, last_reply: str | None
) -> tuple[dict[str, Any], list[SpanRecord]]:
    """Os valores que a mensagem traz, e os spans da chamada ao modelo."""
    stages = definition.get("stages") or []
    prompt = procedural.extraction_prompt(stages, state, message, last_reply)
    started = datetime.now(timezone.utc)
    agent = await run_in_threadpool(_extractor_agent, definition)
    output = await agent.arun(prompt, stream=False)
    if str(getattr(output, "status", "")).lower().endswith("error"):
        raise ProcedureModelError(f"A extração falhou no modelo: {getattr(output, 'content', None) or RUN_FAILED}")
    content = getattr(output, "content", None)
    extracted = content.model_dump() if isinstance(content, BaseModel) else {}
    spans = _model_spans(getattr(output, "metrics", None), started) or [
        SpanRecord(type="GENERATION", name="extracao", started_at=started)
    ]
    for span in spans:
        span.name = f"extracao · {span.name}"
        span.input = prompt
        span.output = extracted
        span.ended_at = datetime.now(timezone.utc)
    return extracted, spans


# -- ação ---------------------------------------------------------------------------------


def _idempotency_key(row: dict[str, Any], stage_id: str, slots: dict[str, Any]) -> str:
    """Estável enquanto os dados não mudam: repetir a ação depois de um timeout (a
    pessoa confirmou de novo) manda a mesma chave, e o destino reconhece que é o
    mesmo pedido. Se a pessoa corrigiu um dado, é outro pedido — e outra chave."""
    digest = hashlib.sha256(json.dumps(slots, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    return f"{row['id']}:{stage_id}:{digest}"


async def _execute_action(
    stage: dict[str, Any], state: dict[str, Any], row: dict[str, Any], run: RunContext, timeout: float | None
) -> tuple[dict[str, Any], SpanRecord]:
    attempt = state["actions"][stage["id"]]["attempts"]
    slots = dict(state["slots"])
    dependencies = {
        **(run.dependencies or {}),
        **slots,
        "idempotency_key": _idempotency_key(row, stage["id"], slots),
    }
    started = datetime.now(timezone.utc)
    try:
        call = invoke_tool(
            stage["tool"],
            slots,
            function_name=stage.get("function"),
            dependencies=dependencies,
            dry_run=run.dry_run,
            lenient_arguments=True,
        )
        outcome = await (asyncio.wait_for(call, timeout) if timeout else call)
    except ToolUnavailableError as exc:
        outcome = {"ok": False, "error": str(exc), "failure": "config"}
    except TimeoutError:
        outcome = {
            "ok": False,
            "error": "a ação passou do tempo limite — confira no sistema de destino se ela aconteceu antes de tentar de novo",
            "failure": "unavailable",
        }
    span = SpanRecord(
        type="TOOL",
        name=stage["tool"],
        started_at=started,
        ended_at=datetime.now(timezone.utc),
        level="DEFAULT" if outcome.get("ok") else "ERROR",
        status_message=None if outcome.get("ok") else str(outcome.get("error"))[:500],
        input=slots,
        output=outcome.get("result"),
        metadata={
            "procedure_stage": stage["id"],
            "attempt": attempt,
            **({"failure": outcome["failure"]} if outcome.get("failure") and not outcome.get("ok") else {}),
            **({"http_status": outcome["http_status"]} if outcome.get("http_status") else {}),
        },
    )
    return outcome, span


# -- turno ------------------------------------------------------------------------------------


def _stage_id(stages: list[dict[str, Any]], state: dict[str, Any]) -> str | None:
    index = procedural.current_index(stages, state)
    return stages[index]["id"] if index is not None else None


async def _save(row: dict[str, Any], state: dict[str, Any], stages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    try:
        return await run_in_threadpool(
            procedure_store.save_procedure, row, state=state, stage=_stage_id(stages, state), **extra
        )
    except ProcedureConflictError as exc:
        raise _busy(row["session_id"]) from exc


async def _run_action(
    stage: dict[str, Any],
    state: dict[str, Any],
    row: dict[str, Any],
    run: RunContext,
    stages: list[dict[str, Any]],
    timeout: float | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], SpanRecord]:
    """Executa e grava o resultado. Roda numa task protegida (`asyncio.shield`):
    se quem chamou desconectar no meio, a tool termina e o resultado é gravado
    mesmo assim — sem isto a ação ficaria `running` e a sessão travada em 409."""
    outcome, span = await _execute_action(stage, state, row, run, timeout)
    state = procedural.finish_action(stages, state, stage["id"], outcome)
    row = await _save(row, state, stages)
    return state, row, outcome, span


def _append_to_session(agent: Agent, run: RunContext, reply: str) -> None:
    """Põe na sessão do Agno a troca que o servidor respondeu sem o modelo (a
    confirmação). Sem isto ela some do histórico: o modelo, na mensagem seguinte,
    não saberia que pediu confirmação, e o console e `kuro sessions show` (que
    leem a sessão) mostrariam a conversa com buracos."""
    from agno.metrics import RunMetrics
    from agno.models.message import Message
    from agno.run.agent import RunInput, RunOutput
    from agno.run.base import RunStatus
    from agno.session.agent import AgentSession

    db = getattr(agent, "db", None)
    if db is None:
        return
    try:
        session = agent.get_session(session_id=run.session_id, user_id=run.user_id)
    except Exception:  # noqa: BLE001 - o Agno levanta Exception para sessão que não existe
        session = None
    if not isinstance(session, AgentSession):
        # A linha da sessão e os runs são gravados separados nesta versão do Agno
        # (`upsert_session` não toca nos runs): sem a linha, o run fica órfão.
        now = int(time.time())
        db.upsert_session(
            AgentSession(
                session_id=run.session_id,
                agent_id=agent.id,
                user_id=run.user_id,
                session_data={},
                created_at=now,
                updated_at=now,
            )
        )
    db.upsert_run(
        RunOutput(
            run_id=run.run_id,
            agent_id=agent.id,
            agent_name=agent.name,
            session_id=run.session_id,
            user_id=run.user_id,
            input=RunInput(input_content=run.message),
            content=reply,
            messages=[Message(role="user", content=run.message), Message(role="assistant", content=reply)],
            # Os tokens do turno são os da extração: sem métricas, o console mostrava "tokens: ?".
            metrics=RunMetrics(
                input_tokens=sum(s.input_tokens for s in run.extra_spans),
                output_tokens=sum(s.output_tokens for s in run.extra_spans),
                total_tokens=sum(s.total_tokens for s in run.extra_spans),
            ),
            status=RunStatus.completed,
        ),
        session_id=run.session_id,
        user_id=run.user_id,
    )


async def run_procedural_turn(agent: Agent, run: RunContext) -> AsyncIterator[tuple[str, Any]]:
    """Eventos `("message", texto)`, `("usage", métricas)`, `("error", motivo)` e,
    por último, `("state", estado)`. Levanta `ProcedureBusyError` (409),
    `ProcedureModelError` (502) e `RunTimeoutError` (504)."""
    definition = run.definition or {}
    stages = definition.get("stages") or []
    deadline = _Deadline(run.timeout_seconds)
    dependencies = dict(run.dependencies or {})

    row = await run_in_threadpool(procedure_store.get_procedure, run.agent_type, run.session_id)
    if row is None:
        initial = procedural.new_state(stages, dependencies)
        try:
            row = await run_in_threadpool(
                lambda: procedure_store.create_procedure(
                    agent_type=run.agent_type,
                    session_id=run.session_id,
                    user_id=run.user_id,
                    state=initial,
                    stage=_stage_id(stages, initial),
                    dry_run=run.dry_run,
                )
            )
        except ProcedureConflictError as exc:
            raise _busy(run.session_id) from exc
    finished = row["status"] == "done"
    state = row["state"] if finished else procedural.sync_with_definition(stages, row["state"])
    last_reply = row.get("last_reply")
    spans: list[SpanRecord] = []

    # 1-3. extração e transição (uma conversa concluída não muda mais)
    if not finished and procedural.current_index(stages, state) is not None:
        remaining = deadline.remaining("extração")
        try:
            call = extract(definition, state, run.message, last_reply)
            extracted, extraction_spans = await (asyncio.wait_for(call, remaining) if remaining else call)
        except TimeoutError:
            raise RunTimeoutError(f"Tempo limite de {run.timeout_seconds:g}s excedido na extração.") from None
        except (RunTimeoutError, ProviderNotConfiguredError, EncryptionNotConfiguredError):
            raise
        except Exception as exc:  # noqa: BLE001 - falha do provedor: o run fica registrado como erro
            message = exc.args[0] if isinstance(exc, ProcedureModelError) else f"A extração falhou no modelo: {exc}"
            await run_in_threadpool(
                lambda: record_run_without_model(run, None, status="error", status_message=message)
            )
            raise ProcedureModelError(message) from exc
        spans.extend(extraction_spans)
        state, _ = procedural.apply_extraction(stages, state, extracted)
        row = await _save(row, state, stages)

    # 4. ações: reservar, executar e gravar antes de responder
    action_error: str | None = None
    while (
        not finished
        and (index := procedural.current_index(stages, state)) is not None
        and stages[index]["type"] == "action"
    ):
        stage = stages[index]
        current = state["actions"].get(stage["id"]) or {}
        if current.get("status") == "running":
            updated_at = row.get("updated_at")
            if isinstance(updated_at, datetime) and updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if updated_at is None or datetime.now(timezone.utc) - updated_at < STALE_ACTION:
                raise _busy(run.session_id)
            # O processo caiu no meio da ação: não dá para saber se ela aconteceu.
            state = procedural.finish_action(
                stages,
                state,
                stage["id"],
                {"ok": False, "error": "a ação foi interrompida — confira no sistema de destino antes de confirmar de novo"},
            )
            row = await _save(row, state, stages)
            action_error = state["actions"][stage["id"]]["error"]
            break
        # O prazo antes da reserva: estourar depois dela deixaria a ação `running` sem dono.
        remaining = deadline.remaining("ação")
        state = procedural.start_action(state, stage["id"])
        row = await _save(row, state, stages)  # a reserva: outra mensagem não executa a mesma ação
        task = asyncio.ensure_future(_run_action(stage, state, row, run, stages, remaining))
        state, row, outcome, span = await asyncio.shield(task)
        spans.append(span)
        if not outcome.get("ok"):
            action_error = state["actions"][stage["id"]]["error"]
            break

    view = procedural.state_view(stages, state, finished=finished)
    # O estado como o turno terminou, gravado no run: `procedure_runs` só guarda o
    # último, e o console precisa mostrar etapa, coletado e faltando turno a turno.
    now = datetime.now(timezone.utc)
    spans.append(
        SpanRecord(
            type="CHAIN",
            name="procedimento · estado",
            started_at=now,
            ended_at=now,
            latency_ms=0.0,
            output=view,
            metadata={PROCEDURE_STATE_SPAN: True},
        )
    )
    turn = dataclasses.replace(
        run,
        extra_spans=tuple(spans),
        metadata={**run.metadata, "procedure_stage": view["stage"] or "done"},
    )

    # 5. resposta
    index = None if finished else procedural.current_index(stages, state)
    reply: str
    if index is not None and stages[index]["type"] == "confirm" and not state.get("declined") and not action_error:
        reply = procedural.confirmation_text(stages, state, index)
        await run_in_threadpool(record_run_without_model, turn, reply)
        try:
            await run_in_threadpool(_append_to_session, agent, turn, reply)
        except Exception:  # noqa: BLE001 - o turno valeu; só o histórico do Agno ficou sem ele
            logger.warning("Confirmação da sessão %r fora do histórico do Agno", run.session_id, exc_info=True)
        yield "message", reply
    else:
        context = procedural.reply_context(
            stages, state, last_reply=last_reply, action_error=action_error, finished=finished
        )
        turn = dataclasses.replace(
            turn,
            dependencies={**dependencies, "procedimento": context},
            timeout_seconds=deadline.remaining("resposta"),
        )
        chunks: list[str] = []
        final: str | None = None
        async with aclosing(traced_run_events(agent, turn)) as events:
            async for event in events:
                if event.event == RunEvent.run_content.value and event.content:
                    chunks.append(event.content)
                    yield "message", event.content
                elif event.event == RunEvent.run_completed.value:
                    if isinstance(event.content, str):
                        final = event.content
                    if event.metrics:
                        yield "usage", event.metrics.to_dict()
                elif event.event == RunEvent.run_error.value:
                    yield "error", event.content or RUN_FAILED
        reply = final if final is not None else "".join(chunks)
        if final is not None and not chunks:
            yield "message", final

    try:
        await run_in_threadpool(
            procedure_store.save_procedure,
            row,
            state=state,
            stage=None if finished else view["stage"],
            last_reply=reply,
        )
    except ProcedureConflictError:
        logger.info("Última resposta da sessão %r não gravada: outra mensagem gravou antes", run.session_id)
    yield "state", view
