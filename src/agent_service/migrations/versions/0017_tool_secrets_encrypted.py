"""Segredos das tools `kind="api"` cifrados em repouso.

Cifra, valor a valor, o que já estava gravado em texto plano em
`tool_definitions.config`: `auth.token`, `auth.value`, `auth.password` e os
headers com nome de credencial. O formato é `enc:v1:<token Fernet>`, com a
`CREDENTIALS_ENCRYPTION_KEY` (a mesma das credenciais de modelo).

Sem a chave (ou com uma inválida), a migração **não falha**: deixa tudo em
texto plano e loga um aviso — o serviço lê texto plano legado normalmente, e
regravar a tool depois de configurar a chave cifra. O downgrade decifra; se
houver valor cifrado e a chave não servir, ele para com erro em vez de deixar
o cifrado para o código antigo mandar como token.

O formato e a lista do que é segredo estão copiados aqui (e não importados de
`tools/crypto.py`) de propósito: a migração descreve o banco deste momento.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-11
"""

import copy
import logging
import re
from typing import Any

import sqlalchemy as sa
from alembic import op
from cryptography.fernet import Fernet, InvalidToken

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

_PREFIX = "enc:v1:"
_AUTH_FIELDS = ("token", "value", "password")
_SECRET_HEADER_RE = re.compile(r"auth|token|key|secret|passw|cookie|session|signature|credential", re.IGNORECASE)

_tools = sa.table(
    "tool_definitions",
    sa.column("tool_name", sa.String),
    sa.column("kind", sa.String),
    sa.column("config", sa.JSON),
)


def _fernet() -> Fernet | None:
    from agent_service.config import get_settings

    key = get_settings().credentials_encryption_key
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except ValueError:
        return None


def _slots(config: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    slots: list[tuple[dict[str, Any], str]] = []
    auth = config.get("auth")
    if isinstance(auth, dict):
        slots += [(auth, k) for k in _AUTH_FIELDS if isinstance(auth.get(k), str) and auth[k]]
    headers = config.get("headers")
    if isinstance(headers, dict):
        slots += [
            (headers, k)
            for k, v in headers.items()
            if isinstance(v, str) and v and (_SECRET_HEADER_RE.search(k) or v.startswith(_PREFIX))
        ]
    return slots


def _api_tools(conn: sa.Connection) -> list[Any]:
    return list(conn.execute(sa.select(_tools.c.tool_name, _tools.c.config).where(_tools.c.kind == "api")))


def upgrade() -> None:
    conn = op.get_bind()
    rows = [r for r in _api_tools(conn) if isinstance(r.config, dict)]
    pending = [r for r in rows if any(not d[k].startswith(_PREFIX) for d, k in _slots(r.config))]
    if not pending:
        return
    fernet = _fernet()
    if fernet is None:
        logger.warning(
            "%d tool(s) de API com segredo em texto plano e CREDENTIALS_ENCRYPTION_KEY ausente ou inválida: "
            "ficam em texto plano (continuam funcionando). Configure a chave e regrave as tools para cifrar.",
            len(pending),
        )
        return
    for row in pending:
        config = copy.deepcopy(row.config)
        for d, k in _slots(config):
            if not d[k].startswith(_PREFIX):
                d[k] = _PREFIX + fernet.encrypt(d[k].encode()).decode()
        conn.execute(_tools.update().where(_tools.c.tool_name == row.tool_name).values(config=config))


def downgrade() -> None:
    conn = op.get_bind()
    rows = [r for r in _api_tools(conn) if isinstance(r.config, dict)]
    pending = [r for r in rows if any(d[k].startswith(_PREFIX) for d, k in _slots(r.config))]
    if not pending:
        return
    fernet = _fernet()
    if fernet is None:
        raise RuntimeError(
            "Há segredos de tool cifrados e a CREDENTIALS_ENCRYPTION_KEY está ausente ou inválida: "
            "sem ela não dá para decifrar, e a versão anterior mandaria o cifrado como token."
        )
    for row in pending:
        config = copy.deepcopy(row.config)
        for d, k in _slots(config):
            if d[k].startswith(_PREFIX):
                try:
                    d[k] = fernet.decrypt(d[k][len(_PREFIX):].encode()).decode()
                except InvalidToken as exc:
                    raise RuntimeError(
                        f"Tool {row.tool_name!r}: segredo cifrado com outra CREDENTIALS_ENCRYPTION_KEY."
                    ) from exc
        conn.execute(_tools.update().where(_tools.c.tool_name == row.tool_name).values(config=config))
