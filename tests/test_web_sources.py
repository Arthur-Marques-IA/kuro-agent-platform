"""Sites como fonte de uma collection: coleta segura, formatação fiel, sync incremental."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from agent_service.api import sources_routes
from agent_service.api.collections_routes import CollectionIn, create_collection, delete_collection, list_collections
from agent_service.documents import sources_store as store
from agent_service.documents import web, web_sync
from agent_service.documents.web_formatter import Formatted, chunk_markdown, fidelity, segments
from agent_service.jobs import store as jobs
from agent_service.jobs.worker import Worker


def pagina(titulo: str, corpo: str, links: list[str] = ()) -> str:
    anchors = "".join(f'<a href="{href}">{href}</a>' for href in links)
    return (
        f"<html><head><title>{titulo}</title></head><body>"
        f"<nav>{anchors}</nav><div class='cookie-consent'>Aceite os cookies</div>"
        f"<main><h1>{titulo}</h1>{corpo}</main><footer>© Loja</footer></body></html>"
    )


class Site:
    """Um site falso servido pelo MockTransport do httpx."""

    def __init__(self) -> None:
        self.paginas: dict[str, httpx.Response | str] = {
            "/robots.txt": "User-agent: *\nDisallow: /docs/privado\nSitemap: https://site.test/sitemap.xml\n",
            "/docs": pagina("Documentação", "<p>Comece pelo guia de garantia e pelo guia de trocas da loja.</p>",
                            ["/docs/garantia", "/docs/trocas", "/docs/privado", "/blog/novidade",
                             "https://outro.test/x", "/docs/interno", "/docs/manual.pdf"]),
            "/docs/garantia": pagina("Garantia", "<h2>Prazo</h2><p>A garantia dos produtos é de 12 meses a partir da compra.</p>"),
            "/docs/trocas": httpx.Response(301, headers={"Location": "/docs/trocas-v2"}),
            "/docs/trocas-v2": pagina("Trocas", "<p>As trocas podem ser pedidas em até 7 dias corridos.</p>"),
            "/docs/privado": pagina("Privado", "<p>Isto não pode ser coletado pelo robots.</p>"),
            "/docs/interno": httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"}),
            "/sitemap.xml": (
                '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<url><loc>https://site.test/docs/garantia</loc></url>"
                "<url><loc>https://site.test/docs/trocas-v2</loc></url></urlset>"
            ),
        }
        self.pedidos: list[str] = []
        self.fora_do_ar = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.pedidos.append(str(request.url))
        if self.fora_do_ar:
            raise httpx.ConnectError("fora do ar")
        if request.url.host != "site.test":
            return httpx.Response(200, text="não deveria chegar aqui")
        resposta = self.paginas.get(request.url.path)
        if resposta is None:
            return httpx.Response(404, text="não achei")
        if isinstance(resposta, httpx.Response):
            return resposta
        tipo = "application/xml" if request.url.path.endswith(".xml") else (
            "text/plain" if request.url.path.endswith(".txt") else "text/html; charset=utf-8")
        return httpx.Response(200, text=resposta, headers={"content-type": tipo})

    def fetcher(self) -> web.Fetcher:
        return web.Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(self.handler)), delay_ms=0)


class BaseFalsa:
    """O mínimo de `Knowledge` que a ingestão usa: grava o que entraria no pgvector."""

    def __init__(self) -> None:
        self.documentos: dict[str, dict] = {}
        self.cargas = 0

    def _build_content_hash(self, content):
        return f"hash-{content.id}"

    async def _aload_content(self, content, upsert, skip_if_exists):
        self.cargas += 1
        chunks = await content.reader.async_read(None)
        for doc in chunks:
            doc.meta_data.update(content.metadata or {})
        self.documentos[content.id] = {"name": content.name, "chunks": chunks}

    async def aremove_content_by_id(self, content_id):
        return self.documentos.pop(content_id, None) is not None

    async def aget_content(self, limit=None, page=None):
        class Doc:
            def __init__(self, id_):
                self.id = id_

        return [Doc(i) for i in self.documentos], len(self.documentos)


@pytest.fixture
def colecao():
    create_collection(CollectionIn(name="site_docs", label="Docs do site"))
    yield "site_docs"
    if any(c["name"] == "site_docs" for c in list_collections()):
        delete_collection("site_docs")


@pytest.fixture
def base(monkeypatch):
    falsa = BaseFalsa()
    monkeypatch.setattr("agent_service.documents.collections.get_collection", lambda name: falsa)
    return falsa


def sincronizar(source_id: str, site: Site, model=None):
    async def run():
        store.mark_running(source_id)
        fetcher = site.fetcher()
        try:
            return await web_sync.sync_source(source_id, fetcher=fetcher, model=model)
        finally:
            await fetcher.client.aclose()

    return asyncio.run(run())


def nova_fonte(colecao: str, url: str, mode: str = "crawl", **options) -> str:
    return store.create_source(
        collection=colecao, url=url, mode=mode, options=web_sync.default_options(**options), refresh="weekly"
    )["id"]


def paginas_por_url(source_id: str) -> dict[str, dict]:
    return {p["url"]: p for p in store.list_pages(source_id)}


# -- coleta ----------------------------------------------------------------------------


def test_crawl_respeita_escopo_robots_redirect_e_egress(colecao, base):
    site = Site()
    sid = nova_fonte(colecao, "https://site.test/docs", max_depth=2)
    fonte = sincronizar(sid, site)
    paginas = paginas_por_url(sid)

    assert fonte["status"] == "partial"  # a página que redireciona para o metadata da nuvem falhou
    assert paginas["https://site.test/docs/garantia"]["status"] == "indexed"
    assert paginas["https://site.test/docs/trocas"]["status"] == "indexed"  # seguiu o 301
    assert paginas["https://site.test/docs/privado"]["status"] == "skipped"
    assert "robots" in paginas["https://site.test/docs/privado"]["error"]
    interno = paginas["https://site.test/docs/interno"]
    assert interno["status"] == "failed" and "169.254.169.254" in interno["error"]
    # Fora do caminho, de outro host, ou que não é página: nem visitados.
    assert not any("/blog/" in u or "outro.test" in u or u.endswith(".pdf") for u in paginas)
    assert not any("169.254" in p or "outro.test" in p for p in site.pedidos)
    assert fonte["pages_indexed"] == 3 and fonte["pages_failed"] == 1
    assert fonte["next_sync_at"] is not None  # refresh semanal agenda o próximo


def test_links_relativos_valem_a_partir_da_url_que_respondeu(colecao, base):
    """`/guia` redireciona para `/guia/`, e o link `instalacao/` é `/guia/instalacao/` —
    resolvido contra a forma normalizada (sem a barra) ele viraria `/instalacao/`."""
    site = Site()
    site.paginas["/guia"] = httpx.Response(301, headers={"Location": "/guia/"})
    site.paginas["/guia/"] = pagina("Guia", "<p>Veja a instalação do produto passo a passo.</p>", ["instalacao/"])
    site.paginas["/guia/instalacao/"] = pagina("Instalação", "<p>Baixe o instalador e execute como administrador.</p>")
    sid = nova_fonte(colecao, "https://site.test/guia/", max_depth=1)
    sincronizar(sid, site)
    assert paginas_por_url(sid)["https://site.test/guia/instalacao/"]["status"] == "indexed"


def test_trechos_levam_a_origem_e_o_texto_sem_ruido(colecao, base):
    sid = nova_fonte(colecao, "https://site.test/docs/garantia", mode="page")
    sincronizar(sid, Site())
    content_id = web_sync.page_content_id(colecao, "https://site.test/docs/garantia")
    doc = base.documentos[content_id]
    texto = "\n".join(c.content for c in doc["chunks"])

    assert doc["name"] == "Garantia"
    assert "12 meses" in texto
    assert "cookies" not in texto and "© Loja" not in texto and "/blog/" not in texto
    meta = doc["chunks"][0].meta_data
    assert meta["source_url"] == "https://site.test/docs/garantia"
    assert meta["page_title"] == "Garantia" and meta["source_type"] == "web" and meta["source_id"] == sid
    assert meta["chunk"] == 1 and meta["section"].startswith("Garantia")


def test_resync_pula_o_que_nao_mudou_substitui_o_que_mudou_e_remove_o_que_sumiu(colecao, base):
    site = Site()
    sid = nova_fonte(colecao, "https://site.test/docs", max_depth=1)
    sincronizar(sid, site)
    cargas = base.cargas

    fonte = sincronizar(sid, site)
    assert base.cargas == cargas  # nada mudou: nenhuma página reindexada
    assert fonte["progress"]["unchanged"] == 3

    site.paginas["/docs/garantia"] = pagina("Garantia", "<p>A garantia passou a ser de 24 meses.</p>")
    site.paginas.pop("/docs/trocas")  # 404
    fonte = sincronizar(sid, site)
    garantia = web_sync.page_content_id(colecao, "https://site.test/docs/garantia")
    trocas = web_sync.page_content_id(colecao, "https://site.test/docs/trocas")

    assert "24 meses" in base.documentos[garantia]["chunks"][0].content  # mesmo id: substituída, não duplicada
    assert trocas not in base.documentos
    assert "https://site.test/docs/trocas" not in paginas_por_url(sid)
    assert fonte["progress"]["indexed"] == 1 and fonte["progress"]["removed"] == 1


def test_site_fora_do_ar_nao_apaga_o_que_ja_estava_indexado(colecao, base):
    site = Site()
    sid = nova_fonte(colecao, "https://site.test/docs", max_depth=1)
    sincronizar(sid, site)
    antes = set(base.documentos)

    site.fora_do_ar = True
    fonte = sincronizar(sid, site)
    assert fonte["status"] == "failed"
    assert set(base.documentos) == antes


def test_sitemap_vem_do_robots(colecao, base):
    sid = nova_fonte(colecao, "https://site.test/", mode="sitemap")
    fonte = sincronizar(sid, Site())
    assert set(paginas_por_url(sid)) == {"https://site.test/docs/garantia", "https://site.test/docs/trocas-v2"}
    assert fonte["status"] == "ready"


def test_max_pages_limita_a_coleta(colecao, base):
    sid = nova_fonte(colecao, "https://site.test/docs", max_depth=3, max_pages=2)
    sincronizar(sid, Site())
    assert len(store.list_pages(sid)) == 2


def test_formatador_reprovado_cai_no_texto_extraido(colecao, base, monkeypatch):
    async def formatador_que_inventa(markdown, *, title, url, model):
        return Formatted(markdown=markdown, how="fallback", reason="o formatador alterou números (36)")

    monkeypatch.setattr(web_sync, "format_page", formatador_que_inventa)
    sid = nova_fonte(colecao, "https://site.test/docs/garantia", mode="page")
    sincronizar(sid, Site(), model=object())
    pagina_ = paginas_por_url(sid)["https://site.test/docs/garantia"]
    assert pagina_["status"] == "indexed" and pagina_["formatted"] == "fallback"
    assert "36" in pagina_["error"]


def test_sync_concorrente_e_recusado(colecao):
    sid = nova_fonte(colecao, "https://site.test/docs", mode="page")
    assert web_sync.start_sync(sid)
    assert not web_sync.start_sync(sid)
    assert len(jobs.list_jobs(dedupe_key=f"web_source:{sid}")) == 1


# -- formatador e trechos ----------------------------------------------------------------


ORIGINAL = (
    "# Garantia\n\nA garantia é de 12 meses.\n\nMenu Home Sobre\n\n## Trocas\n\nAté 7 dias corridos.\n\n"
    "A troca de produto usado não é aceita."
)


def test_fidelidade_aceita_reorganizar_e_tirar_ruido():
    formatado = (
        "# Garantia\n\nA garantia é de 12 meses.\n\n## Trocas\n\n- Até 7 dias corridos.\n"
        "- A troca de produto usado não é aceita."
    )
    assert fidelity(ORIGINAL, formatado) is None


@pytest.mark.parametrize(
    ("resultado", "motivo"),
    [
        ("# Garantia\n\nA garantia é de 24 meses.\n\n## Trocas\n\nAté 7 dias corridos.", "números"),
        ("# Garantia\n\nO prazo de cobertura dura um ano inteiro.\n\n## Trocas\n\nAté 7 dias corridos.", "acrescentou"),
        ("# Garantia", "cortou"),
        ("", "vazio"),
        # Sem palavra nova nem número trocado, mas o sentido mudou.
        (
            "# Garantia\n\nA garantia é de 12 meses.\n\n## Trocas\n\nAté 7 dias corridos.\n\n"
            "A troca de produto usado é aceita.",
            "redação",
        ),
        (
            "# Garantia\n\nDe 12 meses é a garantia.\n\n## Trocas\n\nAté 7 dias corridos.\n\n"
            "A troca de produto usado não é aceita.",
            "redação",
        ),
    ],
)
def test_fidelidade_reprova_reescrita_numero_trocado_e_resumo(resultado, motivo):
    assert motivo in fidelity(ORIGINAL, resultado)


def test_trechos_respeitam_secoes_e_guardam_o_caminho():
    md = "# Manual\n\nIntro curta.\n\n## Instalação\n\n" + ("Passo detalhado. " * 60) + "\n\n## Uso\n\nComo usar."
    chunks = chunk_markdown(md, max_chars=600)
    assert all(len(c.text) <= 600 for c in chunks)
    assert chunks[0].section == "Manual"
    assert any(c.section == "Manual > Instalação" for c in chunks)
    assert chunks[-1].section == "Manual > Uso" and chunks[-1].text.endswith("Como usar.")
    # O texto dos trechos é o da página, sem nada acrescentado.
    assert "".join(c.text for c in chunks).replace("\n", "").replace(" ", "") == md.replace("\n", "").replace(" ", "")


def test_trechos_nao_partem_bloco_de_codigo():
    md = "# Código\n\n```\nlinha 1\n\nlinha 2\n```\n\nFim."
    assert any("linha 1\n\nlinha 2" in c.text for c in chunk_markdown(md))


def test_paginas_longas_vao_ao_formatador_em_partes():
    md = "\n".join(f"## Seção {i}\n\n" + "texto " * 300 for i in range(20))
    partes = segments(md, limit=4000)
    assert len(partes) > 1 and all(len(p) <= 4000 for p in partes)
    assert all(p.lstrip().startswith("## Seção") for p in partes)


# -- API -------------------------------------------------------------------------------


def _criar(colecao, **body):
    return asyncio.run(sources_routes.create_source(colecao, sources_routes.SourceIn(**body)))


def test_api_cria_dispara_e_recusa_duplicada(colecao, monkeypatch):
    disparos = []
    monkeypatch.setattr(web_sync, "start_sync", lambda sid: disparos.append(sid) or True)
    fonte = _criar(colecao, url="https://site.test/docs", mode="crawl", format=False, max_pages=10, refresh="daily")
    assert disparos == [fonte["id"]]
    assert fonte["options"]["max_pages"] == 10 and fonte["options"]["format"] is False
    with pytest.raises(HTTPException) as exc:
        _criar(colecao, url="https://site.test/docs", mode="crawl", format=False)
    assert exc.value.status_code == 409


def test_api_sync_enfileira_e_recusa_o_segundo(colecao, base, monkeypatch):
    """O 202 sai com a fonte `running`; o segundo pedido, com o primeiro ainda na
    fila, é 409; o worker roda o sync uma vez só."""
    chamadas = []

    async def sync_falso(source_id, **_):
        chamadas.append(source_id)
        return store.update_source(source_id, status="ready")

    monkeypatch.setattr(web_sync, "sync_source", sync_falso)
    sid = nova_fonte(colecao, "https://site.test/docs", mode="page")

    async def run():
        primeira = await sources_routes.sync_source(colecao, sid)
        with pytest.raises(HTTPException) as exc:
            await sources_routes.sync_source(colecao, sid)
        while await Worker().run_once():
            pass
        return primeira, exc.value.status_code

    primeira, segunda = asyncio.run(run())
    assert primeira["status"] == "running" and segunda == 409 and chamadas == [sid]


def test_api_recusa_destino_interno(colecao):
    with pytest.raises(HTTPException) as exc:
        _criar(colecao, url="http://169.254.169.254/latest", format=False)
    assert exc.value.status_code == 422 and "A coleta só alcança" in exc.value.detail


def test_api_recusa_formatador_sem_provedor(colecao):
    with pytest.raises(HTTPException) as exc:
        _criar(colecao, url="https://site.test/docs", formatter_provider="inexistente")
    assert exc.value.status_code == 422 and "format=false" in exc.value.detail


def test_api_remove_fonte_e_paginas(colecao, base):
    sid = nova_fonte(colecao, "https://site.test/docs/garantia", mode="page")
    sincronizar(sid, Site())
    assert base.documentos
    detalhe = sources_routes.get_source(colecao, sid)
    assert detalhe["pages"][0]["chunks"] >= 1
    asyncio.run(sources_routes.delete_source(colecao, sid))
    assert base.documentos == {} and store.get_source(sid) is None


def test_api_atualiza_agendamento(colecao):
    sid = nova_fonte(colecao, "https://site.test/docs", mode="page")
    fonte = sources_routes.update_source(colecao, sid, sources_routes.SourceUpdateIn(refresh="never", max_pages=5))
    assert fonte["refresh"] == "never" and fonte["next_sync_at"] is None and fonte["options"]["max_pages"] == 5


def test_remover_collection_leva_as_fontes(colecao):
    sid = nova_fonte(colecao, "https://site.test/docs", mode="page")
    delete_collection(colecao)
    assert store.get_source(sid) is None


# -- robustez ------------------------------------------------------------------------


def test_sitemap_gzip_le_so_ate_o_teto(monkeypatch):
    """Um .xml.gz hostil não pode ser descomprimido inteiro na memória (bomba de gzip)."""
    import gzip

    monkeypatch.setattr(web, "MAX_PAGE_BYTES", 1024)
    sitemap = (
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://a.test/x</loc></url></urlset>'
    )
    assert web.parse_sitemap(gzip.compress(sitemap)) == (["https://a.test/x"], [])
    bomba = gzip.compress(b" " * (50 * 1024 * 1024))
    lidos = []
    original = gzip.GzipFile.read

    def read(self, size=-1):
        lidos.append(size)
        return original(self, size)

    monkeypatch.setattr(gzip.GzipFile, "read", read)
    web.parse_sitemap(bomba)
    assert lidos and all(0 < size <= 4 * 1024 for size in lidos)


def test_xhtml_com_declaracao_de_encoding():
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>Manual</title></head><body><main><h1>Manual</h1>"
        "<p>O prazo de entrega é de cinco dias úteis.</p></main></body></html>"
    )
    extraido = web.extract(body, "https://site.test/manual")
    assert extraido.title == "Manual" and "cinco dias úteis" in extraido.markdown


def test_apagar_documento_de_site_a_mao_faz_o_proximo_sync_trazer_de_volta(colecao, base):
    from agent_service.api.collections_routes import delete_collection_document

    site = Site()
    sid = nova_fonte(colecao, "https://site.test/docs/garantia", mode="page")
    sincronizar(sid, site)
    content_id = web_sync.page_content_id(colecao, "https://site.test/docs/garantia")

    asyncio.run(delete_collection_document(colecao, content_id))
    assert content_id not in base.documentos
    assert store.list_pages(sid) == []  # sem isto o próximo sync a acharia "igual" e não reindexaria

    sincronizar(sid, site)
    assert content_id in base.documentos


@pytest.fixture(autouse=True)
def fila_limpa():
    """Os testes dividem o banco: job deixado por um não pode ser pego pelo worker de outro."""
    with jobs._engine().begin() as conn:
        conn.execute(jobs.update(jobs.jobs).where(jobs.jobs.c.status.in_(jobs.ACTIVE)).values(status="cancelled"))


def test_sync_manual_puxa_o_agendado_para_agora(colecao):
    sid = nova_fonte(colecao, "https://site.test/a", mode="page")
    futuro = datetime.now(timezone.utc) + timedelta(days=1)
    web_sync.schedule(store.update_source(sid, next_sync_at=futuro))
    assert not web_sync.is_running(sid)
    assert web_sync.start_sync(sid)  # 202, não 409: o agendado vira o de agora
    ativos = [j for j in jobs.list_jobs(dedupe_key=f"web_source:{sid}") if j["status"] == "queued"]
    assert len(ativos) == 1 and web_sync.is_running(sid)


def test_refresh_never_tira_da_fila_e_reconcile_agenda(colecao):
    sid = nova_fonte(colecao, "https://site.test/a", mode="page")
    chave = f"web_source:{sid}"
    store.update_source(sid, next_sync_at=datetime.now(timezone.utc) + timedelta(days=3))
    web_sync.reconcile()  # fonte semanal sem job (cadastrada antes da fila): ganha um
    assert jobs.active_job(chave) is not None
    sources_routes.update_source(colecao, sid, sources_routes.SourceUpdateIn(refresh="never"))
    assert jobs.active_job(chave) is None
    sources_routes.update_source(colecao, sid, sources_routes.SourceUpdateIn(refresh="daily"))
    assert jobs.active_job(chave)["run_after"] is not None


def test_reconcile_solta_fonte_presa_em_running(colecao):
    sid = nova_fonte(colecao, "https://site.test/a", mode="page")
    store.update_source(sid, status="running", refresh="never", next_sync_at=None)
    web_sync.reconcile()
    assert store.get_source(sid)["status"] == "failed"


def test_job_de_sync_agenda_o_proximo_pelo_refresh(colecao, base, monkeypatch):
    sid = nova_fonte(colecao, "https://site.test/docs/garantia", mode="page", format=False)
    site, real = Site(), web.Fetcher
    monkeypatch.setattr(web, "Fetcher", lambda: real(httpx.AsyncClient(transport=httpx.MockTransport(site.handler)), delay_ms=0))
    assert web_sync.start_sync(sid)
    assert asyncio.run(Worker().run_once()) == "succeeded"
    assert store.get_source(sid)["status"] == "ready"
    proximo = jobs.active_job(f"web_source:{sid}")
    assert proximo["status"] == "queued"
    assert web_sync._aware(proximo["run_after"]) > datetime.now(timezone.utc) + timedelta(days=6)


def test_retriever_tambem_funciona_num_run_sincrono():
    """O Agno chama o mesmo retriever num `agent.run` síncrono e não aguarda o retorno."""
    from agno.knowledge.document import Document

    from agent_service.documents import retrieval

    class Base:
        vector_db = None

        async def aretrieve(self, **kwargs):
            return [Document(id="c1", name="Doc", content="trecho", meta_data={"similarity_score": 0.8})]

    retrieval.start_run()
    resultado = retrieval.make_retriever("manuais", Base())(query="x")
    assert resultado == [{"name": "Doc", "meta_data": {"similarity_score": 0.8}, "content": "trecho"}]
    assert retrieval.take_search("x")["results"][0]["score"] == 0.8


# -- custo do formatador -------------------------------------------------------------


class _Resposta:
    def __init__(self, content, cost=None):
        self.content = content
        self.status = None
        self.metrics = type("M", (), {"input_tokens": 1000, "output_tokens": 500, "cost": cost})()


def _agente_formatador(monkeypatch, cost=None):
    class Agente:
        def __init__(self, **kwargs):
            pass

        async def arun(self, prompt):
            texto = prompt.split("<pagina>\n", 1)[1].rsplit("\n</pagina>", 1)[0]
            return _Resposta(texto, cost)

    monkeypatch.setattr("agno.agent.Agent", Agente)


def test_formatador_estima_o_custo_pela_tabela_de_precos(monkeypatch):
    from agent_service.documents.web_formatter import format_page

    _agente_formatador(monkeypatch)
    modelo = type("Modelo", (), {"provider": "Google", "id": "gemini-2.5-flash"})()
    formatado = asyncio.run(format_page("A garantia é de 12 meses.", title="G", url="https://x.test", model=modelo))
    assert (formatado.how, formatado.input_tokens, formatado.output_tokens) == ("agent", 1000, 500)
    assert formatado.cost_usd == pytest.approx((1000 * 0.30 + 500 * 2.50) / 1_000_000)


def test_formatador_prefere_o_custo_que_o_provedor_informou(monkeypatch):
    from agent_service.documents.web_formatter import format_page

    _agente_formatador(monkeypatch, cost=0.5)
    modelo = type("Modelo", (), {"provider": "Google", "id": "gemini-2.5-flash"})()
    formatado = asyncio.run(format_page("A garantia é de 12 meses.", title="G", url="https://x.test", model=modelo))
    assert formatado.cost_usd == 0.5


def _custos(source_id: str) -> list[dict]:
    agora = datetime.now(timezone.utc)
    return [c for c in store.list_sync_costs(agora - timedelta(hours=1), agora + timedelta(hours=1))
            if c["source_id"] == source_id]


def test_sync_grava_o_custo_do_formatador(colecao, base, monkeypatch):
    async def formatador(markdown, *, title, url, model):
        return Formatted(markdown=markdown, how="agent", input_tokens=100, output_tokens=50, cost_usd=0.001)

    monkeypatch.setattr(web_sync, "format_page", formatador)
    sid = nova_fonte(colecao, "https://site.test/docs/garantia", mode="page")
    fonte = sincronizar(sid, Site(), model=type("Modelo", (), {"id": "gemini-2.5-flash"})())

    assert fonte["progress"]["format_cost_usd"] == 0.001
    (custo,) = _custos(sid)
    assert (custo["collection"], custo["model"], custo["input_tokens"], custo["output_tokens"], custo["cost_usd"]) == (
        colecao, "gemini-2.5-flash", 100, 50, 0.001)


def test_sync_sem_formatador_nao_grava_custo(colecao, base):
    sid = nova_fonte(colecao, "https://site.test/docs/garantia", mode="page")
    sincronizar(sid, Site(), model=None)
    assert _custos(sid) == []


def test_sync_cancelado_ainda_grava_o_que_o_formatador_gastou(colecao, base, monkeypatch):
    chamadas = 0

    async def formatador(markdown, *, title, url, model):
        nonlocal chamadas
        chamadas += 1
        if chamadas > 1:
            raise asyncio.CancelledError()
        return Formatted(markdown=markdown, how="agent", input_tokens=100, output_tokens=50, cost_usd=0.001)

    monkeypatch.setattr(web_sync, "format_page", formatador)
    monkeypatch.setattr(web_sync.get_settings(), "web_source_concurrency", 1)
    sid = nova_fonte(colecao, "https://site.test/docs")
    with pytest.raises(asyncio.CancelledError):
        sincronizar(sid, Site(), model=object())
    (custo,) = _custos(sid)
    assert (custo["input_tokens"], custo["cost_usd"]) == (100, 0.001)
