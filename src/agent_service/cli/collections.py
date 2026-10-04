"""`kuro collections`: bases de conhecimento — cadastrar, alimentar e buscar.

Um agente consulta uma collection quando aponta para ela:
`kuro agents set <agente> knowledge_collection=manuais`.
"""

import sys
from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from agent_service.cli.common import EXIT_USAGE, call, console, emit, fail, parse_pairs, read_stdin, state

app = typer.Typer(help="Collections de documentos (RAG): listar, criar, alimentar, buscar.")


def _render_list(rows: list[dict[str, Any]]) -> None:
    if not rows:
        console.print("Nenhuma collection. Crie uma com `kuro collections create <nome> --label \"Manuais\"`.")
        return
    table = Table(show_edge=False, header_style="bold")
    for column in ("name", "nome", "embedder", "descrição", "agentes"):
        table.add_column(column)
    for c in rows:
        seed = " [dim](seed)[/]" if c["is_seed"] else ""
        embedder = f"{c.get('embedder_provider', 'google')}/{c.get('embedder_model', '—')}"
        table.add_row(
            f"[cyan]{c['name']}[/]{seed}",
            c["label"],
            embedder,
            c["description"] or "—",
            ", ".join(c["agents_using"]) or "—",
        )
    console.print(table)


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
) -> None:
    """Cria uma collection (a tabela de vetores nasce na primeira ingestão).

    O embedder fica fixo: a tabela `knowledge_<nome>` guarda vetores de um
    embedder só. Para trocar, crie outra collection e reindexe."""
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
        },
    )
    emit(
        st,
        created,
        lambda c: console.print(
            f"[green]✓[/] collection {c['name']} criada ({c['embedder_provider']}/{c['embedder_model']})"
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
    pairs: list[str] = typer.Argument(..., help='campo=valor. Editáveis: label, description.'),
) -> None:
    """Altera só os campos informados. O embedder não muda (ver `create`)."""
    st = state(ctx)
    changes = parse_pairs(st, pairs, "campo")
    desconhecidos = sorted(set(changes) - {"label", "description"})
    if desconhecidos:
        fail(st, f"campos não editáveis: {', '.join(desconhecidos)} (use: label, description)", EXIT_USAGE)
    emit(
        st,
        call(st, st.client.update_collection, name, changes),
        lambda c: console.print(f"[green]✓[/] collection {c['name']} atualizada"),
    )


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
        text = "" if sys.stdin.isatty() else read_stdin()
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
    limit: int = typer.Option(5, "--limit", "-n", min=1, max=50),
) -> None:
    """Busca semântica na collection — o mesmo que o agente enxerga."""
    st = state(ctx)
    results = call(st, st.client.search_collection, name, query, limit)

    def render(items: list[dict[str, Any]]) -> None:
        if not items:
            console.print("[dim]nada encontrado[/]")
            return
        for i, doc in enumerate(items, 1):
            console.print(f"[bold cyan]{i}.[/] {doc['content'][:500]}", highlight=False)

    emit(st, results, render)
