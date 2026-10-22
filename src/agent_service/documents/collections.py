"""Collections de documentos (RAG) sobre Postgres+pgvector.

Cada collection cadastrada (`documents/store.py`) vira uma `Knowledge` (base de
conhecimento do Agno) própria, com sua tabela pgvector. Um agente aponta para
uma delas em `knowledge_collection` e ganha uma tool de busca (`agents/base.py`).
As collections são registradas no
AgentOS (`main.py`, `knowledge=all_collections()`), que já expõe o pipeline de
ingestão completo (upload de arquivo, texto ou URL, com chunking configurável
e processamento assíncrono) em `/knowledge/content`, além de busca em
`/knowledge/search` — ver README para o contrato exato desses endpoints.

`add_text` cobre o caso de ingestão simples e programática (ex. futuramente a
partir de uma tool do agente analista), sem precisar montar um multipart/form.
`add_file` existe porque o pipeline do AgentOS só enxerga as collections
registradas no boot e não aceita escolher a collection — aqui a collection é
resolvida por nome, como no resto do módulo. A ingestão por URL continua lá.
"""

import asyncio
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from agno.knowledge.content import Content, FileData
from agno.knowledge.knowledge import Knowledge
from agno.utils.string import generate_id
from agno.vectordb.pgvector import PgVector
from agno.vectordb.search import SearchType
from sqlalchemy import select

from agent_service.config import get_settings
from agent_service.db import get_db
from agent_service.documents.embedder import (
    EmbedderNotConfiguredError,
    UnknownEmbedderProviderError,
    build_embedder,
)
from agent_service.documents.search import SearchConfig, build_reranker
from agent_service.documents.store import SEED_COLLECTION, get_collection_row, list_collection_names


logger = logging.getLogger(__name__)

EmbedderError = (EmbedderNotConfiguredError, UnknownEmbedderProviderError)


def collection_exists(name: str) -> bool:
    return name in list_collection_names()


def get_collection(name: str) -> Knowledge:
    """Retorna (criando se necessário) a coleção de documentos `name`.

    O embedder vem do cadastro da collection (`documents/store.py`), não de uma
    constante: coleção sem provedor gravado continua no Gemini, que é o que
    todas usavam antes. O cache é por (nome, embedder) porque uma rotação de
    credencial só é relevante no restart — a chave já está dentro do cliente.

    A configuração de busca (`documents/search.py`) também entra na chave: mudar
    o modo ou o k monta outra `Knowledge` na próxima busca, sem restart."""
    return _from_row(name, get_collection_row(name) or {})


def _from_row(name: str, row: dict[str, Any]) -> Knowledge:
    return _build_collection(
        name,
        row.get("embedder_provider"),
        row.get("embedder_model"),
        row.get("embedder_dimensions"),
        SearchConfig.from_row(row),
    )


@lru_cache
def _build_collection(
    name: str,
    embedder_provider: str | None,
    embedder_model: str | None,
    embedder_dimensions: int | None,
    search: SearchConfig = SearchConfig(),
) -> Knowledge:
    vector_db = PgVector(
        table_name=f"knowledge_{name}",
        db_url=get_settings().database_url,
        embedder=build_embedder(embedder_provider, embedder_model, embedder_dimensions),
        search_type=SearchType.hybrid if search.search_mode == "hybrid" else SearchType.vector,
        vector_score_weight=search.vector_weight,
        content_language=search.content_language,
        reranker=build_reranker(search.reranker),
    )
    return Knowledge(
        name=name,
        vector_db=vector_db,
        contents_db=get_db(),
        max_results=search.max_results,
    )


def resolve_search(name: str) -> tuple[Knowledge, SearchConfig, tuple[int, str]]:
    """A collection como está agora, a configuração de busca e a versão dela —
    o que o retriever do agente usa a cada busca (`documents/retrieval.py`)."""
    from agent_service.documents.versions import ensure_version

    row = get_collection_row(name) or {"name": name}
    return _from_row(name, row), SearchConfig.from_row(row), ensure_version(name, row)


def default_collection_name() -> str:
    """Coleção que o pipeline de ingestão do AgentOS usa (a primeira registrada):
    a semeada, quando existir. O console precisa saber disto para não oferecer
    upload de arquivo/URL numa coleção que não receberia o conteúdo."""
    nomes = list_collection_names()
    return SEED_COLLECTION if SEED_COLLECTION in nomes else (nomes[0] if nomes else SEED_COLLECTION)


def all_collections() -> list[Knowledge]:
    """Todas as collections cadastradas, para registrar no AgentOS no startup.

    Uma collection criada depois do boot funciona normalmente no `/chat` (o
    agente a resolve por nome), só não aparece nas rotas de knowledge do
    AgentOS até o próximo restart — mesma ressalva dos agentes."""
    nomes = list_collection_names()
    padrao = default_collection_name()
    # A padrão primeiro: é ela que o AgentOS usa quando ninguém escolhe.
    ordenadas = [padrao] + [n for n in nomes if n != padrao] if padrao in nomes else nomes

    montadas = []
    for name in ordenadas:
        try:
            montadas.append(get_collection(name))
        except EmbedderError:
            # Uma collection apontando para um provedor sem credencial não pode
            # derrubar o serviço no boot: as outras seguem, e quem chamar esta
            # recebe o erro na hora, com o motivo.
            logger.warning("Collection %r ficou fora do AgentOS: embedder não configurado", name, exc_info=True)
    return montadas


async def add_text(
    collection_name: str,
    *,
    text: str,
    name: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Ingestão programática simples: adiciona um texto bruto à coleção, cortado
    pela mesma regra das páginas de site (`_chunked`)."""
    knowledge = get_collection(collection_name)
    content = Content(
        name=name or "texto-avulso",
        file_data=FileData(content=text.encode("utf-8"), type="manual"),
        metadata=metadata,
    )
    # Mesmo padrão do router do AgentOS (`/knowledge/content`): sem isso,
    # `content.id` fica None e o Agno acaba gerando um id não-determinístico
    # por baixo dos panos — o que fez duas ingestões de textos diferentes
    # colidirem no mesmo id durante os testes deste endpoint.
    # A tabela de conteúdo (`agno_knowledge`) é uma só para todas as coleções,
    # enquanto os vetores ficam em `knowledge_<coleção>`. Sem o nome da coleção
    # no hash, o mesmo texto em duas coleções vira uma linha só — e um
    # `skip_if_exists` de outra coleção poderia pular a indexação.
    content.content_hash = knowledge._build_content_hash(content)
    content.id = generate_id(f"{collection_name}:{content.content_hash}")
    _chunked(content, [(None, text)])
    await _load_first_revision(knowledge, collection_name, content, text=text)
    return content.id


async def add_file(
    collection_name: str,
    *,
    content_bytes: bytes,
    filename: str,
    content_type: str | None = None,
    name: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Ingestão de um arquivo (PDF, DOCX, CSV, TXT...) numa coleção qualquer.

    O AgentOS expõe `POST /knowledge/content`, mas ele só enxerga as coleções
    registradas no boot e o console acabou preso à coleção padrão. Aqui a
    coleção é resolvida por nome como em todo o resto — então vale para uma
    criada depois do boot, e com o embedder dela (`documents/embedder.py`).

    O texto sai do arquivo página por página e é cortado pela mesma regra das
    páginas de site (`_chunked`); CSV fica com o leitor do Agno, uma linha por
    trecho. `type` recebe o MIME, como no router do AgentOS: é dele que o Agno
    escolhe o leitor nesse caso."""
    knowledge = get_collection(collection_name)
    content = Content(
        name=name or Path(filename).stem,
        file_data=FileData(
            content=content_bytes,
            type=content_type or None,
            filename=filename,
            size=len(content_bytes),
        ),
        metadata=metadata,
    )
    # Mesmo id determinístico do `add_text`, pela mesma razão: a tabela de
    # conteúdo é uma só para todas as coleções.
    content.content_hash = knowledge._build_content_hash(content)
    content.id = generate_id(f"{collection_name}:{content.content_hash}")
    text = await _chunk_file(content, content_bytes, filename)
    await _load_first_revision(knowledge, collection_name, content, text=text, filename=filename)
    return content.id


def _chunked(content: Content, pages: list[tuple[int | None, str]]) -> str:
    """Põe no `content` os trechos já cortados por `chunk_markdown` (a regra de todo
    documento: não separa pergunta de resposta nem título de conteúdo) e devolve o texto."""
    from agent_service.documents.web_sync import PreChunked, prechunk

    content.reader = PreChunked(prechunk(pages, name=content.name or "documento"))
    return "\n\n".join(text for _, text in pages)


async def _chunk_file(content: Content, body: bytes, filename: str) -> str | None:
    """O texto extraído do arquivo (o que vai para a revisão), já cortado no `content`.
    `None` quando fica com o leitor do Agno: CSV, ou arquivo sem texto extraível."""
    from agent_service.documents.web_sync import extract_pages

    try:
        pages = await asyncio.to_thread(extract_pages, body, filename)
    except Exception:
        logger.warning("Não consegui extrair o texto de %s; fica com o leitor do Agno", filename, exc_info=True)
        return None
    if not pages or not any(text.strip() for _, text in pages):
        return None
    return _chunked(content, pages)


async def _load_first_revision(
    knowledge: Knowledge, collection_name: str, content: Content, *, text: str | None = None, filename: str | None = None
) -> None:
    """Indexa um documento novo como revisão 1 (ou a seguinte, se ele já foi
    removido e volta agora).

    A revisão entra na metadata depois do hash e do id: com ela dentro, o mesmo
    texto mandado de novo ganharia outro id em vez de cair no `skip_if_exists`."""
    from agent_service.documents import revisions

    if await knowledge.aget_content_by_id(content.id) is not None:
        return  # já indexado: o mesmo conteúdo de novo não vira revisão
    last = revisions.latest(content.id)
    number = 1 if last is None else last["revision"] + 1
    content.metadata = {**(content.metadata or {}), revisions.FIELD: number}
    await knowledge._aload_content(content, upsert=False, skip_if_exists=True)
    revisions.record(
        collection_name,
        content.id,
        revision=number,
        content_hash=content.content_hash,
        text=text,
        filename=filename,
        note=None if last is None else "indexado de novo depois de removido",
    )


async def list_documents(
    collection_name: str, *, limit: int = 50, page: int = 1
) -> tuple[list[dict[str, Any]], int]:
    """Documentos indexados nesta coleção.

    A tabela de conteúdo (`agno_knowledge`) é uma só para todas as coleções — o
    recorte vem do próprio Agno, que filtra por `linked_to=<nome da coleção>`."""
    knowledge = get_collection(collection_name)
    documentos, total = await knowledge.aget_content(limit=limit, page=page)
    return [
        {
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "status": getattr(c.status, "value", c.status),
            "status_message": c.status_message,
            "type": c.file_type,
            "size": c.size,
            "created_at": c.created_at,
            "updated_at": c.updated_at,
        }
        for c in documentos
    ], total


async def remove_document(collection_name: str, content_id: str) -> bool:
    """Apaga o documento e os vetores dele. `False` se o id não é da coleção.

    A conferência é necessária: `aremove_content_by_id` acha o documento pelo id
    em toda a tabela, então sem isso um id de outra coleção seria apagado por
    uma chamada endereçada a esta."""
    knowledge = get_collection(collection_name)
    if not await _belongs(knowledge, content_id):
        return False
    removed = await knowledge.aremove_content_by_id(content_id)
    if removed:
        from agent_service.observability import chunk_feedback

        # Não há mais o que corrigir: as marcações abertas dele fecham com o motivo.
        chunk_feedback.close_for_document(content_id, status="resolved", note="documento removido")
    return removed


class DocumentNotFoundError(LookupError):
    """O documento não é desta collection."""


class DocumentConflictError(ValueError):
    """A edição não pode ser feita assim (página de site, ou texto igual ao de outro documento)."""


async def replace_document(
    collection_name: str,
    content_id: str,
    *,
    text: str | None = None,
    content_bytes: bytes | None = None,
    filename: str | None = None,
    content_type: str | None = None,
    name: str | None = None,
    note: str | None = None,
    created_by: str | None = None,
    resolves: list[str] | None = None,
) -> dict[str, Any]:
    """Corrige um documento mantendo o `content_id`: apaga os vetores, indexa o
    conteúdo novo e grava a revisão seguinte. As marcações em `resolves` (ids de
    `chunk_feedback` deste documento) ficam resolvidas nesta revisão; `None`
    resolve todas as abertas dele, e `[]` nenhuma.

    Mesmo texto da revisão atual não vira revisão nova (`changed: false`). Página
    de um site dá `DocumentConflictError`: o próximo sync sobrescreveria a edição,
    então a correção é na fonte."""
    from agent_service.documents import revisions, sources_store
    from agent_service.observability import chunk_feedback

    if (text is None) == (content_bytes is None):
        raise ValueError("mande o texto ou o arquivo, um dos dois")
    knowledge = get_collection(collection_name)
    existing = await knowledge.aget_content_by_id(content_id)
    if existing is None or not await _belongs(knowledge, content_id):
        raise DocumentNotFoundError(content_id)
    if sources_store.source_of_content(content_id) is not None:
        raise DocumentConflictError(
            "este documento é uma página de site: corrija na fonte e rode o sync da collection"
        )

    user_metadata = {
        k: v for k, v in (existing.metadata or {}).items() if not k.startswith("_") and k != revisions.FIELD
    }
    if text is not None:
        file_data = FileData(content=text.encode("utf-8"), type="manual")
    else:
        file_data = FileData(content=content_bytes, type=content_type or None, filename=filename, size=len(content_bytes))
    content = Content(
        id=content_id,
        name=name or existing.name,
        description=existing.description,
        file_data=file_data,
        metadata=user_metadata or None,
    )
    if resolves is None:
        resolves = [f["id"] for f in chunk_feedback.list_feedback(content_id=content_id, status="open")]
    content_hash = knowledge._build_content_hash(content)
    current = revisions.current(collection_name, content_id)
    last = revisions.latest(content_id) or {}
    if last.get("content_hash") == content_hash:
        # Nada mudou, então nada foi corrigido: as marcações continuam como estão.
        return {"content_id": content_id, "revision": current, "changed": False, "resolved": 0}
    twin = generate_id(f"{collection_name}:{content_hash}")
    if twin != content_id and await knowledge.aget_content_by_id(twin) is not None:
        raise DocumentConflictError(f"o conteúdo novo é igual ao do documento {twin!r} desta collection")

    if text is not None:
        _chunked(content, [(None, text)])
    else:
        text = await _chunk_file(content, content_bytes, filename or "documento")
    number = revisions.next_number(content_id)
    content.content_hash = content_hash
    content.metadata = {**user_metadata, revisions.FIELD: number}
    await knowledge.aremove_content_by_id(content_id)
    await knowledge._aload_content(content, upsert=True, skip_if_exists=False)
    revisions.record(
        collection_name,
        content_id,
        revision=number,
        content_hash=content_hash,
        text=text,
        filename=filename,
        note=note,
        created_by=created_by,
    )
    resolved = chunk_feedback.resolve(resolves or [], content_id=content_id, revision=number, by=created_by, note=note)
    return {"content_id": content_id, "revision": number, "changed": True, "resolved": resolved}


def _indexed_chunks(knowledge: Knowledge, content_id: str) -> list[tuple[str, dict[str, Any]]]:
    """(texto, meta_data) de cada trecho do documento, na ordem: página, depois número."""
    vector_db = getattr(knowledge, "vector_db", None)
    table, session = getattr(vector_db, "table", None), getattr(vector_db, "Session", None)
    if table is None or session is None:
        return []
    with session() as sess:
        rows = sess.execute(select(table.c.content, table.c.meta_data).where(table.c.content_id == content_id)).all()

    def order(row: Any) -> tuple[int, int]:
        meta = row.meta_data or {}
        return (int(meta.get("page") or 0), int(meta.get("chunk") or 0))

    return [(row.content or "", dict(row.meta_data or {})) for row in sorted(rows, key=order)]


async def document_pages(collection_name: str, content_id: str) -> list[tuple[int | None, str]]:
    """O texto do documento remontado dos trechos indexados, página por página.

    Os leitores não sobrepõem trechos (`overlap=0` no Agno, e `chunk_markdown` corta
    entre blocos), então juntar os trechos na ordem devolve o texto extraído — é o que
    se mostra para corrigir um arquivo, ou um documento de antes do histórico."""
    knowledge = get_collection(collection_name)
    chunks = await asyncio.to_thread(_indexed_chunks, knowledge, content_id)
    pages: list[tuple[int | None, list[str]]] = []
    for text, meta in chunks:
        page = meta.get("page")
        if not pages or pages[-1][0] != page:
            pages.append((page, []))
        pages[-1][1].append(text)
    return [(page, "\n\n".join(texts)) for page, texts in pages]


async def rechunk_document(collection_name: str, content_id: str, *, created_by: str | None = None) -> dict[str, Any]:
    """Corta de novo um documento já indexado pela regra atual (`chunk_markdown`),
    a partir do texto dos trechos dele: mesmo `content_id`, mesmas páginas, revisão
    nova ("recortado"). Página de site fica de fora: o sync a recorta da fonte."""
    from agent_service.documents import revisions, sources_store

    knowledge = get_collection(collection_name)
    existing = await knowledge.aget_content_by_id(content_id)
    if existing is None:
        raise DocumentNotFoundError(content_id)
    if sources_store.source_of_content(content_id) is not None:
        return {"content_id": content_id, "status": "skipped", "reason": "página de site: o sync recorta da fonte"}
    pages = await document_pages(collection_name, content_id)
    if not any(text.strip() for _, text in pages):
        return {"content_id": content_id, "status": "skipped", "reason": "sem trechos indexados"}
    before = sum(1 for _ in await asyncio.to_thread(_indexed_chunks, knowledge, content_id))
    user_metadata = {
        k: v for k, v in (existing.metadata or {}).items() if not k.startswith("_") and k != revisions.FIELD
    }
    revisions.current(collection_name, content_id)  # documento antigo ganha a revisão 1 antes
    number = revisions.next_number(content_id)
    content = Content(
        id=content_id,
        name=existing.name,
        description=existing.description,
        file_data=FileData(content="\n\n".join(t for _, t in pages).encode("utf-8"), type="manual"),
        metadata={**user_metadata, revisions.FIELD: number},
    )
    text = _chunked(content, pages)
    after = len(content.reader.documents)
    # O hash continua o do conteúdo original: o mesmo arquivo enviado de novo ainda cai no mesmo id.
    content.content_hash = existing.content_hash or knowledge._build_content_hash(content)
    await knowledge.aremove_content_by_id(content_id)
    await knowledge._aload_content(content, upsert=True, skip_if_exists=False)
    revisions.record(
        collection_name,
        content_id,
        revision=number,
        content_hash=(revisions.latest(content_id) or {}).get("content_hash"),
        text=text,
        note="recortado com a regra de trechos atual",
        created_by=created_by,
    )
    return {"content_id": content_id, "status": "rechunked", "revision": number, "chunks_before": before, "chunks_after": after}


async def reindex_collection(collection_name: str, *, created_by: str | None = None) -> list[dict[str, Any]]:
    """`rechunk_document` em todos os documentos da collection (as páginas de site ficam de fora)."""
    knowledge = get_collection(collection_name)
    ids: list[str] = []
    page = 1
    while True:
        documentos, total = await knowledge.aget_content(limit=500, page=page)
        ids.extend(c.id for c in documentos if c.id)
        if not documentos or page * 500 >= total:
            break
        page += 1
    report = []
    for content_id in ids:
        try:
            report.append(await rechunk_document(collection_name, content_id, created_by=created_by))
        except Exception as exc:  # um documento ruim não para os outros
            logger.warning("Falha ao recortar %s em %s", content_id, collection_name, exc_info=True)
            report.append({"content_id": content_id, "status": "failed", "reason": f"{type(exc).__name__}: {exc}"})
    return report


async def _belongs(knowledge: Knowledge, content_id: str) -> bool:
    """O documento é desta coleção? Primeiro pelos vetores dela (uma consulta só,
    vale para coleções de qualquer tamanho — um site indexado passa fácil de mil
    documentos); um documento sem vetores (falhou na indexação) cai na varredura."""
    vector_db = getattr(knowledge, "vector_db", None)
    table, session = getattr(vector_db, "table", None), getattr(vector_db, "Session", None)
    if table is not None and session is not None:

        def found() -> bool:
            with session() as sess:
                return sess.execute(select(table.c.id).where(table.c.content_id == content_id).limit(1)).first() is not None

        try:
            if await asyncio.to_thread(found):
                return True
        except Exception:
            logger.debug("Busca do documento nos vetores falhou; seguindo pela varredura", exc_info=True)
    page = 1
    while True:
        documentos, total = await knowledge.aget_content(limit=500, page=page)
        if any(c.id == content_id for c in documentos):
            return True
        if not documentos or page * 500 >= total:
            return False
        page += 1


async def search(collection_name: str, *, query: str, limit: int | None = None) -> list[Any]:
    """Sem `limit`, o k da collection — o mesmo que o agente recebe."""
    knowledge = get_collection(collection_name)
    return await knowledge.asearch(query=query, max_results=limit or knowledge.max_results)
