"""`kuro dash`: o que está acontecendo no Kuro agora, sem abrir o console web.

Um cabeçalho e duas abas, cada uma respondendo a uma pergunta:

- **cabeçalho**: o corvo (`tui/crow.py`) e a conexão. O humor do corvo é o estado do
  painel: ocioso, execuções chegando, erro, pausado ou serviço fora do ar.
- **Ao vivo** — "o que está acontecendo agora?": as últimas execuções, atualizadas
  sozinhas (consulta repetida, como `kuro runs tail`), com um resumo do que está na tela
  e filtro por agente e status. Enter abre o trace: mensagem, resposta, spans de modelo e
  tools com entrada e saída, e as notas.
- **Panorama** — "como foi o período?": os números do dashboard dos Logs
  (`/observability/overview`) em cartões contra o período anterior, uma linha por agente
  e as tools falhando.

Todo acesso à API roda numa thread (`@work(thread=True)`): o `Client` é síncrono, e
uma chamada lenta não pode congelar a tela.
"""

import json
import os
import time
from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta
from typing import Any

from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import (
    Checkbox,
    DataTable,
    Footer,
    HelpPanel,
    Input,
    Select,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)

from agent_service.cli.client import ApiError, Client, ServiceUnavailable
from agent_service.tui.crow import Crow

PREVIEW = 600  # caracteres da entrada/saída de um span mostrados no trace

PERIODS = [("últimas 24 h", 1), ("últimos 7 dias", 7), ("últimos 30 dias", 30)]

ALERT_SECONDS = 8.0  # quanto tempo o corvo fica em alerta depois de um erro
BUSY_SECONDS = 2.0  # e batendo as asas depois de chegarem execuções

# As cores do console web (frontend), para o terminal e o navegador parecerem o mesmo produto.
KURO_THEME = Theme(
    name="kuro",
    primary="#22d3ee",
    secondary="#0e7490",
    accent="#22d3ee",
    foreground="#e2e8f0",
    background="#0a0a0c",
    surface="#111418",
    panel="#161b22",
    success="#22c55e",
    warning="#f59e0b",
    error="#ef4444",
    dark=True,
)
OK, WARN, BAD, MUTED, BRAND = "#22c55e", "#f59e0b", "#ef4444", "#64748b", "#22d3ee"

SPARK = "▁▂▃▄▅▆▇█"

REPO_URL = "https://github.com/Arthur-Marques-IA/microservice_agents"

_OFF = ("0", "false", "no", "nao", "não", "off")


def prefer_truecolor(environ: MutableMapping[str, str] = os.environ) -> bool:
    """Liga as 16 milhões de cores quando ninguém disse o contrário.

    Pelo SSH chega o `TERM` (`xterm-256color`), mas não o `COLORTERM=truecolor`: o Rich
    então reduz cada cor do tema à paleta de 256, e o painel muda de cara (o corvo fica
    verde-azulado, o cabeçalho, preto). Quase todo terminal de hoje faz truecolor, então
    o padrão é ligar. Fica como está quem já definiu `COLORTERM`, quem pediu `NO_COLOR`, quem
    desligou com `KURO_TRUECOLOR=0` e os terminais que sabidamente não fazem (o console do
    Linux, `dumb` e o Terminal.app antigo do macOS). Devolve se ligou.
    """
    if environ.get("COLORTERM") or "NO_COLOR" in environ:
        return False
    if environ.get("KURO_TRUECOLOR", "").strip().lower() in _OFF:
        return False
    if environ.get("TERM", "").strip().lower() in ("dumb", "linux") or environ.get("TERM_PROGRAM") == "Apple_Terminal":
        return False
    environ["COLORTERM"] = "truecolor"
    return True


def describe_error(exc: Exception) -> str:
    if isinstance(exc, ApiError):
        if exc.status in (401, 403):
            return f"sem permissão (HTTP {exc.status}): use uma chave de escopo admin em KURO_API_KEY"
        if exc.status == 501:
            return "o panorama só existe no trace store local (TRACE_STORE_BACKEND=langfuse responde 501)"
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, ensure_ascii=False)
        return f"{detail} (HTTP {exc.status})"
    return str(exc)


def _when(value: Any) -> str:
    """Data e hora no fuso do terminal; o que não for ISO 8601 sai como veio."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value or "")[5:16].replace("T", " ")
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.strftime("%d/%m %H:%M:%S")


def _cost(value: float | None) -> str:
    return "—" if value is None else f"US$ {value:.4f}"


def _int(value: int | None) -> str:
    return f"{value or 0:,}".replace(",", ".")


def _compact(value: float | None) -> str:
    value = value or 0
    for limit, suffix in ((1e6, "M"), (1e3, "k")):
        if value >= limit:
            return f"{value / limit:.1f}{suffix}"
    return str(int(value))


def _ms(value: float | None) -> str:
    if not value:
        return "—"
    return f"{value:.0f}ms" if value < 1000 else f"{value / 1000:.1f}s"


def _bad(run: dict[str, Any]) -> bool:
    return run["status"] != "success" or (run.get("tool_failures") or 0) > 0


def _status(run: dict[str, Any]) -> Text:
    if run["status"] != "success":
        label = {"error": "erro", "interrupted": "interrompido"}.get(run["status"], run["status"])
        return Text(f"● {label}", style=f"bold {BAD}")
    if (run.get("tool_failures") or 0) > 0:
        return Text("▲ tool", style=WARN)
    return Text("● ok", style=OK)


def _is_test(run: dict[str, Any]) -> bool:
    return str((run.get("metadata") or {}).get("dry_run", "")).lower() == "true"


def _one_line(text: str | None, limit: int = 70) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _preview(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= PREVIEW else text[:PREVIEW] + f"… (+{len(text) - PREVIEW})"


def _p95(values: list[float]) -> float | None:
    values = sorted(v for v in values if v)
    return values[min(len(values) - 1, int(len(values) * 0.95))] if values else None


def spark(values: list[float], width: int = 22) -> str:
    """Minigráfico de barras; agrupa em `width` pontos para caber num cartão."""
    if not values:
        return ""
    if len(values) > width:
        size = len(values) / width
        values = [max(values[int(i * size): max(int((i + 1) * size), int(i * size) + 1)]) for i in range(width)]
    top = max(values)
    if top <= 0:
        return SPARK[0] * len(values)
    return "".join(SPARK[min(len(SPARK) - 1, int(v / top * (len(SPARK) - 1) + 0.5))] for v in values)


def _change(current: float | None, previous: float | None, *, worse_up: bool | None, points: bool = False) -> Text:
    """Variação contra o período anterior. `worse_up`: subir é ruim (vermelho), bom ou neutro (None)."""
    if current is None or previous is None or (not previous and not points):
        return Text("sem base anterior", style=MUTED)
    diff = current - previous
    if abs(diff) < 1e-9:
        return Text("= igual", style=MUTED)
    arrow = "▲" if diff > 0 else "▼"
    label = f"{arrow} {diff * 100:+.1f}pp" if points else f"{arrow} {diff / previous:+.0%}"
    if worse_up is None:
        style = MUTED
    else:
        style = BAD if (diff > 0) == worse_up else OK
    return Text(label, style=style)


# -- teclas em português -------------------------------------------------------------

# `tab` circula entre os blocos de dados (as tabelas e a árvore), não por todo widget:
# os filtros têm tecla própria. Ver `KuroDash.action_focus_next`.
PANEL_BINDINGS = [
    Binding("tab", "app.focus_next", "próximo painel", show=False),
    Binding("shift+tab", "app.focus_previous", "painel anterior", show=False),
]


class Table(DataTable):
    BINDINGS = [
        Binding("enter", "select_cursor", "abrir", show=False),
        Binding("up", "cursor_up", "sobe", show=False),
        Binding("down", "cursor_down", "desce", show=False),
        Binding("right", "cursor_right", "direita", show=False),
        Binding("left", "cursor_left", "esquerda", show=False),
        Binding("pageup", "page_up", "página acima", show=False),
        Binding("pagedown", "page_down", "página abaixo", show=False),
        Binding("ctrl+home", "scroll_top", "primeira linha", show=False),
        Binding("ctrl+end", "scroll_bottom", "última linha", show=False),
        Binding("home", "scroll_home", "início da linha", show=False),
        Binding("end", "scroll_end", "fim da linha", show=False),
    ]


class MainScreen(Screen):
    BINDINGS = PANEL_BINDINGS


# -- cabeçalho -----------------------------------------------------------------------


class AnimToggle(Static):
    """Botão que liga e desliga as animações (o mesmo que a tecla `a`)."""

    def on_click(self) -> None:
        self.app.action_toggle_animations()


class Masthead(Horizontal):
    """O corvo, o nome, onde o painel está ligado e o estado da conexão."""

    def compose(self) -> ComposeResult:
        app: KuroDash = self.app  # type: ignore[assignment]
        yield Crow(app.crow_mood, lambda: app.animate)
        with Vertical(classes="brand"):
            yield Static(Text.assemble(("kuro", f"bold {BRAND}"), (" dash", MUTED)), classes="title")
            yield Static(classes="where")
            yield Static(classes="conn")
            yield Static(
                Text.assemble(
                    (REPO_URL.removeprefix("https://github.com/"), Style(color=MUTED, link=REPO_URL)),
                    ("  g abre no GitHub", MUTED),
                    no_wrap=True,
                    overflow="ellipsis",
                ),
                classes="repo",
            )
        yield AnimToggle(classes="anim-toggle")

    def on_mount(self) -> None:
        self.sync()

    def sync(self) -> None:
        app: KuroDash = self.app  # type: ignore[assignment]
        version = f" · v{app.version}" if app.version else ""
        self.query_one(".where", Static).update(Text(f"{app.client.base_url}{version}", style=MUTED))
        self.query_one(".conn", Static).update(app.connection())
        on_ = app.animate
        self.query_one(AnimToggle).update(
            Text.assemble(("◉ " if on_ else "○ ", BRAND if on_ else MUTED), f"animações {'ligadas' if on_ else 'desligadas'}")
        )


# -- trace de uma execução -----------------------------------------------------------


SPAN_ICON = {"GENERATION": ("◆", BRAND), "TOOL": ("■", WARN)}


class RunScreen(Screen):
    BINDINGS = [Binding("escape,q", "app.pop_screen", "voltar"), *PANEL_BINDINGS]

    def __init__(self, client: Client, run_id: str) -> None:
        super().__init__()
        self.client = client
        self.run_id = run_id

    def compose(self) -> ComposeResult:
        yield Masthead()
        with VerticalScroll(id="trace"):
            yield Static(f"carregando {self.run_id}…", id="run-head", classes="box")
            with Horizontal(id="run-io"):
                yield Static(id="run-msg", classes="box")
                yield Static(id="run-out", classes="box")
            yield Tree("spans", id="spans", classes="panel")
            yield Static(id="scores", classes="box")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#run-head").border_title = "execução"
        self.query_one("#run-msg").border_title = "mensagem"
        self.query_one("#run-out").border_title = "resposta"
        self.query_one("#scores").border_title = "notas"
        self.query_one("#scores").display = False
        tree = self.query_one(Tree)
        tree.border_title = "spans · ◆ modelo  ■ tool · a barra é a duração"
        tree.show_root = False
        self.load()

    @work(thread=True, exclusive=True)
    def load(self) -> None:
        try:
            trace = self.client.run_trace(self.run_id)
        except (ApiError, ServiceUnavailable) as exc:
            self.app.call_from_thread(self.query_one("#run-head", Static).update, Text(describe_error(exc), style=BAD))
            return
        self.app.call_from_thread(self.show, trace)

    def show(self, trace: dict[str, Any]) -> None:
        run = trace["run"]
        version = f" · v{run['agent_version']}" if run.get("agent_version") else ""
        head = Text.assemble(
            (run["agent_type"], "bold"),
            f"{version}   ",
            _status(run),
            f"   {_int(run['total_tokens'])} tokens   {_cost(run.get('cost_usd'))}   {_ms(run.get('latency_ms'))}",
            ("   teste (dry_run)" if _is_test(run) else "", WARN),
            "\n",
            (f"run {run['run_id']} · sessão {run.get('session_id') or '—'} · {_when(run['started_at'])}", MUTED),
        )
        if run.get("status_message"):
            head.append(f"\n{run['status_message']}", style=BAD)
        self.query_one("#run-head", Static).update(head)
        self.query_one("#run-msg", Static).update(run.get("message") or Text("—", style=MUTED))
        self.query_one("#run-out", Static).update(run.get("output") or Text("—", style=MUTED))

        tree = self.query_one(Tree)
        tree.clear()
        spans = sorted(trace.get("spans") or [], key=lambda s: str(s["started_at"]))
        ids = {s["id"] for s in spans}
        longest = max((s.get("latency_ms") or 0 for s in spans), default=0)
        nodes: dict[str, Any] = {}
        for span in spans:
            parent = nodes.get(span.get("parent_id")) if span.get("parent_id") in ids else tree.root
            failed = span["level"] not in ("DEFAULT", "DEBUG")
            icon, color = SPAN_ICON.get(span["type"], ("○", MUTED))
            filled = round((span.get("latency_ms") or 0) / longest * 12) if longest else 0
            if span.get("latency_ms"):
                filled = max(filled, 1)
            label = Text.assemble(
                (icon, BAD if failed else color), " ",
                (span["name"], f"bold {BAD}" if failed else "bold"), "  ",
                ("━" * filled, BAD if failed else color), ("─" * (12 - filled), "#2a3442"),
                (f" {_ms(span.get('latency_ms'))}", MUTED),
            )
            node = (parent or tree.root).add(label, expand=failed)
            if span.get("status_message"):
                node.add_leaf(Text(span["status_message"], style=BAD))
            if span.get("input") is not None:
                node.add_leaf(Text.assemble(("entrada ", BRAND), _preview(span["input"])))
            if span.get("output") is not None:
                node.add_leaf(Text.assemble(("saída ", "#c084fc"), _preview(span["output"])))
            nodes[span["id"]] = node
            if failed:  # a falha não pode ficar escondida dentro de um nó fechado
                ancestor = node.parent
                while ancestor is not None:
                    ancestor.expand()
                    ancestor = ancestor.parent
        tree.root.expand()

        scores = trace.get("scores") or []
        box = self.query_one("#scores", Static)
        box.display = bool(scores)
        box.update("\n".join(f"{s['name']} = {s['value']}  {s.get('comment') or ''}" for s in scores))


# -- abas --------------------------------------------------------------------------


class RunsPane(TabPane):
    """Ao vivo: as últimas execuções, atualizadas a cada `interval` segundos."""

    def __init__(self, client: Client, interval: float, agent: str | None) -> None:
        super().__init__("Ao vivo", id="runs-pane")
        self.client = client
        self.interval = interval
        self.initial_agent = agent or ""
        self.shown: list[str] = []
        # Execuções já vistas com os filtros atuais. `None` = a próxima consulta só
        # forma a base: abrir o painel ou trocar o filtro não é "chegou execução".
        self.seen: set[str] | None = None
        # Cada troca de filtro abre uma geração nova. Uma consulta lenta da geração anterior
        # (thread não se cancela) que chegue depois é descartada em vez de virar "novidade".
        self.generation = 0

    def compose(self) -> ComposeResult:
        with Horizontal(id="filters"):
            yield Input(value=self.initial_agent, placeholder="filtrar por agente (enter)", id="agent")
            yield Select([("só sucesso", "success"), ("só erro", "error")], prompt="todos os status", id="status")
            yield Checkbox("esconder testes (dry_run)", id="no-tests")
        yield Static(id="runs-summary")
        yield Table(id="runs", cursor_type="row", zebra_stripes=True, classes="panel")
        yield Static(id="runs-empty")
        yield Static(id="runs-status")

    def on_mount(self) -> None:
        table = self.query_one("#runs", DataTable)
        table.add_columns("status", "quando", "agente", "v", "tokens", "tempo", "custo", "mensagem")
        self.query_one("#runs-empty").display = False
        # Foco na tabela, não no filtro: com o filtro focado, `q`/`r`/`p`/`a` virariam texto.
        table.focus()
        self.refresh_runs()
        self.set_interval(self.interval, self.tick)

    def tick(self) -> None:
        if not self.app.paused:
            self.refresh_runs()

    def filters(self) -> dict[str, Any]:
        status = self.query_one("#status", Select).value
        return {
            "agent_type": self.query_one("#agent", Input).value.strip() or None,
            "status": status if isinstance(status, str) else None,
            "include_dry_run": False if self.query_one("#no-tests", Checkbox).value else None,
            "limit": 50,
        }

    def refresh_runs(self) -> None:
        # Os widgets só são lidos aqui, na thread da interface; o worker recebe os valores.
        self.fetch_runs(self.filters(), self.generation)

    @work(thread=True, exclusive=True, group="runs")
    def fetch_runs(self, filters: dict[str, Any], generation: int) -> None:
        try:
            page = self.client.list_runs(**filters)
        except (ApiError, ServiceUnavailable) as exc:
            self.app.call_from_thread(self.failed, exc, generation)
            return
        self.app.call_from_thread(self.show, page["items"], generation)

    def failed(self, exc: Exception, generation: int) -> None:
        if generation != self.generation:
            return
        self.set_status(Text(describe_error(exc), style=BAD))
        if not self.shown:  # nada na tela: explica em vez de mostrar uma tabela vazia
            self.show_empty(Text.assemble(
                ("Sem execuções para mostrar.\n", "bold"),
                (describe_error(exc), BAD),
                (f"\n\nO painel tenta de novo sozinho a cada {self.interval:g}s (r tenta agora).", MUTED),
            ))
        self.app.feed_failed(exc)

    def show_empty(self, message: Text | None) -> None:
        empty = self.query_one("#runs-empty", Static)
        self.query_one("#runs", DataTable).display = message is None
        empty.display = message is not None
        if message is not None:
            empty.update(message)

    def show(self, runs: list[dict[str, Any]], generation: int) -> None:
        if generation != self.generation:
            return
        ids = [r["run_id"] for r in runs]
        fresh = [] if self.seen is None else [r for r in runs if r["run_id"] not in self.seen]
        self.seen = (self.seen or set()) | set(ids)
        table = self.query_one("#runs", DataTable)
        if ids != self.shown:
            fresh_ids = {r["run_id"] for r in fresh}
            selected = self.selected_run()
            table.clear()
            for r in runs:
                new = r["run_id"] in fresh_ids
                agent = Text(r["agent_type"])
                if _is_test(r):
                    agent.append(" teste", style=f"italic {MUTED}")
                table.add_row(
                    _status(r),
                    Text(("▸ " if new else "  ") + _when(r["started_at"]), style=f"bold {BRAND}" if new else ""),
                    agent,
                    Text(str(r.get("agent_version") or "—"), style=MUTED),
                    Text(_int(r["total_tokens"]), justify="right"),
                    Text(_ms(r.get("latency_ms")), justify="right"),
                    Text(_cost(r.get("cost_usd")), justify="right", style=MUTED),
                    Text(_one_line(r.get("message")), style=MUTED),
                    key=r["run_id"],
                )
            if selected in ids:
                table.move_cursor(row=ids.index(selected))
            self.shown = ids
        self.query_one("#runs-summary", Static).update(self.summary(runs))
        if runs:
            self.show_empty(None)
        elif any(v is not None for k, v in self.filters().items() if k != "limit"):
            self.show_empty(Text("Nenhuma execução com esses filtros.", style=MUTED))
        else:
            self.show_empty(
                Text("Nenhuma execução ainda: elas aparecem aqui assim que alguém chamar /chat ou /analyze.",
                     style=MUTED)
            )
        since = f" desde {_when(runs[-1]['started_at'])}" if runs else ""
        hint = "pausado, p retoma" if self.app.paused else f"atualiza a cada {self.interval:g}s · enter abre o trace"
        self.set_status(f"{len(runs)} execuções{since} · {hint}")
        self.app.feed_ok(fresh)

    def summary(self, runs: list[dict[str, Any]]) -> Text:
        if not runs:
            return Text("")
        errors = sum(1 for r in runs if r["status"] != "success")
        tools = sum(1 for r in runs if r["status"] == "success" and (r.get("tool_failures") or 0) > 0)
        cost = sum(r.get("cost_usd") or 0 for r in runs)
        return Text.assemble(
            ("● ", OK), (f"{len(runs) - errors - tools} ok", "bold"), "  ",
            ("● ", BAD if errors else MUTED), (f"{errors} erro{'s' if errors != 1 else ''}", "bold"), "  ",
            ("▲ ", WARN if tools else MUTED), (f"{tools} tool falhou", "bold"), "  ",
            ("p95 ", MUTED), (_ms(_p95([r.get("latency_ms") or 0 for r in runs])), "bold"), "  ",
            (_compact(sum(r["total_tokens"] or 0 for r in runs)), "bold"), (" tokens  ", MUTED),
            (_cost(cost), "bold"),
        )

    def set_status(self, message: str | Text) -> None:
        self.query_one("#runs-status", Static).update(message)

    def selected_run(self) -> str | None:
        table = self.query_one("#runs", DataTable)
        if table.row_count == 0:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    @on(Input.Submitted, "#agent")
    @on(Select.Changed, "#status")
    @on(Checkbox.Changed, "#no-tests")
    def filters_changed(self) -> None:
        self.shown = []
        self.seen = None
        self.generation += 1
        self.refresh_runs()

    @on(Input.Submitted, "#agent")
    def back_to_table(self) -> None:
        self.query_one("#runs").focus()

    @on(DataTable.RowSelected, "#runs")
    def open_run(self, event: DataTable.RowSelected) -> None:
        self.app.push_screen(RunScreen(self.client, event.row_key.value))


# (id, título do cartão)
CARDS = [
    ("runs", "execuções"),
    ("errors", "taxa de erro"),
    ("tools", "tool falhou"),
    ("p95", "latência p95"),
    ("tokens", "tokens"),
    ("cost", "custo"),
]


class OverviewPane(TabPane):
    """Panorama: o período inteiro contra o anterior. Precisa de escopo admin."""

    def __init__(self, client: Client, agent: str | None) -> None:
        super().__init__("Panorama", id="overview-pane")
        self.client = client
        self.agent = agent

    def compose(self) -> ComposeResult:
        with Horizontal(id="period-bar"):
            yield Select(PERIODS, value=7, allow_blank=False, id="period")
            yield Checkbox("incluir testes (dry_run)", id="with-tests")
        yield Static(
            "comparado ao período anterior de mesmo tamanho · atualiza a cada minuto · tab alterna agentes e tools",
            id="period-hint",
        )
        with VerticalScroll(id="overview-body"):
            yield Static(id="overview-msg")
            with Grid(id="cards"):
                for key, _ in CARDS:
                    yield Static(id=f"card-{key}", classes="card")
            yield Static(Text.assemble(("Agentes", "bold"), ("   execuções ao longo do período na tendência", MUTED)),
                         classes="section")
            yield Table(id="agents", cursor_type="row", zebra_stripes=True, classes="panel")
            yield Static(Text.assemble(("Tools falhando", "bold"), ("   respostas que deram certo com tool quebrada", MUTED)),
                         classes="section")
            yield Table(id="tool-failures", cursor_type="row", zebra_stripes=True, classes="panel")
            yield Static(Text("✓ nenhuma tool falhou no período", style=OK), id="failures-empty")

    def on_mount(self) -> None:
        for key, title in CARDS:
            self.query_one(f"#card-{key}").border_title = title
        self.query_one("#overview-msg").update(Text("carregando o panorama…", style=MUTED))
        self.query_one("#agents", DataTable).add_columns(
            "agente", "execuções", "tendência", "erros", "tool falhou", "p95", "tokens", "custo", "👍/👎", "última"
        )
        self.query_one("#tool-failures", DataTable).add_columns("tool", "falha", "vezes", "agentes", "HTTP", "última")
        self.query_one("#failures-empty").display = False
        self.load()
        self.set_interval(60, self.load)

    @on(Select.Changed, "#period")
    @on(Checkbox.Changed, "#with-tests")
    def period_changed(self) -> None:
        self.load()

    def load(self) -> None:
        days = self.query_one("#period", Select).value
        since = datetime.now(UTC) - timedelta(days=days if isinstance(days, int) else 7)
        self.fetch(
            {
                "since": since.isoformat(),
                "agent_type": self.agent,
                "include_dry_run": True if self.query_one("#with-tests", Checkbox).value else None,
            }
        )

    @work(thread=True, exclusive=True, group="overview")
    def fetch(self, params: dict[str, Any]) -> None:
        try:
            overview = self.client.overview(**params)
        except (ApiError, ServiceUnavailable) as exc:
            self.app.call_from_thread(self.failed, exc)
            return
        self.app.call_from_thread(self.show, overview)

    def failed(self, exc: Exception) -> None:
        # Só "fora do ar" mexe no corvo: um 403 sem chave admin ou o 501 do Langfuse é
        # configuração, não acontecimento, e não pode deixá-lo em alerta a cada minuto.
        if isinstance(exc, ServiceUnavailable):
            self.app.feed_failed(exc)
        self.query_one("#overview-msg", Static).update(Text(describe_error(exc), style=BAD))
        self.set_body(False)

    def set_body(self, visible: bool) -> None:
        self.query_one("#overview-msg").display = not visible
        for widget in self.query_one("#overview-body").children:
            if widget.id != "overview-msg":
                widget.display = visible

    def card(self, key: str, value: str, change: Text, series: list[float]) -> None:
        self.query_one(f"#card-{key}", Static).update(
            Text.assemble((value, "bold"), "  ", change, "\n", (spark(series), BRAND))
        )

    def show(self, overview: dict[str, Any]) -> None:
        self.set_body(True)
        totals, previous = overview["totals"], overview.get("previous") or {}
        buckets = overview.get("buckets") or []

        def rate(t: dict[str, Any]) -> float | None:
            return t["errors"] / t["runs"] if t.get("runs") and "errors" in t else None

        runs = totals["runs"]
        error_rate = rate(totals)
        self.card("runs", _int(runs), _change(runs, previous.get("runs"), worse_up=None),
                  [b["runs"] for b in buckets])
        self.card(
            "errors",
            f"{error_rate or 0:.1%} · {totals['errors']}",
            _change(error_rate, rate(previous), worse_up=True, points=True),
            [b["errors"] / b["runs"] if b["runs"] else 0 for b in buckets],
        )
        self.card("tools", _int(totals["tool_failure_runs"]),
                  _change(totals["tool_failure_runs"], previous.get("tool_failure_runs"), worse_up=True),
                  [b.get("tool_failure_runs") or 0 for b in buckets])
        self.card("p95", _ms(totals.get("p95_latency_ms")),
                  _change(totals.get("p95_latency_ms"), previous.get("p95_latency_ms"), worse_up=True),
                  [b.get("p95_latency_ms") or 0 for b in buckets])
        self.card("tokens", _compact(totals["total_tokens"]),
                  _change(totals["total_tokens"], previous.get("total_tokens"), worse_up=None),
                  [b.get("total_tokens") or 0 for b in buckets])
        self.card("cost", _cost(totals.get("cost_usd")),
                  _change(totals.get("cost_usd"), previous.get("cost_usd"), worse_up=True),
                  [b.get("cost_usd") or 0 for b in buckets])

        agents = self.query_one("#agents", DataTable)
        agents.clear()
        for row in overview["agents"]:
            t = row["totals"]
            agents.add_row(
                Text(row["agent_type"], style="bold"),
                Text(_int(t["runs"]), justify="right"),
                Text(spark(row.get("trend") or [], width=16), style=BRAND),
                Text(f"{t['errors']} ({rate(t) or 0:.0%})" if t["errors"] else "0", style=BAD if t["errors"] else MUTED),
                Text(str(t["tool_failure_runs"]), style=WARN if t["tool_failure_runs"] else MUTED),
                Text(_ms(t.get("p95_latency_ms")), justify="right"),
                Text(_compact(t["total_tokens"]), justify="right"),
                Text(_cost(t.get("cost_usd")), justify="right", style=MUTED),
                f"{t['feedback_up']}/{t['feedback_down']}",
                Text(_when(row["last_run_at"]), style=MUTED),
                key=row["agent_type"],
            )
        failures = self.query_one("#tool-failures", DataTable)
        failures.clear()
        for row in overview["tool_failures"]:
            failures.add_row(
                Text(row["tool_name"], style="bold"),
                Text(row["failure"], style=WARN),
                Text(str(row["count"]), justify="right"),
                ", ".join(row["agent_types"]),
                ", ".join(str(s) for s in row.get("http_status") or []) or "—",
                Text(_when(row["last_at"]), style=MUTED),
            )
        failures.display = bool(overview["tool_failures"])
        self.query_one("#failures-empty").display = not overview["tool_failures"]


# -- aplicação -----------------------------------------------------------------------


class KuroDash(App):
    TITLE = "kuro dash"
    CSS = """
    Screen { background: $background; }
    Masthead { height: 6; background: $panel; padding: 0 1; }
    Masthead .brand { width: 1fr; height: 6; padding: 1 2 0 2; }
    AnimToggle { width: auto; height: 3; margin-top: 1; padding: 0 1; border: round $primary 40%; }
    AnimToggle:hover { border: round $primary; }
    TabbedContent { height: 1fr; }
    #filters, #period-bar { height: auto; padding: 0 1; }
    #filters Input { width: 34; }
    #filters Select, #period-bar Select { width: 24; }
    #period-hint { padding: 0 2; color: $text-muted; }
    #runs-summary { height: auto; padding: 0 2; }
    .panel { border-left: tall $panel-lighten-1; }
    .panel:focus { border-left: tall $primary; }
    DataTable:focus > .datatable--header { background: $primary 25%; }
    DataTable > .datatable--cursor { background: $primary 12%; color: $foreground; }
    DataTable:focus > .datatable--cursor { background: $primary 35%; color: $foreground; text-style: bold; }
    #runs { height: 1fr; margin: 1 1 0 1; }
    #runs-empty { height: 1fr; content-align: center middle; text-align: center; }
    #runs-status { height: 1; padding: 0 2; color: $text-muted; }
    #overview-body { height: 1fr; }
    #overview-msg { padding: 1 2; }
    #cards { grid-size: 3; grid-gutter: 0 1; grid-rows: 4; height: auto; padding: 1 1 0 1; }
    .card { height: 4; padding: 0 1; border: round $primary 40%; border-title-color: $text-muted; }
    .section { padding: 1 2 0 2; }
    #agents, #tool-failures { height: auto; max-height: 16; margin: 0 1; }
    #failures-empty { padding: 0 2; }
    #trace { height: 1fr; }
    .box { height: auto; margin: 0 1; padding: 0 1; border: round $primary 40%; border-title-color: $text-muted; }
    #run-io { height: auto; }
    #run-io .box { width: 1fr; }
    #spans { height: auto; margin: 0 1; padding: 0 1; border: round $primary 40%; border-title-color: $text-muted; }
    #spans:focus { border: round $primary; }
    Masthead .repo { color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis; }
    """
    # Tudo tem caminho pelo teclado; o mouse é opcional. Nenhum atalho é `priority`: com o
    # filtro de agente focado, as letras viram texto (o `Input` as consome antes).
    BINDINGS = [
        Binding("q", "quit", "sair"),
        Binding("right_square_bracket", "switch_tab(1)", "aba", key_display="]"),
        Binding("left_square_bracket", "switch_tab(-1)", "aba anterior", show=False, key_display="["),
        Binding("1", "tab('runs-pane')", "ao vivo", show=False),
        Binding("2", "tab('overview-pane')", "panorama", show=False),
        Binding("slash", "filter_agent", "filtrar", key_display="/"),
        Binding("s", "cycle_status", "status"),
        Binding("t", "toggle_tests", "testes"),
        Binding("d", "cycle_period", "período"),
        Binding("r", "refresh", "atualizar"),
        Binding("p", "pause", "pausar"),
        Binding("a", "toggle_animations", "animações"),
        Binding("g", "open_repo", "repositório no GitHub", show=False),
        Binding("question_mark", "toggle_help", "ajuda", key_display="?"),
        Binding("j", "move(1)", "desce", show=False),
        Binding("k", "move(-1)", "sobe", show=False),
        Binding("escape", "leave_input", "volta para a tabela", show=False),
    ]
    TABS = ("runs-pane", "overview-pane")

    def __init__(self, client: Client, *, interval: float = 3.0, agent: str | None = None) -> None:
        super().__init__()
        self.client = client
        self.interval = interval
        self.agent = agent
        self.version: str | None = None
        self.paused = False
        # `TEXTUAL_ANIMATIONS=none` já abre com o corvo parado.
        self.animate = self.animation_level != "none"
        self.offline: str | None = None
        self.api_error: str | None = None
        self.updated_at: str | None = None
        self.alert_until = 0.0
        self.busy_until = 0.0
        self.pending_focus: Widget | None = None
        self.register_theme(KURO_THEME)
        self.theme = "kuro"

    def get_default_screen(self) -> Screen:
        return MainScreen()

    def compose(self) -> ComposeResult:
        yield Masthead()
        with TabbedContent(initial="runs-pane"):
            yield RunsPane(self.client, self.interval, self.agent)
            yield OverviewPane(self.client, self.agent)
        yield Footer()

    def on_mount(self) -> None:
        self.check_service()

    @work(thread=True)
    def check_service(self) -> None:
        try:
            version = self.client.health().get("version")
        except (ApiError, ServiceUnavailable) as exc:
            self.call_from_thread(self.feed_failed, exc)
            return
        self.call_from_thread(self.service_up, version)

    def service_up(self, version: str | None) -> None:
        self.version = version
        self.sync_masthead()

    # -- estado do painel (o que o corvo e o cabeçalho mostram) ---------------------

    def crow_mood(self) -> str:
        now = time.monotonic()
        if self.offline:
            return "offline"
        if self.paused:
            return "paused"
        if now < self.alert_until:
            return "error"
        if now < self.busy_until:
            return "working"
        return "idle"

    def feed_ok(self, fresh: list[dict[str, Any]]) -> None:
        """A consulta das execuções respondeu; `fresh` são as que chegaram desde a anterior."""
        self.offline = self.api_error = None
        self.updated_at = datetime.now().strftime("%H:%M:%S")
        if any(_bad(r) for r in fresh):
            self.alert_until = time.monotonic() + ALERT_SECONDS
        elif fresh:
            self.busy_until = time.monotonic() + BUSY_SECONDS
        self.sync_masthead()

    def feed_failed(self, exc: Exception) -> None:
        if isinstance(exc, ServiceUnavailable):
            self.offline = describe_error(exc)
        else:
            message = describe_error(exc)
            # Alerta só ao entrar no erro: uma chave errada que dá 401 a cada consulta é
            # configuração, e não pode deixar o corvo em alerta enquanto o painel estiver aberto.
            if message != self.api_error:
                self.alert_until = time.monotonic() + ALERT_SECONDS
            self.api_error = message
        self.sync_masthead()

    def connection(self) -> Text:
        if self.offline:
            return Text.assemble(("● offline", f"bold {BAD}"), (f"  {_one_line(self.offline, 90)}", MUTED))
        if self.api_error:
            return Text.assemble(("● erro na API", f"bold {WARN}"), (f"  {_one_line(self.api_error, 90)}", MUTED))
        if self.paused:
            return Text.assemble(("● pausado", f"bold {MUTED}"), ("  tecle p para retomar", MUTED))
        if not self.updated_at:
            return Text("● conectando…", style=MUTED)
        return Text.assemble(
            ("● conectado", f"bold {OK}"), (f"  atualizado {self.updated_at}", MUTED)
        )

    @property
    def main(self) -> Screen:
        """A tela das abas, mesmo com um trace aberto por cima (`r` e `p` valem lá também)."""
        return self.screen_stack[0]

    def sync_masthead(self) -> None:
        for screen in self.screen_stack:
            for masthead in screen.query(Masthead):
                masthead.sync()

    # -- ações -----------------------------------------------------------------------

    # -- navegação pelo teclado ------------------------------------------------------

    def active_tab(self) -> str | None:
        try:
            return self.main.query_one(TabbedContent).active
        except NoMatches:
            return None

    def panels(self) -> list[Widget]:
        """Os blocos de dados da tela atual, na ordem do `tab`: as tabelas e a árvore de spans."""
        root: Widget = self.screen
        if self.screen is self.main and self.active_tab():
            root = self.main.query_one(f"#{self.active_tab()}")
        return [w for w in root.query(".panel") if w.display and w.focusable]

    def focus_panel(self, step: int = 0) -> None:
        panels = self.panels()
        if not panels:
            return
        if self.focused in panels:
            target = panels[(panels.index(self.focused) + step) % len(panels)]
        else:
            target = panels[0] if step >= 0 else panels[-1]
        target.focus()

    # `tab` e `shift+tab` circulam só entre os dados: os filtros têm tecla própria.
    def action_focus_next(self) -> None:
        self.focus_panel(1)

    def action_focus_previous(self) -> None:
        self.focus_panel(-1)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Esconde do rodapé o que não vale onde se está (`s` só no Ao vivo, `d` só no Panorama)."""
        tab_actions = {"switch_tab", "tab", "filter_agent", "cycle_status", "toggle_tests", "cycle_period"}
        if action not in tab_actions:
            return True
        if self.screen is not self.main:
            return False
        if action == "cycle_status":
            return self.active_tab() == "runs-pane"
        if action == "cycle_period":
            return self.active_tab() == "overview-pane"
        return True

    @on(TabbedContent.TabActivated)
    def tab_activated(self) -> None:
        # Ao trocar de aba, o foco vai para os dados dela, a não ser que a troca tenha
        # vindo com destino (o `/` troca para o Ao vivo e quer o filtro).
        self.call_after_refresh(self.settle_focus)
        self.refresh_bindings()

    def settle_focus(self) -> None:
        target, self.pending_focus = self.pending_focus, None
        if target is not None:
            target.focus()
            return
        pane = self.main.query_one(f"#{self.active_tab()}")
        if self.focused is None or pane not in self.focused.ancestors_with_self:
            self.focus_panel()

    def action_tab(self, pane: str) -> None:
        # Solta o foco da aba que sai antes de trocar. Um widget focado numa aba escondida
        # ainda pode emitir um `DescendantFocus` atrasado, e o TabbedContent responde a ele
        # ativando a aba do widget: a troca era desfeita logo depois de acontecer.
        if self.focused is not None and self.screen is self.main:
            target = self.main.query_one(f"#{pane}")
            if target not in self.focused.ancestors_with_self:
                self.set_focus(None)
        self.main.query_one(TabbedContent).active = pane

    def action_switch_tab(self, step: int) -> None:
        current = self.active_tab() or self.TABS[0]
        self.action_tab(self.TABS[(self.TABS.index(current) + step) % len(self.TABS)])

    def action_filter_agent(self) -> None:
        field = self.main.query_one("#agent", Input)
        if self.active_tab() == "runs-pane":
            field.focus()
        else:
            self.pending_focus = field
            self.action_tab("runs-pane")

    def action_leave_input(self) -> None:
        if isinstance(self.focused, Input):
            self.focus_panel()

    def action_cycle_status(self) -> None:
        select = self.main.query_one("#status", Select)
        options = [Select.NULL, "success", "error"]
        current = select.value if select.value in options else Select.NULL
        select.value = options[(options.index(current) + 1) % len(options)]

    def action_toggle_tests(self) -> None:
        box = "#no-tests" if self.active_tab() == "runs-pane" else "#with-tests"
        self.main.query_one(box, Checkbox).toggle()

    def action_cycle_period(self) -> None:
        select = self.main.query_one("#period", Select)
        days = [value for _, value in PERIODS]
        current = select.value if select.value in days else 7
        select.value = days[(days.index(current) + 1) % len(days)]

    def action_move(self, step: int) -> None:
        """`j`/`k`, como no vim, nas tabelas e na árvore."""
        if isinstance(self.focused, DataTable | Tree):
            self.focused.action_cursor_down() if step > 0 else self.focused.action_cursor_up()

    def action_toggle_help(self) -> None:
        if self.screen.query(HelpPanel):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()

    def action_open_repo(self) -> None:
        self.open_url(REPO_URL)

    def action_refresh(self) -> None:
        self.main.query_one(RunsPane).refresh_runs()
        self.main.query_one(OverviewPane).load()

    def action_pause(self) -> None:
        self.paused = not self.paused
        self.sync_masthead()
        self.main.query_one(RunsPane).refresh_runs()

    def action_toggle_animations(self) -> None:
        self.animate = not self.animate
        self.sync_masthead()
