"""A regra de corte de todo documento (`web_formatter.chunk_markdown`): o trecho não
separa uma pergunta da resposta, um título do conteúdo nem uma frase ao meio."""

from agent_service.documents.web_formatter import chunk_markdown


def _sem_espacos(text: str) -> str:
    return text.replace("\n", "").replace(" ", "")


def test_pergunta_em_paragrafo_proprio_vai_com_a_resposta():
    md = ("Contexto geral do curso. " * 20) + "\n\nComo peço presença na aula gravada?\n\nPela Central do Aluno, aba Presença."
    chunks = chunk_markdown(md, max_chars=560)
    [com_pergunta] = [c for c in chunks if "Como peço presença" in c.text]
    assert com_pergunta.text.endswith("Pela Central do Aluno, aba Presença.")


def test_prefixo_de_pergunta_e_introducao_tambem_ficam_com_o_que_vem_depois():
    corpo = "Texto anterior bem longo. " * 18
    for gancho in ("P: qual o prazo", "Pergunta 3) qual o prazo", "Passo a passo:"):
        md = f"{corpo}\n\n{gancho}\n\nResposta logo abaixo."
        [trecho] = [c for c in chunk_markdown(md, max_chars=500) if gancho in c.text]
        assert trecho.text.endswith("Resposta logo abaixo."), gancho


def test_titulo_nunca_fica_sozinho_no_fim_de_um_trecho():
    md = ("Parágrafo da seção anterior. " * 15) + "\n\n## Como acionar\n\n" + "Ligue para o suporte."
    chunks = chunk_markdown(md, max_chars=460)
    assert not any(c.text.rstrip().endswith("## Como acionar") for c in chunks)
    [secao] = [c for c in chunks if "## Como acionar" in c.text]
    assert secao.text.startswith("## Como acionar") and secao.section == "Como acionar"


def test_paragrafo_grande_e_cortado_em_fim_de_frase():
    md = " ".join(f"Frase número {i} do regulamento." for i in range(80))
    chunks = chunk_markdown(md, max_chars=400)
    assert len(chunks) > 1 and all(len(c.text) <= 400 for c in chunks)
    assert all(c.text.endswith(".") for c in chunks)
    assert _sem_espacos("".join(c.text for c in chunks)) == _sem_espacos(md)


def test_faq_do_tutorial_continua_inteiro():
    md = (
        "# Tutorial de Solicitação de Frequência\n\n"
        "## Passo 1 — Acesse a Central do Aluno\n\nInforme o CPF e a data de nascimento.\n\n"
        "## Perguntas frequentes\n\n"
        "- Como registro minha presença? Pela Central do Aluno, aba Presença.\n"
        "- Assisti à aula gravada, como solicito? Marque \"Assisti à aula gravada\"."
    )
    chunks = chunk_markdown(md)
    assert len(chunks) == 1
    assert _sem_espacos(chunks[0].text) == _sem_espacos(md)


def test_so_perguntas_sem_resposta_fecham_o_trecho_como_estao():
    md = "\n\n".join(f"Pergunta {i} sem resposta?" for i in range(40))
    chunks = chunk_markdown(md, max_chars=300)
    assert all(len(c.text) <= 300 for c in chunks)
    assert _sem_espacos("".join(c.text for c in chunks)) == _sem_espacos(md)
