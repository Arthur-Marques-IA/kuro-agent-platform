"""A conversa como gente lê: a linha do tempo da sessão (trace store local, separada
em campos pela entrada estruturada) e a transcrição do Agno (resumo e memórias)."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi import HTTPException

from agent_service.api.observability_routes import session_timeline
from agent_service.observability import run_store, trace_store, tracing
from agent_service.observability.run_store import RunRecord, SpanRecord
from agent_service.observability.transcript import compact_session
from agent_service.tools import store as tool_store

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def local_backend(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)
    trace_store.set_trace_store(None)
    yield
    trace_store.set_trace_store(None)


def _block(key, content, *, title=None, volatile=False):
    return {
        "key": key,
        "title": title or key.title(),
        "content": content,
        "volatile": volatile,
        "hash": sha256(content.encode("utf-8")).hexdigest(),
    }


def _record(session_id, minute, *, structured=True, context=(), events=None, message="oi", spans=(), **kw):
    run_id = f"r-{uuid4().hex[:10]}"
    started = T0 + timedelta(minutes=minute)
    fields = dict(
        run_id=run_id,
        trace_id=run_id,
        agent_type=kw.pop("agent_type", "r6"),
        agent_name="R6",
        prompt_version=1,
        endpoint="chat",
        user_id=kw.pop("user_id", "aluno-1"),
        session_id=session_id,
        message=kw.pop("raw", f"texto montado: {message}"),
        started_at=started,
        ended_at=started + timedelta(seconds=2),
        output=kw.pop("output", "resposta"),
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        cost_usd=0.001,
        agent_version=10,
        spans=list(spans),
        **kw,
    )
    if structured:
        fields.update(
            input_message=message,
            input_events=events
            if events is not None
            else [{"role": "contact", "content": message, "at": started.isoformat(), "kind": "text",
                   "attachments": [], "speech": True}],
            input_context=list(context),
        )
    run_store._write_run(RunRecord(**fields))
    return run_id


def _tool(name, args, output, minute, *, failure=None, status_message=None):
    meta = {"failure": failure} if failure else {}
    return SpanRecord(
        type="TOOL",
        name=name,
        started_at=T0 + timedelta(minutes=minute, seconds=1),
        level="ERROR" if failure else "DEFAULT",
        status_message=status_message,
        input=args,
        output=output,
        latency_ms=150.0,
        metadata=meta,
    )


def test_turno_estruturado_contexto_mudou_volatil_e_tools():
    tool = f"decidir-{uuid4().hex[:6]}"
    tool_store.create_tool(
        tool_name=tool, kind="api", label="Decidir", description=None,
        config={"url": "https://exemplo.test", "method": "POST", "params": []}, side_effect=True,
    )
    sid = f"s-{uuid4().hex[:8]}"
    first = [
        _block("divida", "SEM parcela vencida", title="Contexto da dívida"),
        _block("dados", "Nome: Maria"),
        _block("temporal", "Janela 24h ABERTA há 0.1h", volatile=True),
    ]
    second = [
        _block("divida", "1 parcela vencida", title="Contexto da dívida"),
        _block("dados", "Nome: Maria"),
        _block("temporal", "Janela 24h ABERTA há 0.4h", volatile=True),
        _block("anotacoes", "pediu 2ª via"),
    ]
    events = [
        {"role": "agent_sent", "content": "Maria, recebi seus dados", "at": "2026-11-11T12:04:00Z",
         "kind": "text", "attachments": [], "speech": False},
        {"role": "contact", "content": "segue o comprovante", "at": "2026-11-11T12:05:00Z", "kind": "document",
         "attachments": [{"filename": "comp.pdf", "mime_type": "application/pdf"}], "speech": False},
        {"role": "contact", "content": "quero parcelar", "at": "2026-11-11T12:05:30Z", "kind": "text",
         "attachments": [], "speech": True},
    ]
    r1 = _record(sid, 0, context=first, message="oi")
    r2 = _record(
        sid, 5, context=second, events=events, message="quero parcelar",
        spans=[
            _tool(tool, {"acao": "transferir", "departamento": "Secretaria"}, 'HTTP 200: {"ok": true}', 5),
            _tool("consulta", {"cpf": "1"}, "HTTP 503: fora", 5, failure="unavailable", status_message="HTTP 503: fora"),
            _tool("ficha", {"id": "9"}, "HTTP 404: nada", 5, failure="not_found", status_message="HTTP 404: nada"),
        ],
    )

    out = session_timeline(sid).model_dump(mode="json")

    assert out["session_id"] == sid and out["turns"] == 2 and out["agent_types"] == ["r6"]
    assert out["total_tokens"] == 240 and out["input_tokens"] == 200 and out["output_tokens"] == 40
    assert out["cost_usd"] == pytest.approx(0.002)
    assert out["duration_ms"] == pytest.approx(5 * 60_000 + 2_000)
    assert out["user_id"] == "aluno-1" and out["next_cursor"] is None
    t1, t2 = out["items"]
    assert [t1["run_id"], t2["run_id"]] == [r1, r2]
    # Primeiro turno estruturado: nada conta como mudado.
    assert t1["structured"] and not any(b["changed"] for b in t1["context"])
    assert t1["message"] == "oi" and t1["raw_input"] is None
    ctx = {b["key"]: b for b in t2["context"]}
    assert ctx["divida"]["changed"] and ctx["divida"]["previous_content"] == "SEM parcela vencida"
    assert ctx["divida"]["title"] == "Contexto da dívida"
    assert not ctx["dados"]["changed"] and ctx["dados"]["previous_content"] is None
    assert ctx["temporal"]["volatile"] and not ctx["temporal"]["changed"]
    assert ctx["temporal"]["previous_content"] is None
    assert ctx["anotacoes"]["changed"] and ctx["anotacoes"]["previous_content"] is None  # bloco novo
    assert ctx["dados"]["hash"] == sha256(b"Nome: Maria").hexdigest()
    # Eventos como foram gravados (kind, attachments, speech).
    assert t2["events"] == events and t2["message"] == "quero parcelar"
    ok, falhou, nao_achou = t2["steps"]
    assert ok == {
        "tool": tool, "args": {"acao": "transferir", "departamento": "Secretaria"}, "ok": True, "failure": None,
        "http_status": None, "error": None, "result_preview": 'HTTP 200: {"ok": true}', "side_effect": True,
        "latency_ms": 150.0,
    }
    assert not falhou["ok"] and falhou["failure"] == "unavailable" and falhou["error"] == "HTTP 503: fora"
    assert not falhou["side_effect"]
    assert nao_achou["ok"] and nao_achou["failure"] == "not_found"
    assert t2["output"] == "resposta" and t2["sources"] == []
    assert t2["usage"] == {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "cost_usd": 0.001,
                           "latency_ms": 2000.0}
    assert t2["agent_version"] == 10 and t2["status"] == "success" and not t2["dry_run"]


def test_turno_antigo_vem_inteiro_em_raw_input_sem_recorte():
    sid = f"s-{uuid4().hex[:8]}"
    texto = "DADOS DO ALUNO:\nNome: Maria\n[FALA DO CONTATO] quero parcelar"
    _record(sid, 0, structured=False, raw=texto)
    _record(sid, 1, context=[_block("dados", "Nome: Maria")], message="e o boleto?")
    old, new = session_timeline(sid).model_dump(mode="json")["items"]
    assert old["structured"] is False and old["events"] == [] and old["context"] == []
    assert old["message"] is None and old["raw_input"] == texto
    # O turno antigo não serve de base: o primeiro estruturado não tem com quem comparar.
    assert new["structured"] and not new["context"][0]["changed"]


def test_turno_antigo_entre_estruturados_nao_zera_a_comparacao():
    sid = f"s-{uuid4().hex[:8]}"
    _record(sid, 0, context=[_block("divida", "A")])
    _record(sid, 1, structured=False, raw="legado")
    _record(sid, 2, context=[_block("divida", "B")])
    items = session_timeline(sid).model_dump(mode="json")["items"]
    assert items[2]["context"][0]["changed"] and items[2]["context"][0]["previous_content"] == "A"


def test_paginacao_compara_o_primeiro_turno_da_pagina_com_o_anterior():
    sid = f"s-{uuid4().hex[:8]}"
    for minute, content in enumerate(["A", "A", "B"]):
        _record(sid, minute, context=[_block("divida", content)])
    page1 = session_timeline(sid, limit=2).model_dump(mode="json")
    assert len(page1["items"]) == 2 and page1["turns"] == 3 and page1["next_cursor"]
    page2 = session_timeline(sid, limit=2, cursor=page1["next_cursor"]).model_dump(mode="json")
    (last,) = page2["items"]
    assert last["context"][0]["changed"] and last["context"][0]["previous_content"] == "A"
    assert page2["next_cursor"] is None and page2["turns"] == 3


def test_fontes_saem_das_buscas_na_base():
    sid = f"s-{uuid4().hex[:8]}"
    busca = SpanRecord(
        type="TOOL", name="search_knowledge_base", started_at=T0, input={"query": "prazo"}, output="trechos",
        metadata={"references": {"query": "prazo", "collection": "manuais", "results": [
            {"content_id": "c1", "document": "Manual", "score": 0.9, "chunk_id": "k1"}]}},
    )
    _record(sid, 0, spans=[busca])
    (turn,) = session_timeline(sid).model_dump(mode="json")["items"]
    assert turn["sources"][0]["content_id"] == "c1" and turn["sources"][0]["chunk_ids"] == ["k1"]
    assert turn["steps"][0]["tool"] == "search_knowledge_base" and not turn["steps"][0]["side_effect"]


def test_filtro_por_user_id_e_404():
    sid = f"s-{uuid4().hex[:8]}"
    _record(sid, 0, user_id="a")
    _record(sid, 1, user_id="b")
    assert session_timeline(sid, user_id="a").turns == 1
    with pytest.raises(HTTPException) as exc:
        session_timeline(f"s-{uuid4().hex[:8]}")
    assert exc.value.status_code == 404


def test_timeline_pela_api_http():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agent_service.api.observability_routes import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    sid = f"s-{uuid4().hex[:8]}"
    _record(sid, 0, context=[_block("dados", "x")])
    resp = client.get(f"/observability/sessions/{sid}/timeline", params={"limit": 10})
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["context"][0]["key"] == "dados"
    assert client.get(f"/observability/sessions/{sid}/timeline", params={"limit": 999}).status_code == 422


# -- quem é o contato: nome e canal no cabeçalho e na listagem ------------------------


def test_nome_do_contato_regra_em_ordem():
    from agent_service.observability.identity import channel, contact_name

    dados = [_block("dados_aluno", "Curso: Pedagogia\n  NOME:  Maria  Souza \nCPF: 1")]
    # 3. só a linha `nome:` do bloco de dados
    assert contact_name(None, dados) == "Maria Souza"
    # 2. bloco com a chave do nome vence o bloco de dados: primeira linha não vazia
    assert contact_name(None, dados + [_block("contact_name", "\n Ana \nsobrenome")]) == "Ana"
    # 1. dependency vence tudo
    assert contact_name({"nome": "Bia", "cpf": "1"}, dados) == "Bia"
    # nada com chave conhecida: sem nome (não procura no texto de outros blocos)
    assert contact_name({"cpf": "1"}, [_block("divida", "Nome: Fulano")]) is None
    assert contact_name({"nome": 42}, None) is None
    assert contact_name(None, [_block("contact_name", "x" * 200)]).endswith("…")
    # canal: metadata antes de dependency
    assert channel({"canal": "whatsapp"}, {"channel": "email"}) == "whatsapp"
    assert channel(None, {"channel": "email"}) == "email"
    assert channel({}, {}) is None


def test_timeline_e_listagem_trazem_nome_canal_e_ultima_fala():
    from agent_service.api.observability_routes import list_sessions

    sid = f"s-{uuid4().hex[:8]}"
    agent = f"r6-{uuid4().hex[:6]}"
    _record(sid, 0, agent_type=agent, message="oi")
    _record(
        sid, 3, agent_type=agent, message="quero  parcelar\na dívida",
        context=[_block("dados_aluno", "Nome: Maria\nCurso: Pedagogia")],
        input={"message": "...", "dependencies": {"cpf": "1"}},
        metadata={"canal": "whatsapp"},
    )
    out = session_timeline(sid)
    assert out.contact_name == "Maria" and out.channel == "whatsapp"

    [session] = [s for s in list_sessions(agent_type=[agent]).items if s.session_id == sid]
    assert session.last_message == "quero parcelar a dívida"
    assert session.contact_name == "Maria" and session.channel == "whatsapp"

    # run não estruturado: a prévia é o começo do texto inteiro, e sem nome
    legacy = f"s-{uuid4().hex[:8]}"
    _record(legacy, 0, agent_type=agent, structured=False, raw="x " * 200)
    [old] = [s for s in list_sessions(agent_type=[agent]).items if s.session_id == legacy]
    assert len(old.last_message) == 160 and old.contact_name is None and old.channel is None
    assert session_timeline(legacy).contact_name is None


# -- transcrição do Agno: resumo e memórias -----------------------------------------


def _agno_run(run_id, at, message, *, tools=(), content="ok", system="você é o R6"):
    return {
        "run_id": run_id,
        "created_at": at,
        "status": "COMPLETED",
        "input": {"input_content": message},
        "run_input": message,
        "content": content,
        "metrics": {"input_tokens": 100, "output_tokens": 20, "duration": 2.71, "details": {"model": [{"id": "gemini"}]}},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": message}],
        "tools": list(tools),
    }


def test_compact_session_traz_resumo_memorias_e_a_entrada_inteira():
    tool = {
        "tool_name": "regente_r6_decidir",
        "tool_args": {"acao": "responder", "assunto": "financeiro"},
        "result": 'HTTP 200: {"ok": true}',
        "tool_call_error": False,
    }
    session = {
        "session_id": "s1",
        "agent_id": "r6",
        "user_id": "5511",
        "session_data": {"session_summary": {"summary": "aluna quer parcelar", "updated_at": "2026-10-29"}},
        "runs": [
            _agno_run("r2", 1793298800, "contexto\nÚLTIMA MENSAGEM DO ALUNO: 3x", system="v2"),
            _agno_run("r1", 1793298702, "contexto\nÚLTIMA MENSAGEM DO ALUNO: oi", tools=[tool]),
        ],
    }
    out = compact_session(session, [{"memory": "recebe dia 10", "topics": ["pagamento"], "updated_at": 1793298702}])
    first, second = out["items"]
    assert first["run_id"] == "r1" and first["entrada"] == "contexto\nÚLTIMA MENSAGEM DO ALUNO: oi"
    assert first["passos"] == ['regente_r6_decidir(acao=responder, assunto=financeiro) → HTTP 200: {"ok": true}']
    assert first["at"] == "29/10/2026 15:31:42"  # BRT
    assert second["prompt_mudou"]
    assert out["resumo_da_sessao"]["texto"] == "aluna quer parcelar"
    assert out["memorias_do_contato"][0]["texto"] == "recebe dia 10"
    assert "fala" not in first and "contexto" not in first and "messages" not in first
