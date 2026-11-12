"""A conversa como gente lê, em dois formatos.

**Linha do tempo** (`build_timeline`, `GET /observability/sessions/{id}/timeline`): a
sessão lida do trace store local, um turno por run, já separada em campos — os
eventos da conversa, a fala nova, os blocos de contexto (cada um dizendo se mudou
desde o turno anterior), as tools, as fontes e a resposta. É o que o console, o
`kuro sessions show` e a tool MCP `session_show` mostram. A separação vem da entrada
estruturada do `/chat` (`agents/structured_input.py`), gravada em colunas próprias;
nada aqui recorta texto. Run sem essas colunas (chamada só com `message`, ou anterior
à entrada estruturada) vem com `structured: false` e o texto inteiro em `raw_input`.

**Transcrição do Agno** (`compact_session`, `GET /observability/sessions/{id}/transcript`):
a sessão do Agno, com o que o agente "lembra" — o resumo da sessão e as memórias do
contato — e uma linha por turno com a entrada inteira, as tools e a resposta.
"""

import hashlib
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from agent_service.observability.overview import DEFAULT_TIMEZONE
from agent_service.tools.failures import NOT_COUNTED

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


# -- linha do tempo -------------------------------------------------------------------


class TimelineContextBlock(BaseModel):
    key: str
    title: str | None = None
    content: str
    volatile: bool = False
    hash: str
    """sha256 hex do `content` (o mesmo gravado em `runs.input_context`)."""
    changed: bool
    """O `hash` difere do bloco de mesma `key` no turno estruturado anterior da sessão,
    ou o bloco não existia nele. No primeiro turno estruturado da sessão, `false` em
    todos. Bloco `volatile` nunca conta como mudado."""
    previous_content: str | None = None
    """Só quando `changed` e o bloco existia no turno anterior: o texto de lá, para o
    "ver diferença" sem outra chamada. `null` em bloco novo, igual ou volátil."""


class TimelineStep(BaseModel):
    tool: str
    args: Any = None
    """Os argumentos com que o modelo chamou a tool (o `input` do span), inteiros."""
    ok: bool
    """`false` quando a tool falhou (`tools/failures.py`): argumento inválido, auth,
    indisponível (5xx, rede, timeout), configuração ou exceção — o mesmo critério de
    `tool_failures` do run. Um 404 (`not_found`) não conta como falha: `ok: true`, com
    `failure: "not_found"`."""
    failure: str | None = None
    """O tipo registrado (`invalid_arguments`, `not_found`, `auth`, `unavailable`,
    `config`, `exception`); `null` quando a tool respondeu normalmente."""
    http_status: int | None = None
    error: str | None = None
    """A mensagem de erro gravada no span, quando há `failure`."""
    result_preview: str | None = None
    """O começo do resultado, numa linha (até 240 caracteres); o inteiro está no trace do run."""
    side_effect: bool = False
    """A tool está classificada com efeito colateral (`side_effect=true`) — resolvido pela
    classificação atual da tool, como o filtro da fila de revisão. Não classificada = `false`."""
    latency_ms: float | None = None


class TimelineUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: float | None = None


class TimelineTurn(BaseModel):
    run_id: str
    at: datetime
    """Início do run (UTC)."""
    agent_type: str
    agent_version: int | None = None
    status: str
    status_message: str | None = None
    dry_run: bool = False
    """Teste (Playground, `kuro chat --dry-run`, MCP)."""
    structured: bool
    """O run gravou a entrada estruturada (`events`/`context` do `/chat`). `false`: run só
    com `message`, legado ou anterior a ela — aí `events` e `context` vêm vazios,
    `message` vem `null` e o texto inteiro está em `raw_input`, sem recorte."""
    events: list[dict[str, Any]] = []
    """Os eventos da requisição como foram gravados: `{role, content, at, kind,
    attachments, speech}`; `speech: true` marca o evento que é a fala nova."""
    message: str | None = None
    """A fala nova, pura (`runs.input_message`). `null` em turno não estruturado e em
    turno estruturado sem fala (só `context`)."""
    raw_input: str | None = None
    """Só em turno não estruturado: a mensagem inteira que o modelo leu."""
    context: list[TimelineContextBlock] = []
    steps: list[TimelineStep] = []
    """Cada chamada de tool, na ordem."""
    sources: list[dict[str, Any]] = []
    """Os documentos que as buscas na base devolveram — o mesmo `sources` do `/chat`."""
    output: str | None = None
    usage: TimelineUsage


class SessionTimeline(BaseModel):
    session_id: str
    user_id: str | None = None
    agent_types: list[str]
    """Os agentes que responderam na sessão (a sessão inteira, não só esta página)."""
    started_at: datetime
    ended_at: datetime
    """Fim do último run da sessão."""
    duration_ms: float
    turns: int
    """Runs na sessão inteira. `items` traz até `limit` deles."""
    error_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    contact_name: str | None = None
    """Nome do contato pelo run mais recente da sessão: dependency ou bloco de contexto
    com chave conhecida (a regra, em ordem, está em `observability/identity.py`). `None`
    sem nome — o console mostra o `user_id`."""
    channel: str | None = None
    """Canal pela metadata (`channel`/`canal`) ou dependency do run mais recente."""
    items: list[TimelineTurn]
    """Os turnos desta página, do mais antigo ao mais novo."""
    next_cursor: str | None = None
    """Há turnos mais novos: passe em `cursor` para a próxima página."""


def _block_hash(block: dict[str, Any]) -> str:
    stored = block.get("hash")
    if isinstance(stored, str) and stored:
        return stored
    return hashlib.sha256(str(block.get("content") or "").encode("utf-8")).hexdigest()


def _context(blocks: list[dict[str, Any]], previous: dict[str, dict[str, Any]] | None) -> list[TimelineContextBlock]:
    out = []
    for block in blocks:
        digest = _block_hash(block)
        volatile = bool(block.get("volatile"))
        before = (previous or {}).get(block.get("key"))
        changed = previous is not None and not volatile and (before is None or _block_hash(before) != digest)
        out.append(
            TimelineContextBlock(
                key=str(block.get("key")),
                title=block.get("title"),
                content=str(block.get("content") or ""),
                volatile=volatile,
                hash=digest,
                changed=changed,
                previous_content=str(before.get("content") or "") if changed and before is not None else None,
            )
        )
    return out


def _step(span: Any, side_effects: dict[str, bool]) -> TimelineStep:
    meta = span.metadata or {}
    failure = meta.get("failure")
    return TimelineStep(
        tool=span.name,
        args=span.input,
        ok=failure is None or failure in NOT_COUNTED,
        failure=failure,
        http_status=meta.get("http_status"),
        error=span.status_message if failure is not None else None,
        result_preview=None if span.output is None else _short(span.output, TOOL_RESULT_MAX),
        side_effect=side_effects.get(span.name, False),
        latency_ms=span.latency_ms,
    )


def build_timeline(session_id: str, data: dict[str, Any]) -> SessionTimeline:
    """`data` = `run_store.session_timeline(...)`."""
    from agent_service.documents.retrieval import SEARCH_TOOL, sources_from

    total = data["total"]
    seed = data["seed"]
    previous: dict[str, dict[str, Any]] | None = (
        None if seed is None else {b.get("key"): b for b in seed if isinstance(b, dict)}
    )
    items = []
    for row in data["rows"]:
        structured = row.input_events is not None or row.input_context is not None
        spans = data["spans"].get(row.run_id, [])
        context: list[TimelineContextBlock] = []
        if structured:
            blocks = [b for b in row.input_context or [] if isinstance(b, dict)]
            context = _context(blocks, previous)
            previous = {b.get("key"): b for b in blocks}
        searches = [
            s.metadata["references"]
            for s in spans
            if s.name == SEARCH_TOOL and isinstance((s.metadata or {}).get("references"), dict)
        ]
        items.append(
            TimelineTurn(
                run_id=row.run_id,
                at=row.started_at,
                agent_type=row.agent_type,
                agent_version=row.agent_version,
                status=row.status,
                status_message=row.status_message,
                dry_run=row.run_id in data["dry_run"],
                structured=structured,
                events=list(row.input_events or []) if structured else [],
                message=row.input_message if structured else None,
                raw_input=None if structured else row.message,
                context=context,
                steps=[_step(s, data["side_effects"]) for s in spans],
                sources=sources_from(searches),
                output=row.output,
                usage=TimelineUsage(
                    input_tokens=row.input_tokens or 0,
                    output_tokens=row.output_tokens or 0,
                    total_tokens=row.total_tokens or 0,
                    cost_usd=row.cost_usd,
                    latency_ms=row.latency_ms,
                ),
            )
        )
    started, ended = total["started_at"], total["ended_at"]
    return SessionTimeline(
        session_id=session_id,
        user_id=total["user_id"],
        agent_types=data["agent_types"],
        started_at=started,
        ended_at=ended,
        duration_ms=round((ended - started).total_seconds() * 1000, 1) if started and ended else 0.0,
        turns=total["turns"],
        error_count=total["error_count"] or 0,
        input_tokens=total["input_tokens"] or 0,
        output_tokens=total["output_tokens"] or 0,
        total_tokens=total["total_tokens"] or 0,
        cost_usd=total["cost_usd"],
        contact_name=data.get("head", {}).get("contact_name"),
        channel=data.get("head", {}).get("channel"),
        items=items,
        next_cursor=data["next_cursor"],
    )


# -- transcrição do Agno (resumo e memórias) ------------------------------------------


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


def compact_run(run: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
    metrics = run.get("metrics") or {}
    model = next(iter(((metrics.get("details") or {}).get("model") or [])), {})
    item: dict[str, Any] = {
        "run_id": run.get("run_id"),
        "at": _at(run.get("created_at"), tz),
        "status": run.get("status"),
        "modelo": model.get("id") or run.get("model"),
        "tokens": f"{metrics.get('input_tokens', 0)} in / {metrics.get('output_tokens', 0)} out"
        + (f" ({metrics['cache_read_tokens']} do cache)" if metrics.get("cache_read_tokens") else ""),
        "duracao_s": round(metrics["duration"], 1) if isinstance(metrics.get("duration"), (int, float)) else None,
        "entrada": _input_text(run),
    }
    steps = _steps(run)
    if steps:
        item["passos"] = steps
    refs = run.get("references") or []
    if refs:
        item["busca_na_base"] = [_short(r.get("query"), 120) for r in refs if isinstance(r, dict)]
    item["resposta"] = run.get("content")
    return item


def compact_session(
    session: dict[str, Any], memories: list[dict[str, Any]] | None = None, timezone: str = DEFAULT_TIMEZONE
) -> dict[str, Any]:
    """`session` = a linha do Agno (`get_session(..., deserialize=False)`). A entrada de
    cada turno vai inteira em `entrada`; a separação em fala, eventos e contexto é a da
    linha do tempo (`build_timeline`)."""
    tz = ZoneInfo(timezone)
    runs = [r for r in session.get("runs") or [] if not r.get("parent_run_id")]
    runs.sort(key=lambda r: r.get("created_at") or 0)
    items = []
    prompts: list[str] = []
    for run in runs:
        item = compact_run(run, tz)
        h = _system_hash(run)
        if h and (not prompts or prompts[-1] != h):
            prompts.append(h)
            if len(prompts) > 1:
                item["prompt_mudou"] = h
        items.append(item)

    summary = (session.get("session_data") or {}).get("session_summary") or None
    return {
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
