"""Qual embedder indexa e consulta os vetores de uma collection.

Antes isto era fixo: toda collection usava o Gemini com a `GOOGLE_API_KEY` do
ambiente. Isso amarrava o RAG ao Google mesmo num serviço que roda os agentes
no OpenAI ou no Ollama, e ignorava as credenciais cadastradas no console
(`/model-credentials`) — que é de onde `models/provider.py` tira a chave do
modelo.

Aqui a chave é resolvida do mesmo jeito que a do modelo, e o provedor é
escolhido **por collection, na criação**. Ele não muda depois: a tabela
`knowledge_<nome>` guarda vetores de uma dimensão só, produzidos por um
embedder só. Trocar o embedder de uma collection já indexada não dá erro — dá
busca silenciosamente errada, que é pior. Para trocar, crie outra collection e
reindexe.

Provedor novo: um branch em `build_embedder` e uma entrada em `EMBEDDERS`.
"""

from dataclasses import dataclass
from typing import Any

from agent_service.config import get_settings
from agent_service.models import store

DEFAULT_EMBEDDER_PROVIDER = "google"
"""O de antes: uma collection sem provedor gravado continua no Gemini."""


class EmbedderError(ValueError):
    """Base das falhas de embedder: quem só precisa saber que "o embedder desta
    collection não dá para montar" captura esta. Falha de um provedor (a sonda do
    ollama, por exemplo) é convertida para uma subclasse aqui, com `raise ... from`,
    para a mensagem e a causa original chegarem a quem trata."""


class UnknownEmbedderProviderError(EmbedderError):
    pass


class EmbedderNotConfiguredError(EmbedderError):
    """Provedor reconhecido, mas sem chave cadastrada — 422 na criação."""


@dataclass(frozen=True)
class EmbedderSpec:
    provider: str
    label: str
    default_model_id: str
    default_dimensions: int | None
    """Tamanho do vetor de `default_model_id`. `None` = o padrão do Agno para o
    provedor. Outro modelo pode ter outro tamanho: aí a collection precisa gravar
    `embedder_dimensions`, senão a tabela pgvector nasce com a largura errada."""
    requires_api_key: bool
    description: str


EMBEDDERS: dict[str, EmbedderSpec] = {
    "google": EmbedderSpec(
        provider="google",
        label="Google (Gemini)",
        default_model_id="gemini-embedding-001",
        default_dimensions=None,
        requires_api_key=True,
        description="Usa a credencial `google` do console, ou a GOOGLE_API_KEY do ambiente.",
    ),
    "openai": EmbedderSpec(
        provider="openai",
        label="OpenAI",
        default_model_id="text-embedding-3-small",
        default_dimensions=1536,
        requires_api_key=True,
        description="Usa a credencial `openai` do console (respeita o base_url dela).",
    ),
    "ollama": EmbedderSpec(
        provider="ollama",
        label="Ollama (local)",
        default_model_id="nomic-embed-text",
        default_dimensions=768,
        requires_api_key=False,
        description="Roda no Ollama configurado — nenhuma chave de API, nada sai da máquina.",
    ),
}


def resolve_spec(provider: str | None) -> EmbedderSpec:
    nome = (provider or DEFAULT_EMBEDDER_PROVIDER).lower()
    spec = EMBEDDERS.get(nome)
    if spec is None:
        raise UnknownEmbedderProviderError(
            f"Provedor de embedding desconhecido: {provider!r} (use {', '.join(sorted(EMBEDDERS))})"
        )
    return spec


def _credential(provider: str) -> tuple[str | None, str | None]:
    credential = store.resolve_default_credential(provider)
    if credential is None:
        return None, None
    return store.get_decrypted_api_key(credential["id"]), credential["base_url"]


def check_configured(provider: str | None, model_id: str | None = None) -> None:
    """O provedor responderia agora? Levanta `EmbedderNotConfiguredError` se não.

    Para google/openai basta a chave; o `ollama` não tem chave nenhuma, então
    montar o embedder sempre "funciona" e a falha só apareceria na primeira
    ingestão — exatamente o que a checagem na criação existe para evitar. Por
    isso aqui ele é sondado de verdade, pelo mesmo `/api/tags` que
    `models/catalog.py` usa para validar uma credencial."""
    spec = resolve_spec(provider)
    build_embedder(provider, model_id)
    if spec.provider != "ollama":
        return

    from agent_service.models.catalog import ProviderProbeError, PROVIDERS

    _, base_url = _credential("ollama")
    try:
        PROVIDERS["ollama"].probe(api_key=None, base_url=base_url)
    except ProviderProbeError as exc:
        raise EmbedderNotConfiguredError(
            f"ollama: {exc}. Cadastre uma credencial `ollama` com o endereço do servidor "
            "em /model-credentials (o padrão é localhost:11434, que dentro do container é o próprio container)."
        ) from exc


def build_embedder(provider: str | None, model_id: str | None = None, dimensions: int | None = None) -> Any:
    """Instancia o embedder do Agno para o provedor pedido."""
    spec = resolve_spec(provider)
    # Só aplica a dimensão padrão quando o modelo também é o padrão: um modelo
    # trocado sem `dimensions` junto é chute, e chute aqui vira busca errada.
    if dimensions is None and (model_id is None or model_id == spec.default_model_id):
        dimensions = spec.default_dimensions
    model_id = model_id or spec.default_model_id
    extra: dict[str, Any] = {"dimensions": dimensions} if dimensions else {}
    api_key, base_url = _credential(spec.provider)

    if spec.provider == "google":
        from agno.knowledge.embedder.google import GeminiEmbedder

        # Fallback histórico: quem já rodava com GOOGLE_API_KEY no ambiente,
        # antes da UI de credenciais existir, continua funcionando.
        api_key = api_key or get_settings().google_api_key
        if not api_key:
            raise EmbedderNotConfiguredError(
                "google: nenhuma chave cadastrada em /model-credentials nem GOOGLE_API_KEY no ambiente"
            )
        return GeminiEmbedder(id=model_id, api_key=api_key, **extra)

    if spec.provider == "openai":
        from agno.knowledge.embedder.openai import OpenAIEmbedder

        if not api_key:
            raise EmbedderNotConfiguredError("openai: nenhuma chave cadastrada em /model-credentials")
        return OpenAIEmbedder(id=model_id, api_key=api_key, base_url=base_url or None, **extra)

    from agno.knowledge.embedder.ollama import OllamaEmbedder

    return OllamaEmbedder(id=model_id, host=base_url or None, **extra)
