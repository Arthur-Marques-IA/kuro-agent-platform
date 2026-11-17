"""`model_params.prompt_cache` (cache do prefixo no Claude) e os tokens de cache
registrados na span do modelo."""

from types import SimpleNamespace

import pytest

from agent_service.models.params import ModelParamsError, provider_kwargs, validate_model_params
from agent_service.observability.tracing import _model_spans


@pytest.mark.parametrize(("ttl", "extended"), [("5m", False), ("1h", True)])
def test_prompt_cache_liga_o_cache_do_claude(ttl, extended):
    params = validate_model_params({"prompt_cache": ttl}, "anthropic")
    assert provider_kwargs("anthropic", params, "claude-sonnet-5-5") == {
        "cache_system_prompt": True,
        "extended_cache_time": extended,
    }


def test_prompt_cache_off_nao_grava_nada():
    assert validate_model_params({"prompt_cache": "off"}, "anthropic") is None


@pytest.mark.parametrize("provider", ["google", "openai", "ollama"])
def test_prompt_cache_fora_do_claude_e_recusado(provider):
    """Gemini e OpenAI já cacheiam sozinhos: aceitar e ignorar enganaria."""
    with pytest.raises(ModelParamsError, match="só vale para anthropic"):
        validate_model_params({"prompt_cache": "5m"}, provider)


def test_prompt_cache_valor_invalido():
    with pytest.raises(ModelParamsError, match="prompt_cache"):
        validate_model_params({"prompt_cache": "10m"}, "anthropic")


def test_span_do_modelo_registra_tokens_de_cache():
    entry = SimpleNamespace(
        provider="Google",
        id="gemini-2.5-flash",
        input_tokens=6000,
        output_tokens=100,
        total_tokens=6100,
        reasoning_tokens=0,
        cost=None,
        duration=1.0,
        cache_read_tokens=4096,
        cache_write_tokens=0,
    )
    [span] = _model_spans(SimpleNamespace(details={"model": [entry]}), started_at=None)
    assert span.metadata == {"cache_read_tokens": 4096}


def test_span_sem_cache_fica_sem_metadata():
    entry = SimpleNamespace(provider="Google", id="x", input_tokens=1, output_tokens=1, total_tokens=2, cost=0.0)
    [span] = _model_spans(SimpleNamespace(details={"model": [entry]}), started_at=None)
    assert span.metadata == {}
