"""Modelos disponíveis lidos do provedor (`models/listing.py`) e a rota que os expõe."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agent_service.api.model_providers_routes import list_provider_models
from agent_service.models import listing
from agent_service.models.catalog import ProviderProbeError


@pytest.fixture(autouse=True)
def cache_limpo():
    listing.clear_cache()
    yield
    listing.clear_cache()


def test_openai_keeps_only_chat_models_newest_first(monkeypatch):
    class Models:
        def list(self):
            return [
                SimpleNamespace(id=i, created=c)
                for i, c in [
                    ("gpt-4.1-mini", 1),
                    ("gpt-5", 3),
                    ("text-embedding-3-small", 2),
                    ("gpt-4o-realtime-preview", 4),
                    ("o4-mini", 2),
                    ("whisper-1", 5),
                    ("gpt-image-1", 6),
                ]
            ]

    import openai

    monkeypatch.setattr(openai, "OpenAI", lambda **kw: SimpleNamespace(models=Models()))
    models, _ = listing.list_models("openai", api_key="k", base_url=None, cache_key="c1")

    assert [m.id for m in models] == ["gpt-5", "o4-mini", "gpt-4.1-mini"]


def test_google_keeps_gemini_that_generate_content(monkeypatch):
    class Models:
        def list(self, config):
            def m(name, actions=("generateContent",)):
                return SimpleNamespace(
                    name=f"models/{name}", display_name=name.title(), supported_actions=list(actions)
                )

            return [
                m("gemini-3.5-flash"),
                m("gemini-2.5-flash"),
                m("gemini-embedding-001", ("embedContent",)),
                m("gemini-3.5-transcribe"),
                m("gemini-2.5-flash-preview-tts"),
                m("imagen-4.0-generate"),
            ]

    import google.genai

    monkeypatch.setattr(google.genai, "Client", lambda **kw: SimpleNamespace(models=Models()))
    models, _ = listing.list_models("google", api_key="k", base_url=None, cache_key="g1")

    assert [m.id for m in models] == ["gemini-3.5-flash", "gemini-2.5-flash"]


def test_result_is_cached_until_refresh(monkeypatch):
    calls = []

    def fake(api_key, base_url):
        calls.append(1)
        return [listing.ModelInfo(id=f"m{len(calls)}", label="m")]

    monkeypatch.setitem(listing._LISTERS, "ollama", fake)
    first, _ = listing.list_models("ollama", api_key=None, base_url=None, cache_key=None)
    again, _ = listing.list_models("ollama", api_key=None, base_url=None, cache_key=None)
    fresh, _ = listing.list_models("ollama", api_key=None, base_url=None, cache_key=None, refresh=True)

    assert [first[0].id, again[0].id, fresh[0].id] == ["m1", "m1", "m2"]


def test_expired_cache_answers_at_once_and_refreshes_in_background(monkeypatch):
    calls = []

    def fake(api_key, base_url):
        calls.append(1)
        return [listing.ModelInfo(id=f"m{len(calls)}", label="m")]

    monkeypatch.setitem(listing._LISTERS, "ollama", fake)
    started = []
    monkeypatch.setattr(listing, "_refresh_in_background", lambda key, lister, api_key, base_url: started.append(key))
    listing.list_models("ollama", api_key=None, base_url=None, cache_key="x")
    fetched_at, models = listing._cache[("ollama", "x")]
    listing._cache[("ollama", "x")] = (fetched_at - listing.CACHE_SECONDS - 1, models)

    stale, _ = listing.list_models("ollama", api_key=None, base_url=None, cache_key="x")

    assert stale[0].id == "m1" and len(calls) == 1 and started == [("ollama", "x")]


def test_route_maps_provider_failure_and_missing_key(monkeypatch):
    def broken(api_key, base_url):
        raise ProviderProbeError("chave sem permissão")

    monkeypatch.setitem(listing._LISTERS, "ollama", broken)
    with pytest.raises(HTTPException) as exc:
        list_provider_models("ollama")
    assert exc.value.status_code == 502 and "chave sem permissão" in exc.value.detail

    with pytest.raises(HTTPException) as exc:
        list_provider_models("anthropic")  # nenhuma credencial cadastrada nos testes
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        list_provider_models("inexistente")
    assert exc.value.status_code == 404


@pytest.mark.parametrize(
    ("provider", "model_id", "label", "channel"),
    [
        ("google", "gemini-2.5-flash", "Gemini 2.5 Flash", "stable"),
        ("google", "gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview", "preview"),
        ("google", "gemini-omni-flash-preview", "", "preview"),
        ("google", "gemini-2.0-flash-exp", "", "preview"),
        ("google", "gemini-exp-1206", "", "preview"),
        ("google", "gemini-flash-latest", "", "alias"),
        ("anthropic", "claude-3-7-sonnet-latest", "", "alias"),
        ("openai", "gpt-4.1-mini", "gpt-4.1-mini", "stable"),
        ("openai", "gpt-4o-2024-08-06", "", "stable"),
        ("ollama", "llama3:latest", "", None),
    ],
)
def test_channel_deduzido_do_nome(provider, model_id, label, channel):
    from agent_service.models.listing import model_channel

    assert model_channel(provider, model_id, label) == channel
