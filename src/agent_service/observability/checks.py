"""Checagens determinísticas de um agente, avaliadas em toda execução real.

Uma checagem é uma regra do `kuro eval` (`evaluation.py`: `field`, `op`, `value`,
`when` opcional) mais um nome e uma severidade, aplicada aos **fatos do run** em vez
de a um arquivo de casos. Não chama modelo, não julga qualidade de texto: diz "a
resposta veio vazia", "a tool obrigatória não foi chamada", "a tool com efeito
colateral rodou duas vezes".

Os fatos têm caminhos estáveis (`FIELDS`), documentados em `docs/observabilidade.md`.

O resultado de cada checagem avaliada vira uma linha em `run_scores` com
`source="check"` e `name="check:<nome>"`: `value` 1 = passou, 0 = reprovou, -1 = erro
de avaliação; `comment` guarda o detalhe (JSON). Checagem cujo `when` não vale para o
run não grava nada — a taxa de reprovação é reprovadas ÷ avaliadas. A severidade não
muda o status do run: `fail` e `warn` são só rótulos para quem lê a fila.
"""

import json
import re
from typing import Any

from agent_service.evaluation import OPERATORS, pattern_problem, rule_result

SOURCE = "check"
NAME_PREFIX = "check:"
SEVERITIES = ("warn", "fail")
MAX_CHECKS = 50

PASSED, FAILED, ERROR, SKIPPED = "passed", "failed", "error", "skipped"
VALUE_BY_STATUS = {PASSED: 1.0, FAILED: 0.0, ERROR: -1.0}

STATIC_FIELDS = (
    "output",
    "output_chars",
    "status",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "tool_calls",
    "tool_failures",
)
FIELDS = (
    *STATIC_FIELDS,
    "tools.<nome>.called",
    "tools.<nome>.calls",
    "tools.<nome>.ok",
    "tools.<nome>.args.<campo>",
    "result.<campo>",
    "dependencies.<campo>",
    "metadata.<campo>",
)
"""Os campos que uma checagem pode ler (`<nome>` é o nome da tool como aparece nos spans)."""

_TOOL_FIELD = re.compile(r"^tools\.([^.]+)\.(called|calls|ok|args\.[^.].*)$")
_PREFIXED = ("result.", "dependencies.", "metadata.")
_REFERENCED_TOOL = re.compile(r"^tools\.([^.]+)\.")
_ACTUAL_MAX_CHARS = 200
_NEEDS_NO_VALUE = ("exists", "not_exists")


class CheckSpecError(ValueError):
    """Checagem mal formada — vira 422 no cadastro do agente."""


def valid_field(field: Any) -> bool:
    if not isinstance(field, str):
        return False
    if field in STATIC_FIELDS:
        return True
    if _TOOL_FIELD.match(field):
        return True
    return any(field.startswith(p) and len(field) > len(p) and not field.endswith(".") for p in _PREFIXED)


def _validate_part(part: Any, label: str, what: str) -> dict[str, Any]:
    if not isinstance(part, dict):
        raise CheckSpecError(f"checagem {label}: `{what}` precisa ser um objeto {{field, op, value}}")
    field, op = part.get("field"), part.get("op")
    if not valid_field(field):
        raise CheckSpecError(
            f"checagem {label}: campo desconhecido {field!r} em `{what}`. Campos disponíveis: {', '.join(FIELDS)}"
        )
    if op not in OPERATORS:
        raise CheckSpecError(
            f"checagem {label}: operador desconhecido {op!r} em `{what}`. Use um de: {', '.join(OPERATORS)}"
        )
    out: dict[str, Any] = {"field": field, "op": op}
    if op in _NEEDS_NO_VALUE:
        return out
    if "value" not in part:
        raise CheckSpecError(f"checagem {label}: o operador {op!r} precisa de `value`")
    value = part["value"]
    is_dep = isinstance(value, dict) and set(value) == {"$dep"}
    if not is_dep:
        if op in ("in", "not_in") and not isinstance(value, list):
            raise CheckSpecError(f"checagem {label}: {op!r} pede uma lista em `value`")
        if op in ("lt", "lte", "gt", "gte") and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise CheckSpecError(f"checagem {label}: {op!r} pede um número em `value`")
        if op == "contains" and (isinstance(value, (dict, list)) or value is None):
            raise CheckSpecError(f"checagem {label}: 'contains' pede um texto (ou número) em `value`")
        if op == "matches" and (problem := pattern_problem(value)) is not None:
            raise CheckSpecError(f"checagem {label}: {problem}")
    out["value"] = value
    return out


def validate_checks(checks: Any) -> list[dict[str, Any]]:
    """Normaliza e confere a lista de checagens de um agente. Levanta `CheckSpecError`
    com a mensagem do primeiro problema."""
    if checks is None:
        return []
    if not isinstance(checks, list):
        raise CheckSpecError("checks precisa ser uma lista")
    if len(checks) > MAX_CHECKS:
        raise CheckSpecError(f"no máximo {MAX_CHECKS} checagens por agente")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, raw in enumerate(checks):
        if not isinstance(raw, dict):
            raise CheckSpecError(f"checagem #{i + 1}: precisa ser um objeto")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise CheckSpecError(f"checagem #{i + 1}: precisa de `name`")
        name = name.strip()
        if len(name) > 80:
            raise CheckSpecError(f"checagem {name!r}: `name` passa de 80 caracteres")
        if name in seen:
            raise CheckSpecError(f"checagem {name!r}: nome repetido")
        seen.add(name)
        severity = raw.get("severity", "fail")
        if severity not in SEVERITIES:
            raise CheckSpecError(f"checagem {name!r}: `severity` deve ser {' ou '.join(SEVERITIES)}")
        check = {"name": name, **_validate_part(raw, name, "field"), "severity": severity}
        if raw.get("when") is not None:
            check["when"] = _validate_part(raw["when"], name, "when")
        out.append(check)
    return out


# -- fatos do run -----------------------------------------------------------------


def _referenced_tools(checks: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for check in checks:
        for part in (check, check.get("when") or {}):
            if match := _REFERENCED_TOOL.match(str(part.get("field", ""))):
                names.add(match.group(1))
    return names


def _parse_object(output: str | None) -> dict[str, Any] | None:
    if not output:
        return None
    try:
        parsed = json.loads(output)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def run_facts(
    *,
    output: str | None,
    status: str,
    latency_ms: float | None,
    input_tokens: int,
    output_tokens: int,
    spans: list[Any],
    dependencies: Any,
    metadata: dict[str, str],
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Os fatos de um run, no formato que `get_path` percorre. Uma tool que nenhuma
    checagem citou só aparece se foi chamada; as citadas e não chamadas vêm com
    `called: false` e `calls: 0` (sem `ok` nem `args`: não houve chamada)."""
    tool_spans = [s for s in spans if s.type == "TOOL"]
    tools: dict[str, dict[str, Any]] = {}
    for span in tool_spans:
        entry = tools.setdefault(span.name, {"called": True, "calls": 0, "ok": True, "args": {}})
        entry["calls"] += 1
        entry["ok"] = entry["ok"] and span.level != "ERROR"  # o mesmo critério de `tool_failures`
        entry["args"] = span.input if isinstance(span.input, dict) else {}
    for name in _referenced_tools(checks):
        tools.setdefault(name, {"called": False, "calls": 0})
    facts: dict[str, Any] = {
        "output": output or "",
        "output_chars": len(output or ""),
        "status": status,
        "latency_ms": latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tool_calls": len(tool_spans),
        "tool_failures": sum(1 for s in tool_spans if s.level == "ERROR"),
        "tools": tools,
        "dependencies": dependencies if isinstance(dependencies, dict) else {},
        "metadata": dict(metadata),
    }
    if (result := _parse_object(output)) is not None:
        facts["result"] = result
    return facts


# -- avaliação --------------------------------------------------------------------


def _shown(value: Any) -> Any:
    """O valor visto, curto o bastante para gravar: texto longo e objeto viram prévia."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= _ACTUAL_MAX_CHARS else text[:_ACTUAL_MAX_CHARS] + "…"


def _sensitive(field: str, keys: frozenset[str]) -> bool:
    return any(part.lower() in keys for part in field.split("."))


def evaluate_checks(
    checks: list[dict[str, Any]], facts: dict[str, Any], *, redact_keys: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    """Uma entrada por checagem: `{name, severity, status, field, op, expected, actual,
    error?}`, com `status` em `passed`, `failed`, `error` (a avaliação levantou) ou
    `skipped` (o `when` não vale — não se grava). Nunca levanta. O `actual` de um campo
    com chave em `TRACE_REDACT_KEYS` sai mascarado, como no resto do trace."""
    from agent_service.observability.redaction import REDACTED

    results = []
    dependencies = facts.get("dependencies") if isinstance(facts.get("dependencies"), dict) else {}
    for i, check in enumerate(checks):
        entry: dict[str, Any] = {
            "name": check.get("name") or f"#{i + 1}",
            "severity": check.get("severity") if check.get("severity") in SEVERITIES else "fail",
            "field": check.get("field"),
            "op": check.get("op"),
            "expected": check.get("value"),
            "actual": None,
        }
        try:
            outcome = rule_result(check, i, facts, dependencies)
            entry["status"] = outcome["status"]
            if outcome["status"] != SKIPPED:
                hidden = redact_keys and _sensitive(str(check["field"]), redact_keys)
                entry["actual"] = REDACTED if hidden else _shown(outcome["actual"])
        except Exception as exc:
            entry["status"] = ERROR
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
        results.append(entry)
    return results


def to_score_rows(run_id: str, results: list[dict[str, Any]], timestamp: Any) -> list[dict[str, Any]]:
    """As linhas de `run_scores` para as checagens avaliadas (as `skipped` ficam de fora)."""
    import uuid

    rows = []
    for r in results:
        if r["status"] == SKIPPED:
            continue
        detail = {k: v for k, v in r.items() if k not in ("name", "status")}
        rows.append(
            {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:{NAME_PREFIX}{r['name']}")),
                "run_id": run_id,
                "name": f"{NAME_PREFIX}{r['name']}",
                "value": VALUE_BY_STATUS[r["status"]],
                "data_type": "NUMERIC",
                "comment": json.dumps(detail, ensure_ascii=False, default=str),
                "user_id": None,
                "source": SOURCE,
                "timestamp": timestamp,
            }
        )
    return rows


def from_score_row(name: str, value: float, comment: str | None) -> dict[str, Any]:
    """O caminho de volta: uma linha de `run_scores` → `{name, status, severity, ...}`."""
    detail: dict[str, Any] = {}
    if comment:
        try:
            parsed = json.loads(comment)
            detail = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            detail = {}
    status = PASSED if value > 0 else FAILED if value == 0 else ERROR
    return {
        "name": name[len(NAME_PREFIX) :] if name.startswith(NAME_PREFIX) else name,
        "status": status,
        "severity": detail.get("severity") if detail.get("severity") in SEVERITIES else "fail",
        "field": detail.get("field"),
        "op": detail.get("op"),
        "expected": detail.get("expected"),
        "actual": detail.get("actual"),
        "error": detail.get("error"),
    }
