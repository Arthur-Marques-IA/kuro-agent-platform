"""`kuro runs`: execuções registradas no trace store do serviço (via /observability) e feedback."""

import json
import time
from typing import Any

import typer
from rich.table import Table
from rich.tree import Tree

from agent_service.cli.client import ApiError, ServiceUnavailable
from agent_service.cli.common import EXIT_USAGE, call, console, emit, fail, fail_from, print_json, read_json_file, state

app = typer.Typer(help="Execuções: listar, acompanhar ao vivo, ver trace, resumir, registrar score.")


def _runs_table(rows: list[dict[str, Any]], *, header: bool = True) -> Table:
    table = Table(show_edge=False, header_style="bold", show_header=header)
    for column in ("início", "run_id", "agente", "status", "tokens", "latência"):
        table.add_column(column, no_wrap=True)
    for r in rows:
        status = "[green]ok[/]" if r["status"] == "success" else f"[red]{r['status']}[/]"
        latency = f"{r['latency_ms'] / 1000:.1f}s" if (r.get("latency_ms") or 0) > 0 else "—"
        table.add_row(
            str(r["started_at"])[5:16].replace("T", " "),
            r["run_id"],
            r["agent_type"],
            status,
            str(r["total_tokens"]),
            latency,
        )
    return table


def _render_runs(page: dict[str, Any]) -> None:
    console.print(_runs_table(page["items"]))
    if page.get("next_cursor"):
        console.print("[dim]há mais resultados — o cursor da próxima página vem em `--json` (next_cursor)[/]")


def _render_trace(trace: dict[str, Any]) -> None:
    run = trace["run"]
    console.print(f"[bold]{run['agent_type']}[/] [dim]run {run['run_id']} · {run['status']} · {run['total_tokens']} tokens[/]")
    console.print(f"[cyan]mensagem:[/] {run.get('message') or '—'}", highlight=False)
    console.print(f"[magenta]resposta:[/] {run.get('output') or '—'}", highlight=False)
    if run.get("agent_version"):
        console.print(f"[dim]agente v{run['agent_version']} ({run.get('config_hash')})[/]", highlight=False)
    if run.get("knowledge_collection"):
        versao = f" v{run['collection_version']} ({run.get('collection_config_hash')})" if run.get("collection_version") else ""
        console.print(f"[dim]base {run['knowledge_collection']}{versao}[/]", highlight=False)

    spans = trace["spans"]
    ids = {s["id"] for s in spans}
    root = Tree("[bold]spans[/]")
    nodes: dict[str, Tree] = {}
    for span in sorted(spans, key=lambda s: str(s["started_at"])):
        parent = nodes.get(span.get("parent_id")) if span.get("parent_id") in ids else root
        latency = f" {span['latency_ms']:.0f}ms" if span.get("latency_ms") else ""
        level = f" [red]{span['level']}[/]" if span["level"] not in ("DEFAULT", "DEBUG") else ""
        nodes[span["id"]] = (parent or root).add(f"{span['type'].lower()} [bold]{span['name']}[/][dim]{latency}[/]{level}")
    console.print(root)
    for score in trace.get("scores") or []:
        console.print(f"score {score['name']} = {score['value']} [dim]{score.get('comment') or ''}[/]")


@app.callback(invoke_without_command=True)
def runs(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:  # defaults explícitos: chamar direto não resolve os typer.Option
        list_runs(ctx, agent=None, status=None, session_id=None, user_id=None, limit=20, cursor=None, meta=[], version=None,
                  collection=None, collection_version=None)


@app.command("list")
def list_runs(
    ctx: typer.Context,
    agent: str | None = typer.Option(None, "--agent", "-a", help="Filtra por agent_type."),
    status: str | None = typer.Option(None, "--status", help="success | error"),
    session_id: str | None = typer.Option(None, "--session-id"),
    user_id: str | None = typer.Option(None, "--user-id"),
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=100),
    cursor: str | None = typer.Option(None, "--cursor"),
    meta: list[str] = typer.Option([], "--meta", "-m", help="Filtro por metadata chave=valor (repetível), ex.: conversation_id=123."),
    version: int | None = typer.Option(None, "--version", help="Só runs desta versão da configuração (agent_version)."),
    collection: str | None = typer.Option(None, "--collection", help="Só runs de agentes que consultavam esta collection."),
    collection_version: int | None = typer.Option(
        None, "--collection-version", min=1, help="Só runs com esta versão da busca da collection (kuro collections versions)."
    ),
    complexity: list[int] = typer.Option([], "--complexity", "-c", min=1, max=3, help="Só este nível de complexidade (repetível): 1, 2 ou 3."),
    tool_failed: bool = typer.Option(False, "--tool-failed", help="Só runs com alguma tool falhando."),
    side_effect: bool = typer.Option(False, "--side-effect", help="Só runs que chamaram tool com efeito colateral."),
    min_chars: int | None = typer.Option(None, "--min-chars", min=1, help="Esconde mensagens mais curtas que isto."),
    feedback: str | None = typer.Option(None, "--feedback", help="up | down | none"),
    no_tests: bool = typer.Option(False, "--no-tests", help="Tira os testes do Playground (dry_run)."),
    sample: int | None = typer.Option(None, "--sample", min=1, max=100, help="Amostra aleatória deste tamanho."),
) -> None:
    """Execuções mais recentes. Os filtros de revisão montam a fila do que vale olhar,
    ex.: `kuro runs list -a r8 -c 3 --no-tests` ou `kuro runs list -a r8 --sample 20 --no-tests`."""
    st = state(ctx)
    page = call(
        st,
        st.client.list_runs,
        agent_type=agent,
        status=status,
        session_id=session_id,
        user_id=user_id,
        limit=limit,
        cursor=cursor,
        meta=meta or None,
        agent_version=version,
        knowledge_collection=collection,
        collection_version=collection_version,
        complexity=complexity or None,
        tool_failed=True if tool_failed else None,
        side_effect=True if side_effect else None,
        min_message_chars=min_chars,
        feedback=feedback,
        include_dry_run=False if no_tests else None,
        sample=sample,
    )
    emit(st, page, _render_runs)


@app.command("tail")
def tail_runs(
    ctx: typer.Context,
    agent: str | None = typer.Option(None, "--agent", "-a", help="Filtra por agent_type."),
    status: str | None = typer.Option(None, "--status", help="success | error"),
    interval: float = typer.Option(5.0, "--interval", min=1.0, help="Segundos entre as consultas."),
    limit: int = typer.Option(10, "--limit", "-n", min=1, max=100, help="Quantas execuções mostrar de saída."),
) -> None:
    """Acompanha as execuções ao vivo, como um `tail -f`. Ctrl+C para sair.

    É uma consulta repetida, não um stream: um run aparece na primeira consulta
    depois de terminar. Com `--json`, sai um objeto por execução."""
    st = state(ctx)
    seen: set[str] = set()
    first = True
    try:
        while True:
            try:
                page = st.client.list_runs(agent_type=agent, status=status, limit=limit if first else 100)
            except (ServiceUnavailable, ApiError) as exc:
                fail_from(st, exc)
            novos = [r for r in reversed(page["items"]) if r["run_id"] not in seen]
            seen.update(r["run_id"] for r in page["items"])
            if novos:
                if st.json_mode:
                    for run in novos:
                        print_json(run)
                else:
                    console.print(_runs_table(novos, header=first))
            first = False
            time.sleep(interval)
    except KeyboardInterrupt:
        if not st.json_mode:
            console.print()


@app.command("sessions")
def list_run_sessions(
    ctx: typer.Context,
    agent: str | None = typer.Option(None, "--agent", "-a", help="Filtra por agent_type."),
    status: str | None = typer.Option(None, "--status", help="success | error"),
    user_id: str | None = typer.Option(None, "--user-id"),
    limit: int = typer.Option(100, "--limit", "-n", min=1, max=200, help="Execuções varridas."),
) -> None:
    """Execuções agrupadas por sessão: tokens, custo, erros e avaliações.

    É o mesmo recorte da aba Logs do console. Não confundir com
    `kuro sessions`, que lê a conversa em si — isto agrupa as execuções
    registradas (tokens, custo, erros)."""
    st = state(ctx)
    page = call(st, st.client.run_sessions, agent_type=agent, status=status, user_id=user_id, limit=limit)
    emit(st, page, _render_run_sessions)


def _render_run_sessions(page: dict[str, Any]) -> None:
    rows = page["items"]
    if not rows:
        console.print("[dim]nenhuma sessão no período varrido[/]")
        return
    table = Table(show_edge=False, header_style="bold")
    for column in ("session_id", "agentes", "execuções", "erros", "tokens", "última atividade"):
        table.add_column(column, no_wrap=True)
    for s in rows:
        table.add_row(
            s["session_id"],
            ", ".join(s.get("agent_types") or []) or "—",
            str(s["run_count"]),
            f"[red]{s['error_count']}[/]" if s["error_count"] else "0",
            str(s["total_tokens"]),
            str(s["last_activity"])[5:16].replace("T", " "),
        )
    console.print(table)
    console.print(f"[dim]agregado sobre as {page['scanned']} execuções mais recentes[/]")


@app.command("stats")
def stats(
    ctx: typer.Context,
    agent: str | None = typer.Option(None, "--agent", "-a", help="Filtra por agent_type."),
    prompt_version: int | None = typer.Option(None, "--prompt-version", min=1, help="Só uma versão do prompt."),
    status: str | None = typer.Option(None, "--status", help="success | error"),
    user_id: str | None = typer.Option(None, "--user-id"),
    session_id: str | None = typer.Option(None, "--session-id"),
) -> None:
    """Resumo das execuções: total, erros, tokens, custo e latência, com série diária."""
    st = state(ctx)
    result = call(
        st,
        st.client.run_stats,
        agent_type=agent,
        prompt_version=prompt_version,
        status=status,
        user_id=user_id,
        session_id=session_id,
    )
    emit(st, result, _render_stats)


def _render_stats(s: dict[str, Any]) -> None:
    custo = f"US$ {s['total_cost_usd']:.4f}" if s.get("total_cost_usd") else "—"
    latencia = f"{s['avg_latency_ms'] / 1000:.1f}s" if s.get("avg_latency_ms") else "—"
    por_status = ", ".join(f"{k}={v}" for k, v in sorted((s.get("status_counts") or {}).items())) or "—"
    console.print(
        f"[bold]{s['total_runs']}[/] execuções · {s['total_tokens']} tokens · {custo} · latência média {latencia}\n"
        f"[dim]por status: {por_status}[/]"
    )
    if s.get("buckets"):
        table = Table(show_edge=False, header_style="bold")
        for column in ("dia", "execuções", "erros", "tokens", "custo"):
            table.add_column(column, no_wrap=True)
        for b in s["buckets"]:
            table.add_row(
                b["date"],
                str(b["runs"]),
                f"[red]{b['errors']}[/]" if b["errors"] else "0",
                str(b["total_tokens"]),
                f"{b['cost_usd']:.4f}" if b.get("cost_usd") else "—",
            )
        console.print(table)
    # O agregado não varre o histórico inteiro; dizer isso evita conclusão errada.
    console.print(f"[dim]agregado sobre as {s['scanned']} execuções mais recentes[/]")


@app.command("show")
def show_run(ctx: typer.Context, run_id: str) -> None:
    """Trace completo de um run: mensagem, resposta, spans (LLM/tools) e scores."""
    st = state(ctx)
    emit(st, call(st, st.client.run_trace, run_id), _render_trace)


@app.command("score")
def score(
    ctx: typer.Context,
    run_id: str,
    value: float = typer.Argument(..., help="`feedback`: 1 positivo / 0 negativo; outros nomes: numérico."),
    name: str = typer.Option("feedback", "--name"),
    comment: str | None = typer.Option(None, "--comment"),
) -> None:
    """Registra uma avaliação para um run."""
    st = state(ctx)
    body = {"run_id": run_id, "name": name, "value": value, "comment": comment}
    emit(st, call(st, st.client.score, body), lambda s: console.print(f"[green]✓[/] {s['name']}={s['value']} em {s['run_id']}"))


# -- feedback por trecho recuperado (RAG) ---------------------------------------------

_CHUNK_LABELS = ("relevant", "irrelevant", "wrong")


@app.command("chunk-feedback")
def chunk_feedback(
    ctx: typer.Context,
    run_id: str,
    chunk_id: str = typer.Argument(..., help="`chunk_id` do trecho (em `kuro runs show <run_id> --json`, metadata.references)."),
    label: str = typer.Argument(..., help="relevant | irrelevant | wrong (conteúdo errado/desatualizado)."),
    span_id: str | None = typer.Option(None, "--span", help="A span da busca; sem isto, a primeira busca do run com o trecho."),
    comment: str | None = typer.Option(None, "--comment"),
    created_by: str = typer.Option("cli", "--by", envvar="KURO_USER_ID", help="Quem marcou."),
) -> None:
    """Marca um trecho que a busca do run devolveu. Só registra: o Kuro não esconde
    nem despublica documento por causa da marcação."""
    st = state(ctx)
    if label not in _CHUNK_LABELS:
        fail(st, f"rótulo inválido: {label!r} (use {', '.join(_CHUNK_LABELS)})", EXIT_USAGE)
    body = {"run_id": run_id, "chunk_id": chunk_id, "label": label, "span_id": span_id, "comment": comment, "created_by": created_by}
    emit(
        st,
        call(st, st.client.chunk_feedback, body),
        lambda r: console.print(f"[green]✓[/] {r['label']} em {r['document'] or r['chunk_id']} [dim](id {r['id']})[/]"),
    )


@app.command("chunk-unmark")
def chunk_unmark(ctx: typer.Context, feedback_id: str) -> None:
    """Apaga uma marcação de trecho (o id vem de `chunk-feedback` ou `chunk-marks`)."""
    st = state(ctx)
    call(st, st.client.delete_chunk_feedback, feedback_id)
    emit(st, {"deleted": feedback_id}, lambda r: console.print(f"[green]✓[/] marcação {r['deleted']} apagada"))


def _render_marks(rows: list[dict[str, Any]]) -> None:
    table = Table(show_edge=False, header_style="bold")
    for column in ("quando", "rótulo", "documento", "pergunta", "run_id", "id"):
        table.add_column(column)
    for r in rows:
        table.add_row(
            str(r["created_at"])[5:16].replace("T", " "), r["label"], r.get("document") or "—", r["query"], r["run_id"], r["id"]
        )
    console.print(table)


@app.command("chunk-marks")
def chunk_marks(
    ctx: typer.Context,
    run: str | None = typer.Option(None, "--run", help="Só deste run."),
    collection: str | None = typer.Option(None, "--collection", "-c"),
    label: str | None = typer.Option(None, "--label", help="relevant | irrelevant | wrong."),
    limit: int = typer.Option(100, "--limit", "-n", min=1, max=5000),
) -> None:
    """Lista as marcações de trecho, mais recentes primeiro."""
    st = state(ctx)
    rows = call(st, st.client.list_chunk_feedback, run_id=run, collection=collection, label=label, limit=limit)
    emit(st, rows, _render_marks)


def _render_flagged(rows: list[dict[str, Any]]) -> None:
    if not rows:
        console.print("[dim]nenhum documento marcado[/]")
    for r in rows:
        console.print(f"[bold]{r['count']}×[/] {r.get('document') or r.get('content_id')} [dim]({r.get('collection') or '—'})[/]")
        console.print(f"   [dim]runs: {', '.join(r['run_ids'][:5])}[/]", highlight=False)


@app.command("chunk-flagged")
def chunk_flagged(
    ctx: typer.Context,
    collection: str | None = typer.Option(None, "--collection", "-c"),
    label: str = typer.Option("wrong", "--label", help="relevant | irrelevant | wrong."),
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=200),
) -> None:
    """Documentos com mais marcações (padrão: errado/desatualizado), com os runs."""
    st = state(ctx)
    emit(st, call(st, st.client.flagged_documents, collection=collection, label=label, limit=limit), _render_flagged)


@app.command("chunk-export")
def chunk_export(
    ctx: typer.Context,
    output: str | None = typer.Option(None, "--output", "-o", help="Arquivo JSONL; sem isto, stdout."),
    collection: str | None = typer.Option(None, "--collection", "-c"),
    agent: str | None = typer.Option(None, "--agent", "-a", help="agent_type."),
    since: str | None = typer.Option(None, "--since", help="ISO 8601."),
) -> None:
    """Gabarito de RAG em JSONL: uma linha por pergunta (query, collection,
    relevant_chunk_ids, relevant_content_ids, run_ids), das marcações `relevant`."""
    st = state(ctx)
    cases = call(st, st.client.export_chunk_feedback, collection=collection, agent_type=agent, since=since)
    lines = "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases)
    if output:
        try:
            with open(output, "w", encoding="utf-8") as fh:
                fh.write(lines)
        except OSError as exc:
            fail(st, f"não consegui gravar {output}: {exc}", EXIT_USAGE)
        emit(st, {"output": output, "cases": len(cases)}, lambda r: console.print(f"[green]✓[/] {r['cases']} perguntas em {r['output']}"))
        return
    import sys

    sys.stdout.write(lines)


# -- shadow: referência, concordância e export -----------------------------------------


@app.command("reference")
def reference(
    ctx: typer.Context,
    run_id: str,
    file: str = typer.Option(..., "--file", "-f", help="JSON com a decisão de referência (ex.: a do agente legado)."),
    source: str = typer.Option("legacy", "--source", help="De onde veio a referência."),
) -> None:
    """Grava a decisão de referência de um run — no shadow, quem faz isso é o
    próprio sistema integrado (`POST /observability/references`)."""
    st = state(ctx)
    body = {"run_id": run_id, "reference": read_json_file(st, file), "source": source}
    emit(st, call(st, st.client.save_reference, body), lambda r: console.print(f"[green]✓[/] referência gravada em {r['run_id']}"))


def _render_agreement(r: dict[str, Any]) -> None:
    console.print(
        f"[bold]{r['agent_type']}[/]  {r['full_match']}/{r['runs']} runs concordam em tudo ({r['rate']:.0%})"
        + (f"  [dim]{r['skipped']} sem saída comparável[/]" if r["skipped"] else "")
    )
    if r["fields"]:
        table = Table(show_edge=False, header_style="bold")
        for column in ("campo", "concorda", "taxa"):
            table.add_column(column)
        for f in sorted(r["fields"], key=lambda f: f["rate"]):
            color = "green" if f["rate"] >= 0.95 else "yellow" if f["rate"] >= 0.8 else "red"
            table.add_row(f["field"], f"{f['matched']}/{f['compared']}", f"[{color}]{f['rate']:.0%}[/]")
        console.print(table)
    for v in r["by_version"]:
        label = f"v{v['agent_version']}" if v["agent_version"] else "sem versão"
        console.print(f"  {label}: {v['full_match']}/{v['runs']} ({v['rate']:.0%})")
    for d in r["disagreements"][:5]:
        diffs = "; ".join(f"{m['field']}: {m['expected']!r} × {m['actual']!r}" for m in d["mismatches"])
        console.print(f"  [dim]{d['run_id']}[/] {diffs}", highlight=False)


@app.command("agreement")
def agreement(
    ctx: typer.Context,
    agent: str = typer.Option(..., "--agent", "-a", help="agent_type."),
    since: str | None = typer.Option(None, "--since", help="ISO 8601, ex.: 2026-09-01."),
    version: int | None = typer.Option(None, "--version", help="Só esta versão da configuração."),
    fields: str | None = typer.Option(None, "--fields", help="Só estes campos (vírgula)."),
) -> None:
    """Concordância entre a decisão do agente e a referência (o legado, no shadow),
    campo a campo e por versão. É o número que diz quando promover."""
    st = state(ctx)
    result = call(st, st.client.agreement, agent_type=agent, since=since, agent_version=version, fields=fields)
    emit(st, result, _render_agreement)


@app.command("export")
def export(
    ctx: typer.Context,
    agent: str = typer.Option(..., "--agent", "-a", help="agent_type."),
    output: str | None = typer.Option(None, "--output", "-o", help="Arquivo JSONL; sem isto, stdout."),
    since: str | None = typer.Option(None, "--since", help="ISO 8601."),
    version: int | None = typer.Option(None, "--version", help="Só esta versão da configuração."),
    limit: int = typer.Option(1000, "--limit", "-n", min=1, max=5000),
) -> None:
    """Runs com referência como dataset do `kuro eval` (JSONL: id, input,
    dependencies, expected). Ex.: `kuro runs export -a r8 -o casos.jsonl`."""
    st = state(ctx)
    cases = call(st, st.client.export_cases, agent_type=agent, since=since, agent_version=version, limit=limit)
    lines = "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases)
    if output:
        try:
            with open(output, "w", encoding="utf-8") as fh:
                fh.write(lines)
        except OSError as exc:
            fail(st, f"não consegui gravar {output}: {exc}", EXIT_USAGE)
        emit(st, {"output": output, "cases": len(cases)}, lambda r: console.print(f"[green]✓[/] {r['cases']} casos em {r['output']}"))
        return
    import sys

    sys.stdout.write(lines)

