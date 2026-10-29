"""A conversa como gente lê: uma linha por turno, sem o `RunOutput` cru do Agno.

O `/sessions/{id}/runs` do AgentOS devolve cada run inteiro — o prompt de sistema,
a mensagem do usuário duas vezes (`run_input` e `messages`), cada tool duas vezes
(`tools` e a mensagem `role=tool`), `thought_signature`, dezenas de campos nulos e
horários em epoch. Numa sessão do R6 isso é ~20 mil caracteres por turno, e o que
importa (o que o contato disse, o que o agente decidiu, o que ele respondeu) some
no meio. Aqui cada run vira:

- `at` (no fuso de quem opera), modelo, tokens, custo de tempo;
- `fala`: o que o contato disse neste turno;
- `contexto`: o bloco que o sistema chamador mandou junto — só quando MUDOU desde o
  turno anterior (`contexto_igual_ao_anterior` nos outros);
- `passos`: cada tool numa linha, `nome(arg=valor, …) → resultado`;
- `resposta`: o texto final.

E a sessão traz o que o agente "lembra": o resumo da sessão e as memórias do contato.
"""

import hashlib
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from agent_service.observability.overview import DEFAULT_TIMEZONE

FALA_MARKERS = ("[FALA DO CONTATO]", "ÚLTIMA MENSAGEM DO ALUNO:", "ÚLTIMA MENSAGEM DO CONTATO:")
"""Onde termina o contexto e começa a fala, nas mensagens que trazem os dois juntos.
Sem nenhum desses marcadores, a mensagem inteira é a fala."""

TOOL_ARG_MAX = 160
TOOL_RESULT_MAX = 240


def _at(epoch: Any, tz: ZoneInfo) -> str | None:
    if not isinstance(epoch, (int, float)):
        return None
    return datetime.fromtimestamp(epoch, tz).strftime("%d/%m/%Y %H:%M:%S")


def _short(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def split_input(text: str) -> tuple[str, str]:
    """(contexto, fala). O último marcador vence: o histórico citado no contexto pode
    repetir o rótulo."""
    best = -1
    marker = ""
    for m in FALA_MARKERS:
        pos = text.rfind(m)
        if pos > best:
            best, marker = pos, m
    if best < 0:
        return "", text.strip()
    return text[:best].strip(), text[best + len(marker) :].strip()


def _input_text(run: dict[str, Any]) -> str:
    raw = run.get("input")
    if isinstance(raw, dict):
        raw = raw.get("input_content")
    if raw is None:
        raw = run.get("run_input")
    if isinstance(raw, str):
        return raw
    return json.dumps(raw, ensure_ascii=False, default=str) if raw is not None else ""


def _steps(run: dict[str, Any]) -> list[str]:
    steps = []
    for tool in run.get("tools") or []:
        args = tool.get("tool_args") or {}
        rendered = ", ".join(f"{k}={_short(v, TOOL_ARG_MAX)}" for k, v in args.items())
        result = tool.get("result")
        failed = " ✗" if tool.get("tool_call_error") else ""
        steps.append(f"{tool.get('tool_name')}({rendered}){failed} → {_short(result, TOOL_RESULT_MAX)}")
    return steps


def _system_hash(run: dict[str, Any]) -> str | None:
    for msg in run.get("messages") or []:
        if msg.get("role") == "system":
            return hashlib.sha256(str(msg.get("content") or "").encode("utf-8")).hexdigest()[:12]
    return None


def compact_run(run: dict[str, Any], tz: ZoneInfo, previous_context: str | None) -> dict[str, Any]:
    metrics = run.get("metrics") or {}
    model = next(iter(((metrics.get("details") or {}).get("model") or [])), {})
    contexto, fala = split_input(_input_text(run))
    item: dict[str, Any] = {
        "run_id": run.get("run_id"),
        "at": _at(run.get("created_at"), tz),
        "status": run.get("status"),
        "modelo": model.get("id") or run.get("model"),
        "tokens": f"{metrics.get('input_tokens', 0)} in / {metrics.get('output_tokens', 0)} out"
        + (f" ({metrics['cache_read_tokens']} do cache)" if metrics.get("cache_read_tokens") else ""),
        "duracao_s": round(metrics["duration"], 1) if isinstance(metrics.get("duration"), (int, float)) else None,
        "fala": fala,
    }
    if contexto and contexto == previous_context:
        item["contexto_igual_ao_anterior"] = True
    elif contexto:
        item["contexto"] = contexto
    steps = _steps(run)
    if steps:
        item["passos"] = steps
    refs = run.get("references") or []
    if refs:
        item["busca_na_base"] = [_short(r.get("query"), 120) for r in refs if isinstance(r, dict)]
    item["resposta"] = run.get("content") if isinstance(run.get("content"), str) else run.get("content")
    return item


def compact_session(
    session: dict[str, Any], memories: list[dict[str, Any]] | None = None, timezone: str = DEFAULT_TIMEZONE
) -> dict[str, Any]:
    """`session` = a linha do Agno (`get_session(..., deserialize=False)`)."""
    tz = ZoneInfo(timezone)
    runs = [r for r in session.get("runs") or [] if not r.get("parent_run_id")]
    runs.sort(key=lambda r: r.get("created_at") or 0)
    items = []
    previous_context: str | None = None
    prompts: list[str] = []
    for run in runs:
        item = compact_run(run, tz, previous_context)
        previous_context = split_input(_input_text(run))[0] or previous_context
        h = _system_hash(run)
        if h and (not prompts or prompts[-1] != h):
            prompts.append(h)
            if len(prompts) > 1:
                item["prompt_mudou"] = h
        items.append(item)

    summary = (session.get("session_data") or {}).get("session_summary") or None
    out: dict[str, Any] = {
        "session_id": session.get("session_id"),
        "agent_id": session.get("agent_id"),
        "user_id": session.get("user_id"),
        "turnos": len(items),
        "resumo_da_sessao": (
            {"texto": summary.get("summary"), "atualizado_em": summary.get("updated_at")} if summary else None
        ),
        "memorias_do_contato": [
            {"texto": m.get("memory"), "topicos": m.get("topics") or [], "atualizado_em": _at(m.get("updated_at"), tz)}
            for m in memories or []
        ],
        "items": items,
    }
    return out
