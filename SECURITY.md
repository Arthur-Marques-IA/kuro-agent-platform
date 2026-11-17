# Política de segurança

## Versões que recebem correção

| Versão | Correções de segurança |
|---|---|
| a mais recente lançada (ver [releases](https://github.com/Arthur-Marques-IA/kuro-agent-platform/releases)) | sim |
| anteriores | não: atualize pelo [roteiro de migração](docs/migracao.md) |

A versão no ar aparece em `GET /health` (`version`) e no `kuro health`.

## Como relatar uma falha

**Não abra uma issue pública.** Use o relato privado do GitHub: na aba
[Security](https://github.com/Arthur-Marques-IA/kuro-agent-platform/security) do repositório,
**Report a vulnerability**. Só quem mantém o projeto vê o relato.

Ajuda muito se o relato trouxer:

- a versão (`GET /health`) e como o serviço está exposto (compose local, profile `tls` ou `ip`);
- o que um atacante consegue fazer e de onde (sem chave, com a chave de `runtime`, com a de `admin`);
- os passos para reproduzir, de preferência com uma requisição de exemplo.

Quem mantém o projeto responde assim que possível. A correção sai numa versão nova, com a falha descrita nas
[notas de versão](docs/notas-de-versao.md) depois que houver como atualizar, e com o crédito a
quem relatou, se a pessoa quiser.

## O que está no escopo

- A API do agent-service, a autenticação por chave e os escopos `runtime` e `admin`.
- O controle de egress das tools e o validador de tools `kind="python"`.
- O servidor MCP, a CLI, o console e os arquivos de deploy deste repositório.

## Limites conhecidos, que não são falha

Estão descritos em [Segurança e limitações atuais](docs/operacao.md#segurança-e-limitações-atuais)
e no [ROADMAP](ROADMAP.md#fora-de-propósito). Os principais:

- **Tools `kind="python"` não rodam em sandbox.** O namespace restrito reduz a superfície, mas não
  isola memória, CPU nem o processo. Mantenha `CUSTOM_PYTHON_TOOLS_ENABLED` desligado em produção.
- **DNS rebinding contra o controle de egress.** Fechar isso é papel de um firewall de egress no
  ambiente.
- **O console não tem usuários nem papéis.** Ele chama a API com a chave de `admin`; publicado pelo
  profile `tls`, fica atrás de um `basic_auth` único.
- **Sem chave de API e com `ALLOW_OPEN=1`, o serviço fica aberto** a quem alcança a porta. O
  `docker-compose.yml` liga isso por padrão, com a porta só no loopback.
