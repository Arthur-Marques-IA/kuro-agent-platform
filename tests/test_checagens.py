"""Checagens automáticas do agente (`observability/checks.py`): o avaliador sobre fatos de
run, o cadastro (422), a gravação ao fim do run, o filtro da fila de revisão, o panorama e o
`kuro eval --agent-checks`."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from agent_service.api.agents_routes import (
    AgentDefinitionIn,
    AgentDefinitionUpdate,
    CheckIn,
    create_agent,
    delete_agent,
    get_agent_definition,
    update_agent,
)
from agent_service.api.observability_routes import get_run_trace, list_runs, overview
from agent_service.cli.eval import split_agent_checks
from agent_service.evaluation import check_rules, pattern_problem
from agent_service.observability import checks as run_checks
from agent_service.observability import run_store, trace_store
from agent_service.observability.run_store import RunRecord, SpanRecord

UTC = timezone.utc
NOW = datetime(2032, 5, 4, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def local_backend():
    trace_store.set_trace_store(None)
    yield
    trace_store.set_trace_store(None)


def _tool(name, *, args=None, level="DEFAULT"):
    return SpanRecord(type="TOOL", name=name, started_at=NOW, level=level, input=args or {})


def _facts(*, output="olá", status="success", spans=(), dependencies=None, metadata=None, checks=()):
    return run_checks.run_facts(
        output=output,
        status=status,
        latency_ms=1200.0,
        input_tokens=30,
        output_tokens=10,
        spans=list(spans),
        dependencies=dependencies,
        metadata=metadata or {},
        checks=list(checks),
    )


def _check(name="c", field="output", op="exists", value=None, severity="fail", when=None):
    spec = {"name": name, "field": field, "op": op, "severity": severity}
    if op not in ("exists", "not_exists"):
        spec["value"] = value
    if when:
        spec["when"] = when
    return spec


def _status(check, facts):
    (result,) = run_checks.evaluate_checks([check], facts)
    return result["status"]


# -- os fatos e o avaliador ----------------------------------------------------------


def test_run_facts_expose_every_documented_field():
    facts = _facts(
        output='{"acao": "responder", "valor": 3}',
        spans=[
            _tool("consultar", args={"cpf": "1"}),
            _tool("consultar", args={"cpf": "2"}, level="ERROR"),
            _tool("gravar", args={"id": 9}),
        ],
        dependencies={"cpf": "123"},
        metadata={"canal": "wa"},
    )
    cases = {
        "output_chars": 33,
        "status": "success",
        "latency_ms": 1200.0,
        "input_tokens": 30,
        "output_tokens": 10,
        "tool_calls": 3,
        "tool_failures": 1,
        "tools.consultar.called": True,
        "tools.consultar.calls": 2,
        "tools.consultar.ok": False,
        "tools.consultar.args.cpf": "2",  # a ÚLTIMA chamada
        "tools.gravar.ok": True,
        "result.acao": "responder",
        "dependencies.cpf": "123",
        "metadata.canal": "wa",
    }
    for field, expected in cases.items():
        assert _status(_check(field=field, op="eq", value=expected), facts) == "passed", field
    assert _status(_check(field="output", op="contains", value="responder"), facts) == "passed"


def test_tool_never_called_has_called_false_and_zero_calls():
    checks = [_check(field="tools.obrigatoria.called", op="eq", value=True)]
    facts = _facts(spans=[_tool("outra")], checks=checks)
    assert _status(checks[0], facts) == "failed"
    assert _status(_check(field="tools.obrigatoria.calls", op="eq", value=0), facts) == "passed"
    # Sem chamada não há `ok` nem `args`.
    assert _status(_check(field="tools.obrigatoria.ok", op="exists"), facts) == "failed"
    assert _status(_check(field="tools.obrigatoria.args.id", op="not_exists"), facts) == "passed"


def test_empty_output_and_no_structured_result():
    facts = _facts(output=None)
    assert facts["output"] == "" and facts["output_chars"] == 0
    assert _status(_check(field="output_chars", op="gte", value=5), facts) == "failed"
    assert _status(_check(field="result.acao", op="exists"), facts) == "failed"


def test_when_skips_the_check():
    check = _check(
        field="tools.gravar.calls", op="lte", value=1, when={"field": "status", "op": "eq", "value": "success"}
    )
    facts = _facts(status="error", spans=[_tool("gravar"), _tool("gravar")])
    assert _status(check, facts) == "skipped"
    assert _status(check, _facts(spans=[_tool("gravar"), _tool("gravar")])) == "failed"


def test_contains_and_matches():
    facts = _facts(output="Seu protocolo é ABC-123.")
    assert _status(_check(op="contains", value="protocolo"), facts) == "passed"
    assert _status(_check(op="contains", value="boleto"), facts) == "failed"
    assert _status(_check(op="matches", value=r"[A-Z]{3}-\d+"), facts) == "passed"
    assert _status(_check(op="matches", value=r"^\d+$"), facts) == "failed"
    # Texto: um número não "contém" nada nem casa padrão.
    assert _status(_check(field="output_chars", op="contains", value="2"), facts) == "failed"


def test_matches_only_reads_the_first_chars_of_the_text():
    from agent_service.evaluation import MAX_TEXT_CHARS

    facts = _facts(output="x" * MAX_TEXT_CHARS + "fim")
    assert _status(_check(op="matches", value="fim"), facts) == "failed"


@pytest.mark.parametrize("pattern", ["(a+)+$", "(a|aa)*b", "(x*)*y", "(a{2,})+", "(a)\\1", "a" * 201, "(", "[ab"])
def test_dangerous_or_invalid_patterns_are_refused(pattern):
    assert pattern_problem(pattern) is not None
    with pytest.raises(run_checks.CheckSpecError):
        run_checks.validate_checks([_check(op="matches", value=pattern)])


@pytest.mark.parametrize("pattern", [r"\d+", "[ab]+", "(?:ab)+", "(abc)?", r"(\d+)\s", "^ok$"])
def test_ordinary_patterns_are_accepted(pattern):
    assert pattern_problem(pattern) is None


def test_evaluation_error_never_raises_and_is_its_own_status():
    # Um padrão perigoso que escapou do cadastro (agente antigo, banco editado à mão).
    bad = _check(op="matches", value="(a+)+$")
    facts = _facts(output="a" * 40 + "!")
    (result,) = run_checks.evaluate_checks([bad], facts)
    assert result["status"] == "error" and "grupo repetido" in result["error"]


def test_actual_is_truncated_and_redacted():
    facts = _facts(output="y" * 500, dependencies={"cpf": "12345678900"})
    results = run_checks.evaluate_checks(
        [_check("a", field="output", op="eq", value="x"), _check("b", field="dependencies.cpf", op="eq", value="0")],
        facts,
        redact_keys=frozenset({"cpf"}),
    )
    assert len(results[0]["actual"]) <= 201
    assert results[1]["actual"] == "[redigido]"


def test_eval_reuses_the_same_operators():
    rules = [{"name": "sem boleto", "field": "texto", "op": "matches", "value": "boleto"}]
    assert check_rules(rules, {"texto": "tem boleto"}, {}) == []
    assert check_rules(rules, {"texto": "nada"}, {})[0]["rule"] == "sem boleto"


# -- cadastro ------------------------------------------------------------------------


@pytest.fixture
def agente():
    name = f"chk-{uuid4().hex[:8]}"
    yield name
    try:
        delete_agent(name)
    except HTTPException:
        pass


def _create(agente, checks):
    return create_agent(AgentDefinitionIn(agent_type=agente, name="X", instructions=["a"], checks=checks))


def test_unknown_field_and_operator_are_422_with_a_clear_message(agente):
    with pytest.raises(HTTPException) as bad_field:
        _create(agente, [CheckIn(name="a", field="saida", op="exists")])
    assert bad_field.value.status_code == 422 and "campo desconhecido 'saida'" in bad_field.value.detail
    assert "tools.<nome>.called" in bad_field.value.detail
    with pytest.raises(HTTPException) as bad_op:
        _create(agente, [CheckIn(name="a", field="output", op="parecido", value="x")])
    assert bad_op.value.status_code == 422 and "operador desconhecido" in bad_op.value.detail
    with pytest.raises(HTTPException) as bad_severity:
        _create(agente, [CheckIn(name="a", field="output", op="exists", severity="grave")])
    assert "severity" in bad_severity.value.detail
    with pytest.raises(HTTPException) as repeated:
        _create(
            agente,
            [CheckIn(name="a", field="output", op="exists"), CheckIn(name="a", field="status", op="exists")],
        )
    assert "repetido" in repeated.value.detail
    with pytest.raises(HTTPException) as no_value:
        _create(agente, [CheckIn(name="a", field="output_chars", op="gte")])
    assert "precisa de `value`" in no_value.value.detail
    with pytest.raises(HTTPException) as regex:
        _create(agente, [CheckIn(name="a", field="output", op="matches", value="(a+)+$")])
    assert regex.value.status_code == 422 and "pode travar" in regex.value.detail


def test_checks_roundtrip_update_and_config_hash(agente):
    from agent_service.agents.versions import effective_config

    created = _create(agente, [])
    assert created["checks"] == [] and "checks" not in effective_config(created)
    updated = update_agent(
        agente,
        AgentDefinitionUpdate(
            checks=[CheckIn(name="curta", field="output_chars", op="gte", value=5, severity="warn")],
        ),
    )
    assert updated["checks"] == [
        {"name": "curta", "field": "output_chars", "op": "gte", "value": 5, "severity": "warn"}
    ]
    assert get_agent_definition(agente)["checks"][0]["name"] == "curta"
    assert effective_config(updated)["checks"] == updated["checks"]
    # Omitir não mexe; lista vazia limpa.
    assert update_agent(agente, AgentDefinitionUpdate(name="Y"))["checks"]
    assert update_agent(agente, AgentDefinitionUpdate(checks=[]))["checks"] == []


# -- gravação, filtro e panorama ----------------------------------------------------


def _record(agent_type, checks, *, output="resposta", spans=(), dry_run=False, at=NOW, status="success"):
    run_id = f"run-{uuid4().hex[:10]}"
    run_store._write_run(
        RunRecord(
            run_id=run_id,
            trace_id=uuid4().hex,
            agent_type=agent_type,
            agent_name=None,
            prompt_version=1,
            endpoint="chat",
            user_id="u",
            session_id=f"s-{run_id}",
            message="oi",
            output=output,
            status=status,
            started_at=at,
            ended_at=at,
            spans=list(spans),
            input={"message": "oi", "dependencies": {"cpf": "1"}},
            metadata={"dry_run": "true"} if dry_run else {},
            checks=checks,
        )
    )
    return run_id


CHECKS = [
    _check("resposta-curta", field="output_chars", op="gte", value=10, severity="warn"),
    _check("gravar-uma-vez", field="tools.gravar.calls", op="lte", value=1),
    _check(
        "sem-efeito", field="tools.gravar.calls", op="eq", value=0, when={"field": "status", "op": "eq", "value": "x"}
    ),
    _check("padrao-ruim", field="output", op="matches", value="(a+)+$"),
]


def test_run_gets_its_check_results_and_scores_stay_separate(agente):
    run_id = _record(agente, CHECKS, output="curta", spans=[_tool("gravar"), _tool("gravar")])
    run = get_run_trace(run_id).run
    by_name = {c.name: c for c in run.checks}
    assert set(by_name) == {"resposta-curta", "gravar-uma-vez", "padrao-ruim"}  # o `when` falso não grava
    assert by_name["resposta-curta"].status == "failed" and by_name["resposta-curta"].severity == "warn"
    assert by_name["resposta-curta"].actual == 5 and by_name["resposta-curta"].expected == 10
    assert by_name["gravar-uma-vez"].status == "failed" and by_name["gravar-uma-vez"].actual == 2
    assert by_name["padrao-ruim"].status == "error" and by_name["padrao-ruim"].error
    # Não vira nota humana nem voto.
    assert get_run_trace(run_id).scores == []
    assert (run.feedback_up, run.feedback_down) == (0, 0)


def test_passing_run_and_run_without_checks(agente):
    good = _record(agente, CHECKS[:2], output="uma resposta longa", spans=[_tool("gravar")])
    assert [c.status for c in get_run_trace(good).run.checks] == ["passed", "passed"]
    assert get_run_trace(_record(agente, [])).run.checks == []


def test_evaluation_failure_never_loses_the_run(agente, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("bug na avaliação")

    monkeypatch.setattr(run_checks, "run_facts", explode)
    run_id = _record(agente, CHECKS[:1])
    assert get_run_trace(run_id).run.checks == []
    assert get_run_trace(run_id).run.output == "resposta"


def test_dry_run_is_evaluated_too(agente):
    run_id = _record(agente, CHECKS[:1], output="x", dry_run=True)
    assert get_run_trace(run_id).run.checks[0].status == "failed"


def test_list_filters_by_failed_check(agente):
    bad = _record(agente, CHECKS[:2], output="curta", spans=[_tool("gravar")])
    worse = _record(agente, CHECKS[:2], output="curta", spans=[_tool("gravar"), _tool("gravar")])
    ok = _record(agente, CHECKS[:2], output="uma resposta longa", spans=[_tool("gravar")])
    errored = _record(agente, CHECKS[3:], output="a" * 30)

    def ids(**filters):
        page = list_runs(agent_type=[agente], limit=50, **filters)
        return {r.run_id for r in page.items}

    assert ids() == {bad, worse, ok, errored}
    assert ids(check_failed=True) == {bad, worse}
    assert ids(check="gravar-uma-vez") == {worse}
    assert ids(check="resposta-curta") == {bad, worse}
    assert ids(check_failed=False) == {ok, errored}  # erro de avaliação não é reprovação
    page = list_runs(agent_type=[agente], check="gravar-uma-vez", limit=10)
    assert page.items[0].checks[0].name == "gravar-uma-vez"


def test_overview_has_failure_rate_per_check(agente):
    for output in ("curta", "curta", "uma resposta longa", "outra resposta longa"):
        _record(agente, CHECKS[:1], output=output)
    _record(agente, CHECKS[:1], output="x", dry_run=True)
    row = next(
        a
        for a in overview(since=NOW.replace(hour=0), until=NOW.replace(hour=23), agent_type=[agente]).agents
        if a.agent_type == agente
    )
    (rate,) = row.checks
    assert (rate.name, rate.evaluated, rate.failed, rate.failure_rate) == ("resposta-curta", 4, 2, 0.5)
    with_tests = overview(
        since=NOW.replace(hour=0), until=NOW.replace(hour=23), agent_type=[agente], include_dry_run=True
    ).agents[0]
    assert with_tests.checks[0].evaluated == 5


# -- kuro eval --agent-checks --------------------------------------------------------


def test_eval_only_uses_checks_it_can_see_in_the_analyze_response():
    usable, skipped = split_agent_checks(
        [
            _check("a", field="result.acao", op="ne", value="nada"),
            _check("b", field="tools.x.called", op="eq", value=True),
            _check("c", field="result.acao", op="exists", when={"field": "latency_ms", "op": "lt", "value": 5}),
        ]
    )
    assert [c["name"] for c in usable] == ["a"] and skipped == ["b", "c"]
