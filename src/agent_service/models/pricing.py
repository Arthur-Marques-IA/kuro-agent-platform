"""Custo estimado de uma chamada de modelo, em USD.

Quem calculava o custo era o Langfuse. Sem ele, o Agno só informa custo para
alguns provedores (para o Gemini, não), então o trace store local estima aqui a
partir dos tokens: preço por milhão de tokens de entrada e de saída.

A tabela é uma referência de preço de lista e envelhece: confira no site do
provedor e sobrescreva com `MODEL_PRICES` (JSON), por exemplo
`MODEL_PRICES='{"gemini-2.5-flash": [0.30, 2.50]}'`. Um modelo fora da tabela
fica com custo `None` — nunca um custo inventado.

No Gemini, os tokens de raciocínio ("thinking") são cobrados como saída mas não
vêm somados em `output_tokens`; nos demais, já vêm dentro.
"""

import json
import logging
from functools import lru_cache

from agent_service.config import get_settings

logger = logging.getLogger(__name__)

# USD por 1M de tokens: (entrada, saída). Preços de lista, sem cache nem lote.
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


@lru_cache
def _prices() -> dict[str, tuple[float, float]]:
    prices = dict(DEFAULT_PRICES)
    raw = get_settings().model_prices
    if raw:
        try:
            prices.update({model: (float(p[0]), float(p[1])) for model, p in json.loads(raw).items()})
        except (ValueError, TypeError, IndexError, AttributeError):
            logger.warning('MODEL_PRICES inválido (esperado {"modelo": [entrada, saída]}): ignorado')
    return prices


def _price_for(model_id: str) -> tuple[float, float] | None:
    prices = _prices()
    if model_id in prices:
        return prices[model_id]
    # Variantes datadas ("gpt-4.1-mini-2025-04-14", "claude-sonnet-4-5-20250929"):
    # o prefixo mais longo que casar.
    matches = [m for m in prices if model_id.startswith(m + "-")]
    return prices[max(matches, key=len)] if matches else None


def estimate_cost(
    *, provider: str | None, model_id: str | None, input_tokens: int, output_tokens: int, reasoning_tokens: int = 0
) -> float | None:
    if not model_id:
        return None
    price = _price_for(model_id.lower())
    if price is None:
        return None
    billed_output = output_tokens + (reasoning_tokens if (provider or "").lower() in ("google", "gemini") else 0)
    return round((input_tokens * price[0] + billed_output * price[1]) / 1_000_000, 8)
