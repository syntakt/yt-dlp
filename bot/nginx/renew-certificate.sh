#!/bin/sh
# Manual renewal; the HTTP-01 route must be opened by the operator first.
set -eu
DOMAIN=${SSLIP_DOMAIN:?SSLIP_DOMAIN is required}
if ! printf '%s\n' "$DOMAIN" | grep -Eq '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$'; then
    echo 'Некорректный SSLIP_DOMAIN' >&2
    exit 1
fi
case "${1:-}" in
    --dry-run)
        exec certbot renew --cert-name "$DOMAIN" --webroot -w /var/www/certbot --dry-run
        ;;
    '') ;;
    *) echo 'Использование: /renew-certificate.sh [--dry-run]' >&2; exit 1 ;;
esac
certbot renew --cert-name "$DOMAIN" --webroot -w /var/www/certbot
cp -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" /etc/nginx/ssl/fullchain.pem
cp -f "/etc/letsencrypt/live/$DOMAIN/privkey.pem" /etc/nginx/ssl/privkey.pem
chmod 600 /etc/nginx/ssl/privkey.pem
nginx -t
nginx -s reload
echo 'Сертификат установлен из Certbot; Nginx перечитал конфигурацию.'
