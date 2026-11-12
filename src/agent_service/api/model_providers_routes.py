"""Catálogo de provedores de modelo suportados (`/model-providers`) — o que
cada um precisa (chave, base_url) e quantas credenciais já existem, pro
console montar o diálogo de nova chave e agrupar a tela de credenciais por
provedor. CRUD e teste de credenciais em `api/model_credentials_routes.py`.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from agent_service.models import store
from agent_service.models.catalog import PROVIDERS

router = APIRouter(prefix="/model-providers", tags=["model-providers"])


class EnvCredentialOut(BaseModel):
    """Credencial que o serviço lê do **ambiente** (não do cadastro). Só o nome da
    variável: o valor nunca sai daqui, nem mascarado."""

    variable: str
    """Ex.: `GOOGLE_API_KEY`."""
    in_use: bool
    """`true` quando nenhuma credencial cadastrada resolve a chave padrão do
    provedor — é esta variável que os agentes sem credencial fixa usam agora."""


class ModelProviderOut(BaseModel):
    provider: str
    label: str
    requires_api_key: bool
    supports_custom_base_url: bool
    default_model_id: str
    docs_url: str
    credential_count: int
    configured_count: int
    env_credential: EnvCredentialOut | None = None
    """Fallback lido do ambiente, quando o provedor tem um e ele está definido.
    Hoje só o `google` (`GOOGLE_API_KEY`, ou o Vertex com
    `GOOGLE_GENAI_USE_VERTEXAI=true`); os outros só usam o cadastro."""


def _env_credential(provider: str) -> EnvCredentialOut | None:
    """Espelha o fallback de `models/provider.get_model`: só reporta o que o
    serviço de fato usa."""
    import os

    from agent_service.config import get_settings

    if provider != "google":
        return None
    if get_settings().google_api_key:
        variable = "GOOGLE_API_KEY"
    elif os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "false").lower() == "true":
        variable = "GOOGLE_GENAI_USE_VERTEXAI"
    else:
        return None
    default = store.resolve_default_credential(provider)
    in_use = default is None or not default["api_key_encrypted"]
    return EnvCredentialOut(variable=variable, in_use=in_use)


@router.get("", response_model=list[ModelProviderOut])
def list_model_providers() -> list[ModelProviderOut]:
    result = []
    for name, spec in PROVIDERS.items():
        credentials = store.list_credentials(name)
        configured_count = sum(1 for c in credentials if c["api_key_encrypted"] or not spec.requires_api_key)
        result.append(
            ModelProviderOut(
                provider=spec.provider,
                label=spec.label,
                requires_api_key=spec.requires_api_key,
                supports_custom_base_url=spec.supports_custom_base_url,
                default_model_id=spec.default_model_id,
                docs_url=spec.docs_url,
                credential_count=len(credentials),
                configured_count=configured_count,
                env_credential=_env_credential(name),
            )
        )
    return result


class ProviderModelOut(BaseModel):
    id: str
    label: str
    created: int | None = None
    channel: Literal["stable", "preview", "alias"] | None = None
    """Deduzido do nome — o provedor não informa (ver `models/listing.py`).
    `preview` e `alias` (`-latest`) podem mudar sem aviso: evite em produção."""


class ProviderModelsOut(BaseModel):
    provider: str
    models: list[ProviderModelOut]
    default_model_id: str
    fetched_at: float
    """Quando a lista foi lida do provedor (epoch) — ela fica em cache por 15 min."""


@router.get("/{provider}/models", response_model=ProviderModelsOut)
def list_provider_models(provider: str, credential_id: str | None = None, refresh: bool = False) -> ProviderModelsOut:
    """Modelos que o provedor oferece **agora**, lidos da API dele com a credencial
    (a informada, ou a padrão do provedor). Não gasta tokens. `refresh=true`
    ignora o cache. 502 com a mensagem do provedor se a chave não funcionar."""
    from fastapi import HTTPException

    from agent_service.config import get_settings
    from agent_service.models.catalog import ProviderProbeError
    from agent_service.models.listing import as_dict, list_models

    spec = PROVIDERS.get(provider)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Provedor desconhecido: {provider!r}")
    credential = store.get_credential(credential_id) if credential_id else store.resolve_default_credential(provider)
    if credential_id and credential is None:
        raise HTTPException(status_code=404, detail=f"Credencial {credential_id!r} não encontrada")
    api_key = store.get_decrypted_api_key(credential["id"]) if credential else None
    base_url = credential["base_url"] if credential else None
    if provider == "google" and not api_key:
        api_key = get_settings().google_api_key  # o fallback antigo, como em `provider.get_model`
    if spec.requires_api_key and not api_key:
        raise HTTPException(status_code=422, detail=f"{spec.label}: nenhuma chave cadastrada em /model-credentials")
    try:
        models, fetched_at = list_models(
            provider, api_key=api_key, base_url=base_url, cache_key=credential["id"] if credential else None, refresh=refresh
        )
    except ProviderProbeError as exc:
        raise HTTPException(status_code=502, detail=f"{spec.label} não listou os modelos: {exc}") from exc
    return ProviderModelsOut(
        provider=provider,
        models=[ProviderModelOut(**as_dict(m, provider)) for m in models],
        default_model_id=spec.default_model_id,
        fetched_at=fetched_at,
    )
