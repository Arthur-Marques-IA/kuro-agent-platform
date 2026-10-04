"""Erro do provedor do modelo numa frase, para quem chama a API.

Uma falha do Gemini chegava ao `detail` do 502 como o corpo inteiro da resposta
dele, em JSON de várias linhas (`{"error": {"code": 503, "message": ...}}`).
Quem integra precisa de uma linha que diga o que houve e se vale tentar de novo;
o texto original continua no trace do run.
"""

import json
import re
from typing import Any

_TRANSIENT = {408, 429, 500, 502, 503, 504}


def _first_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    while start != -1:
        depth = 0
        for end in range(start, len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start : end + 1])
                    except ValueError:
                        break
                    return value if isinstance(value, dict) else None
        start = text.find("{", start + 1)
    return None


def friendly_model_error(raw: Any, *, prefix: str | None = None) -> str:
    """Com o JSON de erro do provedor: "<prefixo>: o provedor respondeu 503 (UNAVAILABLE):
    ...". Sem ele, a primeira linha do texto, como veio (com o prefixo só se pedido)."""
    text = str(raw or "").strip()
    payload = _first_json_object(text) if text else None
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        code = error.get("code")
        status = error.get("status")
        message = " ".join(str(error.get("message") or "").split())
        detail = f"o provedor respondeu {code}" + (f" ({status})" if status else "")
        if message:
            detail += f": {message[:200].rstrip('.')}"
        if isinstance(code, int) and code in _TRANSIENT:
            detail += " — costuma ser passageiro, tente de novo em instantes"
        return f"{prefix or 'Falha no modelo'}: {detail}."
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = re.sub(r"\s+", " ", line)[:300]
    if not line:
        return f"{prefix}." if prefix else ""
    return f"{prefix}: {line}" if prefix else line
