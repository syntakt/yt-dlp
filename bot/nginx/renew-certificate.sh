#!/bin/sh
# Renewal inside nginx-ssl, called on startup or manually from the host.
# The HTTP-01 route must be prepared by the operator first.
set -eu
DOMAIN=${SSLIP_DOMAIN:-}
if [ -z "$DOMAIN" ]; then
    echo 'SSLIP_DOMAIN не задан. Этот скрипт выполняется внутри контейнера nginx-ssl.' >&2
    echo 'На хосте из bot/: ./deploy.sh renew-cert [--dry-run]' >&2
    echo 'Или: docker exec nginx-ssl /renew-certificate.sh [--dry-run]' >&2
    echo 'Если ошибка возникает внутри контейнера, задайте SSLIP_DOMAIN в bot/.env и пересоздайте nginx-ssl.' >&2
    exit 1
fi
if ! printf '%s\n' "$DOMAIN" | grep -Eq '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$'; then
    echo 'Некорректный SSLIP_DOMAIN' >&2
    exit 1
fi
run_certbot() {
    echo 'Проверка сертификата: без случайной задержки, ограничение времени — 300 секунд.'
    status=0
    timeout -k 10 300 certbot renew --non-interactive --cert-name "$DOMAIN" \
        --webroot -w /var/www/certbot --no-random-sleep-on-renew "$@" || status=$?
    case "$status" in
        124|137|143) echo 'Certbot прерван или превысил 300 секунд; действующий сертификат не заменён.' >&2 ;;
    esac
    return "$status"
}
if [ "$#" -gt 1 ]; then
    echo 'Использование: /renew-certificate.sh [--dry-run]' >&2
    exit 1
fi
case "${1:-}" in
    --dry-run)
        run_certbot --dry-run
        exit 0
        ;;
    '') ;;
    *) echo 'Использование: /renew-certificate.sh [--dry-run]' >&2; exit 1 ;;
esac
run_certbot
cp -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" /etc/nginx/ssl/fullchain.pem
cp -f "/etc/letsencrypt/live/$DOMAIN/privkey.pem" /etc/nginx/ssl/privkey.pem
chmod 600 /etc/nginx/ssl/privkey.pem
nginx -t
nginx -s reload
echo 'Сертификат установлен из Certbot; Nginx перечитал конфигурацию.'
