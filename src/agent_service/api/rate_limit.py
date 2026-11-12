"""Limite de requisições por chave no `/chat`, `/chat/stream` e `/analyze`.

`RATE_LIMIT_PER_MINUTE` (vazio ou 0 = desligado, o padrão) é quantas chamadas uma
mesma chave de API pode fazer nessas rotas em qualquer janela de 60 s. Passou disso:
429 com `Retry-After` (os segundos até a chamada mais antiga da janela sair dela) e
o corpo no padrão de erro do serviço, `{"detail", "error": "rate_limited"}`.

Quem conta:

- com autenticação ligada, a chave apresentada (guardada como hash, nunca crua). Cada
  chave tem a sua cota, e `admin` e `runtime` não dividem nada — mas o console usa a
  chave admin para todo mundo que está nele, então divide uma cota só;
- com o serviço aberto (sem chave configurada), o IP de quem conectou. Atrás de um
  proxy (o Caddy dos profiles `tls` e `ip`) esse IP é o do proxy: todo o tráfego
  anônimo cai numa cota só. `X-Forwarded-For` é ignorado de propósito, porque quem
  chama escreve o que quiser nele.

**Por processo e em memória.** A contagem não é compartilhada entre workers nem
réplicas (N processos = N vezes o limite) e zera num restart. É uma trava contra um
cliente em loop gastando token, não uma cota contratual.

Fica numa dependência das três rotas, e não no middleware de autenticação: a regra é
só delas, e o middleware vale para o app inteiro (inclusive as rotas do AgentOS).
A dependência roda antes de a resposta existir, então o `/chat/stream` também devolve
um 429 de verdade, não um evento `error` no meio do stream.
"""

import hashlib
import math
import time
from collections import deque
from collections.abc import Callable

from fastapi import Request

from agent_service.api import auth
from agent_service.config import get_settings

WINDOW_SECONDS = 60.0


class RateLimitedError(Exception):
    """A chave passou do limite; `retry_after` em segundos inteiros (>= 1)."""

    def __init__(self, limit: int, retry_after: int) -> None:
        self.limit = limit
        self.retry_after = retry_after
        super().__init__(
            f"Limite de {limit} requisições por minuto atingido para esta chave "
            f"(RATE_LIMIT_PER_MINUTE). Tente de novo em {retry_after} s."
        )


class RateLimiter:
    """Janela deslizante: guarda o instante de cada chamada aceita, por chave, e
    descarta os que já saíram da janela. `clock` é injetável para os testes."""

    def __init__(self, limit: int, window: float = WINDOW_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}

    def hit(self, bucket: str) -> None:
        """Conta uma chamada de `bucket`, ou levanta `RateLimitedError` sem contar.

        Síncrono e sem `await` no meio: chamado do event loop, não precisa de lock."""
        now = self.clock()
        self._expire(now)
        hits = self._hits.setdefault(bucket, deque())
        if len(hits) >= self.limit:
            retry_after = max(1, math.ceil(hits[0] + self.window - now))
            raise RateLimitedError(self.limit, retry_after)
        hits.append(now)

    def _expire(self, now: float) -> None:
        """Tira de todas as chaves o que saiu da janela e apaga as que ficaram vazias —
        sem isso, uma rajada de IPs diferentes faria o dicionário crescer para sempre."""
        limite = now - self.window
        for bucket in list(self._hits):
            hits = self._hits[bucket]
            while hits and hits[0] <= limite:
                hits.popleft()
            if not hits:
                del self._hits[bucket]


_limiter: RateLimiter | None = None


def _get_limiter(limit: int) -> RateLimiter:
    """O limitador do processo; refeito se o limite mudar (testes, settings recarregadas)."""
    global _limiter
    if _limiter is None or _limiter.limit != limit:
        _limiter = RateLimiter(limit)
    return _limiter


def reset() -> None:
    """Esquece a contagem (testes)."""
    global _limiter
    _limiter = None


def bucket_of(request: Request) -> str:
    """De quem é a chamada: o hash da chave apresentada ou, sem chave, o IP."""
    key = auth._presented_key(request) if auth.auth_enabled() else None
    if key:
        return "key:" + hashlib.sha256(key.encode()).hexdigest()
    host = request.client.host if request.client else "anonimo"
    return f"ip:{host}"


async def check_rate_limit(request: Request) -> None:
    """Dependência das rotas de execução. `async` de propósito: roda no event loop,
    não numa thread do pool, então o contador não precisa de lock."""
    limit = get_settings().rate_limit_per_minute
    if not limit or limit <= 0:
        return
    _get_limiter(limit).hit(bucket_of(request))
