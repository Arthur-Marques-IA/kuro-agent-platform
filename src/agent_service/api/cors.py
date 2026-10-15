"""Quais sites, rodando no navegador de alguém, podem chamar a API (`CORS_ORIGINS`).

Quase ninguém precisa disto: o console fala com a API pelo próprio servidor (o BFF do
Next.js), a CLI, o MCP e os sistemas integrados não são navegadores. Quem precisa é uma
página aberta em outro domínio chamando a API direto — o playground do Agno
(`os.agno.com`) ou um front seu.

- **Vazio (padrão):** a lista do AgentOS — `http://localhost:3000` e os domínios do Agno
  (`agno.com`, `app.agno.com`, `os.agno.com`, `os-stg.agno.com`). É o que valia antes de a
  variável existir, para não quebrar quem usa o playground.
- **`none`:** nenhuma origem. Um navegador em outro domínio não lê resposta nenhuma.
- **Uma lista** (`https://painel.exemplo.com,https://os.agno.com`): exatamente essas.

`*` é recusado na subida: com `allow_credentials` (o AgentOS liga), o navegador não aceita
`*`, e o AgentOS o descartava calado — quem configurava achava que tinha liberado tudo.

A chave de API continua valendo: CORS só decide se o navegador entrega a resposta à página,
não quem pode chamar.
"""

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

AGNO_DEFAULT = None
"""`cors_allowed_origins=None` no AgentOS = a lista padrão dele."""


class CorsConfigError(ValueError):
    pass


def parse_origins(value: str | None) -> list[str] | None:
    """`None` = o padrão do AgentOS; `[]` = nenhuma origem; senão, a lista pedida."""
    text = (value or "").strip()
    if not text:
        return AGNO_DEFAULT
    if text.lower() == "none":
        return []
    origins = [o.strip().rstrip("/") for o in text.split(",") if o.strip()]
    for origin in origins:
        if origin == "*":
            raise CorsConfigError(
                "CORS_ORIGINS=* não vale: com credenciais o navegador recusa `*`. Liste as origens "
                "(https://painel.exemplo.com,https://os.agno.com) ou use `none`."
            )
        if not origin.startswith(("http://", "https://")) or "/" in origin.split("://", 1)[1]:
            raise CorsConfigError(
                f"CORS_ORIGINS: {origin!r} não é uma origem — esquema e host, sem caminho "
                "(ex.: https://painel.exemplo.com ou http://localhost:3000)."
            )
    return origins


def enforce(app: FastAPI, origins: list[str] | None) -> None:
    """Deixa o CORS do app com exatamente `origins` — o AgentOS cai na lista padrão dele quando
    recebe uma lista vazia, então o `none` só se cumpre trocando a configuração depois."""
    if origins is None:
        return
    for middleware in app.user_middleware:
        if middleware.cls is CORSMiddleware:
            middleware.kwargs["allow_origins"] = list(origins)
    app.middleware_stack = None


def allowed_origins(app: FastAPI) -> list[str]:
    for middleware in app.user_middleware:
        if middleware.cls is CORSMiddleware:
            return list(middleware.kwargs.get("allow_origins") or [])
    return []
