"""Campos compostos (`object`, `array`) em algo descrito por dados.

Dois lugares do produto descrevem campos em JSON e precisam virar um JSON
Schema que o modelo entenda:

- `response_schema` de um agente analista (`agents/response_model.py`), que vira
  o `output_schema` pydantic;
- os parâmetros de uma tool `kind="api"` (`tools/api_tool.py`), que viram o
  schema da função que o modelo enxerga.

O vocabulário é o mesmo nos dois: campo folha igual ao de `dependency_fields`,
mais `object` (com `fields`) e `array` (com `items`). Ficar num módulo neutro
evita `tools/` depender de `agents/` e, principalmente, evita dois formatos
para a mesma ideia.

`fields` e `items` são **obrigatórios** nos seus tipos. Um `object` sem `fields`
vira `{"type": "object"}` e um `array` sem `items` vira `{"type": "array"}` — e
os provedores recusam os dois em saída estruturada e em declaração de tool (o
Gemini exige `properties` em OBJECT e um `items` tipado em ARRAY). Sem esta
trava o schema é aceito no cadastro e falha em toda chamada, que é o pior dos
dois mundos: o erro aparece longe de quem o causou.

`json_schema_for` é o único lugar que monta a propriedade de um campo. Um tipo
novo que apareça só precisa ser tratado aqui para não reabrir o mesmo buraco.
"""

from typing import Any

from agent_service.agents.dependency_fields import DependencyFieldSpecError, validate_enum, validate_field_specs

LEAF_TYPES = frozenset({"string", "integer", "number", "boolean"})
ALL_TYPES = LEAF_TYPES | {"object", "array"}
ITEM_TYPES = LEAF_TYPES | {"object"}
"""`array` de `array` fica de fora: aninhar listas direto raramente é o que se
quer (o caso real é lista de objetos) e complica o schema para o modelo."""

MAX_DEPTH = 5
"""Quantos `object`/`array` podem ser aninhados: 5 passa, 6 é recusado. Não é um
limite de provedor, é para um schema absurdo não virar um prompt gigante."""


class FieldSchemaError(ValueError):
    """Descrição de campo inválida — 422 ao criar/editar o agente ou a tool."""


# -- validação -------------------------------------------------------------------


def validate_fields(specs: Any, *, path: str, depth: int = 0) -> list[dict[str, Any]]:
    """Valida e normaliza uma lista de campos, com os tipos compostos."""
    if not specs:
        return []
    if not isinstance(specs, list):
        raise FieldSchemaError(f"{path} deve ser uma lista")
    if depth > MAX_DEPTH:
        raise FieldSchemaError(f"{path}: aninhamento passa de {MAX_DEPTH} níveis")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in specs:
        field = _validate_field(raw, path=path, depth=depth)
        if field["name"] in seen:
            raise FieldSchemaError(f"{path}: campo duplicado {field['name']!r}")
        seen.add(field["name"])
        normalized.append(field)
    return normalized


def _validate_field(raw: Any, *, path: str, depth: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise FieldSchemaError(f"{path}: cada campo deve ser um objeto")
    field_type = raw.get("type", "string")
    if field_type not in ALL_TYPES:
        raise FieldSchemaError(f"{path}: tipo inválido {field_type!r} em {raw.get('name')!r} (use {sorted(ALL_TYPES)})")
    if field_type in LEAF_TYPES:
        return _leaf(raw, path=path)
    base = _composite_base(raw, path=path)
    base.update(validate_composite(raw, path=f"{path}.{base['name']}", depth=depth))
    return base


def validate_composite(field: dict[str, Any], *, path: str, depth: int = 0) -> dict[str, Any]:
    """A parte composta de um campo: `{"fields": [...]}` ou `{"items": {...}}`.

    Separado de `validate_fields` porque as tools de API validam o composto de um
    parâmetro sem que o parâmetro em si tenha a forma de um campo de
    `dependency_fields` (ele tem `location`, `source`...)."""
    field_type = field.get("type")
    if field_type == "object":
        subcampos = field.get("fields")
        if not subcampos:
            raise FieldSchemaError(
                f"{path}: type='object' exige `fields` (um objeto sem campos não vira schema válido "
                "para o modelo — veja o docstring de agent_service/field_schema.py)"
            )
        return {"fields": validate_fields(subcampos, path=f"{path}.fields", depth=depth + 1)}

    if field_type != "array":
        raise FieldSchemaError(f"{path}: {field_type!r} não é um tipo composto")

    items = field.get("items")
    if not isinstance(items, dict):
        raise FieldSchemaError(
            f"{path}: type='array' exige `items` com o tipo do elemento, ex.: "
            '{"type": "string"} ou {"type": "object", "fields": [...]}'
        )
    item_type = items.get("type", "string")
    if item_type not in ITEM_TYPES:
        raise FieldSchemaError(f"{path}.items: tipo inválido {item_type!r} (use {sorted(ITEM_TYPES)})")
    normalized: dict[str, Any] = {"type": item_type}
    if items.get("enum") is not None:
        if item_type == "object":
            raise FieldSchemaError(f"{path}.items: enum não se aplica a object")
        try:
            normalized["enum"] = validate_enum(items["enum"], item_type, where=f"{path}.items")
        except DependencyFieldSpecError as exc:
            raise FieldSchemaError(str(exc)) from exc
    if item_type == "object":
        if not items.get("fields"):
            raise FieldSchemaError(f"{path}.items: type='object' exige `fields`")
        normalized["fields"] = validate_fields(items["fields"], path=f"{path}.items.fields", depth=depth + 1)
    return {"items": normalized}


def _leaf(raw: dict[str, Any], *, path: str) -> dict[str, Any]:
    """Campo folha é exatamente um campo de `dependency_fields`: mesma
    normalização, para não existirem dois formatos de descrever campo."""
    try:
        return validate_field_specs([raw])[0]
    except DependencyFieldSpecError as exc:
        raise FieldSchemaError(f"{path}: {exc}") from exc


def _composite_base(raw: dict[str, Any], *, path: str) -> dict[str, Any]:
    """O que `_leaf` faria, sem passar pelo validador de folha — ele só conhece
    os quatro tipos simples."""
    name = raw.get("name")
    if not isinstance(name, str) or not name.isidentifier():
        raise FieldSchemaError(f"{path}: nome de campo inválido: {name!r}")
    return {
        "name": name,
        "type": raw["type"],
        "label": raw.get("label") or name,
        "description": raw.get("description") or "",
        "required": bool(raw.get("required", False)),
        "default": None,  # composto não tem default: a ausência já é `None`
    }


# -- emissão de JSON Schema --------------------------------------------------------


def json_schema_for(field: dict[str, Any]) -> dict[str, Any]:
    """A propriedade de um campo, em JSON Schema. Único lugar que monta isto."""
    field_type = field["type"]
    schema: dict[str, Any] = {"type": field_type, "description": field.get("description") or ""}
    if field.get("enum"):
        schema["enum"] = field["enum"]
    if field_type == "object":
        schema.update(_properties(field["fields"]))
    elif field_type == "array":
        schema["items"] = _item_schema(field["items"])
    return schema


def _item_schema(items: dict[str, Any]) -> dict[str, Any]:
    if items["type"] == "object":
        return {"type": "object", **_properties(items["fields"])}
    return {"type": items["type"], **({"enum": items["enum"]} if items.get("enum") else {})}


def _properties(fields: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "properties": {f["name"]: json_schema_for(f) for f in fields},
        "required": [f["name"] for f in fields if f.get("required")],
    }


def object_schema(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """O schema de um objeto com estes campos — o que uma tool declara ao modelo."""
    return {"type": "object", **_properties(fields)}
