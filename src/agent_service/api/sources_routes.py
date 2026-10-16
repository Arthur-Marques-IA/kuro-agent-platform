"""Fontes de uma collection: sites que o Kuro coleta, formata e mantém sincronizados.

`POST /collections/{nome}/sources` cadastra e dispara a primeira sincronização em
background (202): o progresso sai em `GET .../sources/{id}`. A coleta, a formatação
e a indexação estão em `documents/web_sync.py`.
"""

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, computed_field, field_validator

from agent_service.api.collections_routes import _row_or_404
from agent_service.documents import sources_store as store
from agent_service.documents import web, web_sync
from agent_service.tools.egress import EgressBlockedError, EgressUnresolvedError, acheck_url

router = APIRouter(prefix="/collections", tags=["collections"])

SourceMode = Literal["page", "sitemap", "crawl"]
Refresh = Literal["never", "daily", "weekly", "monthly"]


class SourceOptions(BaseModel):
    max_pages: int | None = Field(default=None, ge=1, le=5000, description="Teto de páginas (padrão 50; o serviço limita em WEB_SOURCE_MAX_PAGES).")
    max_depth: int | None = Field(default=None, ge=0, le=10, description="Saltos de link a partir da URL, no modo crawl (padrão 3).")
    include: list[str] | None = Field(default=None, description="Globs de caminho que entram, ex. `/docs/*`.")
    exclude: list[str] | None = Field(default=None, description="Globs de caminho que ficam de fora, ex. `/blog/*`.")
    format: bool | None = Field(default=None, description="Passar cada página pelo agente formatador (padrão true).")
    include_files: bool | None = Field(
        default=None,
        description="Indexar também os arquivos linkados (PDF, DOCX, PPTX, XLSX) do mesmo escopo, dentro de "
        "max_pages e sem o formatador (padrão true nas fontes novas).",
    )
    formatter_provider: str | None = None
    formatter_model_id: str | None = None
    formatter_credential_id: str | None = None


class SourceIn(SourceOptions):
    url: str = Field(..., max_length=2000)
    mode: SourceMode = "crawl"
    refresh: Refresh = "never"
    label: str | None = Field(default=None, max_length=200)

    @field_validator("url")
    @classmethod
    def _http(cls, value: str) -> str:
        value = value.strip()
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("a URL precisa começar com http:// ou https://")
        return value


class SourceUpdateIn(SourceOptions):
    refresh: Refresh | None = None
    label: str | None = Field(default=None, max_length=200)


class SourceOut(BaseModel):
    id: str
    collection: str
    kind: str
    url: str
    label: str | None = None
    mode: str
    options: dict[str, Any]
    refresh: str
    status: str
    """`pending` | `running` | `ready` | `partial` (algumas páginas falharam) | `failed`."""
    status_message: str | None = None
    progress: dict[str, Any] | None = None
    pages_indexed: int
    pages_failed: int
    last_synced_at: datetime | None = None
    next_sync_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SourcePageOut(BaseModel):
    url: str
    title: str | None = None
    status: str
    """`indexed` | `failed` | `skipped` (robots.txt ou sem texto)."""
    error: str | None = None
    """Motivo da falha — ou, numa página indexada, por que o texto do formatador foi descartado."""
    formatted: str | None = None
    """`agent` | `fallback` (texto da extração) | `none` (sem formatador)."""
    chunks: int = 0
    content_id: str | None = None
    fetched_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def kind(self) -> Literal["page", "file"]:
        """`file` para um arquivo linkado (PDF, DOCX...), lido sem o formatador."""
        return "file" if web.file_extension(self.url) else "page"


class SourceDetailOut(SourceOut):
    pages: list[SourcePageOut]


def _source_or_404(collection_name: str, source_id: str) -> dict[str, Any]:
    _row_or_404(collection_name)
    source = store.get_source(source_id)
    if source is None or source["collection"] != collection_name:
        raise HTTPException(status_code=404, detail=f"Fonte {source_id!r} não encontrada em {collection_name!r}")
    return source


async def _check_target(url: str) -> None:
    """A mesma trava de egress das tools, já no cadastro: apontar uma fonte para a
    rede interna falha aqui, com o motivo, e não só no meio da coleta."""
    try:
        await acheck_url(url)
    except EgressUnresolvedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EgressBlockedError as exc:
        raise HTTPException(
            status_code=422, detail=str(exc).replace("Uma tool só alcança", "A coleta só alcança")
        ) from exc


def _check_formatter(options: dict[str, Any]) -> None:
    try:
        web_sync.formatter_model(options)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Formatador indisponível: {exc}. Configure o provedor, escolha outro "
            "(formatter_provider/formatter_model_id) ou use format=false para indexar só o texto extraído.",
        ) from exc


@router.get("/{collection_name}/sources", response_model=list[SourceOut])
def list_sources(collection_name: str) -> list[dict[str, Any]]:
    _row_or_404(collection_name)
    return store.list_sources(collection_name)


@router.post("/{collection_name}/sources", response_model=SourceOut, status_code=202)
async def create_source(collection_name: str, body: SourceIn) -> dict[str, Any]:
    _row_or_404(collection_name)
    await _check_target(body.url)
    options = web_sync.default_options(**body.model_dump(include=set(SourceOptions.model_fields)))
    _check_formatter(options)
    existing = store.find_source(collection_name, body.url, body.mode)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Essa fonte já existe ({existing['id']}). Para coletar de novo: "
            f"POST /collections/{collection_name}/sources/{existing['id']}/sync",
        )
    source = store.create_source(
        collection=collection_name, url=body.url, mode=body.mode, options=options, refresh=body.refresh, label=body.label
    )
    web_sync.start_sync(source["id"])
    return store.get_source(source["id"]) or source


@router.get("/{collection_name}/sources/{source_id}", response_model=SourceDetailOut)
def get_source(collection_name: str, source_id: str) -> dict[str, Any]:
    source = _source_or_404(collection_name, source_id)
    return {**source, "pages": store.list_pages(source_id)}


@router.patch("/{collection_name}/sources/{source_id}", response_model=SourceOut)
def update_source(collection_name: str, source_id: str, body: SourceUpdateIn) -> dict[str, Any]:
    source = _source_or_404(collection_name, source_id)
    changes = body.model_dump(include=set(SourceOptions.model_fields), exclude_none=True)
    options = {**(source["options"] or {}), **changes}
    if changes.keys() & {"format", "formatter_provider", "formatter_model_id", "formatter_credential_id"}:
        _check_formatter(options)
    values: dict[str, Any] = {"options": options}
    if body.label is not None:
        values["label"] = body.label
    if body.refresh is not None:
        values["refresh"] = body.refresh
        values["next_sync_at"] = store.next_sync(body.refresh, source.get("last_synced_at"))
    updated = store.update_source(source_id, **values)
    if body.refresh is not None:
        web_sync.schedule(updated)
    return updated


@router.post("/{collection_name}/sources/{source_id}/sync", response_model=SourceOut, status_code=202)
async def sync_source(collection_name: str, source_id: str) -> dict[str, Any]:
    """Coleta de novo agora: páginas novas entram, as alteradas são substituídas, as
    que sumiram do site saem, e as iguais nem são reprocessadas.

    `async` de propósito: o sync vira uma task no event loop do serviço, que uma
    rota síncrona (rodando numa thread do pool) não enxerga."""
    _source_or_404(collection_name, source_id)
    if not web_sync.start_sync(source_id):
        raise HTTPException(status_code=409, detail="Essa fonte já está sincronizando.")
    return store.get_source(source_id)  # type: ignore[return-value]


@router.delete("/{collection_name}/sources/{source_id}", status_code=204)
async def delete_source(collection_name: str, source_id: str, keep_documents: bool = False) -> Response:
    """Remove a fonte e, a menos de `keep_documents=true`, as páginas dela da collection."""
    source = _source_or_404(collection_name, source_id)
    await web_sync.cancel_sync(source_id)
    if not keep_documents:
        await web_sync.remove_source_pages(source)
    store.delete_source(source_id)
    return Response(status_code=204)
