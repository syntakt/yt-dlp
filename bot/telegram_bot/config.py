import logging as _log
import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import urlparse


_logger = _log.getLogger(__name__)


def _parse_int(
    env_var: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = os.environ.get(env_var, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        _logger.warning("%s=%r is not an integer; using %d", env_var, raw, default)
        return default
    if minimum is not None and value < minimum:
        _logger.warning("%s=%d is below %d; using %d", env_var, value, minimum, default)
        return default
    if maximum is not None and value > maximum:
        _logger.warning("%s=%d is above %d; using %d", env_var, value, maximum, default)
        return default
    return value


def _parse_float(
    env_var: str,
    default: float,
    *,
    minimum: float | None = None,
) -> float:
    raw = os.environ.get(env_var, str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        _logger.warning("%s=%r is not a number; using %s", env_var, raw, default)
        return default
    if minimum is not None and value < minimum:
        _logger.warning("%s=%s is below %s; using %s", env_var, value, minimum, default)
        return default
    return value


def _parse_int_list(env_var: str) -> list[int]:
    result = []
    for x in os.environ.get(env_var, "").split(","):
        x = x.strip()
        if x:
            try:
                result.append(int(x))
            except ValueError:
                _logger.warning("Ignoring non-integer %s entry: %r", env_var, x)
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
MAX_FILE_SIZE_MB = _parse_int("MAX_FILE_SIZE_MB", 10240, minimum=1)  # 10 ГБ
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# Локальный Telegram Bot API сервер (снимает лимит 50 МБ → до 2 ГБ)
# При использовании docker-compose значение: http://telegram-bot-api:8081
LOCAL_API_SERVER = os.environ.get("LOCAL_API_SERVER", "http://telegram-bot-api:8081")

# Concurrency
MAX_CONCURRENT_DOWNLOADS = _parse_int("MAX_CONCURRENT_DOWNLOADS", 3, minimum=1, maximum=32)
MAX_CONCURRENT_DOWNLOADS_PER_USER = _parse_int(
    "MAX_CONCURRENT_DOWNLOADS_PER_USER", 1, minimum=1, maximum=8
)

# Database
DB_PATH = Path(os.environ.get("DB_PATH", "/data/bot.db"))

# Limits
MAX_HISTORY_PER_USER = _parse_int("MAX_HISTORY_PER_USER", 50, minimum=0, maximum=10000)
DOWNLOAD_TIMEOUT = _parse_int("DOWNLOAD_TIMEOUT", 3600, minimum=60)  # seconds
INFO_TIMEOUT = _parse_int("INFO_TIMEOUT", 120, minimum=15, maximum=900)

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
HTTP_PORT = _parse_int("HTTP_PORT", 8080, minimum=1, maximum=65535)
# TTL ссылки: по умолчанию 1 час (файл удаляется после скачивания ИЛИ по истечении TTL)
FILE_TTL_SECONDS = max(300, int(_parse_float("FILE_TTL_HOURS", 1.0, minimum=0.0) * 3600))
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
MAX_PLAYLIST_ITEMS = _parse_int("MAX_PLAYLIST_ITEMS", 10, minimum=1, maximum=100)
# Суммарный лимит плейлиста и неприкосновенный резерв диска.
MAX_PLAYLIST_TOTAL_MB = _parse_int(
    "MAX_PLAYLIST_TOTAL_MB", MAX_FILE_SIZE_MB, minimum=1
)
MAX_PLAYLIST_TOTAL_BYTES = MAX_PLAYLIST_TOTAL_MB * 1024 * 1024
MIN_FREE_DISK_MB = _parse_int("MIN_FREE_DISK_MB", 1024, minimum=128)
MIN_FREE_DISK_BYTES = MIN_FREE_DISK_MB * 1024 * 1024
# Аудио в формате OPUS — ремукс без перекодирования, значительно быстрее MP3
ALLOW_OPUS = os.environ.get("ALLOW_OPUS", "true").lower() == "true"
# Аудио в формате WAV — несжатый PCM, максимальное качество, большие файлы
ALLOW_WAV = os.environ.get("ALLOW_WAV", "false").lower() == "true"
# aria2c: параллельные соединения ускоряют загрузку больших файлов по HTTP
# Требует aria2 в системе (уже установлен в Dockerfile)
USE_ARIA2C = os.environ.get("USE_ARIA2C", "true").lower() == "true"
# SponsorBlock: убирать рекламные вставки из YouTube-видео
USE_SPONSORBLOCK = os.environ.get("USE_SPONSORBLOCK", "false").lower() == "true"

# ── BitTorrent (magnet + .torrent) ────────────────────────────────────────────
# По умолчанию ВЫКЛЮЧЕНО (opt-in): торренты расширяют поверхность атаки.
# Скачивание идёт через aria2c (уже в образе). Модель безопасности:
#   • Входящий порт НЕ публикуется и НЕ проксируется nftables — недостижим извне.
#   • Не сидируем (seed-time=0) → только исходящие соединения, входящий порт не нужен.
#   • DHT/LPD/PEX по умолчанию выключены → нет UDP-listener'ов и анонсов себя.
#   • aria2c-подпроцесс НЕ наследует Python-SSRF-guard — трекеры проверяем сами,
#     а доступ к приватной сети закрывается nftables egress (см. TELEGRAM_BOT.md).
ALLOW_TORRENTS = _is_true("ALLOW_TORRENTS")
# Агрегатный лимит размера всей раздачи (по умолчанию как у плейлиста).
TORRENT_MAX_TOTAL_MB = _parse_int("TORRENT_MAX_TOTAL_MB", MAX_PLAYLIST_TOTAL_MB, minimum=1)
TORRENT_MAX_TOTAL_BYTES = TORRENT_MAX_TOTAL_MB * 1024 * 1024
# Таймаут всей торрент-загрузки (торренты медленные — дефолт 2 часа).
TORRENT_TIMEOUT = _parse_int("TORRENT_TIMEOUT", 7200, minimum=60)
# Ограничение числа пиров и скорости (0 = без лимита скорости).
TORRENT_MAX_PEERS = _parse_int("TORRENT_MAX_PEERS", 50, minimum=1, maximum=1000)
TORRENT_DOWNLOAD_LIMIT = _parse_int("TORRENT_DOWNLOAD_LIMIT", 0, minimum=0)
# Фиксированный listen-порт (чтобы навесить DROP в nftables). НЕ публикуется наружу.
TORRENT_LISTEN_PORT = _parse_int("TORRENT_LISTEN_PORT", 51413, minimum=1024, maximum=65535)
# DHT: по умолчанию выключен (безопаснее). При выключенном DHT magnet без
# трекеров (&tr=) может не получить метаданные.
TORRENT_ENABLE_DHT = _is_true("TORRENT_ENABLE_DHT")
# Отдавать только медиа-файлы (video/audio) из раздачи — в духе назначения бота.
TORRENT_MEDIA_ONLY = _is_true("TORRENT_MEDIA_ONLY", "true")
# Максимальный размер самого .torrent-файла, который принимаем документом (байт).
TORRENT_FILE_MAX_BYTES = _parse_int("TORRENT_FILE_MAX_MB", 2, minimum=1, maximum=64) * 1024 * 1024

# Generic extractor accepts arbitrary public HTTP(S) pages. Keep disabled by
# default to reduce SSRF surface; enable only for trusted/private deployments.
ALLOW_GENERIC_URLS = _is_true("ALLOW_GENERIC_URLS")
SSRF_PROTECTION = _is_true("SSRF_PROTECTION", "true")
# Прокси резолвит целевые имена сам, поэтому должен иметь свою SSRF-фильтрацию.
TRUST_PROXY_FOR_SSRF = _is_true("TRUST_PROXY_FOR_SSRF")

# Proxy (optional)
PROXY_URL = os.environ.get("PROXY_URL", "")

# Cookie file path for age-restricted content
COOKIES_FILE = os.environ.get("COOKIES_FILE", "")

# Registration mode: "open" (anyone can register) or "closed" (admin approves only)
REGISTRATION_MODE = os.environ.get("REGISTRATION_MODE", "closed")

# Авто-удаление сообщений бота после завершения загрузки (секунды).
# 0 = выключено. Пример: AUTO_DELETE_SECONDS=300 → удаляет через 5 минут.
AUTO_DELETE_SECONDS = _parse_int("AUTO_DELETE_SECONDS", 0, minimum=0)

# Webhook-режим (вместо polling). Требует публичного HTTPS-адреса.
# WEBHOOK_URL — публичный URL бота (без trailing slash), например https://example.com
# Если пусто — используется polling.
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_PORT = _parse_int("WEBHOOK_PORT", 8443, minimum=1, maximum=65535)
WEBHOOK_SECRET_TOKEN = os.environ.get("WEBHOOK_SECRET_TOKEN", "")
if WEBHOOK_URL and not WEBHOOK_SECRET_TOKEN:
    _log.getLogger(__name__).warning("WEBHOOK_URL is set but WEBHOOK_SECRET_TOKEN is empty — webhook requests are not verified")

# Мониторинг диска: процент заполнения, при котором слать алерт администраторам.
# 0 = выключено.
DISK_ALERT_THRESHOLD = _parse_int("DISK_ALERT_THRESHOLD", 80, minimum=0, maximum=100)

# Файловый HTTP-сервер
FS_RATE_LIMIT = _parse_int("FS_RATE_LIMIT", 30, minimum=1, maximum=10000)


def validate_config() -> None:
    """Validate cross-setting constraints that cannot be parsed independently."""
    global MAX_CONCURRENT_DOWNLOADS_PER_USER

    if MAX_CONCURRENT_DOWNLOADS_PER_USER > MAX_CONCURRENT_DOWNLOADS:
        _logger.warning(
            "MAX_CONCURRENT_DOWNLOADS_PER_USER=%d exceeds global limit %d; clamping",
            MAX_CONCURRENT_DOWNLOADS_PER_USER,
            MAX_CONCURRENT_DOWNLOADS,
        )
        MAX_CONCURRENT_DOWNLOADS_PER_USER = MAX_CONCURRENT_DOWNLOADS

    if REGISTRATION_MODE not in {"open", "closed"}:
        raise RuntimeError("REGISTRATION_MODE must be 'open' or 'closed'")

    if ALLOW_TORRENTS:
        import shutil as _shutil
        if not _shutil.which("aria2c"):
            _logger.warning(
                "ALLOW_TORRENTS=true, но aria2c не найден в PATH — торренты работать не будут"
            )
        _logger.warning(
            "ALLOW_TORRENTS=true — убедитесь, что торрент-порт %d НЕ проброшен наружу "
            "и настроены nftables egress-правила (см. TELEGRAM_BOT.md)",
            TORRENT_LISTEN_PORT,
        )

    if PROXY_URL and SSRF_PROTECTION and not TRUST_PROXY_FOR_SSRF:
        raise RuntimeError(
            "PROXY_URL with SSRF_PROTECTION requires TRUST_PROXY_FOR_SSRF=true "
            "and an SSRF-filtering proxy"
        )

    if WEBHOOK_URL:
        parsed = urlparse(WEBHOOK_URL)
        if parsed.scheme != "https" or not parsed.netloc:
            raise RuntimeError("WEBHOOK_URL must be a public HTTPS URL")
        if (
            len(WEBHOOK_SECRET_TOKEN) < 32
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", WEBHOOK_SECRET_TOKEN)
        ):
            raise RuntimeError(
                "WEBHOOK_SECRET_TOKEN must be 32-256 chars: A-Z, a-z, 0-9, '_' or '-'"
            )

    for name, values in {
        "PUBLIC_BASE_URL": [PUBLIC_BASE_URL] if PUBLIC_BASE_URL else [],
        "DIRECT_BASE_URL": [DIRECT_BASE_URL] if DIRECT_BASE_URL else [],
        "RELAY_BASE_URLS": RELAY_BASE_URLS,
    }.items():
        for url in values:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise RuntimeError(f"{name} contains invalid URL: {url}")
            if parsed.scheme != "https":
                hostname = parsed.hostname or ""
                try:
                    is_loopback = ipaddress.ip_address(hostname).is_loopback
                except ValueError:
                    is_loopback = hostname == "localhost"
                if not is_loopback:
                    raise RuntimeError(
                        f"{name} must use HTTPS because it carries bearer download tokens: {url}"
                    )


validate_config()
