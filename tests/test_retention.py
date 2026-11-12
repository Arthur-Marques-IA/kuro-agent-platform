"""Retenção do trace store (`observability/retention.py`) e redação por chave na gravação
(`observability/redaction.py`).

O banco é o da suíte inteira: os runs "antigos" daqui são de 2001 e o corte de 2005, bem
antes de qualquer run de outro teste — a retenção apaga por data, não por agente.
"""

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import insert, select

from agent_service.config import Settings, get_settings
from agent_service.db import get_db
from agent_service.jobs import store as jobs
from agent_service.jobs import worker as jobs_worker
from agent_service.jobs.worker import Worker
from agent_service.observability import redaction, retention, run_store
from agent_service.observability.chunk_feedback import chunk_feedback
from agent_service.observability.run_store import (
    RunRecord,
    SpanRecord,
    run_metadata,
    run_references,
    run_scores,
    run_spans,
    runs,
)

ANTIGO = datetime(2001, 3, 1, 12, tzinfo=timezone.utc)
RECENTE = datetime(2010, 3, 1, 12, tzinfo=timezone.utc)
CORTE = datetime(2005, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def chaves(monkeypatch):
    def configura(valor: str) -> None:
        monkeypatch.setattr(get_settings(), "trace_redact_keys", valor)

    return configura


def _grava(started_at: datetime, *, agent_type: str, dependencies=None, metadata=None, spans=None) -> str:
    run_id = uuid4().hex
    run_store._write_run(
        RunRecord(
            run_id=run_id,
            trace_id=uuid4().hex,
            agent_type=agent_type,
            agent_name="Retenção",
            prompt_version=1,
            endpoint="/chat",
            user_id="u1",
            session_id=f"s-{run_id[:6]}",
            message="meu cpf é 123",
            started_at=started_at,
            ended_at=started_at + timedelta(seconds=1),
            input={"message": "meu cpf é 123", "dependencies": dependencies or {"cpf": "123"},
                   "attachments": [{"filename": "rg.png", "mime_type": "image/png", "kind": "image"}]},
            output="resposta com dados",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            cost_usd=0.01,
            agent_version=3,
            metadata=metadata if metadata is not None else {"conversation_id": "c1"},
            input_message="meu cpf é 123",
            input_events=[{"role": "user", "content": "meu cpf é 123"}],
            input_context=[{"key": "ficha", "title": "Ficha", "content": "cpf 123"}],
            spans=spans
            if spans is not None
            else [
                SpanRecord(
                    type="TOOL",
                    name="consulta",
                    started_at=started_at,
                    level="ERROR",
                    status_message="erro: cpf 123",
                    input={"cpf": "123"},
                    output="erro: cpf 123",
                    metadata={"failure": "http"},
                ),
                SpanRecord(
                    type="TOOL",
                    name="search_knowledge_base",
                    started_at=started_at,
                    input={"query": "garantia cpf 123"},
                    output="trecho",
                    metadata={"references": {"query": "garantia cpf 123", "results": [
                        {"rank": 1, "document": "manual", "chunk_id": "k1", "content": "texto do trecho"}]}},
                ),
            ],
        )
    )
    with get_db().db_engine.begin() as conn:
        conn.execute(insert(run_scores).values(id=uuid4().hex, run_id=run_id, name="feedback", value=1.0,
                                               data_type="BOOLEAN", comment="o cliente disse o cpf",
                                               user_id="op", timestamp=started_at))
        conn.execute(insert(run_references).values(run_id=run_id, reference={"acao": "x"}, source="legacy"))
        conn.execute(insert(chunk_feedback).values(id=uuid4().hex, run_id=run_id, span_id=f"{run_id}:2",
                                                   query="garantia cpf 123", chunk_id="k1", label="relevant",
                                                   created_at=started_at, status="open"))
    return run_id


def _linhas(table, run_id):
    with get_db().db_engine.connect() as conn:
        return list(conn.execute(select(table).where(table.c.run_id == run_id)))


# -- redação ---------------------------------------------------------------------


def test_redacao_recursiva_por_chave_sem_diferenciar_maiusculas():
    valor = {
        "CPF": "123",
        "cliente": {"nome": "Ana", "Telefone": "999", "contatos": [{"telefone": "888"}, "livre cpf 123"]},
        "corpo": json.dumps({"dados": {"cpf": "123", "ok": True}}),
        "texto": "cpf: 123",
    }
    original = copy.deepcopy(valor)
    redigido = redaction.redact(valor, frozenset({"cpf", "telefone"}))
    assert redigido["CPF"] == redaction.REDACTED
    assert redigido["cliente"]["Telefone"] == redaction.REDACTED
    assert redigido["cliente"]["contatos"][0]["telefone"] == redaction.REDACTED
    assert redigido["cliente"]["nome"] == "Ana"
    assert json.loads(redigido["corpo"]) == {"dados": {"cpf": redaction.REDACTED, "ok": True}}
    # Texto livre não é varrido.
    assert redigido["texto"] == "cpf: 123" and redigido["cliente"]["contatos"][1] == "livre cpf 123"
    assert valor == original  # cópia: o objeto de quem chamou não muda


def test_sem_chaves_a_gravacao_nao_muda(chaves):
    chaves("")
    run_id = _grava(RECENTE, agent_type="ret-sem-chave")
    raiz = next(s for s in _linhas(run_spans, run_id) if s.id.endswith(":root"))
    assert raiz.input["dependencies"] == {"cpf": "123"}


def test_redacao_na_gravacao_de_dependencies_args_resultado_e_metadata(chaves):
    chaves("cpf, Telefone")
    dependencies = {"cpf": "123", "conta": {"telefone": "999", "id": 7}}
    tool_args = {"filtro": {"cpf": "123"}, "assunto": "fatura"}
    resultado = json.dumps({"cliente": {"cpf": "123", "nome": "Ana"}})
    spans = [
        SpanRecord(type="TOOL", name="ficha", started_at=RECENTE, level="ERROR", status_message=resultado,
                   input=tool_args, output=resultado, metadata={"cpf": "123", "failure": "http"}),
        SpanRecord(type="SPAN", name="procedure_state", started_at=RECENTE,
                   output={"collected": {"cpf": "123", "nome": "Ana"}}),
    ]
    entrada_original = copy.deepcopy((dependencies, tool_args))
    run_id = _grava(RECENTE, agent_type="ret-redacao", dependencies=dependencies,
                    metadata={"conversation_id": "c1", "CPF": "123"}, spans=spans)

    linhas = {s.name: s for s in _linhas(run_spans, run_id)}
    assert linhas["/chat"].input["dependencies"] == {"cpf": "[redigido]", "conta": {"telefone": "[redigido]", "id": 7}}
    assert linhas["ficha"].input == {"filtro": {"cpf": "[redigido]"}, "assunto": "fatura"}
    assert json.loads(linhas["ficha"].output) == {"cliente": {"cpf": "[redigido]", "nome": "Ana"}}
    assert "123" not in linhas["ficha"].status_message
    assert linhas["ficha"].metadata["cpf"] == "[redigido]" and linhas["ficha"].metadata["failure"] == "http"
    assert linhas["procedure_state"].output == {"collected": {"cpf": "[redigido]", "nome": "Ana"}}
    meta = {m.key: m.value for m in _linhas(run_metadata, run_id)}
    assert meta == {"conversation_id": "c1", "CPF": "[redigido]"}
    # O modelo e as tools continuaram com o valor real: os objetos deles não mudaram.
    assert (dependencies, tool_args) == entrada_original
    # A mensagem (texto livre) não é varrida.
    with get_db().db_engine.connect() as conn:
        assert conn.execute(select(runs.c.message).where(runs.c.run_id == run_id)).scalar() == "meu cpf é 123"


def test_mascara_do_langfuse(chaves):
    from langfuse.types import MaskOtelSpansParams, OtelSpanData, OtelSpanIdentifier

    chaves("cpf")
    assert redaction.langfuse_mask(data={"dependencies": {"cpf": "1"}}) == {"dependencies": {"cpf": "[redigido]"}}
    ident = OtelSpanIdentifier(trace_id="0" * 32, span_id="1" * 16)
    span = OtelSpanData(
        trace_id=ident.trace_id, span_id=ident.span_id, parent_span_id=None, name="tool", instrumentation_scope_name="agno",
        instrumentation_scope_version=None, resource_attributes={},
        attributes={"input.value": json.dumps({"cpf": "1", "x": 2}), "tool.cpf": "1", "output.value": "texto cpf 1", "n": 3},
    )
    resultado = redaction.langfuse_mask_otel_spans(params=MaskOtelSpansParams(spans={ident: span}))
    patch = resultado.span_patches[ident]
    assert json.loads(patch.set_attributes["input.value"]) == {"cpf": "[redigido]", "x": 2}
    assert patch.set_attributes["tool.cpf"] == "[redigido]"
    assert "output.value" not in patch.set_attributes  # texto livre fica


# -- retenção --------------------------------------------------------------------


def test_desligada_por_padrao(monkeypatch):
    for nome in ("RUN_RETENTION_DAYS", "RUN_RETENTION_MODE", "TRACE_REDACT_KEYS"):
        monkeypatch.delenv(nome, raising=False)
    padrao = Settings(_env_file=None)
    assert padrao.run_retention_days == 0 and padrao.run_retention_mode == "delete" and padrao.trace_redact_keys == ""
    monkeypatch.setenv("RUN_RETENTION_DAYS", "")
    monkeypatch.setenv("RUN_RETENTION_MODE", "")
    vazio = Settings(_env_file=None)
    assert vazio.run_retention_days == 0 and vazio.run_retention_mode == "delete"


def test_desligada_nao_agenda_e_o_job_nao_apaga(monkeypatch):
    monkeypatch.setattr(get_settings(), "run_retention_days", 0)
    antigo = _grava(ANTIGO, agent_type="ret-desligada")
    jobs.cancel_dedupe(retention.DEDUPE_KEY)
    retention.schedule()
    assert jobs.active_job(retention.DEDUPE_KEY) is None
    assert asyncio.run(retention.run_retention_job({"payload": {}})) is None
    assert _linhas(runs, antigo)
    _limpa(antigo)


def _limpa(*run_ids):
    with get_db().db_engine.begin() as conn:
        for table in (run_spans, run_metadata, run_scores, run_references, chunk_feedback, runs):
            conn.execute(table.delete().where(table.c.run_id.in_(run_ids)))


def test_delete_apaga_os_antigos_e_os_dependentes():
    antigo, recente = _grava(ANTIGO, agent_type="ret-delete"), _grava(RECENTE, agent_type="ret-delete")
    resultado = asyncio.run(retention.purge(CORTE, mode="delete", batch_size=1))
    assert resultado["runs"] >= 1
    for table in (runs, run_spans, run_metadata, run_scores, run_references, chunk_feedback):
        assert _linhas(table, antigo) == [], table.name
        assert _linhas(table, recente), table.name
    assert asyncio.run(retention.purge(CORTE, mode="delete"))["runs"] == 0  # idempotente
    _limpa(recente)


def test_anonymize_apaga_conteudo_e_mantem_estatisticas():
    antigo, recente = _grava(ANTIGO, agent_type="ret-anon"), _grava(RECENTE, agent_type="ret-anon")
    assert asyncio.run(retention.purge(CORTE, mode="anonymize", batch_size=1))["runs"] >= 1

    linha = _linhas(runs, antigo)[0]
    assert (linha.message, linha.output, linha.input_message, linha.input_events, linha.input_context) == (None,) * 5
    assert (linha.input_tokens, linha.output_tokens, linha.total_tokens, linha.cost_usd) == (10, 5, 15, 0.01)
    assert (linha.status, linha.agent_type, linha.agent_version, linha.session_id) == ("success", "ret-anon", 3,
                                                                                      linha.session_id)
    assert linha.started_at is not None and linha.latency_ms is not None
    assert linha.tool_calls == 2 and linha.tool_failures == 1

    for span in _linhas(run_spans, antigo):
        assert span.input is None and span.output is None, span.name
        if span.type == "TOOL":
            assert span.status_message is None
    busca = next(s for s in _linhas(run_spans, antigo) if s.name == "search_knowledge_base")
    refs = busca.metadata["references"]
    assert "query" not in refs and refs["results"] == [{"rank": 1, "document": "manual", "chunk_id": "k1"}]
    assert next(s for s in _linhas(run_spans, antigo) if s.name == "consulta").metadata["failure"] == "http"

    assert [s.comment for s in _linhas(run_scores, antigo)] == [None]
    assert [s.value for s in _linhas(run_scores, antigo)] == [1.0]  # o voto conta nas estatísticas
    assert _linhas(run_references, antigo) == []
    assert [c.query for c in _linhas(chunk_feedback, antigo)] == [retention.REMOVED]
    meta = {m.key: m.value for m in _linhas(run_metadata, antigo)}
    assert meta == {"conversation_id": "c1", retention.MARKER_KEY: retention.MARKER_VALUE}

    # A leitura do trace continua de pé, sem anexos nem conteúdo.
    trace = run_store.DbTraceStore().get_run(antigo)
    assert trace.run.message is None and trace.run.attachments == [] and trace.run.total_tokens == 15

    # O recente não foi tocado.
    novo = _linhas(runs, recente)[0]
    assert novo.message == "meu cpf é 123" and novo.input_events
    assert _linhas(run_references, recente)

    # Idempotente: a segunda rodada não acha nada para fazer.
    assert asyncio.run(retention.purge(CORTE, mode="anonymize"))["runs"] == 0
    _limpa(antigo, recente)


def test_job_pela_fila_roda_uma_vez_e_se_reagenda(monkeypatch):
    monkeypatch.setattr(jobs_worker, "HANDLERS", {retention.JOB_KIND: jobs_worker.HANDLERS[retention.JOB_KIND]})
    # Prazo que cobre 2001 e não 2010.
    dias = (datetime.now(timezone.utc) - CORTE).days
    monkeypatch.setattr(get_settings(), "run_retention_days", dias)
    monkeypatch.setattr(get_settings(), "run_retention_mode", "delete")
    antigo, recente = _grava(ANTIGO, agent_type="ret-job"), _grava(RECENTE, agent_type="ret-job")

    jobs.cancel_dedupe(retention.DEDUPE_KEY)
    retention.schedule()
    retention.schedule()  # outra réplica subindo: continua um job só
    ativos = jobs.list_jobs(dedupe_key=retention.DEDUPE_KEY, status="queued")
    assert len(ativos) == 1

    assert asyncio.run(Worker().run_once()) == "succeeded"
    assert _linhas(runs, antigo) == [] and _linhas(runs, recente)
    proximo = jobs.active_job(retention.DEDUPE_KEY)
    assert proximo is not None and proximo["status"] == "queued"
    run_after = proximo["run_after"] if proximo["run_after"].tzinfo else proximo["run_after"].replace(tzinfo=timezone.utc)
    assert run_after > datetime.now(timezone.utc) + timedelta(hours=5)

    # Desligada depois: o agendado sai da fila.
    monkeypatch.setattr(get_settings(), "run_retention_days", 0)
    retention.schedule()
    assert jobs.active_job(retention.DEDUPE_KEY) is None
    _limpa(recente)
