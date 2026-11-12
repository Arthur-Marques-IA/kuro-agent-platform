"""Comparação determinística de decisões estruturadas — o coração do `kuro eval`
e da taxa de concordância do modo shadow (`/observability/agreement`).

Uma decisão esperada (`expected`, ou a referência gravada para um run) é
comparada campo a campo com a saída do agente: objetos aninhados viram
caminhos (`acordo.qtd_parcelas`), números aceitam uma tolerância e booleanos não
se confundem com números. Regras de política dizem o que nenhuma saída pode
fazer, com condição (`when`) e valores lidos das `dependencies` (`{"$dep": ...}`).
Nada aqui chama modelo: o mesmo caso dá sempre o mesmo resultado.
"""

from typing import Any

_MISSING = object()
_OPS = {"eq", "ne", "in", "not_in", "lt", "lte", "gt", "gte", "exists", "not_exists"}


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """`{"a": {"b": 1}}` → `{"a.b": 1}`. Listas ficam inteiras (comparadas como valor)."""
    if isinstance(value, dict) and value:
        out: dict[str, Any] = {}
        for key, item in value.items():
            out.update(flatten(item, f"{prefix}{key}."))
        return out
    return {prefix[:-1]: value} if prefix else {}


def get_path(value: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _equal(expected: Any, actual: Any, tolerance: float) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(expected - actual) <= tolerance
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip() == actual.strip()
    return expected == actual


def compare(
    expected: dict[str, Any], actual: dict[str, Any], fields: list[str] | None, tolerance: float
) -> list[dict[str, Any]]:
    """Campos que não bateram: `[{field, expected, actual}]`."""
    wanted = flatten(expected)
    if fields:
        wanted = {f: get_path(expected, f) for f in fields if get_path(expected, f) is not _MISSING}
    mismatches = []
    for field, exp in wanted.items():
        got = get_path(actual, field)
        if got is _MISSING or not _equal(exp, got, tolerance):
            mismatches.append({"field": field, "expected": exp, "actual": None if got is _MISSING else got})
    return mismatches


def _resolve(value: Any, dependencies: dict[str, Any]) -> Any:
    if isinstance(value, dict) and set(value) == {"$dep"}:
        found = get_path(dependencies, value["$dep"])
        return None if found is _MISSING else found
    return value


def _holds(actual: Any, op: str, target: Any) -> bool:
    if op == "exists":
        return actual is not _MISSING and actual is not None
    if op == "not_exists":
        return actual is _MISSING or actual is None
    if actual is _MISSING:
        return False
    try:
        if op == "eq":
            return actual == target
        if op == "ne":
            return actual != target
        if op == "in":
            return actual in target
        if op == "not_in":
            return actual not in target
        if op == "lt":
            return actual < target
        if op == "lte":
            return actual <= target
        if op == "gt":
            return actual > target
        if op == "gte":
            return actual >= target
    except TypeError:
        return False
    raise ValueError(op)


def validate_rules(rules: Any) -> list[str]:
    """Problemas no arquivo de regras — melhor recusar antes de gastar tokens."""
    if not isinstance(rules, list):
        return ["o arquivo de regras precisa ser uma lista"]
    problems = []
    for i, rule in enumerate(rules):
        label = rule.get("name", f"#{i + 1}") if isinstance(rule, dict) else f"#{i + 1}"
        for part in (rule, rule.get("when")) if isinstance(rule, dict) else (rule,):
            if part is None:
                continue
            if not isinstance(part, dict) or "field" not in part or part.get("op") not in _OPS:
                problems.append(f"regra {label}: precisa de `field` e `op` em {sorted(_OPS)}")
    return problems


def check_rules(
    rules: list[dict[str, Any]], actual: dict[str, Any], dependencies: dict[str, Any]
) -> list[dict[str, Any]]:
    """Regras violadas por esta saída: `[{rule, field, op, expected, actual}]`."""
    violations = []
    for i, rule in enumerate(rules):
        when = rule.get("when")
        if when and not _holds(get_path(actual, when["field"]), when["op"], _resolve(when.get("value"), dependencies)):
            continue
        got = get_path(actual, rule["field"])
        target = _resolve(rule.get("value"), dependencies)
        if not _holds(got, rule["op"], target):
            violations.append(
                {
                    "rule": rule.get("name", f"#{i + 1}"),
                    "field": rule["field"],
                    "op": rule["op"],
                    "expected": target,
                    "actual": None if got is _MISSING else got,
                }
            )
    return violations


def summarize(agent_type: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    compared = [r for r in results if r["error"] is None]
    fields_total = sum(r["fields_compared"] for r in compared)
    fields_ok = sum(r["fields_compared"] - len(r["mismatches"]) for r in compared)
    passed = sum(1 for r in results if r["passed"])
    versions = {(r.get("agent_version"), r.get("config_hash")) for r in compared}
    return {
        "agent_type": agent_type,
        "agent_version": next(iter(versions))[0] if len(versions) == 1 else None,
        "config_hash": next(iter(versions))[1] if len(versions) == 1 else None,
        "cases": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "errors": sum(1 for r in results if r["error"] is not None),
        "pass_rate": round(passed / len(results), 4) if results else 0.0,
        "field_match_rate": round(fields_ok / fields_total, 4) if fields_total else None,
        "policy_violations": sum(len(r["violations"]) for r in results),
    }
