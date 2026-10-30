"""TUI `kuro dash` dirigida pelo Pilot do Textual, contra um cliente falso."""

import re
from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable, Static, Tree  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from agent_service.cli import main as cli_main  # noqa: E402
from agent_service.cli.client import ApiError, ServiceUnavailable  # noqa: E402
from agent_service.tui import crow  # noqa: E402
from agent_service.tui.app import KuroDash, RunScreen, spark  # noqa: E402

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


RUN = {
    "run_id": "r1",
    "agent_type": "suporte",
    "agent_version": 3,
    "status": "success",
    "started_at": "2026-10-03T12:30:00Z",
    "latency_ms": 1500,
    "total_tokens": 120,
    "cost_usd": 0.0012,
    "message": "meu wifi caiu",
    "output": "Vamos ver.",
    "session_id": "s1",
}

TRACE = {
    "run": RUN,
    "spans": [
        {"id": "a", "parent_id": None, "type": "GENERATION", "name": "gemini", "started_at": "1", "level": "DEFAULT",
         "latency_ms": 900, "input": "x", "output": "y"},
        {"id": "b", "parent_id": "a", "type": "TOOL", "name": "ficha", "started_at": "2", "level": "ERROR",
         "status_message": "HTTP 500", "input": {"cpf": "1"}, "output": None},
    ],
    "scores": [{"name": "eval", "value": 1, "comment": "ok"}],
}

OVERVIEW = {
    "totals": {"runs": 10, "errors": 1, "tool_failure_runs": 2, "sessions": 4, "total_tokens": 5000,
               "cost_usd": 0.5, "feedback_up": 3, "feedback_down": 1},
    "previous": {"runs": 5, "errors": 0, "tool_failure_runs": 2, "total_tokens": 2500, "cost_usd": 0.25},
    "agents": [{"agent_type": "suporte", "last_run_at": "2026-10-03T12:30:00Z",
                "totals": {"runs": 10, "errors": 1, "tool_failure_runs": 2, "total_tokens": 5000, "cost_usd": 0.5,
                           "feedback_up": 3, "feedback_down": 1}}],
    "tool_failures": [{"tool_name": "ficha", "failure": "http_5xx", "count": 2, "agent_types": ["suporte"],
                       "http_status": [500], "last_at": "2026-10-03T12:30:00Z"}],
}


class FakeClient:
    base_url = "http://kuro.test"

    def __init__(self, *, runs=None, overview=None, health=None):
        self.runs = runs
        self.health_result = health
        self.overview_result = overview
        self.run_queries: list[dict] = []

    def health(self):
        if isinstance(self.health_result, Exception):
            raise self.health_result
        return {"version": "0.2.0"}

    def list_runs(self, **params):
        self.run_queries.append(params)
        if isinstance(self.runs, Exception):
            raise self.runs
        return {"items": self.runs if self.runs is not None else [RUN]}

    def run_trace(self, run_id):
        return TRACE

    def overview(self, **params):
        if isinstance(self.overview_result, Exception):
            raise self.overview_result
        return self.overview_result or OVERVIEW


def _text(widget: Static) -> str:
    return str(widget.render())


async def test_runs_table_and_trace_screen():
    app = KuroDash(FakeClient(), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = app.query_one("#runs", DataTable)
        assert table.row_count == 1
        assert "meu wifi caiu" in str(table.get_row_at(0))
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, RunScreen)
        await app.workers.wait_for_complete()
        await pilot.pause()
        tree = app.screen.query_one(Tree)
        labels = [str(node.label) for node in tree.root.children]
        assert any("gemini" in label for label in labels)
        tool = next(n for n in tree.root.children[0].children if "ficha" in str(n.label))
        assert tool.is_expanded and tool.parent.is_expanded  # o span com erro fica à vista
        assert any("HTTP 500" in str(n.label) for n in tool.children)
        assert "eval = 1" in _text(app.screen.query_one("#scores", Static))
        await pilot.press("escape")
        assert not isinstance(app.screen, RunScreen)


async def test_agent_filter_is_sent():
    client = FakeClient()
    app = KuroDash(client, interval=60, agent="r8")
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        assert client.run_queries[0]["agent_type"] == "r8"
        app.query_one("#agent").value = "suporte"
        app.query_one("#agent").focus()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert client.run_queries[-1]["agent_type"] == "suporte"


async def test_service_down_shows_message_instead_of_crashing():
    app = KuroDash(FakeClient(runs=ServiceUnavailable("não consegui falar com http://kuro.test")), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "não consegui falar" in _text(app.query_one("#runs-status", Static))


async def test_overview_renders_totals_agents_and_failures():
    app = KuroDash(FakeClient(), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("2")
        await app.workers.wait_for_complete()
        await pilot.pause()
        runs = _text(app.query_one("#card-runs", Static))
        assert "10" in runs and "+100%" in runs
        assert "10.0%" in _text(app.query_one("#card-errors", Static))
        assert app.query_one("#agents", DataTable).row_count == 1
        assert app.query_one("#tool-failures", DataTable).row_count == 1
        assert not app.query_one("#failures-empty").display


async def test_overview_without_admin_scope_explains():
    app = KuroDash(FakeClient(overview=ApiError(403, "escopo insuficiente")), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("2")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "escopo admin" in _text(app.query_one("#overview-msg", Static))
        assert not app.query_one("#cards").display
        assert app.crow_mood() == "idle"  # configuração faltando não é acontecimento: o corvo não alarma


def test_dash_without_tty_fails_with_usage_error():
    result = CliRunner().invoke(cli_main.app, ["dash"])
    assert result.exit_code == 2
    assert "TTY" in result.output


# -- o corvo ------------------------------------------------------------------------


def test_every_mood_renders_six_rows_of_sixteen_cells():
    for mood in crow.MOODS:
        for animate in (True, False):
            lines = crow.render(crow.frame_for(mood, 1, animate)).plain.splitlines()
            assert len(lines) == 6 and all(len(line) == 16 for line in lines), mood


def test_offline_crow_is_upside_down_with_a_cross_eye():
    frame = crow.frame_for("offline", 0, True)
    assert frame.grid == tuple(reversed(crow.BASE))
    assert "×" in crow.render(frame).plain
    assert "×" not in crow.render(crow.frame_for("idle", 0, True)).plain


def test_console_and_favicon_draw_the_same_crow_as_the_dash():
    frontend = Path(__file__).resolve().parents[1] / "frontend" / "src"
    colors = {**crow.COLORS, "E": crow.EYE_OK}
    del colors["R"]  # a boca só aparece no corvo de erro, que o console não desenha

    source = (frontend / "components" / "brand" / "kuro-crow.tsx").read_text(encoding="utf-8")
    grid = re.search(r"const GRID = \[(.*?)\];", source, re.S).group(1)
    assert tuple(re.findall(r'"([.A-Z]{16})"', grid)) == crow.BASE
    assert dict(re.findall(r'(\w): "(#[0-9a-f]{6})"', source)) == colors

    # icon.svg: o sprite deslocado 2 para a direita e 4 para baixo num ladrilho 20×20.
    pixels = [["."] * 16 for _ in crow.BASE]
    by_color = {color: key for key, color in colors.items()}
    svg = (frontend / "app" / "icon.svg").read_text(encoding="utf-8")
    for x, y, width, fill in re.findall(r'<rect x="(\d+)" y="(\d+)" width="(\d+)" height="1" fill="(#\w+)"', svg):
        for dx in range(int(width)):
            pixels[int(y) - 4][int(x) - 2 + dx] = by_color[fill]
    assert tuple("".join(row) for row in pixels) == crow.BASE


def test_error_crow_opens_the_beak_and_paused_crow_snores():
    error = crow.frame_for("error", 0, False)
    assert error.eye == crow.EYE_ALERT and "R" in "".join(error.grid)
    paused = crow.frame_for("paused", 0, False)
    assert paused.eye is None and "zZ" in crow.render(paused).plain


def test_idle_crow_only_acts_after_a_rest():
    widget = crow.Crow(lambda: "idle", lambda: True)
    frames = [widget.next_frame() for _ in range(60)]
    assert frames[0] == crow.Frame(crow.BASE)
    assert any(f != crow.Frame(crow.BASE) for f in frames)  # pisca, olha, bica ou pula


def test_spark_fits_width():
    assert spark([0, 0, 0]) == "▁▁▁"
    assert len(spark(list(range(30)), width=10)) == 10
    assert spark([1, 8])[-1] == "█"


async def test_new_error_run_alarms_but_baseline_and_filter_change_do_not():
    ok = dict(RUN, run_id="r0")
    client = FakeClient(runs=[ok])
    app = KuroDash(client, interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.crow_mood() == "idle"  # a primeira consulta só forma a base
        client.runs = [dict(RUN, run_id="r9", status="error"), ok]
        app.query_one("#status").value = "error"  # trocar o filtro também não é "chegou execução"
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.crow_mood() == "idle"
        client.runs = [dict(RUN, run_id="r10", status="error"), dict(RUN, run_id="r9", status="error"), ok]
        await pilot.press("r")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.alert_until > 0
        assert "▸" in str(app.query_one("#runs", DataTable).get_row_at(0)[1])


async def test_new_run_makes_crow_work():
    client = FakeClient(runs=[RUN])
    app = KuroDash(client, interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        client.runs = [dict(RUN, run_id="r2"), RUN]
        await pilot.press("r")
        await app.workers.wait_for_complete()
        await pilot.pause()
        # O estado, não o relógio: a janela de 2 s pode passar numa máquina carregada.
        assert app.busy_until > 0 and app.alert_until == 0


async def test_service_down_makes_crow_offline():
    app = KuroDash(FakeClient(health=ServiceUnavailable("fora"), runs=ServiceUnavailable("fora")), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.crow_mood() == "offline"
        assert "offline" in _text(app.query_one(".conn", Static))


async def test_pause_and_animation_toggle():
    app = KuroDash(FakeClient(), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("p")
        await pilot.pause()
        assert app.crow_mood() == "paused"
        assert "pausado" in _text(app.query_one(".conn", Static))
        assert app.animate
        await pilot.press("a")
        assert not app.animate
        assert "desligadas" in _text(app.query_one(".anim-toggle", Static))
        await pilot.click(".anim-toggle")
        assert app.animate


async def test_small_terminal_still_shows_runs():
    app = KuroDash(FakeClient(), interval=60)
    async with app.run_test(size=(80, 24)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = app.query_one("#runs", DataTable)
        assert table.row_count == 1 and table.size.height >= 3


async def test_empty_feed_explains():
    app = KuroDash(FakeClient(runs=[]), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one("#runs-empty").display
        assert "Nenhuma execução ainda" in _text(app.query_one("#runs-empty", Static))


async def test_late_result_from_before_a_filter_change_is_dropped():
    app = KuroDash(FakeClient(runs=[RUN]), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        pane = app.query_one("#runs-pane")
        stale = pane.generation
        app.query_one("#status").value = "error"
        await pilot.pause()
        pane.show([dict(RUN, run_id="velha", status="error")], stale)  # a consulta lenta de antes do filtro
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.crow_mood() == "idle"
        assert "velha" not in (pane.seen or set())


async def test_persistent_api_error_alarms_once():
    app = KuroDash(FakeClient(runs=ApiError(401, "chave inválida")), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        first = app.alert_until
        assert first and "erro na API" in _text(app.query_one(".conn", Static))
        await pilot.press("r")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.alert_until == first  # o mesmo erro de novo não rearma o alerta


async def test_textual_animations_none_starts_still(monkeypatch):
    import textual.constants

    monkeypatch.setattr(textual.constants, "TEXTUAL_ANIMATIONS", "none")
    assert not KuroDash(FakeClient()).animate


# -- teclado ------------------------------------------------------------------------


async def _focus_on(pilot, app, widget_id: str, tab: str | None = None) -> None:
    """Espera o foco chegar ao widget. Trocar de aba move o foco depois da próxima
    renderização (`call_after_refresh`); com a máquina carregada, um `pause` só não basta."""
    for _ in range(40):
        if getattr(app.focused, "id", None) == widget_id and (tab is None or app.active_tab() == tab):
            return
        await pilot.pause(0.05)
    assert getattr(app.focused, "id", None) == widget_id, f"foco em {app.focused!r}, esperado #{widget_id}"
    assert tab is None or app.active_tab() == tab


async def test_tab_cycles_data_panels_and_brackets_switch_tabs():
    app = KuroDash(FakeClient(), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await _focus_on(pilot, app, "runs")
        await pilot.press("tab")  # só um painel no Ao vivo: o foco fica na tabela
        await _focus_on(pilot, app, "runs")
        await pilot.press("right_square_bracket")
        await app.workers.wait_for_complete()
        await _focus_on(pilot, app, "agents", tab="overview-pane")
        await pilot.press("tab")
        await _focus_on(pilot, app, "tool-failures")
        await pilot.press("shift+tab")
        await _focus_on(pilot, app, "agents")
        await pilot.press("left_square_bracket")
        await _focus_on(pilot, app, "runs", tab="runs-pane")


async def test_filters_from_the_keyboard():
    client = FakeClient()
    app = KuroDash(client, interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await _focus_on(pilot, app, "runs")
        await pilot.press("slash")
        await _focus_on(pilot, app, "agent")
        await pilot.press("s", "u", "p")  # dentro do filtro, letras são texto, não atalhos
        assert app.query_one("#agent").value == "sup"
        await pilot.press("escape")
        await _focus_on(pilot, app, "runs")
        await pilot.press("s")
        await app.workers.wait_for_complete()
        assert client.run_queries[-1]["status"] == "success"
        await pilot.press("t")
        await app.workers.wait_for_complete()
        assert client.run_queries[-1]["include_dry_run"] is False
        await pilot.press("s", "s")  # sucesso → erro → todos
        await app.workers.wait_for_complete()
        assert client.run_queries[-1]["status"] is None


async def test_contextual_keys_and_help():
    app = KuroDash(FakeClient(), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        assert app.check_action("cycle_status", ()) and not app.check_action("cycle_period", ())
        await pilot.press("2")
        await pilot.pause()
        assert app.check_action("cycle_period", ()) and not app.check_action("cycle_status", ())
        await pilot.press("d")
        assert app.query_one("#period").value == 30
        await pilot.press("question_mark")
        await pilot.pause()
        assert app.screen.query("HelpPanel")
        await pilot.press("question_mark")
        await pilot.pause()
        assert not app.screen.query("HelpPanel")


async def test_repo_link_in_header_and_g_opens_it(monkeypatch):
    opened = []
    app = KuroDash(FakeClient(), interval=60)
    monkeypatch.setattr(app, "open_url", lambda url, **_: opened.append(url))
    async with app.run_test(size=(140, 40)) as pilot:
        assert "Arthur-Marques-IA/kuro-agent-platform" in _text(app.query_one(".repo", Static))
        await pilot.press("g")
        assert opened == ["https://github.com/Arthur-Marques-IA/kuro-agent-platform"]


async def test_j_k_move_and_trace_keys_hidden():
    client = FakeClient(runs=[RUN, dict(RUN, run_id="r2")])
    app = KuroDash(client, interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = app.query_one("#runs", DataTable)
        await pilot.press("j")
        assert table.cursor_row == 1
        await pilot.press("k")
        assert table.cursor_row == 0
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, RunScreen)
        assert not app.check_action("switch_tab", ())  # no trace, as teclas das abas somem do rodapé


async def test_slash_from_overview_goes_to_the_filter():
    app = KuroDash(FakeClient(), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("2")
        await _focus_on(pilot, app, "agents", tab="overview-pane")
        await pilot.press("slash")
        await _focus_on(pilot, app, "agent", tab="runs-pane")


# -- cores ----------------------------------------------------------------------------


def test_truecolor_is_turned_on_over_ssh(monkeypatch):
    """Pelo SSH chega só o TERM: sem o COLORTERM, o Rich reduziria o tema a 256 cores."""
    import rich.console

    from agent_service.tui.app import prefer_truecolor

    env = {"TERM": "xterm-256color"}
    assert prefer_truecolor(env) and env["COLORTERM"] == "truecolor"
    monkeypatch.setattr(rich.console, "WINDOWS", False)  # no Windows o Rich nem olha o ambiente
    console = rich.console.Console(force_terminal=True, _environ=env)
    assert console.color_system == "truecolor"


def test_truecolor_respects_who_said_otherwise():
    from agent_service.tui.app import prefer_truecolor

    for env in (
        {"TERM": "xterm-256color", "COLORTERM": "256"},  # já definido: não mexe
        {"TERM": "xterm-256color", "NO_COLOR": "1"},
        {"TERM": "xterm-256color", "KURO_TRUECOLOR": "0"},
        {"TERM": "linux"},  # console do Linux, sem X
        {"TERM": "dumb"},
        {"TERM": "xterm-256color", "TERM_PROGRAM": "Apple_Terminal"},
    ):
        before = dict(env)
        assert not prefer_truecolor(env)
        assert env == before
