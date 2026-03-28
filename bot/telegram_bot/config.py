import logging as _log
import os
from pathlib import Path


def _parse_int_list(env_var: str) -> list[int]:
    result = []
    for x in os.environ.get(env_var, "").split(","):
        x = x.strip()
        if x:
            try:
                result.append(int(x))
            except ValueError:
                pass
    return result


# Bot configuration
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = _parse_int_list("ADMIN_IDS")

# Download settings
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "10240"))  # 10 ГБ
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# Локальный Telegram Bot API сервер (снимает лимит 50 МБ → до 2 ГБ)
# При использовании docker-compose значение: http://telegram-bot-api:8081
LOCAL_API_SERVER = os.environ.get("LOCAL_API_SERVER", "http://telegram-bot-api:8081")

# Concurrency
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "3"))

# Database
DB_PATH = Path(os.environ.get("DB_PATH", "/data/bot.db"))

# Limits
MAX_HISTORY_PER_USER = int(os.environ.get("MAX_HISTORY_PER_USER", "50"))
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "3600"))  # seconds (1 час для больших файлов)

# Файловый HTTP-сервер (вместо/вместе с отправкой файла в Telegram — даёт ссылку)
# PUBLIC_BASE_URL — публичный адрес, который видят пользователи (без trailing slash)
#   Пример: https://myserver.com  или  http://1.2.3.4:8080
#   Если пусто — только Telegram (старое поведение), кнопка «Ссылка» не появляется
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
# Прямая ссылка на сервер по IP (без Cloudflare Tunnel).
# Пример: http://1.2.3.4:8080
# Если задан — добавляется кнопка «Прямая ссылка (IP)» в меню доставки.
DIRECT_BASE_URL = os.environ.get("DIRECT_BASE_URL", "").rstrip("/")
# Relay-сервер — резервный путь для пользователей с заблокированным Cloudflare/IP.
# Второй сервер проксирует /info/ и /dl/ на основной бот-сервер.
# Пример: https://1-2-3-4.sslip.io:5443
RELAY_BASE_URL = os.environ.get("RELAY_BASE_URL", "").rstrip("/")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
# TTL ссылки: по умолчанию 1 час (файл удаляется после скачивания ИЛИ по истечении TTL)
FILE_TTL_SECONDS = max(300, int(float(os.environ.get("FILE_TTL_HOURS", "1")) * 3600))
# Секретный ключ для HMAC-подписи токенов файлового сервера (рекомендуется задать)
# Генерация: python3 -c "import secrets; print(secrets.token_hex(32))"
SERVER_SECRET = os.environ.get("SERVER_SECRET", "")
if not SERVER_SECRET:
    _log.getLogger(__name__).warning("SERVER_SECRET is not set — file tokens lack HMAC protection")

# Feature flags
ALLOW_PLAYLISTS = os.environ.get("ALLOW_PLAYLISTS", "true").lower() == "true"
ALLOW_AUDIO = os.environ.get("ALLOW_AUDIO", "true").lower() == "true"
ALLOW_SUBTITLES = os.environ.get("ALLOW_SUBTITLES", "true").lower() == "true"
# Максимальное количество видео в плейлисте (кнопки покажут half и full)
MAX_PLAYLIST_ITEMS = int(os.environ.get("MAX_PLAYLIST_ITEMS", "10"))
# Аудио в формате OPUS — ремукс без перекодирования, значительно быстрее MP3
ALLOW_OPUS = os.environ.get("ALLOW_OPUS", "true").lower() == "true"
# Аудио в формате WAV — несжатый PCM, максимальное качество, большие файлы
ALLOW_WAV = os.environ.get("ALLOW_WAV", "false").lower() == "true"
# aria2c: параллельные соединения ускоряют загрузку больших файлов по HTTP
# Требует aria2 в системе (уже установлен в Dockerfile)
USE_ARIA2C = os.environ.get("USE_ARIA2C", "true").lower() == "true"
# SponsorBlock: убирать рекламные вставки из YouTube-видео
USE_SPONSORBLOCK = os.environ.get("USE_SPONSORBLOCK", "false").lower() == "true"

# Proxy (optional)
PROXY_URL = os.environ.get("PROXY_URL", "")

# Cookie file path for age-restricted content
COOKIES_FILE = os.environ.get("COOKIES_FILE", "")

# Registration mode: "open" (anyone can register) or "closed" (admin approves only)
REGISTRATION_MODE = os.environ.get("REGISTRATION_MODE", "closed")

# Авто-удаление сообщений бота после завершения загрузки (секунды).
# 0 = выключено. Пример: AUTO_DELETE_SECONDS=300 → удаляет через 5 минут.
AUTO_DELETE_SECONDS = int(os.environ.get("AUTO_DELETE_SECONDS", "0"))

# Webhook-режим (вместо polling). Требует публичного HTTPS-адреса.
# WEBHOOK_URL — публичный URL бота (без trailing slash), например https://example.com
# Если пусто — используется polling.
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8443"))
WEBHOOK_SECRET_TOKEN = os.environ.get("WEBHOOK_SECRET_TOKEN", "")
if WEBHOOK_URL and not WEBHOOK_SECRET_TOKEN:
    _log.getLogger(__name__).warning("WEBHOOK_URL is set but WEBHOOK_SECRET_TOKEN is empty — webhook requests are not verified")

# Мониторинг диска: процент заполнения, при котором слать алерт администраторам.
# 0 = выключено.
DISK_ALERT_THRESHOLD = int(os.environ.get("DISK_ALERT_THRESHOLD", "80"))


def validate_config() -> None:
    """Validate configuration values and clamp to safe defaults."""
    global MAX_CONCURRENT_DOWNLOADS, MAX_FILE_SIZE_MB, MAX_FILE_SIZE_BYTES
    global HTTP_PORT, DOWNLOAD_TIMEOUT
    _logger = _log.getLogger(__name__)

    if MAX_CONCURRENT_DOWNLOADS <= 0:
        _logger.warning(
            "MAX_CONCURRENT_DOWNLOADS=%d is invalid, clamping to 1",
            MAX_CONCURRENT_DOWNLOADS,
        )
        MAX_CONCURRENT_DOWNLOADS = 1

    if MAX_FILE_SIZE_MB <= 0:
        _logger.warning(
            "MAX_FILE_SIZE_MB=%d is invalid, clamping to 1",
            MAX_FILE_SIZE_MB,
        )
        MAX_FILE_SIZE_MB = 1
        MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

    if not (1 <= HTTP_PORT <= 65535):
        _logger.warning(
            "HTTP_PORT=%d is out of valid range (1-65535), clamping to 8080",
            HTTP_PORT,
        )
        HTTP_PORT = 8080

    if DOWNLOAD_TIMEOUT <= 0:
        _logger.warning(
            "DOWNLOAD_TIMEOUT=%d is invalid, clamping to 60",
            DOWNLOAD_TIMEOUT,
        )
        DOWNLOAD_TIMEOUT = 60


validate_config()
