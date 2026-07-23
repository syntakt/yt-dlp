import asyncio
from contextlib import contextmanager
import ipaddress
import logging
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse, parse_qs

import yt_dlp

import shutil

from config import (
    ALLOW_GENERIC_URLS,
    COOKIES_FILE,
    DOWNLOAD_TIMEOUT,
    INFO_TIMEOUT,
    MAX_FILE_SIZE_BYTES,
    MAX_PLAYLIST_TOTAL_BYTES,
    MIN_FREE_DISK_BYTES,
    PROXY_URL,
    SSRF_PROTECTION,
    USE_ARIA2C,
    USE_SPONSORBLOCK,
    TORRENT_LISTEN_PORT,
    TORRENT_MAX_PEERS,
    TORRENT_ENABLE_DHT,
    TORRENT_DOWNLOAD_LIMIT,
    TORRENT_TIMEOUT,
    TORRENT_MAX_TOTAL_BYTES,
)

logger = logging.getLogger(__name__)


_ORIGINAL_GETADDRINFO = getattr(
    socket, "_ytdlp_bot_original_getaddrinfo", socket.getaddrinfo
)
if not hasattr(socket, "_ytdlp_bot_original_getaddrinfo"):
    socket._ytdlp_bot_original_getaddrinfo = _ORIGINAL_GETADDRINFO
_NETWORK_GUARD = threading.local()


def _guarded_getaddrinfo(host, *args, **kwargs):
    """Reject non-public DNS answers for guarded yt-dlp worker threads."""
    answers = _ORIGINAL_GETADDRINFO(host, *args, **kwargs)
    if not getattr(_NETWORK_GUARD, "enabled", False):
        return answers
    for answer in answers:
        try:
            address = ipaddress.ip_address(answer[4][0])
        except (ValueError, IndexError, TypeError) as e:
            raise socket.gaierror("blocked invalid DNS response") from e
        if not address.is_global:
            raise socket.gaierror(f"blocked non-public address for {host!r}")
    return answers


socket.getaddrinfo = _guarded_getaddrinfo


@contextmanager
def _guard_network():
    previous = getattr(_NETWORK_GUARD, "enabled", False)
    _NETWORK_GUARD.enabled = SSRF_PROTECTION
    try:
        yield
    finally:
        _NETWORK_GUARD.enabled = previous


class _DownloadCancelled(BaseException):
    """Поднимается из progress-хука для прерывания загрузки.

    Наследует BaseException, а не Exception — yt-dlp использует
    `except Exception` внутри, поэтому только BaseException гарантированно
    пробьётся через все обёртки yt-dlp и отменит загрузку немедленно.
    """


class DownloadCancelledError(Exception):
    """A user-requested cancellation reported back to the async caller."""


# ── Data classes ────────────────────────────────────────────────────────────────

@dataclass
class FormatInfo:
    format_id: str
    ext: str
    quality: str
    resolution: str
    fps: Optional[int]
    vcodec: str
    acodec: str
    filesize: Optional[int]
    tbr: Optional[float]  # total bitrate kbps
    note: str = ""

    @property
    def is_video(self) -> bool:
        return self.vcodec not in ("none", "", None)

    @property
    def is_audio_only(self) -> bool:
        return not self.is_video and self.acodec not in ("none", "", None)

    @property
    def size_str(self) -> str:
        if self.filesize:
            mb = self.filesize / (1024 * 1024)
            return f"{mb:.1f} MB"
        if self.tbr:
            return f"~{self.tbr:.0f} kbps"
        return "unknown"

    @property
    def label(self) -> str:
        parts = []
        if self.resolution and self.resolution != "audio only":
            parts.append(self.resolution)
        if self.fps:
            parts.append(f"{self.fps}fps")
        parts.append(self.ext.upper())
        parts.append(f"[{self.size_str}]")
        return " · ".join(parts)


@dataclass
class VideoInfo:
    url: str
    title: str
    uploader: str
    duration: int  # seconds
    view_count: int
    like_count: Optional[int]
    thumbnail: str
    description: str
    formats: list[FormatInfo] = field(default_factory=list)
    is_playlist: bool = False
    playlist_count: Optional[int] = None
    webpage_url: str = ""
    extractor: str = ""

    @property
    def duration_str(self) -> str:
        h, r = divmod(self.duration or 0, 3600)
        m, s = divmod(r, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    @property
    def views_str(self) -> str:
        v = self.view_count or 0
        if v >= 1_000_000:
            return f"{v/1_000_000:.1f}M"
        if v >= 1_000:
            return f"{v/1_000:.1f}K"
        return str(v)


@dataclass
class DownloadResult:
    success: bool
    file_path: Optional[Path] = None
    title: str = ""
    file_size: int = 0
    error: str = ""


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _base_opts() -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "file_access_retries": 3,
    }
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        opts["cookiefile"] = COOKIES_FILE
    if USE_ARIA2C and not SSRF_PROTECTION and shutil.which("aria2c"):
        # aria2c: до 16 параллельных соединений на файл — ускоряет HTTP/HTTPS загрузки
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = {"default": ["-x16", "-s16", "-k1M", "--quiet"]}
    else:
        # Встроенный загрузчик yt-dlp: 3 потока для HLS/DASH фрагментов
        # (concurrent_fragment_downloads — официальная опция yt-dlp, см. README/download-options)
        # Keep network work in the guarded executor thread. Parallel fragment
        # workers would not inherit the thread-local DNS policy.
        opts["concurrent_fragment_downloads"] = 1 if SSRF_PROTECTION else 3
        if SSRF_PROTECTION:
            opts["hls_prefer_native"] = True
    return opts


def disk_has_capacity(output_dir: Path, required_bytes: int = 0) -> bool:
    """Return whether a download can preserve the configured free-space reserve."""
    output_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(output_dir).free
    return free >= MIN_FREE_DISK_BYTES + max(0, required_bytes)


def _output_size(output_dir: Path) -> int:
    total = 0
    for path in output_dir.iterdir():
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _human_size(n: Optional[int]) -> str:
    if not n:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _redact_url(url: str) -> str:
    """Remove credentials, query and fragment before logging user supplied URLs."""
    try:
        parsed = urlparse(url)
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return parsed._replace(netloc=netloc, query="", fragment="").geturl()
    except Exception:
        return "<invalid-url>"


# ── Info extraction ─────────────────────────────────────────────────────────────

async def get_video_info(url: str) -> VideoInfo:
    """Extract metadata + available formats (no download)."""
    opts = _base_opts()
    opts.update({
        "skip_download": True,
        # extract_flat=True: для плейлистов возвращает только плоский список
        # (id, title, url) без HTTP-запросов для каждого видео — на порядок быстрее.
        # Для одиночных видео поведение не меняется.
        "extract_flat": True,
    })

    loop = asyncio.get_running_loop()

    def _extract():
        with _guard_network(), yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.wait_for(
            loop.run_in_executor(None, _extract), timeout=INFO_TIMEOUT
        )
    except asyncio.TimeoutError as e:
        raise TimeoutError("Metadata request timed out") from e

    is_playlist = info.get("_type") == "playlist"
    if is_playlist:
        # Return minimal playlist info
        entries = list(info.get("entries") or [])
        return VideoInfo(
            url=url,
            title=info.get("title", "Playlist"),
            uploader=info.get("uploader", ""),
            duration=0,
            view_count=0,
            like_count=None,
            thumbnail=info.get("thumbnail", ""),
            description="",
            is_playlist=True,
            playlist_count=len(entries),
            webpage_url=info.get("webpage_url", url),
            extractor=info.get("extractor", ""),
        )

    formats = _parse_formats(info.get("formats") or [])

    return VideoInfo(
        url=url,
        title=info.get("title", ""),
        uploader=info.get("uploader", ""),
        duration=info.get("duration", 0),
        view_count=info.get("view_count", 0),
        like_count=info.get("like_count"),
        thumbnail=info.get("thumbnail", ""),
        description=(info.get("description") or "")[:500],
        formats=formats,
        webpage_url=info.get("webpage_url", url),
        extractor=info.get("extractor", ""),
    )


def _parse_formats(raw_formats: list) -> list[FormatInfo]:
    seen = set()
    result = []

    for f in raw_formats:
        fid = f.get("format_id", "")
        vcodec = f.get("vcodec", "none") or "none"
        acodec = f.get("acodec", "none") or "none"
        ext = f.get("ext", "")
        height = f.get("height")
        width = f.get("width")
        fps = f.get("fps")
        tbr = f.get("tbr")
        filesize = f.get("filesize") or f.get("filesize_approx")

        # Determine resolution label
        if height:
            res = f"{height}p"
        elif width:
            res = f"{width}w"
        elif vcodec == "none":
            res = "audio only"
        else:
            res = "unknown"

        quality = f.get("quality", 0) or 0

        # Skip duplicate resolutions for video formats (keep best tbr)
        dedup_key = (res, ext, vcodec != "none", acodec != "none")
        if dedup_key in seen and res != "audio only":
            continue
        seen.add(dedup_key)

        # Skip storyboard/mhtml
        if ext in ("mhtml", "none"):
            continue
        if "storyboard" in fid.lower():
            continue

        result.append(FormatInfo(
            format_id=fid,
            ext=ext,
            quality=str(quality),
            resolution=res,
            fps=int(fps) if fps else None,
            vcodec=vcodec,
            acodec=acodec,
            filesize=filesize,
            tbr=tbr,
            note=f.get("format_note", ""),
        ))

    # Sort: video by height desc, then audio
    def sort_key(f: FormatInfo):
        if f.is_audio_only:
            return (0, f.tbr or 0)
        try:
            h = int(f.resolution.rstrip("p"))
        except (ValueError, AttributeError):
            h = 0
        return (1, h)

    result.sort(key=sort_key, reverse=True)
    return result


def get_best_video_formats(formats: list[FormatInfo]) -> list[FormatInfo]:
    """Return unique video+audio combined formats for user selection."""
    video_formats = [f for f in formats if f.is_video]
    # Deduplicate by resolution
    seen_res = set()
    unique = []
    for f in video_formats:
        if f.resolution not in seen_res:
            seen_res.add(f.resolution)
            unique.append(f)
    return unique[:8]  # Limit to 8 options


def get_audio_formats(formats: list[FormatInfo]) -> list[FormatInfo]:
    return [f for f in formats if f.is_audio_only][:5]


# Расширения медиа-файлов, которые бот считает результатом загрузки.
# Используется и в download_video (поиск файла), и в download_playlist (сбор
# результатов): аудио-режим плейлиста без постпроцессора даёт .m4a/.opus/.webm.
_MEDIA_EXTS = {
    ".mp4", ".webm", ".mkv", ".avi", ".mov",
    ".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".aac",
}


# ── Download ────────────────────────────────────────────────────────────────────

class ProgressTracker:
    def __init__(self, callback: Optional[Callable] = None, loop=None):
        self.callback = callback
        self.loop = loop
        self.downloaded = 0
        self.total = 0
        self.speed = 0
        self.eta = 0
        self.status = "downloading"
        self._last_update = 0.0

    def hook(self, d: dict) -> None:
        self.status = d.get("status", "")
        if self.status == "downloading":
            self.downloaded = d.get("downloaded_bytes", 0) or 0
            self.total = d.get("total_bytes") or d.get("total_bytes_estimate", 0) or 0
            self.speed = d.get("speed", 0) or 0
            self.eta = d.get("eta", 0) or 0
            # Не показываем пока ничего не скачали (первый вызов с 0 байт)
            if not self.downloaded:
                return
            if self.callback and self.loop:
                now = time.monotonic()
                if now - self._last_update >= 3.0:   # не чаще раза в 3 секунды
                    self._last_update = now
                    asyncio.run_coroutine_threadsafe(self.callback(self), self.loop)
        elif self.status == "finished":
            # Загрузка завершена, идёт пост-обработка (FFmpeg конвертация).
            # Уведомляем UI один раз, чтобы бот показал "Конвертирую..."
            if self.callback and self.loop:
                asyncio.run_coroutine_threadsafe(self.callback(self), self.loop)


async def download_video(
    url: str,
    format_id: str,
    output_dir: Path,
    progress_callback: Optional[Callable] = None,
    audio_only: bool = False,
    audio_format: str = "mp3",   # "mp3" | "opus" | "wav"
    subtitle_lang: Optional[str] = None,
    cancel_flag: Optional[list] = None,
) -> DownloadResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(output_dir / "%(title).80s.%(ext)s")

    opts = _base_opts()

    # Когда ждём прогресс — используем нативный загрузчик yt-dlp:
    # aria2c как внешний загрузчик вызывает progress_hook только при старте/финише,
    # из-за чего сообщение застревает на «Пожалуйста, подождите…».
    # Нативный загрузчик с 3 потоками даёт полноценный прогресс на каждом фрагменте.
    if progress_callback and USE_ARIA2C:
        opts.pop("external_downloader", None)
        opts.pop("external_downloader_args", None)
        opts["concurrent_fragment_downloads"] = 3

    loop = asyncio.get_running_loop()
    tracker = ProgressTracker(progress_callback, loop)
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    timed_out = [False]
    last_disk_check = [0.0]

    def _cancel_hook(d: dict) -> None:
        """Вызывается из yt-dlp прогресс-хука.

        Поднимаем _DownloadCancelled (BaseException), чтобы пробить
        все `except Exception` внутри yt-dlp и немедленно прервать загрузку.
        """
        if cancel_flag and cancel_flag[0]:
            raise _DownloadCancelled("CANCELLED")
        if time.monotonic() >= deadline:
            timed_out[0] = True
            raise _DownloadCancelled("TIMEOUT")
        now = time.monotonic()
        if now - last_disk_check[0] >= 1.0:
            last_disk_check[0] = now
            if not disk_has_capacity(output_dir):
                raise _DownloadCancelled("DISK_FULL")

    if audio_only:
        if audio_format == "opus":
            # OPUS: ремукс из webm-контейнера — транскодирование не требуется,
            # работает в 10–50x быстрее MP3. Исходный поток YouTube уже в OPUS.
            opts.update({
                "format": "bestaudio[ext=webm]/bestaudio",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "opus",
                }],
            })
        elif audio_format == "wav":
            # WAV: несжатый PCM — без потерь, максимальный размер
            opts.update({
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "wav",
                }],
            })
        else:
            # MP3: транскодирование libmp3lame с многопоточным FFmpeg
            opts.update({
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
                # -threads 0 → FFmpeg использует все доступные ядра CPU
                "postprocessor_args": {"ffmpeg": ["-threads", "0"]},
            })
    else:
        # Combine selected video with best audio
        if format_id and format_id != "best":
            fmt = f"{format_id}+bestaudio/best[height<={_height_from_fid(format_id)}]/best"
        else:
            fmt = "bestvideo+bestaudio/best"
        opts["format"] = fmt
        opts["merge_output_format"] = "mp4"

    if subtitle_lang:
        opts.update({
            "writesubtitles": True,
            "subtitleslangs": [subtitle_lang],
            "writeautomaticsub": True,
        })

    # SponsorBlock: автоматически вырезать рекламные вставки из YouTube
    if USE_SPONSORBLOCK and not audio_only:
        opts["sponsorblock_remove"] = ["sponsor", "selfpromo", "interaction"]

    hooks = [tracker.hook, _cancel_hook]
    opts.update({
        "outtmpl": output_template,
        "progress_hooks": hooks,
        "postprocessor_hooks": [_cancel_hook],
        "max_filesize": MAX_FILE_SIZE_BYTES,
        "noplaylist": True,
    })

    result_holder = {}

    def _download():
        try:
            with _guard_network(), yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                result_holder["title"] = info.get("title", "")
                if audio_only:
                    ext = ".opus" if audio_format == "opus" else ".wav" if audio_format == "wav" else ".mp3"
                    filename = Path(filename).with_suffix(ext)
                else:
                    filename = Path(filename).with_suffix(".mp4")
                    if not filename.exists():
                        filename = Path(ydl.prepare_filename(info))
                result_holder["file_path"] = Path(filename)
        except _DownloadCancelled as e:
            # Отмена пользователем — не логируем как ошибку.
            # Safety net: _DownloadCancelled is a BaseException and must
            # never escape into the executor / event-loop machinery.
            result_holder["error"] = str(e) or "CANCELLED"
        except Exception as e:
            result_holder["error"] = str(e)
        except BaseException as e:
            # Safety net: catch any other BaseException (e.g. KeyboardInterrupt)
            # so it doesn't leak out of the executor thread.
            result_holder["error"] = str(e)

    worker = loop.run_in_executor(None, _download)
    try:
        await asyncio.wait_for(asyncio.shield(worker), timeout=DOWNLOAD_TIMEOUT + 60)
    except asyncio.TimeoutError:
        timed_out[0] = True
        if cancel_flag is not None:
            cancel_flag[0] = True
        return DownloadResult(success=False, error="Download timed out")

    if "error" in result_holder:
        if result_holder["error"] == "TIMEOUT" or timed_out[0]:
            return DownloadResult(success=False, error="Download timed out")
        if result_holder["error"] == "DISK_FULL":
            return DownloadResult(success=False, error="Insufficient free disk space")
        if cancel_flag and cancel_flag[0]:
            return DownloadResult(success=False, error="CANCELLED")
        return DownloadResult(success=False, error=result_holder["error"])

    if cancel_flag and cancel_flag[0]:
        return DownloadResult(success=False, error="CANCELLED")

    file_path: Path = result_holder.get("file_path")
    if not file_path or not file_path.exists():
        # Try to find the downloaded file, preferring known media extensions
        all_candidates = sorted(output_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        media_candidates = [p for p in all_candidates if p.suffix.lower() in _MEDIA_EXTS]
        candidates = media_candidates if media_candidates else all_candidates
        if candidates:
            file_path = candidates[0]
        else:
            return DownloadResult(success=False, error="Downloaded file not found")

    # CRITICAL-2: path traversal guard — reject files outside output_dir
    try:
        file_path.resolve().relative_to(output_dir.resolve())
    except ValueError:
        logger.error("Path traversal detected: %s is outside %s", file_path, output_dir)
        return DownloadResult(success=False, error="Download path validation failed")

    file_size = file_path.stat().st_size
    if file_size > MAX_FILE_SIZE_BYTES:
        file_path.unlink(missing_ok=True)
        return DownloadResult(
            success=False,
            error=f"File too large: {_human_size(file_size)} (max {_human_size(MAX_FILE_SIZE_BYTES)})",
        )

    return DownloadResult(
        success=True,
        file_path=file_path,
        title=result_holder.get("title", ""),
        file_size=file_size,
    )


async def download_playlist(
    url: str,
    format_id: str,
    output_dir: Path,
    max_items: int = 10,
    cancel_flag: Optional[list] = None,
) -> list[DownloadResult]:
    output_dir.mkdir(parents=True, exist_ok=True)
    opts = _base_opts()
    if format_id == "bestaudio":
        fmt = "bestaudio/best"
    elif format_id != "best":
        fmt = f"{format_id}+bestaudio/best"
    else:
        fmt = "bestvideo+bestaudio/best"
    opts.update({
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": str(output_dir / "%(playlist_index)s-%(title).60s.%(ext)s"),
        "noplaylist": False,
        "playlistend": max_items,
        # Задержки между запросами для обхода rate-limit YouTube
        "sleep_interval": 3,
        "max_sleep_interval": 8,
        "sleep_interval_requests": 1,
        "ignoreerrors": True,  # не прерываем плейлист на недоступном видео
        "max_filesize": MAX_FILE_SIZE_BYTES,
    })

    loop = asyncio.get_running_loop()
    results = []
    error_holder: dict = {}
    timeout = DOWNLOAD_TIMEOUT * max(1, max_items)
    deadline = time.monotonic() + timeout
    last_disk_check = [0.0]

    def _deadline_hook(d: dict) -> None:
        if cancel_flag and cancel_flag[0]:
            raise _DownloadCancelled("CANCELLED")
        if time.monotonic() >= deadline:
            raise _DownloadCancelled("TIMEOUT")
        now = time.monotonic()
        if now - last_disk_check[0] >= 1.0:
            last_disk_check[0] = now
            if not disk_has_capacity(output_dir):
                raise _DownloadCancelled("DISK_FULL")
            if _output_size(output_dir) > MAX_PLAYLIST_TOTAL_BYTES:
                raise _DownloadCancelled("PLAYLIST_TOTAL_LIMIT")

    opts["progress_hooks"] = [_deadline_hook]
    opts["postprocessor_hooks"] = [_deadline_hook]

    def _download():
        try:
            with _guard_network(), yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except _DownloadCancelled as e:
            error_holder["error"] = str(e) or "CANCELLED"
        except Exception as e:
            error_holder["error"] = str(e)

    worker = loop.run_in_executor(None, _download)
    try:
        await asyncio.wait_for(asyncio.shield(worker), timeout=timeout + 60)
    except asyncio.TimeoutError as e:
        if cancel_flag is not None:
            cancel_flag[0] = True
        raise TimeoutError("Playlist download timed out") from e

    error = error_holder.get("error")
    if error == "TIMEOUT":
        raise TimeoutError("Playlist download timed out")
    if error == "CANCELLED" or (cancel_flag and cancel_flag[0]):
        raise DownloadCancelledError("CANCELLED")
    if error == "DISK_FULL":
        raise RuntimeError("Insufficient free disk space")
    if error == "PLAYLIST_TOTAL_LIMIT":
        raise RuntimeError(
            f"Playlist exceeded aggregate limit: {_human_size(MAX_PLAYLIST_TOTAL_BYTES)}"
        )

    # Если плейлист не скачал ни одного файла и была ошибка — пробрасываем
    if error and not any(output_dir.iterdir()):
        raise RuntimeError(error)

    base_resolved = output_dir.resolve()
    for f in sorted(output_dir.iterdir()):
        # HIGH-5: skip symlinks and files outside output_dir
        if f.is_symlink():
            logger.warning("Skipping symlink in playlist output: %s", f)
            continue
        # _MEDIA_EXTS вместо жёсткого списка: аудио-режим (bestaudio без
        # постпроцессора) даёт .m4a/.opus/.webm — раньше такие файлы терялись
        if not f.is_file() or f.suffix.lower() not in _MEDIA_EXTS:
            continue
        try:
            f.resolve().relative_to(base_resolved)
        except ValueError:
            logger.warning("Skipping out-of-directory file in playlist: %s", f)
            continue
        size = f.stat().st_size
        results.append(DownloadResult(
            success=True,
            file_path=f,
            title=f.stem,
            file_size=size,
        ))
    return results


def _height_from_fid(format_id: str) -> int:
    """Best-effort: if format_id encodes height, extract it."""
    m = re.search(r"(\d{3,4})p?", format_id)
    return int(m.group(1)) if m else 9999


_EXTRACTORS_LOCK = threading.Lock()
_EXTRACTORS: list | None = None


def _is_ssrf_url(url: str) -> bool:
    """Возвращает True, если URL ведёт на приватный/loopback адрес (SSRF-защита).

    Известное ограничение: проверка резолвит имя в момент вызова, а yt-dlp
    резолвит его повторно при загрузке — DNS rebinding (TOCTOU) полностью
    не закрывается. Здесь отсекаем очевидные попытки достучаться до
    внутренней сети; сетевая изоляция контейнера — вторая линия защиты.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return True
        # Разрешаем имя в IP; если резолюция падает — блокируем
        try:
            addr = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        except OSError:
            return True
        for family, _, _, _, sockaddr in addr:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                return True
            # not is_global покрывает loopback/private/link-local/multicast/
            # reserved/unspecified, а также CGNAT 100.64.0.0/10 и 6to4-релеи,
            # которые перечисление отдельных флагов пропускало.
            if not ip.is_global:
                return True
        return False
    except Exception:
        return True


def is_supported_url(url: str) -> bool:
    """Проверяет URL без инстанциирования всех экстракторов каждый раз."""
    global _EXTRACTORS
    if not re.match(r"https?://", url, re.IGNORECASE):
        return False
    # SSRF: блокируем приватные/loopback адреса
    if SSRF_PROTECTION and _is_ssrf_url(url):
        logger.warning("Blocked SSRF attempt: %s", _redact_url(url))
        return False
    if _EXTRACTORS is None:
        with _EXTRACTORS_LOCK:
            if _EXTRACTORS is None:
                _EXTRACTORS = list(yt_dlp.extractor.gen_extractors())
    for e in _EXTRACTORS:
        if e.suitable(url) and e.IE_NAME != "generic":
            return True
    if ALLOW_GENERIC_URLS:
        # Публичные http(s) URL допускаем через generic extractor только явным opt-in.
        # Для публичных ботов это расширяет SSRF surface за счёт HLS/DASH/redirect URL.
        return True
    logger.warning("Blocked generic URL because ALLOW_GENERIC_URLS=false: %s", _redact_url(url))
    return False


# ── BitTorrent (magnet + .torrent) через aria2c ──────────────────────────────────
#
# Модель безопасности (см. также config.py / TELEGRAM_BOT.md):
#   • Не сидируем (--seed-time=0) → только исходящие соединения к пирам.
#   • DHT/LPD/PEX по умолчанию выключены → нет UDP-listener'ов и анонсов себя.
#   • listen-порт фиксирован и НЕ пробрасывается наружу (ни ports:, ни nftables DNAT).
#   • aria2c — подпроцесс, он НЕ наследует Python-SSRF-guard. Трекеры проверяем сами
#     (_torrent_trackers_are_safe), доступ к приватной сети закрывается nftables egress.


class _BencodeError(ValueError):
    """Некорректный bencode в .torrent-файле."""


_BENCODE_MAX_DEPTH = 32  # .torrent не вкладывается глубоко; ограничение против stack overflow


def _bdecode_at(data: bytes, i: int, depth: int = 0):
    """Декодирует один bencode-элемент начиная с позиции i. Возвращает (value, next_i)."""
    if depth > _BENCODE_MAX_DEPTH:
        raise _BencodeError("bencode nesting too deep")
    if i >= len(data):
        raise _BencodeError("unexpected end of data")
    c = data[i:i + 1]
    if c == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1:end]), end + 1
    if c == b"l":
        i += 1
        out = []
        while True:
            if i >= len(data):
                raise _BencodeError("unterminated list")
            if data[i:i + 1] == b"e":
                return out, i + 1
            v, i = _bdecode_at(data, i, depth + 1)
            out.append(v)
    if c == b"d":
        i += 1
        out = {}
        while True:
            if i >= len(data):
                raise _BencodeError("unterminated dict")
            if data[i:i + 1] == b"e":
                return out, i + 1
            k, i = _bdecode_at(data, i, depth + 1)
            v, i = _bdecode_at(data, i, depth + 1)
            out[k] = v
    if c.isdigit():
        colon = data.index(b":", i)
        length = int(data[i:colon])
        if length < 0:
            raise _BencodeError("negative string length")
        start = colon + 1
        end = start + length
        if end > len(data):
            raise _BencodeError("string length out of bounds")
        return data[start:end], end
    raise _BencodeError(f"invalid bencode token {c!r} at {i}")


def _bdecode(data: bytes):
    try:
        value, _ = _bdecode_at(data, 0)
    except (ValueError, IndexError) as e:
        raise _BencodeError(str(e)) from e
    return value


def _b2s(b) -> str:
    if isinstance(b, bytes):
        return b.decode("utf-8", "replace")
    return str(b)


@dataclass
class TorrentMeta:
    name: str
    total_size: Optional[int]      # суммарный размер всех файлов (None для magnet до метаданных)
    files: list[str] = field(default_factory=list)  # относительные пути файлов
    trackers: list[str] = field(default_factory=list)
    btih: str = ""
    source: str = ""               # magnet URI или путь к .torrent
    is_magnet: bool = False

    @property
    def media_files(self) -> list[str]:
        return [f for f in self.files if Path(f).suffix.lower() in _MEDIA_EXTS]


_MAGNET_BTIH_RE = re.compile(r"^urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})$")


def parse_magnet(uri: str) -> TorrentMeta:
    """Парсит magnet-ссылку. Бросает ValueError при отсутствии валидного btih."""
    parsed = urlparse(uri.strip())
    if parsed.scheme != "magnet":
        raise ValueError("not a magnet URI")
    qs = parse_qs(parsed.query)
    btih = ""
    for xt in qs.get("xt", []):
        m = _MAGNET_BTIH_RE.match(xt.strip())
        if m:
            btih = m.group(1).lower()
            break
    if not btih:
        raise ValueError("magnet without a valid btih hash")
    name = (qs.get("dn", [""])[0] or "").strip() or f"magnet-{btih[:12]}"
    trackers = [t for t in qs.get("tr", []) if t]
    return TorrentMeta(
        name=name, total_size=None, files=[], trackers=trackers,
        btih=btih, source=uri.strip(), is_magnet=True,
    )


def parse_torrent_file(path) -> TorrentMeta:
    """Парсит .torrent-файл (bencode). Бросает ValueError при некорректном формате."""
    data = Path(path).read_bytes()
    meta = _bdecode(data)
    if not isinstance(meta, dict):
        raise ValueError("invalid torrent: top-level is not a dict")
    info = meta.get(b"info")
    if not isinstance(info, dict):
        raise ValueError("invalid torrent: missing info dict")
    name = _b2s(info.get(b"name", b"")) or "torrent"
    files: list[str] = []
    total = 0
    if isinstance(info.get(b"files"), list):
        # multi-file: каждый файл лежит внутри папки name/
        for f in info[b"files"]:
            if not isinstance(f, dict):
                continue
            total += int(f.get(b"length", 0) or 0)
            parts = [_b2s(p) for p in (f.get(b"path") or [])]
            files.append("/".join(parts) if parts else "")
    else:
        total = int(info.get(b"length", 0) or 0)
        files.append(name)
    trackers: list[str] = []
    if b"announce" in meta:
        trackers.append(_b2s(meta[b"announce"]))
    for tier in (meta.get(b"announce-list") or []):
        if isinstance(tier, list):
            trackers.extend(_b2s(tr) for tr in tier)
    return TorrentMeta(
        name=name, total_size=total, files=files, trackers=trackers,
        source=str(path), is_magnet=False,
    )


def _host_is_public(hostname: str) -> bool:
    """True, если hostname резолвится ТОЛЬКО в глобальные (публичные) IP."""
    if not hostname:
        return False
    try:
        addrs = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for _family, _t, _p, _c, sockaddr in addrs:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except (ValueError, IndexError):
            return False
        if not ip.is_global:
            return False
    return True


def _torrent_trackers_are_safe(trackers: list[str]) -> bool:
    """При включённой SSRF-защите отклоняет трекеры на приватных/loopback адресах.

    Это best-effort: IP пиров, полученных от трекеров, aria2c не фильтрует —
    основную защиту от SSRF даёт nftables egress (см. TELEGRAM_BOT.md).
    """
    if not SSRF_PROTECTION:
        return True
    for tr in trackers:
        host = urlparse(tr).hostname
        if not host:
            continue
        if not _host_is_public(host):
            logger.warning("Blocked torrent tracker on non-public host: %s", host)
            return False
    return True


def _build_aria2c_args(
    source: str,
    output_dir: Path,
    *,
    listen_port: int,
    max_peers: int,
    enable_dht: bool,
    download_limit: int,
    select_indices: Optional[list[int]] = None,
    metadata_only: bool = False,
) -> list[str]:
    """Собирает hardened командную строку aria2c (чистая функция — тестируется отдельно)."""
    dht = "true" if enable_dht else "false"
    args = [
        "aria2c",
        "--dir", str(output_dir),
        # ── Не работаем как сервер: не сидируем, только исходящие соединения ──
        "--seed-time=0",
        "--seed-ratio=0.0",
        "--bt-detach-seed-only=true",
        # ── Отключаем анонсы себя и UDP-listener'ы ──
        f"--enable-dht={dht}",
        f"--enable-dht6={dht}",
        "--bt-enable-lpd=false",
        "--enable-peer-exchange=false",
        # ── Фиксированный порт (не пробрасывается наружу) ──
        f"--listen-port={listen_port}",
        f"--dht-listen-port={listen_port}",
        f"--bt-max-peers={max_peers}",
        # ── Прочее ──
        "--file-allocation=none",
        "--summary-interval=1",
        "--console-log-level=warn",
        "--check-integrity=true",
        "--bt-stop-timeout=300",
        "--max-file-not-found=3",
        "--bt-tracker-connect-timeout=10",
        "--bt-tracker-timeout=10",
        "--no-conf=true",
        "--auto-file-renaming=false",
        "--allow-overwrite=false",
        # RPC НЕ включаем: никаких дополнительных слушающих сокетов.
    ]
    if download_limit and download_limit > 0:
        args.append(f"--max-overall-download-limit={download_limit}")
    if metadata_only:
        args += ["--bt-metadata-only=true", "--bt-save-metadata=true"]
    if select_indices:
        args.append("--select-file=" + ",".join(str(i) for i in select_indices))
    args.append(source)
    return args


def _dir_size_recursive(path: Path) -> int:
    """Суммарный размер всех файлов рекурсивно (мультифайловые торренты кладут файлы
    во вложенную папку — нерекурсивный _output_size их не учёл бы)."""
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


async def _terminate_proc(proc) -> None:
    """Мягко завершает aria2c (SIGTERM → SIGKILL). aria2c по SIGTERM корректно выходит."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=15)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


_ARIA2_PCT_RE = re.compile(r"\((\d+)%\)")
_ARIA2_SIZES_RE = re.compile(r"([0-9.]+[KMGT]?i?B)\s*/\s*([0-9.]+[KMGT]?i?B)")
_ARIA2_DL_RE = re.compile(r"DL:\s*([0-9.]+[KMGT]?i?B)")
_ARIA2_ETA_RE = re.compile(r"ETA:\s*(\S+)")
_SIZE_UNITS = {"B": 1, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4,
               "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}


def _parse_aria2_size(text: str) -> int:
    m = re.match(r"([0-9.]+)([KMGT]?i?B)", text)
    if not m:
        return 0
    num = float(m.group(1))
    unit = m.group(2).upper()
    return int(num * _SIZE_UNITS.get(unit, 1))


def _parse_eta_seconds(text: str) -> int:
    total = 0
    for value, unit in re.findall(r"(\d+)([dhms])", text):
        total += int(value) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return total


async def fetch_magnet_metadata(magnet: str, meta_dir: Path) -> TorrentMeta:
    """Скачивает только метаданные magnet (без данных) и возвращает TorrentMeta с размером."""
    meta_dir.mkdir(parents=True, exist_ok=True)
    base = parse_magnet(magnet)
    if not _torrent_trackers_are_safe(base.trackers):
        raise RuntimeError("Tracker points to a non-public address")
    args = _build_aria2c_args(
        magnet, meta_dir,
        listen_port=TORRENT_LISTEN_PORT, max_peers=TORRENT_MAX_PEERS,
        enable_dht=TORRENT_ENABLE_DHT, download_limit=TORRENT_DOWNLOAD_LIMIT,
        metadata_only=True,
    )
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=min(120, TORRENT_TIMEOUT))
    except asyncio.TimeoutError as e:
        await _terminate_proc(proc)
        raise TimeoutError("Magnet metadata request timed out") from e

    saved = sorted(meta_dir.glob("*.torrent"))
    if not saved:
        raise RuntimeError(
            "Could not fetch magnet metadata "
            "(magnet needs trackers, or enable TORRENT_ENABLE_DHT)"
        )
    meta = parse_torrent_file(saved[0])
    meta.source = magnet
    meta.is_magnet = True
    meta.btih = base.btih
    # Трекеры из magnet тоже проверяем (в .torrent их может не быть)
    meta.trackers = list(dict.fromkeys(meta.trackers + base.trackers))
    if not base.name.startswith("magnet-"):
        meta.name = base.name
    return meta


async def download_torrent(
    meta: TorrentMeta,
    output_dir: Path,
    progress_callback: Optional[Callable] = None,
    cancel_flag: Optional[list] = None,
    media_only: bool = True,
) -> list[DownloadResult]:
    """Скачивает торрент через aria2c. Возвращает список DownloadResult (по файлу).

    meta должен быть уже провалидирован вызывающей стороной (размер, безопасность).
    Для magnet source = magnet-URI; aria2c повторно подтянет метаданные.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    if not _torrent_trackers_are_safe(meta.trackers):
        raise RuntimeError("Tracker points to a non-public address")

    select_indices: Optional[list[int]] = None
    if media_only and meta.files:
        select_indices = [
            i for i, f in enumerate(meta.files, 1)
            if Path(f).suffix.lower() in _MEDIA_EXTS
        ]
        if not select_indices:
            return []  # в раздаче нет медиа-файлов

    args = _build_aria2c_args(
        meta.source, output_dir,
        listen_port=TORRENT_LISTEN_PORT, max_peers=TORRENT_MAX_PEERS,
        enable_dht=TORRENT_ENABLE_DHT, download_limit=TORRENT_DOWNLOAD_LIMIT,
        select_indices=select_indices,
    )
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )

    loop = asyncio.get_running_loop()
    tracker = ProgressTracker(progress_callback, loop)
    tracker.status = "downloading"
    deadline = time.monotonic() + TORRENT_TIMEOUT
    abort_reason: list[Optional[str]] = [None]

    async def _watchdog():
        last_disk = 0.0
        while proc.returncode is None:
            if cancel_flag and cancel_flag[0]:
                abort_reason[0] = "CANCELLED"
                await _terminate_proc(proc)
                return
            if time.monotonic() >= deadline:
                abort_reason[0] = "TIMEOUT"
                await _terminate_proc(proc)
                return
            now = time.monotonic()
            if now - last_disk >= 1.0:
                last_disk = now
                if not disk_has_capacity(output_dir):
                    abort_reason[0] = "DISK_FULL"
                    await _terminate_proc(proc)
                    return
                if _dir_size_recursive(output_dir) > TORRENT_MAX_TOTAL_BYTES:
                    abort_reason[0] = "TOTAL_LIMIT"
                    await _terminate_proc(proc)
                    return
            await asyncio.sleep(1.0)

    watchdog = asyncio.ensure_future(_watchdog())
    last_emit = [0.0]

    async def _emit():
        now = time.monotonic()
        if progress_callback and now - last_emit[0] >= 3.0:
            last_emit[0] = now
            try:
                await progress_callback(tracker)
            except Exception:
                pass

    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace")
            pct = _ARIA2_PCT_RE.search(text)
            sizes = _ARIA2_SIZES_RE.search(text)
            if sizes:
                tracker.downloaded = _parse_aria2_size(sizes.group(1))
                tracker.total = _parse_aria2_size(sizes.group(2))
            elif pct:
                # Проценты без явных размеров — оценим по проценту (лучше, чем ничего)
                pass
            dl = _ARIA2_DL_RE.search(text)
            if dl:
                tracker.speed = _parse_aria2_size(dl.group(1))
            eta = _ARIA2_ETA_RE.search(text)
            if eta:
                tracker.eta = _parse_eta_seconds(eta.group(1))
            if pct or sizes:
                await _emit()
    finally:
        await proc.wait()
        watchdog.cancel()
        try:
            await watchdog
        except (asyncio.CancelledError, Exception):
            pass

    reason = abort_reason[0]
    if reason == "CANCELLED" or (cancel_flag and cancel_flag[0]):
        raise DownloadCancelledError("CANCELLED")
    if reason == "TIMEOUT":
        raise TimeoutError("Torrent download timed out")
    if reason == "DISK_FULL":
        raise RuntimeError("Insufficient free disk space")
    if reason == "TOTAL_LIMIT":
        raise RuntimeError(
            f"Torrent exceeded aggregate limit: {_human_size(TORRENT_MAX_TOTAL_BYTES)}"
        )

    return _collect_torrent_results(output_dir, media_only)


def _collect_torrent_results(output_dir: Path, media_only: bool) -> list[DownloadResult]:
    """Рекурсивно собирает скачанные файлы в DownloadResult (с path-traversal guard)."""
    base_resolved = output_dir.resolve()
    results: list[DownloadResult] = []
    for f in sorted(output_dir.rglob("*")):
        if f.is_symlink() or not f.is_file():
            continue
        # Служебные файлы aria2c и сохранённые метаданные пропускаем
        if f.suffix.lower() in (".aria2", ".torrent"):
            continue
        if media_only and f.suffix.lower() not in _MEDIA_EXTS:
            continue
        try:
            f.resolve().relative_to(base_resolved)
        except ValueError:
            logger.warning("Skipping out-of-directory torrent file: %s", f)
            continue
        try:
            size = f.stat().st_size
        except OSError:
            continue
        results.append(DownloadResult(
            success=True, file_path=f, title=f.stem, file_size=size,
        ))
    return results
