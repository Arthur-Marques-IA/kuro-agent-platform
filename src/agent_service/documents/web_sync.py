"""Sincronização de um site com uma collection — e o agendador que a repete.

Uma fonte (`sources_store.py`) diz o que coletar:

- `page`: só a URL dada;
- `sitemap`: as páginas do sitemap (a URL dada, os do `robots.txt` ou `/sitemap.xml`);
- `crawl`: a partir da URL, seguindo links do mesmo site e abaixo do mesmo caminho,
  até `max_depth` saltos e `max_pages` páginas.

Cada página passa por: download seguro (`web.py`) → extração do conteúdo principal →
formatação por um agente com checagem de fidelidade (`web_formatter.py`) → trechos
por seção → embedding na collection. O documento de cada página tem id fixo pela
URL (`page_content_id`): ressincronizar **substitui** a página em vez de duplicá-la,
e a página cujo texto não mudou nem é reprocessada (hash do texto extraído). Página
que sumiu do site (fora da descoberta, 404 ou 410) sai da collection.

Roda em background (`start_sync`): uma sincronização de centenas de páginas, cada
uma com uma chamada ao modelo, não cabe numa requisição HTTP. O progresso fica na
própria fonte, e `status='running'` é a trava contra dois syncs ao mesmo tempo.
"""

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from agno.knowledge.content import Content, FileData
from agno.knowledge.document import Document
from agno.knowledge.knowledge import Knowledge
from agno.knowledge.reader.base import Reader
from agno.utils.string import generate_id

from agent_service.config import get_settings
from agent_service.documents import sources_store as store
from agent_service.documents import web
from agent_service.documents.web_formatter import chunk_markdown, format_page

logger = logging.getLogger(__name__)

MIN_PAGE_CHARS = 40
MAX_SITEMAPS = 50
SCHEDULER_INTERVAL_SECONDS = 300
GONE = ("HTTP 404", "HTTP 410")

_tasks: dict[str, asyncio.Task[Any]] = {}


def page_content_id(collection: str, url: str) -> str:
    return generate_id(f"{collection}:web:{url}")


def default_options(**overrides: Any) -> dict[str, Any]:
    options = {
        "max_pages": 50,
        "max_depth": 3,
        "include": [],
        "exclude": [],
        "format": True,
        "formatter_provider": None,
        "formatter_model_id": None,
        "formatter_credential_id": None,
    }
    options.update({k: v for k, v in overrides.items() if v is not None})
    return options


def formatter_model(options: dict[str, Any]) -> Any | None:
    """O modelo do agente formatador: o da fonte, senão `AUX_MODEL_*`, senão o padrão
    do serviço. `format: false` = sem formatador (só a extração)."""
    if not options.get("format", True):
        return None
    from agent_service.models.provider import get_model

    settings = get_settings()
    provider = options.get("formatter_provider") or settings.aux_model_provider or settings.default_model_provider
    model_id = options.get("formatter_model_id") or (settings.aux_model_id if not options.get("formatter_provider") else None)
    return get_model(provider=provider, model_id=model_id, credential_id=options.get("formatter_credential_id"))


class _PreChunked(Reader):
    """Entrega ao Agno trechos já cortados por seção (`chunk_markdown`), em vez de
    deixar um leitor genérico recortar o Markdown por tamanho."""

    def __init__(self, documents: list[Document]) -> None:
        super().__init__(chunk=False)
        self.documents = documents

    def read(self, obj: Any, name: str | None = None, password: str | None = None) -> list[Document]:
        return list(self.documents)

    async def async_read(self, obj: Any, name: str | None = None, password: str | None = None) -> list[Document]:
        return list(self.documents)


async def ingest_page(
    knowledge: Knowledge,
    collection: str,
    *,
    url: str,
    title: str,
    markdown: str,
    metadata: dict[str, Any],
) -> tuple[str, int]:
    """Indexa (ou substitui) o documento de uma página. Devolve (content_id, trechos)."""
    content_id = page_content_id(collection, url)
    chunks = chunk_markdown(markdown)
    documents = [
        Document(
            content=chunk.text,
            name=title,
            meta_data={"chunk": i, "chunk_size": len(chunk.text), **({"section": chunk.section} if chunk.section else {})},
        )
        for i, chunk in enumerate(chunks, 1)
    ]
    await remove_page(knowledge, content_id)
    content = Content(
        id=content_id,
        name=title,
        description=url,
        file_data=FileData(content=markdown.encode("utf-8"), type="text/markdown", filename=f"{title[:80]}.md"),
        metadata=metadata,
        reader=_PreChunked(documents),
    )
    content.content_hash = knowledge._build_content_hash(content)
    await knowledge._aload_content(content, upsert=True, skip_if_exists=False)
    return content_id, len(documents)


async def remove_page(knowledge: Knowledge, content_id: str) -> None:
    try:
        await knowledge.aremove_content_by_id(content_id)
    except Exception:
        logger.debug("Nada a remover para %s", content_id, exc_info=True)


# -- descoberta ----------------------------------------------------------------------


def _is_sitemap_url(url: str) -> bool:
    path = url.lower().split("?", 1)[0]
    return path.endswith((".xml", ".xml.gz")) or "sitemap" in path


async def discover_sitemap(fetcher: web.Fetcher, url: str, scope: web.Scope, limit: int) -> list[str]:
    if _is_sitemap_url(url):
        queue = [url]
    else:
        origin = url.split("/", 3)
        queue = await fetcher.sitemaps_from_robots(url) or [f"{origin[0]}//{origin[2]}/sitemap.xml"]
    pages: list[str] = []
    seen: set[str] = set()
    errors: list[str] = []
    while queue and len(seen) < MAX_SITEMAPS and len(pages) < limit:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        try:
            fetched = await fetcher.get(current, accept=())
        except web.WebFetchError as exc:
            errors.append(f"{current}: {exc}")
            continue
        found, children = web.parse_sitemap(fetched.body)
        queue.extend(children)
        pages.extend(p for p in (web.normalize_url(u) for u in found) if scope.accepts(p))
    if not pages and errors:
        raise web.WebFetchError("sitemap inacessível — " + "; ".join(errors[:3]))
    return web.unique(pages)[:limit]


# -- sincronização -------------------------------------------------------------------


class _Run:
    """O estado de uma sincronização em andamento."""

    def __init__(self, source: dict[str, Any], knowledge: Knowledge, fetcher: web.Fetcher, model: Any | None) -> None:
        self.source = source
        self.options = source["options"] or {}
        self.collection = source["collection"]
        self.knowledge = knowledge
        self.fetcher = fetcher
        self.model = model
        self.previous = {p["url"]: p for p in store.list_pages(source["id"])}
        self.counts = {"discovered": 0, "done": 0, "indexed": 0, "unchanged": 0, "removed": 0, "failed": 0, "skipped": 0,
                       "format_input_tokens": 0, "format_output_tokens": 0, "format_fallbacks": 0}
        self.visited: set[str] = set()
        self.kept: set[str] = set()
        self.responded = 0
        """Páginas que o site de fato entregou nesta rodada."""
        self.sem = asyncio.Semaphore(max(get_settings().web_source_concurrency, 1))

    def progress(self) -> None:
        store.update_source(self.source["id"], progress=dict(self.counts))

    async def process(self, url: str) -> list[str]:
        """Processa uma página; devolve os links dela (para o crawl)."""
        async with self.sem:
            links = await self._process(url)
        self.counts["done"] += 1
        if self.counts["done"] % 5 == 0:
            self.progress()
        return links

    async def _process(self, url: str) -> list[str]:
        sid = self.source["id"]
        now = datetime.now(timezone.utc)
        if not await self.fetcher.allowed(url):
            self.counts["skipped"] += 1
            store.upsert_page(sid, url, status="skipped", error="bloqueada pelo robots.txt", fetched_at=now)
            self.kept.add(url)
            return []
        try:
            fetched = await self.fetcher.get(url)
        except web.WebFetchError as exc:
            message = str(exc)
            previous = self.previous.get(url)
            if message.startswith(GONE) and previous and previous.get("content_id"):
                await remove_page(self.knowledge, previous["content_id"])
                store.delete_page(sid, url)
                self.counts["removed"] += 1
                return []
            self.counts["failed"] += 1
            # Falha passageira não apaga o que já estava indexado.
            store.upsert_page(sid, url, status="failed", error=message[:500], fetched_at=now)
            self.kept.add(url)
            return []

        self.responded += 1
        is_text = fetched.content_type.startswith(web.TEXT_TYPES)
        # Os links relativos da página valem a partir da URL que respondeu (com a barra
        # final, depois dos redirects) — não da forma normalizada usada como chave.
        extracted = (
            web.extract_text_document(fetched.text, url) if is_text else web.extract(fetched.text, fetched.url)
        )
        extracted.url = url
        self.kept.add(url)
        if len(extracted.markdown) < MIN_PAGE_CHARS:
            self.counts["skipped"] += 1
            store.upsert_page(sid, url, status="skipped", error="página sem conteúdo de texto", title=extracted.title, fetched_at=now)
            return extracted.links

        digest = hashlib.sha256(
            f"{extracted.markdown}\x00{bool(self.model)}\x00{extracted.title}".encode("utf-8")
        ).hexdigest()
        previous = self.previous.get(url)
        if previous and previous.get("content_hash") == digest and previous.get("status") == "indexed":
            self.counts["unchanged"] += 1
            store.upsert_page(sid, url, fetched_at=now)
            return extracted.links

        formatted = await format_page(extracted.markdown, title=extracted.title, url=url, model=self.model)
        self.counts["format_input_tokens"] += formatted.input_tokens
        self.counts["format_output_tokens"] += formatted.output_tokens
        if formatted.how == "fallback":
            self.counts["format_fallbacks"] += 1
        try:
            content_id, chunks = await ingest_page(
                self.knowledge,
                self.collection,
                url=url,
                title=extracted.title,
                markdown=formatted.markdown,
                metadata={
                    "source_type": "web",
                    "source_id": sid,
                    "source_url": url,
                    "page_title": extracted.title,
                    "crawled_at": now.isoformat(timespec="seconds"),
                },
            )
        except Exception as exc:
            logger.warning("Falha ao indexar %s", url, exc_info=True)
            self.counts["failed"] += 1
            store.upsert_page(sid, url, status="failed", error=f"indexação: {type(exc).__name__}: {exc}"[:500], fetched_at=now)
            return extracted.links
        self.counts["indexed"] += 1
        store.upsert_page(
            sid, url, status="indexed", content_id=content_id, title=extracted.title, content_hash=digest,
            chunks=chunks, formatted=formatted.how, error=formatted.reason, fetched_at=now,
        )
        return extracted.links

    async def run(self) -> None:
        mode, start = self.source["mode"], web.normalize_url(self.source["url"])
        limit = min(int(self.options.get("max_pages") or 50), get_settings().web_source_max_pages)
        scope = web.Scope(start, include=list(self.options.get("include") or []),
                          exclude=list(self.options.get("exclude") or []), same_path=mode == "crawl")
        if mode == "page":
            batch = [start]
        elif mode == "sitemap":
            batch = await discover_sitemap(self.fetcher, self.source["url"], scope, limit)
        else:
            batch = [start]
        depth = 0
        max_depth = int(self.options.get("max_depth") or 0) if mode == "crawl" else 0
        while batch:
            batch = [u for u in batch if u not in self.visited][: max(limit - len(self.visited), 0)]
            if not batch:
                break
            self.visited.update(batch)
            self.counts["discovered"] = len(self.visited)
            self.progress()
            links_per_page = await asyncio.gather(*(self.process(u) for u in batch))
            if mode != "crawl" or depth >= max_depth:
                break
            depth += 1
            batch = web.unique(link for links in links_per_page for link in links if scope.accepts(link))

        # O que estava indexado e não apareceu desta vez saiu do site (ou do escopo).
        # Se nada desta rodada respondeu (site fora do ar), não dá para concluir isso:
        # apagar tudo por causa de uma queda seria o pior resultado possível.
        # (robots.txt inalcançável também bloqueia tudo: não conta como resposta.)
        if not self.responded:
            return
        for url, page in self.previous.items():
            if url not in self.kept and url not in self.visited:
                if page.get("content_id"):
                    await remove_page(self.knowledge, page["content_id"])
                store.delete_page(self.source["id"], url)
                self.counts["removed"] += 1


async def sync_source(source_id: str, *, fetcher: web.Fetcher | None = None, model: Any = "default") -> dict[str, Any]:
    """Uma sincronização completa. Quem chama já fez `claim_source`."""
    from agent_service.documents.collections import get_collection

    source = store.get_source(source_id)
    if source is None:
        raise store.SourceNotFoundError(source_id)
    own_fetcher = fetcher is None
    fetcher = fetcher or web.Fetcher()
    warning = None
    beat = asyncio.create_task(_heartbeat(source_id), name=f"web-sync-heartbeat-{source_id}")
    try:
        if model == "default":
            try:
                model = formatter_model(source["options"] or {})
            except Exception as exc:
                model = None
                warning = f"Sem formatador ({exc}); páginas indexadas com o texto extraído."
        run = _Run(source, get_collection(source["collection"]), fetcher, model)
        await run.run()
        counts = run.counts
        pages = store.list_pages(source_id)
        indexed = sum(1 for p in pages if p["status"] == "indexed")
        failed = sum(1 for p in pages if p["status"] == "failed")
        if not run.responded:
            # Nada respondeu: o que já estava indexado fica, mas esta rodada falhou.
            first_error = next((p["error"] for p in pages if p["status"] in ("failed", "skipped") and p["error"]), None)
            status = "failed"
            warning = "O site não entregou nenhuma página" + (f" ({first_error})." if first_error else ".")
        elif not indexed:
            status, warning = "failed", warning or "Nenhuma página com conteúdo de texto foi encontrada."
        else:
            status = "partial" if counts["failed"] else "ready"
        now = datetime.now(timezone.utc)
        return store.update_source(
            source_id,
            status=status,
            status_message=warning,
            progress=counts,
            pages_indexed=indexed,
            pages_failed=failed,
            last_synced_at=now,
            next_sync_at=store.next_sync(source["refresh"], now),
        )
    except asyncio.CancelledError:
        store.update_source(source_id, status="failed", status_message="Sincronização cancelada.")
        raise
    except Exception as exc:
        logger.warning("Sincronização da fonte %s falhou", source_id, exc_info=True)
        now = datetime.now(timezone.utc)
        return store.update_source(
            source_id,
            status="failed",
            status_message=f"{type(exc).__name__}: {exc}"[:500],
            last_synced_at=now,
            next_sync_at=store.next_sync(source["refresh"], now),
        )
    finally:
        beat.cancel()
        if own_fetcher:
            await fetcher.aclose()


async def _heartbeat(source_id: str) -> None:
    """Sinal de vida enquanto o sync roda (`sources_store.STALE_RUNNING`): uma página
    lenta no formatador não pode fazer o sync parecer morto para outra réplica."""
    while True:
        await asyncio.sleep(store.HEARTBEAT_SECONDS)
        try:
            store.heartbeat(source_id)
        except Exception:
            logger.debug("Heartbeat da fonte %s falhou", source_id, exc_info=True)


def is_running(source_id: str) -> bool:
    task = _tasks.get(source_id)
    return task is not None and not task.done()


def start_sync(source_id: str) -> bool:
    """Dispara a sincronização em background. `False` se já há uma rodando."""
    loop = asyncio.get_running_loop()  # antes da trava: sem loop, a fonte não pode ficar presa em `running`
    if is_running(source_id) or not store.claim_source(source_id):
        return False
    task = loop.create_task(sync_source(source_id), name=f"web-sync-{source_id}")
    _tasks[source_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(source_id, None))
    return True


async def cancel_sync(source_id: str) -> None:
    task = _tasks.get(source_id)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def remove_source_pages(source: dict[str, Any]) -> int:
    """Tira da collection todas as páginas indexadas por uma fonte."""
    from agent_service.documents.collections import get_collection

    knowledge = get_collection(source["collection"])
    pages = store.list_pages(source["id"])
    for page in pages:
        if page.get("content_id"):
            await remove_page(knowledge, page["content_id"])
    return len(pages)


async def _scheduler() -> None:
    while True:
        await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)
        try:
            store.fail_stale_running()
            for source in store.due_sources():
                start_sync(source["id"])
        except Exception:
            logger.warning("Agendador de fontes falhou nesta rodada", exc_info=True)


@asynccontextmanager
async def scheduler_lifespan(_app: Any = None) -> AsyncIterator[None]:
    """Liga o agendador de ressincronização no startup (e marca como interrompido
    o sync sem heartbeat — o que estava rodando quando o processo anterior caiu)."""
    try:
        if store.fail_stale_running():
            logger.info("Fontes com sincronização interrompida pelo restart marcadas como falha.")
    except Exception:
        logger.warning("Não consegui revisar as fontes no startup", exc_info=True)
    task = asyncio.create_task(_scheduler(), name="web-sources-scheduler") if get_settings().web_source_scheduler else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
        for source_id in list(_tasks):
            await cancel_sync(source_id)
