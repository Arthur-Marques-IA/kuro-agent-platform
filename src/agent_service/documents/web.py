"""Coleta de sites: baixar páginas com segurança e tirar delas o conteúdo principal.

Quem pede a coleta escolhe a URL, e o download sai de dentro do container — o
mesmo risco de uma tool (`tools/egress.py`): sem trava, uma "fonte" apontando para
`http://postgres:5432` ou para o metadata da nuvem viraria uma forma de ler a rede
interna e guardar o resultado numa base consultável. Por isso nada aqui usa os
leitores de site do Agno, que baixam sem essa checagem:

- cada salto é conferido (`acheck_url`), inclusive os de redirect, que são
  seguidos à mão (`follow_redirects=False`);
- o corpo tem teto de tamanho e só HTML/texto/XML é aceito — ou, para os arquivos
  linkados (PDF, DOCX, PPTX, XLSX), o tipo do arquivo, com teto próprio
  (`WEB_SOURCE_MAX_FILE_MB`) e conferido pelos primeiros bytes;
- o `robots.txt` do site é respeitado, com o `User-Agent` de `WEB_SOURCE_USER_AGENT`;
- requisições ao mesmo host respeitam um intervalo mínimo.

A extração (`extract`) é determinística: tira script, estilo, menus, rodapés,
banners de cookie e afins, e converte o que sobra em Markdown. É isso que o
formatador (`web_formatter.py`) recebe — menos tokens e menos texto de terceiros
tentando instruir o modelo.
"""

import asyncio
import fnmatch
import gzip
import io
import re
import time
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from lxml import etree
from lxml import html as lxml_html

from agent_service.config import get_settings
from agent_service.tools.egress import EgressBlockedError, acheck_url

MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 20.0
HTML_TYPES = ("text/html", "application/xhtml+xml")
TEXT_TYPES = ("text/plain", "text/markdown")
XML_TYPES = ("application/xml", "text/xml", "application/rss+xml", "application/atom+xml")
SKIP_EXTENSIONS = frozenset(
    ".pdf .png .jpg .jpeg .gif .webp .svg .ico .css .js .json .zip .gz .tar .rar .7z .mp3 .mp4 .avi .mov "
    ".webm .woff .woff2 .ttf .eot .exe .dmg .apk .doc .docx .xls .xlsx .ppt .pptx .csv".split()
)
FILE_TYPES: dict[str, tuple[tuple[str, ...], bytes]] = {
    ".pdf": (("application/pdf", "application/x-pdf"), b"%PDF-"),
    ".docx": (("application/vnd.openxmlformats-officedocument.wordprocessingml.document",), b"PK"),
    ".pptx": (("application/vnd.openxmlformats-officedocument.presentationml.presentation",), b"PK"),
    ".xlsx": (("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",), b"PK"),
}
"""Arquivos linkados que a coleta indexa (`include_files`): extensão → (tipos aceitos,
assinatura dos primeiros bytes). `.doc`, `.xls` e `.ppt` ficam de fora: o formato
binário antigo não abre nos leitores do Agno (`python-docx` só lê o Open XML)."""
GENERIC_FILE_TYPES = ("application/octet-stream", "binary/octet-stream", "application/download")
"""Muito servidor entrega PDF assim; a assinatura dos bytes decide."""
XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>", re.I)
TRACKING_PARAMS = re.compile(r"^(utm_[a-z]+|gclid|fbclid|mc_cid|mc_eid|ref|_ga)$", re.I)


class WebFetchError(RuntimeError):
    """A página não pôde ser baixada — o motivo vai para a página no status do sync."""


@dataclass
class Fetched:
    url: str
    status: int
    content_type: str
    body: bytes

    @property
    def text(self) -> str:
        charset = "utf-8"
        match = re.search(r"charset=([\w-]+)", self.content_type, re.I)
        if match:
            charset = match.group(1)
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


def normalize_url(url: str) -> str:
    """A forma canônica de uma URL para deduplicar: sem fragmento, sem parâmetros
    de rastreio e com o host em minúsculas. A barra final fica: `/guia` e `/guia/`
    são URLs diferentes, e nem todo servidor redireciona de uma para a outra."""
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)])
    return urlunsplit((parts.scheme.lower(), (parts.netloc or "").lower(), parts.path or "/", query, ""))


def looks_like_page(url: str) -> bool:
    path = urlsplit(url).path.lower()
    dot = path.rfind(".")
    return dot == -1 or path[dot:] not in SKIP_EXTENSIONS


def file_extension(url: str) -> str | None:
    """A extensão de um arquivo que a coleta sabe ler (`.pdf`...), ou `None`."""
    path = urlsplit(url).path.lower()
    dot = path.rfind(".")
    ext = path[dot:] if dot != -1 and "/" not in path[dot:] else ""
    return ext if ext in FILE_TYPES else None


def check_file(fetched: "Fetched", ext: str) -> None:
    """O corpo é mesmo o arquivo? Um `.pdf` que responde HTML costuma ser página de
    login ou um "não encontrado" com status 200 — e quebraria dentro do leitor."""
    if not fetched.body.startswith(FILE_TYPES[ext][1]):
        raise WebFetchError(f"o conteúdo não é um {ext[1:].upper()} ({fetched.content_type or 'sem tipo'})")


class Fetcher:
    """Downloads de uma sincronização: trava de egress, robots, teto e ritmo."""

    def __init__(self, client: httpx.AsyncClient | None = None, *, delay_ms: int | None = None) -> None:
        settings = get_settings()
        self.user_agent = settings.web_source_user_agent
        self.delay = (settings.web_source_request_delay_ms if delay_ms is None else delay_ms) / 1000
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, follow_redirects=False, headers={"User-Agent": self.user_agent}
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_lock = asyncio.Lock()
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}

    async def aclose(self) -> None:
        if self._own_client:
            await self.client.aclose()

    async def _pace(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._last_request.get(host, 0.0) + self.delay - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request[host] = time.monotonic()

    async def get(
        self, url: str, *, accept: tuple[str, ...] = HTML_TYPES + TEXT_TYPES, max_bytes: int = MAX_PAGE_BYTES,
        what: str = "página",
    ) -> Fetched:
        current = url
        too_big = f"{what} maior que {max_bytes // (1024 * 1024)} MB"
        for _ in range(MAX_REDIRECTS + 1):
            try:
                await acheck_url(current)
            except EgressBlockedError as exc:
                raise WebFetchError(str(exc).replace("Uma tool só alcança", "A coleta só alcança")) from exc
            await self._pace(urlsplit(current).hostname or "")
            try:
                async with self.client.stream("GET", current, headers={"User-Agent": self.user_agent}) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise WebFetchError(f"HTTP {resp.status_code} sem Location")
                        current = urljoin(current, location)
                        continue
                    if resp.status_code >= 400:
                        raise WebFetchError(f"HTTP {resp.status_code}")
                    content_type = resp.headers.get("content-type", "").lower()
                    if accept and not any(content_type.startswith(t) for t in accept):
                        raise WebFetchError(f"tipo de conteúdo não suportado: {content_type or '(vazio)'}")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise WebFetchError(too_big)
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise WebFetchError(too_big)
                    return Fetched(url=current, status=resp.status_code, content_type=content_type, body=bytes(body))
            except httpx.HTTPError as exc:
                raise WebFetchError(f"falha de rede: {type(exc).__name__}") from exc
        raise WebFetchError(f"redirects demais (mais de {MAX_REDIRECTS})")

    async def allowed(self, url: str) -> bool:
        """O `robots.txt` do site deixa o KuroBot baixar esta URL?

        Como no RFC 9309: robots inexistente (4xx) libera tudo; inalcançável (5xx,
        rede) bloqueia tudo — melhor não coletar do que coletar o que o site proíbe."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        async with self._robots_lock:
            if origin not in self._robots:
                self._robots[origin] = await self._load_robots(origin)
        robots = self._robots[origin]
        return True if robots is None else robots.can_fetch(self.user_agent, url)

    async def _load_robots(self, origin: str) -> RobotFileParser | None:
        parser = RobotFileParser()
        try:
            fetched = await self.get(f"{origin}/robots.txt", accept=())
        except WebFetchError as exc:
            if str(exc).startswith("HTTP 4"):
                return None
            parser.disallow_all = True
            return parser
        parser.parse(fetched.text.splitlines())
        return parser

    async def sitemaps_from_robots(self, url: str) -> list[str]:
        await self.allowed(url)
        parts = urlsplit(url)
        robots = self._robots.get(f"{parts.scheme}://{parts.netloc}")
        return list(robots.site_maps() or []) if robots is not None else []


# -- sitemap -----------------------------------------------------------------------

_SAFE_XML = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=True)


def parse_sitemap(body: bytes) -> tuple[list[str], list[str]]:
    """(páginas, sitemaps filhos) de um sitemap ou índice de sitemaps.

    O parser não resolve entidades nem busca nada na rede (XXE)."""
    if body[:2] == b"\x1f\x8b":
        # Lê só até o teto: `gzip.decompress` expandiria tudo antes (bomba de gzip).
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as unzipped:
            body = unzipped.read(MAX_PAGE_BYTES * 4)
    try:
        root = etree.fromstring(body, parser=_SAFE_XML)
    except etree.XMLSyntaxError:
        return [], []
    if root is None:
        return [], []
    locs = [el.text.strip() for el in root.iter() if isinstance(el.tag, str) and el.tag.endswith("loc") and el.text]
    if etree.QName(root).localname == "sitemapindex":
        return [], locs
    return locs, []


# -- escopo do crawl ---------------------------------------------------------------


@dataclass
class Scope:
    """Quais URLs um crawl pode visitar: o mesmo host, abaixo do caminho inicial,
    e os filtros `include`/`exclude` (globs no caminho, ex. `/docs/*`)."""

    start_url: str
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    same_path: bool = True

    def __post_init__(self) -> None:
        parts = urlsplit(normalize_url(self.start_url))
        self.host = parts.netloc
        path = parts.path or "/"
        # `/docs/intro` → `/docs/`; `/docs` → `/docs` (cobre `/docs/...` também).
        self.prefix = path if path.endswith("/") or "." not in path.rsplit("/", 1)[-1] else path.rsplit("/", 1)[0] + "/"

    def accepts(self, url: str) -> bool:
        """Uma página do escopo (arquivos ficam de fora: `accepts_file`)."""
        return looks_like_page(url) and self.in_scope(url)

    def accepts_file(self, url: str) -> bool:
        """Um arquivo legível (`FILE_TYPES`) do mesmo escopo das páginas."""
        return file_extension(url) is not None and self.in_scope(url)

    def in_scope(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.netloc.lower() != self.host:
            return False
        path = parts.path or "/"
        if self.same_path and self.prefix not in ("", "/") and not (
            path == self.prefix.rstrip("/") or path.startswith(self.prefix.rstrip("/") + "/")
        ):
            return False
        if self.include and not any(fnmatch.fnmatch(path, p) for p in self.include):
            return False
        return not any(fnmatch.fnmatch(path, p) for p in self.exclude)


# -- extração ----------------------------------------------------------------------

REMOVE_TAGS = (
    "script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed",
    "form", "button", "input", "select", "textarea", "dialog", "link", "meta",
)
CHROME_TAGS = ("nav", "footer", "aside", "header")
CHROME_ROLES = ("navigation", "banner", "contentinfo", "complementary", "search", "dialog", "alert")
NOISE = re.compile(
    r"(^|[-_\s])(cookies?|consent|gdpr|newsletter|share|sharing|social|advert|ads|promo|popup|modal|"
    r"breadcrumbs?|skip-link|sidebar|toc|table-of-contents|feedback|edit-?page|pagination)([-_\s]|$)",
    re.I,
)
BLOCK_TAGS = frozenset(
    "p div section article main li ul ol h1 h2 h3 h4 h5 h6 pre table tr blockquote dl dt dd figure figcaption "
    "details summary hr br body".split()
)


@dataclass
class Extracted:
    url: str
    title: str
    markdown: str
    links: list[str]
    canonical: str | None = None
    lang: str | None = None


def _text(el: etree._Element) -> str:
    return re.sub(r"\s+", " ", el.text_content() or "").strip()


def _is_noise(el: etree._Element) -> bool:
    if el.get("hidden") is not None or el.get("aria-hidden") == "true":
        return True
    style = (el.get("style") or "").replace(" ", "").lower()
    if "display:none" in style or "visibility:hidden" in style:
        return True
    if (el.get("role") or "").lower() in CHROME_ROLES:
        return True
    marker = f"{el.get('id') or ''} {el.get('class') or ''}"
    return bool(marker.strip()) and bool(NOISE.search(marker))


def _drop(el: etree._Element) -> None:
    parent = el.getparent()
    if parent is not None:
        el.drop_tree()


def _content_root(doc: etree._Element) -> etree._Element:
    for xpath in ("//main", "//*[@role='main']"):
        found = doc.xpath(xpath)
        if found:
            return max(found, key=lambda e: len(_text(e)))
    articles = doc.xpath("//article")
    if articles:
        best = max(articles, key=lambda e: len(_text(e)))
        if len(_text(best)) > 200:
            return best
    body = doc.find("body")
    return body if body is not None else doc


class _Markdown:
    """HTML → Markdown suficiente para RAG: títulos, listas, tabelas, código e parágrafos."""

    def __init__(self) -> None:
        self.out: list[str] = []

    def render(self, root: etree._Element) -> str:
        self._block(root, depth=0)
        text = "\n".join(self.out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    def _inline(self, el: etree._Element) -> str:
        parts: list[str] = [el.text or ""] if el.tag not in ("br",) else ["\n"]
        for child in el:
            if not isinstance(child.tag, str):
                parts.append(child.tail or "")
                continue
            tag = child.tag.lower()
            if tag == "br":
                parts.append("\n")
            elif tag == "code":
                parts.append(f"`{child.text_content().strip()}`")
            elif tag == "img":
                alt = (child.get("alt") or "").strip()
                parts.append(f"[imagem: {alt}]" if alt else "")
            else:
                parts.append(self._inline(child))
            parts.append(child.tail or "")
        return "".join(parts)

    @staticmethod
    def _clean(text: str) -> str:
        return "\n".join(re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n")).strip()

    def _emit(self, text: str) -> None:
        text = self._clean(text)
        if text:
            self.out.extend([text, ""])

    def _has_blocks(self, el: etree._Element) -> bool:
        return any(isinstance(c.tag, str) and c.tag.lower() in BLOCK_TAGS for c in el.iterdescendants())

    def _block(self, el: etree._Element, depth: int) -> None:
        tag = el.tag.lower() if isinstance(el.tag, str) else ""
        if re.fullmatch(r"h[1-6]", tag):
            title = self._clean(self._inline(el)).replace("\n", " ")
            if title:
                self.out.extend(["#" * int(tag[1]) + " " + title, ""])
            return
        if tag in ("ul", "ol"):
            self._list(el, ordered=tag == "ol", depth=depth)
            self.out.append("")
            return
        if tag == "pre":
            code = (el.text_content() or "").strip("\n")
            if code.strip():
                self.out.extend(["```", code, "```", ""])
            return
        if tag == "table":
            self._table(el)
            return
        if tag == "blockquote":
            inner = _Markdown()
            inner._block_children(el, depth)
            body = "\n".join(inner.out).strip()
            if body:
                self.out.extend(["> " + line if line else ">" for line in body.split("\n")] + [""])
            return
        if tag == "hr":
            return
        if tag in ("dt",):
            self._emit(f"**{self._clean(self._inline(el))}**")
            return
        if not self._has_blocks(el):
            self._emit(self._inline(el))
            return
        self._block_children(el, depth)

    def _block_children(self, el: etree._Element, depth: int) -> None:
        if el.text and el.text.strip():
            self._emit(el.text)
        for child in el:
            if isinstance(child.tag, str):
                self._block(child, depth)
            if child.tail and child.tail.strip():
                self._emit(child.tail)

    def _list(self, el: etree._Element, *, ordered: bool, depth: int) -> None:
        n = 0
        for li in el:
            if not isinstance(li.tag, str) or li.tag.lower() != "li":
                continue
            n += 1
            bullet = f"{n}." if ordered else "-"
            nested = [c for c in li if isinstance(c.tag, str) and c.tag.lower() in ("ul", "ol")]
            for c in nested:
                li.remove(c)
            text = self._clean(_text(li) if self._has_blocks(li) else self._inline(li)).replace("\n", " ")
            if text:
                self.out.append("  " * depth + f"{bullet} {text}")
            for c in nested:
                self._list(c, ordered=c.tag.lower() == "ol", depth=depth + 1)

    def _table(self, el: etree._Element) -> None:
        rows = []
        for tr in el.iter("tr"):
            cells = [self._clean(_text(c)).replace("|", "\\|") for c in tr if isinstance(c.tag, str) and c.tag.lower() in ("td", "th")]
            if any(cells):
                rows.append(cells)
        if not rows:
            return
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        self.out.extend(lines + [""])


def extract(body: str, url: str) -> Extracted:
    """O conteúdo principal de uma página HTML, em Markdown, e os links dela."""
    # O lxml recusa uma string com declaração de encoding (`<?xml ... encoding=...?>`),
    # comum em XHTML — sem isto a página sairia "sem conteúdo".
    body = XML_DECLARATION.sub("", body, count=1)
    try:
        doc = lxml_html.fromstring(body)
    except (etree.ParserError, ValueError):
        return Extracted(url=url, title="", markdown="", links=[])
    base = url
    base_el = doc.find(".//base[@href]")
    if base_el is not None:
        base = urljoin(url, base_el.get("href"))
    canonical = None
    for link in doc.xpath("//link[@rel='canonical'][@href]"):
        canonical = normalize_url(urljoin(base, link.get("href")))
        break
    links = []
    for a in doc.xpath("//a[@href]"):
        href = (a.get("href") or "").strip()
        if href and not href.startswith(("mailto:", "tel:", "javascript:", "#")):
            links.append(normalize_url(urljoin(base, href)))

    og_title = doc.xpath("string(//meta[@property='og:title']/@content)").strip()
    title_tag = doc.xpath("string(//title)").strip()
    h1 = doc.xpath("string((//h1)[1])").strip()
    title = re.sub(r"\s+", " ", og_title or title_tag or h1 or url)
    lang = (doc.get("lang") or "").strip() or None

    for el in list(doc.iter(*REMOVE_TAGS)):
        _drop(el)
    root = _content_root(doc)
    for el in list(root.iter()):
        if not isinstance(el.tag, str) or el is root:
            continue
        if el.getparent() is None:
            continue  # já saiu junto com um ancestral
        tag = el.tag.lower()
        # Um <header> dentro do conteúdo costuma ser o título da própria página.
        chrome = tag in CHROME_TAGS and not (tag == "header" and el.xpath(".//h1|.//h2"))
        if chrome or _is_noise(el):
            _drop(el)
    markdown = _Markdown().render(root)
    return Extracted(url=url, title=title, markdown=markdown, links=list(dict.fromkeys(links)), canonical=canonical, lang=lang)


def extract_text_document(body: str, url: str) -> Extracted:
    """Página que já é texto (`text/plain`, `text/markdown`): nada a limpar."""
    first = next((line.strip("# ").strip() for line in body.splitlines() if line.strip()), "")
    return Extracted(url=url, title=first[:200] or url, markdown=body.strip(), links=[])


def unique(urls: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(urls))
