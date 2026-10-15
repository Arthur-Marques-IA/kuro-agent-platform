"""Versões da configuração de busca de uma collection.

O mesmo desenho de `agents/versions.py`, para a outra metade do que decide uma
resposta com RAG: trocar o modo de busca, o k ou o peso do vetor muda os trechos
que o agente lê sem mudar nada no agente. Cada configuração vira um snapshot
imutável em `collection_versions` (embedder + busca, ver `documents/search.py`),
identificado por um hash:

- a versão nasce na criação da collection e a cada mudança da busca; a mesma
  configuração tem sempre o mesmo número (voltar a uma anterior reaproveita a dela);
- cada run grava `knowledge_collection`, `collection_version` e
  `collection_config_hash`, e cada busca do trace leva a versão que a respondeu.
  Um agente na mesma versão que passa a responder diferente por causa da
  collection aparece aí.
"""

import hashlib
import json
from typing import Any

from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, func, insert, select
from sqlalchemy.exc import IntegrityError

from agent_service.db import get_db
from agent_service.documents.search import snapshot

metadata = MetaData()

collection_versions = Table(
    "collection_versions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("collection", String, nullable=False, index=True),
    Column("version", Integer, nullable=False),
    Column("config_hash", String, nullable=False),
    Column("config", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)


def config_hash(config: dict[str, Any]) -> str:
    canonical = json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def ensure_version(collection: str, row: dict[str, Any]) -> tuple[int, str]:
    """Versão da configuração atual desta collection — a existente ou uma nova."""
    config = snapshot(row)
    digest = config_hash(config)
    engine = get_db().db_engine
    for _ in range(3):
        try:
            with engine.begin() as conn:
                existing = conn.execute(
                    select(collection_versions.c.version).where(
                        collection_versions.c.collection == collection, collection_versions.c.config_hash == digest
                    )
                ).scalar()
                if existing is not None:
                    return existing, digest
                version = (
                    conn.execute(
                        select(func.max(collection_versions.c.version)).where(
                            collection_versions.c.collection == collection
                        )
                    ).scalar()
                    or 0
                ) + 1
                conn.execute(
                    insert(collection_versions).values(
                        collection=collection, version=version, config_hash=digest, config=config
                    )
                )
                return version, digest
        except IntegrityError:
            # Outro worker gravou ao mesmo tempo: a próxima volta lê o que ele gravou.
            continue
    raise RuntimeError(f"Não foi possível registrar a versão da collection {collection!r}")


def list_versions(collection: str) -> list[dict[str, Any]]:
    with get_db().db_engine.connect() as conn:
        rows = conn.execute(
            select(collection_versions)
            .where(collection_versions.c.collection == collection)
            .order_by(collection_versions.c.version.desc())
        ).all()
    return [dict(r._mapping) for r in rows]


def get_version(collection: str, version: int) -> dict[str, Any] | None:
    with get_db().db_engine.connect() as conn:
        row = conn.execute(
            select(collection_versions).where(
                collection_versions.c.collection == collection, collection_versions.c.version == version
            )
        ).first()
    return dict(row._mapping) if row else None
