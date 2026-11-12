"""Agente procedural: um fluxo em etapas que o servidor conduz, não o modelo.

Um agente `kind="procedural"` declara `stages` — coletar dados, confirmar,
executar uma ação. O modelo faz só duas coisas: extrair valores da mensagem do
usuário (saída estruturada) e redigir a resposta. **Quem decide avançar, voltar
ou concluir é o código daqui**, e o estado da conversa (etapa e dados
coletados) fica numa tabela própria (`agents/procedure_store.py`), não no
prompt nem na sessão do Agno.

```json
{"kind": "procedural", "stages": [
  {"id": "identificacao", "goal": "Obter o CPF e o nome do cliente",
   "fields": [{"name": "cpf", "type": "string", "required": true, "pattern": "^\\\\d{11}$"},
              {"name": "nome", "type": "string", "required": true}]},
  {"id": "problema", "goal": "Entender o problema",
   "fields": [{"name": "categoria", "type": "string", "required": true, "enum": ["internet", "tv"]}]},
  {"id": "confirmacao", "type": "confirm"},
  {"id": "abrir", "type": "action", "tool": "abrir_chamado"}]}
```

Tipos de etapa:

- `collect` (padrão): campos folha no formato de `dependency_fields`, mais
  `pattern` (regex) em string. Fica pronta quando todos os obrigatórios têm valor.
- `confirm`: o servidor mostra o que foi coletado — texto montado aqui, sem
  LLM, para o modelo não "confirmar" um dado que não coletou — e espera um sim.
- `action`: o servidor chama a tool com os dados coletados. Só depois de uma
  confirmação: uma ação nunca roda sem a pessoa ter visto os dados.

Regras que o código garante (e o modelo não tem como desobedecer):

- a etapa atual é sempre a primeira que ainda não está pronta — corrigir um
  campo de uma etapa anterior volta o fluxo para ela sozinho;
- mudar um dado depois da confirmação desfaz a confirmação;
- um dado que já foi usado por uma ação executada não muda mais;
- todo valor extraído é revalidado aqui (tipo, enum, pattern); o inválido é
  descartado e aparece em `invalid`.

Este módulo não faz I/O: as funções recebem e devolvem o estado. A execução de
um turno (modelo, tool, banco, trace) fica em `agents/procedure_runner.py`.
"""

import copy
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel

from agent_service.agents.dependency_fields import DependencyFieldSpecError, validate_field_specs
from agent_service.agents.response_model import build_response_model

StageType = Literal["collect", "confirm", "action"]
STAGE_TYPES = ("collect", "confirm", "action")
_STAGE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
MAX_STAGES = 30

CONFIRM_FIELD = "confirmacao"
"""Campo extra da extração quando há etapa de confirmação: "sim" ou "nao"."""

RESERVED_NAMES = frozenset({CONFIRM_FIELD, "idempotency_key", "dry_run"})


class StageSpecError(ValueError):
    """`stages` inválido — 422 ao criar/editar o agente."""


# -- validação da definição -----------------------------------------------------------


def validate_stages(raw: Any) -> list[dict[str, Any]]:
    """Valida e normaliza `stages` — a parte que não depende do banco. Que a tool
    de uma etapa `action` existe e recebe os dados certos é conferido na rota
    (`api/agents_routes.py`), que tem acesso às tools."""
    if not isinstance(raw, list) or not raw:
        raise StageSpecError("kind='procedural' precisa de stages (ao menos uma etapa)")
    if len(raw) > MAX_STAGES:
        raise StageSpecError(f"stages aceita no máximo {MAX_STAGES} etapas")

    stages: list[dict[str, Any]] = []
    ids: set[str] = set()
    owners: dict[str, str] = {}
    confirmed_since_action = False
    collected_any = False
    for position, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise StageSpecError(f"stages[{position}] deve ser um objeto")
        stage_id = item.get("id")
        if not isinstance(stage_id, str) or not _STAGE_ID.match(stage_id):
            raise StageSpecError(f"stages[{position}]: id inválido {stage_id!r} (use um slug: a-z, 0-9, '-' ou '_')")
        if stage_id in ids:
            raise StageSpecError(f"stages: id repetido {stage_id!r}")
        ids.add(stage_id)
        stage_type = item.get("type") or "collect"
        if stage_type not in STAGE_TYPES:
            raise StageSpecError(f"etapa {stage_id!r}: tipo inválido {stage_type!r} (use {', '.join(STAGE_TYPES)})")
        goal = item.get("goal") or ""
        if not isinstance(goal, str):
            raise StageSpecError(f"etapa {stage_id!r}: goal deve ser texto")
        stage: dict[str, Any] = {"id": stage_id, "type": stage_type, "goal": goal.strip()}

        if stage_type == "collect":
            stage["fields"] = _validate_collect_fields(stage_id, item.get("fields"), owners)
            collected_any = True
        elif stage_type == "confirm":
            if not collected_any:
                raise StageSpecError(f"etapa {stage_id!r}: confirm precisa vir depois de uma etapa de coleta")
            confirmed_since_action = True
        else:
            tool = item.get("tool")
            if not isinstance(tool, str) or not tool:
                raise StageSpecError(f"etapa {stage_id!r}: action precisa de `tool` (o nome de uma tool cadastrada)")
            if not confirmed_since_action:
                raise StageSpecError(
                    f"etapa {stage_id!r}: uma action precisa de uma etapa confirm antes dela — "
                    "a pessoa vê os dados antes de qualquer efeito"
                )
            stage["tool"] = tool
            if item.get("function"):
                stage["function"] = str(item["function"])
            confirmed_since_action = False
        if stage_type != "collect" and item.get("fields"):
            raise StageSpecError(f"etapa {stage_id!r}: fields só se aplica a etapas collect")
        stages.append(stage)
    return stages


def _validate_collect_fields(stage_id: str, raw: Any, owners: dict[str, str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise StageSpecError(f"etapa {stage_id!r}: collect precisa de fields (ao menos um campo)")
    try:
        fields = validate_field_specs(raw)
    except DependencyFieldSpecError as exc:
        raise StageSpecError(f"etapa {stage_id!r}: {exc}") from exc
    for spec, field in zip(raw, fields):
        name = field["name"]
        if name in RESERVED_NAMES:
            raise StageSpecError(f"etapa {stage_id!r}: {name!r} é um nome reservado")
        if name in owners:
            raise StageSpecError(
                f"campo {name!r} aparece nas etapas {owners[name]!r} e {stage_id!r} — cada campo é de uma etapa só"
            )
        owners[name] = stage_id
        pattern = spec.get("pattern")
        if pattern is not None:
            if field["type"] != "string":
                raise StageSpecError(f"etapa {stage_id!r}: pattern em {name!r} só vale para string")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise StageSpecError(f"etapa {stage_id!r}: pattern inválido em {name!r}: {exc}") from exc
            field["pattern"] = pattern
    if not any(f["required"] for f in fields):
        raise StageSpecError(
            f"etapa {stage_id!r}: marque ao menos um campo como obrigatório — sem isso a etapa "
            "estaria pronta antes de perguntar qualquer coisa"
        )
    return fields


def stages_hash(stages: list[dict[str, Any]]) -> str:
    """Identifica a versão das etapas. Uma confirmação vale para as etapas que a
    pessoa viu: se o agente foi editado no meio da conversa, ela não vale mais."""
    canonical = json.dumps(stages, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def collect_fields(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for s in stages if s["type"] == "collect" for f in s["fields"]]


def fields_before(stages: list[dict[str, Any]], index: int) -> list[str]:
    """Os campos coletados antes da etapa `index` — o que uma action recebe."""
    return [f["name"] for s in stages[:index] if s["type"] == "collect" for f in s["fields"]]


# -- estado -----------------------------------------------------------------------------


def new_state(stages: list[dict[str, Any]], dependencies: dict[str, Any] | None = None) -> dict[str, Any]:
    """Estado inicial. Um campo cujo valor já veio em `dependencies` (quem integra
    mandou o CPF, por exemplo) nasce preenchido — a etapa dele pode já estar pronta."""
    state: dict[str, Any] = {
        "slots": {},
        "confirmed": [],
        "actions": {},
        "invalid": {},
        "declined": False,
        "stages_hash": stages_hash(stages),
    }
    for field in collect_fields(stages):
        value = (dependencies or {}).get(field["name"])
        if value is not None and check_value(field, value) is None:
            state["slots"][field["name"]] = value
    return state


def stage_ready(stage: dict[str, Any], state: dict[str, Any]) -> bool:
    if stage["type"] == "collect":
        return all(state["slots"].get(f["name"]) is not None for f in stage["fields"] if f["required"])
    if stage["type"] == "confirm":
        return stage["id"] in state["confirmed"]
    return (state["actions"].get(stage["id"]) or {}).get("status") == "done"


def current_index(stages: list[dict[str, Any]], state: dict[str, Any]) -> int | None:
    """A primeira etapa que não está pronta; `None` = procedimento concluído."""
    for index, stage in enumerate(stages):
        if not stage_ready(stage, state):
            return index
    return None


def _owner_index(stages: list[dict[str, Any]]) -> dict[str, int]:
    return {f["name"]: i for i, s in enumerate(stages) if s["type"] == "collect" for f in s["fields"]}


def _locked_fields(stages: list[dict[str, Any]], state: dict[str, Any]) -> set[str]:
    """Campos usados por uma action que já executou: mudá-los agora não desfaria o efeito."""
    locked: set[str] = set()
    for index, stage in enumerate(stages):
        if stage["type"] == "action" and (state["actions"].get(stage["id"]) or {}).get("status") in ("done", "running"):
            locked.update(fields_before(stages, index))
    return locked


def check_value(field: dict[str, Any], value: Any) -> str | None:
    """`None` se o valor serve; senão, o motivo — que volta ao usuário pela resposta."""
    field_type = field["type"]
    if field_type == "boolean":
        if not isinstance(value, bool):
            return "esperava sim ou não"
    elif field_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return "esperava um número inteiro"
    elif field_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "esperava um número"
    elif not isinstance(value, str) or not value.strip():
        return "esperava um texto"
    if field.get("enum") and value not in field["enum"]:
        return f"precisa ser um de: {', '.join(map(str, field['enum']))}"
    if field.get("pattern") and not re.fullmatch(field["pattern"], value):
        return "formato inválido"
    return None


def apply_extraction(
    stages: list[dict[str, Any]], state: dict[str, Any], extracted: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Aplica o que o modelo extraiu de uma mensagem. Devolve o estado novo e os
    campos que mudaram. Puro: não chama modelo nem banco."""
    state = copy.deepcopy(state)
    # `invalid` é desta mensagem: o que veio errado antes e não foi reenviado não
    # é mais assunto (o campo continua em `faltando`, se for obrigatório).
    state["invalid"] = {}
    before = current_index(stages, state)
    owners = _owner_index(stages)
    locked = _locked_fields(stages, state)
    by_name = {f["name"]: f for f in collect_fields(stages)}

    changed: list[str] = []
    for name, value in extracted.items():
        if name == CONFIRM_FIELD or value is None or name not in by_name:
            continue
        if isinstance(value, str):
            value = value.strip()
        problem = check_value(by_name[name], value)
        if problem is not None:
            state["invalid"][name] = problem
            continue
        if state["slots"].get(name) == value:
            state["invalid"].pop(name, None)
            continue
        if name in locked:
            state["invalid"][name] = "não dá mais para alterar: a ação que usou este dado já foi executada"
            continue
        state["slots"][name] = value
        state["invalid"].pop(name, None)
        changed.append(name)

    if changed:
        # Uma confirmação cobre os campos das etapas antes dela: se um deles mudou,
        # a pessoa precisa ver os dados de novo.
        earliest = min(owners[name] for name in changed)
        confirm_ids = {s["id"] for i, s in enumerate(stages) if s["type"] == "confirm" and i > earliest}
        state["confirmed"] = [c for c in state["confirmed"] if c not in confirm_ids]

    answer = extracted.get(CONFIRM_FIELD)
    state["declined"] = False
    if before is not None and stages[before]["type"] == "confirm":
        stage_id = stages[before]["id"]
        if answer == "sim" and not changed:
            # Confirma só o que foi mostrado: com um dado novo na mesma mensagem,
            # a confirmação seguinte mostra os dados atualizados.
            if stage_id not in state["confirmed"]:
                state["confirmed"].append(stage_id)
        elif answer == "nao" and not changed:
            state["declined"] = True
    return state, changed


def start_action(state: dict[str, Any], stage_id: str) -> dict[str, Any]:
    state = copy.deepcopy(state)
    previous = state["actions"].get(stage_id) or {}
    state["actions"][stage_id] = {"status": "running", "attempts": int(previous.get("attempts") or 0) + 1}
    return state


def finish_action(
    stages: list[dict[str, Any]], state: dict[str, Any], stage_id: str, outcome: dict[str, Any]
) -> dict[str, Any]:
    """Grava o resultado da action. Se ela falhou, a confirmação anterior é desfeita:
    tentar de novo exige a pessoa confirmar de novo — um efeito colateral nunca é
    repetido sem ninguém ver."""
    state = copy.deepcopy(state)
    action = dict(state["actions"].get(stage_id) or {})
    if outcome.get("ok"):
        action.update(status="done", result=outcome.get("result"), error=None)
    else:
        action.update(status="failed", error=outcome.get("error") or "a ação falhou")
        index = next(i for i, s in enumerate(stages) if s["id"] == stage_id)
        previous_confirm = max((i for i, s in enumerate(stages[:index]) if s["type"] == "confirm"), default=None)
        if previous_confirm is not None:
            confirm_id = stages[previous_confirm]["id"]
            state["confirmed"] = [c for c in state["confirmed"] if c != confirm_id]
    state["actions"][stage_id] = action
    return state


# -- o que sai para fora ---------------------------------------------------------------------


def _field_view(field: dict[str, Any]) -> dict[str, Any]:
    view = {"name": field["name"], "label": field["label"], "type": field["type"], "description": field["description"]}
    if field.get("enum"):
        view["enum"] = field["enum"]
    return view


def sync_with_definition(stages: list[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any]:
    """O agente foi editado desde a última mensagem? Então as confirmações dadas
    valiam para outras etapas e caem: uma ação nova (ou renomeada) não roda em
    cima de um "sim" dado para outra coisa."""
    current = stages_hash(stages)
    if state.get("stages_hash") == current:
        return state
    state = copy.deepcopy(state)
    state["stages_hash"] = current
    state["confirmed"] = []
    state["declined"] = False
    return state


def state_view(stages: list[dict[str, Any]], state: dict[str, Any], *, finished: bool = False) -> dict[str, Any]:
    """O `state` do `/chat`: quem integra sabe a etapa e quando acabou sem
    interpretar o texto da resposta. `finished` vem da linha gravada: uma conversa
    concluída continua concluída mesmo que o agente ganhe etapas depois."""
    index = None if finished else current_index(stages, state)
    stage = stages[index] if index is not None else None
    missing = (
        [_field_view(f) for f in stage["fields"] if state["slots"].get(f["name"]) is None and f["required"]]
        if stage and stage["type"] == "collect"
        else []
    )
    actions = {
        stage_id: {k: v for k, v in action.items() if k in ("status", "attempts", "result", "error")}
        for stage_id, action in state["actions"].items()
    }
    return {
        "stage": stage["id"] if stage else None,
        "stage_type": stage["type"] if stage else None,
        "stage_index": index if index is not None else len(stages),
        "stages_total": len(stages),
        "stages": [
            {
                "id": s["id"],
                "type": s["type"],
                "goal": s["goal"],
                "status": "done" if finished or stage_ready(s, state) else ("current" if i == index else "pending"),
            }
            for i, s in enumerate(stages)
        ],
        "collected": dict(state["slots"]),
        "missing": missing,
        "invalid": dict(state["invalid"]),
        "actions": actions,
        "done": index is None,
        "result": {"collected": dict(state["slots"]), "actions": {k: v.get("result") for k, v in actions.items()}}
        if index is None
        else None,
    }


def _display(value: Any) -> str:
    if isinstance(value, bool):
        return "sim" if value else "não"
    return str(value)


def confirmation_text(stages: list[dict[str, Any]], state: dict[str, Any], index: int) -> str:
    """O texto da etapa `confirm`, montado dos dados coletados — sem modelo, para
    a pessoa confirmar exatamente o que o sistema vai usar."""
    names = set(fields_before(stages, index))
    # Lista em Markdown: o console renderiza como lista, e em texto puro (WhatsApp,
    # e-mail) "- campo: valor" continua legível — "•" numa linha só não.
    lines = [
        f"- {f['label']}: {_display(state['slots'][f['name']])}"
        for f in collect_fields(stages)
        if f["name"] in names and state["slots"].get(f["name"]) is not None
    ]
    goal = stages[index]["goal"]
    head = goal if goal else "Confira os dados antes de continuar:"
    return f"{head}\n\n" + "\n".join(lines) + "\n\nEstá tudo certo? Responda sim para confirmar ou diga o que corrigir."


def reply_context(
    stages: list[dict[str, Any]],
    state: dict[str, Any],
    *,
    last_reply: str | None,
    action_error: str | None = None,
    finished: bool = False,
) -> dict[str, Any]:
    """O que o modelo recebe em `dependencies.procedimento` para redigir a resposta."""
    index = None if finished else current_index(stages, state)
    stage = stages[index] if index is not None else None
    context: dict[str, Any] = {
        "etapa": stage["id"] if stage else None,
        "tipo_da_etapa": stage["type"] if stage else None,
        "objetivo": stage["goal"] if stage else None,
        "coletado": dict(state["slots"]),
        "faltando": state_view(stages, state, finished=finished)["missing"],
        "invalidos": dict(state["invalid"]),
        "concluido": stage is None,
        "ultima_mensagem_do_agente": last_reply,
    }
    if state.get("declined"):
        context["usuario_recusou_a_confirmacao"] = True
    if action_error:
        context["acao_falhou"] = action_error
    if stage is None:
        context["resultado"] = {k: v.get("result") for k, v in state["actions"].items()}
    return context


PROCEDURAL_INSTRUCTIONS = """\
Você conduz um atendimento em etapas, e quem controla as etapas é o sistema, não você.
A cada mensagem, `procedimento` (nas dependencies) diz a etapa atual (`etapa`, `objetivo`),
o que já foi coletado (`coletado`), o que falta (`faltando`) e o que veio inválido (`invalidos`).
- Peça só o que está em `faltando`, no máximo dois campos por vez, de forma natural.
- Se houver `invalidos`, explique o problema e peça o valor de novo.
- Se `usuario_recusou_a_confirmacao`, pergunte o que precisa ser corrigido.
- Nunca diga que uma ação foi feita se `concluido` não for verdadeiro; se houver `acao_falhou`,
  explique que não foi possível concluir e que a pessoa pode confirmar de novo para tentar outra vez.
- Quando `concluido` for verdadeiro, informe o resultado (`resultado`) de forma clara e encerre.
- Não peça confirmação por conta própria: o sistema monta e envia a confirmação."""


# -- extração ----------------------------------------------------------------------------------


def extraction_model(agent_type: str, stages: list[dict[str, Any]]) -> type[BaseModel]:
    """O schema da extração: todo campo de coleta, opcional (a mensagem pode trazer
    qualquer um, inclusive a correção de um campo antigo), e o sim/não da confirmação."""
    fields = [
        {k: v for k, v in f.items() if k != "pattern"} | {"required": False, "default": None}
        for f in collect_fields(stages)
    ]
    if any(s["type"] == "confirm" for s in stages):
        fields.append(
            {
                "name": CONFIRM_FIELD,
                "type": "string",
                "label": CONFIRM_FIELD,
                "description": "sim se o usuário confirmou os dados apresentados; nao se recusou ou quer corrigir; vazio se a mensagem não responde a uma confirmação",
                "required": False,
                "default": None,
                "enum": ["sim", "nao"],
            }
        )
    return build_response_model(f"{agent_type}-extracao", fields)


EXTRACTION_INSTRUCTIONS = [
    "Você extrai dados de uma mensagem de usuário para um atendimento em etapas.",
    "Preencha só os campos que a mensagem do usuário informa explicitamente; deixe os demais vazios.",
    "Nunca invente nem complete um valor. Se o usuário corrigir um valor já coletado, devolva o novo valor.",
    "Use a última mensagem do agente para entender respostas curtas (ex.: 'sim', 'é 123').",
    "Numa confirmação, a mesma mensagem pode recusar e corrigir ('não, o problema é na TV'): devolva "
    "confirmacao='nao' E o campo corrigido com o valor novo. Só devolva confirmacao='sim' se a pessoa "
    "concordou com os dados sem pedir mudança nenhuma.",
    "Em campos com valores permitidos, devolva exatamente um deles, ajustando a grafia (ex.: 'TV' -> 'tv').",
]


def extraction_prompt(stages: list[dict[str, Any]], state: dict[str, Any], message: str, last_reply: str | None) -> str:
    index = current_index(stages, state)
    stage = stages[index] if index is not None else None
    lines = []
    if stage is not None:
        lines.append(f"Etapa atual: {stage['id']} ({stage['type']}) — {stage['goal'] or 'sem objetivo descrito'}")
    lines.append(f"Já coletado: {state['slots'] or 'nada'}")
    missing = state_view(stages, state)["missing"]
    if missing:
        lines.append("Faltando nesta etapa: " + ", ".join(f"{f['name']} ({f['label']})" for f in missing))
    if stage is not None and stage["type"] == "confirm":
        # Na confirmação nada "falta": sem isto o modelo tende a olhar só para o
        # sim/não e deixa passar a correção que vem na mesma frase.
        names = set(fields_before(stages, index))
        corrigiveis = [
            f"{f['name']} ({f['label']}{': ' + ' | '.join(map(str, f['enum'])) if f.get('enum') else ''})"
            for f in collect_fields(stages)
            if f["name"] in names
        ]
        lines.append("Campos que a pessoa pode corrigir agora: " + ", ".join(corrigiveis))
    lines.append(f"Última mensagem do agente: {last_reply or '(nenhuma)'}")
    lines.append(f"Mensagem do usuário: {message}")
    return "\n".join(lines)
