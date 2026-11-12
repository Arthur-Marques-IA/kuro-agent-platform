"""Segredos de tools `kind="api"` cifrados em repouso.

O que é segredo numa tool de API é o que a leitura pela API já mascarava:
`auth.token`, `auth.value`, `auth.password` e o valor dos `headers` cujo nome
parece de credencial (`SECRET_HEADER_RE`: Authorization, X-Api-Key, Cookie...).
Cifra-se **valor a valor**, não o objeto inteiro: o resto da config (URL,
parâmetros, `auth.type`, `auth.header`, `auth.username`, `Accept`) continua
legível no banco e no `--editable`, e só o que é segredo muda de forma.

Formato guardado: `enc:v1:<token Fernet>`, com a mesma chave das credenciais de
modelo (`CREDENTIALS_ENCRYPTION_KEY`, `models/crypto.py`). O prefixo é o que
separa cifrado de texto plano — sem ele, o valor é texto plano legado e vale
como está (a leitura é tolerante; regravar a config cifra).

Onde cada coisa acontece:
- **gravação** (`tools/store.py`): `encrypt_config` cifra o que ainda está em
  claro. Sem a chave configurada, grava em texto plano e loga um aviso — o
  `.env.example` traz a chave vazia, e recusar quebraria quem já cadastra tools
  com `auth` assim (ver `docs/notas-de-versao.md`).
- **leitura**: o store devolve o valor guardado, sem decifrar. A API mascara
  (`api/tools_routes.py`), e ecoar a máscara restaura o valor guardado tal qual.
- **chamada** (`tools/api_tool.py`): `decrypt_config` decifra numa cópia, na
  hora de montar a requisição. Chave errada ou ausente vira erro de
  configuração da tool — sem mandar o cifrado ao destino nem mostrá-lo.
- **hash da versão do agente** (`agents/versions.py`): `decrypt_for_hash` —
  o hash é do texto plano, então cifrar não cria versão nova de agente.
"""

import copy
import logging
import re
from collections.abc import Iterator
from typing import Any

from agent_service.models.crypto import EncryptionNotConfiguredError, decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

PREFIX = "enc:v1:"
SECRET_AUTH_FIELDS = ("token", "value", "password")
# O lugar do segredo é `auth` (`api_key` aceita header próprio), mas há tools com o
# token direto em `headers`: o valor desses é tratado como o de `auth`.
SECRET_HEADER_RE = re.compile(r"auth|token|key|secret|passw|cookie|session|signature|credential", re.IGNORECASE)


class ToolSecretError(RuntimeError):
    """Um segredo guardado não pôde ser decifrado (chave ausente ou trocada)."""


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def _secret_slots(config: dict[str, Any]) -> Iterator[tuple[dict[str, Any], str, str]]:
    """(dicionário, chave, onde) de cada segredo não vazio da config."""
    auth = config.get("auth")
    if isinstance(auth, dict):
        for name in SECRET_AUTH_FIELDS:
            if isinstance(auth.get(name), str) and auth[name]:
                yield auth, name, f"auth.{name}"
    headers = config.get("headers")
    if isinstance(headers, dict):
        for name, value in headers.items():
            if isinstance(value, str) and value and (SECRET_HEADER_RE.search(name) or is_encrypted(value)):
                yield headers, name, f"headers.{name}"


def secrets_at_rest(config: dict[str, Any]) -> str | None:
    """`None` (sem segredo), `encrypted` ou `plaintext` (algum ainda em claro)."""
    values = [d[k] for d, k, _ in _secret_slots(config)]
    if not values:
        return None
    return "encrypted" if all(is_encrypted(v) for v in values) else "plaintext"


def encrypt_config(config: dict[str, Any], *, tool_name: str = "") -> dict[str, Any]:
    """Cópia da config com os segredos em claro cifrados (os já cifrados ficam
    como estão — um eco da máscara restaura o cifrado e ele não é cifrado de novo).
    Sem `CREDENTIALS_ENCRYPTION_KEY` válida, devolve a cópia em texto plano e avisa."""
    out = copy.deepcopy(config)
    pending = [(d, k) for d, k, _ in _secret_slots(out) if not is_encrypted(d[k])]
    if not pending:
        return out
    try:
        for d, k in pending:
            d[k] = PREFIX + encrypt_secret(d[k])
    except EncryptionNotConfiguredError as exc:
        logger.warning(
            "Tool %r: segredo gravado em texto plano — %s. Configure a chave e regrave a tool para cifrar.",
            tool_name,
            exc,
        )
        return copy.deepcopy(config)
    return out


def decrypt_config(config: dict[str, Any]) -> dict[str, Any]:
    """Cópia da config com os segredos em claro, para a chamada. Texto plano
    legado passa como está. Levanta `ToolSecretError` sem o cifrado na mensagem."""
    out = copy.deepcopy(config)
    for d, k, where in _secret_slots(out):
        if not is_encrypted(d[k]):
            continue
        try:
            d[k] = decrypt_secret(d[k][len(PREFIX):])
        except EncryptionNotConfiguredError as exc:
            raise ToolSecretError(
                f"o segredo em {where} está cifrado e não pôde ser decifrado — a CREDENTIALS_ENCRYPTION_KEY "
                "do serviço está ausente ou não é a mesma de quando a tool foi salva. Configure a chave "
                "certa ou cadastre o segredo de novo."
            ) from exc
    return out


def decrypt_for_hash(config: dict[str, Any]) -> dict[str, Any]:
    """Para o hash da configuração do agente: o texto plano quando dá, o valor
    guardado quando não dá. Nunca levanta — uma chave errada não pode derrubar
    o agente inteiro, só a chamada da tool."""
    out = copy.deepcopy(config)
    for d, k, _ in _secret_slots(out):
        if is_encrypted(d[k]):
            try:
                d[k] = decrypt_secret(d[k][len(PREFIX):])
            except EncryptionNotConfiguredError:
                pass
    return out
