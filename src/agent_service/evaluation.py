"""Comparação determinística de decisões estruturadas — o coração do `kuro eval`
e da taxa de concordância do modo shadow (`/observability/agreement`).

Uma decisão esperada (`expected`, ou a referência gravada para um run) é
comparada campo a campo com a saída do agente: objetos aninhados viram
caminhos (`acordo.qtd_parcelas`), números aceitam uma tolerância e booleanos não
se confundem com números. Regras de política dizem o que nenhuma saída pode
fazer, com condição (`when`) e valores lidos das `dependencies` (`{"$dep": ...}`).
Nada aqui chama modelo: o mesmo caso dá sempre o mesmo resultado.

O mesmo avaliador serve às checagens de um agente em execuções reais
(`observability/checks.py`): lá o "actual" são os fatos do run, e uma regra pode
sair `skipped` (o `when` não vale), `passed` ou `failed`.
"""

import re
from functools import lru_cache
from typing import Any

_MISSING = object()
_OPS = {"eq", "ne", "in", "not_in", "lt", "lte", "gt", "gte", "exists", "not_exists", "contains", "matches"}
OPERATORS = tuple(sorted(_OPS))

MAX_PATTERN_CHARS = 200
"""Tamanho máximo de um padrão de `matches`."""
MAX_TEXT_CHARS = 20_000
"""`matches` e `contains` olham só os primeiros caracteres do texto avaliado."""


def pattern_problem(pattern: Any) -> str | None:
    """Por que um padrão de `matches` não é aceito (`None` = aceito).

    O `re` do Python não tem timeout e a avaliação roda na thread que grava os traces:
    uma regex catastrófica pararia a gravação de tudo. Por isso recusamos antes, em vez
    de interromper depois: padrão longo, que não compila, com referência a grupo e com
    grupo repetido (`* + {n,}`) que contém repetição ou alternação — `(a+)+`, `(a|aa)*`
    (use uma classe de caracteres: `[ab]*`)."""
    if not isinstance(pattern, str) or not pattern:
        return "`matches` pede um padrão (texto) em `value`"
    if len(pattern) > MAX_PATTERN_CHARS:
        return f"o padrão passa de {MAX_PATTERN_CHARS} caracteres"
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"padrão inválido: {exc}"
    if re.search(r"\\[1-9]|\(\?P=|\\k<", pattern):
        return "referência a grupo (\\1, (?P=nome)) não é aceita"
    return _nested_repeat(pattern)


_BRACE = re.compile(r"\{\d*,?\d*\}")


def _nested_repeat(pattern: str) -> str | None:
    stack: list[bool] = []  # por grupo aberto: já tem repetição ou alternação dentro?
    i, n = 0, len(pattern)
    in_class = False
    while i < n:
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            in_class = ch != "]"
            i += 1
            continue
        if ch == "[":
            in_class = True
            i += 1
            if i < n and pattern[i] == "^":
                i += 1
            if i < n and pattern[i] == "]":
                i += 1
            continue
        if ch == "(":
            stack.append(False)
            i += 1
            if i < n and pattern[i] == "?":  # `(?:`, `(?P<n>`, `(?=`: o `?` aqui não repete nada
                i += 1
            continue
        if ch == ")" and stack:
            inner = stack.pop()
            nxt = pattern[i + 1] if i + 1 < n else ""
            unbounded = nxt in ("*", "+") or (nxt == "{" and _BRACE.match(pattern, i + 1) is not None)
            if inner and unbounded:
                return (
                    "grupo repetido que já contém repetição ou alternação (como `(a+)+`) pode travar a "
                    "avaliação — troque por uma classe de caracteres ou tire a repetição de fora"
                )
            if stack and (inner or unbounded or nxt == "?"):
                stack[-1] = True
            i += 1
            continue
        if stack and (ch in "*+?|" or (ch == "{" and _BRACE.match(pattern, i) is not None)):
            stack[-1] = True
        i += 1
    return None


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> "re.Pattern[str]":
    if (problem := pattern_problem(pattern)) is not None:
        raise ValueError(problem)
    return re.compile(pattern)


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
        if op == "contains":
            if isinstance(actual, str):
                return isinstance(target, str) and target in actual[:MAX_TEXT_CHARS]
            return isinstance(actual, (list, tuple)) and target in actual
        if op == "matches":
            if not isinstance(actual, str):
                return False
            return _compiled(target).search(actual[:MAX_TEXT_CHARS]) is not None
    except TypeError:
        return False
    raise ValueError(op)


def _part_problem(part: Any, label: str) -> str | None:
    if not isinstance(part, dict) or "field" not in part or part.get("op") not in _OPS:
        return f"regra {label}: precisa de `field` e `op` em {sorted(_OPS)}"
    if part["op"] == "matches" and not isinstance(part.get("value"), dict):
        if (problem := pattern_problem(part.get("value"))) is not None:
            return f"regra {label}: {problem}"
    return None


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
            if problem := _part_problem(part, label):
                problems.append(problem)
    return problems


def rule_result(
    rule: dict[str, Any], index: int, actual: dict[str, Any], dependencies: dict[str, Any]
) -> dict[str, Any]:
    """Avalia uma regra: `{rule, field, op, expected, actual, status}` com `status` em
    `skipped` (o `when` não vale para esta saída), `passed` ou `failed`. Uma regra malformada
    levanta: quem chama decide (o eval recusa o arquivo antes; a checagem de um run vira
    "erro de avaliação")."""
    field, op = rule["field"], rule["op"]
    base = {"rule": rule.get("name", f"#{index + 1}"), "field": field, "op": op}
    when = rule.get("when")
    if when and not _holds(get_path(actual, when["field"]), when["op"], _resolve(when.get("value"), dependencies)):
        return {**base, "expected": None, "actual": None, "status": "skipped"}
    got = get_path(actual, field)
    target = _resolve(rule.get("value"), dependencies)
    return {
        **base,
        "expected": target,
        "actual": None if got is _MISSING else got,
        "status": "passed" if _holds(got, op, target) else "failed",
    }


def check_rules(
    rules: list[dict[str, Any]], actual: dict[str, Any], dependencies: dict[str, Any]
) -> list[dict[str, Any]]:
    """Regras violadas por esta saída: `[{rule, field, op, expected, actual}]`."""
    violations = []
    for i, rule in enumerate(rules):
        result = rule_result(rule, i, actual, dependencies)
        if result["status"] == "failed":
            violations.append({k: v for k, v in result.items() if k != "status"})
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
