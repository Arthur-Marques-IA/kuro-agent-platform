"""Testes das credenciais de modelo: cifra em repouso (`crypto.py`),
persistência (`store.py`, várias credenciais por provedor), resolução em
`provider.get_model` e o CRUD/teste expostos em
`api/model_credentials_routes.py`.
"""

import dataclasses

import pytest
from fastapi import HTTPException

from agent_service.models import catalog, crypto, store
from agent_service.models.catalog import ProviderProbeError
from agent_service.models.provider import ProviderNotConfiguredError, UnknownModelProviderError, get_model

from agent_service.api.model_credentials_routes import (
    ModelCredentialAdHocTestIn,
    ModelCredentialCreateIn,
    ModelCredentialUpdateIn,
    create_model_credential,
    delete_model_credential,
    list_model_credentials,
    update_model_credential,
)
from agent_service.api.model_credentials_routes import test_ad_hoc_credential as call_test_ad_hoc_credential
from agent_service.api.model_credentials_routes import test_model_credential as call_test_model_credential


@pytest.fixture(autouse=True)
def clean_credentials():
    for row in store.list_credentials():
        store.delete_credential(row["id"])
    yield
    for row in store.list_credentials():
        store.delete_credential(row["id"])


# -- crypto -----------------------------------------------------------------


def test_encrypt_decrypt_roundtrip():
    token = crypto.encrypt_secret("sk-super-secret-123")
    assert token != "sk-super-secret-123"
    assert crypto.decrypt_secret(token) == "sk-super-secret-123"


def test_mask_secret_keeps_only_last_four_chars():
    assert crypto.mask_secret("sk-abcdefgh1234") == "····1234"
    assert crypto.mask_secret("ab") == "····"


# -- store --------------------------------------------------------------


def test_create_and_preserve_key_when_omitted():
    created = store.create_credential("openai", "Padrão", api_key="sk-first-key", enabled=True)
    assert created["key_hint"] == crypto.mask_secret("sk-first-key")
    assert store.get_decrypted_api_key(created["id"]) == "sk-first-key"

    # Atualiza só `enabled`, sem reenviar a chave: ela continua a mesma.
    store.update_credential(created["id"], enabled=False)
    assert store.get_decrypted_api_key(created["id"]) is None  # desabilitada, não lê a chave
    row = store.get_credential(created["id"])
    assert row["enabled"] is False
    assert row["api_key_encrypted"] is not None  # a chave em si não foi apagada


def test_clear_api_key_removes_it():
    created = store.create_credential("openai", "Padrão", api_key="sk-to-clear", enabled=True)
    store.update_credential(created["id"], clear_api_key=True)
    row = store.get_credential(created["id"])
    assert row["api_key_encrypted"] is None
    assert row["key_hint"] is None


def test_multiple_credentials_per_provider():
    a = store.create_credential("openai", "Cliente A", api_key="sk-a")
    b = store.create_credential("openai", "Cliente B", api_key="sk-b")
    assert a["id"] != b["id"]
    ids = {row["id"] for row in store.list_credentials("openai")}
    assert ids == {a["id"], b["id"]}
    assert store.get_decrypted_api_key(a["id"]) == "sk-a"
    assert store.get_decrypted_api_key(b["id"]) == "sk-b"


def test_resolve_default_credential_picks_oldest_enabled():
    first = store.create_credential("openai", "Primeira", api_key="sk-1")
    store.create_credential("openai", "Segunda", api_key="sk-2")
    assert store.resolve_default_credential("openai")["id"] == first["id"]

    store.update_credential(first["id"], enabled=False)
    assert store.resolve_default_credential("openai")["id"] != first["id"]


def test_record_test_result_updates_row():
    created = store.create_credential("openai", "Padrão", api_key="sk-x", enabled=True)
    from datetime import UTC, datetime

    updated = store.record_test_result(created["id"], ok=True, message=None, tested_at=datetime.now(UTC))
    assert updated["last_test_ok"] is True

    assert store.record_test_result("does-not-exist", ok=True, message=None, tested_at=datetime.now(UTC)) is None


# -- provider.get_model ------------------------------------------------------


def test_get_model_google_falls_back_to_settings_env_key():
    model = get_model()
    assert model.provider == "Google"


def test_get_model_unknown_provider_raises():
    with pytest.raises(UnknownModelProviderError):
        get_model(provider="does-not-exist")


def test_get_model_openai_without_stored_key_raises_not_configured():
    with pytest.raises(ProviderNotConfiguredError):
        get_model(provider="openai", model_id="gpt-4.1-mini")


def test_get_model_openai_uses_default_credential():
    store.create_credential("openai", "Padrão", api_key="sk-test-key", enabled=True)
    model = get_model(provider="openai", model_id="gpt-4.1-mini")
    assert model.id == "gpt-4.1-mini"


def test_get_model_openai_uses_pinned_credential_over_default():
    store.create_credential("openai", "Padrão", api_key="sk-default")
    other = store.create_credential("openai", "Cliente X", api_key="sk-client-x")
    model = get_model(provider="openai", model_id="gpt-4.1-mini", credential_id=other["id"])
    assert model.api_key == "sk-client-x"


def test_get_model_ollama_needs_no_key():
    model = get_model(provider="ollama", model_id="llama3.1")
    assert model.id == "llama3.1"


# -- routes ---------------------------------------------------------------


def test_list_model_credentials_hides_secrets_but_reports_hint():
    store.create_credential("anthropic", "Padrão", api_key="sk-ant-123456", enabled=True)

    credentials = list_model_credentials()

    assert len(credentials) == 1
    assert credentials[0].configured is True
    assert credentials[0].key_hint == "····3456"
    assert not hasattr(credentials[0], "api_key")


def test_create_model_credential_unknown_provider_is_404():
    with pytest.raises(HTTPException) as exc:
        create_model_credential(ModelCredentialCreateIn(provider="does-not-exist", label="x"))
    assert exc.value.status_code == 404


def test_create_model_credential_rejects_enabling_without_key():
    with pytest.raises(HTTPException) as exc:
        create_model_credential(ModelCredentialCreateIn(provider="openai", label="Padrão", enabled=True))
    assert exc.value.status_code == 422


def test_create_model_credential_rejects_base_url_when_unsupported():
    with pytest.raises(HTTPException) as exc:
        create_model_credential(
            ModelCredentialCreateIn(provider="anthropic", label="Padrão", base_url="http://evil.example")
        )
    assert exc.value.status_code == 422


def test_create_and_update_model_credential_saves_key_and_enables():
    out = create_model_credential(ModelCredentialCreateIn(provider="openai", label="Padrão", api_key="sk-live-key"))
    assert out.configured is True
    assert out.enabled is True
    assert out.key_hint == "····-key"

    renamed = update_model_credential(out.id, ModelCredentialUpdateIn(label="Renomeada"))
    assert renamed.label == "Renomeada"
    assert renamed.configured is True  # chave preservada


def test_delete_model_credential_removes_it():
    out = create_model_credential(ModelCredentialCreateIn(provider="openai", label="Padrão", api_key="sk-x"))
    delete_model_credential(out.id)
    assert store.get_credential(out.id) is None


def test_delete_model_credential_unknown_is_404():
    with pytest.raises(HTTPException) as exc:
        delete_model_credential("does-not-exist")
    assert exc.value.status_code == 404


def test_credential_reports_agents_using_it(monkeypatch):
    out = create_model_credential(ModelCredentialCreateIn(provider="openai", label="Padrão", api_key="sk-x"))
    monkeypatch.setattr(
        "agent_service.api.model_credentials_routes.list_definitions",
        lambda: [
            {"name": "Agente Um", "model_credential_id": out.id},
            {"name": "Agente Dois", "model_credential_id": None},
        ],
    )
    credentials = list_model_credentials()
    assert credentials[0].agents_using == ["Agente Um"]


def test_ad_hoc_test_requires_key():
    with pytest.raises(HTTPException) as exc:
        call_test_ad_hoc_credential(ModelCredentialAdHocTestIn(provider="openai"))
    assert exc.value.status_code == 422


def test_ad_hoc_test_reports_success_without_saving(monkeypatch):
    monkeypatch.setitem(
        catalog.PROVIDERS,
        "openai",
        dataclasses.replace(catalog.PROVIDERS["openai"], probe=lambda *, api_key, base_url: None),
    )
    result = call_test_ad_hoc_credential(ModelCredentialAdHocTestIn(provider="openai", api_key="sk-good"))
    assert result.ok is True
    assert store.list_credentials("openai") == []  # testar um valor ad-hoc não grava nada


def test_test_model_credential_reports_success_and_records_it(monkeypatch):
    monkeypatch.setitem(
        catalog.PROVIDERS,
        "openai",
        dataclasses.replace(catalog.PROVIDERS["openai"], probe=lambda *, api_key, base_url: None),
    )
    created = create_model_credential(ModelCredentialCreateIn(provider="openai", label="Padrão", api_key="sk-good"))

    result = call_test_model_credential(created.id, ModelCredentialUpdateIn())

    assert result.ok is True
    assert store.get_credential(created.id)["last_test_ok"] is True


def test_test_model_credential_reports_failure(monkeypatch):
    def fake_probe(*, api_key: str, base_url: str | None) -> None:
        raise ProviderProbeError("chave inválida")

    monkeypatch.setitem(catalog.PROVIDERS, "openai", dataclasses.replace(catalog.PROVIDERS["openai"], probe=fake_probe))
    created = create_model_credential(ModelCredentialCreateIn(provider="openai", label="Padrão", api_key="sk-x"))

    result = call_test_model_credential(created.id, ModelCredentialUpdateIn())

    assert result.ok is False
    assert result.message == "chave inválida"


# -- /model-providers: credencial do ambiente ---------------------------------


def _providers_by_name():
    from agent_service.api.model_providers_routes import list_model_providers

    return {p.provider: p for p in list_model_providers()}


def test_model_providers_reports_env_credential_without_value(monkeypatch):
    from agent_service.config import get_settings

    monkeypatch.setattr(get_settings(), "google_api_key", "AIza-env-secret-9876")
    providers = _providers_by_name()
    env = providers["google"].env_credential
    assert env is not None
    assert env.variable == "GOOGLE_API_KEY"
    assert env.in_use is True  # nada cadastrado: é a do ambiente que responde
    # Os outros provedores não têm fallback de ambiente no `get_model`.
    assert providers["openai"].env_credential is None
    assert providers["anthropic"].env_credential is None
    body = str([p.model_dump() for p in providers.values()])
    assert "AIza-env-secret-9876" not in body
    assert "9876" not in body


def test_model_providers_env_credential_not_in_use_with_stored_key(monkeypatch):
    from agent_service.config import get_settings

    monkeypatch.setattr(get_settings(), "google_api_key", "AIza-env-secret")
    store.create_credential("google", "Cadastrada", api_key="AIza-stored", enabled=True)
    env = _providers_by_name()["google"].env_credential
    assert env is not None
    assert env.in_use is False


def test_model_providers_without_env_key(monkeypatch):
    from agent_service.config import get_settings

    monkeypatch.setattr(get_settings(), "google_api_key", None)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    assert _providers_by_name()["google"].env_credential is None
