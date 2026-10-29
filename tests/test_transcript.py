"""A conversa compacta: fala separada do contexto, contexto só quando muda, tools numa linha."""

from agent_service.observability.transcript import compact_session, split_input

SISTEMA = "[SISTEMA]\nFila: CS\n[/SISTEMA]\n\nDADOS DO ALUNO:\nNome: Maria\n\n"


def _run(run_id, at, message, *, tools=(), content="ok", system="você é o R6"):
    return {
        "run_id": run_id,
        "created_at": at,
        "status": "COMPLETED",
        "input": {"input_content": message},
        "run_input": message,
        "content": content,
        "metrics": {"input_tokens": 100, "output_tokens": 20, "duration": 2.71, "details": {"model": [{"id": "gemini"}]}},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": message}],
        "tools": list(tools),
    }


def test_split_input_uses_the_last_marker():
    ctx, fala = split_input("HISTÓRICO: ÚLTIMA MENSAGEM DO ALUNO: velha\nÚLTIMA MENSAGEM DO ALUNO: quero parcelar")
    assert fala == "quero parcelar" and ctx.endswith("velha")
    assert split_input("oi") == ("", "oi")


def test_compact_session_shows_context_once_and_tools_in_one_line():
    tool = {
        "tool_name": "regente_r6_decidir",
        "tool_args": {"acao": "responder", "assunto": "financeiro"},
        "result": 'HTTP 200: {"ok": true}',
        "tool_call_error": False,
    }
    session = {
        "session_id": "s1",
        "agent_id": "r6",
        "user_id": "5511",
        "session_data": {"session_summary": {"summary": "aluna quer parcelar", "updated_at": "2026-10-29"}},
        "runs": [
            _run("r2", 1793298800, SISTEMA + "ÚLTIMA MENSAGEM DO ALUNO: 3x", system="v2"),
            _run("r1", 1793298702, SISTEMA + "ÚLTIMA MENSAGEM DO ALUNO: oi", tools=[tool]),
        ],
    }
    out = compact_session(session, [{"memory": "recebe dia 10", "topics": ["pagamento"], "updated_at": 1793298702}])
    first, second = out["items"]
    assert first["run_id"] == "r1" and first["fala"] == "oi" and "contexto" in first
    assert first["passos"] == ['regente_r6_decidir(acao=responder, assunto=financeiro) → HTTP 200: {"ok": true}']
    assert first["at"] == "29/10/2026 15:31:42"  # BRT
    assert second.get("contexto_igual_ao_anterior") and "contexto" not in second
    assert second["prompt_mudou"]
    assert out["resumo_da_sessao"]["texto"] == "aluna quer parcelar"
    assert out["memorias_do_contato"][0]["texto"] == "recebe dia 10"
    assert "messages" not in first and "run_input" not in first
