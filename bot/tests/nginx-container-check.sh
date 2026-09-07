#!/usr/bin/env bash
set -euo pipefail
name="ytdlp-nginx-check-${RANDOM}"
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
docker run -d --name "$name" --add-host ytdlp-bot:127.0.0.1 \
    -e SSLIP_DOMAIN=example.com -e ENABLE_CERTBOT=false ytdlp-nginx:test >/dev/null
ready=false
for _ in $(seq 1 30); do
    if docker exec "$name" wget -qO- http://127.0.0.1:8081/health >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 1
done
if [ "$ready" != true ]; then
    docker logs "$name"
    exit 1
fi
docker exec "$name" nginx -t
docker exec "$name" sh -c 'openssl s_client -connect 127.0.0.1:1443 -servername example.com -brief </dev/null'
