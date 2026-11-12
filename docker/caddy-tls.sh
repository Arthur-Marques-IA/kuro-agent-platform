#!/bin/sh
# TLS na frente do agent-service e do console (profile `tls`).
#
# O console injeta a chave admin em toda chamada que faz à API (o BFF do Next.js
# guarda a chave no servidor). Publicado sem barreira, quem achasse o domínio do
# console teria acesso total: agentes, traces, conversas e credenciais. Por isso o
# Caddyfile põe basic_auth no bloco do console, e este script só sobe o Caddy com
# usuário e hash definidos.
#
# A checagem fica aqui, e não num `${VAR:?}` do docker-compose.yml, porque o
# Compose interpola o arquivo inteiro antes de olhar os profiles: um `:?` no
# serviço `caddy` faria até o `docker compose up -d` sem profile nenhum falhar.
set -eu

recusar() {
	echo "kuro: recusei subir o profile tls: $1" >&2
	echo "kuro: gere o hash (bcrypt) com: docker run --rm caddy:2-alpine caddy hash-password --plaintext 'sua-senha'" >&2
	echo "kuro: e ponha no .env, com o hash entre aspas simples (o \$ do bcrypt não pode ser interpolado):" >&2
	echo "kuro:   CONSOLE_BASIC_AUTH_USER=admin" >&2
	echo "kuro:   CONSOLE_BASIC_AUTH_HASH='\$2a\$14\$...'" >&2
	echo "kuro: depois, docker compose --profile tls up -d de novo. Detalhes em docs/operacao.md." >&2
	sleep 30 # sem isso o restart do Docker repete a mensagem sem parar
	exit 1
}

if [ -z "${CONSOLE_BASIC_AUTH_USER:-}" ] || [ -z "${CONSOLE_BASIC_AUTH_HASH:-}" ]; then
	recusar "CONSOLE_BASIC_AUTH_USER e CONSOLE_BASIC_AUTH_HASH são obrigatórias (o console usa a chave admin e não pode ficar aberto)."
fi

# Um hash bcrypt tem 60 caracteres e começa com $2a$, $2b$ ou $2y$. Se não tem essa
# cara, o mais provável é que o `$` tenha sido comido na interpolação do .env.
case "$CONSOLE_BASIC_AUTH_HASH" in
	'$2a$'* | '$2b$'* | '$2y$'*) formato_ok=1 ;;
	*) formato_ok=0 ;;
esac
if [ "$formato_ok" -ne 1 ] || [ "${#CONSOLE_BASIC_AUTH_HASH}" -ne 60 ]; then
	recusar "CONSOLE_BASIC_AUTH_HASH não parece um hash bcrypt (60 caracteres começando com \$2a\$). Faltaram as aspas simples no .env? Sem elas o Compose interpreta cada \$ como variável. Não ponha a senha em texto claro aqui."
fi

exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
