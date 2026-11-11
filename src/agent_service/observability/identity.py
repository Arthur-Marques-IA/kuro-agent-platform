"""Quem é o contato e por qual canal ele fala, para o cabeçalho de uma sessão no console
(`SessionTimeline.contact_name`/`channel`, `SessionSummary.contact_name`/`channel`).

A regra lê campos com nome conhecido, em ordem, e para no primeiro que tiver valor.
Nada de regex nem de procurar o nome no meio do texto da mensagem:

**Nome do contato**

1. uma `dependency` com chave em `CONTACT_NAME_KEYS` (`contact_name`, `nome_contato`,
   `nome`, `name`) e valor texto;
2. um bloco de `context` com `key` em `CONTACT_NAME_KEYS`: a primeira linha não vazia do
   `content`;
3. um bloco de `context` com `key` em `CONTACT_DATA_BLOCKS` (`dados_contato`,
   `dados_aluno`, `dados_cliente`): a linha que começa com `nome:` (sem diferenciar
   maiúsculas), o que vem depois dos dois-pontos. É o único passo que lê texto livre, e
   só dentro desse bloco.

**Canal**: a `metadata` do run com chave em `CHANNEL_KEYS` (`channel`, `canal`); senão,
uma `dependency` com essa chave.

Sem nada disso, `None` — o console mostra o `user_id`.
"""

from typing import Any

CONTACT_NAME_KEYS = ("contact_name", "nome_contato", "nome", "name")
CONTACT_DATA_BLOCKS = ("dados_contato", "dados_aluno", "dados_cliente")
CHANNEL_KEYS = ("channel", "canal")
NAME_MAX = 80
PREVIEW_MAX = 160


def _clean(value: Any, limit: int = NAME_MAX) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def preview(text: Any) -> str | None:
    """A prévia de uma fala na listagem de sessões: espaços colapsados, até `PREVIEW_MAX`."""
    return _clean(text, PREVIEW_MAX)


def _first_line(text: str) -> str | None:
    for line in text.splitlines():
        if line.strip():
            return _clean(line)
    return None


def _name_line(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("nome:"):
            return _clean(stripped[len("nome:") :])
    return None


def contact_name(
    dependencies: dict[str, Any] | None, context: list[dict[str, Any]] | None
) -> str | None:
    deps = dependencies if isinstance(dependencies, dict) else {}
    for key in CONTACT_NAME_KEYS:
        if name := _clean(deps.get(key)):
            return name
    blocks = {
        b.get("key"): str(b.get("content") or "") for b in context or [] if isinstance(b, dict)
    }
    for key in CONTACT_NAME_KEYS:
        if key in blocks and (name := _first_line(blocks[key])):
            return name
    for key in CONTACT_DATA_BLOCKS:
        if key in blocks and (name := _name_line(blocks[key])):
            return name
    return None


def channel(metadata: dict[str, Any] | None, dependencies: dict[str, Any] | None) -> str | None:
    for source in (metadata, dependencies):
        if not isinstance(source, dict):
            continue
        for key in CHANNEL_KEYS:
            if value := _clean(source.get(key)):
                return value
    return None
