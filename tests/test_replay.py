"""Replay de conversa (`kuro sessions replay`, tool `session_replay`) contra uma API falsa.

O que importa aqui é o que o replay manda: as falas na ordem, numa sessão nova por rodada,
com as dependencies da conversa original e sempre em dry_run — e como ele resume a decisão
de cada turno para comparar o draft com a produção."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from agent_service.cli import main as cli_main
from agent_service.cli import replay as replay_mod
from agent_service.cli.client import Client

ORIGINAL = "a1b2c3d4-r6v2"
TURNS = [
    {"message": "[SISTEMA] dados da oferta…\n[ALUNO] 12345678900", "acao": "responder", "output": "Olá, Ana…"},
    {"message": "[ALUNO] Vou conseguir pagar em novembro lá pelo dia 28", "acao": "efetivar_acordo", "output": "Acordo formalizado"},
]
DEPS = {"telefone": "5511900000000", "conversation_id": "a1b2c3d4"}


def _sse(*events):
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


def _trace(run_id, message, acao, output, deps):
    return {
        "run": {"run_id": run_id, "message": message, "output": output, "status_message": None},
        "spans": [
            {"id": f"{run_id}:root", "parent_id": None, "type": "SPAN", "name": "chat",
             "input": {"message": message, "dependencies": deps}},
            {"id": f"{run_id}:0", "parent_id": f"{run_id}:root", "type": "TOOL", "name": "regente_r6_decidir",
             "input": {"acao": acao, "acordo": {"qtd_parcelas": 2}}, "status_message": None},
        ],
    }


class FakeKuro:
    """A API que o replay usa: runs por sessão, traces, agentes e o /chat/stream."""

    def __init__(self):
        self.chats: list[dict] = []
        self.kinds = {"r6": "conversational", "r6-draft": "conversational", "extrator": "analysis"}
        # O draft sempre responde; a produção efetiva no 2º turno da 1ª rodada (o bug).
        self.prod_effective = iter([True, False, False, False])
        self.trace_misses = 0  # o trace de um run novo pode levar um instante para aparecer
        self.traces: dict[str, dict] = {
            f"orig-{i}": _trace(f"orig-{i}", t["message"], t["acao"], t["output"], DEPS) for i, t in enumerate(TURNS)
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        if path == "/observability/runs":
            if params.get("session_id") == ORIGINAL:
                items = [{"run_id": f"orig-{i}", "agent_type": "r6", "session_id": ORIGINAL, "user_id": "5511900000000",
                          "started_at": f"2026-10-30T21:3{i}:00Z"} for i in (1, 0)]  # fora de ordem de propósito
                return httpx.Response(200, json={"items": items, "next_cursor": None})
            if params.get("search") and params["search"] in ORIGINAL:
                # O replay anterior casa pela metadata (replay_of) e não deve ser sugerido.
                items = [{"session_id": "replay-abc123", "user_id": "replay"}, {"session_id": ORIGINAL, "user_id": "5511900000000"}]
                return httpx.Response(200, json={"items": items})
            return httpx.Response(200, json={"items": [], "next_cursor": None})
        if path.startswith("/observability/runs/") and path.endswith("/trace"):
            run_id = path.split("/")[3]
            if run_id.startswith("rp-") and self.trace_misses:
                self.trace_misses -= 1
                return httpx.Response(404, json={"detail": "ainda não gravado"})
            return httpx.Response(200, json=self.traces[run_id])
        if path.startswith("/agents/"):
            name = path.split("/")[2]
            return httpx.Response(200, json={"agent_type": name, "kind": self.kinds[name]})
        if path == "/chat/stream":
            body = json.loads(request.content)
            self.chats.append(body)
            run_id = f"rp-{len(self.chats)}"
            second = body["message"] == TURNS[1]["message"]
            acao = "efetivar_acordo" if body["agent_type"] == "r6" and second and next(self.prod_effective) else "responder"
            self.traces[run_id] = _trace(run_id, body["message"], acao, f"resposta {run_id}", body["dependencies"])
            return httpx.Response(
                200, content=_sse(("run", {"run_id": run_id}), ("message", {"content": f"resposta {run_id}"})),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(404, json={"detail": "não encontrado"})


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(replay_mod.time, "sleep", lambda s: None)
    return FakeKuro()


@pytest.fixture
def client(fake):
    return Client("http://kuro.test", transport=httpx.MockTransport(fake))


def test_reenvia_as_falas_em_ordem_numa_sessao_nova_por_rodada(fake, client):
    report = replay_mod.replay(client, ORIGINAL, agent_type="r6-draft", compare_with="r6", repeat=2)
    assert len(fake.chats) == 2 * 2 * 2  # turnos × rodadas × agentes
    for agent in ("r6-draft", "r6"):
        bodies = [c for c in fake.chats if c["agent_type"] == agent]
        sessions = [c["session_id"] for c in bodies]
        # Cada rodada é uma conversa nova, com os turnos na ordem original.
        assert sessions[0] == sessions[1] != sessions[2] == sessions[3]
        assert [c["message"] for c in bodies] == [TURNS[0]["message"], TURNS[1]["message"]] * 2
    for body in fake.chats:
        assert body["dry_run"] is True and body["dependencies"] == DEPS
        assert body["metadata"]["replay_of"] == ORIGINAL and body["session_id"].startswith("replay-")
    assert report["source_agent"] == "r6" and report["turns"] == 2
    assert [t["decision"] for t in report["original"]] == ["regente_r6_decidir", "regente_r6_decidir"]


def test_focus_separa_a_decisao_e_conta_por_turno(fake, client):
    report = replay_mod.replay(
        client, ORIGINAL, agent_type="r6-draft", compare_with="r6", repeat=2, focus=["regente_r6_decidir.acao"]
    )
    efetivou = '{"regente_r6_decidir.acao": "efetivar_acordo"}'
    respondeu = '{"regente_r6_decidir.acao": "responder"}'
    assert report["original"][1]["decision"] == efetivou
    draft, prod = report["agents"]["r6-draft"]["summary"], report["agents"]["r6"]["summary"]
    assert draft[1] == {"turn": 2, "original": efetivou, "decisions": {respondeu: 2}, "same_as_original": 0}
    assert prod[1]["decisions"] == {efetivou: 1, respondeu: 1} and prod[1]["same_as_original"] == 1
    # Cada run do replay fica referenciado, para abrir com run_show.
    assert all(t["run_id"].startswith("rp-") for r in report["agents"]["r6"]["rounds"] for t in r["turns"])


def test_decisao_com_caminho_aninhado_e_tool_ausente():
    tools = [{"name": "decidir", "args": {"acordo": {"qtd_parcelas": 2}}}, {"name": "anotar", "args": {}}]
    assert replay_mod.decision(tools, None) == "decidir → anotar"
    assert replay_mod.decision([], None) == "(nenhuma tool)"
    assert json.loads(replay_mod.decision(tools, ["decidir.acordo.qtd_parcelas", "transferir"])) == {
        "decidir.acordo.qtd_parcelas": 2, "transferir": None,
    }


def test_dependencies_sobrepostas_e_so_os_primeiros_turnos(fake, client):
    replay_mod.replay(client, ORIGINAL, turns=1, dependencies={"conversation_id": "teste-1"})
    assert len(fake.chats) == 1
    assert fake.chats[0]["dependencies"] == {**DEPS, "conversation_id": "teste-1"}
    assert fake.chats[0]["agent_type"] == "r6"  # sem agent_type, o próprio agente da conversa


def test_espera_o_trace_do_run_novo(fake, client):
    fake.trace_misses = 2
    report = replay_mod.replay(client, ORIGINAL, turns=1)
    assert report["agents"]["r6"]["rounds"][0]["turns"][0]["decision"] == "regente_r6_decidir"


def test_conversa_inexistente_sugere_a_parecida(fake, client):
    with pytest.raises(replay_mod.ReplayError, match=ORIGINAL) as caught:
        replay_mod.replay(client, "a1b2c3d4-r6")
    assert "replay-abc123" not in str(caught.value)


def test_teto_de_mensagens(fake, client, monkeypatch):
    monkeypatch.setattr(replay_mod, "MAX_CALLS", 7)
    with pytest.raises(replay_mod.ReplayError, match="8 mensagens"):
        replay_mod.replay(client, ORIGINAL, compare_with="r6-draft", repeat=2)  # 2 turnos × 2 × 2 agentes
    assert not fake.chats


def test_agente_analysis_vai_para_o_eval(fake, client):
    with pytest.raises(replay_mod.ReplayError, match="eval"):
        replay_mod.replay(client, ORIGINAL, agent_type="extrator")
    assert not fake.chats


def test_compare_com_o_mesmo_agente(fake, client):
    with pytest.raises(replay_mod.ReplayError, match="outro agente"):
        replay_mod.replay(client, ORIGINAL, compare_with="r6")


# -- a CLI ---------------------------------------------------------------------------


@pytest.fixture
def cli(fake, monkeypatch, tmp_path):
    monkeypatch.setenv("KURO_HOME", str(tmp_path))
    transport = httpx.MockTransport(fake)
    monkeypatch.setattr(
        cli_main, "Client", lambda url, timeout, api_key=None, **kw: Client(url, timeout, transport=transport, api_key=api_key, **kw)
    )
    return CliRunner()


def test_cli_replay_em_json(fake, cli):
    result = cli.invoke(cli_main.app, [
        "--json", "sessions", "replay", ORIGINAL, "-a", "r6-draft", "--compare", "r6", "-n", "2",
        "-f", "regente_r6_decidir.acao", "-d", "conversation_id=teste",
    ])
    assert result.exit_code == 0, result.stderr
    out = json.loads(result.stdout)
    assert set(out["agents"]) == {"r6-draft", "r6"}
    assert all(c["dependencies"]["conversation_id"] == "teste" for c in fake.chats)


def test_cli_replay_em_texto(fake, cli):
    result = cli.invoke(cli_main.app, ["sessions", "replay", ORIGINAL, "-f", "regente_r6_decidir.acao"])
    assert result.exit_code == 0, result.stderr
    assert "efetivar_acordo" in result.stdout and "r6" in result.stdout


def test_cli_replay_de_conversa_inexistente(fake, cli):
    result = cli.invoke(cli_main.app, ["--json", "sessions", "replay", "a1b2c3d4-r6"])
    assert result.exit_code == 2 and ORIGINAL in json.loads(result.stderr)["error"]


def test_cli_show_diz_de_quem_e_a_conversa(fake, cli):
    """O `kuro sessions show` procura com o user_id `cli`: a conversa de uma integração dava um
    404 seco."""
    result = cli.invoke(cli_main.app, ["--json", "sessions", "show", ORIGINAL])
    assert result.exit_code == 1
    assert "-u 5511900000000" in json.loads(result.stderr)["error"]


def test_cli_show_sugere_o_id_parecido(fake, cli):
    result = cli.invoke(cli_main.app, ["--json", "sessions", "show", "a1b2c3d4-r6"])
    assert ORIGINAL in json.loads(result.stderr)["error"]
