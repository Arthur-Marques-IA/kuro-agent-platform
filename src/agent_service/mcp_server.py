"""Servidor MCP do Kuro (`kuro-mcp`): as operações da CLI como tools MCP.

Roda na máquina de quem opera, por stdio, e fala com o serviço pela API HTTP —
local ou numa VPS, com as mesmas variáveis da CLI (`KURO_API_URL`, `KURO_API_KEY`,
`KURO_CA_BUNDLE`). Não toca no banco: é mais um cliente da API, como a CLI e o console.

Três regras de projeto:

- **Toda operação que na CLI pede `--yes` pede confirmação ao usuário** por
  elicitation do MCP (remover, restaurar, promover). A confirmação vem do cliente
  MCP, mostrada à pessoa — não existe parâmetro que o modelo possa preencher para
  se autoconfirmar. Cliente sem suporte a elicitation recebe erro, nunca a ação.
  O resolver que monta a pergunta só lê; o efeito acontece só no corpo da tool.
- **Chave de modelo não passa por aqui.** Cadastrar ou trocar uma credencial
  levaria a chave pelo contexto do modelo; isso fica na CLI e no console.
- **Testes são `dry_run` por padrão** (`chat`, `analyze`, `tool_invoke`, `eval`):
  um agente testando outro não deveria fechar um acordo de verdade por engano.
  As tools recebem `X-Kuro-Dry-Run: true` e decidem o que simular.

As sessões de chat abertas por aqui não usam `~/.kuro/sessions.json` (o arquivo
da CLI): `chat` devolve o `session_id`, e quem chama o repassa para continuar.
"""

import json
import logging
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, Elicit, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from agent_service.cli.agents import EDITABLE_FIELDS as AGENT_FIELDS
from agent_service.cli.agents import editable as agent_editable
from agent_service.cli.chat import _coerce, collect_stream
from agent_service.cli.client import ApiError, Client, PortBlocked, ServiceUnavailable, TlsError
from agent_service.cli.connection import client_from_env
from agent_service.cli.eval import EvalInputError, check_rules_spec, parse_cases, run_evaluation
from agent_service.cli.tools import EDITABLE_FIELDS as TOOL_FIELDS

# Texto que volta ao modelo: um trace inteiro ou uma transcrição longa comeria o
# contexto dele. Corta cada string e diz que cortou.
MAX_TEXT = 4000

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# Só acrescenta (uma nota, um documento novo).
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
# Sobrescreve o que existia (definição, regras), mas tem histórico/versão para voltar
# — como na CLI, não pede confirmação. A dica deixa o cliente decidir se pergunta.
OVERWRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
# `chat`/`analyze`/`tool_invoke` chamam o modelo e as tools do agente, que alcançam
# sistemas de fora — por isso open_world.
RUN = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
# Coletar um site sai para a web aberta; ressincronizar também troca e remove páginas da base.
CRAWL = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
RECRAWL = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

INSTRUCTIONS = """\
Opera o Kuro (agent-service): agentes de IA, tools, execuções, bases de conhecimento.

Fluxo seguro para mudar um agente em produção: agent_promote(origem=prod, destino=<prod>-draft)
→ agent_set/agent_apply no draft → eval(draft, compare_with=prod) → agent_promote(draft → prod).
Antes de editar, agent_get(editable=true) devolve exatamente os campos aceitos.
chat/analyze/tool_invoke/eval rodam em dry_run por padrão (as tools sabem que é teste).
Para continuar uma conversa, repasse o session_id que o chat devolveu.
Remover, restaurar e promover pedem confirmação ao usuário — não tente contornar.
Para investigar uma resposta ruim: runs_list(agent_type=...) → run_show(run_id).
"""


class Confirmacao(BaseModel):
    confirmar: bool = Field(default=False, title="Confirmar", description="Marque para executar a ação.")


class NadaAFazer(Confirmacao):
    """O resolver viu que a ação não mudaria nada e não perguntou. Não autoriza
    escrita: se o estado mudou até o corpo da tool rodar, ela recusa em vez de
    gravar sem ninguém ter confirmado."""


def _ask(ctx: Context, cli: str, message: str) -> Elicit[Confirmacao]:
    """A pergunta ao usuário — ou a recusa, se o cliente MCP não sabe perguntar.

    Sem elicitation o SDK já recusaria, mas com uma mensagem genérica; esta diz à
    pessoa como fazer a mesma coisa pela CLI, onde ela mesma confirma."""
    elicitation = ctx.client_capabilities.elicitation if ctx.client_capabilities else None
    if elicitation is None or (elicitation.form is None and elicitation.url is not None):
        raise ToolError(
            "esta ação pede confirmação do usuário, e este cliente MCP não suporta elicitation. "
            f"Peça à pessoa para rodar no terminal: `{cli}`"
        )
    return Elicit(message + "\n\nMarque “Confirmar” para executar.", Confirmacao)


# -- utilitários -----------------------------------------------------------------


def _clip(value: Any, limit: int = MAX_TEXT) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"… [cortado: {len(value) - limit} caracteres a mais]"
    if isinstance(value, dict):
        return {k: _clip(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v, limit) for v in value]
    return value


def _api_message(exc: ApiError) -> str:
    detail = exc.detail
    if isinstance(detail, list):  # erros de validação do FastAPI
        detail = "; ".join(
            f"{'.'.join(str(p) for p in d.get('loc', [])[1:]) or 'body'}: {d.get('msg')}" if isinstance(d, dict) else str(d)
            for d in detail
        )
    elif isinstance(detail, dict):
        detail = json.dumps(detail, ensure_ascii=False, default=str)
    return f"{detail} (HTTP {exc.status})"


def _tool_error(exc: ApiError | ServiceUnavailable) -> ToolError:
    """A falha do client como `ToolError`, cuja mensagem chega ao modelo — qualquer
    outra exceção o SDK esconde atrás de uma mensagem genérica."""
    if isinstance(exc, (TlsError, PortBlocked)):
        return ToolError(str(exc))
    if isinstance(exc, ServiceUnavailable):
        return ToolError(f"{exc}. Confira KURO_API_URL e se o serviço está no ar.")
    if exc.status == 401:
        return ToolError(f"{_api_message(exc)} — defina KURO_API_KEY (escopo admin) na configuração do servidor MCP")
    return ToolError(_api_message(exc))


def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except (ApiError, ServiceUnavailable) as exc:
        raise _tool_error(exc) from exc


def _fields_only(changes: dict[str, Any], allowed: tuple[str, ...]) -> None:
    unknown = sorted(set(changes) - set(allowed))
    if unknown:
        raise ToolError(f"campos não editáveis: {', '.join(unknown)} (use: {', '.join(allowed)})")


def _dependencies(definition: dict[str, Any], given: dict[str, Any] | None) -> dict[str, Any]:
    """Converte para o tipo declarado e recusa se faltar uma obrigatória — o
    mesmo que a CLI faz sem TTY."""
    deps = dict(given or {})
    fields = definition.get("dependency_fields") or []
    for field in fields:
        if field["name"] in deps:
            deps[field["name"]] = _coerce(deps[field["name"]], field["type"])
    missing = [f for f in fields if f["required"] and deps.get(f["name"]) is None]
    if missing:
        names = ", ".join(f"{f['name']} ({f['type']})" for f in missing)
        raise ToolError(f"dependencies obrigatórias ausentes: {names}")
    return deps


NO_LOCAL_FILES = (
    "este servidor MCP roda no servidor do Kuro, não na sua máquina: não lê arquivos por caminho. "
    "Mande o conteúdo inline ({inline})."
)


def _files(local_files: bool) -> tuple[Callable[[str, str], str], Callable[[str, str], bytes]]:
    """Leitores de arquivo por caminho — ou a recusa, quando o servidor MCP não roda na
    máquina de quem usa (`/mcp` e `docker exec`): ali o caminho seria do container, e ler
    o `.env` ou o `/proc/self/environ` dele entregaria os segredos do serviço."""

    def read_bytes(path: str, inline: str) -> bytes:
        if not local_files:
            raise ToolError(NO_LOCAL_FILES.format(inline=inline))
        try:
            return Path(path).read_bytes()
        except OSError as exc:
            raise ToolError(f"não consegui ler {path}: {exc}") from exc

    def read_text(path: str, inline: str) -> str:
        try:
            return read_bytes(path, inline).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError(f"{path} não é texto UTF-8: {exc}") from exc

    return read_text, read_bytes


# -- servidor --------------------------------------------------------------------


def build_server(client: Client, local_files: bool = True) -> MCPServer:
    """`local_files=False` quando o servidor MCP roda junto do serviço, e não na máquina
    de quem usa: os parâmetros que recebem um caminho de arquivo passam a ser recusados."""
    instructions = INSTRUCTIONS
    if not local_files:
        instructions += NO_LOCAL_FILES.format(inline="document, cases, rules, text") + "\n"
    server = MCPServer(name="kuro", title="Kuro", instructions=instructions, version=_version())
    _read_text, _read_bytes = _files(local_files)

    # -- saúde e modelos ------------------------------------------------------

    @server.tool(annotations=READ)
    def health() -> dict[str, Any]:
        """Diagnóstico do serviço: versão, autenticação, gravação de execuções e provedores com chave."""
        report: dict[str, Any] = {"url": client.base_url, "service": _call(client.health)}
        for key, fn in (("observability", client.observability_config), ("credentials", client.list_credentials)):
            try:
                report[key] = fn()
            except ApiError as exc:
                report[key] = {"error": exc.detail, "status": exc.status}
            except ServiceUnavailable as exc:
                raise ToolError(str(exc)) from exc
        if isinstance(report["credentials"], list):
            report["credentials"] = [
                {k: c.get(k) for k in ("id", "provider", "label", "enabled", "configured", "last_test_ok")}
                for c in report["credentials"]
            ]
        return report

    @server.tool(annotations=READ)
    def providers_list() -> dict[str, Any]:
        """Provedores de modelo suportados, quantas credenciais cada um tem e o modelo padrão."""
        return {"items": _call(client.list_providers)}

    @server.tool(annotations=READ)
    def provider_models(provider: str, refresh: bool = False) -> dict[str, Any]:
        """Modelos que o provedor oferece agora (o que vale em `model_id`). Cada um traz
        `channel`: stable | preview | alias — não use preview/alias em produção."""
        return _call(client.provider_models, provider, refresh="true" if refresh else None)

    @server.tool(annotations=READ)
    def credentials_list(provider: str | None = None) -> dict[str, Any]:
        """Credenciais de modelo cadastradas (a chave nunca aparece). Cadastrar ou trocar
        uma chave não é feito por aqui: use `kuro credentials add` ou o console."""
        rows = _call(client.list_credentials)
        return {"items": [c for c in rows if provider is None or c["provider"] == provider]}

    # -- agentes --------------------------------------------------------------

    @server.tool(annotations=READ)
    def agents_list() -> dict[str, Any]:
        """Agentes cadastrados, resumidos. Use agent_get para a definição completa."""
        keys = ("agent_type", "name", "kind", "model_provider", "model_id", "prompt_version", "is_seed")
        return {"items": [{k: a.get(k) for k in keys} for a in _call(client.list_agents)]}

    @server.tool(annotations=READ)
    def agent_get(agent_type: str, editable: bool = False) -> dict[str, Any]:
        """Definição de um agente. Com `editable=true`, só os campos aceitos por
        agent_apply/agent_set — o ponto de partida para editar."""
        definition = _call(client.get_agent, agent_type)
        return agent_editable(definition) if editable else definition

    @server.tool(annotations=OVERWRITE)
    def agent_apply(definition: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        """Cria o agente se não existir; senão envia só os campos que mudaram. Mesmo formato
        de `kuro agents apply`: agent_type, name, instructions, tools, model_provider,
        model_id, kind, response_schema, dependency_fields... `dry_run=true` valida no
        servidor sem gravar. Mudar `instructions` gera uma versão nova do prompt."""
        agent_type = definition.get("agent_type")
        if not isinstance(agent_type, str) or not agent_type:
            raise ToolError("a definição precisa do campo agent_type")
        _fields_only({k: v for k, v in definition.items() if k != "agent_type"}, AGENT_FIELDS)
        current = next((a for a in _call(client.list_agents) if a["agent_type"] == agent_type), None)
        if current is None:
            agent = _call(client.create_agent, definition, dry_run=dry_run)
            return {"action": "create", "dry_run": dry_run, "changed": sorted(k for k in definition if k != "agent_type"), "agent": agent}
        changes = {k: v for k, v in definition.items() if k != "agent_type" and current.get(k) != v}
        if not changes:
            return {"action": "unchanged", "dry_run": dry_run, "changed": [], "agent": current}
        agent = _call(client.update_agent, agent_type, changes, dry_run=dry_run)
        return {"action": "update", "dry_run": dry_run, "changed": sorted(changes), "agent": agent}

    @server.tool(annotations=OVERWRITE)
    def agent_set(agent_type: str, changes: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        """Altera só os campos informados, ex.: {"num_history_runs": 5, "tools": ["calculator"]}.
        `instructions` aceita lista de strings (ou uma string só)."""
        _fields_only(changes, AGENT_FIELDS)
        if isinstance(changes.get("instructions"), str):
            changes = {**changes, "instructions": [changes["instructions"]]}
        return _call(client.update_agent, agent_type, changes, dry_run=dry_run)

    @server.tool(annotations=READ)
    def agent_versions(agent_type: str) -> dict[str, Any]:
        """Histórico do prompt (instructions), versão a versão."""
        return {"items": _call(client.agent_versions, agent_type)}

    @server.tool(annotations=READ)
    def agent_revisions(agent_type: str, version: int | None = None) -> dict[str, Any]:
        """Versões da configuração inteira (modelo, tools, schema, regras) — é o
        `agent_version` que cada run grava. Com `version`, a configuração completa dela."""
        if version is not None:
            return _call(client.agent_revision, agent_type, version)
        return {"items": _call(client.agent_revisions, agent_type)}

    @server.tool(annotations=READ)
    def agent_integration(agent_type: str) -> dict[str, Any]:
        """Como outro sistema chama este agente: endpoint, cURL e dependências obrigatórias."""
        return _call(client.integration, agent_type)

    @server.tool(annotations=READ)
    def procedure_state(agent_type: str, session_id: str) -> dict[str, Any]:
        """Agente procedural: em que etapa a conversa está, o que foi coletado, o que
        falta e, se concluída, o resultado. O `chat` já devolve isso em `state`."""
        return _call(client.procedure_state, agent_type, session_id)

    @server.tool(annotations=READ)
    def procedure_funnel(agent_type: str, include_tests: bool = False) -> dict[str, Any]:
        """Agente procedural: quantas conversas estão paradas em cada etapa e quantas
        concluíram — onde as pessoas desistem."""
        return _call(client.procedure_funnel, agent_type, include_dry_run=include_tests)

    def _confirm_delete_agent(agent_type: str, ctx: Context) -> Elicit[Confirmacao]:
        return _ask(ctx, f"kuro agents delete {agent_type} --yes", f"Remover o agente {agent_type!r}? Não tem volta.")

    @server.tool(annotations=DESTRUCTIVE)
    def agent_delete(
        agent_type: str, ok: Annotated[Confirmacao, Resolve(_confirm_delete_agent)]
    ) -> dict[str, Any]:
        """Remove um agente (pede confirmação ao usuário). Agentes seed não saem."""
        if not ok.confirmar:
            return {"cancelled": True}
        _call(client.delete_agent, agent_type)
        return {"deleted": agent_type}

    def _rollback_target(agent_type: str, version: int) -> dict[str, Any]:
        versions = _call(client.agent_versions, agent_type)
        target = next((v for v in versions if v["version"] == version), None)
        if target is None:
            available = ", ".join(f"v{v['version']}" for v in versions) or "nenhuma"
            raise ToolError(f"{agent_type} não tem a versão v{version} (disponíveis: {available})")
        return target

    def _confirm_rollback(agent_type: str, version: int, ctx: Context) -> Elicit[Confirmacao] | Confirmacao:
        target = _rollback_target(agent_type, version)
        if _call(client.get_agent, agent_type)["instructions"] == target["instructions"]:
            return NadaAFazer()  # nada vai mudar: não incomoda a pessoa
        preview = "\n".join(target["instructions"])
        return _ask(ctx, f"kuro agents rollback {agent_type} {version} --yes", f"Restaurar as instructions da v{version} de {agent_type!r}? Vira uma versão nova.\n\n{_clip(preview, 1500)}")

    @server.tool(annotations=DESTRUCTIVE)
    def agent_rollback(
        agent_type: str, version: int, ok: Annotated[Confirmacao, Resolve(_confirm_rollback)]
    ) -> dict[str, Any]:
        """Reaplica as instructions de uma versão anterior do prompt (pede confirmação).
        Não apaga histórico: grava uma versão nova com o texto antigo."""
        asked = not isinstance(ok, NadaAFazer)
        if asked and not ok.confirmar:
            return {"cancelled": True}
        target = _rollback_target(agent_type, version)
        current = _call(client.get_agent, agent_type)
        if current["instructions"] == target["instructions"]:
            return {"agent_type": agent_type, "unchanged": True, "prompt_version": current["prompt_version"]}
        if not asked:
            raise ToolError(f"o prompt de {agent_type} mudou durante a chamada — rode agent_rollback de novo")
        return _call(client.update_agent, agent_type, {"instructions": target["instructions"]})

    def _confirm_promote(source: str, to: str, ctx: Context) -> Elicit[Confirmacao]:
        return _ask(
            ctx,
            f"kuro agents promote {source} --to {to} --yes",
            f"Copiar a configuração de {source!r} para {to!r}? Instructions, modelo, tools, schema e regras "
            f"de {to!r} serão substituídos (o destino é criado se não existir). Rodou o eval antes?"
        )

    @server.tool(annotations=DESTRUCTIVE)
    def agent_promote(
        source: str, to: str, ok: Annotated[Confirmacao, Resolve(_confirm_promote)]
    ) -> dict[str, Any]:
        """Copia a configuração de um agente para outro — o fluxo draft → prod (pede
        confirmação). Para criar o draft, promova ao contrário: source=prod, to=prod-draft."""
        if not ok.confirmar:
            return {"cancelled": True}
        return _call(client.promote_agent, source, to)

    # -- feedback (nota de regras do agente) ----------------------------------

    @server.tool(annotations=READ)
    def feedback_show(agent_type: str) -> dict[str, Any]:
        """Regras de comportamento aprendidas do feedback, com os ids."""
        return _call(client.get_feedback, agent_type)

    @server.tool(annotations=READ)
    def feedback_versions(agent_type: str) -> dict[str, Any]:
        """Histórico de versões da nota de feedback."""
        return {"items": _call(client.feedback_versions, agent_type)}

    @server.tool(annotations=OVERWRITE)
    def feedback_send(agent_type: str, session_id: str, message: str) -> dict[str, Any]:
        """Ensina o agente a partir de uma conversa (a `session_id` que o chat devolveu):
        o feedback é mesclado nas regras e a resposta traz o `diff`. Só para agentes
        conversacionais. Vale na próxima mensagem — prefira fazer isso num draft."""
        return _call(client.send_feedback, agent_type, session_id, message)

    @server.tool(annotations=OVERWRITE)
    def feedback_remove(agent_type: str, rule_ids: list[str]) -> dict[str, Any]:
        """Apaga regras da nota pelo id (veja feedback_show). Fica no histórico."""
        current = _call(client.get_feedback, agent_type)
        keep = [r for r in current["rules"] if r["id"] not in set(rule_ids)]
        if len(keep) == len(current["rules"]):
            raise ToolError(f"nenhuma regra com id em {', '.join(rule_ids)}")
        if not keep:
            raise ToolError("isso apagaria todas as regras — use feedback_clear")
        return _call(client.replace_feedback, agent_type, keep)

    @server.tool(annotations=OVERWRITE)
    def feedback_rollback(agent_type: str, version: int) -> dict[str, Any]:
        """Reaplica as regras de uma versão anterior da nota (vira uma versão nova)."""
        return _call(client.rollback_feedback, agent_type, version)

    def _confirm_clear(agent_type: str, ctx: Context) -> Elicit[Confirmacao]:
        return _ask(ctx, f"kuro agents feedback {agent_type} --clear --yes", f"Zerar a nota de feedback de {agent_type!r}, com o histórico? Não tem volta.")

    @server.tool(annotations=DESTRUCTIVE)
    def feedback_clear(agent_type: str, ok: Annotated[Confirmacao, Resolve(_confirm_clear)]) -> dict[str, Any]:
        """Zera a nota de feedback e o histórico dela (pede confirmação)."""
        if not ok.confirmar:
            return {"cancelled": True}
        _call(client.clear_feedback, agent_type)
        return {"cleared": agent_type}

    # -- executar -------------------------------------------------------------

    @server.tool(annotations=RUN)
    def chat(
        agent_type: str,
        message: str,
        session_id: str | None = None,
        dependencies: dict[str, Any] | None = None,
        user_id: str = "mcp",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Manda uma mensagem a um agente conversacional. Sem `session_id`, começa uma
        conversa nova; para continuá-la, repasse o `session_id` devolvido.
        `dependencies` são os dados que quem integra mandaria (ex.: {"cpf": "..."}).
        `dry_run` (padrão true) avisa as tools de que é teste."""
        definition = _call(client.get_agent, agent_type)
        body: dict[str, Any] = {
            "agent_type": agent_type,
            "user_id": user_id,
            "session_id": session_id or f"mcp-{uuid.uuid4().hex[:12]}",
            "message": message,
            "dependencies": _dependencies(definition, dependencies) or None,
        }
        if dry_run:
            body["dry_run"] = True
        result = _call(collect_stream, client, body)
        if result["error"]:
            raise ToolError(f"{result['error']} (run {result['run_id']}; veja run_show)")
        return result

    @server.tool(annotations=RUN)
    def analyze(
        agent_type: str,
        document: str | None = None,
        document_file: str | None = None,
        dependencies: dict[str, Any] | None = None,
        session_id: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Roda um agente analista (kind=analysis): devolve `result` no formato do
        response_schema dele. Passe o texto em `document` ou o caminho de um arquivo
        de texto local em `document_file`."""
        if (document is None) == (document_file is None):
            raise ToolError("informe document ou document_file (um dos dois)")
        definition = _call(client.get_agent, agent_type)
        if definition.get("kind") != "analysis":
            raise ToolError(f"{agent_type!r} não é kind=analysis — use chat")
        body: dict[str, Any] = {
            "agent_type": agent_type,
            "document": document if document is not None else _read_text(document_file or "", "document"),
            "dependencies": _dependencies(definition, dependencies) or None,
        }
        if session_id:
            body["session_id"] = session_id
        if dry_run:
            body["dry_run"] = True
        return _call(client.analyze, body)

    @server.tool(annotations=RUN)
    def eval(
        agent_type: str,
        cases_file: str | None = None,
        cases: list[dict[str, Any]] | None = None,
        rules_file: str | None = None,
        rules: list[dict[str, Any]] | None = None,
        compare_with: str | None = None,
        fields: list[str] | None = None,
        min_pass: float = 0.9,
        tolerance: float = 0.01,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Avalia um agente analista contra um dataset, campo a campo (o mesmo que
        `kuro eval`). Casos: `cases_file` (JSONL local) ou `cases` ({id, input,
        dependencies, expected}). `compare_with` roda o mesmo dataset no agente de
        produção. Veja `verdict.ok` antes de promover. Os resultados vêm só dos casos
        que falharam, para caber no contexto."""
        try:
            if (cases_file is None) == (cases is None):
                raise EvalInputError("informe cases_file ou cases (um dos dois)")
            parsed = parse_cases(_read_text(cases_file, "cases"), cases_file) if cases_file else parse_cases(
                "\n".join(json.dumps(c, ensure_ascii=False) for c in cases or []), "cases"
            )
            if rules_file and rules is not None:
                raise EvalInputError("informe rules_file ou rules, não os dois")
            raw_rules = json.loads(_read_text(rules_file, "rules")) if rules_file else (rules or [])
            report = run_evaluation(
                client,
                agent_type,
                parsed,
                compare_with=compare_with,
                rules=check_rules_spec(raw_rules),
                fields=fields,
                min_pass=min_pass,
                tolerance=tolerance,
                dry_run=dry_run,
            )
        except (EvalInputError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        except (ApiError, ServiceUnavailable) as exc:
            raise _tool_error(exc) from exc
        for section in [report] + ([report["compare"]] if report.get("compare") else []):
            section["results"] = [_clip(r) for r in section["results"] if not r["passed"]]
        return report

    # -- tools ----------------------------------------------------------------

    @server.tool(annotations=READ)
    def tools_list() -> dict[str, Any]:
        """Tools cadastradas (as que um agente pode usar em `tools`)."""
        keys = ("tool_name", "kind", "label", "enabled", "side_effect", "is_seed")
        return {"items": [{k: t.get(k) for k in keys} for t in _call(client.list_tools)]}

    @server.tool(annotations=READ)
    def tool_get(tool_name: str, editable: bool = False) -> dict[str, Any]:
        """Uma tool (segredos mascarados). Com `editable=true`, só os campos aceitos por
        tool_apply — devolver a máscara de um segredo mantém o valor guardado."""
        detail = _call(client.get_tool, tool_name)
        if editable:
            return {"tool_name": detail["tool_name"], "kind": detail["kind"], **{k: detail.get(k) for k in TOOL_FIELDS}}
        return detail

    @server.tool(annotations=READ)
    def tool_catalog() -> dict[str, Any]:
        """Toolkits builtin disponíveis para criar uma tool kind=builtin."""
        return {"items": _call(client.tool_catalog)}

    @server.tool(annotations=OVERWRITE)
    def tool_apply(definition: dict[str, Any]) -> dict[str, Any]:
        """Cria a tool (precisa de tool_name e kind: builtin | api | python) ou atualiza
        os campos informados. O kind de uma tool existente não muda. Ver AGENTS.md,
        "Tools: de onde vem cada parâmetro" (source model | dependency | const)."""
        tool_name = definition.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            raise ToolError("a definição precisa do campo tool_name")
        existing = next((t for t in _call(client.list_tools) if t["tool_name"] == tool_name), None)
        if existing is None:
            if not definition.get("kind"):
                raise ToolError("tool nova precisa do campo kind (builtin, api ou python)")
            return {"action": "create", "tool": _call(client.create_tool, definition)}
        kind = definition.get("kind")
        if kind is not None and kind != existing["kind"]:
            raise ToolError(f"{tool_name} é kind={existing['kind']!r} e o kind não muda — crie outra tool")
        changes = {k: v for k, v in definition.items() if k not in ("tool_name", "kind")}
        _fields_only(changes, TOOL_FIELDS)
        return {"action": "update", "tool": _call(client.update_tool, tool_name, changes)}

    @server.tool(annotations=RUN)
    def tool_invoke(
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        function_name: str | None = None,
        dependencies: dict[str, Any] | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Executa uma tool isolada, sem agente. `function_name` é obrigatório em
        kind=builtin. `dependencies` preenche os parâmetros source=dependency.
        Falha da tool volta com `ok: false` e o motivo."""
        return _clip(_call(client.invoke_tool, tool_name, arguments or {}, function_name, dependencies, dry_run))

    def _confirm_delete_tool(tool_name: str, ctx: Context) -> Elicit[Confirmacao]:
        return _ask(ctx, f"kuro tools delete {tool_name} --yes", f"Remover a tool {tool_name!r}? Não tem volta.")

    @server.tool(annotations=DESTRUCTIVE)
    def tool_delete(tool_name: str, ok: Annotated[Confirmacao, Resolve(_confirm_delete_tool)]) -> dict[str, Any]:
        """Remove uma tool (pede confirmação). Trava se algum agente a usa."""
        if not ok.confirmar:
            return {"cancelled": True}
        _call(client.delete_tool, tool_name)
        return {"deleted": tool_name}

    # -- execuções (trace store) ----------------------------------------------

    @server.tool(annotations=READ)
    def runs_list(
        agent_type: str | None = None,
        status: Literal["success", "error"] | None = None,
        session_id: str | None = None,
        agent_version: int | None = None,
        meta: dict[str, str] | None = None,
        complexity: list[int] | None = None,
        tool_failed: bool = False,
        side_effect: bool = False,
        feedback: Literal["up", "down", "none"] | None = None,
        include_tests: bool = True,
        sample: int | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Execuções mais recentes, com filtros de revisão: `complexity` (1–3),
        `tool_failed`, `side_effect`, `feedback`, `sample` (amostra aleatória),
        `meta` (metadata que quem chamou mandou, ex.: {"conversation_id": "98231"}).
        `include_tests=false` tira os testes em dry_run. Detalhe: run_show."""
        page = _call(
            client.list_runs,
            agent_type=agent_type,
            status=status,
            session_id=session_id,
            agent_version=agent_version,
            meta=[f"{k}={v}" for k, v in (meta or {}).items()] or None,
            complexity=complexity or None,
            tool_failed=True if tool_failed else None,
            side_effect=True if side_effect else None,
            feedback=feedback,
            include_dry_run=None if include_tests else False,
            sample=sample,
            limit=max(1, min(limit, 100)),
            cursor=cursor,
        )
        return _clip(page, 300)

    @server.tool(annotations=READ)
    def run_show(run_id: str) -> dict[str, Any]:
        """Uma execução: mensagem, resposta, spans (modelo e tools, com entrada e saída)
        e notas. Textos longos vêm cortados."""
        return _clip(_call(client.run_trace, run_id))

    @server.tool(annotations=READ)
    def runs_stats(agent_type: str | None = None, since: str | None = None) -> dict[str, Any]:
        """Total, erros, tokens, custo estimado e série diária. `since` em ISO 8601."""
        return _call(client.run_stats, agent_type=agent_type, since=since)

    @server.tool(annotations=READ)
    def runs_overview(agent_type: str | None = None, since: str | None = None, include_tests: bool = False) -> dict[str, Any]:
        """O panorama do dashboard: totais × período anterior, agentes e versões, tools
        falhando. Exige chave de escopo admin."""
        return _call(client.overview, agent_type=agent_type, since=since, include_dry_run=include_tests)

    @server.tool(annotations=WRITE)
    def run_score(run_id: str, value: float, name: str = "manual", comment: str | None = None) -> dict[str, Any]:
        """Grava uma nota numa execução (ex.: 1 = correta, 0 = errada)."""
        return _call(client.score, {"run_id": run_id, "name": name, "value": value, "comment": comment})

    @server.tool(annotations=READ)
    def runs_agreement(agent_type: str | None = None, since: str | None = None) -> dict[str, Any]:
        """Modo shadow: concordância das decisões do Kuro com a referência do legado,
        por campo e por versão."""
        return _call(client.agreement, agent_type=agent_type, since=since)

    # -- conversas guardadas --------------------------------------------------

    @server.tool(annotations=READ)
    def sessions_list(agent_type: str | None = None, user_id: str = "mcp", limit: int = 20) -> dict[str, Any]:
        """Conversas guardadas de um user_id (o do chat por aqui é `mcp`; o da CLI, `cli`)."""
        return _clip(_call(client.list_sessions, user_id=user_id, component_id=agent_type, limit=limit, page=1), 300)

    @server.tool(annotations=READ)
    def session_show(session_id: str, user_id: str = "mcp") -> dict[str, Any]:
        """Transcrição de uma conversa: cada mensagem, a resposta e os tokens."""
        return {"items": _clip(_call(client.session_runs, session_id, user_id))}

    def _confirm_delete_session(session_id: str, ctx: Context) -> Elicit[Confirmacao]:
        return _ask(ctx, f"kuro sessions delete {session_id} --yes", f"Apagar a conversa {session_id!r}? Não tem volta (o registro em runs continua).")

    @server.tool(annotations=DESTRUCTIVE)
    def session_delete(
        session_id: str, ok: Annotated[Confirmacao, Resolve(_confirm_delete_session)], user_id: str = "mcp"
    ) -> dict[str, Any]:
        """Apaga uma conversa guardada (pede confirmação)."""
        if not ok.confirmar:
            return {"cancelled": True}
        _call(client.delete_session, session_id, user_id)
        return {"deleted": session_id}

    # -- bases de conhecimento ------------------------------------------------

    @server.tool(annotations=READ)
    def collections_list() -> dict[str, Any]:
        """Bases de conhecimento (RAG). Um agente consulta a de `knowledge_collection`."""
        return {"items": _call(client.list_collections)}

    @server.tool(annotations=READ)
    def collection_docs(name: str) -> dict[str, Any]:
        """Documentos indexados numa collection, com o status."""
        return _clip(_call(client.list_collection_documents, name), 300)

    @server.tool(annotations=READ)
    def collection_search(name: str, query: str, limit: int = 5) -> dict[str, Any]:
        """Busca na collection — o mesmo que o agente enxerga."""
        return {"items": _clip(_call(client.search_collection, name, query, limit), 1500)}

    @server.tool(annotations=WRITE)
    def collection_add(name: str, text: str | None = None, file: str | None = None, title: str | None = None) -> dict[str, Any]:
        """Indexa um documento: `text` direto ou `file` (caminho local: PDF, DOCX, CSV, TXT, MD...)."""
        if (text is None) == (file is None):
            raise ToolError("informe text ou file (um dos dois)")
        if file is not None:
            content = _read_bytes(file, "text")
            return _call(client.add_collection_file, name, filename=Path(file).name, content=content, title=title)
        return _call(client.add_document, name, {"text": text, "name": title})

    def _confirm_rm_doc(name: str, content_id: str, ctx: Context) -> Elicit[Confirmacao]:
        return _ask(ctx, f"kuro collections rm-doc {name} {content_id} --yes", f"Tirar o documento {content_id!r} da collection {name!r}? Não tem volta.")

    @server.tool(annotations=DESTRUCTIVE)
    def collection_rm_doc(
        name: str, content_id: str, ok: Annotated[Confirmacao, Resolve(_confirm_rm_doc)]
    ) -> dict[str, Any]:
        """Tira um documento da collection (pede confirmação)."""
        if not ok.confirmar:
            return {"cancelled": True}
        _call(client.delete_collection_document, name, content_id)
        return {"deleted": content_id}

    @server.tool(annotations=CRAWL)
    def collection_add_site(
        name: str,
        url: str,
        mode: Literal["crawl", "sitemap", "page"] = "crawl",
        max_pages: int | None = None,
        max_depth: int | None = None,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        format: bool = True,
        refresh: Literal["never", "daily", "weekly", "monthly"] = "never",
    ) -> dict[str, Any]:
        """Indexa um site na collection, em background: `crawl` segue links abaixo do caminho
        da URL, `sitemap` usa o sitemap, `page` só a URL. Cada página passa por um agente que
        organiza o texto sem reescrever (`format=false` pula). Acompanhe com `collection_sources`."""
        payload = {"url": url, "mode": mode, "max_pages": max_pages, "max_depth": max_depth,
                   "include": include, "exclude": exclude, "format": format, "refresh": refresh}
        return _call(client.create_source, name, {k: v for k, v in payload.items() if v is not None})

    @server.tool(annotations=READ)
    def collection_sources(name: str, source_id: str | None = None) -> dict[str, Any]:
        """Sites da collection e o estado da sincronização; com `source_id`, as páginas
        dele (status, trechos, se o formatador foi usado e o motivo de cada falha)."""
        if source_id:
            return _clip(_call(client.get_source, name, source_id), 300)
        return {"items": _call(client.list_sources, name)}

    @server.tool(annotations=RECRAWL)
    def collection_sync_site(name: str, source_id: str) -> dict[str, Any]:
        """Coleta o site de novo agora (só reprocessa as páginas que mudaram)."""
        return _call(client.sync_source, name, source_id)

    def _confirm_rm_site(name: str, source_id: str, ctx: Context) -> Elicit[Confirmacao]:
        return _ask(ctx, f"kuro collections rm-site {name} {source_id} --yes", f"Remover o site {source_id!r} e as páginas dele da collection {name!r}?")

    @server.tool(annotations=DESTRUCTIVE)
    def collection_rm_site(
        name: str, source_id: str, ok: Annotated[Confirmacao, Resolve(_confirm_rm_site)]
    ) -> dict[str, Any]:
        """Remove um site e as páginas dele da collection (pede confirmação)."""
        if not ok.confirmar:
            return {"cancelled": True}
        _call(client.delete_source, name, source_id)
        return {"deleted": source_id}

    return server


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("agent-service")
    except Exception:  # noqa: BLE001 - versão é informativa
        return ""


def main() -> None:
    # O stdout é o canal do protocolo: nada pode ser impresso nele.
    timeout = float(os.environ.get("KURO_TIMEOUT") or 300)
    # `KURO_MCP_LOCAL_FILES=0`: o processo roda no container (o `kuro connect` o chama por
    # `docker exec`), então um caminho de arquivo seria do servidor, não de quem pediu.
    local_files = os.environ.get("KURO_MCP_LOCAL_FILES", "1").strip().lower() not in ("0", "false", "no")
    # Uma linha INFO no stderr por chamada à API: vira ruído no log do cliente MCP e, pelo
    # `kuro connect`, no terminal de quem conecta. Erro de conexão já volta na tool.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build_server(client_from_env(timeout=timeout), local_files=local_files).run("stdio")


if __name__ == "__main__":
    main()
