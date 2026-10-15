"""Servidor MCP do Kuro (`kuro-mcp`): as operações da CLI como tools MCP.

Três jeitos de rodar, sempre falando com o serviço pela API HTTP (não toca no banco: é
mais um cliente da API, como a CLI e o console):

- dentro do container, por stdio — o que o `kuro connect` registra (`docker exec` pelo SSH);
- servido pelo próprio serviço em `/mcp` (`api/mcp_routes.py`);
- na máquina de quem opera, com as mesmas variáveis da CLI (`KURO_API_URL`, `KURO_API_KEY`,
  `KURO_CA_BUNDLE`).

Nos dois primeiros (`local_files=False`) o processo está no servidor: caminhos de arquivo
são recusados e tools `kind="python"` não são criadas nem alteradas por aqui.

Regras de projeto:

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
- **Resposta com teto** (`MAX_RESPONSE`): um trace com muitas spans, o histórico de
  um prompt longo ou uma busca com RAG comeriam o contexto do modelo. Textos e listas
  são cortados até caber, e a resposta diz `truncated: true`.

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
# E a resposta inteira, em caracteres de JSON: 30 spans de 4000 caracteres passam de 100 mil.
MAX_RESPONSE = 30_000

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
# Testar uma credencial chama o provedor e grava o resultado, sem mudar nada que importe.
PROBE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
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
runs_list, runs_overview e procedure_funnel deixam os testes (dry_run) de fora por padrão — e o chat
daqui é dry_run: para ver o run que você acabou de criar, use run_show(run_id), runs_list(session_id=...)
ou include_tests=true.
Modo shadow → dataset: runs_agreement mostra onde o agente discorda do legado; runs_export devolve esses
runs como casos, prontos para eval(cases=...). runs_reference grava a decisão do legado num run.
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


def _is_handle(key: str) -> bool:
    """Valores que o modelo devolve à API (cursor, ids, URLs): cortado, não servem mais."""
    return key in ("id", "cursor", "next_cursor", "url", "source_url") or key.endswith("_id")


def _clip(value: Any, limit: int = MAX_TEXT, max_items: int | None = None) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"… [cortado: {len(value) - limit} caracteres a mais]"
    if isinstance(value, dict):
        return {k: v if _is_handle(k) else _clip(v, limit, max_items) for k, v in value.items()}
    if isinstance(value, list):
        items = [_clip(v, limit, max_items) for v in value[:max_items]]
        if max_items is not None and len(value) > max_items:
            items.append(f"… [cortado: {len(value) - max_items} itens a mais]")
        return items
    return value


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


# Cada passo corta mais: primeiro só os textos, depois também as listas.
_FIT_STEPS = ((MAX_TEXT, None), (1500, 40), (500, 20), (200, 10), (80, 5))


def _fit(value: dict[str, Any], budget: int = MAX_RESPONSE, *, paginated: bool = False) -> dict[str, Any]:
    """Corta textos e listas até a resposta caber em `budget` caracteres de JSON. Se cortou
    alguma coisa, diz (`truncated: true`): o modelo pede o pedaço que precisa (um run, uma
    versão) em vez de concluir sobre um dado incompleto achando que está inteiro.

    `paginated`: só os textos encolhem, nunca a lista — tirar itens de uma página faria o
    `next_cursor` pular os que saíram. Cursor, ids e URLs nunca são cortados (`_is_handle`)."""
    fitted = value
    for limit, max_items in _FIT_STEPS:
        fitted = _clip(value, limit, None if paginated else max_items)
        if _size(fitted) <= budget:
            break
    if fitted == value:
        return value
    return {**fitted, "truncated": True}


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


def _extras(
    body: dict[str, Any], attachments: list["Anexo"] | None, metadata: dict[str, Any] | None, dry_run: bool
) -> None:
    """Os campos opcionais comuns ao /chat e ao /analyze — só entram quando vêm."""
    if attachments:
        anexos = [a.model_dump(exclude_none=True) for a in attachments]
        if any(("url" in a) == ("content_base64" in a) for a in anexos):
            raise ToolError("cada anexo leva url ou content_base64 (um dos dois)")
        body["attachments"] = anexos
    if metadata:
        body["metadata"] = metadata
    if dry_run:
        body["dry_run"] = True


COLLECTION_FIELDS = ("label", "description", "search_mode", "max_results", "vector_weight", "reranker")
"""O que `PUT /collections/{nome}` aceita (`CollectionUpdateIn`)."""

NO_LOCAL_FILES = (
    "este servidor MCP roda no servidor do Kuro, não na sua máquina: não lê arquivos por caminho. "
    "Mande o conteúdo inline ({inline})."
)

NO_PYTHON_TOOLS = (
    "tools kind=python não são criadas nem alteradas por este servidor MCP: ele roda no servidor do "
    "Kuro, e o código de uma tool Python executa dentro do processo do serviço, ao alcance dos segredos "
    "do container. Quem tem acesso ao servidor faz isso pela CLI (`kuro tools apply -f`) ou pelo console."
)

# -- parâmetros ------------------------------------------------------------------
# O schema que chega ao modelo só tem o que estiver em `Field(description=...)`: sem isso,
# cada parâmetro vira `{"title": "Agent Type", "type": "string"}` e o modelo adivinha.

AgentType = Annotated[str, Field(description="Slug do agente (agent_type), ex.: suporte. Veja agents_list.")]
AgentFilter = Annotated[str | None, Field(description="Só deste agente (agent_type). Sem ele, todos.")]
ToolName = Annotated[str, Field(description="Nome da tool (tool_name), ex.: calculator. Veja tools_list.")]
RunId = Annotated[str, Field(description="Id do run (run_id), como runs_list e chat devolvem.")]
SessionId = Annotated[str, Field(description="Id da conversa (session_id), como chat e sessions_list devolvem.")]
CollectionName = Annotated[str, Field(description="Nome da collection (base de conhecimento). Veja collections_list.")]
SourceId = Annotated[str, Field(description="Id do site na collection (source_id). Veja collection_sources.")]
Since = Annotated[
    str | None, Field(description="Início do período em ISO 8601, ex.: 2026-10-01 ou 2026-10-01T09:00:00-03:00.")
]
IncludeTests = Annotated[bool, Field(description="Inclui as execuções de teste (dry_run), como as do Playground.")]
DryRun = Annotated[
    bool,
    Field(description="Teste: as tools recebem X-Kuro-Dry-Run: true e decidem o que simular. O run fica marcado."),
]
ValidateOnly = Annotated[bool, Field(description="Valida no servidor sem gravar nada.")]
Dependencies = Annotated[
    dict[str, Any] | None,
    Field(
        description='Os dados que quem integra mandaria em `dependencies`, ex.: {"cpf": "12345678900"}. '
        "Os obrigatórios estão em dependency_fields do agente (agent_get)."
    ),
]
UserId = Annotated[
    str | None,
    Field(
        description="Só as conversas deste dono: mcp para as abertas por aqui, cli para as da CLI, ou o user_id de "
        "uma integração. Sem ele, as de todos."
    ),
]
Metadata = Annotated[
    dict[str, str | int | float | bool] | None,
    Field(
        description='Correlação com o sistema que chama (até 20 pares), ex.: {"conversation_id": "98231"}. Não vai ao '
        "modelo; fica no run e filtra runs_list(meta=...)."
    ),
]


class Anexo(BaseModel):
    """Um anexo direto ao modelo — o mesmo `attachments` do /chat e do /analyze."""

    url: str | None = Field(
        default=None,
        description="Endereço público do arquivo. Prefira isto a content_base64, que passa inteiro pelo seu contexto.",
    )
    content_base64: str | None = Field(default=None, description="O conteúdo em base64 (um dos dois: url ou este).")
    mime_type: str | None = Field(default=None, description="Ex.: image/png, application/pdf, audio/mpeg.")
    filename: str | None = Field(default=None, description="O nome do arquivo, ex.: contrato.pdf.")


Attachments = Annotated[
    list[Anexo] | None,
    Field(
        description="Imagem, áudio, vídeo ou arquivo (PDF, DOCX...) direto ao modelo: [{url | content_base64, "
        "mime_type, filename}]. O modelo do agente precisa suportar o tipo (ex.: Gemini); limite MAX_ATTACHMENT_MB."
    ),
]

# O que `analyze` manda em `document` quando só vem anexo — o mesmo da CLI.
ATTACHMENT_PROMPT = "Analise o(s) anexo(s)."


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
        instructions += "Tools kind=python não são criadas nem alteradas por aqui (use a CLI no servidor).\n"
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
    def provider_models(
        provider: Annotated[str, Field(description="Provedor de modelo, ex.: google, openai, anthropic. Veja providers_list.")],
        refresh: Annotated[bool, Field(description="Ignora o cache de 15 minutos e consulta o provedor de novo.")] = False,
    ) -> dict[str, Any]:
        """Modelos que o provedor oferece agora (o que vale em `model_id`). Cada um traz
        `channel`: stable | preview | alias — não use preview/alias em produção."""
        return _call(client.provider_models, provider, refresh="true" if refresh else None)

    @server.tool(annotations=READ)
    def credentials_list(
        provider: Annotated[str | None, Field(description="Só deste provedor, ex.: google. Sem ele, todos.")] = None,
    ) -> dict[str, Any]:
        """Credenciais de modelo cadastradas (a chave nunca aparece). Cadastrar ou trocar
        uma chave não é feito por aqui: use `kuro credentials add` ou o console."""
        rows = _call(client.list_credentials)
        return {"items": [c for c in rows if provider is None or c["provider"] == provider]}

    @server.tool(annotations=PROBE)
    def credentials_test(
        credential_id: Annotated[str, Field(description="O id da credencial (veja credentials_list).")],
    ) -> dict[str, Any]:
        """Testa uma credencial salva no provedor, sem gastar tokens, e grava o resultado
        (`last_test_ok`). É o primeiro passo diante de um 503 model_provider_not_configured
        ou de um 502 do provedor. A chave não aparece na resposta."""
        return {"credential_id": credential_id, **_call(client.test_credential, credential_id)}

    # -- agentes --------------------------------------------------------------

    @server.tool(annotations=READ)
    def agents_list() -> dict[str, Any]:
        """Agentes cadastrados, resumidos. Use agent_get para a definição completa."""
        keys = ("agent_type", "name", "kind", "model_provider", "model_id", "prompt_version", "is_seed")
        return {"items": [{k: a.get(k) for k in keys} for a in _call(client.list_agents)]}

    @server.tool(annotations=READ)
    def agent_get(
        agent_type: AgentType,
        editable: Annotated[
            bool, Field(description="Só os campos aceitos por agent_apply/agent_set, sem corte: o ponto de partida para editar.")
        ] = False,
    ) -> dict[str, Any]:
        """Definição de um agente. Com `editable=true`, só os campos aceitos por
        agent_apply/agent_set — o ponto de partida para editar. Sem `editable`, textos
        longos podem vir cortados (`truncated: true`)."""
        definition = _call(client.get_agent, agent_type)
        # Para editar não se corta nada: o modelo devolveria o texto cortado no agent_apply.
        return agent_editable(definition) if editable else _fit(definition)

    @server.tool(annotations=OVERWRITE)
    def agent_apply(
        definition: Annotated[
            dict[str, Any],
            Field(description="A definição do agente: agent_type (obrigatório) e os campos de agent_get(editable=true)."),
        ],
        dry_run: ValidateOnly = False,
    ) -> dict[str, Any]:
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
    def agent_set(
        agent_type: AgentType,
        changes: Annotated[
            dict[str, Any],
            Field(description='Só os campos a mudar, ex.: {"num_history_runs": 5}. Aceitos: os de agent_get(editable=true).'),
        ],
        dry_run: ValidateOnly = False,
    ) -> dict[str, Any]:
        """Altera só os campos informados, ex.: {"num_history_runs": 5, "tools": ["calculator"]}.
        `instructions` aceita lista de strings (ou uma string só)."""
        _fields_only(changes, AGENT_FIELDS)
        if isinstance(changes.get("instructions"), str):
            changes = {**changes, "instructions": [changes["instructions"]]}
        return _call(client.update_agent, agent_type, changes, dry_run=dry_run)

    @server.tool(annotations=READ)
    def agent_versions(
        agent_type: AgentType,
        version: Annotated[
            int | None, Field(description="Uma versão só, com o texto inteiro das instructions. Sem ela, a lista resumida.")
        ] = None,
    ) -> dict[str, Any]:
        """Histórico do prompt (instructions). Sem `version`, a lista resumida (versão, data,
        tamanho e o começo do texto); com `version`, as instructions daquela versão."""
        versions = _call(client.agent_versions, agent_type)
        if version is not None:
            target = next((v for v in versions if v["version"] == version), None)
            if target is None:
                available = ", ".join(f"v{v['version']}" for v in versions) or "nenhuma"
                raise ToolError(f"{agent_type} não tem a versão v{version} (disponíveis: {available})")
            return _fit(target)
        items = []
        for v in versions:
            text = "\n".join(v.get("instructions") or [])
            items.append(
                {"version": v["version"], "created_at": v.get("created_at"), "chars": len(text), "preview": _clip(text, 200)}
            )
        return _fit({"items": items})

    @server.tool(annotations=READ)
    def agent_revisions(
        agent_type: AgentType,
        version: Annotated[int | None, Field(description="Uma versão só (o agent_version de um run), completa.")] = None,
    ) -> dict[str, Any]:
        """Versões da configuração inteira (modelo, tools, schema, regras) — é o
        `agent_version` que cada run grava. Com `version`, a configuração completa dela."""
        if version is not None:
            return _fit(_call(client.agent_revision, agent_type, version))
        return _fit({"items": _call(client.agent_revisions, agent_type)})

    @server.tool(annotations=READ)
    def agent_integration(agent_type: AgentType) -> dict[str, Any]:
        """Como outro sistema chama este agente: endpoint (com a URL pública do serviço),
        cURL, dependências obrigatórias, exemplo de resposta, eventos do stream e erros."""
        return _call(client.integration, agent_type)

    @server.tool(annotations=READ)
    def agents_export(
        agent_types: Annotated[
            list[str] | None, Field(description="Os agentes a exportar. Sem nenhum, todos menos os seed.")
        ] = None,
    ) -> dict[str, Any]:
        """A definição editável de cada agente, como `kuro agents export` grava num
        <agent_type>.json: o agente como código, pronto para agent_apply. Nada é cortado;
        o que não couber na resposta vem listado em `remaining` — peça esses depois."""
        rows = _call(client.list_agents)
        wanted = list(dict.fromkeys(agent_types or []))
        missing = sorted(set(wanted) - {a["agent_type"] for a in rows})
        if missing:
            raise ToolError(f"agentes não encontrados: {', '.join(missing)}")
        chosen = [a for a in rows if a["agent_type"] in wanted] if wanted else [a for a in rows if not a.get("is_seed")]
        # Cortar o texto de uma definição faria o agent_apply gravar o prompt pela metade.
        items: list[dict[str, Any]] = []
        remaining: list[str] = []
        for agent in chosen:
            data = {k: v for k, v in agent_editable(agent).items() if v is not None}
            if remaining or (items and _size(items + [data]) > MAX_RESPONSE):
                remaining.append(agent["agent_type"])
            else:
                items.append(data)
        return {"items": items, **({"remaining": remaining} if remaining else {})}

    @server.tool(annotations=READ)
    def procedure_state(agent_type: AgentType, session_id: SessionId) -> dict[str, Any]:
        """Agente procedural: em que etapa a conversa está, o que foi coletado, o que
        falta e, se concluída, o resultado. O `chat` já devolve isso em `state`."""
        return _call(client.procedure_state, agent_type, session_id)

    @server.tool(annotations=READ)
    def procedure_funnel(agent_type: AgentType, include_tests: IncludeTests = False) -> dict[str, Any]:
        """Agente procedural: quantas conversas estão paradas em cada etapa e quantas
        concluíram — onde as pessoas desistem."""
        return _call(client.procedure_funnel, agent_type, include_dry_run=include_tests)

    def _confirm_delete_agent(agent_type: str, ctx: Context) -> Elicit[Confirmacao]:
        return _ask(ctx, f"kuro agents delete {agent_type} --yes", f"Remover o agente {agent_type!r}? Não tem volta.")

    @server.tool(annotations=DESTRUCTIVE)
    def agent_delete(
        agent_type: AgentType, ok: Annotated[Confirmacao, Resolve(_confirm_delete_agent)]
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
        agent_type: AgentType,
        version: Annotated[int, Field(description="A versão do prompt a restaurar (veja agent_versions).")],
        ok: Annotated[Confirmacao, Resolve(_confirm_rollback)]
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
        source: Annotated[str, Field(description="O agente de onde a configuração sai, ex.: r8-draft.")],
        to: Annotated[str, Field(description="O agente que recebe a configuração (criado se não existir), ex.: r8.")],
        ok: Annotated[Confirmacao, Resolve(_confirm_promote)]
    ) -> dict[str, Any]:
        """Copia a configuração de um agente para outro — o fluxo draft → prod (pede
        confirmação). Para criar o draft, promova ao contrário: source=prod, to=prod-draft."""
        if not ok.confirmar:
            return {"cancelled": True}
        return _call(client.promote_agent, source, to)

    # -- feedback (nota de regras do agente) ----------------------------------

    @server.tool(annotations=READ)
    def feedback_show(agent_type: AgentType) -> dict[str, Any]:
        """Regras de comportamento aprendidas do feedback, com os ids."""
        return _call(client.get_feedback, agent_type)

    @server.tool(annotations=READ)
    def feedback_versions(agent_type: AgentType) -> dict[str, Any]:
        """Histórico de versões da nota de feedback (textos longos vêm cortados)."""
        return _fit({"items": _call(client.feedback_versions, agent_type)})

    @server.tool(annotations=OVERWRITE)
    def feedback_send(
        agent_type: AgentType,
        session_id: SessionId,
        message: Annotated[
            str, Field(description="O que o agente deveria ter feito, ex.: confirmar o CPF antes de dar detalhes.")
        ],
    ) -> dict[str, Any]:
        """Ensina o agente a partir de uma conversa (a `session_id` que o chat devolveu):
        o feedback é mesclado nas regras e a resposta traz o `diff`. Só para agentes
        conversacionais. Vale na próxima mensagem — prefira fazer isso num draft."""
        return _call(client.send_feedback, agent_type, session_id, message)

    @server.tool(annotations=OVERWRITE)
    def feedback_remove(
        agent_type: AgentType,
        rule_ids: Annotated[list[str], Field(description="Ids das regras a apagar (veja feedback_show).")],
    ) -> dict[str, Any]:
        """Apaga regras da nota pelo id (veja feedback_show). Fica no histórico."""
        current = _call(client.get_feedback, agent_type)
        keep = [r for r in current["rules"] if r["id"] not in set(rule_ids)]
        if len(keep) == len(current["rules"]):
            raise ToolError(f"nenhuma regra com id em {', '.join(rule_ids)}")
        if not keep:
            raise ToolError("isso apagaria todas as regras — use feedback_clear")
        return _call(client.replace_feedback, agent_type, keep)

    @server.tool(annotations=OVERWRITE)
    def feedback_rollback(
        agent_type: AgentType,
        version: Annotated[int, Field(description="A versão da nota a restaurar (veja feedback_versions).")],
    ) -> dict[str, Any]:
        """Reaplica as regras de uma versão anterior da nota (vira uma versão nova)."""
        return _call(client.rollback_feedback, agent_type, version)

    def _confirm_clear(agent_type: str, ctx: Context) -> Elicit[Confirmacao]:
        return _ask(ctx, f"kuro agents feedback {agent_type} --clear --yes", f"Zerar a nota de feedback de {agent_type!r}, com o histórico? Não tem volta.")

    @server.tool(annotations=DESTRUCTIVE)
    def feedback_clear(agent_type: AgentType, ok: Annotated[Confirmacao, Resolve(_confirm_clear)]) -> dict[str, Any]:
        """Zera a nota de feedback e o histórico dela (pede confirmação)."""
        if not ok.confirmar:
            return {"cancelled": True}
        _call(client.clear_feedback, agent_type)
        return {"cleared": agent_type}

    # -- executar -------------------------------------------------------------

    @server.tool(annotations=RUN)
    def chat(
        agent_type: AgentType,
        message: Annotated[str, Field(description="A mensagem do usuário.")],
        session_id: Annotated[
            str | None, Field(description="Para continuar uma conversa: o session_id que o chat anterior devolveu.")
        ] = None,
        dependencies: Dependencies = None,
        user_id: Annotated[str, Field(description="Quem conversa (o histórico é por user_id e session_id).")] = "mcp",
        attachments: Attachments = None,
        metadata: Metadata = None,
        dry_run: DryRun = True,
    ) -> dict[str, Any]:
        """Manda uma mensagem a um agente conversacional. Sem `session_id`, começa uma
        conversa nova; para continuá-la, repasse o `session_id` devolvido.
        `dependencies` são os dados que quem integra mandaria (ex.: {"cpf": "..."}).
        `attachments` vão direto ao modelo e ficam no histórico da conversa.
        `dry_run` (padrão true) avisa as tools de que é teste."""
        definition = _call(client.get_agent, agent_type)
        body: dict[str, Any] = {
            "agent_type": agent_type,
            "user_id": user_id,
            "session_id": session_id or f"mcp-{uuid.uuid4().hex[:12]}",
            "message": message,
            "dependencies": _dependencies(definition, dependencies) or None,
        }
        _extras(body, attachments, metadata, dry_run)
        result = _call(collect_stream, client, body)
        if result["error"]:
            raise ToolError(f"{result['error']} (run {result['run_id']}; veja run_show)")
        return result

    @server.tool(annotations=RUN)
    def analyze(
        agent_type: AgentType,
        document: Annotated[str | None, Field(description="O texto a analisar (ou JSON como texto).")] = None,
        document_file: Annotated[
            str | None, Field(description="Caminho de um arquivo de texto local. Recusado quando o MCP roda no servidor.")
        ] = None,
        dependencies: Dependencies = None,
        session_id: Annotated[
            str | None, Field(description="Agrupa os runs por conversa (ex.: o id da conversa no outro sistema).")
        ] = None,
        user_id: Annotated[str | None, Field(description="Quem está por trás do documento (filtra os runs).")] = None,
        attachments: Attachments = None,
        metadata: Metadata = None,
        dry_run: DryRun = True,
    ) -> dict[str, Any]:
        """Roda um agente analista (kind=analysis): devolve `result` no formato do
        response_schema dele. Passe o texto em `document`, o caminho de um arquivo
        de texto local em `document_file`, ou só `attachments` (um PDF, uma imagem)."""
        if document is not None and document_file is not None:
            raise ToolError("informe document ou document_file, não os dois")
        if document is None and document_file is None and not attachments:
            raise ToolError("informe document, document_file ou attachments")
        definition = _call(client.get_agent, agent_type)
        if definition.get("kind") != "analysis":
            raise ToolError(f"{agent_type!r} não é kind=analysis — use chat")
        text = document if document is not None else _read_text(document_file, "document") if document_file else ""
        body: dict[str, Any] = {
            "agent_type": agent_type,
            # Só anexo: o /analyze exige um `document`, e a CLI manda esta instrução curta.
            "document": text if text.strip() or not attachments else ATTACHMENT_PROMPT,
            "dependencies": _dependencies(definition, dependencies) or None,
        }
        if session_id:
            body["session_id"] = session_id
        if user_id:
            body["user_id"] = user_id
        _extras(body, attachments, metadata, dry_run)
        return _call(client.analyze, body)

    @server.tool(annotations=RUN)
    def eval(
        agent_type: AgentType,
        cases_file: Annotated[
            str | None, Field(description="Caminho de um JSONL local. Recusado quando o MCP roda no servidor: use cases.")
        ] = None,
        cases: Annotated[
            list[dict[str, Any]] | None,
            Field(description='Os casos: [{"id", "input": texto ou objeto, "dependencies", "expected": {campo: valor}}].'),
        ] = None,
        rules_file: Annotated[
            str | None, Field(description="Caminho de um JSON de regras local. Recusado quando o MCP roda no servidor.")
        ] = None,
        rules: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description='Regras de negócio: [{"name", "field", "op": eq|ne|in|not_in|lt|lte|gt|gte|exists|not_exists, '
                '"value" (ou {"$dep": "campo"}), "when"?}]. Violação reprova o caso.'
            ),
        ] = None,
        compare_with: Annotated[
            str | None, Field(description="Roda o mesmo dataset neste outro agente (ex.: o de produção) e compara.")
        ] = None,
        fields: Annotated[
            list[str] | None, Field(description='Só estes campos do expected entram na comparação, ex.: ["acao"].')
        ] = None,
        min_pass: Annotated[float, Field(description="Taxa de acerto mínima (0 a 1) para verdict.ok.")] = 0.9,
        tolerance: Annotated[float, Field(description="Diferença aceita entre números (relativa).")] = 0.01,
        dry_run: DryRun = True,
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
    def tool_get(
        tool_name: ToolName,
        editable: Annotated[
            bool, Field(description="Só os campos aceitos por tool_apply, o ponto de partida para editar.")
        ] = False,
    ) -> dict[str, Any]:
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
    def tool_apply(
        definition: Annotated[
            dict[str, Any],
            Field(
                description="tool_name e kind (builtin | api | python) para criar; para atualizar, tool_name e os "
                "campos de tool_get(editable=true): label, description, config, enabled, side_effect."
            ),
        ],
    ) -> dict[str, Any]:
        """Cria a tool (precisa de tool_name e kind: builtin | api | python) ou atualiza
        os campos informados. O kind de uma tool existente não muda. Ver AGENTS.md,
        "Tools: de onde vem cada parâmetro" (source model | dependency | const)."""
        tool_name = definition.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            raise ToolError("a definição precisa do campo tool_name")
        existing = next((t for t in _call(client.list_tools) if t["tool_name"] == tool_name), None)
        # O código de uma tool Python roda no processo do serviço: pelo MCP no servidor, quem
        # tem a chave admin leria os segredos do container por aí — o que `local_files` evita.
        if not local_files and "python" in (definition.get("kind"), (existing or {}).get("kind")):
            raise ToolError(NO_PYTHON_TOOLS)
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
        tool_name: ToolName,
        arguments: Annotated[
            dict[str, Any] | None, Field(description='Os argumentos que o modelo preencheria, ex.: {"a": 2, "b": 3}.')
        ] = None,
        function_name: Annotated[
            str | None, Field(description="Obrigatório em kind=builtin: a função do toolkit (veja tool_get), ex.: add.")
        ] = None,
        dependencies: Annotated[
            dict[str, Any] | None, Field(description='Valores dos parâmetros source=dependency, ex.: {"cpf": "..."}.')
        ] = None,
        dry_run: DryRun = True,
    ) -> dict[str, Any]:
        """Executa uma tool isolada, sem agente. `function_name` é obrigatório em
        kind=builtin. `dependencies` preenche os parâmetros source=dependency.
        Falha da tool volta com `ok: false` e o motivo."""
        return _fit(_clip(_call(client.invoke_tool, tool_name, arguments or {}, function_name, dependencies, dry_run)))

    def _confirm_delete_tool(tool_name: str, ctx: Context) -> Elicit[Confirmacao]:
        return _ask(ctx, f"kuro tools delete {tool_name} --yes", f"Remover a tool {tool_name!r}? Não tem volta.")

    @server.tool(annotations=DESTRUCTIVE)
    def tool_delete(tool_name: ToolName, ok: Annotated[Confirmacao, Resolve(_confirm_delete_tool)]) -> dict[str, Any]:
        """Remove uma tool (pede confirmação). Trava se algum agente a usa."""
        if not ok.confirmar:
            return {"cancelled": True}
        _call(client.delete_tool, tool_name)
        return {"deleted": tool_name}

    # -- execuções (trace store) ----------------------------------------------

    @server.tool(annotations=READ)
    def runs_list(
        agent_type: AgentFilter = None,
        status: Annotated[
            Literal["success", "error"] | None, Field(description="Só os que deram certo ou só os com erro.")
        ] = None,
        session_id: Annotated[
            str | None, Field(description="Só os runs desta conversa (inclui os testes, qualquer que seja include_tests).")
        ] = None,
        user_id: Annotated[str | None, Field(description="Só os runs deste user_id (mcp, cli ou o de uma integração).")] = None,
        agent_version: Annotated[
            int | None, Field(description="Só runs desta versão da configuração (agent_revisions).")
        ] = None,
        meta: Annotated[
            dict[str, str] | None,
            Field(description='Pela metadata que quem chamou mandou, ex.: {"conversation_id": "98231"}.'),
        ] = None,
        knowledge_collection: Annotated[
            str | None, Field(description="Só runs de agentes que consultavam esta collection.")
        ] = None,
        collection_version: Annotated[
            int | None, Field(description="Só runs com esta versão da busca da collection (collection_versions).")
        ] = None,
        complexity: Annotated[
            list[int] | None,
            Field(description="Complexidade 1 a 3 (tools de negócio distintas que o run chamou), ex.: [3] ou [2, 3]."),
        ] = None,
        tool_failed: Annotated[bool, Field(description="Só os em que alguma tool falhou.")] = False,
        side_effect: Annotated[bool, Field(description="Só os que chamaram tool com efeito colateral.")] = False,
        feedback: Annotated[
            Literal["up", "down", "none"] | None,
            Field(description="Pelo feedback: positivo (up), negativo (down) ou sem nota (none)."),
        ] = None,
        min_chars: Annotated[
            int | None, Field(description='Esconde mensagens mais curtas que isto (tira "ok", "oi"), ex.: 20.')
        ] = None,
        include_tests: IncludeTests = False,
        sample: Annotated[
            int | None, Field(description="Amostra aleatória deste tamanho (pega o erro que filtro nenhum aponta).")
        ] = None,
        limit: Annotated[int, Field(description="Quantos por página (1 a 100).")] = 20,
        cursor: Annotated[str | None, Field(description="O next_cursor da página anterior.")] = None,
    ) -> dict[str, Any]:
        """Execuções mais recentes, com filtros de revisão: `complexity` (1–3),
        `tool_failed`, `side_effect`, `feedback`, `sample` (amostra aleatória),
        `meta` (metadata que quem chamou mandou, ex.: {"conversation_id": "98231"}).
        Os testes em dry_run (como o chat daqui) ficam de fora, a menos que `include_tests=true`
        ou que se filtre por `session_id`. Detalhe: run_show."""
        page = _call(
            client.list_runs,
            agent_type=agent_type,
            status=status,
            session_id=session_id,
            user_id=user_id,
            min_message_chars=min_chars,
            agent_version=agent_version,
            knowledge_collection=knowledge_collection,
            collection_version=collection_version,
            meta=[f"{k}={v}" for k, v in (meta or {}).items()] or None,
            complexity=complexity or None,
            tool_failed=True if tool_failed else None,
            side_effect=True if side_effect else None,
            feedback=feedback,
            # Numa conversa específica o teste é o que se procura: o run que o chat acabou de criar.
            include_dry_run=None if include_tests or session_id else False,
            sample=sample,
            limit=max(1, min(limit, 100)),
            cursor=cursor,
        )
        return _fit(_clip(page, 300), paginated=True)

    @server.tool(annotations=READ)
    def run_show(run_id: RunId) -> dict[str, Any]:
        """Uma execução: mensagem, resposta, spans (modelo e tools, com entrada e saída)
        e notas. Textos longos e listas grandes vêm cortados (`truncated: true`)."""
        return _fit(_call(client.run_trace, run_id))

    @server.tool(annotations=READ)
    def runs_stats(agent_type: AgentFilter = None, since: Since = None) -> dict[str, Any]:
        """Total, erros, tokens, custo estimado e série diária. `since` em ISO 8601."""
        return _call(client.run_stats, agent_type=agent_type, since=since)

    @server.tool(annotations=READ)
    def runs_overview(
        agent_type: AgentFilter = None, since: Since = None, include_tests: IncludeTests = False
    ) -> dict[str, Any]:
        """O panorama do dashboard: totais × período anterior, agentes e versões, tools
        falhando. Exige chave de escopo admin."""
        return _call(client.overview, agent_type=agent_type, since=since, include_dry_run=include_tests)

    @server.tool(annotations=READ)
    def runs_sessions(
        agent_type: AgentFilter = None,
        status: Annotated[
            Literal["success", "error"] | None, Field(description="Só as sessões com runs neste status.")
        ] = None,
        user_id: Annotated[str | None, Field(description="Só as sessões deste user_id.")] = None,
        limit: Annotated[
            int, Field(description="Quantas execuções recentes são varridas para agrupar (1 a 200) — não o nº de sessões.")
        ] = 100,
    ) -> dict[str, Any]:
        """Execuções agrupadas por sessão (session_id): runs, tokens, custo, erros e notas
        somados — a revisão por conversa, como a aba Logs. Inclui os testes (dry_run).
        A transcrição de uma delas: session_show; os runs: runs_list(session_id=...)."""
        page = _call(
            client.run_sessions, agent_type=agent_type, status=status, user_id=user_id, limit=max(1, min(limit, 200))
        )
        return _fit(_clip(page, 300), paginated=True)

    @server.tool(annotations=WRITE)
    def run_score(
        run_id: RunId,
        value: Annotated[float, Field(description="A nota, ex.: 1 = correta, 0 = errada.")],
        name: Annotated[str, Field(description="O nome da nota (separa critérios diferentes).")] = "manual",
        comment: Annotated[str | None, Field(description="Por que essa nota.")] = None,
    ) -> dict[str, Any]:
        """Grava uma nota numa execução (ex.: 1 = correta, 0 = errada)."""
        return _call(client.score, {"run_id": run_id, "name": name, "value": value, "comment": comment})

    @server.tool(annotations=READ)
    def runs_agreement(agent_type: AgentFilter = None, since: Since = None) -> dict[str, Any]:
        """Modo shadow: concordância das decisões do Kuro com a referência do legado,
        por campo e por versão."""
        return _call(client.agreement, agent_type=agent_type, since=since)

    @server.tool(annotations=OVERWRITE)
    def runs_reference(
        run_id: RunId,
        reference: Annotated[
            dict[str, Any],
            Field(description='A decisão de referência no formato do result do agente, ex.: {"acao": "escalar"}.'),
        ],
        source: Annotated[str, Field(description="De onde veio a referência, ex.: legacy, humano.")] = "legacy",
    ) -> dict[str, Any]:
        """Grava a decisão de referência de um run (modo shadow: a do agente legado, ou a
        correção de um humano). Substitui a que o run já tiver. É o que runs_agreement
        compara e o que runs_export transforma em `expected`."""
        return _call(client.save_reference, {"run_id": run_id, "reference": reference, "source": source})

    @server.tool(annotations=READ)
    def runs_export(
        agent_type: AgentType,
        since: Since = None,
        agent_version: Annotated[
            int | None, Field(description="Só runs desta versão da configuração (agent_revisions).")
        ] = None,
        limit: Annotated[int, Field(description="Quantos casos, dos mais recentes (1 a 500).")] = 100,
    ) -> dict[str, Any]:
        """Runs com referência como dataset: [{id, input, dependencies, expected}], o formato
        que eval(cases=...) aceita — o mesmo JSONL de `kuro runs export`. Nenhum caso vem
        cortado (um input pela metade estragaria o eval): o que não couber fica de fora e
        `omitted` diz quantos — filtre com since/agent_version ou baixe pela CLI."""
        cases = _call(
            client.export_cases,
            agent_type=agent_type,
            since=since,
            agent_version=agent_version,
            limit=max(1, min(limit, 500)),
        )
        kept: list[dict[str, Any]] = []
        for case in cases:
            if kept and _size(kept + [case]) > MAX_RESPONSE:
                break
            kept.append(case)
        report: dict[str, Any] = {"agent_type": agent_type, "cases": kept, "count": len(kept)}
        if len(kept) < len(cases):
            report["omitted"] = len(cases) - len(kept)
            report["hint"] = f"use since/agent_version, ou `kuro runs export -a {agent_type} -o casos.jsonl`"
        return report

    # -- feedback por trecho recuperado (RAG) ---------------------------------

    @server.tool(annotations=OVERWRITE)
    def run_chunk_feedback(
        run_id: RunId,
        chunk_id: Annotated[
            str, Field(description="chunk_id do trecho, em run_show: spans search_knowledge_base, metadata.references.results.")
        ],
        label: Annotated[
            Literal["relevant", "irrelevant", "wrong"],
            Field(description="relevant (era o trecho certo), irrelevant (não respondia) ou wrong (conteúdo errado/desatualizado)."),
        ],
        comment: Annotated[str | None, Field(description="Por que essa marcação.")] = None,
        span_id: Annotated[str | None, Field(description="A span da busca, se o trecho voltou em mais de uma.")] = None,
    ) -> dict[str, Any]:
        """Marca um trecho que a busca de um run devolveu. Substitui a marcação anterior do
        mesmo trecho. Só registra: nenhum documento é escondido ou despublicado."""
        body = {
            "run_id": run_id,
            "chunk_id": chunk_id,
            "label": label,
            "comment": comment,
            "span_id": span_id,
            "created_by": "mcp",
        }
        return _call(client.chunk_feedback, body)

    @server.tool(annotations=READ)
    def chunk_feedback_flagged(
        collection: Annotated[str | None, Field(description="Só desta collection. Sem ela, todas.")] = None,
        label: Annotated[Literal["relevant", "irrelevant", "wrong"], Field(description="Qual marcação contar.")] = "wrong",
    ) -> dict[str, Any]:
        """Documentos com mais marcações (padrão: wrong, errado/desatualizado), com os runs
        que as geraram — para alguém revisar o documento na fonte."""
        return {"documents": _call(client.flagged_documents, collection=collection, label=label, limit=20)}

    @server.tool(annotations=READ)
    def chunk_feedback_export(
        collection: Annotated[str | None, Field(description="Só desta collection.")] = None,
        agent_type: AgentFilter = None,
        since: Since = None,
    ) -> dict[str, Any]:
        """Gabarito de um eval de RAG a partir das marcações relevant: uma linha por pergunta
        com query, collection, relevant_chunk_ids, relevant_content_ids e run_ids — o mesmo
        JSONL de `kuro runs chunk-export`. O que não couber fica de fora (`omitted`)."""
        cases = _call(client.export_chunk_feedback, collection=collection, agent_type=agent_type, since=since)
        kept: list[dict[str, Any]] = []
        for case in cases:
            if kept and _size(kept + [case]) > MAX_RESPONSE:
                break
            kept.append(case)
        report: dict[str, Any] = {"cases": kept, "count": len(kept)}
        if len(kept) < len(cases):
            report["omitted"] = len(cases) - len(kept)
            report["hint"] = "filtre por collection/since, ou `kuro runs chunk-export -o gabarito.jsonl`"
        return report

    # -- conversas guardadas --------------------------------------------------

    @server.tool(annotations=READ)
    def sessions_list(
        agent_type: AgentFilter = None,
        user_id: UserId = None,
        limit: Annotated[int, Field(description="Quantas conversas por página, das mais recentes (1 a 100).")] = 20,
        page: Annotated[int, Field(description="A página (1, 2, ...). `meta.total_pages` diz quantas há.")] = 1,
    ) -> dict[str, Any]:
        """Conversas guardadas (a transcrição fica em session_show). Sem `user_id`, as de
        todos: as do chat daqui (`mcp`), da CLI (`cli`) e das integrações. `meta` traz a
        paginação (page, total_pages, total_count)."""
        result = _call(
            client.list_sessions,
            user_id=user_id,
            component_id=agent_type,
            limit=max(1, min(limit, 100)),
            page=max(1, page),
        )
        return _fit(_clip(result, 300), paginated=True)

    @server.tool(annotations=READ)
    def session_show(session_id: SessionId, user_id: UserId = None) -> dict[str, Any]:
        """Transcrição de uma conversa: cada mensagem, a resposta e os tokens."""
        return _fit({"items": _call(client.session_runs, session_id, user_id)})

    def _confirm_delete_session(session_id: str, ctx: Context) -> Elicit[Confirmacao]:
        return _ask(ctx, f"kuro sessions delete {session_id} --yes", f"Apagar a conversa {session_id!r}? Não tem volta (o registro em runs continua).")

    @server.tool(annotations=DESTRUCTIVE)
    def session_delete(
        session_id: SessionId, ok: Annotated[Confirmacao, Resolve(_confirm_delete_session)], user_id: UserId = None
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
        return _fit({"items": _call(client.list_collections)})

    @server.tool(annotations=READ)
    def collection_get(name: CollectionName) -> dict[str, Any]:
        """Uma collection: rótulo, descrição, embedder (provedor, modelo, dimensões) e os
        agentes que a usam em knowledge_collection."""
        return _fit(_call(client.get_collection, name))

    @server.tool(annotations=READ)
    def collection_embedders() -> dict[str, Any]:
        """Provedores de embedding que collection_create aceita, o modelo padrão de cada
        um e se há credencial para ele (ollama não precisa de chave)."""
        return {"items": _call(client.list_embedders)}

    @server.tool(annotations=WRITE)
    def collection_create(
        name: Annotated[str, Field(description="Slug estável da collection: minúsculas, números, - ou _, ex.: manuais.")],
        label: Annotated[str, Field(description='O nome que aparece no console, ex.: "Manuais do produto".')],
        description: Annotated[str | None, Field(description="Para que serve a base (até 1000 caracteres).")] = None,
        embedder: Annotated[
            str | None,
            Field(
                description="Quem gera os vetores: google (padrão), openai ou ollama — veja collection_embedders. "
                "Definitivo: para trocar depois, só criando outra collection e reindexando."
            ),
        ] = None,
        embedder_model: Annotated[
            str | None, Field(description="Modelo de embedding fora do padrão do provedor (exige embedder_dimensions).")
        ] = None,
        embedder_dimensions: Annotated[
            int | None, Field(description="O tamanho do vetor que embedder_model devolve, ex.: 768.")
        ] = None,
        search_mode: Annotated[
            Literal["vector", "hybrid"] | None,
            Field(description="vector (padrão): só similaridade; hybrid: vetor + full-text do Postgres (bom para "
                  "códigos, siglas e nomes exatos)."),
        ] = None,
        language: Annotated[
            str | None,
            Field(description="Dicionário do full-text do Postgres, ex.: portuguese (padrão), english, simple. "
                  "Definitivo, como o embedder."),
        ] = None,
        max_results: Annotated[int | None, Field(description="Trechos por busca, o k (1 a 50; padrão 10).")] = None,
        vector_weight: Annotated[
            float | None, Field(description="No hybrid, o peso do vetor de 0 a 1 (o resto é do texto; padrão 0.5).")
        ] = None,
    ) -> dict[str, Any]:
        """Cria uma base de conhecimento vazia. O embedder e o idioma são escolhidos aqui e NÃO
        mudam depois (a tabela de vetores é de um embedder só). Modo, k e peso mudam com
        collection_set. Depois: collection_add ou collection_add_site para indexar, e
        agent_set(knowledge_collection=name) no agente."""
        payload = {
            "name": name,
            "label": label,
            "description": description,
            "embedder_provider": embedder,
            "embedder_model": embedder_model,
            "embedder_dimensions": embedder_dimensions,
            "search_mode": search_mode,
            "language": language,
            "max_results": max_results,
            "vector_weight": vector_weight,
        }
        return _call(client.create_collection, {k: v for k, v in payload.items() if v is not None})

    @server.tool(annotations=OVERWRITE)
    def collection_set(
        name: CollectionName,
        changes: Annotated[
            dict[str, Any],
            Field(
                description="label, description e a busca: search_mode (vector|hybrid), max_results (k), "
                'vector_weight (0–1), reranker. Ex.: {"search_mode": "hybrid", "vector_weight": 0.7}; null volta ao padrão.'
            ),
        ],
    ) -> dict[str, Any]:
        """Altera o rótulo, a descrição ou a configuração de busca de uma collection. Mudar a
        busca gera uma versão nova (collection_versions), que os runs gravam. O embedder e o
        idioma não mudam: para outros, crie uma collection nova e reindexe."""
        if {"embedder", "embedder_provider", "embedder_model", "embedder_dimensions", "language"} & set(changes):
            raise ToolError("o embedder e o idioma de uma collection não mudam: crie outra (collection_create) e reindexe")
        _fields_only(changes, COLLECTION_FIELDS)
        return _call(client.update_collection, name, changes)

    @server.tool(annotations=READ)
    def collection_versions(
        name: CollectionName,
        version: Annotated[int | None, Field(description="Uma versão: devolve só ela, com a configuração completa.")] = None,
    ) -> dict[str, Any]:
        """Histórico da configuração de busca da collection (embedder, modo, idioma, k, peso),
        mais recente primeiro. Cada run grava `collection_version`: um agente na mesma versão
        que passou a responder diferente pode ter mudado aqui. Filtre com
        runs_list(knowledge_collection=..., collection_version=...)."""
        if version is not None:
            return _call(client.collection_version, name, version)
        return _fit({"items": _call(client.collection_versions, name)})

    @server.tool(annotations=READ)
    def collection_docs(name: CollectionName) -> dict[str, Any]:
        """Documentos indexados numa collection, com o status."""
        return _fit(_clip(_call(client.list_collection_documents, name), 300))

    @server.tool(annotations=READ)
    def collection_search(
        name: CollectionName,
        query: Annotated[str, Field(description="O que buscar, em linguagem natural, ex.: prazo de garantia.")],
        limit: Annotated[int | None, Field(description="Quantos trechos (padrão: o k da collection).")] = None,
    ) -> dict[str, Any]:
        """Busca na collection — o mesmo que o agente enxerga (modo, k e idioma dela). Cada
        trecho traz `score_kind`: similarity (vetorial), hybrid (vetor + texto) ou rerank."""
        return {"items": _clip(_call(client.search_collection, name, query, limit), 1500)}

    @server.tool(annotations=WRITE)
    def collection_add(
        name: CollectionName,
        text: Annotated[str | None, Field(description="O conteúdo a indexar.")] = None,
        file: Annotated[
            str | None, Field(description="Caminho de um arquivo local. Recusado quando o MCP roda no servidor: use text.")
        ] = None,
        title: Annotated[str | None, Field(description="O título do documento na base.")] = None,
    ) -> dict[str, Any]:
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
        name: CollectionName,
        content_id: Annotated[str, Field(description="O documento (content_id), como collection_docs devolve.")],
        ok: Annotated[Confirmacao, Resolve(_confirm_rm_doc)]
    ) -> dict[str, Any]:
        """Tira um documento da collection (pede confirmação)."""
        if not ok.confirmar:
            return {"cancelled": True}
        _call(client.delete_collection_document, name, content_id)
        return {"deleted": content_id}

    @server.tool(annotations=CRAWL)
    def collection_add_site(
        name: CollectionName,
        url: Annotated[str, Field(description="A URL de partida, ex.: https://docs.exemplo.com/guia.")],
        mode: Annotated[
            Literal["crawl", "sitemap", "page"],
            Field(description="crawl: segue links abaixo do caminho; sitemap: o sitemap do site; page: só a URL."),
        ] = "crawl",
        max_pages: Annotated[int | None, Field(description="Teto de páginas coletadas.")] = None,
        max_depth: Annotated[int | None, Field(description="Teto de cliques a partir da URL (só crawl).")] = None,
        include: Annotated[
            list[str] | None, Field(description='Só URLs com estes trechos de caminho, ex.: ["/guia/"].')
        ] = None,
        exclude: Annotated[list[str] | None, Field(description="Pula URLs com estes trechos de caminho.")] = None,
        format: Annotated[bool, Field(description="Um agente organiza o texto de cada página (sem reescrever).")] = True,
        refresh: Annotated[
            Literal["never", "daily", "weekly", "monthly"], Field(description="Ressincronização automática.")
        ] = "never",
    ) -> dict[str, Any]:
        """Indexa um site na collection, em background: `crawl` segue links abaixo do caminho
        da URL, `sitemap` usa o sitemap, `page` só a URL. Cada página passa por um agente que
        organiza o texto sem reescrever (`format=false` pula). Acompanhe com `collection_sources`."""
        payload = {"url": url, "mode": mode, "max_pages": max_pages, "max_depth": max_depth,
                   "include": include, "exclude": exclude, "format": format, "refresh": refresh}
        return _call(client.create_source, name, {k: v for k, v in payload.items() if v is not None})

    @server.tool(annotations=READ)
    def collection_sources(
        name: CollectionName,
        source_id: Annotated[str | None, Field(description="Um site só, com as páginas dele.")] = None,
    ) -> dict[str, Any]:
        """Sites da collection e o estado da sincronização; com `source_id`, as páginas
        dele (status, trechos, se o formatador foi usado e o motivo de cada falha)."""
        if source_id:
            return _clip(_call(client.get_source, name, source_id), 300)
        return _fit({"items": _call(client.list_sources, name)})

    @server.tool(annotations=RECRAWL)
    def collection_sync_site(name: CollectionName, source_id: SourceId) -> dict[str, Any]:
        """Coleta o site de novo agora (só reprocessa as páginas que mudaram)."""
        return _call(client.sync_source, name, source_id)

    def _confirm_rm_site(name: str, source_id: str, ctx: Context) -> Elicit[Confirmacao]:
        return _ask(ctx, f"kuro collections rm-site {name} {source_id} --yes", f"Remover o site {source_id!r} e as páginas dele da collection {name!r}?")

    @server.tool(annotations=DESTRUCTIVE)
    def collection_rm_site(
        name: CollectionName, source_id: SourceId, ok: Annotated[Confirmacao, Resolve(_confirm_rm_site)]
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
