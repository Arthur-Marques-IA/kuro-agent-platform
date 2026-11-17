"""Falha de tool: a API da tool respondeu erro, o agente seguiu — e o trace tem
que mostrar isso (span ERROR, `tool_failures` no run), sem mudar o `status`."""

import asyncio
from contextlib import aclosing
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from agno.models.response import ToolExecution
from agno.run.agent import RunCompletedEvent, ToolCallCompletedEvent, ToolCallErrorEvent

from agent_service.api.observability_routes import get_run_trace
from agent_service.api.tools_routes import (
    ToolIn,
    ToolInvokeIn,
    ToolUpdateIn,
    create_tool,
    delete_tool,
    invoke_tool,
    update_tool,
)
from agent_service.observability import trace_store, tracing
from agent_service.observability.tracing import RunContext
from agent_service.tools import api_tool, failures
from agent_service.tools.api_tool import build_api_function

CONFIG = {
    "method": "POST",
    "url": "https://exemplo.test/ofertas",
    "parameters": [{"name": "valor", "type": "number", "location": "body", "required": True}],
}


@pytest.fixture(autouse=True)
def local_backend(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)
    trace_store.set_trace_store(None)
    yield
    trace_store.set_trace_store(None)


def rede(status: int = 200, payload=None, erro: Exception | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if erro is not None:
            raise erro
        return httpx.Response(status, json=payload or {"ok": status < 400})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (200, None),
        (201, None),
        (400, "invalid_arguments"),
        (422, "invalid_arguments"),
        (401, "auth"),
        (403, "auth"),
        (404, "not_found"),
        (429, "unavailable"),
        (500, "unavailable"),
        (503, "unavailable"),
    ],
)
def test_classificacao_http(status, kind):
    assert failures.classify_http(status) == kind


@pytest.mark.parametrize(
    ("nomes", "nivel"),
    [
        ([], 1),
        (["update_user_memory", "search_knowledge_base"], 1),
        (["ficha"], 2),
        (["ficha", "ficha", "ficha", "lead"], 2),
        (["ficha", "lead", "oferta"], 3),
    ],
)
def test_complexidade_conta_tools_de_negocio_distintas(nomes, nivel):
    assert failures.complexity(nomes) == nivel


def _chamar(monkeypatch, client, **args) -> str:
    monkeypatch.setattr(api_tool, "_client", client)
    fn = build_api_function(tool_name="oferta", description=None, config=CONFIG)

    async def run():
        failures.start_run()
        resultado = await fn.entrypoint(**args)
        return resultado, failures.lookup_failure("oferta", resultado)

    return asyncio.run(run())


def test_http_500_e_registrado_mas_o_modelo_continua_lendo_o_texto(monkeypatch):
    resultado, falha = _chamar(monkeypatch, rede(500, {"mensagem": "Falha ao gravar"}), valor=10)
    assert resultado.startswith("HTTP 500: ")
    assert falha == ("unavailable", 500)


def test_sucesso_nao_e_registrado(monkeypatch):
    _, falha = _chamar(monkeypatch, rede(200), valor=10)
    assert falha is None


def test_parametro_faltando_e_argumento_invalido(monkeypatch):
    resultado, falha = _chamar(monkeypatch, rede(200))
    assert "faltam parâmetros" in resultado
    assert falha == ("invalid_arguments", None)


def test_timeout_diz_o_tipo_do_erro(monkeypatch):
    """Antes saía "Falha ao chamar a API: " — o timeout do httpx vem sem mensagem."""
    resultado, falha = _chamar(monkeypatch, rede(erro=httpx.ReadTimeout("")), valor=10)
    assert resultado == "Falha ao chamar a API: ReadTimeout"
    assert falha == ("unavailable", None)


# -- no trace ----------------------------------------------------------------------


class AgenteQueChamaATool:
    """Chama a tool real de dentro do run e emite o evento como o Agno emitiria."""

    def __init__(self, fn, **args) -> None:
        self.fn, self.args = fn, args

    def arun(self, message, **kwargs):
        async def stream():
            resultado = await self.fn.entrypoint(**self.args)
            yield ToolCallCompletedEvent(tool=ToolExecution(tool_name="oferta", tool_args=self.args, result=resultado))
            yield ToolCallErrorEvent(
                tool=ToolExecution(tool_name="script", tool_args={}, result="boom", tool_call_error=True), error="boom"
            )
            yield RunCompletedEvent(run_id=kwargs.get("run_id"), content="Não consegui registrar a oferta agora.")

        return stream()


def test_run_com_tool_falhando_continua_success_mas_conta_a_falha(monkeypatch):
    monkeypatch.setattr(api_tool, "_client", rede(500))
    agent = AgenteQueChamaATool(build_api_function(tool_name="oferta", description=None, config=CONFIG), valor=10)
    run = RunContext(
        endpoint="chat",
        agent_type=f"agente-{uuid4().hex[:8]}",
        agent_name="t",
        prompt_version=1,
        user_id="u",
        session_id="s",
        message="fecha por 10",
    )

    async def consumir():
        async with aclosing(tracing.traced_run_events(agent, run)) as eventos:
            async for _ in eventos:
                pass

    asyncio.run(consumir())
    trace = get_run_trace(run.run_id)

    assert trace.run.status == "success"
    assert (trace.run.tool_calls, trace.run.tool_failures, trace.run.complexity) == (2, 2, 2)
    oferta = next(s for s in trace.spans if s.name == "oferta")
    assert oferta.level == "ERROR"
    assert oferta.metadata == {"failure": "unavailable", "http_status": 500}
    script = next(s for s in trace.spans if s.name == "script")
    assert script.level == "ERROR" and script.metadata == {"failure": "exception"}


# -- teste da tool pelo console / CLI ------------------------------------------------


@pytest.fixture
def tool_oferta():
    create_tool(ToolIn(tool_name="oferta_teste", kind="api", label="Oferta", config=CONFIG))
    yield "oferta_teste"
    delete_tool("oferta_teste")


def test_invoke_com_http_422_sai_com_ok_false(monkeypatch, tool_oferta):
    monkeypatch.setattr(api_tool, "_client", rede(422, {"mensagem": "Informe oferta_id"}))
    out = asyncio.run(invoke_tool(tool_oferta, ToolInvokeIn(arguments={"valor": 1})))
    assert out["ok"] is False
    assert (out["failure"], out["http_status"]) == ("invalid_arguments", 422)
    assert out["result"].startswith("HTTP 422")


def test_side_effect_e_opcional_e_volta_para_nao_classificada(tool_oferta):
    assert update_tool(tool_oferta, ToolUpdateIn(label="Oferta"))["side_effect"] is None
    assert update_tool(tool_oferta, ToolUpdateIn(side_effect=True))["side_effect"] is True
    # Omitir não mexe; `null` explícito volta para "não classificada".
    assert update_tool(tool_oferta, ToolUpdateIn(label="Oferta 2"))["side_effect"] is True
    assert update_tool(tool_oferta, ToolUpdateIn.model_validate({"side_effect": None}))["side_effect"] is None


# -- migração dos runs antigos ------------------------------------------------------


def test_migracao_reclassifica_falhas_antigas_so_de_tools_api(tmp_path):
    from alembic import command

    from agent_service.migrations import _config

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'antigo.db'}")
    config = _config()
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "0005")
        conn.execute(
            sa.text(
                "INSERT INTO tool_definitions (tool_name, kind, label, config, enabled, is_seed) VALUES "
                "('ficha', 'api', 'Ficha', '{}', 1, 0), ('script', 'python', 'Script', '{}', 1, 0)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO runs (run_id, trace_id, tenant_id, agent_type, status, input_tokens, output_tokens, "
                "total_tokens, started_at) VALUES ('r1', 't1', 'default', 'r8', 'success', 0, 0, 0, '2026-09-30'), "
                "('r2', 't2', 'default', 'r8', 'success', 0, 0, 0, '2026-09-30')"
            )
        )
        for span_id, name, output in [
            ("s1", "ficha", '"HTTP 500: {\\"ok\\": false}"'),
            ("s2", "ficha", '"HTTP 200: {\\"ok\\": true}"'),
            ("s3", "script", '"HTTP 500: texto qualquer de uma tool python"'),
            ("s4", "ficha", '"HTTP 404: {\\"ok\\": false}"'),
        ]:
            conn.execute(
                sa.text(
                    "INSERT INTO run_spans (id, run_id, type, name, level, output, input_tokens, output_tokens, "
                    f"total_tokens, started_at) VALUES ('{span_id}', 'r1', 'TOOL', '{name}', 'DEFAULT', '{output}', 0, 0, 0, '2026-09-30')"
                )
            )
        command.upgrade(config, "head")

    with engine.connect() as conn:
        spans = {r.id: r for r in conn.execute(sa.text("SELECT id, level, metadata FROM run_spans"))}
        r1 = conn.execute(sa.text("SELECT tool_calls, tool_failures, complexity FROM runs WHERE run_id='r1'")).one()
        r2 = conn.execute(sa.text("SELECT tool_calls, tool_failures, complexity FROM runs WHERE run_id='r2'")).one()
    assert spans["s1"].level == "ERROR" and "unavailable" in spans["s1"].metadata
    assert spans["s2"].level == "DEFAULT"
    assert spans["s3"].level == "DEFAULT", "tool python não é reclassificada pelo texto"
    assert spans["s4"].level == "WARNING" and "not_found" in spans["s4"].metadata
    assert tuple(r1) == (4, 1, 2), "o 404 conta como chamada, não como falha"
    assert tuple(r2) == (0, 0, 1)


# -- 404 não é falha ----------------------------------------------------------------


def test_404_no_run_vira_aviso_e_nao_conta(monkeypatch):
    monkeypatch.setattr(api_tool, "_client", rede(404, {"mensagem": "sem sessões anteriores"}))

    class Agente:
        def arun(self, message, **kwargs):
            async def stream():
                fn = build_api_function(tool_name="oferta", description=None, config=CONFIG)
                resultado = await fn.entrypoint(valor=1)
                yield ToolCallCompletedEvent(tool=ToolExecution(tool_name="oferta", tool_args={}, result=resultado))
                yield RunCompletedEvent(run_id=kwargs.get("run_id"), content="ok")

            return stream()

    run = RunContext(
        endpoint="chat",
        agent_type=f"agente-{uuid4().hex[:8]}",
        agent_name="t",
        prompt_version=1,
        user_id="u",
        session_id="s",
        message="m",
    )

    async def consumir():
        async with aclosing(tracing.traced_run_events(Agente(), run)) as eventos:
            async for _ in eventos:
                pass

    asyncio.run(consumir())
    trace = get_run_trace(run.run_id)
    assert (trace.run.tool_calls, trace.run.tool_failures) == (1, 0)
    [span] = [s for s in trace.spans if s.type == "TOOL"]
    assert span.level == "WARNING" and span.metadata == {"failure": "not_found", "http_status": 404}


def test_invoke_com_404_e_ok_mas_indica_o_tipo(monkeypatch, tool_oferta):
    monkeypatch.setattr(api_tool, "_client", rede(404))
    out = asyncio.run(invoke_tool(tool_oferta, ToolInvokeIn(arguments={"valor": 1})))
    assert out["ok"] is True
    assert (out["failure"], out["http_status"]) == ("not_found", 404)
