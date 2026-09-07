#!/usr/bin/env bash
# Сборка и запуск Docker-контейнеров yt-dlp бота.
#
# Использование:
#   ./deploy.sh                — полная пересборка + запуск базовых контейнеров
#                                (+ cloudflared при ENABLE_CLOUDFLARED=true,
#                                   + nginx-ssl при COMPOSE_PROFILES=ssl)
#   ./deploy.sh build          — только сборка включённых контейнеров
#   ./deploy.sh build nginx-ssl— пересборка только nginx-ssl
#   ./deploy.sh up             — запуск без пересборки
#   ./deploy.sh restart        — перезапуск всех контейнеров
#   ./deploy.sh restart nginx-ssl — перезапуск только nginx-ssl
#   ./deploy.sh down           — остановка всех контейнеров
#   ./deploy.sh logs           — логи всех контейнеров (follow)
#   ./deploy.sh logs bot       — логи бота (follow)
#   ./deploy.sh logs nginx     — логи nginx-ssl (follow)
#   ./deploy.sh logs nginx 50  — последние 50 строк nginx-ssl
#   ./deploy.sh renew-cert [--dry-run] — ручное продление внутри nginx-ssl
set -euo pipefail
cd "$(dirname "$0")"

# Renewal uses the running container's environment and certificate volumes.
# No build, Compose dotenv parsing or host Certbot installation is needed.
if [ "${1:-}" = "renew-cert" ]; then
    shift
    if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--dry-run" ]; }; then
        echo "Использование: $0 renew-cert [--dry-run]" >&2
        exit 1
    fi
    exec docker exec nginx-ssl /renew-certificate.sh "$@"
fi

export GIT_COMMIT BUILD_DATE
GIT_COMMIT=$(git rev-parse --short=7 HEAD 2>/dev/null || echo "dev")
BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Экстракторы yt-dlp ломаются на стороне сайтов, поэтому важно собирать образ
# из свежего чекаута. Показываем версию и отставание от upstream, если он настроен.
show_source_freshness() {
    local version behind
    version=$(python3 -c 'import sys; sys.path.insert(0, ".."); from yt_dlp.version import __version__; print(__version__)' 2>/dev/null || echo "unknown")
    echo "yt-dlp в чекауте: $version (commit $GIT_COMMIT)"
    if git rev-parse --verify --quiet upstream/master >/dev/null 2>&1; then
        behind=$(git rev-list --count HEAD..upstream/master 2>/dev/null || echo 0)
        if [ "${behind:-0}" -gt 0 ]; then
            echo "⚠ Отставание от upstream/master: $behind коммит(ов)."
            echo "  Обновиться: git fetch upstream && git merge upstream/master"
        fi
    fi
}

read_env_value() {
    # Let Compose handle quoting, interpolation, comments and duplicate keys.
    # Only these non-secret switches may be returned; never dump the environment.
    local key="$1" line
    case "$key" in
        COMPOSE_PROFILES|ENABLE_CLOUDFLARED|ENABLE_CLOUDFLARE_QUICK_TUNNEL|ENABLE_POT_PROVIDER) ;;
        *) return 1 ;;
    esac
    [ -f .env ] || return 0
    docker compose config --environment | while IFS= read -r line; do
        case "$line" in
            "$key="*) printf '%s' "${line#*=}" ;;
        esac
    done
}

is_true() {
    case "${1:-}" in
        1|true|TRUE|yes|YES|on|ON) return 0 ;;
        *) return 1 ;;
    esac
}

has_profile() {
    local requested=",${REQUESTED_COMPOSE_PROFILES// /,},"
    case "$requested" in
        *,"$1",*) return 0 ;;
        *) return 1 ;;
    esac
}

REQUESTED_COMPOSE_PROFILES="${COMPOSE_PROFILES:-$(read_env_value COMPOSE_PROFILES)}"
ENABLE_CLOUDFLARED="${ENABLE_CLOUDFLARED:-$(read_env_value ENABLE_CLOUDFLARED)}"
ENABLE_CLOUDFLARED="${ENABLE_CLOUDFLARED:-false}"
ENABLE_CLOUDFLARE_QUICK_TUNNEL="${ENABLE_CLOUDFLARE_QUICK_TUNNEL:-$(read_env_value ENABLE_CLOUDFLARE_QUICK_TUNNEL)}"
ENABLE_CLOUDFLARE_QUICK_TUNNEL="${ENABLE_CLOUDFLARE_QUICK_TUNNEL:-false}"
ENABLE_POT_PROVIDER="${ENABLE_POT_PROVIDER:-$(read_env_value ENABLE_POT_PROVIDER)}"
ENABLE_POT_PROVIDER="${ENABLE_POT_PROVIDER:-false}"

ACTIVE_PROFILES=()
if has_profile ssl || [ "${1:-}" = "nginx-ssl" ] || [ "${2:-}" = "nginx-ssl" ]; then
    ACTIVE_PROFILES+=(ssl)
fi

if has_profile pot || is_true "$ENABLE_POT_PROVIDER" || [ "${1:-}" = "bgutil-pot" ] || [ "${2:-}" = "bgutil-pot" ]; then
    ACTIVE_PROFILES+=(pot)
fi

if is_true "$ENABLE_CLOUDFLARED"; then
    ACTIVE_PROFILES+=(cloudflare)
elif has_profile cloudflare; then
    echo "ENABLE_CLOUDFLARED=false — профиль cloudflare из COMPOSE_PROFILES игнорируется"
fi

if [ "${#ACTIVE_PROFILES[@]}" -gt 0 ]; then
    COMPOSE_PROFILES="$(IFS=,; echo "${ACTIVE_PROFILES[*]}")"
else
    COMPOSE_PROFILES=""
fi
export COMPOSE_PROFILES ENABLE_CLOUDFLARED ENABLE_CLOUDFLARE_QUICK_TUNNEL ENABLE_POT_PROVIDER

COMPOSE=(docker compose)

case "${1:-all}" in
    build)
        show_source_freshness
        echo "Building with GIT_COMMIT=$GIT_COMMIT ..."
        if [ -n "${2:-}" ]; then
            "${COMPOSE[@]}" build --pull "$2"
        else
            "${COMPOSE[@]}" build --pull
        fi
        ;;
    up)
        "${COMPOSE[@]}" up -d
        ;;
    restart)
        if [ -n "${2:-}" ]; then
            "${COMPOSE[@]}" restart "$2"
        else
            "${COMPOSE[@]}" restart
        fi
        ;;
    down)
        "${COMPOSE[@]}" down
        ;;
    logs)
        case "${2:-all}" in
            bot)
                if [ -n "${3:-}" ]; then
                    docker logs --tail "$3" ytdlp-bot
                else
                    docker logs -f ytdlp-bot
                fi
                ;;
            nginx|nginx-ssl)
                if [ -n "${3:-}" ]; then
                    docker logs --tail "$3" nginx-ssl
                else
                    docker logs -f nginx-ssl
                fi
                ;;
            tunnel|cloudflared)
                if ! is_true "$ENABLE_CLOUDFLARED"; then
                    echo "cloudflared отключён: ENABLE_CLOUDFLARED=false"
                    exit 0
                fi
                if [ -n "${3:-}" ]; then
                    docker logs --tail "$3" cloudflared
                else
                    docker logs -f cloudflared
                fi
                ;;
            api|telegram-bot-api)
                if [ -n "${3:-}" ]; then
                    docker logs --tail "$3" telegram-bot-api
                else
                    docker logs -f telegram-bot-api
                fi
                ;;
            all|"")
                "${COMPOSE[@]}" logs -f
                ;;
            *)
                if [ -n "${3:-}" ]; then
                    docker logs --tail "$3" "$2"
                else
                    docker logs -f "$2"
                fi
                ;;
        esac
        ;;
    all|"")
        show_source_freshness
        echo "Building with GIT_COMMIT=$GIT_COMMIT ..."
        "${COMPOSE[@]}" build --pull
        "${COMPOSE[@]}" up -d
        echo "Done. Version commit: $GIT_COMMIT"
        ;;
    *)
        echo "Usage: $0 {build|up|restart|down|logs|renew-cert} [service|--dry-run] [tail-lines]" >&2
        exit 1
        ;;
esac
