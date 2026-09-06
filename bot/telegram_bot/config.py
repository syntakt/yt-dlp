import logging as _log
import ipaddress
import math
import os
import re
from pathlib import Path
from urllib.parse import urlparse


_logger = _log.getLogger(__name__)


def _env_str(env_var: str, default: str = "") -> str:
    """Значение переменной окружения; ПУСТАЯ строка считается «не задано».

    docker-compose подставляет пустую строку для `${VAR:-}`, когда ключа нет в
    .env, поэтому os.environ.get(VAR, default) возвращает "" вместо default.
    Ровно на этом бот уходил в цикл перезапусков: SPONSORBLOCK_MODE="" не
    проходил валидацию, хотя пользователь ничего не настраивал.
    """
    return (os.environ.get(env_var) or "").strip() or default


def _parse_int(
    env_var: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = _env_str(env_var, str(default))
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
    maximum: float | None = None,
) -> float:
    raw = _env_str(env_var, str(default))
    try:
        value = float(raw)
    except ValueError:
        _logger.warning("%s=%r is not a number; using %s", env_var, raw, default)
        return default
    if not math.isfinite(value):
        _logger.warning("%s must be finite; using %s", env_var, default)
        return default
    if maximum is not None and value > maximum:
        _logger.warning("%s is above %s; using %s", env_var, maximum, default)
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
    return _env_str(env_var, default).lower() in ("1", "true", "yes", "on")


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

# Жёсткий потолок Telegram на размер отправляемого файла: 2000 МБ у локального
# Bot API сервера и 50 МБ у публичного api.telegram.org. MAX_FILE_SIZE_MB может
# быть больше (такие файлы отдаются ссылкой), поэтому кнопка «Отправить в
# Telegram» показывается только для файлов, которые реально влезут.
TELEGRAM_UPLOAD_LIMIT_BYTES = (2000 if LOCAL_API_SERVER else 50) * 1024 * 1024

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
# Прямая ссылка на сервер (без Cloudflare Tunnel), через nginx-ssl.
# ТОЛЬКО https:// — ссылка несёт bearer-токен скачивания, validate_config()
# отклоняет http:// для всего, кроме loopback. Пример: https://1-2-3-4.sslip.io:7443
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
FILE_TTL_SECONDS = max(300, int(_parse_float("FILE_TTL_HOURS", 1.0, minimum=0.0, maximum=8760) * 3600))
# Секретный ключ для HMAC-подписи токенов файлового сервера (рекомендуется задать)
# Генерация: python3 -c "import secrets; print(secrets.token_hex(32))"
SERVER_SECRET = os.environ.get("SERVER_SECRET", "")
if not SERVER_SECRET:
    _log.getLogger(__name__).warning("SERVER_SECRET is not set — file tokens lack HMAC protection")

# Feature flags. Все булевы настройки читаются через _is_true(), чтобы 1/yes/on
# работали одинаково везде (раньше часть флагов сравнивалась с "true" буквально
# и ALLOW_AUDIO=1 молча означало «выключено»).
ALLOW_PLAYLISTS = _is_true("ALLOW_PLAYLISTS", "true")
ALLOW_AUDIO = _is_true("ALLOW_AUDIO", "true")
ALLOW_SUBTITLES = _is_true("ALLOW_SUBTITLES", "true")
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
ALLOW_OPUS = _is_true("ALLOW_OPUS", "true")
# Аудио в формате WAV — несжатый PCM, максимальное качество, большие файлы
ALLOW_WAV = _is_true("ALLOW_WAV")
# aria2c: параллельные соединения ускоряют загрузку больших файлов по HTTP
# Требует aria2 в системе (уже установлен в Dockerfile)
USE_ARIA2C = _is_true("USE_ARIA2C", "true")
# SponsorBlock: off | remove (вырезать) | mark (разметить главами).
# Старый USE_SPONSORBLOCK=true продолжает работать как remove.
USE_SPONSORBLOCK = _is_true("USE_SPONSORBLOCK")
SPONSORBLOCK_MODE = _env_str(
    "SPONSORBLOCK_MODE", "remove" if USE_SPONSORBLOCK else "off"
).lower()

# ── Постобработка медиа ───────────────────────────────────────────────────────
# Обложка вшивается mutagen'ом (mp3/m4a/mp4/opus/flac) — Telegram показывает её
# в плеере. Требует extra `default` у yt-dlp (см. bot/Dockerfile).
EMBED_THUMBNAIL = _is_true("EMBED_THUMBNAIL", "true")
# Теги (исполнитель/название/описание) через FFmpegMetadata
EMBED_METADATA = _is_true("EMBED_METADATA", "true")
# Главы видео в контейнер (работает вместе с EMBED_METADATA)
EMBED_CHAPTERS = _is_true("EMBED_CHAPTERS", "true")
# Стримы качать с начала эфира, а не с текущего момента
LIVE_FROM_START = _is_true("LIVE_FROM_START")
# Кнопка «✂️ Отрывок»: скачать только заданный интервал видео
ALLOW_CLIPS = _is_true("ALLOW_CLIPS", "true")
# Максимальная длительность отрывка (секунды), защита от «отрежь мне 10 часов»
MAX_CLIP_SECONDS = _parse_int("MAX_CLIP_SECONDS", 7200, minimum=10)
# Кнопка «🔖 По главам»: разбить видео на файлы по главам (может дать много файлов)
ALLOW_SPLIT_CHAPTERS = _is_true("ALLOW_SPLIT_CHAPTERS")

# ── JavaScript-рантайм (обязателен для YouTube) ───────────────────────────────
# yt-dlp решает n/sig-челленджи YouTube внешним JS-движком. Без него клиент `web`
# исключается из списка по умолчанию и часть форматов пропадает; сам режим
# «без рантайма» объявлен deprecated. В образ ставится deno (единственный
# рантайм, включённый в yt-dlp по умолчанию).
# Поддерживаются: deno, node, quickjs, bun. Пусто — оставить дефолт yt-dlp.
JS_RUNTIMES = [
    r.strip().lower() for r in _env_str("JS_RUNTIMES", "deno").split(",") if r.strip()
]

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
# External downloaders cannot inherit Python's network policy. Only opt in
# after configuring host/container egress filtering, including IPv6.
TRUST_EXTERNAL_NETWORK_FOR_SSRF = _is_true("TRUST_EXTERNAL_NETWORK_FOR_SSRF")

# ── Impersonation (curl_cffi) ─────────────────────────────────────────────────
# Подмена TLS/JA3-отпечатка под настоящий браузер — снимает блокировки на
# Instagram/TikTok/X. Примеры: chrome, chrome:windows-10, safari, edge.
# ⚠ curl_cffi ходит через libcurl (C) и НЕ проходит через socket.getaddrinfo,
#   то есть обходит SSRF-guard бота ровно так же, как внешний прокси.
IMPERSONATE = os.environ.get("IMPERSONATE", "").strip()
TRUST_IMPERSONATE_FOR_SSRF = _is_true("TRUST_IMPERSONATE_FOR_SSRF")

# ── PO Token (YouTube) ────────────────────────────────────────────────────────
# Лечит «Sign in to confirm you're not a bot» на серверных IP.
# POT_PROVIDER_URL — адрес bgutil-провайдера (контейнер профиля `pot`),
# например http://bgutil-pot:4416. Хост провайдера добавляется в allowlist
# SSRF-guard: иначе собственный guard заблокировал бы приватный 10.10.2.x.
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "").strip().rstrip("/")
# Ручной вариант без провайдера: токены в формате CLIENT.CONTEXT+TOKEN через запятую
YOUTUBE_PO_TOKEN = os.environ.get("YOUTUBE_PO_TOKEN", "").strip()
# Переопределение списка клиентов YouTube (например: default,-web или tv_simply)
YOUTUBE_PLAYER_CLIENT = os.environ.get("YOUTUBE_PLAYER_CLIENT", "").strip()

# Proxy (optional)
PROXY_URL = os.environ.get("PROXY_URL", "")

# Cookie file path for age-restricted content
COOKIES_FILE = os.environ.get("COOKIES_FILE", "")

# Registration mode: "open" (anyone can register) or "closed" (admin approves only)
REGISTRATION_MODE = _env_str("REGISTRATION_MODE", "closed").lower()

# Авто-удаление сообщений бота после завершения загрузки (секунды).
# 0 = выключено. Пример: AUTO_DELETE_SECONDS=300 → удаляет через 5 минут.
AUTO_DELETE_SECONDS = _parse_int("AUTO_DELETE_SECONDS", 0, minimum=0)

# Служебные сообщения (ошибки, «ссылка не распознана», лимиты, итоги пачек)
# живут отдельно от AUTO_DELETE_SECONDS: они не несут результата и чистятся
# всегда, иначе чат ими зарастает. 0 = не удалять.
TRANSIENT_DELETE_SECONDS = _parse_int("TRANSIENT_DELETE_SECONDS", 60, minimum=0)

# Удалять сообщение со ссылкой, когда истёк TTL файла: ссылка уже нерабочая,
# держать её в чате смысла нет. 0 = оставлять.
DELETE_EXPIRED_LINK_MESSAGES = _is_true("DELETE_EXPIRED_LINK_MESSAGES", "true")

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

# Анти-флуд: сколько «дорогих» действий (разбор ссылок, .torrent-файлы) один
# пользователь может инициировать в минуту. Ограничение на параллельные
# загрузки (MAX_CONCURRENT_DOWNLOADS_PER_USER) не мешало ставить в очередь
# неограниченное число запросов метаданных.
USER_ACTIONS_PER_MINUTE = _parse_int("USER_ACTIONS_PER_MINUTE", 20, minimum=1, maximum=600)


def ttl_label() -> str:
    """Человекочитаемый TTL ссылок: «2 ч», «45 м», «5 м».

    Раньше в UI везде стояло max(1, FILE_TTL_SECONDS // 3600), поэтому при
    FILE_TTL_HOURS=0.25 бот обещал «1 ч» вместо реальных 15 минут.
    """
    if FILE_TTL_SECONDS >= 3600:
        hours = FILE_TTL_SECONDS / 3600
        return f"{hours:.0f} ч" if abs(hours - round(hours)) < 0.05 else f"{hours:.1f} ч"
    return f"{max(1, FILE_TTL_SECONDS // 60)} м"


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
        if SSRF_PROTECTION and not TRUST_EXTERNAL_NETWORK_FOR_SSRF:
            raise RuntimeError(
                "ALLOW_TORRENTS with SSRF_PROTECTION requires "
                "TRUST_EXTERNAL_NETWORK_FOR_SSRF=true and external egress filtering"
            )
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

    # curl_cffi ходит мимо socket.getaddrinfo, поэтому SSRF-guard его не видит —
    # та же схема доверия, что и для внешнего прокси.
    if IMPERSONATE and SSRF_PROTECTION and not TRUST_IMPERSONATE_FOR_SSRF:
        raise RuntimeError(
            "IMPERSONATE uses curl_cffi, which bypasses the DNS-level SSRF guard. "
            "Set TRUST_IMPERSONATE_FOR_SSRF=true (and rely on nftables egress rules) "
            "or disable IMPERSONATE"
        )

    if SPONSORBLOCK_MODE not in {"off", "remove", "mark"}:
        raise RuntimeError("SPONSORBLOCK_MODE must be 'off', 'remove' or 'mark'")

    for runtime in JS_RUNTIMES:
        if runtime not in {"deno", "node", "quickjs", "bun"}:
            raise RuntimeError(
                f"JS_RUNTIMES contains unsupported runtime: {runtime} "
                "(supported: deno, node, quickjs, bun)"
            )
    if not JS_RUNTIMES:
        # Пустой список = не передаём js_runtimes в yt-dlp, а у него дефолт — deno
        _logger.info("JS_RUNTIMES is empty — using yt-dlp's own default (deno)")

    if POT_PROVIDER_URL:
        parsed = urlparse(POT_PROVIDER_URL)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise RuntimeError("POT_PROVIDER_URL must be an HTTP(S) base URL without credentials, query or fragment")
        _ = parsed.port

    if WEBHOOK_URL:
        parsed = urlparse(WEBHOOK_URL)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise RuntimeError("WEBHOOK_URL must be a public HTTPS URL")
        _ = parsed.port
        if (
            len(WEBHOOK_SECRET_TOKEN) < 32
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", WEBHOOK_SECRET_TOKEN)
        ):
            raise RuntimeError(
                "WEBHOOK_SECRET_TOKEN must be 32-256 chars: A-Z, a-z, 0-9, '_' or '-'"
            )

    if len(RELAY_BASE_URLS) > 16:
        raise RuntimeError("RELAY_BASE_URLS supports at most 16 relays")

    for name, values in {
        "PUBLIC_BASE_URL": [PUBLIC_BASE_URL] if PUBLIC_BASE_URL else [],
        "DIRECT_BASE_URL": [DIRECT_BASE_URL] if DIRECT_BASE_URL else [],
        "RELAY_BASE_URLS": RELAY_BASE_URLS,
    }.items():
        for url in values:
            if len(url) > 512 or any(ord(c) < 33 or ord(c) == 127 for c in url):
                raise RuntimeError(f"{name} must be at most 512 characters without whitespace or controls")
            parsed = urlparse(url)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise RuntimeError(f"{name} must be an HTTP(S) base URL without credentials, query or fragment")
            _ = parsed.port
            if parsed.scheme != "https":
                hostname = parsed.hostname or ""
                try:
                    is_loopback = ipaddress.ip_address(hostname).is_loopback
                except ValueError:
                    is_loopback = hostname == "localhost"
                if not is_loopback:
                    raise RuntimeError(
                        f"{name} must use HTTPS because it carries bearer download tokens"
                    )


validate_config()
