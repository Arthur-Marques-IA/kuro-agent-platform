"""O aviso de que o contexto da sessão mudou, e as dependencies na composição do prompt.

O contexto vigente vai no prompt de sistema, antes do histórico. Medido com um modelo real,
as respostas antigas do agente às vezes venciam o contexto novo; um aviso no turno em que o
bloco muda resolveu (`structured_input.context_change_notice`)."""

from types import SimpleNamespace

from agent_service.agents import session_context, structured_input
from agent_service.agents.structured_input import ContextBlock, InputEvent, context_change_notice, context_for_storage
from agent_service.api import routes
from agent_service.observability import prompt_composition


def _ctx(**blocks: str) -> list[dict]:
    return context_for_storage([ContextBlock(key=k, title=k.capitalize(), content=v) for k, v in blocks.items()])


def test_sem_contexto_anterior_nao_ha_aviso():
    assert context_change_notice(None, _ctx(divida="R$ 100")) is None


def test_contexto_igual_nao_gera_aviso():
    assert context_change_notice(_ctx(divida="R$ 100"), _ctx(divida="R$ 100")) is None


def test_bloco_mudado_novo_e_removido_aparecem_pelo_titulo():
    aviso = context_change_notice(
        _ctx(divida="R$ 100", dados="Maria", obs="x"), _ctx(divida="R$ 250", dados="Maria", novo="y")
    )
    assert aviso is not None
    assert 'bloco "Divida"' in aviso
    assert 'bloco novo "Novo"' in aviso
    assert 'bloco removido "Obs"' in aviso
    assert '"Dados"' not in aviso
    # O conteúdo dos blocos não entra no aviso (ele fica no histórico).
    assert "250" not in aviso and "100" not in aviso


def test_limpar_o_contexto_avisa_os_blocos_removidos():
    aviso = context_change_notice(_ctx(divida="R$ 100"), [])
    assert aviso is not None and 'bloco removido "Divida"' in aviso


def test_bloco_volatil_nao_conta():
    antes = context_for_storage([ContextBlock(key="tempo", title="Tempo", content="há 1 min", volatile=True)])
    depois = context_for_storage([ContextBlock(key="tempo", title="Tempo", content="há 2 min", volatile=True)])
    assert context_change_notice(antes, depois) is None


def test_aviso_entra_como_primeiro_evento_do_sistema():
    eventos = [InputEvent(role="human_agent", content="um momento")]
    texto = structured_input.build_turn_text("quanto devo?", eventos, notice="O contexto mudou.")
    assert texto.index("[Sistema]\nO contexto mudou.") < texto.index("[Atendente humano]")
    assert texto.endswith("quanto devo?")
    # Sem aviso, o texto é o de sempre.
    assert "[Sistema]" not in structured_input.build_turn_text("quanto devo?", eventos)


def test_aviso_sozinho_abre_a_secao_de_eventos():
    texto = structured_input.build_turn_text("oi", None, notice="O contexto mudou.")
    assert texto.startswith(structured_input.EVENTS_HEADING)
    assert structured_input.MESSAGE_HEADING in texto


def _request(**extra):
    return routes.ChatRequest(agent_type="a", user_id="u", session_id="s-aviso", message="quanto devo?", **extra)


def test_a_rota_poe_o_aviso_quando_o_contexto_enviado_difere_do_vigente(monkeypatch):
    monkeypatch.setattr(session_context, "current_context", lambda _sid: _ctx(divida="R$ 100"))
    monkeypatch.setattr(session_context, "last_reply", lambda _sid: None)
    novo = [{"key": "divida", "title": "Divida", "content": "R$ 250"}]
    campos = routes._input_fields(_request(context=novo))
    assert 'foi atualizado agora: bloco "Divida"' in campos["message"]
    assert "R$ 250" in campos["system_context"] and "R$ 250" not in campos["message"]

    igual = [{"key": "divida", "title": "Divida", "content": "R$ 100"}]
    assert "[Sistema]" not in routes._input_fields(_request(context=igual))["message"]
    # Herdado (campo ausente): nada mudou, sem aviso.
    assert "[Sistema]" not in routes._input_fields(_request(events=[]))["message"]


def test_primeiro_contexto_da_sessao_nao_leva_aviso(monkeypatch):
    monkeypatch.setattr(session_context, "current_context", lambda _sid: None)
    monkeypatch.setattr(session_context, "last_reply", lambda _sid: None)
    campos = routes._input_fields(_request(context=[{"key": "divida", "title": "Divida", "content": "R$ 100"}]))
    assert "[Sistema]" not in campos["message"]


def test_composicao_conta_as_dependencies_do_sistema_como_dependencies():
    contexto = "# Contexto\n\n## Dados\nMaria"
    deps = '<additional context>\n{\n  "plano": "premium"\n}\n</additional context>'
    sistema = SimpleNamespace(role="system", content=f"Você é um assistente.\n{contexto}\n{deps}\n", from_history=False)
    usuario = SimpleNamespace(role="user", content="oi", from_history=False)
    medida = prompt_composition.measure([sistema, usuario], system_context=contexto)
    assert medida is not None
    partes = medida["parts"]
    assert partes["dependencies"] == len(deps)
    assert partes["session_context"] == len(contexto)
    assert partes["instructions"] == len(sistema.content) - len(contexto) - len(deps)
    assert partes["turn"] == 2
