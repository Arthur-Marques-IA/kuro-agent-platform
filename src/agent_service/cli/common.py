"""Contexto, saída e erros compartilhados pelos comandos.

Convenções (documentadas em AGENTS.md):
- `--json`: stdout só com JSON; mensagens/erros vão para stderr.
- Códigos de saída: 0 ok, 1 operação falhou (erro da API, tool com
  `ok: false`, erro no stream do chat), 2 uso incorreto, 3 serviço inacessível.
- Prompts só aparecem com TTY e sem `--no-input`; sem isso, argumento
  obrigatório ausente é erro — nunca trava esperando entrada.
"""

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.console import Console

from agent_service.cli.client import ApiError, Client, PortBlocked, ServiceUnavailable, TlsError
from agent_service.cli.connection import AUTH_HINT

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3

console = Console()
err_console = Console(stderr=True)


@dataclass
class State:
    client: Client
    json_mode: bool
    no_input: bool

    @property
    def interactive(self) -> bool:
        return not self.no_input and not self.json_mode and sys.stdin.isatty() and sys.stdout.isatty()


def state(ctx: typer.Context) -> State:
    return ctx.find_root().obj


def emit(st: State, data: Any, render: Callable[[Any], None] | None = None) -> None:
    if st.json_mode or render is None:
        print_json(data)
    else:
        render(data)


def print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")
    sys.stdout.flush()


def fail(st: State | None, message: str, code: int = EXIT_FAILED, **extra: Any) -> NoReturn:
    if st is not None and st.json_mode:
        sys.stderr.write(json.dumps({"error": message, **extra}, ensure_ascii=False, default=str) + "\n")
    else:
        err_console.print(f"[bold red]erro:[/] {message}", highlight=False)
    raise typer.Exit(code)


def call(st: State, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Executa uma chamada do client traduzindo falhas em mensagem + código de saída."""
    try:
        return fn(*args, **kwargs)
    except (ServiceUnavailable, ApiError) as exc:
        fail_from(st, exc)


def fail_from(st: State, exc: ServiceUnavailable | ApiError) -> NoReturn:
    if isinstance(exc, (TlsError, PortBlocked)):
        # O serviço está de pé (ou pode estar): sugerir `docker compose up -d` aqui
        # seria apontar para o lugar errado.
        fail(st, str(exc), EXIT_UNAVAILABLE)
    if isinstance(exc, ServiceUnavailable):
        fail(st, f"{exc}. O serviço está no ar? (docker compose up -d)", EXIT_UNAVAILABLE)
    fail(st, _api_message(exc), EXIT_FAILED, status=exc.status, detail=exc.detail)


def _api_message(exc: ApiError) -> str:
    detail = exc.detail
    if isinstance(detail, list):  # erros de validação do FastAPI
        detail = "; ".join(
            f"{'.'.join(str(p) for p in d.get('loc', [])[1:]) or 'body'}: {d.get('msg')}" if isinstance(d, dict) else str(d)
            for d in detail
        )
    if exc.status == 401:
        return f"{detail} (HTTP 401) — {AUTH_HINT}"
    return f"{detail} (HTTP {exc.status})"


def parse_pairs(st: State, pairs: list[str], option: str) -> dict[str, Any]:
    """`chave=valor` repetível; o valor é lido como JSON quando possível
    (`n=3`, `ativo=true`, `tags=["a"]`), senão fica string."""
    result: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            fail(st, f"{option} espera chave=valor, recebi {pair!r}", EXIT_USAGE)
        try:
            result[key] = json.loads(raw)
        except ValueError:
            result[key] = raw
    return result


def parse_json_object(st: State, raw: str, option: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except ValueError as exc:
        fail(st, f"{option} não é JSON válido: {exc}", EXIT_USAGE)
    if not isinstance(value, dict):
        fail(st, f"{option} deve ser um objeto JSON", EXIT_USAGE)
    return value


def read_json_file(st: State, path: str) -> dict[str, Any]:
    """Lê um objeto JSON de um arquivo, ou de stdin com `-`."""
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        fail(st, f"não consegui ler {path}: {exc}", EXIT_USAGE)
    return parse_json_object(st, raw, path)
