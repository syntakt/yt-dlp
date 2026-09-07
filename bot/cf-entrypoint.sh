#!/bin/sh
# Точка входа контейнера cloudflared.
# Named tunnel: просто запускает cloudflared с токеном.
# Quick tunnel: запускает cloudflared, извлекает URL из логов
#               и сохраняет в /cf-url/public_url для бота.

set -e

CF_URL_FILE="/cf-url/public_url"
CF_LOG="/cf-url/cf.pipe"

# ── Named Tunnel (CLOUDFLARE_TUNNEL_TOKEN задан) ──────────────────────────────
if [ -n "$TUNNEL_TOKEN" ]; then
    echo "[cloudflared] Named tunnel mode"
    exec cloudflared tunnel --no-autoupdate --metrics 127.0.0.1:2000 --edge-ip-version 4 run --token "$TUNNEL_TOKEN"
fi

case "${ENABLE_CLOUDFLARE_QUICK_TUNNEL:-false}" in
    1|true|TRUE|yes|YES|on|ON) ;;
    *)
        echo "[cloudflared] Quick Tunnel отключён: задайте CLOUDFLARE_TUNNEL_TOKEN или ENABLE_CLOUDFLARE_QUICK_TUNNEL=true"
        exit 1
        ;;
esac

# ── Quick Tunnel (без токена, URL меняется при перезапуске) ───────────────────
echo "[cloudflared] Quick tunnel mode — URL появится ниже (занимает ~10 сек)"

mkdir -p /cf-url
rm -f "$CF_LOG" /cf-url/cf.log /cf-url/ready_url
mkfifo "$CF_LOG"
# Перезаписываем старый URL до запуска cloudflared — бот увидит "STARTING"
# и не будет использовать stale URL из предыдущего сеанса.
# Используем атомарную запись: write→rename, чтобы бот не прочитал пустой файл.
echo "STARTING" > "${CF_URL_FILE}.tmp"
mv -f "${CF_URL_FILE}.tmp" "$CF_URL_FILE"
# Также убеждаемся, что файл видим по NFS/shared-volume
sync 2>/dev/null || true

# Запускаем cloudflared в фоне, логи пишем в файл
cloudflared tunnel --no-autoupdate \
    --metrics 127.0.0.1:2000 \
    --edge-ip-version 4 \
    --url "http://ytdlp-bot:${HTTP_PORT:-8080}" \
    >"$CF_LOG" 2>&1 &
CF_PID=$!

# Stream to Docker's bounded log driver; do not grow a persistent log file.
(
    while IFS= read -r line; do
        printf '%s\n' "$line"
        found_url=$(printf '%s\n' "$line" | grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' | head -1)
        if [ -n "$found_url" ]; then
            printf '%s\n' "$found_url" > "${CF_URL_FILE}.reader"
            mv -f "${CF_URL_FILE}.reader" "$CF_URL_FILE"
        fi
    done < "$CF_LOG"
) &
TAIL_PID=$!
trap 'rm -f /cf-url/ready_url; kill "$CF_PID" "$TAIL_PID" 2>/dev/null || true; exit 0' TERM INT

# Ищем URL в логах (cloudflared печатает его в первые ~15 сек)
FOUND=0
for _ in $(seq 1 90); do
    URL=$(grep -oE '^https://[a-zA-Z0-9-]+\.trycloudflare\.com$' "$CF_URL_FILE" 2>/dev/null || true)
    if [ -n "$URL" ]; then
        # Атомарная запись: write→rename, чтобы бот не прочитал частично записанный файл
        echo "$URL" > "${CF_URL_FILE}.tmp"
        mv -f "${CF_URL_FILE}.tmp" "$CF_URL_FILE"
        sync 2>/dev/null || true
        echo ""
        echo "╔══════════════════════════════════════════════════════════╗"
        echo "║  Cloudflare Quick Tunnel URL:                            ║"
        echo "║  $URL"
        echo "║                                                          ║"
        echo "║  Бот получит этот URL автоматически.                     ║"
        echo "║  URL изменится при следующем перезапуске cloudflared.    ║"
        echo "╚══════════════════════════════════════════════════════════╝"
        echo ""
        FOUND=1
        break
    fi
    sleep 2
done

if [ "$FOUND" -eq 0 ]; then
    echo "[cloudflared] WARNING: URL тоннеля не найден в логах за 3 минуты"
fi

# Publish readiness only while this generation is connected; mtime is a lease.
while kill -0 "$CF_PID" 2>/dev/null; do
    if [ "$FOUND" -eq 1 ] && wget -qO- http://127.0.0.1:2000/ready >/dev/null 2>&1; then
        echo "$URL" > /cf-url/ready_url.tmp
        mv -f /cf-url/ready_url.tmp /cf-url/ready_url
    else
        rm -f /cf-url/ready_url
    fi
    sleep 5
done
rm -f /cf-url/ready_url
kill "$TAIL_PID" 2>/dev/null || true
wait "$CF_PID"
