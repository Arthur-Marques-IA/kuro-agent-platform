"""Redação por chave do que vai para o trace (`TRACE_REDACT_KEYS`).

Na gravação, o valor de toda chave listada vira `[redigido]` — em qualquer
profundidade de dicts e listas, e dentro de strings que são JSON (o resultado de
uma tool de API costuma ser o corpo da resposta como texto). Comparação exata do
nome da chave, sem diferenciar maiúsculas.

Não é varredura de texto livre: o CPF que o contato digitou na mensagem continua
na mensagem. E só o que é **persistido** muda — as funções aqui sempre devolvem uma
cópia e nunca mexem no objeto recebido, que é o mesmo que o modelo e as tools usam.

Os pontos de aplicação são `run_store._write_run` (o trace store local) e o
`mask`/`mask_otel_spans` do cliente do Langfuse (`tracing.configure_tracing`).
"""

import json
import logging
from typing import Any

from agent_service.config import get_settings

logger = logging.getLogger(__name__)

REDACTED = "[redigido]"


def redact_keys() -> frozenset[str]:
    """As chaves de `TRACE_REDACT_KEYS`, em minúsculas. Lida a cada chamada: muda sem reiniciar nos testes."""
    raw = get_settings().trace_redact_keys or ""
    return frozenset(k.strip().lower() for k in raw.split(",") if k.strip())


def redact(value: Any, keys: frozenset[str] | None = None) -> Any:
    """Cópia de `value` com as chaves de `keys` mascaradas, recursivamente."""
    keys = redact_keys() if keys is None else keys
    if not keys:
        return value
    return _redact(value, keys)


def _redact(value: Any, keys: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and k.lower() in keys else _redact(v, keys)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v, keys) for v in value]
    if isinstance(value, str):
        return _redact_json_text(value, keys)
    return value


def _redact_json_text(text: str, keys: frozenset[str]) -> str:
    """Uma string que é um objeto/array JSON volta redigida e re-serializada; qualquer
    outra (ou um JSON sem nenhuma chave a mascarar) volta idêntica."""
    stripped = text.strip()
    if stripped[:1] not in ("{", "["):
        return text
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return text
    redacted = _redact(parsed, keys)
    if redacted == parsed:
        return text
    return json.dumps(redacted, ensure_ascii=False)


# -- Langfuse --------------------------------------------------------------------


def langfuse_mask(*, data: Any, **_: Any) -> Any:
    """`mask` do cliente do Langfuse: o que o Kuro manda pela API do SDK (a entrada da
    observação raiz, com `dependencies`, e o `update` da saída). Nunca levanta."""
    try:
        return redact(data)
    except Exception:
        logger.warning("Redação do trace para o Langfuse falhou; o valor foi trocado pelo marcador", exc_info=True)
        return REDACTED


def langfuse_mask_otel_spans(*, params: Any) -> Any:
    """`mask_otel_spans` do cliente do Langfuse: os spans do instrumentor do Agno (tool
    calls, chamadas ao modelo). Atributo cujo último segmento (`a.b.cpf`) é uma chave
    listada vira o marcador; atributo string que é JSON é redigido por dentro. O texto
    livre das mensagens ao modelo não muda. Nunca levanta: uma exceção aqui faria o
    Langfuse descartar o lote inteiro."""
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    keys = redact_keys()
    if not keys:
        return None
    patches: dict[Any, Any] = {}
    for identifier, span in params.spans.items():
        try:
            changed: dict[str, Any] = {}
            for name, value in span.attributes.items():
                if name.rsplit(".", 1)[-1].lower() in keys:
                    changed[name] = REDACTED
                elif isinstance(value, str):
                    redacted = _redact_json_text(value, keys)
                    if redacted is not value and redacted != value:
                        changed[name] = redacted
            if changed:
                patches[identifier] = OtelSpanPatch(set_attributes=changed)
        except Exception:
            logger.warning("Redação de um span para o Langfuse falhou; o span vai sem os atributos", exc_info=True)
            patches[identifier] = OtelSpanPatch(delete_attributes=tuple(span.attributes))
    return MaskOtelSpansResult(span_patches=patches)
