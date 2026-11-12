"""Agente procedural: regras das etapas (puras), validação no cadastro e o turno
inteiro pelo `/chat` — com a extração e a resposta do modelo falsas e a tool de
verdade, com o HTTP interceptado."""

import asyncio
import json

import httpx
import pytest
from agno.run.agent import RunCompletedEvent, RunContentEvent
from fastapi import HTTPException

from agent_service.agents import procedural, procedure_runner, procedure_store
from agent_service.agents.procedural import (
    StageSpecError,
    apply_extraction,
    confirmation_text,
    current_index,
    finish_action,
    new_state,
    start_action,
    state_view,
    validate_stages,
)
from agent_service.agents.versions import config_hash, effective_config
from agent_service.api.agents_routes import (
    AgentDefinitionIn,
    AgentDefinitionUpdate,
    create_agent,
    delete_agent,
    get_procedure_funnel,
    get_procedure_state,
    update_agent,
)
from agent_service.api.routes import ChatRequest, chat, chat_stream
from agent_service.tools import api_tool
from agent_service.tools import store as tool_store

STAGES = [
    {
        "id": "identificacao",
        "goal": "Obter o CPF e o nome",
        "fields": [
            {"name": "cpf", "type": "string", "label": "CPF", "required": True, "pattern": r"^\d{11}$"},
            {"name": "nome", "type": "string", "label": "Nome", "required": True},
        ],
    },
    {
        "id": "problema",
        "goal": "Entender o problema",
        "fields": [
            {"name": "categoria", "type": "string", "label": "Categoria", "required": True, "enum": ["internet", "tv"]}
        ],
    },
    {"id": "confirmacao", "type": "confirm"},
    {"id": "abrir", "type": "action", "tool": "abrir_chamado_teste"},
]


@pytest.fixture
def stages():
    return validate_stages(STAGES)


# -- validação da definição -----------------------------------------------------------


@pytest.mark.parametrize(
    ("stages_raw", "trecho"),
    [
        ([], "ao menos uma etapa"),
        ([{"id": "a", "fields": [{"name": "x", "required": True}]}, {"id": "a", "type": "confirm"}], "repetido"),
        (
            [
                {"id": "a", "fields": [{"name": "x", "required": True}]},
                {"id": "b", "fields": [{"name": "x", "required": True}]},
            ],
            "cada campo",
        ),
        ([{"id": "a", "fields": [{"name": "x"}]}], "obrigatório"),
        ([{"id": "c", "type": "confirm"}], "depois de uma etapa de coleta"),
        (
            [{"id": "a", "fields": [{"name": "x", "required": True}]}, {"id": "b", "type": "action", "tool": "t"}],
            "confirm antes",
        ),
        ([{"id": "a", "fields": [{"name": "x", "required": True, "pattern": "("}]}], "pattern inválido"),
        ([{"id": "a", "fields": [{"name": "confirmacao", "required": True}]}], "reservado"),
        ([{"id": "A B", "fields": [{"name": "x", "required": True}]}], "id inválido"),
    ],
)
def test_validate_stages_recusa(stages_raw, trecho):
    with pytest.raises(StageSpecError) as exc:
        validate_stages(stages_raw)
    assert trecho in str(exc.value)


def test_action_depois_de_outra_action_exige_nova_confirmacao():
    with pytest.raises(StageSpecError):
        validate_stages(STAGES + [{"id": "notificar", "type": "action", "tool": "t"}])


# -- regras de transição (puras) ---------------------------------------------------------


def test_dependencies_preenchem_a_etapa_e_valor_invalido_e_ignorado(stages):
    state = new_state(stages, {"cpf": "12345678901", "nome": "Ana"})
    assert current_index(stages, state) == 1
    assert new_state(stages, {"cpf": "123"})["slots"] == {}


def test_extracao_avanca_e_revalida(stages):
    state = new_state(stages)
    state, changed = apply_extraction(stages, state, {"cpf": "123", "nome": "Ana", "categoria": "fogão"})
    assert changed == ["nome"]
    assert state["invalid"] == {"cpf": "formato inválido", "categoria": "precisa ser um de: internet, tv"}
    assert current_index(stages, state) == 0
    state, _ = apply_extraction(stages, state, {"cpf": "12345678901"})
    assert state["invalid"] == {}  # o inválido é só da mensagem em que veio
    assert current_index(stages, state) == 1


def _ate_a_confirmacao(stages):
    state, _ = apply_extraction(stages, new_state(stages), {"cpf": "12345678901", "nome": "Ana", "categoria": "tv"})
    assert stages[current_index(stages, state)]["type"] == "confirm"
    return state


def test_sim_confirma_e_libera_a_acao(stages):
    state, _ = apply_extraction(stages, _ate_a_confirmacao(stages), {"confirmacao": "sim"})
    assert stages[current_index(stages, state)]["type"] == "action"


def test_sim_junto_com_correcao_nao_confirma(stages):
    state, changed = apply_extraction(stages, _ate_a_confirmacao(stages), {"confirmacao": "sim", "nome": "Ana Maria"})
    assert changed == ["nome"]
    assert stages[current_index(stages, state)]["type"] == "confirm"


def test_corrigir_campo_depois_de_confirmar_desfaz_a_confirmacao_e_volta(stages):
    state, _ = apply_extraction(stages, _ate_a_confirmacao(stages), {"confirmacao": "sim"})
    state, _ = apply_extraction(stages, state, {"categoria": "internet"})
    assert state["confirmed"] == []
    assert stages[current_index(stages, state)]["id"] == "confirmacao"


def test_nao_marca_recusa_sem_confirmar(stages):
    state, _ = apply_extraction(stages, _ate_a_confirmacao(stages), {"confirmacao": "nao"})
    assert state["declined"] is True and state["confirmed"] == []


def test_campo_usado_por_acao_executada_nao_muda(stages):
    state, _ = apply_extraction(stages, _ate_a_confirmacao(stages), {"confirmacao": "sim"})
    state = finish_action(stages, start_action(state, "abrir"), "abrir", {"ok": True, "result": "#42"})
    assert current_index(stages, state) is None
    state, changed = apply_extraction(stages, state, {"cpf": "99999999999"})
    assert changed == [] and state["slots"]["cpf"] == "12345678901"
    assert "já foi executada" in state["invalid"]["cpf"]


def test_acao_que_falha_exige_confirmar_de_novo(stages):
    state, _ = apply_extraction(stages, _ate_a_confirmacao(stages), {"confirmacao": "sim"})
    state = finish_action(stages, start_action(state, "abrir"), "abrir", {"ok": False, "error": "HTTP 500"})
    assert stages[current_index(stages, state)]["id"] == "confirmacao"
    assert state["actions"]["abrir"] == {"status": "failed", "attempts": 1, "error": "HTTP 500"}


def test_confirmacao_e_montada_dos_dados(stages):
    texto = confirmation_text(stages, _ate_a_confirmacao(stages), 2)
    assert "- CPF: 12345678901" in texto and "- Categoria: tv" in texto


def test_state_view(stages):
    view = state_view(stages, new_state(stages, {"cpf": "12345678901"}))
    assert view["stage"] == "identificacao" and view["stage_index"] == 0 and view["stages_total"] == 4
    assert [f["name"] for f in view["missing"]] == ["nome"]
    assert [s["status"] for s in view["stages"]] == ["current", "pending", "pending", "pending"]
    assert view["done"] is False and view["result"] is None


def test_prompt_da_confirmacao_lista_o_que_pode_ser_corrigido(stages):
    """Na confirmação nada falta; sem a lista, o modelo olhava só o sim/não e
    deixava passar a correção que vinha na mesma frase."""
    prompt = procedural.extraction_prompt(stages, _ate_a_confirmacao(stages), "não, é na TV", "Confira...")
    assert "pode corrigir agora" in prompt and "categoria (Categoria: internet | tv)" in prompt


def test_schema_da_extracao_tem_tudo_opcional_e_a_confirmacao(stages):
    model = procedural.extraction_model("teste", stages)
    assert model().model_dump() == {"cpf": None, "nome": None, "categoria": None, "confirmacao": None}
    with pytest.raises(Exception):
        model(confirmacao="talvez")


# -- cadastro ---------------------------------------------------------------------------


@pytest.fixture
def tool_de_acao():
    tool_store.create_tool(
        tool_name="abrir_chamado_teste",
        kind="api",
        label="Abrir chamado",
        description=None,
        side_effect=True,
        config={
            "method": "POST",
            "url": "https://exemplo.test/chamados",
            "parameters": [
                {"name": "cpf", "type": "string", "location": "body", "required": True},
                {"name": "categoria", "type": "string", "location": "body", "required": True},
                {
                    "name": "chave",
                    "type": "string",
                    "location": "header",
                    "required": True,
                    "source": "dependency",
                    "dependency": "idempotency_key",
                },
            ],
        },
    )
    yield "abrir_chamado_teste"
    tool_store.delete_tool("abrir_chamado_teste")


def _criar(agent_type="proc_teste", **extra):
    return create_agent(
        AgentDefinitionIn(
            agent_type=agent_type,
            name="Abertura de chamado",
            instructions=["Você abre chamados técnicos."],
            kind="procedural",
            stages=extra.pop("stages", STAGES),
            **extra,
        )
    )


@pytest.fixture
def agente(tool_de_acao):
    _criar()
    yield "proc_teste"
    delete_agent("proc_teste")


def test_procedural_sem_stages_e_422():
    with pytest.raises(HTTPException) as exc:
        create_agent(AgentDefinitionIn(agent_type="x", name="X", instructions=["a"], kind="procedural"))
    assert exc.value.status_code == 422


def test_stages_em_agente_conversacional_e_422():
    with pytest.raises(HTTPException) as exc:
        create_agent(AgentDefinitionIn(agent_type="x", name="X", instructions=["a"], stages=STAGES))
    assert exc.value.status_code == 422


def test_acao_com_tool_que_nao_existe_e_422():
    with pytest.raises(HTTPException) as exc:
        _criar(agent_type="x")
    assert "tool desconhecida" in exc.value.detail


def test_acao_precisa_receber_os_parametros_da_tool(tool_de_acao):
    sem_categoria = [STAGES[0], {"id": "confirmacao", "type": "confirm"}, STAGES[3]]
    with pytest.raises(HTTPException) as exc:
        _criar(agent_type="x", stages=sem_categoria)
    assert "categoria" in exc.value.detail


def test_tool_com_efeito_colateral_nao_entra_em_tools(tool_de_acao):
    with pytest.raises(HTTPException) as exc:
        _criar(agent_type="x", tools=[tool_de_acao])
    assert "etapa" in exc.value.detail and "action" in exc.value.detail


def test_tipo_procedural_nao_muda(agente):
    with pytest.raises(HTTPException) as exc:
        update_agent(agente, AgentDefinitionUpdate(kind="conversational", stages=[]))
    assert exc.value.status_code == 422


def test_stages_entram_na_versao_so_do_procedural(agente):
    from agent_service.agents.store import get_definition

    definition = get_definition(agente)
    assert effective_config(definition)["stages"][0]["id"] == "identificacao"
    conversational = {**definition, "kind": "conversational", "stages": []}
    assert "stages" not in effective_config(conversational)
    assert config_hash(effective_config(definition)) != config_hash(
        effective_config({**definition, "stages": STAGES[:1]})
    )


def test_contrato_de_integracao_mostra_etapas_e_onde_ler_o_estado(agente):
    from agent_service.api.integration_routes import get_integration_contract

    contrato = get_integration_contract(agente, base_url="https://kuro.test")
    assert contrato.endpoint == "/chat" and contrato.stream_url == "https://kuro.test/chat/stream"
    assert [s["id"] for s in contrato.stages] == ["identificacao", "problema", "confirmacao", "abrir"]
    assert contrato.state_url == "https://kuro.test/agents/proc_teste/procedures/{session_id}"
    # A tool da etapa action pede idempotency_key, que o servidor põe: não é aviso.
    assert contrato.warnings == []


# -- o turno, pelo /chat -------------------------------------------------------------------


class Modelo:
    """Extração e resposta falsas: o que importa aqui é o que o servidor faz com elas."""

    def __init__(self, monkeypatch):
        self.extracoes: list[dict] = []
        self.contextos: list[dict] = []
        monkeypatch.setattr(procedure_runner, "extract", self._extract)
        monkeypatch.setattr(procedure_runner, "traced_run_events", self._reply)

    def responde(self, **valores):
        self.extracoes.append(valores)

    async def _extract(self, definition, state, message, last_reply):
        return (self.extracoes.pop(0) if self.extracoes else {}), []

    def _reply(self, agent, run):
        self.contextos.append(run.dependencies["procedimento"])

        async def stream():
            yield RunContentEvent(content="resposta do modelo")
            yield RunCompletedEvent(run_id=run.run_id, content="resposta do modelo")

        return stream()


@pytest.fixture
def chamadas_http():
    chamadas: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(request)
        return httpx.Response(200, json={"protocolo": "#42"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    original = api_tool.get_client
    api_tool.get_client = lambda: client
    try:
        yield chamadas
    finally:
        api_tool.get_client = original


def _chat(agent_type, mensagem, sessao="s-proc", dry_run=False):
    return asyncio.run(
        chat(ChatRequest(agent_type=agent_type, user_id="u", session_id=sessao, message=mensagem, dry_run=dry_run))
    )


def test_conversa_inteira_coleta_confirma_executa(agente, monkeypatch, chamadas_http):
    modelo = Modelo(monkeypatch)

    modelo.responde(nome="Ana")
    r = _chat(agente, "oi, sou a Ana")
    assert r.state["stage"] == "identificacao" and [f["name"] for f in r.state["missing"]] == ["cpf"]
    assert r.content == "resposta do modelo"
    assert modelo.contextos[-1]["faltando"][0]["name"] == "cpf"

    modelo.responde(cpf="12345678901", categoria="internet")
    r = _chat(agente, "cpf 12345678901, minha internet caiu")
    # Confirmação montada pelo servidor, sem o modelo redigir.
    assert r.state["stage"] == "confirmacao"
    assert "- CPF: 12345678901" in r.content and "- Categoria: internet" in r.content
    assert len(modelo.contextos) == 1
    assert chamadas_http == []

    modelo.responde(confirmacao="sim")
    r = _chat(agente, "sim")
    assert r.state["done"] is True
    assert "#42" in str(r.state["result"]["actions"]["abrir"])
    assert r.state["result"]["collected"]["categoria"] == "internet"
    assert len(chamadas_http) == 1
    corpo = json.loads(chamadas_http[0].content)
    assert corpo == {"cpf": "12345678901", "categoria": "internet"}
    linha, etapa, dados = chamadas_http[0].headers["chave"].split(":")  # idempotency_key
    assert etapa == "abrir" and len(dados) == 16
    assert modelo.contextos[-1]["concluido"] is True
    # A resposta seguinte sabe o que o agente disse por último (a confirmação veio sem o modelo).
    modelo.responde()
    _chat(agente, "obrigada")
    assert "resposta do modelo" in modelo.contextos[-1]["ultima_mensagem_do_agente"]
    assert len(chamadas_http) == 1  # concluído: nada roda de novo

    assert get_procedure_state(agente, "s-proc")["done"] is True
    funil = get_procedure_funnel(agente)
    assert funil["done"] == 1 and funil["total"] == 1


def test_acao_que_falha_nao_avanca_e_nao_repete_sozinha(agente, monkeypatch):
    modelo = Modelo(monkeypatch)
    chamadas: list[httpx.Request] = []

    def handler(request):
        chamadas.append(request)
        return httpx.Response(500, json={"erro": "fora do ar"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(api_tool, "get_client", lambda: client)

    modelo.responde(cpf="12345678901", nome="Ana", categoria="tv")
    _chat(agente, "dados", sessao="s-falha")
    modelo.responde(confirmacao="sim")
    r = _chat(agente, "sim", sessao="s-falha")
    assert len(chamadas) == 1
    assert r.state["stage"] == "confirmacao" and r.state["actions"]["abrir"]["status"] == "failed"
    assert "acao_falhou" in modelo.contextos[-1]
    modelo.responde()
    r = _chat(agente, "e aí?", sessao="s-falha")
    assert len(chamadas) == 1  # sem confirmar de novo, não tenta de novo
    assert "- CPF" in r.content


def test_acao_em_andamento_em_outra_mensagem_e_409(agente, monkeypatch, chamadas_http):
    modelo = Modelo(monkeypatch)
    modelo.responde(cpf="12345678901", nome="Ana", categoria="tv")
    _chat(agente, "dados", sessao="s-corrida")
    row = procedure_store.get_procedure(agente, "s-corrida")
    state, _ = apply_extraction(validate_stages(STAGES), row["state"], {"confirmacao": "sim"})
    procedure_store.save_procedure(row, state=start_action(state, "abrir"), stage="abrir")

    modelo.responde()
    with pytest.raises(HTTPException) as exc:
        _chat(agente, "sim", sessao="s-corrida")
    assert exc.value.status_code == 409
    assert chamadas_http == []


def test_dry_run_chega_na_acao_e_fica_fora_do_funil(agente, monkeypatch, chamadas_http):
    modelo = Modelo(monkeypatch)
    modelo.responde(cpf="12345678901", nome="Ana", categoria="tv")
    _chat(agente, "dados", sessao="s-teste", dry_run=True)
    modelo.responde(confirmacao="sim")
    _chat(agente, "sim", sessao="s-teste", dry_run=True)
    assert chamadas_http[0].headers["X-Kuro-Dry-Run"] == "true"
    assert get_procedure_funnel(agente)["total"] == 0
    assert get_procedure_funnel(agente, include_dry_run=True)["total"] == 1


def test_stream_manda_o_estado_antes_do_done(agente, monkeypatch):
    modelo = Modelo(monkeypatch)
    modelo.responde(nome="Ana")

    async def consumir():
        response = await chat_stream(ChatRequest(agent_type=agente, user_id="u", session_id="s-stream", message="oi"))
        return [chunk async for chunk in response.body_iterator]

    eventos = [e if isinstance(e, dict) else {} for e in asyncio.run(consumir())]
    nomes = [e.get("event") for e in eventos]
    assert nomes.index("state") < nomes.index("done")
    estado = json.loads(eventos[nomes.index("state")]["data"])
    assert estado["stage"] == "identificacao"


# -- correções da revisão --------------------------------------------------------------------


def test_conversa_concluida_nao_reexecuta_acao_depois_de_editar_o_agente(agente, monkeypatch, chamadas_http):
    """Renomear a etapa action de um agente não pode fazer uma conversa já
    concluída executar a "nova" ação em cima do sim que foi dado para a antiga."""
    modelo = Modelo(monkeypatch)
    modelo.responde(cpf="12345678901", nome="Ana", categoria="tv")
    _chat(agente, "dados", sessao="s-editado")
    modelo.responde(confirmacao="sim")
    assert _chat(agente, "sim", sessao="s-editado").state["done"] is True
    assert len(chamadas_http) == 1

    renomeado = [*STAGES[:3], {**STAGES[3], "id": "abrir-chamado"}]
    update_agent(agente, AgentDefinitionUpdate(stages=renomeado))
    modelo.responde(confirmacao="sim")
    r = _chat(agente, "obrigada", sessao="s-editado")
    assert len(chamadas_http) == 1
    assert r.state["done"] is True
    assert get_procedure_state(agente, "s-editado")["done"] is True


def test_editar_o_agente_no_meio_desfaz_a_confirmacao(stages):
    state, _ = apply_extraction(stages, _ate_a_confirmacao(stages), {"confirmacao": "sim"})
    assert state["confirmed"] == ["confirmacao"]
    editado = validate_stages([*STAGES[:3], {**STAGES[3], "id": "abrir-chamado"}])
    sincronizado = procedural.sync_with_definition(editado, state)
    assert sincronizado["confirmed"] == []
    assert procedural.sync_with_definition(stages, state) is state  # sem edição, nada muda


def test_falha_na_extracao_e_502_e_fica_registrada(agente, monkeypatch):
    from agent_service.observability import run_store

    gravados = []
    monkeypatch.setattr(run_store, "record_run", gravados.append)

    async def falha(definition, state, message, last_reply):
        raise RuntimeError("429 quota excedida")

    monkeypatch.setattr(procedure_runner, "extract", falha)
    with pytest.raises(HTTPException) as exc:
        _chat(agente, "oi", sessao="s-falha-modelo")
    assert exc.value.status_code == 502 and "quota" in exc.value.detail
    assert [r.status for r in gravados] == ["error"]
    assert "quota" in gravados[0].status_message


def test_chave_de_idempotencia_estavel_por_dados():
    linha = {"id": "abc"}
    dados = {"cpf": "12345678901", "categoria": "tv"}
    chave = procedure_runner._idempotency_key(linha, "abrir", dados)
    assert chave == procedure_runner._idempotency_key(linha, "abrir", dict(reversed(list(dados.items()))))
    assert chave != procedure_runner._idempotency_key(linha, "abrir", {**dados, "categoria": "internet"})


def test_promote_nao_troca_o_tipo_de_um_procedural(agente):
    from agent_service.api.agents_routes import PromoteIn, promote_agent

    create_agent(AgentDefinitionIn(agent_type="conv_destino", name="Destino", instructions=["oi"]))
    try:
        with pytest.raises(HTTPException) as exc:
            promote_agent(agente, PromoteIn(to="conv_destino"))
        assert exc.value.status_code == 422
    finally:
        delete_agent("conv_destino")


def test_confirmacao_entra_na_sessao_do_agno():
    """A confirmação não passa pelo modelo, mas precisa estar no histórico: é o
    que o modelo lê na mensagem seguinte e o que o console mostra ao reabrir."""
    from agno.session.agent import AgentSession

    from agent_service.observability.tracing import RunContext

    class BancoFalso:
        """Como o Postgres do Agno: a linha da sessão e os runs gravados separados."""

        def __init__(self):
            self.sessoes, self.runs = [], []

        def upsert_session(self, session):
            self.sessoes.append(session)

        def upsert_run(self, run, session_id, user_id=None):
            self.runs.append((run, session_id, user_id))

    class AgenteFalso:
        id, name = "proc", "Proc"

        def __init__(self, existe):
            self.db = BancoFalso()
            self.existe = existe

        def get_session(self, session_id, user_id):
            if not self.existe:
                raise Exception("Session not found")
            return AgentSession(session_id=session_id, agent_id=self.id, user_id=user_id)

    run = RunContext(
        endpoint="chat",
        agent_type="proc",
        agent_name="Proc",
        prompt_version=1,
        user_id="u",
        session_id="s1",
        message="dados",
    )

    nova = AgenteFalso(existe=False)
    procedure_runner._append_to_session(nova, run, "Confira:\n\n- CPF: 1")
    assert [s.session_id for s in nova.db.sessoes] == ["s1"]  # a linha da sessão, senão o run fica órfão
    ((saida, sessao, usuario),) = nova.db.runs
    assert (sessao, usuario) == ("s1", "u")
    assert [m.role for m in saida.messages] == ["user", "assistant"]
    assert saida.content == "Confira:\n\n- CPF: 1" and saida.run_id == run.run_id

    existente = AgenteFalso(existe=True)
    procedure_runner._append_to_session(existente, run, "Confira")
    assert existente.db.sessoes == [] and len(existente.db.runs) == 1
