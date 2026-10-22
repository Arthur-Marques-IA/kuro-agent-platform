"""Collections de documentos: cadastro dinâmico e o elo com os agentes
(`knowledge_collection`), que é o que dá ao agente uma tool de busca na base.
"""

from dataclasses import replace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from agent_service.agents.base import build_agent
from agent_service.api.agents_routes import (
    AgentDefinitionIn,
    AgentDefinitionUpdate,
    create_agent,
    delete_agent,
    update_agent,
)
from agent_service.api.collections_routes import (
    CollectionIn,
    CollectionUpdateIn,
    create_collection,
    delete_collection,
    get_collection_detail,
    list_collections,
    update_collection,
)
from agent_service.documents.collections import collection_exists


@pytest.fixture
def manuais():
    created = create_collection(CollectionIn(name="manuais", label="Manuais", description="PDFs do produto"))
    yield created["name"]
    if any(c["name"] == "manuais" for c in list_collections()):
        delete_collection("manuais")


def _agent(**extra):
    return AgentDefinitionIn(agent_type="agente_rag", name="RAG", instructions=["Consulte os manuais."], **extra)


# -- cadastro ---------------------------------------------------------------------


def test_seeded_collection_is_listed():
    assert "general" in [c["name"] for c in list_collections()]


def test_create_and_update_collection(manuais):
    assert collection_exists("manuais")
    assert get_collection_detail("manuais")["label"] == "Manuais"
    assert update_collection("manuais", CollectionUpdateIn(label="Manuais do produto"))["label"] == "Manuais do produto"


def test_duplicate_and_invalid_names_are_rejected(manuais):
    with pytest.raises(HTTPException) as exc:
        create_collection(CollectionIn(name="manuais", label="Outra"))
    assert exc.value.status_code == 409

    with pytest.raises(HTTPException) as exc:
        create_collection(CollectionIn(name="Com Espaço", label="x"))
    assert exc.value.status_code == 422


def test_seeded_collection_cannot_be_deleted():
    with pytest.raises(HTTPException) as exc:
        delete_collection("general")
    assert exc.value.status_code == 403


# -- elo com o agente ---------------------------------------------------------------


def test_agent_can_point_to_a_collection_and_gets_a_search_tool(manuais):
    created = create_agent(_agent(knowledge_collection="manuais"))
    try:
        assert created["knowledge_collection"] == "manuais"
        with patch("agent_service.agents.base.get_collection") as get_collection:
            agent = build_agent(agent_id="x", name="x", instructions=["x"], knowledge_collection="manuais")
        get_collection.assert_called_once_with("manuais")
        assert agent.knowledge is get_collection.return_value
        assert agent.search_knowledge is True
    finally:
        delete_agent("agente_rag")


def test_agent_without_collection_has_no_knowledge():
    agent = build_agent(agent_id="x", name="x", instructions=["x"])
    assert agent.knowledge is None and agent.search_knowledge is False


def test_unknown_collection_is_rejected_on_create_and_update(manuais):
    with pytest.raises(HTTPException) as exc:
        create_agent(_agent(knowledge_collection="nao-existe"))
    assert exc.value.status_code == 422

    create_agent(_agent(knowledge_collection="manuais"))
    try:
        with pytest.raises(HTTPException) as exc:
            update_agent("agente_rag", AgentDefinitionUpdate(knowledge_collection="nao-existe"))
        assert exc.value.status_code == 422
        # E dá para desligar a base voltando o campo para None.
        assert update_agent("agente_rag", AgentDefinitionUpdate(knowledge_collection=None))["knowledge_collection"] is None
    finally:
        delete_agent("agente_rag")


def test_collection_in_use_cannot_be_deleted(manuais):
    create_agent(_agent(knowledge_collection="manuais"))
    try:
        with pytest.raises(HTTPException) as exc:
            delete_collection("manuais")
        assert exc.value.status_code == 409
        assert "agente_rag" in exc.value.detail
        assert get_collection_detail("manuais")["agents_using"] == ["agente_rag"]
    finally:
        delete_agent("agente_rag")


def test_default_collection_comes_first_and_is_flagged(manuais):
    """O upload do AgentOS alimenta a primeira coleção registrada — a semeada
    precisa vir na frente mesmo com nomes que ordenariam antes dela."""
    from agent_service.documents import collections as collections_module

    create_collection(CollectionIn(name="atendimento", label="Atendimento"))
    try:
        assert collections_module.default_collection_name() == "general"
        # `all_collections` monta objetos do Agno (pgvector); aqui só a ordem importa.
        with patch.object(collections_module, "get_collection", side_effect=lambda nome: nome):
            assert collections_module.all_collections()[0] == "general"
        flags = {c["name"]: c["is_default"] for c in list_collections()}
        assert flags["general"] is True and flags["atendimento"] is False
    finally:
        delete_collection("atendimento")


def test_same_text_in_two_collections_gets_distinct_content_ids(manuais):
    """A tabela de conteúdo é compartilhada; o id precisa distinguir a coleção,
    senão o mesmo texto vira uma linha só e uma indexação pode ser pulada."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch as mock_patch

    from agent_service.documents.collections import add_text

    knowledge = MagicMock()
    knowledge._build_content_hash.return_value = "hash-igual"
    knowledge._aload_content = AsyncMock()
    knowledge.aget_content_by_id = AsyncMock(return_value=None)
    with mock_patch("agent_service.documents.collections.get_collection", return_value=knowledge):
        id_general = asyncio.run(add_text("general", text="mesmo texto"))
        id_manuais = asyncio.run(add_text("manuais", text="mesmo texto"))
    assert id_general != id_manuais


# -- embedder por collection -------------------------------------------------
#
# O embedder deixou de ser fixo no Gemini: cada collection grava o seu na
# criação (ver documents/embedder.py). O que estes testes cobrem é o que
# quebraria em silêncio — uma collection antiga sem a coluna, e a troca de
# embedder depois de indexada.


def test_collection_sem_embedder_gravado_continua_no_google(manuais):
    """Linha anterior à coluna: `embedder_provider` nulo é google, não erro."""
    from agent_service.documents import store as doc_store

    doc_store.update_collection(manuais, label="Manuais")
    row = doc_store.get_collection_row(manuais)
    row["embedder_provider"] = None
    with patch.object(doc_store, "get_collection_row", return_value=row):
        detail = get_collection_detail(manuais)
    assert detail["embedder_provider"] == "google"
    assert detail["embedder_model"] == "gemini-embedding-001"


@pytest.fixture
def ollama_no_ar():
    """O ollama não tem chave, então a checagem de criação o sonda de verdade."""
    from agent_service.api import collections_routes

    with patch.object(collections_routes, "check_configured", side_effect=lambda p=None, m=None: None):
        yield


def test_create_collection_guarda_o_embedder_escolhido(ollama_no_ar):
    created = create_collection(
        CollectionIn(name="local", label="Local", embedder_provider="ollama", embedder_model="nomic-embed-text")
    )
    try:
        assert created["embedder_provider"] == "ollama"
        assert created["embedder_model"] == "nomic-embed-text"
        # Fixo: o update não expõe o campo, então não há como trocá-lo pela API.
        assert "embedder_provider" not in CollectionUpdateIn.model_fields
    finally:
        delete_collection("local")


def test_create_collection_recusa_provedor_desconhecido():
    with pytest.raises(HTTPException) as exc:
        create_collection(CollectionIn(name="x", label="X", embedder_provider="cohere"))
    assert exc.value.status_code == 422
    assert "cohere" in str(exc.value.detail)


def test_create_collection_recusa_provedor_sem_credencial():
    """Falha na criação, não na primeira ingestão: uma collection que parece
    pronta e só quebra ao receber documento é o pior dos dois erros."""
    from agent_service.documents import embedder

    with patch.object(embedder, "_credential", return_value=(None, None)):
        with pytest.raises(HTTPException) as exc:
            create_collection(CollectionIn(name="x", label="X", embedder_provider="openai"))
    assert exc.value.status_code == 422
    assert "openai" in str(exc.value.detail)


def test_embedder_google_usa_a_credencial_cadastrada_antes_do_env():
    """O bug original: o embedder ignorava /model-credentials e só olhava a
    GOOGLE_API_KEY do ambiente."""
    from agent_service.documents import embedder

    with patch.object(embedder, "_credential", return_value=("chave-do-console", None)):
        built = embedder.build_embedder("google")
    assert built.api_key == "chave-do-console"


def test_embedder_nao_chuta_dimensao_de_modelo_trocado():
    """A dimensão padrão vale para o modelo padrão. Com outro modelo, aplicar a
    mesma largura criaria a tabela pgvector errada — e busca errada é silenciosa."""
    from agent_service.documents import embedder

    with patch.object(embedder, "_credential", return_value=(None, None)):
        padrao = embedder.build_embedder("ollama")
        trocado = embedder.build_embedder("ollama", "mxbai-embed-large")
    assert padrao.dimensions == 768
    assert trocado.dimensions != 768


def test_create_collection_recusa_ollama_fora_do_ar():
    """Sem chave, `ollama` passaria em qualquer checagem baseada só em credencial
    — e quebraria na primeira ingestão. Daí a sonda no `/api/tags`."""
    from agent_service.models import catalog

    with patch.dict(
        catalog.PROVIDERS,
        {"ollama": replace(catalog.PROVIDERS["ollama"], probe=_probe_fora_do_ar)},
    ):
        with pytest.raises(HTTPException) as exc:
            create_collection(CollectionIn(name="x", label="X", embedder_provider="ollama"))
    assert exc.value.status_code == 422
    assert "ollama" in str(exc.value.detail)


def _probe_fora_do_ar(*, api_key=None, base_url=None):
    from agent_service.models.catalog import ProviderProbeError

    raise ProviderProbeError("connection refused")


def test_create_collection_exige_dimensoes_com_modelo_trocado(ollama_no_ar):
    with pytest.raises(HTTPException) as exc:
        create_collection(
            CollectionIn(name="x", label="X", embedder_provider="ollama", embedder_model="mxbai-embed-large")
        )
    assert exc.value.status_code == 422
    assert "embedder_dimensions" in str(exc.value.detail)


def test_create_collection_recusa_nome_com_vetores_orfaos(ollama_no_ar):
    """Apagar a collection não apaga `knowledge_<nome>`. Recriar com outro
    embedder misturaria vetores de dois embedders — e quando as larguras batem,
    o pgvector aceita calado."""
    from agent_service.api import collections_routes

    with patch.object(collections_routes, "_reject_orphan_vectors", side_effect=_conflito):
        with pytest.raises(HTTPException) as exc:
            create_collection(CollectionIn(name="antiga", label="Antiga"))
    assert exc.value.status_code == 409


def _conflito(name):
    raise HTTPException(status_code=409, detail=f"knowledge_{name} ainda tem documentos")


def test_agente_com_collection_sem_credencial_nao_derruba_o_boot():
    """`all_agents()` monta todos os agentes no startup. Um deles com o embedder
    sem credencial derrubava o processo inteiro."""
    from agent_service.agents import registry
    from agent_service.documents.embedder import EmbedderNotConfiguredError

    with patch.object(registry, "_build_from_definition", side_effect=EmbedderNotConfiguredError("openai: sem chave")):
        assert registry.all_agents() == []
