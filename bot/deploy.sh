#!/usr/bin/env bash
# Сборка и запуск Docker-контейнеров yt-dlp бота.
#
# Использование:
#   ./deploy.sh                — полная пересборка + запуск (все контейнеры; nginx-ssl требует COMPOSE_PROFILES=ssl)
#   ./deploy.sh build          — только сборка всех контейнеров
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

# Подключаем ssl profile если COMPOSE_PROFILES=ssl задан в .env или окружении
if [ -f .env ]; then
    # shellcheck disable=SC1091
    # NB: sourcing .env is standard Docker practice but may execute arbitrary code
    . ./.env 2>/dev/null || true
fi
PROFILES=""
case "${COMPOSE_PROFILES:-}" in
    *ssl*) PROFILES="--profile ssl" ;;
esac
COMPOSE="docker compose $PROFILES"

# Автоматически включаем ssl profile если аргумент — nginx-ssl
if [ "${1:-}" = "nginx-ssl" ] || [ "${2:-}" = "nginx-ssl" ]; then
    COMPOSE="docker compose --profile ssl"
fi

case "${1:-all}" in
    build)
        echo "Building with GIT_COMMIT=$GIT_COMMIT ..."
        if [ -n "${2:-}" ]; then
            $COMPOSE build "$2"
        else
            $COMPOSE build
        fi
        ;;
    up)
        $COMPOSE up -d
        ;;
    restart)
        if [ -n "${2:-}" ]; then
            $COMPOSE restart "$2"
        else
            $COMPOSE restart
        fi
        ;;
    down)
        $COMPOSE down
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
                $COMPOSE logs -f
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
        $COMPOSE up -d --build
        echo "Done. Version commit: $GIT_COMMIT"
        ;;
    *)
        echo "Usage: $0 {build|up|restart|down|logs} [service] [tail-lines]" >&2
        exit 1
        ;;
esac
