"""Configuração faltando no serviço (chave de cifragem, credencial do provedor)
tem que aparecer como tal: 503 com o motivo, não 500 cru nem um run
"interrompido (cliente desconectou)"."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_service.api import errors
from agent_service.config import get_settings
from agent_service.models import crypto, store
from agent_service.models.provider import ProviderNotConfiguredError, get_model


@pytest.fixture(autouse=True)
def clean_credentials():
    for row in store.list_credentials():
        store.delete_credential(row["id"])
    yield
    for row in store.list_credentials():
        store.delete_credential(row["id"])
    crypto.reset_cache()


@pytest.fixture
def sem_cifra(monkeypatch):
    monkeypatch.setattr(get_settings(), "credentials_encryption_key", None)
    crypto.reset_cache()


def test_google_sem_chave_falha_na_montagem(monkeypatch):
    """Antes o Gemini era montado com `api_key=None` e o run falhava lá adiante."""
    monkeypatch.setattr(get_settings(), "google_api_key", None)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    with pytest.raises(ProviderNotConfiguredError, match="GOOGLE_API_KEY"):
        get_model(provider="google", model_id="gemini-2.5-flash")


def test_google_no_vertex_nao_exige_chave(monkeypatch):
    monkeypatch.setattr(get_settings(), "google_api_key", None)
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    assert get_model(provider="google", model_id="gemini-2.5-flash").provider == "Google"


def test_credencial_que_nao_decifra_vira_provider_not_configured(monkeypatch):
    store.create_credential("openai", "Prod", api_key="sk-test-key", enabled=True)
    monkeypatch.setattr(get_settings(), "credentials_encryption_key", None)
    crypto.reset_cache()
    with pytest.raises(ProviderNotConfiguredError, match="CREDENTIALS_ENCRYPTION_KEY"):
        get_model(provider="openai", model_id="gpt-4.1-mini")


def _app_que_falha(exc: Exception) -> TestClient:
    app = FastAPI()

    @app.post("/x")
    def x():
        raise exc

    errors.install(app)
    return TestClient(app)


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (
            crypto.EncryptionNotConfiguredError("CREDENTIALS_ENCRYPTION_KEY não configurada"),
            "encryption_not_configured",
        ),
        (ProviderNotConfiguredError("openai: nenhuma chave"), "model_provider_not_configured"),
    ],
)
def test_erro_de_configuracao_responde_503_com_motivo(exc, kind):
    resposta = _app_que_falha(exc).post("/x")
    assert resposta.status_code == 503
    assert resposta.json() == {"detail": str(exc), "error": kind}


def test_cadastrar_chave_sem_cifra_responde_503(sem_cifra):
    from agent_service.api.model_credentials_routes import router

    app = FastAPI()
    app.include_router(router)
    errors.install(app)
    resposta = TestClient(app).post(
        "/model-credentials", json={"provider": "openai", "label": "x", "api_key": "sk-1", "enabled": True}
    )
    assert resposta.status_code == 503
    assert "CREDENTIALS_ENCRYPTION_KEY" in resposta.json()["detail"]


def test_health_reporta_cifra(sem_cifra):
    from agent_service.api.routes import health

    assert health()["model_credentials"].startswith("disabled")


def test_health_reporta_cifra_invalida(monkeypatch):
    from agent_service.api.routes import health

    monkeypatch.setattr(get_settings(), "credentials_encryption_key", "nao-e-fernet")
    crypto.reset_cache()
    assert health()["model_credentials"].startswith("invalid")


def test_health_mostra_a_versao_do_servico():
    from importlib.metadata import version

    from agent_service.api.routes import health

    assert health()["version"] == version("agent-service")
