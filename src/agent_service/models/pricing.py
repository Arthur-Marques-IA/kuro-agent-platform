"""Custo estimado de uma chamada de modelo, em USD.

Quem calculava o custo era o Langfuse. Sem ele, o Agno só informa custo para
alguns provedores (para o Gemini, não), então o trace store local estima aqui a
partir dos tokens: preço por milhão de tokens de entrada e de saída.

A tabela é uma referência de preço de lista e envelhece: confira no site do
provedor e sobrescreva com `MODEL_PRICES` (JSON), por exemplo
`MODEL_PRICES='{"gemini-2.5-flash": [0.30, 2.50]}'`. Um modelo fora da tabela
fica com custo `None` — nunca um custo inventado.

Cache de prompt: o token lido do cache custa menos que o de lista (e, no Claude,
o escrito custa mais). `CACHE_PRICES` traz o preço de leitura (e de escrita, onde o
provedor cobra) dos modelos que a tabela acima tem; sem entrada ali, o token do cache
é cobrado a preço de lista (o custo sai igual ao de antes, sem desconto). No Gemini e
no OpenAI, `input_tokens` já inclui os lidos do cache; no Claude, não: lidos e
escritos vêm à parte e somam à entrada.

No Gemini, os tokens de raciocínio ("thinking") são cobrados como saída mas não
vêm somados em `output_tokens`; nos demais, já vêm dentro.
"""

import json
import logging
from functools import lru_cache

from agent_service.config import get_settings

logger = logging.getLogger(__name__)

# USD por 1M de tokens: (entrada, saída). Preços de lista, sem lote (o cache está em CACHE_PRICES).
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.0-flash": (0.10, 0.40),
    # Gemini 3.x (ai.google.dev/gemini-api/docs/pricing, conferido em 2026-10-01).
    # 3.6/3.7/3.8 têm preço promocional até 2026-12-31 e dobram em 2027-01-01
    # (1.50 / 7.50): atualize aqui ou use MODEL_PRICES na virada.
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.7-flash": (0.75, 3.75),
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    # Faixa de prompts até 200k tokens; acima disso é 4.00 / 18.00.
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "gemini-omni-1.1-flash": (1.50, 9.00),  # saída de texto; vídeo é 17.50
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "claude-opus-4-1": (15.00, 75.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


# USD por 1M de tokens: (leitura do cache, escrita no cache ou None quando o provedor não cobra à parte).
# Modelo sem entrada aqui: cache a preço de lista (sem desconto), marcado abaixo.
CACHE_PRICES: dict[str, tuple[float, float | None]] = {
    # Gemini 2.5 e 3.x: ai.google.dev/gemini-api/docs/pricing, conferido em 2026-11-13 (10% da
    # entrada, faixa de até 200k tokens; o armazenamento por hora não entra). Os 3.6/3.7/3.8 seguem o
    # preço promocional da entrada até 2026-12-31 e dobram em 2027-01-01 junto com ela.
    "gemini-2.5-pro": (0.125, None),
    "gemini-2.5-flash": (0.03, None),
    "gemini-2.5-flash-lite": (0.01, None),
    "gemini-3.8-flash": (0.075, None),
    "gemini-3.7-flash": (0.075, None),  # a página não lista; segue o 3.6/3.8 (10% da entrada)
    "gemini-3.6-flash": (0.075, None),
    "gemini-3.5-flash-lite": (0.03, None),
    "gemini-3.1-flash-lite": (0.025, None),
    "gemini-3.1-pro-preview": (0.20, None),
    # SEM preço de cache confirmado (ficam a preço de lista): gemini-2.0-flash, gemini-3.5-flash,
    # gemini-omni-1.1-flash.
    # OpenAI (openai.com/api/pricing, listas de 2024-10 e 2025-04): leitura com desconto, escrita
    # não cobrada à parte.
    "gpt-4.1": (0.50, None),
    "gpt-4.1-mini": (0.10, None),
    "gpt-4.1-nano": (0.025, None),
    "gpt-4o": (1.25, None),
    "gpt-4o-mini": (0.075, None),
    # Claude (docs.claude.com, prompt caching: leitura 0,1x a entrada; escrita de 5 min 1,25x).
    # A escrita de 1 h custa 2x e não é distinguida aqui: `prompt_cache: 1h` sai subestimado.
    "claude-opus-4-1": (1.50, 18.75),
    "claude-sonnet-4-5": (0.30, 3.75),
    "claude-haiku-4-5": (0.10, 1.25),
}

_CACHE_APART_FROM_INPUT = ("anthropic", "claude")


@lru_cache
def _prices() -> dict[str, tuple[float, float, float | None, float | None]]:
    prices = {m: (i, o, *CACHE_PRICES.get(m, (None, None))) for m, (i, o) in DEFAULT_PRICES.items()}
    raw = get_settings().model_prices
    if raw:
        try:
            for model, p in json.loads(raw).items():
                prices[model] = (
                    float(p[0]),
                    float(p[1]),
                    float(p[2]) if len(p) > 2 and p[2] is not None else None,
                    float(p[3]) if len(p) > 3 and p[3] is not None else None,
                )
        except (ValueError, TypeError, IndexError, AttributeError):
            logger.warning('MODEL_PRICES inválido (esperado {"modelo": [entrada, saída, cache?, escrita?]}): ignorado')
    return prices


def _price_for(model_id: str) -> tuple[float, float, float | None, float | None] | None:
    prices = _prices()
    if model_id in prices:
        return prices[model_id]
    # Variantes datadas ("gpt-4.1-mini-2025-04-14", "claude-sonnet-4-5-20250929"):
    # o prefixo mais longo que casar.
    matches = [m for m in prices if model_id.startswith(m + "-")]
    return prices[max(matches, key=len)] if matches else None


def estimate_cost(
    *,
    provider: str | None,
    model_id: str | None,
    input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float | None:
    """Custo em USD. Os tokens lidos do cache saem da entrada a preço cheio e entram a preço
    de cache (`CACHE_PRICES`; sem ele, a preço de lista). A escrita só é cobrada à parte onde o
    provedor cobra (Claude)."""
    if not model_id:
        return None
    price = _price_for(model_id.lower())
    if price is None:
        return None
    in_price, out_price, read_price, write_price = price
    name = (provider or "").lower()
    billed_output = output_tokens + (reasoning_tokens if name in ("google", "gemini") else 0)
    read = max(cache_read_tokens or 0, 0)
    write = max(cache_write_tokens or 0, 0) if write_price is not None else 0
    fresh = input_tokens if name in _CACHE_APART_FROM_INPUT else max(input_tokens - read, 0)
    total = (
        fresh * in_price
        + read * (read_price if read_price is not None else in_price)
        + write * (write_price or 0)
        + billed_output * out_price
    )
    return round(total / 1_000_000, 8)
