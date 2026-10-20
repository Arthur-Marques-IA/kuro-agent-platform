"""Configurações centrais do microserviço, carregadas de variáveis de ambiente."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Servidor
    app_name: str = "agent-service"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    # Banco de dados (memória comum, sessões, tracing, knowledge)
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/agent_service"


    # Provedor de modelo padrão
    default_model_provider: str = "google"
    default_model_id: str = "gemini-2.5-flash"
    google_api_key: str | None = None
    """Fallback do provedor `google` quando não há chave salva em `model_providers`
    (ver `models/store.py`) — mantém o comportamento anterior à UI de chaves."""

    # Autenticação da API (ver agent_service/api/auth.py)
    admin_api_key: str | None = None
    """Chave com acesso total: CRUD de agentes/tools/credenciais e leitura de
    traces e sessões. É a do console e da CLI (`KURO_API_KEY`). Aceita uma lista
    separada por vírgula para rotacionar (`nova,antiga`); ver `split_keys`."""
    runtime_api_key: str | None = None
    """Chave que só executa: `/chat`, `/chat/stream`, `/analyze` e scores. É a que
    vai para os outros módulos da plataforma — se vazar, não lê conversa alheia
    nem troca prompt. Também aceita lista (`nova,antiga`)."""

    cors_origins: str | None = None
    """Sites (no navegador) que podem chamar a API, separados por vírgula. Vazio = a lista
    padrão do AgentOS (localhost:3000 e os domínios do Agno); `none` = nenhum. Ver
    `api/cors.py`. O console não precisa disto: ele chama a API pelo servidor."""

    # Criptografia das chaves de provedor de modelo salvas pela UI (`/model-providers`)
    credentials_encryption_key: str | None = None
    """Chave Fernet (`Fernet.generate_key()`) usada para cifrar em repouso as chaves de
    API que o admin cadastra pelo console. Sem ela, `models/store.py` recusa salvar ou
    ler chaves — nunca caem para texto plano."""

    # Mem0 (camada de memória semântica opcional)
    mem0_api_key: str | None = None
    mem0_enabled: bool = False

    # Onde `/observability/*` e `kuro runs` leem as execuções: "db" (tabelas
    # `runs`/`run_spans`/`run_scores` no Postgres do serviço, sempre gravadas) ou
    # "langfuse" (a API do Langfuse — exige o Langfuse ligado).
    trace_store_backend: Literal["db", "langfuse"] = "db"
    run_timeout_seconds: int = 90
    """Tempo limite padrão de uma execução (o agente pode ter o seu,
    `timeout_seconds`). Estourou: 504, e quem chama cai no próprio fallback."""
    max_concurrent_runs: int = 16
    """Execuções simultâneas por processo. Acima disso a chamada espera na fila
    (`queue_max_size`/`queue_max_wait_seconds`); sem lugar nela, recebe 503 com
    `Retry-After`."""
    queue_max_size: int = 64
    """Quantas chamadas podem esperar uma vaga, por processo. Fila cheia: 503 na
    hora, em vez de acumular conexões abertas até derrubar o serviço."""
    queue_max_wait_seconds: float = 30.0
    """Quanto uma chamada espera na fila antes do 503. Não conta no
    `timeout_seconds` do run, que só começa quando ele ganha a vaga. 0 desliga a
    fila (503 imediato, o comportamento antigo). Mantenha abaixo do timeout de
    quem chama."""
    idempotency_ttl_hours: int = 24
    """Por quanto tempo um `Idempotency-Key` de `/chat`/`/analyze` devolve a resposta
    guardada (`api/idempotency.py`). Passou disso, a mesma chave roda de novo."""
    aux_model_id: str | None = None
    """Modelo das tarefas de apoio — extrair memória de longo prazo e resumir a
    sessão. Vazio = o modelo do próprio agente. Um modelo barato aqui (ex.:
    `gemini-flash-lite-latest`) corta o custo dessas chamadas extras."""
    aux_model_provider: str | None = None
    """Provedor de `aux_model_id`; vazio = o do agente."""
    memory_context_limit: int = 10
    """Quantas memórias de longo prazo (as mais recentes) entram no prompt."""
    model_prices: str | None = None
    """JSON `{"modelo": [USD/1M entrada, USD/1M saída]}` que sobrescreve a tabela
    de `models/pricing.py` — o custo estimado das execuções sem Langfuse."""

    # Observabilidade — Langfuse (sem as duas chaves, o tracing vira no-op)
    langfuse_enabled: bool = False
    """Desligado por padrão, o mesmo valor do `.env.example` e do `docker-compose.yml`
    — os três precisam concordar, senão o comportamento depende de como o serviço
    subiu. Ligar com o Langfuse fora do ar custa o timeout do exportador por run
    (`langfuse_timeout_seconds`), então quem liga isto sobe também o profile
    `observability`."""
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_base_url: str = "http://localhost:3100"
    """Endereço pelo qual o serviço envia traces e scores."""
    langfuse_public_url: str | None = None
    """Endereço da UI no browser, para os links do console (padrão: `langfuse_base_url`)."""
    langfuse_project_id: str | None = None
    """Id do projeto; se ausente, é descoberto pela API do Langfuse."""
    langfuse_environment: str = "development"
    langfuse_timeout_seconds: int = 20
    """Timeout do exportador de spans. O default do SDK (5s) descarta lotes
    silenciosamente quando o Langfuse está ocupado (ex.: logo após subir)."""

    # Texto de entrada de /chat (`message`) e /analyze (`document`)
    max_input_chars: int = 200_000
    """Teto do texto enviado numa chamada, em caracteres (~50 mil tokens). Sem
    isto, um documento grande — o JSON de um histórico de conversa, por exemplo —
    segue inteiro para o modelo e estoura contexto e custo com um erro obscuro do
    provedor. Os anexos têm o seu próprio limite (`max_attachment_mb`)."""

    # Anexos multimodais em /chat e /analyze (imagem/áudio/vídeo/arquivo)
    max_attachment_mb: int = 20
    """Limite por anexo, checado antes de decodificar o base64 — evita gastar
    memória decodificando algo gigante antes de rejeitar."""

    # Tools — para onde elas podem falar (ver agent_service/tools/egress.py)
    tool_egress_allowlist: str = ""
    """Hosts internos que as tools podem alcançar mesmo resolvendo para IP privado,
    separados por vírgula (`faturamento.interno,10.0.0.5`). Vazio = só endereços
    públicos, que é o padrão: sem isso uma tool alcança o Postgres, o Redis e o
    metadata da nuvem a partir de dentro do container."""

    correlation_metadata_keys: str = "ciclo_id"
    """Chaves de metadata que ligam execuções de um mesmo fluxo, separadas por vírgula
    (`ciclo_id,conversation_id`). A tela do run mostra, em ordem, os runs que têm o
    mesmo valor numa delas (ex.: triagem → redação → base consultada num ciclo do
    moderador). Vazio desliga a linha do tempo."""

    # Sites como fonte de uma collection (ver agent_service/documents/web_sync.py)
    web_source_max_pages: int = 500
    """Teto de páginas por fonte, qualquer que seja o `max_pages` pedido."""
    web_source_concurrency: int = 3
    """Páginas processadas ao mesmo tempo por fonte (download + formatação)."""
    web_source_request_delay_ms: int = 300
    """Intervalo mínimo entre duas requisições ao mesmo site — educação com quem hospeda."""
    web_source_max_file_mb: int = 20
    """Teto de cada arquivo linkado (PDF, DOCX...) que a coleta baixa — o mesmo
    tamanho padrão de `max_attachment_mb`. As páginas HTML têm 5 MB."""
    web_source_user_agent: str = "KuroBot/1.0 (+https://github.com/Arthur-Marques-IA)"
    """Como o crawler se apresenta; é este nome que o `robots.txt` do site enxerga."""
    web_source_scheduler: bool = True
    """Ressincroniza as fontes com `refresh` diário/semanal/mensal (o próximo sync
    entra na fila de jobs com `run_after` no futuro). Desligado, só o sync manual roda."""

    # Fila de jobs no Postgres (ver agent_service/jobs/)
    jobs_enabled: bool = True
    """Roda o worker de jobs neste processo. Desligado, a réplica só enfileira
    (outra réplica com o worker ligado executa)."""
    jobs_concurrency: int = 2
    """Jobs rodando ao mesmo tempo nesta réplica."""
    jobs_poll_seconds: float = 5.0
    """De quanto em quanto tempo o worker olha a fila quando nada o acordou."""
    jobs_lease_seconds: int = 60
    """Validade do lease de um job em execução (renovado a cada terço). Um processo
    que morre libera o job para outra réplica depois disto."""
    jobs_max_attempts: int = 3
    """Tentativas de um job antes de virar `failed`."""
    jobs_backoff_seconds: int = 30
    """Espera antes da 2ª tentativa; dobra a cada uma (até 1 hora)."""
    jobs_listen: bool = True
    """`LISTEN/NOTIFY` para acordar o worker assim que algo entra na fila."""

    # Tools — kind="python" (ver agent_service/tools/python_tool.py)
    custom_python_tools_enabled: bool = False
    """Desligado por padrão: tools Python rodam num namespace restrito, mas
    NÃO são uma sandbox forte contra um autor mal-intencionado — só ligue se
    quem tem a chave de escopo `admin` já for confiável, porque é ela que
    permite criar uma tool Python (`api/auth.py`). Sem `ADMIN_API_KEY`
    configurada, isso é qualquer um que alcance a porta."""


@lru_cache
def get_settings() -> Settings:
    return Settings()


def split_keys(value: str | None) -> list[str]:
    """`ADMIN_API_KEY`/`RUNTIME_API_KEY` (e o `KURO_API_KEY` que o compose copia delas) como
    lista: `nova,antiga` durante uma rotação. A primeira é a atual — a que um cliente manda."""
    return [k.strip() for k in (value or "").split(",") if k.strip()]


def current_key(value: str | None) -> str | None:
    """A chave que um cliente deve mandar quando a variável traz uma lista (a primeira)."""
    keys = split_keys(value)
    return keys[0] if keys else None
