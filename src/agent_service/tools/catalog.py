"""Catálogo de toolkits padrão do Agno oferecidas para tools `kind="builtin"`.

Curado, não o pacote `agno.tools.*` inteiro: cada entrada aqui é uma toolkit
útil para um agente de negócio que roda sem infraestrutura extra (ou só com uma
chave/credencial que o próprio usuário informa ao criar a tool pela API/UI —
nunca um nome de classe/módulo arbitrário vindo da API, para não virar um jeito
de instanciar qualquer coisa do Agno com kwargs livres). O ponto de partida de
um agente continua sendo as tools `kind="api"`/`"python"` do próprio negócio; o
catálogo cobre o que se repete entre empresas (busca, Slack, Jira...), e por
isso fica enxuto.

Para adicionar uma entrada: importe a toolkit dentro da própria `factory`
(import tardio — o pacote vem do extra `tools` do `pyproject.toml`, e quem não o
instalou vê a entrada como indisponível), descreva os parâmetros em `params`,
liste em `requires` os módulos que ela importa e, se ela muda algo fora do
serviço, as funções em `writes`.

**Chaves nunca caem no ambiente.** Quase toda toolkit do Agno faz
`api_key or getenv("X_API_KEY")`: sem chave na config, a tool gastaria a chave
que o container tivesse. Por isso toda credencial aqui é `required`.

**Efeito colateral e `dry_run`.** As funções em `writes` (enviar mensagem, abrir
chamado, agendar) não rodam num `dry_run`: devolvem ao modelo o que teriam
feito. É o mesmo contrato das tools `kind="api"`, que recebem o header e
decidem — só que aqui quem decide é o serviço, porque a toolkit não sabe do
header. `read_only` tira essas funções da toolkit de vez; `never` são funções que
nenhum agente deveria ter (apagar repositório).

**O que fica de fora de propósito.**
- Toolkits que buscam uma URL na hora com cliente HTTP próprio — `CustomApiTools`,
  `WebsiteTools`, `WebTools`, Crawl4ai, Newspaper, Playwright. Elas não passariam
  por `tools/egress.py`, então seriam o caminho mais curto para um agente
  alcançar `http://postgres:5432` ou o metadata da nuvem. Ler uma página passa
  pelo Jina Reader, que busca do lado de lá; chamar uma API é o que as tools
  `kind="api"` fazem, com destino checado a cada chamada. Os hosts configuráveis
  (GitHub Enterprise, GitLab, Jira, Confluence) são conferidos ao salvar e ao
  construir a toolkit.
- Execução de código, shell, SQL e arquivos locais (Shell, Python, Docker, Pandas,
  DuckDB, Postgres): é o que `kind="python"` faz, desligado por padrão.
- Login OAuth com arquivo de token (Gmail, Google Calendar/Sheets/Drive) e nuvem
  com credencial do ambiente (AWS): não cabem numa chave colada no formulário.
- Geração de imagem, áudio e vídeo (DALL-E, ElevenLabs, Replicate, Fal): o
  `/chat` devolve texto, então a mídia se perderia.
- Redes sociais, gerenciadores de tarefa pessoais e buscadores redundantes
  (Reddit, X, Discord, Trello, Todoist, ClickUp, Exa, SerpApi, Brave...): pouco
  uso num agente de atendimento, e cada entrada a mais é ruído na hora de escolher.
  Brave, além disso, exige um httpx antigo, incompatível com o serviço.
"""

import functools
import importlib.util
import inspect
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from agent_service.tools.context import is_dry_run
from agent_service.tools.egress import EgressBlockedError, check_url

ParamType = Literal["string", "integer", "boolean"]

CATEGORIES = ("Pesquisa", "Dados", "Trabalho em equipe", "Comunicação", "Utilidades")
"""Ordem em que o catálogo é apresentado."""

INSTALL_HINT = 'KURO_EXTRAS="--extra tools" docker compose build agent-service'


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: ParamType
    label: str
    description: str = ""
    required: bool = False
    secret: bool = False
    """Mascarado nas respostas da API (ex.: senha de app do e-mail)."""
    default: Any = None
    choices: tuple[str, ...] = ()
    """Valores aceitos (string). Vazio = livre."""
    url: bool = False
    """Endereço que a toolkit vai chamar: precisa ser público (`tools/egress.py`)."""


@dataclass(frozen=True)
class BuiltinToolSpec:
    builtin_id: str
    label: str
    description: str
    category: str = "Utilidades"
    params: list[ParamSpec] = field(default_factory=list)
    factory: Callable[[dict[str, Any], str], Any] = field(repr=False, default=lambda params, tool_name: None)
    """`(params_validados, tool_name) -> Toolkit` do Agno. Import tardio dentro da função."""
    requires: tuple[str, ...] = ()
    """Módulos que a toolkit importa — sem eles, a entrada aparece indisponível."""
    writes: frozenset[str] = frozenset()
    """Funções com efeito fora do serviço: simuladas em `dry_run`."""
    never: frozenset[str] = frozenset()
    """Funções que a toolkit tem, mas que nenhum agente recebe."""
    read_only_default: bool | None = None
    """Não-`None` acrescenta o parâmetro `read_only`, com este padrão."""

    @property
    def all_params(self) -> list[ParamSpec]:
        if self.read_only_default is None:
            return list(self.params)
        return [
            *self.params,
            ParamSpec(
                "read_only", "boolean", "Somente leitura",
                description="Sem as funções que criam, alteram ou enviam: "
                + ", ".join(sorted(self.writes)) + ".",
                default=self.read_only_default,
            ),
        ]

    @property
    def side_effect(self) -> bool:
        return bool(self.writes)


def _tools_base_dir(tool_name: str) -> Path:
    """Diretório isolado por tool para toolkits que leem/escrevem arquivo —
    nunca o código-fonte do serviço nem um caminho escolhido pelo usuário.

    O isolamento é contra travessia de caminho, **não** entre conversas: a chave
    é o nome da tool, então duas sessões que usam a mesma tool compartilham o
    diretório e enxergam os arquivos uma da outra. Separar por sessão exigiria
    construir a toolkit por run, e hoje ela é cacheada por tool
    (`tools/registry.py`). Até lá, isso precisa estar na cara de quem liga a
    tool — está na `description` das entradas que usam este diretório."""
    base = Path(tempfile.gettempdir()) / "agent-service-tools" / tool_name
    base.mkdir(parents=True, exist_ok=True)
    return base


def _kwargs(params: dict[str, Any], *names: str) -> dict[str, Any]:
    """Só os parâmetros preenchidos — o resto fica com o padrão da toolkit."""
    return {n: params[n] for n in names if params.get(n) not in (None, "")}


def _web_search(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.duckduckgo import DuckDuckGoTools

    return DuckDuckGoTools(
        enable_news=params.get("enable_news", True),
        fixed_max_results=params.get("max_results") or None,
        region=params.get("region") or None,
    )


def _calculator(_params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.calculator import CalculatorTools

    return CalculatorTools()


def _hackernews(_params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.hackernews import HackerNewsTools

    return HackerNewsTools()


def _reasoning(_params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.reasoning import ReasoningTools

    return ReasoningTools(add_instructions=True)


def _email(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.email import EmailTools

    return EmailTools(
        receiver_email=params.get("receiver_email") or None,
        sender_name=params.get("sender_name") or None,
        sender_email=params.get("sender_email") or None,
        sender_passkey=params.get("sender_passkey") or None,
    )


def _files(params: dict[str, Any], tool_name: str) -> Any:
    from agno.tools.file import FileTools

    return FileTools(
        base_dir=_tools_base_dir(tool_name),
        enable_delete_file=params.get("enable_delete_file", False),
    )


def _sleep(_params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.sleep import SleepTools

    return SleepTools()


def _pubmed(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.pubmed import PubmedTools

    return PubmedTools(
        email=params.get("email") or "agent-service@example.com",
        max_results=params.get("max_results") or None,
        results_expanded=params.get("results_expanded", False),
    )


def _openweather(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.openweather import OpenWeatherTools

    return OpenWeatherTools(api_key=params.get("api_key") or None, units=params.get("units") or "metric")


def _file_generation(params: dict[str, Any], tool_name: str) -> Any:
    from agno.tools.file_generation import FileGenerationTools

    # PDF e DOCX ficam desligados: o Agno só os gera com pacotes que este
    # serviço não instala, e a tool falharia na hora da chamada, não aqui.
    return FileGenerationTools(
        output_directory=str(_tools_base_dir(tool_name)),
        save_files=True,
        enable_pdf_generation=False,
        enable_docx_generation=False,
        enable_code_generation=params.get("enable_code_generation", False),
    )


def _tavily(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.tavily import TavilyTools

    return TavilyTools(**_kwargs(params, "api_key", "search_depth", "enable_extract"))


def _serper(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.serper import SerperTools

    return SerperTools(**_kwargs(params, "api_key", "location", "language", "num_results"))


def _jina_reader(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.jina import JinaReaderTools

    # base_url/search_url ficam no padrão: são o que torna a leitura remota.
    return JinaReaderTools(**_kwargs(params, "api_key", "enable_search_query", "max_content_length"))


def _wikipedia(_params: dict[str, Any], _tool_name: str) -> Any:
    import wikipedia
    from agno.tools.wikipedia import WikipediaTools

    # Estado do módulo: vale para todas as tools Wikipédia. O User-Agent padrão da
    # biblioteca é recusado com 429 pela Wikimedia, que exige um que identifique o cliente.
    wikipedia.set_lang("pt")
    wikipedia.set_user_agent("Kuro-agent-service/1.0 (agente de IA; Agno WikipediaTools)")
    return WikipediaTools()


def _youtube(_params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.youtube import YouTubeTools

    return YouTubeTools(languages=["pt", "pt-BR", "en"])


def _yfinance(_params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.yfinance import YFinanceTools

    return YFinanceTools(all=True)


def _github(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.github import GithubTools

    return GithubTools(**_kwargs(params, "access_token", "base_url"))


def _gitlab(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.gitlab import GitlabTools

    return GitlabTools(**_kwargs(params, "access_token", "base_url"))


def _jira(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.jira import JiraTools

    return JiraTools(**_kwargs(params, "server_url", "username", "token"))


def _confluence(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.confluence import ConfluenceTools

    return ConfluenceTools(**_kwargs(params, "url", "username", "api_key"))


def _notion(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.notion import NotionTools

    return NotionTools(**_kwargs(params, "api_key", "database_id"))


def _linear(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.linear import LinearTools

    return LinearTools(**_kwargs(params, "api_key"))


def _zendesk(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.zendesk import ZendeskTools

    return ZendeskTools(**_kwargs(params, "username", "password", "company_name"))


def _calcom(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.calcom import CalComTools

    return CalComTools(**_kwargs(params, "api_key", "event_type_id", "user_timezone"))


def _slack(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.slack import SlackTools

    # Arquivos ficam de fora: baixar grava no disco do container.
    return SlackTools(
        **_kwargs(params, "token"),
        enable_upload_file=False,
        enable_download_file=False,
        save_downloads=False,
    )


def _telegram(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.telegram import TelegramTools

    return TelegramTools(**_kwargs(params, "token", "chat_id"))


def _whatsapp(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.whatsapp import WhatsAppTools

    return WhatsAppTools(**_kwargs(params, "access_token", "phone_number_id", "recipient_waid"))


def _twilio(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.twilio import TwilioTools

    return TwilioTools(**_kwargs(params, "account_sid", "auth_token"))


def _resend(params: dict[str, Any], _tool_name: str) -> Any:
    from agno.tools.resend import ResendTools

    return ResendTools(**_kwargs(params, "api_key", "from_email"))


def _key(name: str, label: str, description: str = "") -> ParamSpec:
    return ParamSpec(name, "string", label, description=description, required=True, secret=True)


BUILTIN_CATALOG: dict[str, BuiltinToolSpec] = {
    spec.builtin_id: spec
    for spec in [
        # -- Pesquisa ------------------------------------------------------
        BuiltinToolSpec(
            builtin_id="web_search",
            label="Busca na web (DuckDuckGo)",
            description="Pesquisa na web e notícias, sem chave.",
            category="Pesquisa",
            params=[
                ParamSpec("max_results", "integer", "Máx. de resultados", default=None),
                ParamSpec("enable_news", "boolean", "Incluir notícias", default=True),
                ParamSpec("region", "string", "Região (ex.: br-pt)", default=None),
            ],
            factory=_web_search,
            requires=("ddgs",),
        ),
        BuiltinToolSpec(
            builtin_id="tavily",
            label="Busca na web (Tavily)",
            description="Busca feita para agentes: devolve os trechos relevantes e uma resposta resumida. "
            "Pode também ler o conteúdo de páginas. Precisa de uma chave da Tavily.",
            category="Pesquisa",
            params=[
                _key("api_key", "Chave da Tavily"),
                ParamSpec("search_depth", "string", "Profundidade", default="advanced", choices=("basic", "advanced")),
                ParamSpec("enable_extract", "boolean", "Ler o conteúdo de páginas", default=False),
            ],
            factory=_tavily,
            requires=("tavily",),
        ),
        BuiltinToolSpec(
            builtin_id="serper",
            label="Busca no Google (Serper)",
            description="Resultados do Google, notícias e Google Acadêmico, e leitura de páginas pela "
            "Serper. Precisa de uma chave da Serper.",
            category="Pesquisa",
            params=[
                _key("api_key", "Chave da Serper"),
                ParamSpec("location", "string", "País (código)", default="br"),
                ParamSpec("language", "string", "Idioma (código)", default="pt"),
                ParamSpec("num_results", "integer", "Máx. de resultados", default=10),
            ],
            factory=_serper,
        ),
        BuiltinToolSpec(
            builtin_id="jina_reader",
            label="Ler página da web (Jina Reader)",
            description="Lê uma página pública e devolve o texto limpo. A página é buscada pelos "
            "servidores da Jina, não pelos do Kuro. Precisa de uma chave da Jina.",
            category="Pesquisa",
            params=[
                _key("api_key", "Chave da Jina"),
                ParamSpec("enable_search_query", "boolean", "Também pesquisar na web", default=False),
                ParamSpec("max_content_length", "integer", "Máx. de caracteres por página", default=10000),
            ],
            factory=_jina_reader,
        ),
        BuiltinToolSpec(
            builtin_id="wikipedia",
            label="Wikipédia",
            description="Resumo de um verbete da Wikipédia em português. API pública, sem chave.",
            category="Pesquisa",
            factory=_wikipedia,
            requires=("wikipedia",),
        ),
        BuiltinToolSpec(
            builtin_id="youtube",
            label="YouTube (legendas e dados do vídeo)",
            description="Lê as legendas, os capítulos e os dados de um vídeo do YouTube a partir do link.",
            category="Pesquisa",
            factory=_youtube,
            requires=("youtube_transcript_api",),
        ),
        BuiltinToolSpec(
            builtin_id="pubmed",
            label="PubMed (artigos científicos)",
            description="Busca artigos e resumos na base do PubMed/NCBI. API pública, sem chave.",
            category="Pesquisa",
            params=[
                ParamSpec(
                    "email", "string", "E-mail de contato",
                    description="O NCBI pede um e-mail para identificar quem consulta a API.",
                ),
                ParamSpec("max_results", "integer", "Máx. de artigos por busca", default=None),
                ParamSpec("results_expanded", "boolean", "Trazer o resumo completo", default=False),
            ],
            factory=_pubmed,
        ),
        BuiltinToolSpec(
            builtin_id="hackernews",
            label="Hacker News",
            description="Lê as histórias em alta e detalhes de usuários do Hacker News (API pública).",
            category="Pesquisa",
            factory=_hackernews,
        ),
        # -- Dados -----------------------------------------------------------
        BuiltinToolSpec(
            builtin_id="yfinance",
            label="Cotações e dados de empresas (Yahoo Finance)",
            description="Preço, histórico, indicadores, fundamentos e notícias de ações. Na B3, use o "
            "sufixo .SA (ex.: PETR4.SA). Sem chave.",
            category="Dados",
            factory=_yfinance,
            requires=("yfinance",),
        ),
        BuiltinToolSpec(
            builtin_id="openweather",
            label="Clima (OpenWeather)",
            description="Clima atual, previsão e qualidade do ar. Precisa de uma chave do OpenWeatherMap.",
            category="Dados",
            params=[
                _key("api_key", "Chave do OpenWeatherMap"),
                ParamSpec("units", "string", "Unidades", default="metric", choices=("metric", "imperial", "standard")),
            ],
            factory=_openweather,
        ),
        # -- Trabalho em equipe ------------------------------------------------
        BuiltinToolSpec(
            builtin_id="jira",
            label="Jira",
            description="Consulta, busca (JQL) e abre issues, comenta e lança horas.",
            category="Trabalho em equipe",
            params=[
                ParamSpec("server_url", "string", "Endereço do Jira", required=True, url=True,
                          description="Ex.: https://suaempresa.atlassian.net"),
                ParamSpec("username", "string", "Usuário (e-mail)", required=True),
                _key("token", "Token de API"),
            ],
            factory=_jira,
            requires=("jira",),
            writes=frozenset({"create_issue", "add_comment", "add_worklog"}),
            read_only_default=False,
        ),
        BuiltinToolSpec(
            builtin_id="confluence",
            label="Confluence",
            description="Lê espaços e páginas do Confluence; pode criar e atualizar páginas.",
            category="Trabalho em equipe",
            params=[
                ParamSpec("url", "string", "Endereço do Confluence", required=True, url=True,
                          description="Ex.: https://suaempresa.atlassian.net/wiki"),
                ParamSpec("username", "string", "Usuário (e-mail)", required=True),
                _key("api_key", "Token de API"),
            ],
            factory=_confluence,
            requires=("atlassian",),
            writes=frozenset({"create_page", "update_page"}),
            read_only_default=True,
        ),
        BuiltinToolSpec(
            builtin_id="notion",
            label="Notion",
            description="Busca, cria e atualiza páginas de um banco de dados do Notion.",
            category="Trabalho em equipe",
            params=[
                _key("api_key", "Token da integração"),
                ParamSpec("database_id", "string", "ID do banco de dados", required=True),
            ],
            factory=_notion,
            requires=("notion_client",),
            writes=frozenset({"create_page", "update_page"}),
            read_only_default=False,
        ),
        BuiltinToolSpec(
            builtin_id="linear",
            label="Linear",
            description="Consulta times e issues do Linear; pode abrir e atualizar issues.",
            category="Trabalho em equipe",
            params=[_key("api_key", "Chave de API do Linear")],
            factory=_linear,
            writes=frozenset({"create_issue", "update_issue"}),
            read_only_default=False,
        ),
        BuiltinToolSpec(
            builtin_id="zendesk",
            label="Zendesk (central de ajuda)",
            description="Pesquisa os artigos da central de ajuda do Zendesk. Só leitura.",
            category="Trabalho em equipe",
            params=[
                ParamSpec("company_name", "string", "Subdomínio", required=True,
                          description="O que vem antes de .zendesk.com"),
                ParamSpec("username", "string", "Usuário (e-mail)", required=True),
                ParamSpec("password", "string", "Senha ou token", required=True, secret=True),
            ],
            factory=_zendesk,
        ),
        BuiltinToolSpec(
            builtin_id="calcom",
            label="Agenda (Cal.com)",
            description="Consulta horários livres e agenda, remarca ou cancela reuniões de um tipo de evento.",
            category="Trabalho em equipe",
            params=[
                _key("api_key", "Chave do Cal.com"),
                ParamSpec("event_type_id", "integer", "ID do tipo de evento", required=True),
                ParamSpec("user_timezone", "string", "Fuso horário", default="America/Sao_Paulo"),
            ],
            factory=_calcom,
            requires=("pytz", "requests"),
            writes=frozenset({"create_booking", "reschedule_booking", "cancel_booking"}),
            read_only_default=False,
        ),
        BuiltinToolSpec(
            builtin_id="github",
            label="GitHub",
            description="Repositórios, issues, pull requests e código. Somente leitura por padrão; "
            "apagar repositório, arquivo ou mudar a branch padrão nunca fica disponível.",
            category="Trabalho em equipe",
            params=[
                _key("access_token", "Token de acesso"),
                ParamSpec("base_url", "string", "Endereço do GitHub Enterprise", url=True,
                          description="Vazio = github.com"),
            ],
            factory=_github,
            requires=("github",),
            writes=frozenset({
                "create_issue", "comment_on_issue", "close_issue", "reopen_issue", "assign_issue",
                "label_issue", "edit_issue", "create_pull_request_comment", "edit_pull_request_comment",
                "create_pull_request", "create_review_request", "create_file", "update_file", "create_branch",
            }),
            never=frozenset({"create_repository", "delete_repository", "delete_file", "set_default_branch"}),
            read_only_default=True,
        ),
        BuiltinToolSpec(
            builtin_id="gitlab",
            label="GitLab",
            description="Projetos, merge requests e issues do GitLab. Só leitura.",
            category="Trabalho em equipe",
            params=[
                _key("access_token", "Token de acesso"),
                ParamSpec("base_url", "string", "Endereço do GitLab", url=True, description="Vazio = gitlab.com"),
            ],
            factory=_gitlab,
            requires=("gitlab", "httpx"),
        ),
        # -- Comunicação -------------------------------------------------------
        BuiltinToolSpec(
            builtin_id="slack",
            label="Slack",
            description="Envia mensagens, lista canais e lê o histórico de um canal.",
            category="Comunicação",
            params=[_key("token", "Token do bot (xoxb-...)")],
            factory=_slack,
            requires=("slack_sdk",),
            writes=frozenset({"send_message", "send_message_thread"}),
            read_only_default=False,
        ),
        BuiltinToolSpec(
            builtin_id="whatsapp",
            label="WhatsApp (Cloud API)",
            description="Envia mensagens de texto e de modelo (template) pela API oficial da Meta.",
            category="Comunicação",
            params=[
                _key("access_token", "Token de acesso"),
                ParamSpec("phone_number_id", "string", "ID do número de envio", required=True),
                ParamSpec("recipient_waid", "string", "Destinatário padrão", description="Número com DDI, ex.: 5511999999999"),
            ],
            factory=_whatsapp,
            writes=frozenset({"send_text_message", "send_template_message"}),
        ),
        BuiltinToolSpec(
            builtin_id="telegram",
            label="Telegram",
            description="Envia mensagens de texto para um chat do Telegram por um bot.",
            category="Comunicação",
            params=[
                _key("token", "Token do bot"),
                ParamSpec("chat_id", "string", "ID do chat", required=True),
            ],
            factory=_telegram,
            requires=("telebot",),
            writes=frozenset({"send_message"}),
        ),
        BuiltinToolSpec(
            builtin_id="twilio",
            label="SMS (Twilio)",
            description="Envia SMS, consulta mensagens enviadas e valida números de telefone.",
            category="Comunicação",
            params=[
                ParamSpec("account_sid", "string", "Account SID", required=True),
                _key("auth_token", "Auth token"),
            ],
            factory=_twilio,
            requires=("twilio",),
            writes=frozenset({"send_sms"}),
        ),
        BuiltinToolSpec(
            builtin_id="resend",
            label="Enviar e-mail (Resend)",
            description="Envia e-mails transacionais pela Resend, a partir de um domínio verificado.",
            category="Comunicação",
            params=[
                _key("api_key", "Chave da Resend"),
                ParamSpec("from_email", "string", "Remetente", required=True, description="Ex.: suporte@suaempresa.com"),
            ],
            factory=_resend,
            requires=("resend",),
            writes=frozenset({"send_email"}),
        ),
        BuiltinToolSpec(
            builtin_id="email",
            label="Enviar e-mail (Gmail SMTP)",
            description="Envia e-mails via Gmail SMTP para um destinatário fixo. Configure com uma senha de app.",
            category="Comunicação",
            params=[
                ParamSpec("receiver_email", "string", "E-mail de destino", required=True),
                ParamSpec("sender_name", "string", "Nome do remetente"),
                ParamSpec("sender_email", "string", "E-mail remetente (Gmail)", required=True),
                ParamSpec("sender_passkey", "string", "Senha de app do remetente", required=True, secret=True),
            ],
            factory=_email,
            writes=frozenset({"email_user"}),
        ),
        # -- Utilidades --------------------------------------------------------
        BuiltinToolSpec(
            builtin_id="calculator",
            label="Calculadora",
            description="Operações aritméticas exatas (soma, potência, fatorial, primalidade...).",
            factory=_calculator,
        ),
        BuiltinToolSpec(
            builtin_id="reasoning",
            label="Raciocínio estruturado",
            description="Dá ao agente as funções think/analyze do Agno para planejar antes de responder.",
            factory=_reasoning,
        ),
        BuiltinToolSpec(
            builtin_id="files",
            label="Arquivos (sandbox)",
            description="Lê, lista e grava arquivos num diretório isolado por tool no próprio container "
            "(nunca o código do serviço nem um caminho escolhido pelo usuário). ATENÇÃO: o diretório é "
            "por tool, não por conversa — o que uma conversa grava fica visível para qualquer outra "
            "que use esta tool.",
            params=[ParamSpec("enable_delete_file", "boolean", "Permitir excluir arquivos", default=False)],
            factory=_files,
        ),
        BuiltinToolSpec(
            builtin_id="file_generation",
            label="Gerar arquivos (JSON, CSV, TXT, HTML)",
            description="Deixa o agente montar um arquivo com o resultado do trabalho. ATENÇÃO: o "
            "diretório é por tool, não por conversa — o que uma conversa gera fica visível para "
            "qualquer outra que use esta tool (ou a tool 'files' com o mesmo nome). Não use para "
            "conteúdo de um cliente específico. PDF e DOCX ficam de fora: exigem pacotes que o "
            "serviço não instala.",
            params=[
                ParamSpec(
                    "enable_code_generation", "boolean", "Permitir gerar arquivos de código", default=False
                )
            ],
            factory=_file_generation,
        ),
        BuiltinToolSpec(
            builtin_id="sleep",
            label="Aguardar (debug)",
            description="Pausa a execução por N segundos — só para testar timeouts/streaming.",
            factory=_sleep,
        ),
    ]
}


def list_builtin_catalog() -> list[BuiltinToolSpec]:
    """Na ordem de apresentação: por categoria, e dentro dela na ordem do catálogo."""
    return sorted(BUILTIN_CATALOG.values(), key=lambda s: CATEGORIES.index(s.category))


def get_builtin_spec(builtin_id: str) -> BuiltinToolSpec | None:
    return BUILTIN_CATALOG.get(builtin_id)


@functools.cache
def is_available(builtin_id: str) -> bool:
    """Os pacotes da toolkit estão instalados? Procura os módulos sem importá-los
    (yfinance traz o pandas inteiro). O resultado não muda sem reiniciar o serviço."""
    spec = BUILTIN_CATALOG[builtin_id]
    return all(importlib.util.find_spec(module) is not None for module in spec.requires)


def unavailable_reason(spec: BuiltinToolSpec) -> str:
    return (
        f"A toolkit {spec.label!r} precisa de pacotes que este serviço não tem instalados. "
        f"Reconstrua a imagem com o extra `tools`: {INSTALL_HINT}"
    )


class BuiltinConfigError(ValueError):
    pass


_TYPE_CHECK: dict[ParamType, type] = {"string": str, "integer": int, "boolean": bool}


def _check_public_url(name: str, value: str) -> None:
    if not value.startswith(("https://", "http://")):
        raise BuiltinConfigError(f"{name} deve ser um endereço http(s)://")
    try:
        check_url(value)
    except EgressBlockedError as exc:
        raise BuiltinConfigError(f"{name}: {exc}") from exc


def validate_builtin_config(config: dict[str, Any], *, check_available: bool = False) -> dict[str, Any]:
    """Confere `builtin_id` e valida/normaliza `params` contra `ParamSpec.params`
    do catálogo — tipo, obrigatoriedade, valores aceitos, nomes desconhecidos e
    endereços (que precisam ser públicos).

    `check_available` recusa uma toolkit cujos pacotes não estão instalados: vale
    ao criar a tool, não ao editar uma que já existe (desligar uma tool não pode
    depender de reinstalar o pacote dela)."""
    builtin_id = config.get("builtin_id")
    spec = get_builtin_spec(builtin_id) if isinstance(builtin_id, str) else None
    if spec is None:
        raise BuiltinConfigError(f"builtin_id desconhecido: {builtin_id!r}")
    if check_available and not is_available(spec.builtin_id):
        raise BuiltinConfigError(unavailable_reason(spec))

    raw_params = config.get("params") or {}
    if not isinstance(raw_params, dict):
        raise BuiltinConfigError("params deve ser um objeto")

    all_params = spec.all_params
    known = {p.name: p for p in all_params}
    unknown = set(raw_params) - set(known)
    if unknown:
        raise BuiltinConfigError(f"parâmetros desconhecidos para {builtin_id!r}: {sorted(unknown)}")

    normalized: dict[str, Any] = {}
    for p in all_params:
        value = raw_params.get(p.name)
        if isinstance(value, str) and p.type == "string":
            value = value.strip()
        if value is not None and value != "":
            expected = _TYPE_CHECK[p.type]
            if expected is bool and not isinstance(value, bool):
                raise BuiltinConfigError(f"{p.name} deve ser booleano")
            if expected is int and not (isinstance(value, int) and not isinstance(value, bool)):
                raise BuiltinConfigError(f"{p.name} deve ser inteiro")
            if expected is str and not isinstance(value, str):
                raise BuiltinConfigError(f"{p.name} deve ser texto")
            if p.choices and value not in p.choices:
                raise BuiltinConfigError(f"{p.name} deve ser um de {list(p.choices)}")
            if p.url:
                _check_public_url(p.name, value)
            normalized[p.name] = value
        elif p.required:
            raise BuiltinConfigError(f"{p.name} é obrigatório para {builtin_id!r}")
        elif p.default is not None:
            normalized[p.name] = p.default

    return {"builtin_id": builtin_id, "params": normalized}


def _dry_run_guard(name: str, entrypoint: Callable[..., Any]) -> Callable[..., Any]:
    """Num `dry_run`, a função não roda: o modelo recebe o que ela faria."""

    def simulated(kwargs: dict[str, Any]) -> str:
        args = json.dumps(kwargs, ensure_ascii=False, default=str)
        # Dirigido ao modelo: sem o pedido explícito, ele respondia "issue criada com sucesso".
        return (
            f"[dry_run] {name} não foi executada: é uma simulação, nada foi criado, alterado ou "
            f"enviado. Na resposta, diga que foi só um teste e que nada foi feito de verdade. "
            f"Argumentos que seriam usados: {args}"
        )

    if inspect.iscoroutinefunction(entrypoint):

        @functools.wraps(entrypoint)
        async def guarded_async(*args: Any, **kwargs: Any) -> Any:
            if is_dry_run():
                return simulated(kwargs)
            return await entrypoint(*args, **kwargs)

        return guarded_async

    @functools.wraps(entrypoint)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        if is_dry_run():
            return simulated(kwargs)
        return entrypoint(*args, **kwargs)

    return guarded


def build_builtin(spec: BuiltinToolSpec, params: dict[str, Any], tool_name: str) -> Any:
    """Constrói a toolkit e aplica a política do catálogo: tira `never` (e
    `writes`, se `read_only`) e põe a trava de `dry_run` nas escritas que sobram."""
    if not is_available(spec.builtin_id):
        raise RuntimeError(unavailable_reason(spec))
    for p in spec.params:
        # O DNS pode ter mudado desde que a tool foi salva.
        if p.url and params.get(p.name):
            try:
                check_url(params[p.name])
            except EgressBlockedError as exc:
                raise RuntimeError(f"{p.name}: {exc}") from exc

    toolkit = spec.factory(params, tool_name)
    removed = spec.never | (spec.writes if params.get("read_only") else frozenset())
    for registry in (getattr(toolkit, "functions", None), getattr(toolkit, "async_functions", None)):
        if not registry:
            continue
        for name in removed:
            registry.pop(name, None)
        for name, function in registry.items():
            if name in spec.writes and function.entrypoint is not None:
                function.entrypoint = _dry_run_guard(name, function.entrypoint)
    return toolkit
