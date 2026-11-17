"""O merge de feedback: o que ele faz com as regras que já existem.

O modelo é substituído por um plano fixo. O que se testa aqui é a parte que
não depende dele — aplicar operações sobre ids e não deixar passar duplicata —
porque é justamente aí que a versão anterior falhava: ela pedia ao modelo para
reescrever a nota inteira e torcer para que ele não duplicasse nem esquecesse.
"""

import pytest
from fastapi import HTTPException

from agent_service.agents.feedback import MergePlan, RuleOp, apply_plan
from agent_service.agents.store import (
    create_definition,
    delete_definition,
    get_feedback_note,
    save_feedback_note,
)
from agent_service.api.agents_routes import (
    FeedbackRule,
    FeedbackRulesIn,
    clear_feedback,
    get_feedback_versions,
    replace_feedback,
    rollback_feedback,
)

CPF = "Sempre confirme o CPF antes de dar detalhes da conta"


@pytest.fixture
def agente():
    create_definition(agent_type="teste-merge", name="Teste merge", instructions=["Atenda bem."])
    yield "teste-merge"
    delete_definition("teste-merge")


def _regras(*textos):
    return [{"id": f"r{i}", "texto": t} for i, t in enumerate(textos, 1)]


def test_regra_repetida_nao_entra_de_novo():
    """O caso que gerava duplicata: o modelo manda `add` de algo que já existe,
    com outras palavras. A fusão é por semelhança de texto, aqui, sem o modelo."""
    atuais = _regras(CPF)
    plano = MergePlan(operacoes=[RuleOp(op="add", texto="Sempre confirme o CPF antes de dar detalhes da conta.")])
    finais, diff = apply_plan(atuais, plano)
    assert len(finais) == 1
    assert diff["fundidas"]


def test_refino_edita_a_regra_no_lugar():
    atuais = _regras("Confirme o CPF")
    plano = MergePlan(operacoes=[RuleOp(op="edit", id="r1", texto=CPF)])
    finais, diff = apply_plan(atuais, plano)
    assert [r["texto"] for r in finais] == [CPF]
    assert finais[0]["id"] == "r1", "editar mantém o id — senão o próximo merge não acha a regra"
    assert diff["editadas"] and not diff["adicionadas"]


def test_contradicao_remove_e_aparece_no_diff():
    """Sumir uma regra é operação explícita e visível: antes, a reescrita
    completa podia derrubar qualquer uma sem deixar rastro."""
    atuais = _regras(CPF, "Responda sempre em inglês")
    plano = MergePlan(operacoes=[RuleOp(op="remove", id="r2"), RuleOp(op="add", texto="Responda em português")])
    finais, diff = apply_plan(atuais, plano)
    assert [r["texto"] for r in finais] == [CPF, "Responda em português"]
    assert diff["removidas"] == ["Responda sempre em inglês"]


def test_regra_nao_citada_no_plano_permanece():
    atuais = _regras(CPF, "Seja breve")
    finais, _ = apply_plan(atuais, MergePlan(operacoes=[RuleOp(op="add", texto="Ofereça o canal de WhatsApp")]))
    assert [r["texto"] for r in finais] == [CPF, "Seja breve", "Ofereça o canal de WhatsApp"]


def test_id_inventado_pelo_modelo_nao_derruba_o_merge():
    """Operação sobre id inexistente vira registro em `ignoradas`, não erro: o
    feedback do operador não deveria se perder porque o modelo errou um id."""
    atuais = _regras(CPF)
    plano = MergePlan(operacoes=[RuleOp(op="remove", id="nao-existe"), RuleOp(op="add", texto="Seja breve")])
    finais, diff = apply_plan(atuais, plano)
    assert [r["texto"] for r in finais] == [CPF, "Seja breve"]
    assert diff["ignoradas"]


def test_edit_com_id_inexistente_vira_regra_nova():
    atuais = _regras(CPF)
    finais, diff = apply_plan(atuais, MergePlan(operacoes=[RuleOp(op="edit", id="xx", texto="Seja breve")]))
    assert [r["texto"] for r in finais] == [CPF, "Seja breve"]
    assert diff["adicionadas"] == ["Seja breve"]


# -- edição manual, histórico e rollback -------------------------------------


def test_put_substitui_as_regras_e_gera_versao(agente):
    save_feedback_note(agente, _regras(CPF))
    nova = replace_feedback(agente, FeedbackRulesIn(rules=[FeedbackRule(texto="Seja breve")]))
    assert [r["texto"] for r in nova["rules"]] == ["Seja breve"]
    assert nova["version"] == 2
    assert [v["origin"] for v in get_feedback_versions(agente)] == ["manual", "merge"]


def test_put_gera_id_para_regra_nova(agente):
    nova = replace_feedback(agente, FeedbackRulesIn(rules=[FeedbackRule(texto="Seja breve")]))
    assert nova["rules"][0]["id"]


def test_put_sem_regras_e_422_com_o_caminho_certo(agente):
    save_feedback_note(agente, _regras(CPF))
    with pytest.raises(HTTPException) as exc:
        replace_feedback(agente, FeedbackRulesIn(rules=[]))
    assert exc.value.status_code == 422
    assert "DELETE" in str(exc.value.detail)


def test_rollback_reaplica_como_versao_nova(agente):
    """Versão nova, não volta no tempo: o caminho de volta do rollback também
    precisa continuar existindo."""
    save_feedback_note(agente, _regras(CPF))
    replace_feedback(agente, FeedbackRulesIn(rules=[FeedbackRule(texto="Seja breve")]))
    voltou = rollback_feedback(agente, 1)
    assert [r["texto"] for r in voltou["rules"]] == [CPF]
    assert voltou["version"] == 3
    assert get_feedback_versions(agente)[0]["origin"] == "rollback"


def test_rollback_de_versao_inexistente_e_404(agente):
    save_feedback_note(agente, _regras(CPF))
    with pytest.raises(HTTPException) as exc:
        rollback_feedback(agente, 99)
    assert exc.value.status_code == 404


def test_delete_zera_nota_e_historico(agente):
    save_feedback_note(agente, _regras(CPF))
    clear_feedback(agente)
    assert get_feedback_note(agente) is None
    assert get_feedback_versions(agente) == []


def test_negacao_nao_e_duplicata():
    """ "Não informe o saldo sem confirmar o CPF" e a versão sem o "não" batem
    0,96 de semelhança e dizem o oposto. Fundi-las engoliria o feedback novo."""
    atuais = _regras("Não informe o saldo sem confirmar o CPF do cliente")
    plano = MergePlan(operacoes=[RuleOp(op="add", texto="Informe o saldo sem confirmar o CPF do cliente")])
    finais, diff = apply_plan(atuais, plano)
    assert len(finais) == 2, "a regra oposta precisa entrar, não ser fundida"
    assert not diff["fundidas"]


def test_regra_gigante_e_ignorada_com_motivo():
    """Acima do teto da API, a regra faria o GET da nota falhar na validação da
    resposta — a aba inteira quebraria por causa de uma regra."""
    atuais = _regras(CPF)
    plano = MergePlan(operacoes=[RuleOp(op="add", texto="x" * 600)])
    finais, diff = apply_plan(atuais, plano)
    assert [r["texto"] for r in finais] == [CPF]
    assert diff["ignoradas"]
