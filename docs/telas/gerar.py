"""Gera as capturas do `kuro dash` em docs/telas/ (PNG), sem serviço no ar.

O painel roda com o Pilot do Textual contra um cliente falso com uma loja de demonstração
(`kuro.minhaloja.com.br`), o Textual exporta cada tela em SVG e um Chrome ou Edge sem janela
converte para PNG. Os dados incluem entradas longas de propósito:
um run antigo cuja mensagem é o contexto inteiro do sistema chamador (milhares de
caracteres) e runs com entrada estruturada, em que a coluna e a caixa mostram só a fala.

    uv run --extra tui python docs/telas/gerar.py            # todas
    uv run --extra tui python docs/telas/gerar.py trace      # só uma (ao-vivo, panorama, trace, ajuda, sessao, contexto)
    uv run --extra tui python docs/telas/gerar.py --svg      # deixa os SVGs ao lado, sem converter

O navegador é procurado em `KURO_CHROME`, no PATH (chrome, chromium, msedge) e nos caminhos
padrão do Windows e do macOS. O SVG puxa a fonte Fira Code de um CDN: sem rede, o PNG sai com
a fonte monoespaçada do sistema.
"""

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from agent_service.tui.app import KuroDash, RunScreen

HERE = Path(__file__).resolve().parent
SIZE = (150, 42)  # colunas × linhas: ~1850 px de largura no PNG, como as capturas antigas
NOW = datetime(2026, 10, 30, 10, 58, 12)


def at(minutes_ago: float) -> str:
    """Horário local (o painel mostra no fuso do terminal), em ISO 8601 com o fuso."""
    return (NOW - timedelta(minutes=minutes_ago)).astimezone().isoformat()


CONTEXTO_LONGO = (
    "[SISTEMA] Você é o atendimento da Minha Loja. Responda em português, com no máximo três frases.\n"
    "[FICHA] cliente Ana Souza · cliente desde 2021 · 14 pedidos · plano Prime\n"
    "[PEDIDOS] "
    + " | ".join(f"#{10400 + i} entregue em {(i % 27) + 1:02d}/09" for i in range(60))
    + "\n[HISTÓRICO]\n"
    + "\n".join(
        f"contato: mensagem {i} sobre o pedido anterior\natendente: resposta {i} com o prazo combinado"
        for i in range(40)
    )
    + "\ncontato: meu pedido não chegou, o rastreio é br123456789bz"
)


def run(i: int, agent: str, version: int, message: str, *, speech: str | None = None, **extra) -> dict:
    tokens = extra.pop("tokens", 1200)
    base = {
        "run_id": f"7f3a{i:04x}",
        "agent_type": agent,
        "agent_version": version,
        "status": "success",
        "started_at": at(3 * i),
        "latency_ms": extra.pop("latency", 1500),
        "total_tokens": tokens,
        "cost_usd": round(tokens * 1.85e-6, 4),
        "message": message,
        "input_message": speech,
        "output": extra.pop("output", "Pronto."),
        "session_id": "s0",
        "tool_failures": 0,
        "metadata": {},
    }
    return {**base, **extra}


# A primeira linha é o run do trace: entrada estruturada, com o contexto longo fora da fala.
RUNS = [
    run(
        0,
        "atendimento",
        4,
        CONTEXTO_LONGO,
        speech="meu pedido não chegou, o rastreio é br123456789bz",
        tokens=1363,
        tool_failures=1,
        output="Encontrei seu pedido #10432: saiu para entrega hoje às 8h e chega até as 18h. "
        "Quer que eu avise quando for entregue?",
    ),
    run(
        1,
        "triagem-chamados",
        2,
        "Assunto: produto com defeito",
        status="error",
        latency=30000,
        tokens=1508,
        status_message="tempo esgotado (timeout_seconds=30)",
    ),
    # Run antigo: sem entrada estruturada, a coluna mostra o começo da mensagem inteira.
    run(
        2,
        "atendimento",
        4,
        CONTEXTO_LONGO.replace("meu pedido não chegou", "o cupom PRIMEIRA10 não funciona"),
        tokens=798,
    ),
    run(3, "extrator-nota-fiscal", 3, "nf-48213.pdf", tokens=8194, metadata={"dry_run": "true"}),
    run(4, "atendimento", 4, CONTEXTO_LONGO, speech="cadê a nota fiscal?", tokens=1448, latency=3300),
    run(5, "devolucao", 5, "devolver a jaqueta, ficou grande", tokens=818, latency=3000),
    run(6, "atendimento", 4, CONTEXTO_LONGO, speech="meu pedido não chegou", tokens=1139),
    run(7, "triagem-chamados", 2, "Assunto: produto com defeito", tokens=876, latency=2700),
    run(8, "atendimento", 4, CONTEXTO_LONGO, speech="o cupom PRIMEIRA10 não funciona", tokens=1556),
    run(9, "atendimento", 4, CONTEXTO_LONGO, speech="posso pagar no pix?", tokens=1192),
    run(10, "extrator-nota-fiscal", 3, "nf-48214.pdf", tokens=8257, latency=2600),
    run(11, "devolucao", 5, "devolver a jaqueta, ficou grande", tokens=821, latency=3200),
    run(12, "atendimento", 4, CONTEXTO_LONGO, speech="meu pedido não chegou", tokens=953, latency=1800),
    run(13, "triagem-chamados", 2, "Assunto: produto com defeito", tokens=1991, latency=3500),
    run(14, "atendimento", 4, CONTEXTO_LONGO, speech="o cupom PRIMEIRA10 não funciona", tokens=1893),
]
TRACE_RUN = RUNS[0]


def span(sid: str, parent: str | None, kind: str, name: str, start: int, ms: int, **extra) -> dict:
    return {
        "id": sid,
        "parent_id": parent,
        "type": kind,
        "name": name,
        "started_at": f"2026-10-30T10:58:{start:02d}",
        "level": "DEFAULT",
        "latency_ms": ms,
        **extra,
    }


TRACE = {
    "run": TRACE_RUN,
    "input_events": [
        {"role": "human_agent", "content": "Bom dia! Vou te passar para o assistente.", "at": at(1)},
        {"role": "contact", "content": TRACE_RUN["input_message"], "at": at(0), "speech": True},
    ],
    "input_context": [
        {
            "key": "instrucoes_loja",
            "title": "Regras da loja",
            "volatile": False,
            "hash": "9c1e4f" + "0" * 58,
            "content": "Responda em português, com no máximo três frases. Nunca prometa reembolso sem abrir chamado.",
        },
        {
            "key": "ficha",
            "title": "Ficha do cliente",
            "volatile": False,
            "hash": "41ab07" + "0" * 58,
            "content": "Ana Souza · cliente desde 2021 · 14 pedidos · plano Prime\n"
            "Endereço de entrega: Rua das Acácias, 120 · Goiânia/GO",
        },
        {
            "key": "pedidos_abertos",
            "title": "Pedidos em aberto",
            "volatile": True,
            "hash": "e07d3a" + "0" * 58,
            "content": "#10432 · tênis de corrida · pago 28/10 · transportadora Rápido Log · rastreio br123456789bz",
        },
    ],
    "spans": [
        span("root", None, "SPAN", "atendimento", 0, 3400),
        span(
            "g1",
            "root",
            "GENERATION",
            "gemini-2.5-flash",
            1,
            820,
            input=CONTEXTO_LONGO,
            output={"tool_call": "rastrear-pedido", "args": {"codigo": "br123456789bz"}},
        ),
        span(
            "t1",
            "root",
            "TOOL",
            "rastrear-pedido",
            2,
            310,
            level="ERROR",
            status_message="HTTP 404: código de rastreio não encontrado",
            input={"codigo": "br123456789bz"},
        ),
        span(
            "g2",
            "root",
            "GENERATION",
            "gemini-2.5-flash",
            3,
            640,
            input="(o mesmo contexto + o erro da tool)",
            output={"tool_call": "rastrear-pedido", "args": {"codigo": "BR123456789BZ"}},
        ),
        span(
            "t2",
            "root",
            "TOOL",
            "rastrear-pedido",
            4,
            280,
            input={"codigo": "BR123456789BZ"},
            output={"status": "saiu para entrega", "previsao": "hoje até 18h"},
        ),
        span(
            "g3",
            "root",
            "GENERATION",
            "gemini-2.5-flash",
            5,
            1100,
            input="(o contexto + o rastreio)",
            output=TRACE_RUN["output"],
        ),
    ],
    "scores": [{"name": "feedback", "value": 1, "comment": "resolveu na primeira mensagem"}],
}

TIMELINE = {
    "session_id": "s0",
    "agent_types": ["atendimento"],
    "turns": 3,
    "total_tokens": 3250,
    "cost_usd": 0.0061,
    "items": [
        {
            "run_id": "6e21a0c4",
            "at": at(42),
            "agent_type": "atendimento",
            "agent_version": 3,
            "structured": False,
            "raw_input": CONTEXTO_LONGO.replace(
                "meu pedido não chegou, o rastreio é br123456789bz", "oi, comprei um tênis"
            ),
            "steps": [],
            "output": "Oi, Ana! Como posso ajudar com o seu pedido?",
            "usage": {"input_tokens": 980, "output_tokens": 18, "cost_usd": 0.0018, "latency_ms": 900},
        },
        {
            "run_id": "6e21a0c9",
            "at": at(20),
            "agent_type": "atendimento",
            "agent_version": 4,
            "structured": True,
            "events": [{"role": "contact", "content": "quando chega?", "speech": True}],
            "message": "quando chega?",
            "context": [{"key": "pedidos_abertos", "title": "Pedidos em aberto", "changed": True}],
            "steps": [
                {
                    "tool": "consultar-pedido",
                    "args": {"pedido": 10432},
                    "ok": True,
                    "result_preview": '{"status": "em transporte", "previsao": "30/10"}',
                }
            ],
            "output": "O pedido #10432 está em transporte, com previsão para hoje.",
            "usage": {"input_tokens": 1020, "output_tokens": 22, "cost_usd": 0.0019, "latency_ms": 1300},
        },
        {
            "run_id": TRACE_RUN["run_id"],
            "at": TRACE_RUN["started_at"],
            "agent_type": "atendimento",
            "agent_version": 4,
            "structured": True,
            "events": TRACE["input_events"],
            "message": TRACE_RUN["input_message"],
            "context": [{"key": "pedidos_abertos", "title": "Pedidos em aberto", "changed": True}],
            "steps": [
                {
                    "tool": "rastrear-pedido",
                    "args": {"codigo": "br123456789bz"},
                    "ok": False,
                    "failure": "http_4xx",
                    "error": "HTTP 404: código de rastreio não encontrado",
                },
                {
                    "tool": "rastrear-pedido",
                    "args": {"codigo": "BR123456789BZ"},
                    "ok": True,
                    "result_preview": '{"status": "saiu para entrega", "previsao": "hoje até 18h"}',
                },
            ],
            "output": TRACE_RUN["output"],
            "usage": {"input_tokens": 1290, "output_tokens": 73, "cost_usd": 0.0025, "latency_ms": 1500},
        },
    ],
}


def _bucket_series(n: int, seed: int) -> list[int]:
    return [5 + ((i * 7 + seed) % 6) for i in range(n)]


OVERVIEW = {
    "totals": {
        "runs": 1284,
        "errors": 9,
        "tool_failure_runs": 21,
        "total_tokens": 2_300_000,
        "cost_usd": 4.16,
        "p95_latency_ms": 4100,
        "feedback_up": 87,
        "feedback_down": 6,
    },
    "previous": {
        "runs": 1097,
        "errors": 14,
        "tool_failure_runs": 33,
        "total_tokens": 2_035_000,
        "cost_usd": 3.71,
        "p95_latency_ms": 4820,
    },
    "buckets": [
        {
            "runs": r * 9,
            "errors": (i % 5 == 1),
            "tool_failure_runs": (i % 3),
            "total_tokens": r * 16000,
            "cost_usd": r * 0.03,
            "p95_latency_ms": 3800 + r * 40,
        }
        for i, r in enumerate(_bucket_series(28, 3))
    ],
    "agents": [
        {
            "agent_type": name,
            "last_run_at": at(0),
            "trend": _bucket_series(16, seed),
            "totals": {
                "runs": runs,
                "errors": errors,
                "tool_failure_runs": tools,
                "total_tokens": tokens,
                "cost_usd": cost,
                "p95_latency_ms": p95,
                "feedback_up": up,
                "feedback_down": down,
            },
        }
        for name, seed, runs, errors, tools, tokens, cost, p95, up, down in (
            ("atendimento", 1, 812, 4, 12, 1_300_000, 2.36, 3900, 71, 4),
            ("triagem-chamados", 2, 301, 3, 0, 240_000, 0.43, 1800, 9, 1),
            ("extrator-nota-fiscal", 4, 118, 1, 0, 690_000, 1.24, 6200, 0, 0),
            ("devolucao", 5, 53, 1, 9, 70_000, 0.13, 4400, 7, 1),
        )
    ],
    "tool_failures": [
        {
            "tool_name": "rastrear-pedido",
            "failure": "http_4xx",
            "count": 12,
            "agent_types": ["atendimento"],
            "http_status": [404],
            "last_at": at(0),
        },
        {
            "tool_name": "abrir-devolucao",
            "failure": "timeout",
            "count": 9,
            "agent_types": ["devolucao"],
            "http_status": [],
            "last_at": at(158),
        },
    ],
}


class DemoClient:
    base_url = "https://kuro.minhaloja.com.br"

    def health(self):
        return {"version": "0.4.0"}

    def list_runs(self, **params):
        return {"items": RUNS}

    def run_trace(self, run_id):
        return TRACE

    def session_timeline(self, session_id, user_id=None, **_):
        return TIMELINE

    def overview(self, **params):
        return OVERVIEW


async def settle(pilot, app) -> None:
    for _ in range(3):
        await pilot.pause()
        await app.workers.wait_for_complete()
    await pilot.pause(0.3)


async def capture(name: str) -> str:
    app = KuroDash(DemoClient(), interval=600)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        if name == "panorama":
            await pilot.press("2")
        elif name == "ajuda":
            await pilot.press("question_mark")
        elif name in ("trace", "sessao", "contexto"):
            app.push_screen(RunScreen(app.client, TRACE_RUN["run_id"]))
            await settle(pilot, app)
            if name == "sessao":
                await pilot.press("s")
            elif name == "contexto":
                await pilot.press("c")
        await settle(pilot, app)
        app.updated_at = "12:44:28"  # hora fixa: gerar de novo não muda a imagem à toa
        app.sync_masthead()
        await pilot.pause()
        return app.export_screenshot(title="kuro dash")


def browser() -> str | None:
    candidates = [os.environ.get("KURO_CHROME")]
    candidates += [shutil.which(name) for name in ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge")]
    candidates += [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    return next((c for c in candidates if c and Path(c).exists()), None)


def to_png(svg: str, target: Path, exe: str) -> None:
    width, height = (float(v) for v in re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg).groups())
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "tela.html"
        # O SVG numa página sem margem, do tamanho exato do viewBox.
        page.write_text(
            "<!doctype html><meta charset='utf-8'><style>html,body{margin:0;background:#0a0a0c}"
            f"svg{{display:block;width:{width}px;height:{height}px}}</style>{svg}",
            encoding="utf-8",
        )
        subprocess.run(
            [
                exe,
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
                f"--user-data-dir={Path(tmp) / 'perfil'}",
                "--virtual-time-budget=5000",
                f"--window-size={int(width + 0.5)},{int(height + 0.5)}",
                f"--screenshot={target.resolve()}",
                page.as_uri(),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )


NAMES = ("ao-vivo", "panorama", "trace", "ajuda", "sessao", "contexto")


def main(argv: list[str]) -> int:
    keep_svg = "--svg" in argv
    names = [a for a in argv if not a.startswith("--")] or list(NAMES)
    unknown = set(names) - set(NAMES)
    if unknown:
        print(f"tela desconhecida: {', '.join(sorted(unknown))} (use {', '.join(NAMES)})", file=sys.stderr)
        return 2
    exe = None if keep_svg else browser()
    if not keep_svg and exe is None:
        print("sem Chrome/Edge para converter: defina KURO_CHROME ou rode com --svg", file=sys.stderr)
        return 2
    for name in names:
        svg = asyncio.run(capture(name))
        if keep_svg:
            (HERE / f"tui-{name}.svg").write_text(svg, encoding="utf-8")
        else:
            to_png(svg, HERE / f"tui-{name}.png", exe)
        print(f"tui-{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
