"""Agente de merge de feedback: transforma "não gostei disso, queria que
respondesse assim" em regras de comportamento persistidas por agente —
`registry.py` concatena essas regras nas `instructions` em runtime.

A nota é uma lista de regras com id (`agents/store.py`), não um markdown solto.
O modelo não reescreve o texto inteiro: ele devolve **operações** sobre as
regras que existem (`keep`, `edit`, `remove`, `add`) e o servidor as aplica.
A diferença importa:

- antes, cada merge reescrevia tudo; uma regra podia sumir sem ninguém ver.
  Agora sumir é um `remove` explícito, que aparece no diff da resposta;
- duplicata deixa de depender do modelo obedecer "não duplique": depois de
  aplicar as operações, regras parecidas demais são fundidas por comparação de
  texto, aqui, deterministicamente.

Não é um agente configurável pelo usuário (sem tools, sem sessão, sem
`agent_definitions`): existe só para essa tarefa de merge, com instructions
fixas. O modelo é o do próprio agente que recebeu o feedback — um agente no
OpenAI não deveria precisar de uma chave do Google para aprender.
"""

import difflib
import re
import unicodedata
import uuid
from typing import Any, Literal

from agno.agent import Agent
from pydantic import BaseModel, Field

from agent_service.agents.store import get_feedback_note, save_feedback_note
from agent_service.models.provider import get_model

_INSTRUCTIONS = [
    "Você mantém as diretrizes de comportamento de um agente de IA de atendimento.",
    "Você recebe: (1) as regras atuais, cada uma com um id, (2) um trecho de uma "
    "conversa recente do agente, e (3) um feedback textual de um humano sobre o "
    "que não gostou nessa conversa.",
    "Responda com a lista de operações sobre essas regras — uma por regra que muda, mais as regras novas:",
    "- `edit` (com o `id`) quando o feedback refina uma regra que já existe. "
    "Prefira `edit` a `add`: regra parecida com outra é duplicata.",
    "- `remove` (com o `id`) quando o feedback contradiz uma regra existente.",
    "- `add` (sem `id`) só quando o feedback trata de algo que nenhuma regra cobre.",
    "Regra que o feedback não toca não precisa de operação nenhuma — ela fica como está.",
    "Cada `texto` é uma diretriz específica e acionável, numa frase, no imperativo "
    "(ex.: 'Sempre confirme o CPF antes de dar detalhes da conta' em vez de "
    "'seja mais cuidadoso'). Sem marcador, sem numeração, sem comentários seus.",
]

MAX_TEXTO = 500
"""Mesmo teto de `FeedbackRule.texto` em `api/agents_routes.py`."""

_SIMILARIDADE_DE_DUPLICATA = 0.9
"""Acima disto, duas regras dizem a mesma coisa. Calibrado para pegar reescrita
("confirme o CPF antes" vs "confirme o CPF antes de dar detalhes") sem fundir
regras que só compartilham o assunto."""


class RuleOp(BaseModel):
    op: Literal["add", "edit", "remove", "keep"]
    id: str | None = Field(default=None, description="Id da regra existente — obrigatório em edit/remove/keep.")
    texto: str | None = Field(default=None, description="Texto da regra — obrigatório em add/edit.")


class MergePlan(BaseModel):
    operacoes: list[RuleOp] = Field(default_factory=list)


class FeedbackMergeError(RuntimeError):
    """O modelo não devolveu um plano utilizável — 502, com o motivo."""


def _merge_agent(model_provider: str | None, model_id: str | None, credential_id: str | None) -> Agent:
    """Montado a cada chamada, com o modelo do agente que recebeu o feedback.

    Era um singleton com o provedor padrão: além de prender o merge ao Google,
    ele não enxergava credencial cadastrada depois do primeiro uso."""
    return Agent(
        id="feedback-merge",
        name="Feedback merge",
        model=get_model(provider=model_provider, model_id=model_id, credential_id=credential_id),
        instructions=_INSTRUCTIONS,
        output_schema=MergePlan,
        markdown=False,
    )


def _prompt(rules: list[dict[str, Any]], transcript: str, feedback: str) -> str:
    atuais = "\n".join(f"[{r['id']}] {r['texto']}" for r in rules) or "(nenhuma regra ainda)"
    return (
        f"Regras atuais:\n{atuais}\n\n"
        f"Conversa (a mais recente por último; se uma resposta estiver marcada, o feedback é sobre ela):\n"
        f"{transcript}\n\n"
        f"Feedback do usuário sobre essa conversa:\n{feedback}"
    )


def _normalizar(texto: str) -> str:
    """Só para comparar: sem acento, sem pontuação, sem caixa."""
    sem_acento = "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9 ]+", " ", sem_acento).strip()


_NEGACOES = {"nao", "nunca", "jamais"}
"""`sem` fica de fora de propósito: ele aparece nos dois lados de um par como
"Não informe o saldo sem confirmar o CPF" / "Informe o saldo sem confirmar o
CPF", que é exatamente o par que precisa ser distinguido."""


def _nega(texto: str) -> bool:
    return bool(_NEGACOES & set(_normalizar(texto).split()))


def _duplicada(texto: str, anteriores: list[str]) -> int | None:
    """Índice da regra anterior que diz a mesma coisa, se houver.

    Uma regra e a negação dela são quase idênticas como texto ("Não informe o
    saldo sem confirmar o CPF" vs "Informe o saldo sem confirmar o CPF" batem
    0,96) e opostas como instrução. Fundi-las engoliria o feedback novo em
    silêncio, então diferença de negativa desfaz a semelhança."""
    alvo = _normalizar(texto)
    for i, outro in enumerate(anteriores):
        if difflib.SequenceMatcher(None, alvo, _normalizar(outro)).ratio() < _SIMILARIDADE_DE_DUPLICATA:
            continue
        if _nega(texto) != _nega(outro):
            continue
        return i
    return None


def apply_plan(rules: list[dict[str, Any]], plan: MergePlan) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Aplica as operações e devolve `(regras, diff)`.

    Operação sobre id inexistente é ignorada em vez de virar erro: o modelo
    inventar um id não deveria custar ao operador o feedback inteiro. O que ele
    ignorou aparece no diff como regra intocada."""
    por_id = {r["id"]: dict(r) for r in rules}
    ordem = [r["id"] for r in rules]
    diff: dict[str, list[str]] = {"adicionadas": [], "editadas": [], "removidas": [], "ignoradas": []}

    for op in plan.operacoes:
        if op.op == "keep":
            continue
        if op.op == "remove":
            if op.id in por_id:
                diff["removidas"].append(por_id.pop(op.id)["texto"])
                ordem.remove(op.id)
            else:
                diff["ignoradas"].append(f"remove {op.id!r}: regra inexistente")
            continue
        texto = (op.texto or "").strip()
        if not texto:
            diff["ignoradas"].append(f"{op.op} sem texto")
            continue
        if len(texto) > MAX_TEXTO:
            # O mesmo teto do `FeedbackRule` da API. Deixar entrar uma regra
            # maior faria o GET da nota falhar na validação da resposta — a aba
            # inteira quebraria por causa de uma regra.
            diff["ignoradas"].append(f"{op.op}: regra com mais de {MAX_TEXTO} caracteres")
            continue
        if op.op == "edit" and op.id in por_id:
            anterior = por_id[op.id]["texto"]
            if anterior != texto:
                por_id[op.id]["texto"] = texto
                diff["editadas"].append(f"{anterior} → {texto}")
            continue
        # `add`, ou um `edit` cujo id não existe (o modelo errou o id, mas o
        # texto que ele quis dizer continua válido).
        novo_id = uuid.uuid4().hex[:8]
        por_id[novo_id] = {"id": novo_id, "texto": texto}
        ordem.append(novo_id)
        diff["adicionadas"].append(texto)

    finais, fundidas = _dedup([por_id[i] for i in ordem])
    diff["fundidas"] = fundidas
    return finais, diff


def _dedup(rules: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Funde regras que dizem a mesma coisa, mantendo a mais longa (a mais
    específica). Isto não depende do modelo ter obedecido a instrução."""
    finais: list[dict[str, Any]] = []
    fundidas: list[str] = []
    for regra in rules:
        i = _duplicada(regra["texto"], [r["texto"] for r in finais])
        if i is None:
            finais.append(regra)
            continue
        fundidas.append(f"{regra['texto']} ≡ {finais[i]['texto']}")
        if len(regra["texto"]) > len(finais[i]["texto"]):
            finais[i] = {**finais[i], "texto": regra["texto"]}
    return finais, fundidas


def merge_feedback(
    agent_type: str,
    *,
    feedback: str,
    transcript: str,
    model_provider: str | None = None,
    model_id: str | None = None,
    credential_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Mescla o feedback nas regras do agente. Devolve `(nota, diff)`."""
    atual = get_feedback_note(agent_type)
    rules = list(atual["rules"]) if atual else []

    agent = _merge_agent(model_provider, model_id, credential_id)
    result = agent.run(_prompt(rules, transcript, feedback))
    if not isinstance(result.content, MergePlan):
        raise FeedbackMergeError(
            f"o modelo não devolveu um plano de merge válido (veio {type(result.content).__name__})"
        )

    novas, diff = apply_plan(rules, result.content)
    if not novas:
        raise FeedbackMergeError(
            "o merge deixaria a nota vazia — nenhuma regra sobrou. O feedback foi aplicado? "
            "Reveja o texto ou edite as regras direto (PUT /agents/{tipo}/feedback)."
        )
    return save_feedback_note(agent_type, novas, origin="merge"), diff
