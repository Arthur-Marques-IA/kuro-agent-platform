"""Limite de requisições por chave (`RATE_LIMIT_PER_MINUTE`) no `/chat`, `/chat/stream`
e `/analyze`: desligado por padrão, contado por chave, 429 com `Retry-After` no padrão
de erro do serviço, e a janela anda com o relógio."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_service.api import auth, errors, rate_limit, routes
from agent_service.config import Settings, get_settings

ADMIN = "chave-admin"
RUNTIME = "chave-runtime"
CHAT = {"agent_type": "x", "user_id": "u", "session_id": "s", "message": "oi"}
ANALYZE = {"agent_type": "x", "document": "texto"}


class Relogio:
    def __init__(self) -> None:
        self.agora = 1000.0

    def __call__(self) -> float:
        return self.agora


@pytest.fixture(autouse=True)
def limpa_contagem():
    rate_limit.reset()
    yield
    rate_limit.reset()


@pytest.fixture
def cliente(monkeypatch):
    """O router de verdade, com a execução do agente trocada por uma resposta fixa:
    o alvo é o limite, não o agente."""

    async def sem_agente(endpoint, request, key, handler):
        if endpoint == "analyze":
            return routes.AnalyzeResponse(agent_type="x", result={}, run_id="r")
        return routes.ChatResponse(agent_type="x", session_id="s", content="ok", run_id="r")

    monkeypatch.setattr(routes, "_idempotent", sem_agente)
    monkeypatch.setattr(get_settings(), "admin_api_key", ADMIN)
    monkeypatch.setattr(get_settings(), "runtime_api_key", RUNTIME)
    app = FastAPI()
    app.include_router(routes.router)
    auth.install(app)
    errors.install(app)
    return TestClient(app)


def _com(chave: str) -> dict:
    return {"Authorization": f"Bearer {chave}"}


def test_desligado_por_padrao(cliente):
    assert Settings().rate_limit_per_minute == 0
    for _ in range(30):
        assert cliente.post("/chat", json=CHAT, headers=_com(RUNTIME)).status_code == 200


def test_vazio_no_env_desliga(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "")
    assert Settings().rate_limit_per_minute == 0
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "30")
    assert Settings().rate_limit_per_minute == 30


def test_abaixo_do_limite_passa_e_acima_da_429(cliente, monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_per_minute", 3)
    assert cliente.post("/chat", json=CHAT, headers=_com(RUNTIME)).status_code == 200
    assert cliente.post("/analyze", json=ANALYZE, headers=_com(RUNTIME)).status_code == 200
    assert cliente.post("/chat", json=CHAT, headers=_com(RUNTIME)).status_code == 200

    resposta = cliente.post("/analyze", json=ANALYZE, headers=_com(RUNTIME))
    assert resposta.status_code == 429
    assert 1 <= int(resposta.headers["Retry-After"]) <= 60
    corpo = resposta.json()
    assert corpo["error"] == "rate_limited"
    assert "RATE_LIMIT_PER_MINUTE" in corpo["detail"]
    assert set(corpo) == {"detail", "error"}


def test_stream_responde_429_antes_de_abrir_o_stream(cliente, monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_per_minute", 1)
    assert cliente.post("/chat", json=CHAT, headers=_com(RUNTIME)).status_code == 200
    resposta = cliente.post("/chat/stream", json=CHAT, headers=_com(RUNTIME))
    assert resposta.status_code == 429
    assert resposta.headers["content-type"].startswith("application/json")
    assert "Retry-After" in resposta.headers
    assert resposta.json()["error"] == "rate_limited"


def test_chaves_diferentes_nao_se_afetam(cliente, monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_per_minute", 1)
    assert cliente.post("/chat", json=CHAT, headers=_com(RUNTIME)).status_code == 200
    assert cliente.post("/chat", json=CHAT, headers=_com(RUNTIME)).status_code == 429
    assert cliente.post("/chat", json=CHAT, headers=_com(ADMIN)).status_code == 200


def test_rotas_fora_do_limite_nao_contam(cliente, monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_per_minute", 1)
    for _ in range(5):
        cliente.get("/agent-types", headers=_com(ADMIN))
    assert cliente.post("/chat", json=CHAT, headers=_com(ADMIN)).status_code == 200


def test_servico_aberto_conta_por_ip(monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_api_key", None)
    monkeypatch.setattr(get_settings(), "runtime_api_key", None)

    class Req:
        def __init__(self, host, headers=None):
            self.client = type("C", (), {"host": host})()
            self.headers = headers or {}

    # Sem autenticação, a chave mandada não vale nada: quem conta é o IP.
    assert rate_limit.bucket_of(Req("10.0.0.1", {"x-api-key": "qualquer"})) == "ip:10.0.0.1"
    assert rate_limit.bucket_of(Req("10.0.0.2")) == "ip:10.0.0.2"


def test_bucket_guarda_hash_e_nao_a_chave(monkeypatch):
    monkeypatch.setattr(get_settings(), "runtime_api_key", RUNTIME)

    class Req:
        client = None
        headers = {"authorization": f"Bearer {RUNTIME}"}

    bucket = rate_limit.bucket_of(Req())
    assert bucket.startswith("key:") and RUNTIME not in bucket


def test_janela_expira_com_o_relogio():
    relogio = Relogio()
    limitador = rate_limit.RateLimiter(2, clock=relogio)
    limitador.hit("a")
    relogio.agora += 20
    limitador.hit("a")

    with pytest.raises(rate_limit.RateLimitedError) as erro:
        limitador.hit("a")
    # A primeira sai da janela em 40 s.
    assert erro.value.retry_after == 40

    relogio.agora += 40
    limitador.hit("a")  # a primeira saiu; abre uma vaga
    with pytest.raises(rate_limit.RateLimitedError):
        limitador.hit("a")

    relogio.agora += 60
    limitador.hit("a")
    limitador.hit("a")


def test_recusada_nao_conta_e_chave_vazia_some():
    relogio = Relogio()
    limitador = rate_limit.RateLimiter(1, clock=relogio)
    limitador.hit("a")
    for _ in range(5):
        with pytest.raises(rate_limit.RateLimitedError):
            limitador.hit("a")
    relogio.agora += 60
    limitador.hit("b")
    # A recusa não estende a janela, e "a" (vazia) não fica ocupando memória.
    assert "a" not in limitador._hits
    limitador.hit("a")
