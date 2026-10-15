"""`kuro collections`: bases de conhecimento — cadastrar, alimentar e buscar.

Um agente consulta uma collection quando aponta para ela:
`kuro agents set <agente> knowledge_collection=manuais`.
"""

import sys
import time
from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from agent_service.cli.common import EXIT_USAGE, call, console, emit, fail, parse_pairs, state

app = typer.Typer(help="Collections de documentos (RAG): listar, criar, alimentar, buscar.")


def _render_list(rows: list[dict[str, Any]]) -> None:
    if not rows:
        console.print("Nenhuma collection. Crie uma com `kuro collections create <nome> --label \"Manuais\"`.")
        return
    table = Table(show_edge=False, header_style="bold")
    for column in ("name", "nome", "embedder", "busca", "descrição", "agentes"):
        table.add_column(column)
    for c in rows:
        seed = " [dim](seed)[/]" if c["is_seed"] else ""
        embedder = f"{c.get('embedder_provider', 'google')}/{c.get('embedder_model', '—')}"
        table.add_row(
            f"[cyan]{c['name']}[/]{seed}",
            c["label"],
            embedder,
            _search_label(c),
            c["description"] or "—",
            ", ".join(c["agents_using"]) or "—",
        )
    console.print(table)


def _search_label(c: dict[str, Any]) -> str:
    s = c.get("search") or {}
    if not s:
        return "—"
    modo = f"hybrid (vetor {s['vector_weight']:g})" if s["search_mode"] == "hybrid" else "vector"
    versao = f" [dim]v{c['version']}[/]" if c.get("version") else ""
    return f"{modo}, k={s['max_results']}, {s['language']}{versao}"


@app.callback(invoke_without_command=True)
def collections(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        list_collections(ctx)


@app.command("list")
def list_collections(ctx: typer.Context) -> None:
    """Lista as collections e quais agentes usam cada uma."""
    st = state(ctx)
    emit(st, call(st, st.client.list_collections), _render_list)


@app.command("create")
def create_collection(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Slug: minúsculas, números, '-' ou '_'."),
    label: str = typer.Option(..., "--label", "-l", help="Nome exibido."),
    description: str | None = typer.Option(None, "--description", "-d"),
    embedder: str | None = typer.Option(
        None, "--embedder", help="Quem gera os vetores: google (padrão), openai ou ollama. Não muda depois."
    ),
    embedder_model: str | None = typer.Option(None, "--embedder-model", help="Modelo de embedding do provedor."),
    embedder_dimensions: int | None = typer.Option(
        None, "--embedder-dimensions", min=1, help="Só com um --embedder-model fora do padrão do provedor."
    ),
    mode: str | None = typer.Option(None, "--mode", help="vector (padrão) ou hybrid (vetor + full-text do Postgres)."),
    language: str | None = typer.Option(
        None, "--language", help="Dicionário do full-text do Postgres (padrão portuguese). Não muda depois."
    ),
    max_results: int | None = typer.Option(None, "--k", "--max-results", min=1, max=50, help="Trechos por busca (padrão 10)."),
    vector_weight: float | None = typer.Option(
        None, "--vector-weight", min=0.0, max=1.0, help="No hybrid, o peso do vetor; o resto é do texto (padrão 0.5)."
    ),
) -> None:
    """Cria uma collection (a tabela de vetores nasce na primeira ingestão).

    O embedder e o idioma ficam fixos: a tabela `knowledge_<nome>` guarda vetores
    de um embedder só, e o idioma decide quais trechos o full-text casa. Para
    trocar, crie outra collection e reindexe. Modo, k e peso mudam com `set`."""
    st = state(ctx)
    created = call(
        st,
        st.client.create_collection,
        {
            "name": name,
            "label": label,
            "description": description,
            "embedder_provider": embedder,
            "embedder_model": embedder_model,
            "embedder_dimensions": embedder_dimensions,
            **{
                k: v
                for k, v in (
                    ("search_mode", mode),
                    ("language", language),
                    ("max_results", max_results),
                    ("vector_weight", vector_weight),
                )
                if v is not None
            },
        },
    )
    emit(
        st,
        created,
        lambda c: console.print(
            f"[green]✓[/] collection {c['name']} criada ({c['embedder_provider']}/{c['embedder_model']}; "
            f"{_search_label(c)})"
        ),
    )


@app.command("get")
def get_collection(ctx: typer.Context, name: str) -> None:
    """Mostra uma collection: embedder, descrição e quais agentes a usam."""
    st = state(ctx)
    emit(st, call(st, st.client.get_collection, name), lambda c: _render_list([c]))


@app.command("set")
def set_fields(
    ctx: typer.Context,
    name: str,
    pairs: list[str] = typer.Argument(
        ..., help="campo=valor. Editáveis: label, description, search_mode, max_results, vector_weight, reranker."
    ),
) -> None:
    """Altera só os campos informados. Mudar a busca gera uma versão nova da collection
    (`kuro collections versions`); `campo=null` volta ao padrão. O embedder e o
    idioma não mudam (ver `create`).

    Ex.: `kuro collections set manuais search_mode=hybrid vector_weight=0.7 max_results=8`."""
    st = state(ctx)
    changes = parse_pairs(st, pairs, "campo")
    editaveis = {"label", "description", "search_mode", "max_results", "vector_weight", "reranker"}
    desconhecidos = sorted(set(changes) - editaveis)
    if desconhecidos:
        fail(st, f"campos não editáveis: {', '.join(desconhecidos)} (use: {', '.join(sorted(editaveis))})", EXIT_USAGE)
    emit(
        st,
        call(st, st.client.update_collection, name, changes),
        lambda c: console.print(f"[green]✓[/] collection {c['name']} atualizada — {_search_label(c)}"),
    )


@app.command("versions")
def list_versions(
    ctx: typer.Context,
    name: str,
    version: int | None = typer.Argument(None, help="Uma versão: mostra a configuração completa dela."),
) -> None:
    """Histórico da configuração (embedder + busca). Cada run grava a versão com que
    buscou: `kuro runs list --collection <nome> --collection-version N`."""
    st = state(ctx)
    if version is not None:
        emit(st, call(st, st.client.collection_version, name, version), lambda v: console.print_json(data=v["config"]))
        return

    def render(rows: list[dict[str, Any]]) -> None:
        table = Table(show_edge=False, header_style="bold")
        for column in ("versão", "hash", "busca", "criada"):
            table.add_column(column)
        for v in rows:
            cfg = v["config"]
            busca = cfg.get("search_mode", "vector")
            if cfg.get("vector_weight") is not None:
                busca += f" (vetor {cfg['vector_weight']:g})"
            busca += f", k={cfg.get('max_results')}, {cfg.get('language') or 'english'}"
            table.add_row(f"v{v['version']}", v["config_hash"], busca, str(v["created_at"])[:19])
        console.print(table)

    emit(st, call(st, st.client.collection_versions, name), render)


@app.command("embedders")
def list_embedders(ctx: typer.Context) -> None:
    """Provedores de embedding e quais já têm credencial cadastrada."""
    st = state(ctx)

    def render(rows: list[dict[str, Any]]) -> None:
        table = Table(show_edge=False, header_style="bold")
        for column in ("provider", "modelo padrão", "chave", "pronto"):
            table.add_column(column)
        for e in rows:
            table.add_row(
                f"[cyan]{e['provider']}[/]",
                e["default_model_id"],
                "sim" if e["requires_api_key"] else "não precisa",
                "sim" if e["configured"] else "[red]falta credencial[/]",
            )
        console.print(table)

    emit(st, call(st, st.client.list_embedders), render)


@app.command("delete")
def delete_collection(
    ctx: typer.Context,
    name: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Não pede confirmação (obrigatório sem TTY)."),
) -> None:
    """Remove o cadastro da collection.

    Os vetores já indexados continuam na tabela `knowledge_<nome>`, que é do
    Agno. Por isso recriar uma collection com o mesmo nome é recusado com 409
    enquanto essa tabela tiver documentos: não dá para saber qual embedder os
    gerou, e misturar dois embedders na mesma tabela dá busca errada calada.
    Use outro nome, ou apague a tabela antes."""
    st = state(ctx)
    if not yes:
        if not st.interactive:
            fail(st, "confirme com --yes para remover sem TTY", EXIT_USAGE)
        if not typer.confirm(f"Remover a collection {name!r}?"):
            raise typer.Exit()
    call(st, st.client.delete_collection, name)
    emit(st, {"deleted": name}, lambda _: console.print(f"[green]✓[/] {name} removida"))


@app.command("add")
def add_document(
    ctx: typer.Context,
    name: str,
    text: str | None = typer.Option(None, "--text", "-t", help="Texto a indexar; sem isto, lê de stdin."),
    title: str | None = typer.Option(None, "--title", help="Nome do documento."),
    file: str | None = typer.Option(None, "--file", "-f", help="Arquivo a indexar (PDF, DOCX, CSV, TXT, MD...)."),
) -> None:
    """Indexa um texto ou um arquivo na collection.

    Ex.: `kuro collections add manuais -f manual.pdf`, ou
    `type manual.txt | kuro collections add manuais`."""
    st = state(ctx)
    if file is not None:
        if text is not None:
            fail(st, "use --file ou --text, não os dois", EXIT_USAGE)
        caminho = Path(file)
        if not caminho.is_file():
            fail(st, f"arquivo não encontrado: {file}", EXIT_USAGE)
        result = call(
            st,
            st.client.add_collection_file,
            name,
            filename=caminho.name,
            content=caminho.read_bytes(),
            title=title,
        )
        emit(st, result, lambda r: console.print(f"[green]✓[/] {caminho.name} indexado ({r['content_id']})"))
        return
    if text is None:
        text = "" if sys.stdin.isatty() else sys.stdin.read()
        if not text.strip():
            fail(st, "sem texto: use --text ou envie o conteúdo por stdin", EXIT_USAGE)
    result = call(st, st.client.add_document, name, {"text": text, "name": title})
    emit(st, result, lambda r: console.print(f"[green]✓[/] indexado ({r['content_id']})"))


@app.command("docs")
def list_documents(ctx: typer.Context, name: str) -> None:
    """Documentos indexados na collection, com o status do processamento."""
    st = state(ctx)

    def render(page: dict[str, Any]) -> None:
        rows = page["data"]
        if not rows:
            console.print("[dim]nenhum documento indexado[/]")
            return
        table = Table(show_edge=False, header_style="bold")
        for column in ("id", "nome", "tipo", "status"):
            table.add_column(column)
        for d in rows:
            status = d.get("status") or "—"
            cor = "green" if status == "completed" else "red" if status == "failed" else "yellow"
            table.add_row(f"[cyan]{d['id']}[/]", d.get("name") or "—", d.get("type") or "—", f"[{cor}]{status}[/]")
        console.print(table)

    emit(st, call(st, st.client.list_collection_documents, name), render)


@app.command("rm-doc")
def remove_document(
    ctx: typer.Context,
    name: str,
    content_id: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Não pede confirmação (obrigatório sem TTY)."),
) -> None:
    """Tira um documento da base (o texto e os vetores dele). Não tem volta.

    É como se corrige uma resposta errada que o agente estava citando: ache o
    id com `kuro collections docs <coleção>`."""
    st = state(ctx)
    if not yes:
        if not st.interactive:
            fail(st, "confirme com --yes para remover sem TTY", EXIT_USAGE)
        if not typer.confirm(f"Remover o documento {content_id!r} de {name!r}?"):
            raise typer.Exit()
    call(st, st.client.delete_collection_document, name, content_id)
    emit(st, {"deleted": content_id}, lambda _: console.print(f"[green]✓[/] documento removido de {name}"))


@app.command("search")
def search_collection(
    ctx: typer.Context,
    name: str,
    query: str,
    limit: int | None = typer.Option(None, "--limit", "-n", min=1, max=50, help="Padrão: o k da collection."),
) -> None:
    """Busca na collection — o mesmo que o agente enxerga (modo, k e idioma dela).

    O score depende do modo: similaridade (vector), vetor + texto (hybrid) ou o do reranker."""
    st = state(ctx)
    results = call(st, st.client.search_collection, name, query, limit)

    def render(items: list[dict[str, Any]]) -> None:
        if not items:
            console.print("[dim]nada encontrado[/]")
            return
        for i, doc in enumerate(items, 1):
            origem = doc.get("source_url") or doc.get("document") or ""
            kind = f" {doc['score_kind']}" if doc.get("score_kind") else ""
            score = f" [dim]{doc['score']:.2f}{kind}[/]" if doc.get("score") is not None else ""
            console.print(f"[bold cyan]{i}.[/]{score} [dim]{origem}[/]", highlight=False)
            console.print(f"   {doc['content'][:500]}", highlight=False)

    emit(st, results, render)


# -- sites -----------------------------------------------------------------------------

_STATUS_COR = {"ready": "green", "partial": "yellow", "running": "cyan", "pending": "cyan", "failed": "red"}
_TERMINAL = ("ready", "partial", "failed")


def _render_source(s: dict[str, Any]) -> None:
    cor = _STATUS_COR.get(s["status"], "white")
    p = s.get("progress") or {}
    falhas = f", [red]{s['pages_failed']} com falha[/]" if s["pages_failed"] else ""
    console.print(
        f"[cyan]{s['id']}[/] {s['url']} [dim]({s['mode']}, refresh {s['refresh']})[/] — [{cor}]{s['status']}[/] "
        f"· {s['pages_indexed']} páginas indexadas{falhas}",
        highlight=False,
    )
    if s["status"] == "running" and p:
        console.print(f"   [dim]{p.get('done', 0)}/{p.get('discovered', 0)} páginas processadas[/]")
    if s.get("status_message"):
        console.print(f"   [dim]{s['status_message']}[/]", highlight=False)


def _render_source_detail(s: dict[str, Any]) -> None:
    _render_source(s)
    pages = s.get("pages") or []
    if not pages:
        return
    table = Table(show_edge=False, header_style="bold")
    for column in ("url", "status", "trechos", "formatação", "detalhe"):
        table.add_column(column)
    for page in pages:
        cor = {"indexed": "green", "failed": "red"}.get(page["status"], "yellow")
        table.add_row(
            page["url"],
            f"[{cor}]{page['status']}[/]",
            str(page.get("chunks") or 0),
            page.get("formatted") or "—",
            page.get("error") or "",
        )
    console.print(table)


def _wait(st: Any, name: str, source_id: str, timeout: float) -> dict[str, Any]:
    """Acompanha a sincronização até terminar (ou estourar `timeout`)."""
    deadline = time.monotonic() + timeout
    while True:
        source = call(st, st.client.get_source, name, source_id)
        if source["status"] in _TERMINAL or time.monotonic() > deadline:
            return source
        if st.interactive:
            p = source.get("progress") or {}
            console.print(f"[dim]… {p.get('done', 0)}/{p.get('discovered', 0)} páginas[/]")
        time.sleep(2)


def _finish(st: Any, source: dict[str, Any]) -> None:
    emit(st, source, _render_source)
    if source["status"] == "failed":
        raise typer.Exit(1)


@app.command("add-site")
def add_site(
    ctx: typer.Context,
    name: str,
    url: str,
    mode: str = typer.Option(
        "crawl",
        "--mode",
        "-m",
        help="crawl (segue links abaixo do caminho da URL), sitemap (as páginas do sitemap) ou page (só a URL).",
    ),
    max_pages: int | None = typer.Option(None, "--max-pages", min=1, help="Teto de páginas (padrão 50)."),
    max_depth: int | None = typer.Option(None, "--max-depth", min=0, help="Saltos de link no crawl (padrão 3)."),
    include: list[str] = typer.Option([], "--include", help="Glob de caminho que entra, ex. '/docs/*'. Repetível."),
    exclude: list[str] = typer.Option([], "--exclude", help="Glob de caminho que fica fora, ex. '/blog/*'. Repetível."),
    no_format: bool = typer.Option(False, "--no-format", help="Sem o agente formatador: indexa o texto extraído."),
    formatter_provider: str | None = typer.Option(None, "--formatter-provider", help="Provedor do formatador."),
    formatter_model: str | None = typer.Option(None, "--formatter-model", help="Modelo do formatador."),
    refresh: str = typer.Option("never", "--refresh", help="Ressincronizar: never, daily, weekly ou monthly."),
    label: str | None = typer.Option(None, "--label", "-l"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Espera a primeira sincronização terminar."),
    timeout: float = typer.Option(900, "--timeout", help="Com --wait: quanto esperar, em segundos."),
) -> None:
    """Indexa um site na collection e (opcionalmente) o mantém sincronizado.

    Cada página é baixada (respeitando o robots.txt), tem o conteúdo principal
    extraído, passa por um agente que organiza o Markdown sem reescrever, e vira
    trechos por seção — cada um com a URL de origem, que aparece no trace.

    Ex.: `kuro collections add-site manuais https://docs.exemplo.com/guia --refresh weekly --wait`"""
    st = state(ctx)
    if mode not in ("crawl", "sitemap", "page"):
        fail(st, "--mode deve ser crawl, sitemap ou page", EXIT_USAGE)
    payload = {
        "url": url,
        "mode": mode,
        "refresh": refresh,
        "label": label,
        "max_pages": max_pages,
        "max_depth": max_depth,
        "include": include or None,
        "exclude": exclude or None,
        "format": False if no_format else None,
        "formatter_provider": formatter_provider,
        "formatter_model_id": formatter_model,
    }
    source = call(st, st.client.create_source, name, {k: v for k, v in payload.items() if v is not None})
    _finish(st, _wait(st, name, source["id"], timeout) if wait else source)


@app.command("sources")
def list_sources(ctx: typer.Context, name: str, source_id: str | None = typer.Argument(None)) -> None:
    """Sites da collection e o estado da sincronização; com o id, as páginas de um deles."""
    st = state(ctx)
    if source_id:
        emit(st, call(st, st.client.get_source, name, source_id), _render_source_detail)
        return

    def render(rows: list[dict[str, Any]]) -> None:
        if not rows:
            console.print(f"Nenhum site. Adicione com `kuro collections add-site {name} <url>`.")
        for row in rows:
            _render_source(row)

    emit(st, call(st, st.client.list_sources, name), render)


@app.command("sync")
def sync_source(
    ctx: typer.Context,
    name: str,
    source_id: str,
    wait: bool = typer.Option(False, "--wait", "-w", help="Espera terminar."),
    timeout: float = typer.Option(900, "--timeout"),
) -> None:
    """Coleta o site de novo agora: entra o que é novo, troca o que mudou, sai o que sumiu."""
    st = state(ctx)
    source = call(st, st.client.sync_source, name, source_id)
    _finish(st, _wait(st, name, source_id, timeout) if wait else source)


@app.command("set-site")
def set_site(
    ctx: typer.Context,
    name: str,
    source_id: str,
    pairs: list[str] = typer.Argument(
        ...,
        help="campo=valor: refresh, label, max_pages, max_depth, include, exclude, format, "
        "formatter_provider, formatter_model_id.",
    ),
) -> None:
    """Altera o agendamento ou as opções de coleta de um site (vale a partir do próximo sync)."""
    st = state(ctx)
    changes = parse_pairs(st, pairs, "campo")
    emit(st, call(st, st.client.update_source, name, source_id, changes), _render_source)


@app.command("rm-site")
def remove_site(
    ctx: typer.Context,
    name: str,
    source_id: str,
    keep_documents: bool = typer.Option(
        False, "--keep-documents", help="Tira só o cadastro; as páginas continuam na base."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Não pede confirmação (obrigatório sem TTY)."),
) -> None:
    """Remove o site da collection — e, sem --keep-documents, as páginas dele da base."""
    st = state(ctx)
    if not yes:
        if not st.interactive:
            fail(st, "confirme com --yes para remover sem TTY", EXIT_USAGE)
        if not typer.confirm(f"Remover o site {source_id!r} de {name!r}?"):
            raise typer.Exit()
    call(st, st.client.delete_source, name, source_id, keep_documents)
    emit(st, {"deleted": source_id}, lambda _: console.print(f"[green]✓[/] site removido de {name}"))
