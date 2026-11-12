"""Persistência das credenciais de modelo — uma linha por credencial
(`model_credentials`), várias credenciais por provedor permitidas: na
configuração de cada agente, quem o cadastra define qual usar
(`agent_definitions.model_credential_id`), então provedores iguais podem ter
chaves diferentes para clientes/times diferentes.
A chave de API nunca é guardada em texto plano: `crypto.py` cifra antes de
`insert`/`update` e decifra só quando `provider.get_model` precisa montar o
cliente do SDK; as respostas da API (`api/model_credentials_routes.py`)
nunca devolvem `api_key_encrypted`.

A tabela antiga (`model_provider_credentials`, uma credencial por provedor) é
convertida uma vez pela migração de base do Alembic (`agent_service/migrations`).
"""

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import Boolean, Column, DateTime, MetaData, String, delete, func, insert, select, update
from sqlalchemy import Table as SATable

from agent_service.db import get_db
from agent_service.models.crypto import decrypt_secret, encrypt_secret, mask_secret

metadata = MetaData()

model_credentials = SATable(
    "model_credentials",
    metadata,
    Column("id", String, primary_key=True),
    Column("provider", String, nullable=False, index=True),
    Column("label", String, nullable=False),
    Column("api_key_encrypted", String, nullable=True),
    # Só os últimos 4 chars da chave, em texto plano — pra reconhecer qual é sem
    # decifrar a cada listagem. Nunca a chave inteira.
    Column("key_hint", String, nullable=True),
    Column("base_url", String, nullable=True),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("last_tested_at", DateTime(timezone=True), nullable=True),
    Column("last_test_ok", Boolean, nullable=True),
    Column("last_test_message", String, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    ),
)


def _row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def list_credentials(provider: str | None = None) -> list[dict[str, Any]]:
    with get_db().db_engine.connect() as conn:
        query = select(model_credentials)
        if provider:
            query = query.where(model_credentials.c.provider == provider)
        rows = conn.execute(query.order_by(model_credentials.c.provider, model_credentials.c.created_at)).all()
        return [_row_to_dict(r) for r in rows]


def get_credential(credential_id: str) -> dict[str, Any] | None:
    with get_db().db_engine.connect() as conn:
        row = conn.execute(select(model_credentials).where(model_credentials.c.id == credential_id)).first()
        return _row_to_dict(row) if row else None


def get_decrypted_api_key(credential_id: str) -> str | None:
    """`None` se a credencial não existe, está desabilitada ou não tem chave salva."""
    row = get_credential(credential_id)
    if row is None or not row["enabled"] or not row["api_key_encrypted"]:
        return None
    return decrypt_secret(row["api_key_encrypted"])


def resolve_default_credential(provider: str) -> dict[str, Any] | None:
    """Credencial usada quando o agente não fixa uma (`model_credential_id=None`)
    — a mais antiga habilitada do provedor, pra preservar o comportamento de
    quando só existia uma chave por provedor."""
    for row in list_credentials(provider):
        if row["enabled"]:
            return row
    return None


def create_credential(
    provider: str,
    label: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    credential_id = uuid4().hex
    engine = get_db().db_engine
    with engine.begin() as conn:
        conn.execute(
            insert(model_credentials).values(
                id=credential_id,
                provider=provider,
                label=label,
                api_key_encrypted=encrypt_secret(api_key) if api_key else None,
                key_hint=mask_secret(api_key) if api_key else None,
                base_url=base_url or None,
                enabled=enabled,
            )
        )
    created = get_credential(credential_id)
    assert created is not None
    return created


def update_credential(
    credential_id: str,
    *,
    label: str | None = None,
    api_key: str | None = None,
    clear_api_key: bool = False,
    base_url: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any] | None:
    """`api_key=None` (sem `clear_api_key`) preserva a chave já salva — só
    reenvia quando o admin realmente quer trocá-la. `None` se a credencial
    não existe."""
    if get_credential(credential_id) is None:
        return None

    values: dict[str, Any] = {}
    if label is not None:
        values["label"] = label
    if api_key is not None:
        values["api_key_encrypted"] = encrypt_secret(api_key)
        values["key_hint"] = mask_secret(api_key)
    elif clear_api_key:
        values["api_key_encrypted"] = None
        values["key_hint"] = None
    if base_url is not None:
        values["base_url"] = base_url or None
    if enabled is not None:
        values["enabled"] = enabled

    if values:
        with get_db().db_engine.begin() as conn:
            conn.execute(update(model_credentials).where(model_credentials.c.id == credential_id).values(**values))

    return get_credential(credential_id)


def record_test_result(
    credential_id: str, *, ok: bool, message: str | None, tested_at: datetime
) -> dict[str, Any] | None:
    engine = get_db().db_engine
    with engine.begin() as conn:
        result = conn.execute(
            update(model_credentials)
            .where(model_credentials.c.id == credential_id)
            .values(last_tested_at=tested_at, last_test_ok=ok, last_test_message=message)
        )
        if result.rowcount == 0:
            return None
    return get_credential(credential_id)


def delete_credential(credential_id: str) -> None:
    engine = get_db().db_engine
    with engine.begin() as conn:
        conn.execute(delete(model_credentials).where(model_credentials.c.id == credential_id))
