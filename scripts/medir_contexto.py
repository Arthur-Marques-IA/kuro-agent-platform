"""Mede quanto texto o modelo recebe por turno numa conversa com entrada estruturada.

Uma sessão de 12 turnos de um agente conversacional (histórico de 10 runs, o padrão),
com um contexto de ~7 mil caracteres mandado no turno 1 e alterado no turno 6, e
eventos e falas curtos — o formato de uma integração como a do Regente. Para cada
turno, soma o tamanho (em caracteres) de todas as mensagens da chamada ao modelo:
prompt de sistema, histórico e mensagem nova.

Dois cenários de chamada:

- `mudancas`: `context` só nos turnos em que ele muda (1 e 6), ausente nos outros;
- `todo_turno`: `context` inteiro em todo turno, como a integração da 0.3.0 faz.

O agente é o de verdade (`agents/base.py::build_agent`), passando pela rota `/chat`
(`api/routes.py::chat`), com o histórico numa sessão do Agno em SQLite e um modelo
falso que só guarda o que recebeu. Nada vai para a rede.

    uv run python scripts/medir_contexto.py

Roda igual num checkout de outra versão (é como se mede o "antes"): aponte o
`PYTHONPATH` para o `src` dela. `tests/test_medir_contexto.py` chama `medir` daqui.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator, Iterator
from uuid import uuid4

TURNOS = 12
TURNO_DA_MUDANCA = 6


def _bloco(rotulo: str, tamanho: int) -> str:
    linha = f"{rotulo}: dado de exemplo com algum texto para ocupar espaço no prompt. "
    return (linha * (tamanho // len(linha) + 1))[:tamanho]


def contexto(versao: int) -> list[dict[str, Any]]:
    """~7 mil caracteres em quatro blocos; a versão 2 muda o da dívida."""
    divida = f"[divida v{versao}] " + _bloco("SEM parcela vencida" if versao == 1 else "1 parcela vencida", 2985)
    return [
        {"key": "configuracao", "title": "Configuração desta conversa", "content": _bloco("EFETIVAÇÃO", 500)},
        {"key": "dados_aluno", "title": "Dados do aluno", "content": _bloco("Nome: Maria", 1000)},
        {"key": "divida", "title": "Contexto da dívida", "content": divida},
        {"key": "anotacoes", "title": "Anotações internas", "content": _bloco("Anotação", 2500)},
    ]


def _modelo_que_guarda() -> Any:
    from agno.metrics import MessageMetrics
    from agno.models.base import Model
    from agno.models.message import Message
    from agno.models.response import ModelResponse

    class ModeloQueGuarda(Model):
        name: str = "ModeloQueGuarda"
        provider: str = "teste"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.chamadas: list[list[Any]] = []

        def _resposta(self, messages: list[Message]) -> ModelResponse:
            self.chamadas.append([(m.role, str(m.content or "")) for m in messages])
            response = ModelResponse(role="assistant", content="Certo, Maria. Vou verificar isso para você agora.")
            response.response_usage = MessageMetrics(input_tokens=1, output_tokens=1, total_tokens=2)
            return response

        def invoke(self, messages: list[Message], assistant_message: Message, **kwargs: Any) -> ModelResponse:
            return self._resposta(messages)

        async def ainvoke(self, messages: list[Message], assistant_message: Message, **kwargs: Any) -> ModelResponse:
            return self._resposta(messages)

        def invoke_stream(self, messages: list[Message], assistant_message: Message, **kwargs: Any) -> Iterator[Any]:
            yield self._resposta(messages)

        async def ainvoke_stream(
            self, messages: list[Message], assistant_message: Message, **kwargs: Any
        ) -> AsyncIterator[Any]:
            yield self._resposta(messages)

        def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
            return response

        def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
            return response

    return ModeloQueGuarda(id="modelo-que-guarda")


def medir(cenario: str, pasta: Path) -> list[dict[str, int]]:
    """Roda a sessão e devolve, por turno, `{"total", "sistema", "historico", "mensagem"}`
    (caracteres) e `"copias"`: quantas vezes o bloco da dívida aparece no prompt.

    Supõe o serviço já preparado (banco migrado, gravação síncrona do trace)."""
    from agno.db.sqlite import SqliteDb

    from agent_service.agents import base
    from agent_service.api import routes

    modelo = _modelo_que_guarda()
    banco = SqliteDb(db_url=f"sqlite:///{(pasta / f'agno-{uuid4().hex[:8]}.db').as_posix()}")
    originais = (base.get_db, base.get_model, routes.get_agent_with_definition)
    base.get_db = lambda: banco  # type: ignore[assignment]
    base.get_model = lambda **_: modelo  # type: ignore[assignment]
    agent = base.build_agent(agent_id="medicao", name="Medição", instructions=["Você atende alunos. Responda curto."])
    definicao = {"kind": "conversational", "name": "Medição", "prompt_version": 1, "dependency_fields": []}
    routes.get_agent_with_definition = lambda agent_type: (agent, definicao)  # type: ignore[assignment]

    sid = f"medicao-{uuid4().hex[:8]}"
    resultado = []
    try:
        ultima_resposta: str | None = None
        for turno in range(1, TURNOS + 1):
            versao = 1 if turno < TURNO_DA_MUDANCA else 2
            corpo: dict[str, Any] = {
                "agent_type": "medicao",
                "user_id": "aluno",
                "session_id": sid,
                "message": f"mensagem curta do aluno no turno {turno}",
                "events": [{"role": "contact", "content": f"oi, é sobre a mensalidade (turno {turno})"}],
            }
            if ultima_resposta is not None:
                corpo["events"].insert(0, {"role": "agent_sent", "content": ultima_resposta})
            if cenario == "todo_turno" or turno in (1, TURNO_DA_MUDANCA):
                corpo["context"] = contexto(versao)
            resposta = asyncio.run(routes.chat(routes.ChatRequest.model_validate(corpo)))
            ultima_resposta = resposta.content
            chamada = modelo.chamadas[-1]
            sistema = sum(len(c) for papel, c in chamada if papel == "system")
            mensagem = len(chamada[-1][1])
            total = sum(len(c) for _, c in chamada)
            divida = f"[divida v{versao}]"
            antiga = "[divida v1]"
            resultado.append(
                {
                    "turno": turno,
                    "total": total,
                    "sistema": sistema,
                    "historico": total - sistema - mensagem,
                    "mensagem": mensagem,
                    "copias": sum(c.count(divida) for _, c in chamada),
                    "copias_antigas": sum(c.count(antiga) for _, c in chamada) if versao == 2 else 0,
                }
            )
    finally:
        base.get_db, base.get_model, routes.get_agent_with_definition = originais  # type: ignore[assignment]
    return resultado


def preparar() -> Path:
    """Ambiente de teste (SQLite descartável, sem rede), antes de importar o serviço."""
    pasta = Path(tempfile.mkdtemp(prefix="kuro-medicao-"))
    from cryptography.fernet import Fernet

    os.environ.update(
        {
            "DATABASE_URL": f"sqlite:///{(pasta / 'agent_service.db').as_posix()}",
            "CREDENTIALS_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "GOOGLE_API_KEY": "chave-de-teste",
            "ALLOW_OPEN": "1",
            "LANGFUSE_ENABLED": "false",
            "AGNO_TELEMETRY": "false",
        }
    )
    os.chdir(pasta)
    from agent_service.migrations import upgrade_database
    from agent_service.observability import run_store

    upgrade_database()
    run_store._submit = run_store.write_now  # type: ignore[assignment]
    return pasta


def main() -> None:
    pasta = preparar()
    print(f"# agent_service de {Path(sys.modules['agent_service'].__file__ or '').parent}")
    for cenario in ("mudancas", "todo_turno"):
        print(f"\n## cenário {cenario}")
        print("turno | total | sistema | histórico | mensagem | cópias do bloco | cópias do bloco antigo")
        for r in medir(cenario, pasta):
            print(
                f"{r['turno']:>5} | {r['total']:>6} | {r['sistema']:>7} | {r['historico']:>9} | "
                f"{r['mensagem']:>8} | {r['copias']:>15} | {r['copias_antigas']:>22}"
            )


if __name__ == "__main__":
    main()
