"""Replay de uma conversa guardada: o teste de regressão dos agentes conversacionais.

O `eval` compara saídas estruturadas, então só serve para agentes `analysis`. Um agente
conversacional decide pelas tools que chama, turno a turno, e com histórico: o que ele sabe no
2º turno pode ter chegado só no 1º (o bloco de dados que o sistema integrado manda uma vez).
Por isso o replay não reenvia um turno solto: ele reenvia **todas** as falas do contato, na
ordem, numa sessão nova, para o agente montar o histórico como na conversa de verdade.

Cada turno sai com a decisão do agente (as tools chamadas e os argumentos) e a resposta. Com
`repeat`, a mesma conversa roda várias vezes (o modelo não é determinístico); com `compare_with`,
roda também em outro agente — o de produção, contra o draft. Tudo em `dry_run`: as tools
recebem `X-Kuro-Dry-Run: true` e o run fica marcado como teste.
"""

import json
import time
import uuid
from collections import Counter
from typing import Any

from agent_service.cli.chat import collect_stream
from agent_service.cli.client import ApiError, Client

MAX_CALLS = 40
"""Teto de mensagens por replay (turnos × repetições × agentes): cada uma é uma chamada ao modelo."""


class ReplayError(ValueError):
    """Conversa, agente ou parâmetros inadequados — erro de uso, não de execução."""


def similar_sessions(client: Client, session_id: str, limit: int = 5) -> list[str]:
    """Sessões com um id parecido, pelos runs gravados: o id pedido como trecho de outro (`…-r6`
    dentro de `…-r6v2`) ou o mesmo começo (o UUID antes do sufixo)."""
    found: list[str] = []
    for needle in dict.fromkeys([session_id, session_id[:36]]):
        if len(needle) < 8:
            continue
        try:
            page = client.list_runs(search=needle, limit=50)
        except ApiError:
            return found
        for run in page.get("items") or []:
            sid = run.get("session_id")
            # O `search` também casa metadata: os runs de um replay guardam o id original em
            # `replay_of`, e a sessão deles não é a que a pessoa procura.
            if sid and sid != session_id and needle in sid and sid not in found:
                found.append(sid)
        if found:
            break
    return found[:limit]


def not_found_hint(client: Client, session_id: str) -> str:
    similar = similar_sessions(client, session_id)
    return f" Parecidas: {', '.join(similar)}." if similar else ""


def _tool_calls(trace: dict[str, Any]) -> list[dict[str, Any]]:
    calls = []
    for span in trace.get("spans") or []:
        if span.get("type") == "TOOL":
            calls.append({"name": span.get("name"), "args": span.get("input"), "error": span.get("status_message")})
    return calls


def _dependencies(trace: dict[str, Any]) -> dict[str, Any]:
    """As `dependencies` que o sistema integrado mandou: ficam no input da span raiz."""
    for span in trace.get("spans") or []:
        if span.get("parent_id") is None and isinstance(span.get("input"), dict):
            return dict(span["input"].get("dependencies") or {})
    return {}


def session_turns(client: Client, session_id: str) -> list[dict[str, Any]]:
    """Os turnos de uma conversa, do mais antigo ao mais novo: a fala do contato como chegou
    (`message`), as `dependencies`, as tools chamadas e a resposta. Vem do trace store, que
    guarda a mensagem inteira — inclusive o bloco de dados que o sistema integrado manda."""
    runs: list[dict[str, Any]] = []
    cursor = None
    while True:
        page = client.list_runs(session_id=session_id, limit=100, cursor=cursor)
        runs += page.get("items") or []
        cursor = page.get("next_cursor")
        if not cursor:
            break
    turns = []
    for run in sorted(runs, key=lambda r: r.get("started_at") or ""):
        trace = client.run_trace(run["run_id"])
        detail = trace.get("run") or {}
        turns.append({
            "run_id": run["run_id"],
            "agent_type": run.get("agent_type"),
            "message": detail.get("message") or run.get("message") or "",
            "dependencies": _dependencies(trace),
            "tools": _tool_calls(trace),
            "output": detail.get("output") or "",
            "error": detail.get("status_message"),
        })
    return turns


def _trace(client: Client, run_id: str, wait: float = 15.0) -> dict[str, Any]:
    """O trace de um run que acabou de terminar: a gravação sai do caminho da resposta, então
    ele pode levar um instante para aparecer."""
    deadline = time.monotonic() + wait
    while True:
        try:
            return client.run_trace(run_id)
        except ApiError as exc:
            if exc.status != 404 or time.monotonic() > deadline:
                return {}
            time.sleep(0.5)


def _dig(value: Any, path: list[str]) -> Any:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def decision(tools: list[dict[str, Any]], focus: list[str] | None) -> str:
    """Um resumo comparável do que o agente decidiu no turno. Sem `focus`, a sequência de tools;
    com `focus` (`tool.arg`, `tool.arg.sub`), o valor desses argumentos na última chamada de cada
    tool — o que separa "respondeu" de "efetivou o acordo" sem se perder no texto livre."""
    if not focus:
        return " → ".join(t["name"] for t in tools) or "(nenhuma tool)"
    picked = {}
    for path in focus:
        name, *rest = path.split(".")
        calls = [t for t in tools if t["name"] == name]
        if not calls:
            picked[path] = None
            continue
        picked[path] = _dig(calls[-1]["args"], rest) if rest else True
    return json.dumps(picked, ensure_ascii=False, sort_keys=True)


def _short(text: Any, limit: int = 300) -> Any:
    if isinstance(text, str) and len(text) > limit:
        return text[:limit] + "…"
    return text


def replay(
    client: Client,
    session_id: str,
    *,
    agent_type: str | None = None,
    compare_with: str | None = None,
    repeat: int = 1,
    turns: int | None = None,
    focus: list[str] | None = None,
    dependencies: dict[str, Any] | None = None,
    user_id: str = "replay",
) -> dict[str, Any]:
    """Reenvia a conversa `session_id` a `agent_type` (padrão: o agente da conversa) e, se
    pedido, a `compare_with`, `repeat` vezes cada, numa sessão nova por vez. `dependencies`
    sobrepõe as da conversa original. Devolve, por turno, a decisão original e a contagem das
    decisões de cada agente."""
    if not 1 <= repeat <= 10:
        raise ReplayError("repeat vai de 1 a 10")
    original = session_turns(client, session_id)
    if not original:
        raise ReplayError(f"nenhuma execução gravada na conversa {session_id!r}.{not_found_hint(client, session_id)}")
    if turns is not None:
        if turns < 1:
            raise ReplayError("turns precisa ser 1 ou mais")
        original = original[:turns]
    source = original[0]["agent_type"]
    agents = [agent_type or source] + ([compare_with] if compare_with else [])
    if len(set(agents)) != len(agents):
        raise ReplayError("compare_with precisa ser outro agente")
    calls = len(original) * repeat * len(agents)
    if calls > MAX_CALLS:
        raise ReplayError(
            f"isso daria {calls} mensagens ({len(original)} turnos × {repeat} repetições × {len(agents)} agentes); "
            f"o teto é {MAX_CALLS} — use turns ou menos repetições"
        )
    for name in agents:
        kind = client.get_agent(name).get("kind") or "conversational"
        if kind == "analysis":
            raise ReplayError(f"{name!r} é kind=analysis, sem conversa: use o eval (runs_export vira o dataset)")

    report: dict[str, Any] = {
        "session_id": session_id,
        "source_agent": source,
        "turns": len(original),
        "repeat": repeat,
        "focus": focus,
        "original": [
            {
                "turn": i + 1,
                "run_id": t["run_id"],
                "message": _short(t["message"]),
                "decision": decision(t["tools"], focus),
                "output": _short(t["output"]),
            }
            for i, t in enumerate(original)
        ],
        "agents": {},
    }
    for name in agents:
        rounds = []
        for _ in range(repeat):
            sid = f"replay-{uuid.uuid4().hex[:12]}"
            steps = []
            for i, turn in enumerate(original):
                deps = {**turn["dependencies"], **(dependencies or {})}
                body = {
                    "agent_type": name,
                    "user_id": user_id,
                    "session_id": sid,
                    "message": turn["message"],
                    "dependencies": deps or None,
                    "dry_run": True,
                    "metadata": {"replay_of": session_id, "replay_turn": str(i + 1)},
                }
                result = collect_stream(client, body)
                tools = _tool_calls(_trace(client, result["run_id"])) if result["run_id"] else []
                steps.append({
                    "turn": i + 1,
                    "run_id": result["run_id"],
                    "decision": decision(tools, focus),
                    "tools": tools,
                    "output": _short(result["content"]),
                    "error": result["error"],
                })
            rounds.append({"session_id": sid, "turns": steps})
        summary = []
        for i, orig in enumerate(report["original"]):
            counts = Counter(r["turns"][i]["decision"] for r in rounds)
            summary.append({
                "turn": i + 1,
                "original": orig["decision"],
                "decisions": dict(counts.most_common()),
                "same_as_original": counts.get(orig["decision"], 0),
            })
        report["agents"][name] = {"summary": summary, "rounds": rounds}
    return report
