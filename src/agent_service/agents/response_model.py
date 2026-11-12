"""O `output_schema` (pydantic) de um agente `kind="analysis"`, montado a partir
de `response_schema`.

Os campos folha (`string`, `integer`, `number`, `boolean`) têm a mesma forma de
`dependency_fields`. Além deles a saída aceita dois tipos compostos, que
`dependency_fields` não tem: `object` (com `fields`) e `array` (com `items`). É
o que permite extrair uma lista de itens de um documento — as parcelas de um
contrato, as mensagens de um histórico de conversa — em vez de só campos soltos.

A validação desse vocabulário mora em `agent_service/field_schema.py`, junto com
os parâmetros de tool, que descrevem campo do mesmo jeito. Aqui fica só a parte
que é específica daqui: virar um modelo pydantic.

```json
{"name": "parcelas", "type": "array",
 "items": {"type": "object", "fields": [{"name": "numero", "type": "integer", "required": true},
                                        {"name": "valor",  "type": "number",  "required": true}]}}
{"name": "cliente", "type": "object", "fields": [{"name": "nome", "type": "string"}]}
{"name": "tags",    "type": "array",  "items": {"type": "string"}}
```

`fields` e `items` são obrigatórios nos seus tipos — o porquê está no docstring
de `field_schema.py`.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, create_model

from agent_service.field_schema import FieldSchemaError, validate_fields

_PY_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}

ResponseSchemaError = FieldSchemaError
"""Nome histórico do erro, mantido porque as rotas e os testes importam por ele."""


def validate_response_schema(specs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Valida e normaliza `response_schema` — as regras compostas vivem em
    `agent_service/field_schema.py`, compartilhadas com os parâmetros de tool."""
    return validate_fields(specs, path="response_schema")


def _model_name(*parts: str) -> str:
    """Nome único por caminho, só para facilitar debug — aparece no schema e nos
    erros de validação, e não tem efeito em runtime."""
    return (
        "".join("".join(p.capitalize() for p in part.replace("-", "_").split("_")) for part in parts if part)
        or "Analysis"
    )


def _leaf_type(field: dict[str, Any]) -> Any:
    """`enum` vira `Literal[...]`: o provedor recebe o enum no schema da saída
    estruturada e a validação recusa qualquer valor fora dele."""
    if field.get("enum"):
        return Literal[tuple(field["enum"])]  # type: ignore[valid-type]
    return _PY_TYPES[field["type"]]


def _annotation(field: dict[str, Any], prefix: str) -> Any:
    field_type = field["type"]
    if field_type == "object":
        return _model(field["fields"], _model_name(prefix, field["name"]))
    if field_type == "array":
        items = field["items"]
        if items["type"] == "object":
            return list[_model(items["fields"], _model_name(prefix, field["name"], "item"))]  # type: ignore[misc]
        return list[_leaf_type(items)]  # type: ignore[misc]
    return _leaf_type(field)


def _model(fields: list[dict[str, Any]], name: str) -> type[BaseModel]:
    model_fields: dict[str, Any] = {}
    for f in fields:
        annotation = _annotation(f, name)
        description = f.get("description") or f.get("label") or f["name"]
        if f.get("required"):
            model_fields[f["name"]] = (annotation, Field(..., description=description))
        else:
            model_fields[f["name"]] = (annotation | None, Field(default=f.get("default"), description=description))
    return create_model(name, **model_fields)


def build_response_model(agent_type: str, fields: list[dict[str, Any]]) -> type[BaseModel]:
    return _model(fields, _model_name(agent_type) + "Output")
