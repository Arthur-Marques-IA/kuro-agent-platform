"""Erro do provedor numa frase (`models/errors.py`)."""

from agent_service.models.errors import friendly_model_error

GEMINI_503 = (
    'Error in Agent run: {\n  "error": {\n    "code": 503,\n    "message": "The service is currently unavailable.",\n'
    '    "status": "UNAVAILABLE"\n  }\n}\n'
)


def test_json_do_provedor_vira_uma_frase():
    msg = friendly_model_error(GEMINI_503)
    assert "\n" not in msg
    assert msg.startswith("Falha no modelo: o provedor respondeu 503 (UNAVAILABLE): The service is currently unavailable")
    assert "tente de novo" in msg


def test_prefixo_e_erro_nao_passageiro():
    msg = friendly_model_error('{"error": {"code": 400, "message": "Invalid  schema"}}', prefix="A extração falhou no modelo")
    assert msg == "A extração falhou no modelo: o provedor respondeu 400: Invalid schema."


def test_texto_sem_json_sai_como_veio():
    assert friendly_model_error("modelo caiu") == "modelo caiu"
    assert friendly_model_error("linha 1\nlinha 2") == "linha 1"
    assert friendly_model_error("quota", prefix="A extração falhou no modelo") == "A extração falhou no modelo: quota"
