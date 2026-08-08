# ── JS-рантайм для YouTube ────────────────────────────────────────────────────
# yt-dlp решает n/sig-челленджи YouTube внешним JS-движком. Без него клиент `web`
# выпадает из списка по умолчанию, часть форматов пропадает, а сам режим «без
# рантайма» объявлен deprecated. Берём готовый статический бинарник deno —
# единственный рантайм, включённый в yt-dlp по умолчанию (npm не нужен).
FROM denoland/deno:bin-2.9.5 AS deno

FROM python:3.12.13-slim

# Устанавливаем системные зависимости + gosu для безопасного снижения привилегий
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        aria2 \
        gosu \
    && rm -rf /var/lib/apt/lists/* \
    # Создаём пользователя botuser с фиксированным UID/GID=1000.
    # Фиксированный UID важен: владелец примонтированных папок на хосте
    # должен совпадать с UID внутри контейнера.
    && groupadd --gid 1000 botuser \
    && useradd  --uid 1000 --gid 1000 --no-create-home --shell /bin/false botuser

COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app

# ── Устанавливаем yt-dlp из локального исходного кода ──────────────────────
# При синхронизации master с upstream yt-dlp, сборка получит актуальную версию.
# Копируем только файлы, нужные для pip install (pyproject.toml + yt_dlp/)
COPY pyproject.toml README.md LICENSE /build/
COPY yt_dlp/ /build/yt_dlp/
# ВАЖНО: extras, а не голый `pip install /build`. У yt-dlp `dependencies = []`,
# все реальные зависимости лежат в extra `default`, и без него не ставились:
#   mutagen        — обложки в mp3/m4a/mp4/opus/flac (EmbedThumbnail)
#   pycryptodomex  — быстрый AES (часть сайтов и HLS AES-128)
#   brotli         — сжатие ответов, меньше трафика
#   websockets     — часть экстракторов и live
#   yt-dlp-ejs     — локальные solver-скрипты для JS-челленджей YouTube,
#                    без них пришлось бы разрешать загрузку --remote-components
# curl-cffi — подмена TLS-отпечатка (IMPERSONATE), нужна для Instagram/TikTok/X.
RUN pip install --no-cache-dir "/build[default,curl-cffi]" && rm -rf /build

# ── Устанавливаем зависимости бота ─────────────────────────────────────────
COPY bot/telegram_bot/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# ── PO Token provider (опционально) ────────────────────────────────────────
# Плагин обращается к контейнеру bgutil-pot (профиль `pot` в docker-compose) и
# добывает PO-токены — лечит «Sign in to confirm you're not a bot» на серверных
# IP. Сам по себе плагин ничего не делает, пока не задан POT_PROVIDER_URL.
# Версия плагина обязана совпадать с версией контейнера-провайдера.
RUN pip install --no-cache-dir bgutil-ytdlp-pot-provider==1.3.1

# ── Копируем код бота ──────────────────────────────────────────────────────
COPY bot/telegram_bot/ /app/

# ── Версия: записываем git commit в файл ───────────────────────────────────
# Передаётся автоматически через deploy.sh (рекомендуется).
# Fallback: RELEASE_GIT_HEAD из yt_dlp/version.py.
ARG GIT_COMMIT=
RUN if [ -n "${GIT_COMMIT}" ]; then \
      echo "${GIT_COMMIT}" > /app/.git_commit; \
    else \
      python -c "from yt_dlp.version import RELEASE_GIT_HEAD; print(RELEASE_GIT_HEAD[:7])" > /app/.git_commit 2>/dev/null || echo "dev" > /app/.git_commit; \
    fi

# ── Дата сборки: /status показывает возраст образа ─────────────────────────
# Экстракторы ломаются на стороне сайтов, поэтому образ нужно пересобирать
# регулярно; без этой метки админ не видел, насколько сборка протухла.
ARG BUILD_DATE=
RUN if [ -n "${BUILD_DATE}" ]; then \
      echo "${BUILD_DATE}" > /app/.build_date; \
    else \
      date -u +%Y-%m-%dT%H:%M:%SZ > /app/.build_date; \
    fi

RUN chmod +x /app/entrypoint.sh \
    # Папки создаются заранее; entrypoint исправляет владельца после монтирования томов
    && mkdir -p /downloads /data \
    && chown botuser:botuser /app /downloads /data

# НЕ ставим USER botuser здесь — entrypoint запускается от root,
# исправляет chown на смонтированных томах, затем exec gosu понижает до botuser.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "bot.py"]
