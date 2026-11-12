"""Campos de `dependencies` esperados por um agente.

Cada agente pode declarar quais chaves do `dependencies` do `/chat` espera
(ex.: `cpf`, obrigatório, string) — configurado em `agents_routes.py`,
guardado em `agent_definitions.dependency_fields` e validado a cada chamada
em `routes._resolve`. Mesma forma de `tools/catalog.py::ParamSpec`.

Campos NÃO declarados continuam passando livres: isto só garante que os
campos que o agente *espera* cheguem com o tipo certo (e apliquem o default
quando ausentes) — não transforma `dependencies` num schema fechado.
"""

from dataclasses import dataclass
from typing import Any, Literal

FieldType = Literal["string", "integer", "number", "boolean"]
_VALID_TYPES: set[str] = {"string", "integer", "number", "boolean"}


class DependencyFieldSpecError(ValueError):
    """A configuração dos campos em si é inválida — 422 ao criar/editar o agente."""


class DependencyValidationError(ValueError):
    """`dependencies` de uma chamada não bate com os campos configurados — 422 no `/chat`."""


@dataclass(frozen=True)
class DependencyField:
    name: str
    type: FieldType
    label: str
    description: str = ""
    required: bool = False
    default: Any = None


def validate_field_specs(specs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Valida e normaliza a configuração dos campos (não os valores de uma chamada)."""
    if not specs:
        return []
    if not isinstance(specs, list):
        raise DependencyFieldSpecError("dependency_fields deve ser uma lista")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in specs:
        name = raw.get("name")
        if not isinstance(name, str) or not name.isidentifier():
            raise DependencyFieldSpecError(f"nome de campo inválido: {name!r}")
        if name in seen:
            raise DependencyFieldSpecError(f"campo duplicado: {name!r}")
        seen.add(name)
        field_type = raw.get("type", "string")
        if field_type not in _VALID_TYPES:
            raise DependencyFieldSpecError(f"tipo inválido em {name!r}: {field_type!r} (use {sorted(_VALID_TYPES)})")
        field = {
            "name": name,
            "type": field_type,
            "label": raw.get("label") or name,
            "description": raw.get("description") or "",
            "required": bool(raw.get("required", False)),
            "default": raw.get("default"),
        }
        if raw.get("enum") is not None:
            field["enum"] = validate_enum(raw["enum"], field_type, where=repr(name))
            if field["default"] is not None and field["default"] not in field["enum"]:
                raise DependencyFieldSpecError(f"default de {name!r} fora de enum: {field['default']!r}")
        normalized.append(field)
    return normalized


def validate_enum(values: Any, field_type: str, *, where: str) -> list[Any]:
    """Valores permitidos de um campo. Só entra na definição quando declarado —
    um `"enum": null` em todos os campos mudaria o hash de todo agente existente."""
    if field_type == "boolean":
        raise DependencyFieldSpecError(f"enum em {where}: não se aplica a boolean")
    if not isinstance(values, list) or not values:
        raise DependencyFieldSpecError(f"enum em {where} deve ser uma lista não vazia")
    for value in values:
        if not _type_ok(field_type, value):
            raise DependencyFieldSpecError(f"enum em {where}: {value!r} não é do tipo {field_type}")
    if len(set(map(repr, values))) != len(values):
        raise DependencyFieldSpecError(f"enum em {where}: valores repetidos")
    return list(values)


def _type_ok(field_type: str, value: Any) -> bool:
    if field_type == "boolean":
        return isinstance(value, bool)
    if field_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, str)  # "string"


def validate_dependencies(specs: list[dict[str, Any]] | None, dependencies: dict[str, Any] | None) -> dict[str, Any]:
    """Confere obrigatoriedade + tipo dos campos declarados num `dependencies` de
    chamada, aplica os defaults ausentes e devolve o dict pronto pro `RunContext`."""
    result = dict(dependencies or {})
    if not specs:
        return result

    for field in specs:
        name = field["name"]
        field_type = field.get("type", "string")
        if name not in result or result[name] is None:
            if field.get("required", False):
                raise DependencyValidationError(f"campo obrigatório ausente em dependencies: {name!r}")
            if field.get("default") is not None:
                result[name] = field["default"]
            continue
        if not _type_ok(field_type, result[name]):
            raise DependencyValidationError(f"dependencies.{name} deve ser do tipo {field_type}")
        if field.get("enum") and result[name] not in field["enum"]:
            raise DependencyValidationError(f"dependencies.{name} deve ser um de {field['enum']}")
    return result
