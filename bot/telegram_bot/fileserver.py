"""
Встроенный HTTP-сервер для раздачи скачанных файлов по токенам.

Безопасность:
  • UUID4-токен (122 бит энтропии — практически неугадываемый)
  • Необязательная HMAC-SHA256 подпись каждого токена через SERVER_SECRET (.env)
  • Rate-limiting: не более FS_RATE_LIMIT запросов с одного IP в минуту
  • Одна активная передача на токен; ограниченные повторы и докачка до TTL
  • TTL: ссылка и файл удаляются через час если никто не скачал
  • Стандартные security-заголовки (X-Content-Type-Options, X-Frame-Options, …)
  • Токен не содержит пути на диске; перебор невозможен даже без HMAC
  • HTML-страница предварительного просмотра (/info/<token>) защищает от
    случайного скачивания при предпросмотре ссылок мессенджерами

Структура URL:
  GET /info/<token>   — HTML-страница с кнопкой «Скачать»
  GET /dl/<token>     — прямое скачивание (используется кнопкой со страницы)
  GET /health         — healthcheck
"""

import asyncio
import hashlib
import hmac as _hmac_mod
import ipaddress
import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote as _urlquote

from aiohttp import web

from config import FS_RATE_LIMIT as _RATE_LIMIT
from config import SERVER_SECRET as _SERVER_SECRET_STR

logger = logging.getLogger(__name__)

# ── Настройки безопасности ───────────────────────────────────────────────────────

# Секрет для HMAC-подписи токенов (задаётся в .env как SERVER_SECRET).
# Если не задан — токены без подписи (UUID4 = 122 бит энтропии, всё ещё безопасно).
_SERVER_SECRET: bytes = _SERVER_SECRET_STR.encode()

# uuid4().hex (32) и uuid4().hex + hmac16 (48) — только строчные hex-символы
_HEX32_RE = re.compile(r"[0-9a-f]{32}")
_HEX48_RE = re.compile(r"[0-9a-f]{48}")


def _parse_trusted_proxy_cidrs() -> list[ipaddress._BaseNetwork]:
    raw = os.environ.get("FS_TRUSTED_PROXY_CIDRS", "10.10.2.4/32,10.10.2.5/32,127.0.0.1/32,::1/128")
    networks: list[ipaddress._BaseNetwork] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid FS_TRUSTED_PROXY_CIDRS entry: %s", item)
    return networks


_TRUSTED_PROXY_CIDRS = _parse_trusted_proxy_cidrs()

# ── Реестр файлов ────────────────────────────────────────────────────────────────

@dataclass
class FileEntry:
    path: Path
    filename: str
    file_size: int
    expires_at: float   # unix timestamp
    download_id: int | None = None
    attempts: int = 0
    bytes_sent: int = 0
    completed: bool = False
    busy: bool = False
    ranges: list = field(default_factory=list)


# uuid_key (32 hex) → FileEntry
_registry: dict[str, FileEntry] = {}

# IP → список unix-timestamps запросов за последнюю минуту
_rate_counters: dict[str, list[float]] = defaultdict(list)


# ── Токены ───────────────────────────────────────────────────────────────────────

def _make_token() -> tuple[str, str]:
    """Создаёт токен. Возвращает (uuid_key, full_token).

    Если SERVER_SECRET задан — full_token = uuid_key + hmac16 (48 символов).
    Иначе — full_token == uuid_key (32 символа).
    """
    uuid_key = uuid.uuid4().hex  # 32 hex-символа
    if _SERVER_SECRET:
        sig = _hmac_mod.new(
            _SERVER_SECRET, uuid_key.encode(), hashlib.sha256
        ).hexdigest()[:16]
        return uuid_key, uuid_key + sig  # 48 символов
    return uuid_key, uuid_key


def _verify_token(token: str) -> Optional[str]:
    """Проверяет токен и возвращает uuid_key (32 hex) или None при ошибке."""
    token = token.strip()
    if _SERVER_SECRET:
        if not _HEX48_RE.fullmatch(token):
            return None
        uuid_key = token[:32]
        sig = token[32:]
        # compare_digest защищает от timing-атак
        expected = _hmac_mod.new(
            _SERVER_SECRET, uuid_key.encode(), hashlib.sha256
        ).hexdigest()[:16]
        if not _hmac_mod.compare_digest(sig, expected):
            return None
        return uuid_key
    else:
        # Строгая regex-проверка вместо int(token, 16): int() принимает '0x…',
        # знак '-' и пробелы, то есть пропускал бы в реестр строки, которые
        # ключом uuid4().hex быть не могут.
        if not _HEX32_RE.fullmatch(token):
            return None
        return token


def token_for_key(uuid_key):
    if _SERVER_SECRET:
        return uuid_key + _hmac_mod.new(_SERVER_SECRET, uuid_key.encode(), hashlib.sha256).hexdigest()[:16]
    return uuid_key


def _persist(uuid_key, entry):
    import database as db
    db.save_delivery(uuid_key, entry.path, entry.expires_at, entry.download_id,
                     attempts=entry.attempts, bytes_sent=entry.bytes_sent, completed=entry.completed, ranges=entry.ranges)


def claim(full_token):
    entry = get_entry(full_token)
    if not entry or entry.busy:
        return None
    entry.busy = True
    return entry


def release(full_token):
    key = _verify_token(full_token)
    entry = _registry.get(key)
    if entry:
        entry.busy = False


# ── Публичный API ────────────────────────────────────────────────────────────────

def _fileserver_root() -> Path:
    from config import DOWNLOAD_DIR
    return (DOWNLOAD_DIR / "fileserver").resolve()


def _validate_served_path(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError("symlinks are not allowed")
    real = path.resolve(strict=True)
    real.relative_to(_fileserver_root())
    if not real.is_file():
        raise ValueError("served path is not a regular file")
    return real


def register_file(
    path: Path, ttl_seconds: int = 3600, *, download_id: int | None = None
) -> str:
    """Регистрирует файл, уже изолированный в DOWNLOAD_DIR/fileserver."""
    real_path = _validate_served_path(path)
    uuid_key, full_token = _make_token()
    _registry[uuid_key] = FileEntry(
        path=real_path,
        filename=real_path.name,
        file_size=real_path.stat().st_size,
        expires_at=time.time() + ttl_seconds,
        download_id=download_id,
    )
    try:
        _persist(uuid_key, _registry[uuid_key])
    except BaseException:
        _registry.pop(uuid_key, None)
        raise
    logger.info("Зарегистрирован '%s' → %s… (TTL=%ds)", real_path.name, uuid_key[:8], ttl_seconds)
    return full_token


def move_and_register(
    src: Path, ttl_seconds: int = 3600, *, download_id: int | None = None
) -> str:
    """Перемещает файл в изолированную директорию и регистрирует его.

    Возвращает full_token. Файл переносится из временной папки бота в
    /downloads/fileserver/<uuid_key>/<filename>, чтобы TTL-очистка не
    зависела от tmp_dir загрузчика.

    Права явно выставляются в 755/644, чтобы telegram-bot-api контейнер
    (работает под другим UID) мог выполнить stat() на файл через
    общий том /downloads:ro — независимо от umask процесса.
    """
    from config import DOWNLOAD_DIR
    if src.is_symlink() or not src.is_file():
        raise ValueError(f"Refusing to register non-regular file: {src}")
    src.resolve(strict=True).relative_to(DOWNLOAD_DIR.resolve())
    uuid_key, full_token = _make_token()
    # Создаём промежуточный каталог /downloads/fileserver/ и uuid-подкаталог
    fs_root = DOWNLOAD_DIR / "fileserver"
    fs_root.mkdir(parents=True, exist_ok=True)
    fs_root.chmod(0o755)
    serve_dir = fs_root / uuid_key
    serve_dir.mkdir(parents=True, exist_ok=True)
    serve_dir.chmod(0o755)
    dest = serve_dir / src.name
    moved = False
    try:
        os.replace(src, dest)
        moved = True
        dest.chmod(0o644)
        # yt-dlp preserves the remote Last-Modified time. Restoration uses
        # mtime, so start the link lifetime when it is published, not years ago.
        os.utime(dest, None)
        real_dest = _validate_served_path(dest)
        _registry[uuid_key] = FileEntry(
            path=real_dest,
            filename=real_dest.name,
            file_size=real_dest.stat().st_size,
            expires_at=time.time() + ttl_seconds,
            download_id=download_id,
        )
        _persist(uuid_key, _registry[uuid_key])
    except BaseException:
        _registry.pop(uuid_key, None)
        if moved and dest.exists() and not src.exists():
            try:
                os.replace(dest, src)
            except OSError as rollback_error:
                logger.error("Could not roll back failed file registration: %s", rollback_error)
        _rmdir_safe(serve_dir)
        raise
    logger.info(
        "Перемещён и зарегистрирован '%s' → %s… (TTL=%ds)", src.name, uuid_key[:8], ttl_seconds
    )
    return full_token


def get_entry(full_token: str) -> Optional[FileEntry]:
    """Возвращает FileEntry по токену (без удаления) или None."""
    uuid_key = _verify_token(full_token)
    if not uuid_key:
        return None
    entry = _registry.get(uuid_key)
    if entry and not entry.busy and time.time() >= entry.expires_at:
        _remove(uuid_key, "TTL истёк (get_entry)")
        return None
    return entry


def unregister(full_token: str, *, delete_file: bool = False) -> None:
    """Убирает токен из реестра.

    delete_file=False (по умолчанию): файл остаётся на диске — использовать
    когда файл будет удалён отправителем (например, после send_document).
    delete_file=True: удаляет файл и его директорию.
    """
    uuid_key = _verify_token(full_token)
    if uuid_key is None:
        return
    entry = _registry.pop(uuid_key, None)
    if entry is None:
        return
    import database as db
    db.delete_delivery(uuid_key)
    if delete_file:
        _delete_entry_file(entry)
        logger.debug("Токен %s… отозван + файл удалён", uuid_key[:8])
    else:
        logger.debug("Токен %s… отозван (файл сохранён)", uuid_key[:8])


# ── Внутренние утилиты ───────────────────────────────────────────────────────────

def _remove(uuid_key: str, reason: str = "") -> None:
    if _registry.get(uuid_key) and _registry[uuid_key].busy:
        return
    entry = _registry.pop(uuid_key, None)
    if entry:
        import database as db
        db.delete_delivery(uuid_key)
        _delete_entry_file(entry)
        if not entry.completed:
            _update_download_status(entry, "error", reason or "file link expired")
        logger.info("Файл '%s' удалён (%s)", entry.filename, reason)


def _delete_entry_file(entry: FileEntry) -> None:
    try:
        real_path = _validate_served_path(entry.path)
    except FileNotFoundError:
        # Файла уже нет (например, он ушёл через Telegram и был удалён отправителем).
        # Это штатная ситуация, а не «путь вне корня»: раньше сюда попадал
        # пугающий ERROR, а пустой каталог <uuid_key>/ оставался на диске навсегда.
        _rmdir_if_inside_root(entry.path.parent)
        return
    except (ValueError, OSError):
        logger.error("Refusing to delete file outside fileserver root: %s", entry.path)
        return
    real_path.unlink(missing_ok=True)
    _rmdir_safe(real_path.parent)


def _rmdir_if_inside_root(directory: Path) -> None:
    """Удаляет пустой каталог, но только если он лежит внутри fileserver-корня."""
    try:
        resolved = directory.resolve(strict=True)
        resolved.relative_to(_fileserver_root())
    except (ValueError, OSError):
        return
    _rmdir_safe(resolved)


def _update_download_status(
    entry: FileEntry, status: str, error: str | None = None
) -> None:
    if entry.download_id is None:
        return
    try:
        import database

        database.update_download(entry.download_id, status=status, error=error)
    except Exception as e:
        logger.error("Could not update download %s status: %s", entry.download_id, e)


def _rmdir_safe(d: Path) -> None:
    """Удаляет директорию если пустая."""
    try:
        d.rmdir()
    except OSError:
        pass


_MAX_RATE_KEYS = 10_000  # Лимит уникальных IP в словаре (защита от memory leak)


def _check_rate_limit(ip: str) -> bool:
    """True если IP ещё не исчерпал лимит запросов."""
    now = time.time()
    # Защита от переполнения. НЕ используем clear(): полный сброс позволял бы
    # атакующему обнулять лимиты, заполняя словарь фиктивными IP.
    if len(_rate_counters) > _MAX_RATE_KEYS:
        cutoff = now - 60
        for k in list(_rate_counters.keys()):
            fresh = [h for h in _rate_counters[k] if h > cutoff]
            if fresh:
                _rate_counters[k] = fresh
            else:
                del _rate_counters[k]
        # Всё ещё переполнен (распределённая атака) — вытесняем старейшие ключи
        while len(_rate_counters) > _MAX_RATE_KEYS:
            _rate_counters.pop(next(iter(_rate_counters)), None)
    hits = _rate_counters[ip]
    _rate_counters[ip] = [h for h in hits if now - h < 60]
    if len(_rate_counters[ip]) >= _RATE_LIMIT:
        return False
    _rate_counters[ip].append(now)
    return True


def _client_ip(request: web.Request) -> str:
    """Return real client IP; trust forwarded headers only from configured proxies."""
    remote = request.remote or "unknown"
    try:
        remote_ip = ipaddress.ip_address(remote)
    except ValueError:
        return remote

    if any(remote_ip in network for network in _TRUSTED_PROXY_CIDRS):
        # Порядок важен: CF-Connecting-IP выставляет Cloudflare на edge —
        # клиент его подделать не может. X-Real-IP через Cloudflare Tunnel
        # проходит от клиента НАСКВОЗЬ (cloudflared не вырезает), поэтому
        # проверяем его вторым; nginx затирает CF-Connecting-IP и выставляет
        # X-Real-IP=$remote_addr сам (см. nginx.conf.template).
        forwarded = (
            request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Real-IP")
        )
        if not forwarded:
            # Последний элемент XFF дописан ближайшим доверенным прокси;
            # первый элемент контролируется клиентом (спуф rate-limit ключа).
            forwarded = (request.headers.get("X-Forwarded-For") or "").rsplit(",", 1)[-1].strip()
        if forwarded:
            try:
                ipaddress.ip_address(forwarded)
                return forwarded
            except ValueError:
                logger.warning("Ignoring invalid forwarded client IP: %s", forwarded)

    return remote


def _sanitize_header_value(name: str) -> str:
    """Удаляет символы которые могут инжектировать дополнительные HTTP-заголовки.

    Основная угроза: имя файла содержит \\r\\n — браузер или прокси интерпретирует
    это как конец текущего заголовка и начало нового (HTTP response splitting).
    aiohttp 3.x тоже выбрасывает исключение при \\r/\\n в заголовке, но явная
    санитизация защищает на уровне приложения независимо от версии библиотеки.
    """
    return "".join(c for c in name if ord(c) >= 32 and ord(c) != 127)


def _fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def _fmt_ttl(seconds: int) -> str:
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}ч {m}м" if m else f"{h}ч"
    return f"{m}м {s}с" if m else f"{s}с"


# ── Security headers (применяются ко всем ответам) ───────────────────────────────

_SEC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "X-Robots-Tag": "noindex, nofollow",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
}

# ── HTML info page ───────────────────────────────────────────────────────────────

_INFO_TEMPLATE = """\
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Скачать файл</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:system-ui,-apple-system,sans-serif;background:#0f1117;color:#e2e8f0;
         display:flex;justify-content:center;align-items:center;min-height:100vh;padding:20px}}
    .card{{background:#1e2130;border-radius:16px;padding:36px 32px;max-width:480px;width:100%;
           box-shadow:0 8px 32px rgba(0,0,0,.4)}}
    .icon{{font-size:2.5rem;margin-bottom:16px}}
    h2{{font-size:1.1rem;font-weight:600;word-break:break-word;margin-bottom:8px;color:#f1f5f9}}
    .meta{{color:#94a3b8;font-size:.9rem;margin-bottom:28px;line-height:1.7}}
    .btn{{display:block;width:100%;padding:15px;background:#3b82f6;color:#fff;border:none;
          border-radius:10px;font-size:1rem;font-weight:700;text-align:center;
          text-decoration:none;cursor:pointer;transition:background .15s}}
    .btn:hover{{background:#2563eb}}
    .warn{{background:#1c1917;border-left:3px solid #f59e0b;border-radius:0 8px 8px 0;
           padding:12px 16px;font-size:.82rem;color:#fcd34d;margin-top:20px;line-height:1.5}}
  </style>
</head>
<body>
<div class="card">
  <div class="icon">📁</div>
  <h2>{safe_name}</h2>
  <p class="meta">
    📦 {size_str}<br>
    ⏱ Ссылка истекает через <strong>{ttl_str}</strong><br>
    🗑 Файл удаляется по истечении срока ссылки
  </p>
  <a class="btn" href="/dl/{token}">⬇️ Скачать</a>
  <div class="warn">
    Сохраните ссылку до окончания загрузки. Доступна докачка и ограниченное число повторов.
    Одновременно поддерживается одно соединение. Не передавайте ссылку другим людям.
  </div>
</div>
</body>
</html>
"""


# ── HTTP handlers ────────────────────────────────────────────────────────────────

async def _handle_info(request: web.Request) -> web.Response:
    """HTML-страница с информацией о файле и кнопкой «Скачать»."""
    token = request.match_info["token"]
    ip = _client_ip(request)

    if not _check_rate_limit(ip):
        logger.warning("Rate limit exceeded: %s /info/%s…", ip, token[:8])
        raise web.HTTPTooManyRequests(
            text="Слишком много запросов. Попробуйте через минуту.",
            content_type="text/plain",
            headers=_SEC_HEADERS,
        )

    uuid_key = _verify_token(token)
    if uuid_key is None:
        raise web.HTTPNotFound(headers=_SEC_HEADERS)

    entry = _registry.get(uuid_key)
    if entry is None:
        raise web.HTTPGone(
            text="Файл не найден или ссылка уже использована.",
            content_type="text/plain",
            headers=_SEC_HEADERS,
        )

    if time.time() >= entry.expires_at:
        _remove(uuid_key, "TTL истёк (info)")
        raise web.HTTPGone(
            text="Срок действия ссылки истёк.",
            content_type="text/plain",
            headers=_SEC_HEADERS,
        )

    remaining = int(entry.expires_at - time.time())
    # & должен экранироваться первым, иначе уже экранированные &lt; превратятся в &amp;lt;
    safe_name = (entry.filename
                 .replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;")
                 .replace('"', "&quot;")
                 .replace("'", "&#x27;"))
    html = _INFO_TEMPLATE.format(
        safe_name=safe_name,
        size_str=_fmt_size(entry.file_size),
        ttl_str=_fmt_ttl(remaining),
        token=token,
    )
    return web.Response(
        text=html,
        content_type="text/html",
        charset="utf-8",
        headers=_SEC_HEADERS,
    )


def _byte_range(header, size):
    if not header:
        return 0, max(0, size - 1), False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
    if not match or not size or not any(match.groups()):
        raise ValueError("Invalid range")
    first, last = match.groups()
    if not first:
        count = int(last)
        if count <= 0:
            raise ValueError("Invalid suffix")
        return max(0, size - count), size - 1, True
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or start > end:
        raise ValueError("Unsatisfiable range")
    return start, end, True


async def _handle_download(request: web.Request) -> web.StreamResponse:
    """One active transfer per token; bounded retries and HTTP Range until TTL."""
    import database as db
    token = request.match_info["token"]
    if not _check_rate_limit(_client_ip(request)):
        raise web.HTTPTooManyRequests(headers=_SEC_HEADERS)
    key = _verify_token(token)
    if key is None:
        raise web.HTTPNotFound(headers=_SEC_HEADERS)
    entry = get_entry(token)
    if entry is None:
        raise web.HTTPGone(headers=_SEC_HEADERS)
    if entry.busy:
        raise web.HTTPConflict(text="Файл уже передаётся. Повторите позже.", headers=_SEC_HEADERS)
    try:
        path = _validate_served_path(entry.path)
        size = path.stat().st_size
    except (OSError, ValueError):
        raise web.HTTPNotFound(headers=_SEC_HEADERS)
    etag = '"' + key + '"'
    range_header = request.headers.get('Range')
    if request.headers.get('If-Range') not in (None, etag):
        range_header = None
    try:
        start, end, partial = _byte_range(range_header, size)
    except ValueError:
        raise web.HTTPRequestRangeNotSatisfiable(headers={**_SEC_HEADERS, 'Content-Range': f'bytes */{size}'})
    length = end - start + 1 if size else 0
    if entry.attempts >= 16 or entry.bytes_sent + length > max(1, size) * 3:
        raise web.HTTPTooManyRequests(text="Лимит повторных скачиваний исчерпан.", headers=_SEC_HEADERS)
    entry.busy = True
    entry.attempts += 1
    # Reserve the full response before streaming, so restart cannot reset the budget.
    entry.bytes_sent += length
    response = None
    sent = 0
    try:
        _persist(key, entry)
        clean = _sanitize_header_value(entry.filename).replace('"', '_').replace("\\", '_')
        headers = {
            **_SEC_HEADERS, 'Content-Type': 'application/octet-stream',
            'Content-Length': str(length), 'Accept-Ranges': 'bytes', 'ETag': etag,
            'Content-Disposition': f"attachment; filename*=UTF-8''{_urlquote(clean, safe='')}",
        }
        if partial:
            headers['Content-Range'] = f'bytes {start}-{end}/{size}'
        response = web.StreamResponse(status=206 if partial else 200, headers=headers)
        await response.prepare(request)
        with path.open('rb') as stream:
            stream.seek(start)
            while sent < length:
                chunk = stream.read(min(524288, length - sent))
                if not chunk:
                    raise OSError('File changed during delivery')
                await response.write(chunk)
                sent += len(chunk)
        await response.write_eof()
        merged = []
        for lower, upper in sorted([*entry.ranges, [start, start + length]]):
            if merged and lower <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], upper)
            else:
                merged.append([lower, upper])
        entry.ranges = merged
        entry.completed = merged == [[0, size]]
        _persist(key, entry)
        if entry.completed:
            if entry.download_id and db.all_delivered(entry.download_id):
                _update_download_status(entry, 'done')
        return response
    except (ConnectionError, asyncio.CancelledError):
        raise
    finally:
        entry.busy = False
        # Keep the reservation on failure; a retry has a finite transfer budget.



async def _handle_health(request: web.Request) -> web.Response:
    return web.Response(
        text="ok",
        headers={**_SEC_HEADERS, "Cache-Control": "no-store"},
    )


# ── Фоновая очистка ──────────────────────────────────────────────────────────────

async def _cleanup_loop() -> None:
    """Каждые 5 минут: удаляет файлы с истёкшим TTL и очищает rate-counters."""
    while True:
        await asyncio.sleep(300)
        now = time.time()

        # Удаляем просроченные файлы
        expired = [k for k, e in list(_registry.items()) if now >= e.expires_at]
        for key in expired:
            _remove(key, "TTL истёк (cleanup)")

        # Чистим rate-counters от старых записей
        cutoff = now - 60
        for ip in list(_rate_counters.keys()):
            _rate_counters[ip] = [h for h in _rate_counters[ip] if h > cutoff]
            if not _rate_counters[ip]:
                del _rate_counters[ip]

        if expired:
            logger.info("Очистка файлового сервера: удалено %d файлов", len(expired))


# ── Запуск / остановка ───────────────────────────────────────────────────────────

_runner: Optional[web.AppRunner] = None
_cleanup_task: Optional[asyncio.Task] = None


def _restore_registry() -> None:
    """Восстанавливает реестр из файловой системы после перезапуска бота.

    Сканирует DOWNLOAD_DIR/fileserver/<uuid_key>/<filename> и добавляет
    в _registry все файлы, у которых TTL ещё не истёк.
    TTL считается от mtime файла: expires_at = mtime + FILE_TTL_SECONDS.

    Это решает «ссылка не работает после перезапуска»: файлы на диске живут,
    но in-memory _registry был пуст → бот возвращал 410 Gone.
    """
    from config import DOWNLOAD_DIR, FILE_TTL_SECONDS

    import database as db
    records = db.list_deliveries()
    fs_root = DOWNLOAD_DIR / "fileserver"
    if not fs_root.exists():
        for key in records:
            db.delete_delivery(key)
        return

    now = time.time()
    restored = expired_removed = 0

    for serve_dir in fs_root.iterdir():
        if serve_dir.is_symlink():
            logger.warning("Удаляю симлинк из fileserver root: %s", serve_dir)
            serve_dir.unlink(missing_ok=True)
            continue
        if not serve_dir.is_dir():
            continue
        uuid_key = serve_dir.name
        # Валидируем: должен быть ровно 32 hex-символа
        if len(uuid_key) != 32 or not all(c in "0123456789abcdef" for c in uuid_key):
            continue

        children = list(serve_dir.iterdir())
        for child in children:
            if child.is_symlink():
                logger.warning("Удаляю симлинк из fileserver-каталога: %s", child)
                child.unlink(missing_ok=True)
        files = [f for f in children if not f.is_symlink() and f.is_file()]
        if not files:
            try:
                serve_dir.rmdir()
            except OSError:
                pass
            continue

        try:
            f = _validate_served_path(files[0])
        except (ValueError, OSError):
            logger.warning("Пропускаю небезопасный файл при восстановлении: %s", files[0])
            continue
        # В норме в serve_dir ровно один файл. Если их несколько (сбой/повреждение),
        # лишние не попадут в реестр и не будут удалены по /dl → чистим их сейчас,
        # чтобы не текла квота диска.
        for extra in files[1:]:
            extra.unlink(missing_ok=True)
        # TTL отсчитываем от момента записи файла на диск (mtime)
        record = records.get(uuid_key, {})
        if record and record['path'] != str(f):
            record = {}
        expires_at = record.get('expires_at', f.stat().st_mtime + FILE_TTL_SECONDS)

        if expires_at < now:
            # Срок истёк — убираем с диска
            for ff in files:
                ff.unlink(missing_ok=True)
            try:
                serve_dir.rmdir()
            except OSError:
                pass
            expired_removed += 1
            continue

        _registry[uuid_key] = FileEntry(
            path=f,
            filename=f.name,
            file_size=f.stat().st_size,
            expires_at=expires_at,
            download_id=record.get('download_id'),
            attempts=record.get('attempts', 0), bytes_sent=record.get('bytes_sent', 0),
            completed=bool(record.get('completed', False)),
            ranges=json.loads(record.get('ranges', '[]')),
        )
        _persist(uuid_key, _registry[uuid_key])
        restored += 1

    for key in records.keys() - _registry.keys():
        db.delete_delivery(key)

    if restored or expired_removed:
        logger.info(
            "Файловый сервер: восстановлено %d файлов, удалено %d просроченных после рестарта",
            restored, expired_removed,
        )


async def start(host: str = "0.0.0.0", port: int = 8080) -> web.AppRunner:
    """Запускает HTTP-сервер и фоновую очистку."""
    global _runner, _cleanup_task

    # HIGH-2: warn when SERVER_SECRET is not set (tokens lack HMAC protection)
    if not _SERVER_SECRET:
        logger.warning(
            "SERVER_SECRET не задан — токены выдаются без HMAC-подписи. "
            "Установите SERVER_SECRET в .env для усиленной защиты."
        )

    # Восстанавливаем файлы, зарегистрированные до рестарта бота
    _restore_registry()

    app = web.Application()
    transfer_slots = asyncio.Semaphore(32)

    async def limited_download(request):
        if transfer_slots.locked():
            raise web.HTTPServiceUnavailable(headers={**_SEC_HEADERS, "Retry-After": "60"})
        async with transfer_slots:
            # Bound slow readers even when traffic bypasses nginx.
            async with asyncio.timeout(3600):
                return await _handle_download(request)
    # allow_head=False: aiohttp по умолчанию вешает и HEAD на add_get. Для /dl/
    # это критично — HEAD запустил бы _handle_download, который извлекает токен
    # из реестра и удаляет файл в finally, НЕ отдав тело. Тогда превью ссылки
    # мессенджером/антивирусом/сканером (HEAD или GET) сжигало бы одноразовую
    # ссылку. nginx режет HEAD через limit_except, но Cloudflare-туннель и прямой
    # DNAT на :8080 идут мимо nginx — защищаемся на уровне приложения.
    app.router.add_get("/info/{token}", _handle_info, allow_head=False)
    app.router.add_get("/dl/{token}", limited_download, allow_head=False)
    app.router.add_get("/health", _handle_health)

    _runner = web.AppRunner(app, access_log=None)
    await _runner.setup()
    site = web.TCPSite(_runner, host, port)
    await site.start()

    _cleanup_task = asyncio.create_task(_cleanup_loop())

    hmac_status = "HMAC подпись включена ✓" if _SERVER_SECRET else "HMAC выключен (только UUID4)"
    logger.info(
        "Файловый сервер запущен: http://%s:%d  |  %s  |  rate-limit=%d req/min",
        host, port, hmac_status, _RATE_LIMIT,
    )
    return _runner


async def stop() -> None:
    """Graceful shutdown файлового сервера."""
    global _runner, _cleanup_task
    if _cleanup_task:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
        _cleanup_task = None
    if _runner:
        await _runner.cleanup()
        _runner = None
    logger.info("Файловый сервер остановлен")
