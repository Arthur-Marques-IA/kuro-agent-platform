#!/bin/sh
# TLS na frente do agent-service (profile `tls`): confere a autenticação e sobe o Caddy.
#
# Este profile põe o serviço na internet pelo domínio. O `docker-compose.yml` liga o
# ALLOW_OPEN por padrão (para quem desenvolve, com a porta só no loopback), então um
# agent-service sem chave sobe aberto — e o Caddy o publicaria assim. Só publica com a
# autenticação ligada, a mesma verificação do profile `ip` (`caddy-ip.sh`).
set -eu

if [ "${KURO_TLS_ALLOW_OPEN:-}" != "1" ]; then
	health="$(wget -qO- -T 10 http://agent-service:8000/health 2>/dev/null || true)"
	case "$health" in
		*'"auth":"enabled"'* | *'"auth": "enabled"'*) ;;
		*)
			echo "kuro: recusei publicar o serviço pelo domínio: a autenticação está desligada (ou o agent-service não respondeu)." >&2
			echo "kuro: defina ADMIN_API_KEY e RUNTIME_API_KEY no .env (openssl rand -hex 32) e rode docker compose --profile tls up -d de novo." >&2
			echo "kuro: só para teste local, KURO_TLS_ALLOW_OPEN=1 pula esta verificação." >&2
			sleep 30 # sem isso o restart do Docker repete a mensagem sem parar
			exit 1
			;;
	esac
fi

exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
