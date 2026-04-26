import logging as _log
import os
from pathlib import Path
from urllib.parse import urlparse


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


def _parse_base_url_list(env_var: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in os.environ.get(env_var, "").split(","):
        url = raw.strip().rstrip("/")
        if not url or url in seen:
            continue
        result.append(url)
        seen.add(url)
    return result


def _is_true(env_var: str, default: str = "false") -> bool:
    return os.environ.get(env_var, default).strip().lower() in ("1", "true", "yes", "on")


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
# Можно задать несколько через RELAY_BASE_URLS через запятую. Старый RELAY_BASE_URL
# поддерживается как первый relay для обратной совместимости.
RELAY_BASE_URLS = _parse_base_url_list("RELAY_BASE_URLS")
_legacy_relay_url = os.environ.get("RELAY_BASE_URL", "").strip().rstrip("/")
if _legacy_relay_url and _legacy_relay_url not in RELAY_BASE_URLS:
    RELAY_BASE_URLS.insert(0, _legacy_relay_url)
RELAY_BASE_URL = RELAY_BASE_URLS[0] if RELAY_BASE_URLS else ""
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

# Generic extractor accepts arbitrary public HTTP(S) pages. Keep disabled by
# default to reduce SSRF surface; enable only for trusted/private deployments.
ALLOW_GENERIC_URLS = _is_true("ALLOW_GENERIC_URLS")

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

    for name, values in {
        "PUBLIC_BASE_URL": [PUBLIC_BASE_URL] if PUBLIC_BASE_URL else [],
        "DIRECT_BASE_URL": [DIRECT_BASE_URL] if DIRECT_BASE_URL else [],
        "RELAY_BASE_URLS": RELAY_BASE_URLS,
    }.items():
        for url in values:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                _logger.warning("%s contains invalid URL and may not work: %s", name, url)
            elif parsed.scheme != "https":
                _logger.warning("%s uses plain HTTP; download tokens should be exposed only over HTTPS: %s", name, url)


validate_config()
