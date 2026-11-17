"""`kuro eval`: comparação determinística de decisões e regras de política."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from agent_service import evaluation as kuro_eval
from agent_service.cli import main as cli_main
from agent_service.cli.client import Client

runner = CliRunner()
ANALYST = {"agent_type": "r6", "kind": "analysis", "dependency_fields": []}


# -- lógica pura ---------------------------------------------------------------------


def test_compare_walks_nested_fields_and_tolerates_rounding():
    expected = {"acao": "efetivar_acordo", "acordo": {"qtd_parcelas": 3, "valor_acordo": 1234.56}}
    actual = {"acao": "efetivar_acordo", "acordo": {"qtd_parcelas": 4, "valor_acordo": 1234.561}}

    mismatches = kuro_eval.compare(expected, actual, fields=None, tolerance=0.01)

    assert mismatches == [{"field": "acordo.qtd_parcelas", "expected": 3, "actual": 4}]


def test_compare_only_the_chosen_fields_and_reports_missing_ones():
    assert kuro_eval.compare({"acao": "a", "resposta": "x"}, {"acao": "a", "resposta": "y"}, ["acao"], 0) == []
    assert kuro_eval.compare({"departamento": "Financeiro"}, {}, None, 0) == [
        {"field": "departamento", "expected": "Financeiro", "actual": None}
    ]


def test_booleans_are_not_numbers():
    assert kuro_eval.compare({"prioridade_alta": True}, {"prioridade_alta": 1}, None, 0.5)


def test_rules_with_condition_and_dependency_reference():
    rules = [
        {"name": "nunca parado", "field": "acao", "op": "ne", "value": "nada"},
        {
            "name": "identidade antes do acordo",
            "when": {"field": "acao", "op": "eq", "value": "efetivar_acordo"},
            "field": "identidade_confirmada",
            "op": "eq",
            "value": True,
        },
        {"name": "teto", "field": "acordo.valor_acordo", "op": "lte", "value": {"$dep": "teto"}},
    ]
    ok = {"acao": "efetivar_acordo", "identidade_confirmada": True, "acordo": {"valor_acordo": 900}}
    bad = {"acao": "efetivar_acordo", "identidade_confirmada": False, "acordo": {"valor_acordo": 1100}}

    assert kuro_eval.check_rules(rules, ok, {"teto": 1000}) == []
    assert [v["rule"] for v in kuro_eval.check_rules(rules, bad, {"teto": 1000})] == [
        "identidade antes do acordo",
        "teto",
    ]
    # `when` falso: a regra não se aplica.
    assert kuro_eval.check_rules(rules[1:2], {"acao": "responder"}, {}) == []


def test_invalid_rules_are_rejected_before_spending_tokens():
    assert kuro_eval.validate_rules({"field": "acao"})
    assert kuro_eval.validate_rules([{"field": "acao", "op": "parecido"}])
    assert kuro_eval.validate_rules([{"field": "acao", "op": "eq", "when": {"op": "eq"}}])
    assert kuro_eval.validate_rules([{"field": "acao", "op": "in", "value": ["a"]}]) == []


# -- comando ---------------------------------------------------------------------------


@pytest.fixture
def api(monkeypatch, tmp_path):
    """Agentes falsos: cada um decide pela função registrada em `deciders`."""
    monkeypatch.setenv("KURO_HOME", str(tmp_path))
    deciders: dict[str, object] = {}
    scores: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.startswith("/agents/"):
            return httpx.Response(200, json={**ANALYST, "agent_type": path.rsplit("/", 1)[-1]})
        if path == "/analyze":
            body = json.loads(request.content)
            result = deciders[body["agent_type"]](json.loads(body["document"]))
            return httpx.Response(
                200, json={"result": result, "run_id": f"run-{len(scores)}", "agent_version": 7, "config_hash": "abc"}
            )
        if path == "/observability/scores":
            scores.append(json.loads(request.content))
            return httpx.Response(202, json={})
        return httpx.Response(404, json={"detail": "não encontrado"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli_main,
        "Client",
        lambda url, timeout, api_key=None, **kw: Client(url, timeout, transport=transport, api_key=api_key, **kw),
    )
    return deciders, scores


def _dataset(tmp_path, cases):
    path = tmp_path / "casos.jsonl"
    path.write_text("\n".join(json.dumps(c) for c in cases), encoding="utf-8")
    return str(path)


CASES = [
    {"id": "a", "input": {"divida": 100}, "expected": {"acao": "responder"}},
    {"id": "b", "input": {"divida": 900}, "expected": {"acao": "transferir"}},
]


def test_eval_passes_when_decisions_match_and_scores_each_run(api, tmp_path):
    deciders, scores = api
    deciders["r6"] = lambda doc: {"acao": "responder" if doc["divida"] < 500 else "transferir"}

    result = runner.invoke(cli_main.app, ["--json", "eval", "r6", "-f", _dataset(tmp_path, CASES)])

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["summary"]["passed"] == 2 and report["summary"]["agent_version"] == 7
    assert report["verdict"]["ok"] is True
    assert sorted(s["value"] for s in scores) == [1, 1] and {s["name"] for s in scores} == {"eval"}


def test_eval_fails_below_min_pass(api, tmp_path):
    deciders, _ = api
    deciders["r6"] = lambda doc: {"acao": "responder"}

    result = runner.invoke(cli_main.app, ["--json", "eval", "r6", "-f", _dataset(tmp_path, CASES), "--no-score"])

    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["summary"]["pass_rate"] == 0.5
    assert report["results"][1]["mismatches"] == [{"field": "acao", "expected": "transferir", "actual": "responder"}]


def test_a_policy_violation_fails_even_with_every_decision_right(api, tmp_path):
    deciders, _ = api
    deciders["r6"] = lambda doc: {"acao": "responder" if doc["divida"] < 500 else "transferir", "valor": doc["divida"]}
    rules = tmp_path / "regras.json"
    rules.write_text(json.dumps([{"name": "teto", "field": "valor", "op": "lte", "value": 500}]), encoding="utf-8")

    result = runner.invoke(
        cli_main.app, ["--json", "eval", "r6", "-f", _dataset(tmp_path, CASES), "-r", str(rules), "--no-score"]
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["summary"]["policy_violations"] == 1


def test_compare_fails_when_the_candidate_is_worse_than_production(api, tmp_path):
    deciders, _ = api
    deciders["r6"] = lambda doc: {"acao": "responder" if doc["divida"] < 500 else "transferir"}
    deciders["r6-draft"] = lambda doc: {"acao": "responder"}

    worse = runner.invoke(
        cli_main.app, ["--json", "eval", "r6-draft", "--compare", "r6", "-f", _dataset(tmp_path, CASES), "--no-score"]
    )
    better = runner.invoke(
        cli_main.app, ["--json", "eval", "r6", "--compare", "r6-draft", "-f", _dataset(tmp_path, CASES), "--no-score"]
    )

    assert worse.exit_code == 1 and "pior que" in json.loads(worse.stdout)["verdict"]["reason"]
    assert better.exit_code == 0


def test_bad_dataset_is_a_usage_error(api, tmp_path):
    path = tmp_path / "ruim.jsonl"
    path.write_text('{"expected": {}}\n', encoding="utf-8")
    assert runner.invoke(cli_main.app, ["--json", "eval", "r6", "-f", str(path)]).exit_code == 2
