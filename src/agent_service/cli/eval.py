"""`kuro eval`: roda um dataset de casos num agente analista e diz se a decisão bate.

É o "provo que não piorou" antes de promover: cada caso tem a entrada e a
decisão esperada (ex.: a que o agente legado tomou), e a comparação é
**determinística**, campo a campo — nada de LLM-juiz decidindo se `acao` ou
`qtd_parcelas` estão certos.

Formato do dataset (JSONL, um caso por linha):

    {"id": "c1", "input": {...} | "texto", "dependencies": {...}, "expected": {"acao": "responder", ...}}

`input` objeto vira JSON no `document` do `/analyze`. Só os campos presentes em
`expected` são comparados (objetos aninhados viram caminhos: `acordo.qtd_parcelas`),
ou só os de `--fields`.

Regras de política (`--rules regras.json`) valem para **toda** saída, com ou sem
`expected` — são o que não pode acontecer nunca:

    [{"name": "R6 nunca fica parado", "field": "acao", "op": "ne", "value": "nada"},
     {"name": "acordo só com identidade", "when": {"field": "acao", "op": "eq", "value": "efetivar_acordo"},
      "field": "identidade_confirmada", "op": "eq", "value": true},
     {"name": "teto", "field": "acordo.valor_acordo", "op": "lte", "value": {"$dep": "teto"}}]

`op`: eq, ne, in, not_in, lt, lte, gt, gte, exists, not_exists. `{"$dep": "a.b"}`
lê o valor das `dependencies` do caso. Um caso passa quando todos os campos
comparados batem e nenhuma regra é violada.

Saída: código 0 se a taxa de casos aprovados atinge `--min-pass` e nenhuma regra
foi violada; 1 caso contrário. Com `--compare outro-agente`, os dois rodam o
mesmo dataset e o código é 1 se o candidato ficar pior que o comparado.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import typer
from rich.table import Table

from agent_service.cli.client import ApiError, Client, ServiceUnavailable
from agent_service.cli.common import BOM, EXIT_FAILED, EXIT_USAGE, console, emit, fail, fail_from, state
from agent_service.evaluation import check_rules, compare, flatten, summarize, validate_rules


# -- núcleo (sem typer: a CLI e o servidor MCP chamam o mesmo) -----------------------


class EvalInputError(ValueError):
    """Dataset, regras ou agente inadequados — erro de uso, não de execução."""


def parse_cases(text: str, source: str) -> list[dict[str, Any]]:
    cases = []
    for number, line in enumerate(text.lstrip(BOM).splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except ValueError as exc:
            raise EvalInputError(f"{source}:{number}: JSON inválido ({exc})") from exc
        if not isinstance(case, dict) or "input" not in case:
            raise EvalInputError(f"{source}:{number}: cada caso precisa de `input` (e normalmente de `expected`)")
        case.setdefault("id", str(number))
        cases.append(case)
    if not cases:
        raise EvalInputError(f"{source}: nenhum caso")
    return cases


def check_rules_spec(rules: Any) -> list[dict[str, Any]]:
    if not isinstance(rules, list):
        raise EvalInputError("as regras devem ser uma lista de objetos")
    if problems := validate_rules(rules):
        raise EvalInputError("; ".join(problems))
    return rules


def _run_case(
    client: Client,
    agent_type: str,
    case: dict[str, Any],
    fields: list[str] | None,
    rules: list[dict[str, Any]],
    tolerance: float,
    score: bool,
    dry_run: bool = False,
) -> dict[str, Any]:
    document = case["input"] if isinstance(case["input"], str) else json.dumps(case["input"], ensure_ascii=False)
    dependencies = case.get("dependencies") or {}
    expected = case.get("expected") or {}
    base: dict[str, Any] = {"id": case["id"], "run_id": None, "error": None, "result": None}
    try:
        body = {"agent_type": agent_type, "document": document, "dependencies": dependencies or None}
        if dry_run:
            body["dry_run"] = True
        response = client.analyze(body)
    except (ServiceUnavailable, ApiError) as exc:
        detail = exc.detail if isinstance(exc, ApiError) else str(exc)
        return {**base, "error": str(detail), "passed": False, "mismatches": [], "violations": [], "fields_compared": 0}

    result = response["result"]
    mismatches = compare(expected, result, fields, tolerance)
    violations = check_rules(rules, result, dependencies)
    fields_compared = len(fields) if fields else len(flatten(expected))
    passed = not mismatches and not violations
    if score:
        try:
            client.score(
                {"run_id": response["run_id"], "name": "eval", "value": 1 if passed else 0, "comment": f"caso {case['id']}"}
            )
        except (ServiceUnavailable, ApiError):
            pass  # a nota é um extra; o relatório continua valendo
    return {
        **base,
        "run_id": response["run_id"],
        "agent_version": response.get("agent_version"),
        "config_hash": response.get("config_hash"),
        "result": result,
        "passed": passed,
        "mismatches": mismatches,
        "violations": violations,
        "fields_compared": fields_compared,
    }


def _run_all(client: Client, agent_type: str, cases: list[dict[str, Any]], concurrency: int, **options: Any) -> list[dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return list(pool.map(lambda case: _run_case(client, agent_type, case, **options), cases))


def run_evaluation(
    client: Client,
    agent_type: str,
    cases: list[dict[str, Any]],
    *,
    compare_with: str | None = None,
    rules: list[dict[str, Any]] | None = None,
    fields: list[str] | None = None,
    min_pass: float = 0.9,
    tolerance: float = 0.01,
    concurrency: int = 4,
    score: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Roda o dataset e devolve `{summary, results, compare?, verdict}`.

    Agente que não é `analysis` sobe como `EvalInputError`, e a falha ao buscá-lo
    como `ApiError`/`ServiceUnavailable`. A falha de um caso fica no `error` dele."""
    for name in [agent_type] + ([compare_with] if compare_with else []):
        if client.get_agent(name).get("kind") != "analysis":
            raise EvalInputError(f"{name!r} não é um agente 'analysis' — o eval v0 compara saídas estruturadas")

    options = {"fields": fields, "rules": rules or [], "tolerance": tolerance, "score": score, "dry_run": dry_run}
    results = _run_all(client, agent_type, cases, concurrency, **options)
    compare_results = _run_all(client, compare_with, cases, concurrency, **options) if compare_with else None

    summary = summarize(agent_type, results)
    report: dict[str, Any] = {"summary": summary, "results": results}
    if summary["errors"] == summary["cases"]:
        verdict = {"ok": False, "reason": "todos os casos falharam ao executar — veja `error` em cada um"}
    elif summary["policy_violations"]:
        verdict = {"ok": False, "reason": f"{summary['policy_violations']} violação(ões) de política"}
    elif compare_results is not None:
        other = summarize(compare_with, compare_results)
        report["compare"] = {"summary": other, "results": compare_results}
        worse = summary["passed"] < other["passed"]
        verdict = {
            "ok": not worse,
            "reason": f"{agent_type} {'pior que' if worse else 'não é pior que'} {compare_with} "
            f"({summary['passed']} × {other['passed']} casos ok)",
        }
    else:
        ok = summary["pass_rate"] >= min_pass
        verdict = {"ok": ok, "reason": f"{summary['pass_rate']:.0%} dos casos ok (mínimo {min_pass:.0%})"}
    report["verdict"] = verdict
    return report


# -- comando -------------------------------------------------------------------------


def _render(report: dict[str, Any]) -> None:
    for summary, results in [(report["summary"], report["results"])] + (
        [(report["compare"]["summary"], report["compare"]["results"])] if report.get("compare") else []
    ):
        version = f" v{summary['agent_version']}" if summary["agent_version"] else ""
        console.print(
            f"\n[bold]{summary['agent_type']}[/]{version}  "
            f"{summary['passed']}/{summary['cases']} casos ok ({summary['pass_rate']:.0%})  "
            f"campos: {summary['field_match_rate'] if summary['field_match_rate'] is not None else '—'}  "
            f"violações: {summary['policy_violations']}  erros: {summary['errors']}"
        )
        failed = [r for r in results if not r["passed"]]
        if failed:
            table = Table(show_edge=False, header_style="bold")
            for column in ("caso", "o que falhou"):
                table.add_column(column)
            for r in failed[:30]:
                reasons = [r["error"]] if r["error"] else []
                reasons += [f"{m['field']}: esperado {m['expected']!r}, veio {m['actual']!r}" for m in r["mismatches"]]
                reasons += [f"[red]regra {v['rule']}[/] ({v['field']} {v['op']} {v['expected']!r}; veio {v['actual']!r})" for v in r["violations"]]
                table.add_row(str(r["id"]), "\n".join(reasons))
            console.print(table)
    verdict = report["verdict"]
    color = "green" if verdict["ok"] else "red"
    console.print(f"\n[{color}]{'✓' if verdict['ok'] else '✗'} {verdict['reason']}[/]")


def eval_command(
    ctx: typer.Context,
    agent_type: str = typer.Argument(..., help="Agente candidato (kind='analysis'), ex.: r8-draft."),
    file: str = typer.Option(..., "--file", "-f", help="Dataset JSONL: {id, input, dependencies, expected} por linha."),
    rules_file: str | None = typer.Option(None, "--rules", "-r", help="Regras de política (JSON) que nenhuma saída pode violar."),
    fields: str | None = typer.Option(None, "--fields", help="Só estes campos (vírgula; caminho com ponto: acordo.qtd_parcelas)."),
    compare_with: str | None = typer.Option(None, "--compare", help="Roda o mesmo dataset neste agente (ex.: o de produção) e compara."),
    min_pass: float = typer.Option(0.9, "--min-pass", min=0.0, max=1.0, help="Fração mínima de casos aprovados para sair com 0."),
    tolerance: float = typer.Option(0.01, "--tolerance", min=0.0, help="Diferença aceita em campos numéricos."),
    concurrency: int = typer.Option(4, "--concurrency", "-c", min=1, max=16, help="Casos em paralelo."),
    no_score: bool = typer.Option(False, "--no-score", help="Não grava a nota `eval` (1/0) em cada run."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Execução de teste: as tools recebem `X-Kuro-Dry-Run: true` e decidem o que simular."
    ),
) -> None:
    """Avalia um agente analista contra um dataset, de forma determinística.

    Ex.: `kuro eval r8-draft -f casos.jsonl --rules regras.json --compare r8 --json`.
    Sai com 1 se piorou (ou não atingiu --min-pass, ou violou alguma regra)."""
    st = state(ctx)
    try:
        text = open(file, encoding="utf-8").read()
    except OSError as exc:
        fail(st, f"não consegui ler {file}: {exc}", EXIT_USAGE)
    rules: list[dict[str, Any]] = []
    try:
        cases = parse_cases(text, file)
        if rules_file:
            try:
                raw_rules = json.loads(open(rules_file, encoding="utf-8").read().lstrip(BOM))
            except (OSError, ValueError) as exc:
                raise EvalInputError(f"não consegui ler as regras de {rules_file}: {exc}") from exc
            rules = check_rules_spec(raw_rules)
        report = run_evaluation(
            st.client,
            agent_type,
            cases,
            compare_with=compare_with,
            rules=rules,
            fields=[f.strip() for f in fields.split(",") if f.strip()] if fields else None,
            min_pass=min_pass,
            tolerance=tolerance,
            concurrency=concurrency,
            score=not no_score,
            dry_run=dry_run,
        )
    except EvalInputError as exc:
        fail(st, str(exc), EXIT_USAGE)
    except (ServiceUnavailable, ApiError) as exc:
        fail_from(st, exc)

    emit(st, report, _render)
    if not report["verdict"]["ok"]:
        raise typer.Exit(EXIT_FAILED)
