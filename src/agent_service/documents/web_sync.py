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

Arquivos linkados nas páginas (PDF, DOCX, PPTX, XLSX — `include_files`) entram
também, do mesmo escopo e dentro de `max_pages`, depois das páginas: o mesmo
download seguro, com teto próprio (`WEB_SOURCE_MAX_FILE_MB`), e a leitura pelos
leitores do Agno, **sem** o formatador — o arquivo já vem estruturado e reescrevê-lo
só arriscaria a fidelidade. Os trechos de um PDF levam a página (`page`).

Roda em background, como job `web_source.sync` da fila (`agent_service/jobs/`): uma
sincronização de centenas de páginas, cada uma com uma chamada ao modelo, não cabe
numa requisição HTTP. O progresso fica na própria fonte; a trava contra dois syncs
ao mesmo tempo é a `dedupe_key` do job (`web_source:<id>`), e o `refresh` é o
próximo job enfileirado com `run_after` no futuro.
"""

import asyncio
import hashlib
import io
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from urllib.parse import unquote, urlsplit

from agno.knowledge.content import Content, FileData
from agno.knowledge.document import Document
from agno.knowledge.knowledge import Knowledge
from agno.knowledge.reader.base import Reader
from agno.utils.string import generate_id

from agent_service.config import get_settings
from agent_service.documents import revisions
from agent_service.documents import sources_store as store
from agent_service.documents import web
from agent_service.documents.web_formatter import chunk_markdown, format_page
from agent_service.jobs import store as jobs
from agent_service.jobs import worker as jobs_worker

logger = logging.getLogger(__name__)

MIN_PAGE_CHARS = 40
MAX_SITEMAPS = 50
GONE = ("HTTP 404", "HTTP 410")
JOB_KIND = "web_source.sync"


def page_content_id(collection: str, url: str) -> str:
    return generate_id(f"{collection}:web:{url}")


def default_options(**overrides: Any) -> dict[str, Any]:
    options = {
        "max_pages": 50,
        "max_depth": 3,
        "include": [],
        "exclude": [],
        "format": True,
        # Novas fontes indexam os arquivos linkados: não passam pelo formatador (nem
        # gastam modelo) e contam no `max_pages`. Fonte antiga sem a chave fica como
        # estava (`_Run`), para o próximo sync agendado não começar a baixar arquivos.
        "include_files": True,
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


class PreChunked(Reader):
    """Entrega ao Agno trechos já cortados por seção (`chunk_markdown`), em vez de
    deixar um leitor genérico recortar o texto por tamanho. Vale para toda ingestão,
    não só a de sites (`documents/collections.py`)."""

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
    pages: list[tuple[int | None, str]] | None = None,
    file_type: str = "text/markdown",
    filename: str | None = None,
) -> tuple[str, int]:
    """Indexa (ou substitui) o documento de uma página — ou de um arquivo, cujas
    `pages` (número, texto) são cortadas uma a uma, com o número em `meta_data.page`.
    Devolve (content_id, trechos)."""
    content_id = page_content_id(collection, url)
    documents = prechunk(pages if pages is not None else [(None, markdown)], name=title)
    # Só chega aqui o que mudou desde o último sync: cada vez é uma revisão nova.
    revision = 1 if revisions.latest(content_id) is None else revisions.next_number(content_id)
    for document in documents:
        document.meta_data[revisions.FIELD] = revision
    await remove_page(knowledge, content_id)
    content = Content(
        id=content_id,
        name=title,
        description=url,
        file_data=FileData(content=markdown.encode("utf-8"), type=file_type, filename=filename or f"{title[:80]}.md"),
        metadata={**metadata, revisions.FIELD: revision},
        reader=PreChunked(documents),
    )
    content.content_hash = knowledge._build_content_hash(content)
    await knowledge._aload_content(content, upsert=True, skip_if_exists=False)
    revisions.record(
        collection,
        content_id,
        revision=revision,
        content_hash=content.content_hash,
        text=markdown,
        filename=filename,
        note=None if revision == 1 else "a página mudou na fonte",
    )
    return content_id, len(documents)


async def remove_page(knowledge: Knowledge, content_id: str) -> None:
    try:
        await knowledge.aremove_content_by_id(content_id)
    except Exception:
        logger.debug("Nada a remover para %s", content_id, exc_info=True)


def prechunk(pages: list[tuple[int | None, str]], *, name: str) -> list[Document]:
    """Os trechos de um documento, página por página, pela regra de `chunk_markdown`:
    cada um com o número (`chunk`), o tamanho, a página quando há e a seção."""
    documents: list[Document] = []
    for page, text in pages:
        for chunk in chunk_markdown(text):
            meta: dict[str, Any] = {"chunk": len(documents) + 1, "chunk_size": len(chunk.text)}
            if page is not None:
                meta["page"] = page
            if chunk.section:
                meta["section"] = chunk.section
            documents.append(Document(content=chunk.text, name=name, meta_data=meta))
    return documents


READABLE_FILES = (".pdf", ".docx", ".pptx", ".xlsx")
"""Arquivos que `read_file` sabe extrair; o resto que não é tabela é lido como texto."""


def extract_pages(body: bytes, filename: str) -> list[tuple[int | None, str]] | None:
    """O texto de um arquivo enviado, por página, para `prechunk`. `None` para CSV,
    que o leitor do Agno corta por linha — uma linha por trecho já é o certo numa tabela."""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext == ".csv":
        return None
    if ext in READABLE_FILES:
        return read_file(body, ext, filename)
    return [(None, body.decode("utf-8", errors="replace"))]


def read_file(body: bytes, ext: str, filename: str) -> list[tuple[int | None, str]]:
    """O texto de um arquivo, pelos leitores do Agno: (página, texto) — a página é a
    física do PDF (1, 2...) ou o slide do PPTX; DOCX e XLSX não têm (`None`).

    No PDF, o número vem da ordem e não do `meta_data["page"]` do Agno, que desloca
    a contagem quando a capa não é numerada; e sem os marcadores `<start page N>`,
    que sujariam o texto exato que o trace mostra."""
    from agno.knowledge.reader.docx_reader import DocxReader
    from agno.knowledge.reader.excel_reader import ExcelReader
    from agno.knowledge.reader.pdf_reader import PDFReader
    from agno.knowledge.reader.pptx_reader import PPTXReader

    stream = io.BytesIO(body)
    stream.name = filename
    if ext == ".pdf":
        reader = PDFReader(chunk=False, split_on_pages=True, page_start_numbering_format="", page_end_numbering_format="")
        return [(n, doc.content) for n, doc in enumerate(reader.read(stream, name=filename), 1)]
    if ext == ".pptx":
        text = "\n\n".join(doc.content for doc in PPTXReader(chunk=False).read(stream, name=filename))
        parts = re.split(r"(?m)^Slide (\d+):\n", text)
        return [(int(n), part.replace("(No text content)", "")) for n, part in zip(parts[1::2], parts[2::2])]
    reader = ExcelReader(chunk=False) if ext == ".xlsx" else DocxReader(chunk=False)
    return [(None, doc.content) for doc in reader.read(stream, name=filename)]


def file_name(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or url


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
        self.include_files = bool(self.options.get("include_files", False))
        self.previous = {p["url"]: p for p in store.list_pages(source["id"])}
        self.counts = {"discovered": 0, "done": 0, "indexed": 0, "unchanged": 0, "removed": 0, "failed": 0, "skipped": 0,
                       "format_input_tokens": 0, "format_output_tokens": 0, "format_fallbacks": 0,
                       "format_cost_usd": None}
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

    async def _fetch(self, url: str, now: datetime, ext: str | None = None) -> web.Fetched | None:
        """Baixa a página (ou o arquivo `ext`); `None` se não deu — já registrado."""
        sid = self.source["id"]
        if not await self.fetcher.allowed(url):
            self.counts["skipped"] += 1
            store.upsert_page(sid, url, status="skipped", error="bloqueada pelo robots.txt", fetched_at=now)
            self.kept.add(url)
            return None
        try:
            if ext is None:
                return await self.fetcher.get(url)
            fetched = await self.fetcher.get(
                url, accept=web.FILE_TYPES[ext][0] + web.GENERIC_FILE_TYPES, what="arquivo",
                max_bytes=get_settings().web_source_max_file_mb * 1024 * 1024,
            )
            web.check_file(fetched, ext)
            return fetched
        except web.WebFetchError as exc:
            message = str(exc)
            previous = self.previous.get(url)
            if message.startswith(GONE) and previous and previous.get("content_id"):
                await remove_page(self.knowledge, previous["content_id"])
                store.delete_page(sid, url)
                self.counts["removed"] += 1
                return None
            self.counts["failed"] += 1
            # Falha passageira não apaga o que já estava indexado.
            store.upsert_page(sid, url, status="failed", error=message[:500], fetched_at=now)
            self.kept.add(url)
            return None

    async def _process(self, url: str) -> list[str]:
        sid = self.source["id"]
        now = datetime.now(timezone.utc)
        ext = web.file_extension(url)
        if ext is not None:
            await self._process_file(url, ext, now)
            return []
        fetched = await self._fetch(url, now)
        if fetched is None:
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
        if formatted.cost_usd is not None:
            self.counts["format_cost_usd"] = round((self.counts["format_cost_usd"] or 0) + formatted.cost_usd, 8)
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

    async def _process_file(self, url: str, ext: str, now: datetime) -> None:
        """Um arquivo linkado: lido pelo leitor do Agno, sem formatador."""
        sid = self.source["id"]
        fetched = await self._fetch(url, now, ext)
        if fetched is None:
            return
        self.responded += 1
        self.kept.add(url)
        title = file_name(url)
        digest = hashlib.sha256(b"file\x00" + fetched.body).hexdigest()
        previous = self.previous.get(url)
        if previous and previous.get("content_hash") == digest and previous.get("status") == "indexed":
            self.counts["unchanged"] += 1
            store.upsert_page(sid, url, fetched_at=now)
            return
        try:
            # pypdf é CPU puro: fora do event loop, para não travar o heartbeat e a API.
            pages = await asyncio.to_thread(read_file, fetched.body, ext, title)
        except Exception as exc:
            self.counts["failed"] += 1
            store.upsert_page(sid, url, status="failed", error=f"leitura: {type(exc).__name__}: {exc}"[:500],
                              title=title, fetched_at=now)
            return
        pages = [(n, text.strip()) for n, text in pages if text.strip()]
        if sum(len(text) for _, text in pages) < MIN_PAGE_CHARS:
            # PDF escaneado (só imagem) ou protegido por senha.
            self.counts["skipped"] += 1
            store.upsert_page(sid, url, status="skipped", error="arquivo sem texto extraível (escaneado ou protegido?)",
                              title=title, fetched_at=now)
            return
        try:
            content_id, chunks = await ingest_page(
                self.knowledge,
                self.collection,
                url=url,
                title=title,
                markdown="\n\n".join(text for _, text in pages),
                pages=pages,
                file_type=web.FILE_TYPES[ext][0][0],
                filename=title,
                metadata={
                    "source_type": "web",
                    "source_id": sid,
                    "source_url": url,
                    "page_title": title,
                    "file_type": ext[1:],
                    "crawled_at": now.isoformat(timespec="seconds"),
                },
            )
        except Exception as exc:
            logger.warning("Falha ao indexar %s", url, exc_info=True)
            self.counts["failed"] += 1
            store.upsert_page(sid, url, status="failed", error=f"indexação: {type(exc).__name__}: {exc}"[:500], fetched_at=now)
            return
        self.counts["indexed"] += 1
        store.upsert_page(
            sid, url, status="indexed", content_id=content_id, title=title, content_hash=digest,
            chunks=chunks, formatted="none", error=None, fetched_at=now,
        )

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
        files: list[str] = []
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
            if self.include_files:
                files += [link for links in links_per_page for link in links if scope.accepts_file(link)]
            if mode != "crawl" or depth >= max_depth:
                break
            depth += 1
            batch = web.unique(link for links in links_per_page for link in links if scope.accepts(link))
        # Os arquivos linkados vão depois das páginas, no que sobrar de `max_pages`.
        files = [u for u in web.unique(files) if u not in self.visited][: max(limit - len(self.visited), 0)]
        if files:
            self.visited.update(files)
            self.counts["discovered"] = len(self.visited)
            self.progress()
            await asyncio.gather(*(self.process(u) for u in files))

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
    """Uma sincronização completa. Quem chama já fez `mark_running`."""
    from agent_service.documents.collections import get_collection

    source = store.get_source(source_id)
    if source is None:
        raise store.SourceNotFoundError(source_id)
    own_fetcher = fetcher is None
    fetcher = fetcher or web.Fetcher()
    warning = None
    run: _Run | None = None
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
        if jobs_worker.is_stopping():
            # O job volta para a fila e recomeça quando o serviço subir.
            store.update_source(source_id, status_message="Interrompida pelo restart; retoma sozinha.")
        else:
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
        if run is not None:
            # Também numa sincronização que falhou ou foi cancelada: o que o
            # formatador já gastou não volta.
            _record_cost(run)
        if own_fetcher:
            await fetcher.aclose()


def _record_cost(run: _Run) -> None:
    counts = run.counts
    if not (counts["format_input_tokens"] or counts["format_output_tokens"]):
        return
    try:
        store.record_sync_cost(
            source_id=run.source["id"],
            collection=run.collection,
            model=getattr(run.model, "id", None),
            input_tokens=counts["format_input_tokens"],
            output_tokens=counts["format_output_tokens"],
            cost_usd=counts["format_cost_usd"],
        )
    except Exception:
        logger.warning("Não gravei o custo da sincronização da fonte %s", run.source["id"], exc_info=True)


def _dedupe(source_id: str) -> str:
    return f"web_source:{source_id}"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _due(job: dict[str, Any]) -> bool:
    """Rodando, ou na fila para já — o que conta como "já está sincronizando"."""
    return job["status"] == "running" or _aware(job["run_after"]) <= datetime.now(timezone.utc)


def is_running(source_id: str) -> bool:
    job = jobs.active_job(_dedupe(source_id))
    return job is not None and _due(job)


def start_sync(source_id: str) -> bool:
    """Põe a sincronização na fila para agora. `False` se já há uma na fila ou rodando
    (o 409 da API); um sync só agendado para depois é puxado para agora."""
    key = _dedupe(source_id)
    try:
        jobs.enqueue(JOB_KIND, {"source_id": source_id}, dedupe_key=key)
    except jobs.JobConflictError:
        active = jobs.active_job(key)
        if active is None or _due(active) or not jobs.advance(key):
            return False
    store.mark_running(source_id)
    jobs_worker.wake()
    return True


def schedule(source: dict[str, Any]) -> None:
    """Deixa a fila coerente com o `refresh` da fonte: troca o próximo sync agendado
    pelo de `next_sync_at` (ou nenhum, com `never`). Um sync rodando agenda o seguinte
    sozinho ao terminar, lendo o `refresh` de então."""
    key = _dedupe(source["id"])
    active = jobs.active_job(key)
    if active is not None and _due(active):
        return
    jobs.cancel_dedupe(key, only_queued=True)
    if source.get("next_sync_at") and get_settings().web_source_scheduler:
        try:
            jobs.enqueue(JOB_KIND, {"source_id": source["id"]}, dedupe_key=key, run_after=_aware(source["next_sync_at"]))
        except jobs.JobConflictError:
            pass


async def cancel_sync(source_id: str) -> None:
    """Cancela o sync da fonte: na fila, sai; rodando aqui, para já; numa outra
    réplica, para no próximo `renew` do lease dela."""
    worker = jobs_worker.current()
    for job_id in jobs.cancel_dedupe(_dedupe(source_id)):
        if worker is not None:
            await worker.cancel_local(job_id)


def _gave_up(job: dict[str, Any]) -> None:
    """As tentativas acabaram (o processo caiu no meio, de novo e de novo): a fonte
    sai de `running`, e o próximo sync agendado continua valendo."""
    source = store.get_source(job["payload"].get("source_id") or "")
    if source is None:
        return
    now = datetime.now(timezone.utc)
    source = store.update_source(
        source["id"], status="failed",
        status_message=f"Sincronização interrompida: {job.get('last_error') or 'falhou'}"[:500],
        last_synced_at=now, next_sync_at=store.next_sync(source["refresh"], now),
    )
    schedule(source)


@jobs_worker.register(JOB_KIND, on_give_up=_gave_up)
async def run_sync_job(job: dict[str, Any]) -> jobs_worker.Reschedule | None:
    """O handler do job: sincroniza e agenda o próximo pelo `refresh`. Falha do site
    (fora do ar, nada indexável) é um job que terminou — a fonte fica `failed` e o
    próximo sync continua agendado; retry com backoff é para o processo que caiu."""
    source_id = job["payload"]["source_id"]
    if store.get_source(source_id) is None:
        return None  # a fonte foi removida enquanto o job esperava
    store.mark_running(source_id)
    await sync_source(source_id)
    source = store.get_source(source_id)
    if source is None or not source.get("next_sync_at") or not get_settings().web_source_scheduler:
        return None
    return jobs_worker.Reschedule(run_after=_aware(source["next_sync_at"]))


async def remove_source_pages(source: dict[str, Any]) -> int:
    """Tira da collection todas as páginas indexadas por uma fonte."""
    from agent_service.documents.collections import get_collection

    knowledge = get_collection(source["collection"])
    pages = store.list_pages(source["id"])
    for page in pages:
        if page.get("content_id"):
            await remove_page(knowledge, page["content_id"])
    return len(pages)


def reconcile() -> None:
    """No startup: fonte presa em `running` sem job ativo (o processo caiu antes de a
    fila existir) vira falha, e fonte agendada sem job na fila ganha o seu — é o que
    leva para a fila as fontes cadastradas antes dela. Idempotente."""
    for source in store.list_sources():
        if jobs.active_job(_dedupe(source["id"])) is not None:
            continue
        if source["status"] == "running":
            source = store.update_source(
                source["id"], status="failed", status_message="Sincronização interrompida (o serviço reiniciou)."
            )
        if source["refresh"] != "never" and not source.get("next_sync_at"):
            source = store.update_source(source["id"], next_sync_at=datetime.now(timezone.utc))
        schedule(source)


@asynccontextmanager
async def scheduler_lifespan(_app: Any = None) -> AsyncIterator[None]:
    """Acerta a fila com as fontes no startup (`reconcile`). Quem roda os syncs é o
    worker de jobs (`jobs.worker.lifespan`)."""
    try:
        reconcile()
    except Exception:
        logger.warning("Não consegui revisar as fontes no startup", exc_info=True)
    yield
