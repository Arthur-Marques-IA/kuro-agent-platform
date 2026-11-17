# Contribuindo com o Kuro

Obrigado pelo interesse. Este guia é o caminho curto; o detalhe de como rodar e testar está em
[docs/desenvolvimento.md](docs/desenvolvimento.md).

## Antes de começar

- **Dúvida ou ideia:** abra uma [issue](https://github.com/Arthur-Marques-IA/kuro-agent-platform/issues/new/choose).
  Para uma mudança grande, combine o desenho na issue antes de escrever o código.
- **Falha de segurança:** não abra issue. Siga a [política de segurança](SECURITY.md).
- **O que já está planejado** (e o que ficou de fora de propósito) está no [ROADMAP](ROADMAP.md).

## Preparar o ambiente

```bash
cp .env.example .env            # GOOGLE_API_KEY e CREDENTIALS_ENCRYPTION_KEY
docker compose up -d            # Postgres e o serviço
uv sync                         # dependências do backend
npm --prefix frontend ci        # dependências do console
```

O passo a passo, com o console e o primeiro agente, está no [quickstart](docs/quickstart.md).

## O que precisa passar

| Onde | Comando |
|---|---|
| Backend | `uv run pytest -q` |
| Backend, lint e formatação | `uv run ruff check` e `uv run ruff format --check` |
| Backend, tipos | `uv run mypy` |
| Console | `npm run lint`, `npm test` e `npm run build`, em `frontend/` |
| Console, de ponta a ponta | `npm run e2e`, em `frontend/` (quando a mudança toca criar agente, conversar, sessão ou trace) |
| Tipos do console | `npm run gen:types`, em `frontend/` (quando a mudança altera a API) |

O CI roda tudo isso, mais os testes marcados contra Postgres e o build das imagens.

## Regras da casa

- **Um assunto por PR.** Pequeno o bastante para ser revisado de uma vez.
- **Não quebre o contrato do `/chat` e do `/analyze`.** Campo novo é opcional.
- **Mudou algo que quem integra ou opera percebe** (campo, status HTTP, texto de erro, default,
  variável de ambiente, passo de deploy)? Registre em
  [docs/notas-de-versao.md](docs/notas-de-versao.md), numa seção `## Não lançada` no topo, no
  mesmo commit. O formato está no fim daquele arquivo.
- **Mudou o que alguém lê para usar o Kuro?** Atualize o documento junto: os links relativos são
  conferidos por `tests/test_docs_links.py`.
- **Console:** leia `frontend/AGENTS.md` antes de editar, e confira a mudança no navegador com
  `npm --prefix frontend run dev`.
- **Commits** em português, no imperativo curto, dizendo o que mudou para quem usa
  (`Egress: só endereço público passa`), como no `git log`.
- **Código, comentários e documentação** em português, como o resto do repositório.

## Abrir o PR

1. Crie uma branch a partir de `master`.
2. Preencha o modelo do PR: o que mudou, como foi verificado e o que não foi.
3. Espere o CI ficar verde. Um mantenedor revisa e faz o merge.

Ao contribuir, você concorda que sua contribuição é licenciada sob a
[Apache-2.0](LICENSE), a licença do projeto, e em seguir o [código de conduta](CODE_OF_CONDUCT.md).
