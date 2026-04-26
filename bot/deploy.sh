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
set -euo pipefail
cd "$(dirname "$0")"

export GIT_COMMIT
GIT_COMMIT=$(git rev-parse --short=7 HEAD 2>/dev/null || echo "dev")

read_env_value() {
    local key="$1"
    local line value

    [ -f .env ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}"
        case "$line" in
            ""|\#*) continue ;;
            export\ *) line="${line#export }" ;;
        esac
        case "$line" in
            "$key="*)
                value="${line#*=}"
                case "$value" in
                    \"*\") value="${value#\"}"; value="${value%\"}" ;;
                    \'*\') value="${value#\'}"; value="${value%\'}" ;;
                esac
                printf '%s' "$value"
                return 0
                ;;
        esac
    done < .env
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

ACTIVE_PROFILES=()
if has_profile ssl || [ "${1:-}" = "nginx-ssl" ] || [ "${2:-}" = "nginx-ssl" ]; then
    ACTIVE_PROFILES+=(ssl)
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
export COMPOSE_PROFILES ENABLE_CLOUDFLARED ENABLE_CLOUDFLARE_QUICK_TUNNEL

COMPOSE=(docker compose)

case "${1:-all}" in
    build)
        echo "Building with GIT_COMMIT=$GIT_COMMIT ..."
        if [ -n "${2:-}" ]; then
            "${COMPOSE[@]}" build "$2"
        else
            "${COMPOSE[@]}" build
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
        echo "Building with GIT_COMMIT=$GIT_COMMIT ..."
        "${COMPOSE[@]}" up -d --build
        echo "Done. Version commit: $GIT_COMMIT"
        ;;
    *)
        echo "Usage: $0 {build|up|restart|down|logs} [service] [tail-lines]" >&2
        exit 1
        ;;
esac
