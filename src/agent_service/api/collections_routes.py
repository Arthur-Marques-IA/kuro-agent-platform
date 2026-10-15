"""CRUD das collections de documentos + ingestão simples e busca.

Uma collection é uma base de conhecimento (pgvector) que um agente pode
consultar: o agente aponta para ela em `knowledge_collection`
(`api/agents_routes.py`) e ganha, com isso, uma tool de busca — é o elo que
antes não existia entre os documentos e os agentes.

Para upload de arquivo/URL com chunking, o AgentOS expõe `/knowledge/content`
(ver README); aqui fica o cadastro em si, a ingestão de texto avulso e a busca.
"""

import re
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import inspect, text

from agent_service.config import get_settings
from agent_service.db import get_db

from agent_service.agents.store import list_definitions
from agent_service.documents import store
from agent_service.documents.collections import (
    add_file,
    add_text,
    default_collection_name,
    get_collection,
    list_documents,
    remove_document,
    search,
)
from agent_service.documents.retrieval import describe_hits
from agent_service.documents.search import (
    DEFAULT_LANGUAGE,
    MAX_RESULTS_LIMIT,
    SearchConfig,
    SearchConfigError,
    validate,
    validate_language,
)
from agent_service.documents import versions
from agent_service.documents.embedder import (
    DEFAULT_EMBEDDER_PROVIDER,
    EMBEDDERS,
    EmbedderNotConfiguredError,
    UnknownEmbedderProviderError,
    check_configured,
    resolve_spec,
)

router = APIRouter(prefix="/collections", tags=["collections"])

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class CollectionIn(BaseModel):
    name: str = Field(..., description="Slug estável — vira o nome da tabela pgvector")
    label: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    embedder_provider: str | None = Field(
        default=None,
        description="Quem gera os vetores: google (padrão), openai ou ollama. Fixo depois de criada.",
    )
    embedder_model: str | None = Field(default=None, max_length=200)
    embedder_dimensions: int | None = Field(
        default=None, gt=0, le=8192, description="Só é preciso com um `embedder_model` fora do padrão do provedor."
    )
    search_mode: Literal["vector", "hybrid"] | None = Field(
        default=None, description="vector (padrão): só similaridade; hybrid: vetor + full-text do Postgres."
    )
    language: str | None = Field(
        default=None,
        description=f"Dicionário do full-text do Postgres ({DEFAULT_LANGUAGE} por padrão). Fixo depois de criada.",
    )
    max_results: int | None = Field(default=None, ge=1, le=MAX_RESULTS_LIMIT, description="Trechos por busca (k).")
    vector_weight: float | None = Field(
        default=None, ge=0, le=1, description="No híbrido, o peso do vetor (o resto é do full-text). Padrão 0,5."
    )
    reranker: str | None = Field(default=None, description="Gancho para reranker; nenhum suportado por enquanto.")


class CollectionUpdateIn(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    search_mode: Literal["vector", "hybrid"] | None = None
    max_results: int | None = Field(default=None, ge=1, le=MAX_RESULTS_LIMIT)
    vector_weight: float | None = Field(default=None, ge=0, le=1)
    reranker: str | None = None
    language: str | None = Field(default=None, description="Não muda depois da criação: só aceita o valor atual.")


class SearchConfigOut(BaseModel):
    search_mode: str
    language: str
    max_results: int
    vector_weight: float
    reranker: str | None = None
    score_kind: str
    """O que o `score` dos trechos mede: similarity (vetorial), hybrid (vetor + texto) ou rerank."""


class CollectionVersionOut(BaseModel):
    version: int
    config_hash: str
    config: dict[str, Any]
    created_at: datetime


class CollectionOut(BaseModel):
    name: str
    is_default: bool = False
    """Coleção que o pipeline de upload do AgentOS alimenta (arquivo/URL)."""
    label: str
    description: str | None
    is_seed: bool
    embedder_provider: str
    embedder_model: str
    embedder_dimensions: int | None = None
    search: SearchConfigOut
    version: int | None = None
    """Versão atual da configuração (embedder + busca), ver `documents/versions.py`."""
    config_hash: str | None = None
    agents_using: list[str] = []
    """Agentes com `knowledge_collection` apontando para cá — avisa antes de excluir."""
    created_at: datetime
    updated_at: datetime


class AddTextRequest(BaseModel):
    text: str
    name: str | None = None
    metadata: dict[str, Any] | None = None


class SearchResult(BaseModel):
    """Um trecho encontrado — o mesmo formato de `metadata.references` na span
    da busca do agente (`documents/retrieval.py`)."""

    content: str
    metadata: dict[str, Any] | None = None
    rank: int | None = None
    score: float | None = Field(
        default=None, description="Similaridade (0–1), score híbrido (0–1) ou o do reranker — ver `score_kind`"
    )
    score_kind: str | None = None
    document: str | None = None
    content_id: str | None = None
    chunk_id: str | None = None
    chunk: int | None = None
    section: str | None = None
    source_url: str | None = None
    page_title: str | None = None


def _agents_using(name: str) -> list[str]:
    return [d["agent_type"] for d in list_definitions() if d.get("knowledge_collection") == name]


def _out(row: dict[str, Any]) -> dict[str, Any]:
    # `embedder_provider` sai preenchido mesmo para as collections anteriores à
    # coluna: quem lê precisa saber qual embedder responde, não se a linha é velha.
    spec = resolve_spec(row.get("embedder_provider"))
    version, digest = versions.ensure_version(row["name"], row)
    return {
        **row,
        "search": SearchConfig.from_row(row).public(),
        "version": version,
        "config_hash": digest,
        "embedder_provider": spec.provider,
        "embedder_model": row.get("embedder_model") or spec.default_model_id,
        "agents_using": _agents_using(row["name"]),
        "is_default": row["name"] == default_collection_name(),
    }


class EmbedderOut(BaseModel):
    provider: str
    label: str
    default_model_id: str
    default_dimensions: int | None
    requires_api_key: bool
    description: str
    configured: bool
    """A credencial existe agora — uma collection neste provedor indexaria."""


@router.get("/embedders", response_model=list[EmbedderOut])
def list_embedders() -> list[dict[str, Any]]:
    """Provedores de embedding e quais já têm credencial. Declarada antes de
    `/{collection_name}` porque o FastAPI casa as rotas na ordem."""
    saida = []
    for spec in EMBEDDERS.values():
        try:
            check_configured(spec.provider)
            configured = True
        except EmbedderNotConfiguredError:
            configured = False
        saida.append({**vars(spec), "configured": configured})
    return saida


def _row_or_404(name: str) -> dict[str, Any]:
    row = store.get_collection_row(name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Coleção desconhecida: {name!r}")
    return row


_VECTOR_SCHEMA = "ai"


def _reject_orphan_vectors(name: str) -> None:
    """Recusa recriar uma collection cujos vetores antigos ainda estão lá.

    `delete_collection` só apaga o cadastro — a tabela `knowledge_<nome>` é do
    Agno e fica. Recriar com outro embedder juntaria vetores de dois embedders
    na mesma tabela; se as larguras baterem (Gemini e text-embedding-3-small
    têm ambos 1536), o pgvector não reclama e a busca sai errada em silêncio."""
    engine = get_db().db_engine
    tabela = f"knowledge_{name}"
    # Schema "ai": é o default do `PgVector` do Agno, e `documents/collections.py`
    # não passa outro. Procurar no schema padrão da conexão não acharia nada.
    if not inspect(engine).has_table(tabela, schema=_VECTOR_SCHEMA):
        return
    with engine.begin() as conn:
        existentes = conn.execute(text(f'SELECT 1 FROM "{_VECTOR_SCHEMA}"."{tabela}" LIMIT 1')).fetchone()
    if existentes is not None:
        raise HTTPException(
            status_code=409,
            detail=f"A tabela de vetores {_VECTOR_SCHEMA}.{tabela} de uma collection {name!r} anterior ainda "
            "tem documentos. Reaproveitá-los só é seguro com o mesmo embedder, e o cadastro que dizia qual era "
            f'já foi apagado — use outro nome, ou apague a tabela (DROP TABLE "{_VECTOR_SCHEMA}"."{tabela}").',
        )


@router.get("", response_model=list[CollectionOut])
def list_collections() -> list[dict[str, Any]]:
    return [_out(row) for row in store.list_collections()]


@router.post("", response_model=CollectionOut, status_code=201)
def create_collection(body: CollectionIn) -> dict[str, Any]:
    if not _SLUG_RE.match(body.name):
        raise HTTPException(status_code=422, detail="name deve ser um slug: minúsculas, números, '-' ou '_'")
    if store.get_collection_row(body.name) is not None:
        raise HTTPException(status_code=409, detail=f"Coleção {body.name!r} já existe")
    # Falhar aqui, e não na primeira ingestão: uma collection criada com um
    # provedor sem chave parece pronta e só quebra quando alguém manda um documento.
    try:
        spec = resolve_spec(body.embedder_provider)
        # Modelo trocado sem a dimensão junto seria chute: a tabela pgvector
        # nasceria com a largura do modelo padrão e a busca sairia errada calada.
        if body.embedder_model and body.embedder_model != spec.default_model_id and not body.embedder_dimensions:
            raise HTTPException(
                status_code=422,
                detail=f"embedder_model={body.embedder_model!r} não é o padrão de {spec.provider!r} "
                "— informe também embedder_dimensions (o tamanho do vetor que esse modelo devolve).",
            )
        check_configured(body.embedder_provider, body.embedder_model)
    except (UnknownEmbedderProviderError, EmbedderNotConfiguredError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    search = {
        "search_mode": body.search_mode,
        "language": body.language or DEFAULT_LANGUAGE,
        "max_results": body.max_results,
        "vector_weight": body.vector_weight,
        "reranker": body.reranker,
    }
    try:
        validate(search)
        validate_language(search["language"], get_db().db_engine)
    except SearchConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _reject_orphan_vectors(body.name)
    created = store.create_collection(
        name=body.name,
        label=body.label,
        description=body.description,
        embedder_provider=(body.embedder_provider or DEFAULT_EMBEDDER_PROVIDER).lower(),
        embedder_model=body.embedder_model,
        embedder_dimensions=body.embedder_dimensions,
        search={k: v for k, v in search.items() if v is not None},
    )
    return _out(created)


@router.get("/{collection_name}", response_model=CollectionOut)
def get_collection_detail(collection_name: str) -> dict[str, Any]:
    return _out(_row_or_404(collection_name))


@router.put("/{collection_name}", response_model=CollectionOut)
def update_collection(collection_name: str, body: CollectionUpdateIn) -> dict[str, Any]:
    """Os campos de busca mandados (inclusive `null`, que volta ao padrão) gravam
    como vieram; os ausentes ficam. Mudar a busca gera uma versão nova."""
    row = _row_or_404(collection_name)
    sent = body.model_dump(exclude_unset=True)
    current_language = SearchConfig.from_row(row).content_language
    if "language" in sent and sent["language"] != current_language:
        raise HTTPException(
            status_code=422,
            detail=f"language é fixo depois da criação (esta usa {current_language!r}): o dicionário decide "
            "quais trechos casam. Para trocar, crie outra collection e reindexe.",
        )
    search = {k: v for k, v in sent.items() if k in store.SEARCH_FIELDS}
    try:
        validate(search)
    except SearchConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _out(store.update_collection(collection_name, label=body.label, description=body.description, search=search))


@router.get("/{collection_name}/versions", response_model=list[CollectionVersionOut])
def list_collection_versions(collection_name: str) -> list[dict[str, Any]]:
    """Histórico da configuração (embedder + busca), mais recente primeiro. Cada run
    grava a versão com que buscou (`collection_version` em `/observability/runs`)."""
    row = _row_or_404(collection_name)
    versions.ensure_version(collection_name, row)
    return versions.list_versions(collection_name)


@router.get("/{collection_name}/versions/{version}", response_model=CollectionVersionOut)
def get_collection_version(collection_name: str, version: int) -> dict[str, Any]:
    _row_or_404(collection_name)
    found = versions.get_version(collection_name, version)
    if found is None:
        raise HTTPException(status_code=404, detail=f"{collection_name!r} não tem a versão {version}")
    return found


@router.delete("/{collection_name}", status_code=204)
def delete_collection(collection_name: str) -> None:
    row = _row_or_404(collection_name)
    if row["is_seed"]:
        raise HTTPException(status_code=403, detail="Coleção semeada pelo sistema não pode ser removida")
    using = _agents_using(collection_name)
    if using:
        raise HTTPException(
            status_code=409,
            detail=f"Coleção em uso por: {', '.join(using)}. Tire-a desses agentes primeiro.",
        )
    # As fontes de site saem junto: o agendador não pode seguir coletando para
    # uma collection que não existe mais.
    from agent_service.documents import sources_store

    for source in sources_store.list_sources(collection_name):
        sources_store.delete_source(source["id"])
    store.delete_collection(collection_name)


@router.post("/{collection_name}/documents", status_code=201)
async def ingest_text(collection_name: str, request: AddTextRequest) -> dict[str, str]:
    _row_or_404(collection_name)
    content_id = await add_text(collection_name, text=request.text, name=request.name, metadata=request.metadata)
    return {"content_id": content_id}


@router.post("/{collection_name}/files", status_code=201)
async def ingest_file(
    collection_name: str,
    file: UploadFile = File(..., description="PDF, DOCX, CSV, TXT, MD..."),
    name: str | None = Form(default=None, description="Nome do documento (padrão: o do arquivo)."),
) -> dict[str, str]:
    """Indexa um arquivo na collection, com o embedder dela.

    Existe além do `/knowledge/content` do AgentOS porque aquele só enxerga as
    coleções registradas no boot — o console ficava preso à coleção padrão e a
    CLI não tinha upload nenhum."""
    _row_or_404(collection_name)
    conteudo = await file.read()
    if not conteudo:
        raise HTTPException(status_code=422, detail="arquivo vazio")
    limite = get_settings().max_attachment_mb * 1024 * 1024
    if len(conteudo) > limite:
        raise HTTPException(
            status_code=413,
            detail=f"arquivo tem {len(conteudo) // 1024 // 1024}MB e o limite é "
            f"{get_settings().max_attachment_mb}MB (MAX_ATTACHMENT_MB)",
        )
    content_id = await add_file(
        collection_name,
        content_bytes=conteudo,
        filename=file.filename or "documento",
        content_type=file.content_type,
        name=name,
    )
    return {"content_id": content_id}


class DocumentOut(BaseModel):
    id: str
    name: str | None = None
    description: str | None = None
    status: str | None = None
    status_message: str | None = None
    type: str | None = None
    size: int | None = None
    created_at: int | None = None
    updated_at: int | None = None


class DocumentPageMeta(BaseModel):
    page: int
    limit: int
    total_count: int
    total_pages: int


class DocumentPage(BaseModel):
    data: list[DocumentOut]
    meta: DocumentPageMeta


@router.get("/{collection_name}/documents", response_model=DocumentPage)
async def list_collection_documents(
    collection_name: str, limit: int = 50, page: int = 1
) -> dict[str, Any]:
    """O que está indexado nesta coleção, com o status do processamento.

    Paginado porque o console lista isso numa tabela — e porque a tabela de
    conteúdo é compartilhada por todas as coleções, então sem o recorte por
    coleção (que o Agno faz por `linked_to`) viriam documentos alheios."""
    _row_or_404(collection_name)
    documentos, total = await list_documents(collection_name, limit=limit, page=page)
    return {
        "data": documentos,
        "meta": {
            "page": page,
            "limit": limit,
            "total_count": total,
            "total_pages": max(1, -(-total // limit)),
        },
    }


@router.delete("/{collection_name}/documents/{content_id}", status_code=204)
async def delete_collection_document(collection_name: str, content_id: str) -> None:
    """Remove o documento e os vetores dele — é como se tira da base uma
    resposta errada que o agente estava citando."""
    _row_or_404(collection_name)
    if not await remove_document(collection_name, content_id):
        raise HTTPException(
            status_code=404, detail=f"Documento {content_id!r} não encontrado em {collection_name!r}"
        )
    # Página de um site: sem o registro dela, o próximo sync acharia a página
    # "igual" e não a indexaria de novo — ficaria contada como indexada sem estar.
    # Apagando o registro, ela volta no próximo sync (para tirar de vez, use
    # `exclude` na fonte).
    from agent_service.documents import sources_store

    page = sources_store.source_of_content(content_id)
    if page is not None:
        sources_store.delete_page(page["source_id"], page["url"])
        source = sources_store.get_source(page["source_id"])
        if source is not None:
            sources_store.update_source(page["source_id"], pages_indexed=max(source["pages_indexed"] - 1, 0))


@router.get("/{collection_name}/search", response_model=list[SearchResult])
async def search_collection(
    collection_name: str, query: str, limit: int | None = Query(default=None, ge=1, le=MAX_RESULTS_LIMIT)
) -> list[SearchResult]:
    """Sem `limit`, o k da collection: o mesmo que o agente enxerga."""
    row = _row_or_404(collection_name)
    documents = await search(collection_name, query=query, limit=limit)
    hits = await describe_hits(get_collection(collection_name), documents)
    kind = SearchConfig.from_row(row).score_kind
    return [
        SearchResult(
            **{k: v for k, v in hit.items() if k in SearchResult.model_fields},
            metadata=doc.meta_data,
            score_kind="rerank" if doc.reranking_score is not None else kind,
        )
        for hit, doc in zip(hits, documents)
    ]
