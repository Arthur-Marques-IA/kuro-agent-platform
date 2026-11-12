"""Erros de configuração do serviço viram 503 com o motivo, não 500 cru.

Sem chave de cifragem ou sem credencial do provedor, qualquer rota que monte um
modelo (`/chat`, `/analyze`, feedback) ou que grave uma chave
(`/model-credentials`) falhava com um "Internal Server Error" sem corpo útil — o
motivo só aparecia no log do container. É problema de quem opera o serviço, não
de quem chama: 503, e a mensagem diz o que configurar.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent_service.api.rate_limit import RateLimitedError
from agent_service.models.crypto import EncryptionNotConfiguredError
from agent_service.models.provider import ProviderNotConfiguredError

_ERROR_KIND = {
    EncryptionNotConfiguredError: "encryption_not_configured",
    ProviderNotConfiguredError: "model_provider_not_configured",
}


def _handler(error: str):  # noqa: ANN202
    async def handle(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc), "error": error})

    return handle


async def _rate_limited(request: Request, exc: RateLimitedError) -> JSONResponse:
    """429 no mesmo formato, com `Retry-After` (ver `api/rate_limit.py`)."""
    return JSONResponse(
        status_code=429,
        content={"detail": str(exc), "error": "rate_limited"},
        headers={"Retry-After": str(exc.retry_after)},
    )


def install(app: FastAPI) -> None:
    for exc_type, error in _ERROR_KIND.items():
        app.add_exception_handler(exc_type, _handler(error))
    app.add_exception_handler(RateLimitedError, _rate_limited)
