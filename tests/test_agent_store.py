from agent_service.agents.store import (
    agent_feedback_notes,
    create_definition,
    delete_definition,
    get_definition,
    get_feedback_note,
    get_feedback_version,
    list_feedback_versions,
    list_prompt_versions,
    save_feedback_note,
    update_definition,
)
from agent_service.db import get_db


def test_create_definition_round_trip():
    create_definition(agent_type="teste-a", name="Teste A", instructions=["v1"])
    definition = get_definition("teste-a")
    assert definition is not None
    assert definition["prompt_version"] == 1
    delete_definition("teste-a")


def test_update_instructions_bumps_prompt_version_and_records_history():
    create_definition(agent_type="teste-b", name="Teste B", instructions=["v1"])
    update_definition("teste-b", instructions=["v2"])
    update_definition("teste-b", instructions=["v3"])

    definition = get_definition("teste-b")
    assert definition["prompt_version"] == 3
    assert definition["instructions"] == ["v3"]

    versions = list_prompt_versions("teste-b")
    assert [v["version"] for v in versions] == [3, 2, 1]
    delete_definition("teste-b")


def test_update_without_instructions_does_not_bump_version():
    create_definition(agent_type="teste-c", name="Teste C", instructions=["v1"])
    update_definition("teste-c", name="Novo nome")

    definition = get_definition("teste-c")
    assert definition["prompt_version"] == 1
    assert definition["name"] == "Novo nome"
    delete_definition("teste-c")


def test_create_definition_defaults_to_conversational_kind():
    create_definition(agent_type="teste-d", name="Teste D", instructions=["v1"])
    definition = get_definition("teste-d")
    assert definition["kind"] == "conversational"
    assert definition["response_schema"] == []
    delete_definition("teste-d")


def test_create_analysis_definition_with_response_schema():
    schema = [{"name": "valor", "type": "number", "required": True}]
    create_definition(
        agent_type="teste-e", name="Teste E", instructions=["v1"], kind="analysis", response_schema=schema
    )
    definition = get_definition("teste-e")
    assert definition["kind"] == "analysis"
    assert definition["response_schema"] == schema
    delete_definition("teste-e")


def test_save_feedback_note_versiona_cada_gravacao():
    """O histórico é o que permite desfazer um merge ruim sem abrir o banco."""
    assert get_feedback_note("teste-f") is None
    first = save_feedback_note("teste-f", [{"id": "r1", "texto": "confirme o CPF antes de responder"}])
    assert first["content"] == "- confirme o CPF antes de responder"
    assert first["version"] == 1

    second = save_feedback_note(
        "teste-f",
        [{"id": "r1", "texto": "confirme o CPF antes de responder"}, {"id": "r2", "texto": "seja breve"}],
    )
    assert second["version"] == 2
    assert second["content"].splitlines() == ["- confirme o CPF antes de responder", "- seja breve"]
    assert [v["version"] for v in list_feedback_versions("teste-f")] == [2, 1]
    assert get_feedback_version("teste-f", 1)["rules"] == [{"id": "r1", "texto": "confirme o CPF antes de responder"}]
    delete_definition("teste-f")


def test_nota_antiga_em_markdown_vira_regras_na_leitura():
    """Notas gravadas antes da coluna `rules`: sem isto, o primeiro merge sobre
    uma delas recomeçaria do zero e perderia tudo que já estava escrito."""
    create_definition(agent_type="teste-legado", name="Legado", instructions=["v1"])
    with get_db().db_engine.begin() as conn:
        conn.execute(
            agent_feedback_notes.insert().values(
                agent_type="teste-legado",
                content="*   confirme o CPF\n- seja breve",
                rules=[],
                version=1,
            )
        )
    note = get_feedback_note("teste-legado")
    assert [r["texto"] for r in note["rules"]] == ["confirme o CPF", "seja breve"]
    delete_definition("teste-legado")


def test_delete_definition_removes_feedback_note():
    create_definition(agent_type="teste-g", name="Teste G", instructions=["v1"])
    save_feedback_note("teste-g", [{"id": "r1", "texto": "seja breve"}])
    delete_definition("teste-g")
    assert get_feedback_note("teste-g") is None
    assert list_feedback_versions("teste-g") == []


def test_migracao_grava_as_regras_da_nota_legada_e_a_v1():
    """Converter só na leitura não bastava: os ids saíam diferentes a cada
    consulta (então `--remove <id>` nunca casava) e não havia v1 para voltar."""
    import importlib

    baseline = importlib.import_module("agent_service.migrations.versions.0001_baseline")

    create_definition(agent_type="teste-mig", name="Mig", instructions=["v1"])
    with get_db().db_engine.begin() as conn:
        conn.execute(
            agent_feedback_notes.insert().values(
                agent_type="teste-mig", content="- confirme o CPF\n- seja breve", rules=[], version=1
            )
        )
    with get_db().db_engine.begin() as conn:
        baseline.migrate_markdown_notes(conn)

    primeira = get_feedback_note("teste-mig")["rules"]
    segunda = get_feedback_note("teste-mig")["rules"]
    assert [r["texto"] for r in primeira] == ["confirme o CPF", "seja breve"]
    assert primeira == segunda, "os ids precisam ser estáveis entre leituras"
    assert [v["version"] for v in list_feedback_versions("teste-mig")] == [1]
    delete_definition("teste-mig")
