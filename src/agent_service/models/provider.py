"""Abstração de gerenciamento de modelos.

Isola o resto do serviço de qual SDK/provedor de LLM está por trás de um
agente. As chaves de API vêm de `models/store.py` (cifradas em repouso,
cadastradas pelo console em `/model-credentials`) — um agente pode fixar
qual credencial usar (`credential_id`, vindo de `agent_definitions
.model_credential_id`); sem isso, cai na credencial habilitada mais antiga
do provedor. `google` também aceita o fallback antigo via `GOOGLE_API_KEY`
no ambiente, para não quebrar quem já rodava assim antes da UI de chaves
existir.

Novo provedor: um branch aqui + uma entrada em `models/catalog.py` (pro
console saber pedir a chave certa e validar).
"""

import os
from typing import Any

from agno.models.base import Model

from agent_service.config import get_settings
from agent_service.models import store
from agent_service.models.crypto import EncryptionNotConfiguredError
from agent_service.models.params import provider_kwargs


class UnknownModelProviderError(ValueError):
    pass


class ProviderNotConfiguredError(ValueError):
    """Provedor reconhecido, mas sem credencial utilizável: nenhuma cadastrada em
    /model-credentials, ou uma cadastrada que não dá para decifrar. É configuração
    do serviço, não da chamada — as rotas devolvem 503 com esta mensagem."""


def _credentials(provider: str, credential_id: str | None) -> tuple[str | None, str | None]:
    """(api_key decifrada, base_url) da credencial pedida — ou, sem uma fixa,
    da credencial padrão do provedor. `(None, None)` se nada resolver."""
    credential = store.get_credential(credential_id) if credential_id else store.resolve_default_credential(provider)
    if credential is None:
        return None, None
    try:
        api_key = store.get_decrypted_api_key(credential["id"])
    except EncryptionNotConfiguredError as exc:
        # Sem isto, a falha de cifragem subia crua e virava um 500 sem explicação.
        raise ProviderNotConfiguredError(
            f"{provider}: a credencial {credential['label']!r} não pôde ser lida — {exc}"
        ) from exc
    return api_key, credential["base_url"]


def get_model(
    provider: str | None = None,
    model_id: str | None = None,
    credential_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> Model:
    """`params`: temperatura etc. no vocabulário de `models/params.py`."""
    settings = get_settings()
    provider = (provider or settings.default_model_provider).lower()
    model_id = model_id or settings.default_model_id
    extra = provider_kwargs(provider, params, model_id)

    if provider == "google":
        from agno.models.google import Gemini

        api_key, _ = _credentials("google", credential_id)
        api_key = api_key or settings.google_api_key
        # Sem chave o Agno só loga um erro e monta o cliente mesmo assim: o run
        # falhava lá adiante, e quem chamava via "interrompido (cliente
        # desconectou)" em vez de "falta a chave". O Vertex autentica por ADC.
        if not api_key and os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "false").lower() != "true":
            raise ProviderNotConfiguredError(
                "google: nenhuma chave cadastrada em /model-credentials nem GOOGLE_API_KEY no ambiente"
            )
        return Gemini(id=model_id, api_key=api_key, **extra)

    if provider == "openai":
        from agno.models.openai import OpenAIChat

        api_key, base_url = _credentials("openai", credential_id)
        if not api_key:
            raise ProviderNotConfiguredError("openai: nenhuma chave cadastrada em /model-credentials")
        return OpenAIChat(id=model_id, api_key=api_key, base_url=base_url or None, **extra)

    if provider == "anthropic":
        from agno.models.anthropic import Claude

        api_key, _ = _credentials("anthropic", credential_id)
        if not api_key:
            raise ProviderNotConfiguredError("anthropic: nenhuma chave cadastrada em /model-credentials")
        return Claude(id=model_id, api_key=api_key, **extra)

    if provider == "ollama":
        from agno.models.ollama import Ollama

        _, base_url = _credentials("ollama", credential_id)
        return Ollama(id=model_id, host=base_url or None, **extra)

    raise UnknownModelProviderError(
        f"Provedor de modelo desconhecido: {provider!r}. "
        "Adicione um novo branch em agent_service.models.provider.get_model."
    )
