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
    """Ingestão programática simples: adiciona um texto bruto à coleção.

    Para upload de arquivo/URL com opções de chunking, use o endpoint
    `POST /knowledge/content` exposto pelo AgentOS (ver README).
    """
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
    await knowledge._aload_content(content, upsert=False, skip_if_exists=True)
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

    `type` recebe o MIME, como no router do AgentOS: é dele que o Agno escolhe
    o leitor. Passar a extensão faria um PDF cair no leitor de texto."""
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
    await knowledge._aload_content(content, upsert=False, skip_if_exists=True)
    return content.id


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
    return await knowledge.aremove_content_by_id(content_id)


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
