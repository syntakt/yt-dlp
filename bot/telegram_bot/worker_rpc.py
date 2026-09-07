"""Bounded subprocesses for untrusted extraction and media processing."""

import asyncio
import base64
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import weakref

import config
import downloader

_SLOTS = weakref.WeakKeyDictionary()
_TASKS: set[asyncio.Task] = set()
_OUTPUTS: set[Path] = set()
_STOPPING = False


def download_work_in_progress(path):
    path = Path(path).resolve()
    return (any(path == out or path in out.parents for out in _OUTPUTS)
            or downloader.download_work_in_progress(path))


async def shutdown():
    global _STOPPING
    _STOPPING = True
    tasks = list(_TASKS)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _settings():
    # Only downloader settings are sent to the child, never bot/API credentials.
    return {key: value for key, value in vars(downloader).items()
            if key.isupper() and not key.startswith('_')
            and isinstance(value, (str, int, float, bool, list))}


async def _terminate(proc):
    # Kill the group even if the direct child has already exited: FFmpeg may remain.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        if sig == signal.SIGTERM:
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                pass
    await proc.wait()


def _check_storage(output, total_limit):
    if not output or not output.exists():
        return
    total = 0
    for directory, _, files in os.walk(output, followlinks=False):
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                raise RuntimeError('Unsafe output symlink')
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            if size > config.MAX_FILE_SIZE_BYTES:
                raise RuntimeError('File exceeded download size limit')
            total += size
    if total > total_limit:
        raise RuntimeError('Download exceeded temporary storage limit')
    if not downloader.disk_has_capacity(output):
        raise RuntimeError('Insufficient free disk space')


async def _call(operation, params, *, timeout, cancel_flag=None, progress_callback=None,
                output=None, total_limit=None):
    if _STOPPING:
        raise downloader.DownloadCancelledError('CANCELLED')
    task = asyncio.current_task()
    _TASKS.add(task)
    proc = reader = None
    output = Path(output).resolve() if output else None
    loop = asyncio.get_running_loop()
    slots = _SLOTS.setdefault(loop, asyncio.Semaphore(max(4, config.MAX_CONCURRENT_DOWNLOADS + 2)))
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while not acquired:
            if cancel_flag and cancel_flag[0]:
                raise downloader.DownloadCancelledError('CANCELLED')
            if time.monotonic() >= deadline:
                raise TimeoutError('Worker queue timed out')
            try:
                await asyncio.wait_for(slots.acquire(), .2)
                acquired = True
            except asyncio.TimeoutError:
                continue
        if output:
            output.mkdir(parents=True, exist_ok=True)
            _OUTPUTS.add(output)
        with tempfile.TemporaryDirectory(prefix='ytdlp-worker-') as temp:
            env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'TZ', 'SSL_CERT_FILE', 'SSL_CERT_DIR')
                   if key in os.environ}
            env.update(HOME=temp, TMPDIR=temp, PYTHONDONTWRITEBYTECODE='1',
                       PYTHONPATH=str(Path(downloader.yt_dlp.__file__).resolve().parent.parent))
            payload = {'operation': operation, 'params': params, 'settings': _settings(),
                       'file_limit': config.MAX_FILE_SIZE_BYTES}
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).resolve()), '--worker',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env, cwd=temp,
                start_new_session=True, limit=16 * 1024 * 1024,
            )
            proc.stdin.write(json.dumps(payload, default=str).encode() + b'\n')
            await proc.stdin.drain()
            proc.stdin.close()
            reader = asyncio.create_task(proc.stdout.readline())
            try:
                while True:
                    if cancel_flag and cancel_flag[0]:
                        raise downloader.DownloadCancelledError('CANCELLED')
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Media worker timed out')
                    _check_storage(output, total_limit or 3 * config.MAX_FILE_SIZE_BYTES)
                    done, _ = await asyncio.wait({reader}, timeout=.2)
                    if not done:
                        continue
                    line = reader.result()
                    if not line:
                        raise RuntimeError('Media worker exited without a result')
                    message = json.loads(line)
                    if message['type'] == 'result':
                        return message['value']
                    if message['type'] == 'error':
                        raise RuntimeError(message['value'])
                    if message['type'] == 'progress' and progress_callback:
                        tracker = downloader.ProgressTracker()
                        for key in ('downloaded', 'total', 'speed', 'eta', 'status'):
                            setattr(tracker, key, message['value'][key])
                        # Slow Telegram requests must not suspend the watchdog.
                        try:
                            await asyncio.wait_for(progress_callback(tracker), 1)
                        except (Exception, asyncio.TimeoutError):
                            pass
                    reader = asyncio.create_task(proc.stdout.readline())
            finally:
                if reader:
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
                await _terminate(proc)
    finally:
        # Submission/stdin failures also require reaping the whole process group.
        if proc and proc.returncode is None:
            await _terminate(proc)
        if output:
            _OUTPUTS.discard(output)
        if acquired:
            slots.release()
        _TASKS.discard(task)


def _result(value):
    value['file_path'] = Path(value['file_path']) if value.get('file_path') else None
    value['parts'] = [Path(path) for path in value.get('parts', [])]
    return downloader.DownloadResult(**value)


async def get_video_info(url):
    value = await _call('info', {'url': url}, timeout=config.INFO_TIMEOUT)
    value['formats'] = [downloader.FormatInfo(**item) for item in value.get('formats', [])]
    return downloader.VideoInfo(**value)


async def get_thumbnail(url):
    return base64.b64decode(await _call('thumbnail', {'url': url}, timeout=config.INFO_TIMEOUT))


async def supported_urls(urls):
    return await _call('supported', {'urls': urls[:5]}, timeout=config.INFO_TIMEOUT)


async def search_videos(query):
    return await _call('search', {'query': query}, timeout=config.INFO_TIMEOUT)


async def download_video(url, format_id, output_dir, progress_callback=None, cancel_flag=None, **kwargs):
    try:
        value = await _call('video', dict(url=url, format_id=format_id, output_dir=str(output_dir), **kwargs),
                            output=output_dir, timeout=config.DOWNLOAD_TIMEOUT,
                            cancel_flag=cancel_flag, progress_callback=progress_callback)
        return _result(value)
    except downloader.DownloadCancelledError:
        return downloader.DownloadResult(success=False, error='CANCELLED')
    except (RuntimeError, TimeoutError) as exc:
        return downloader.DownloadResult(success=False, error=str(exc))


async def download_playlist(url, format_id, output_dir, max_items=10, cancel_flag=None, **kwargs):
    values = await _call('playlist', dict(url=url, format_id=format_id, output_dir=str(output_dir),
                                        max_items=max_items, **kwargs),
                         output=output_dir, timeout=config.DOWNLOAD_TIMEOUT * max(1, max_items),
                         cancel_flag=cancel_flag, total_limit=3 * config.MAX_PLAYLIST_TOTAL_BYTES)
    return [_result(value) for value in values]


async def fetch_magnet_metadata(magnet, meta_dir):
    value = await _call('magnet', {'magnet': magnet, 'output_dir': str(meta_dir)},
                        output=meta_dir, timeout=min(120, config.TORRENT_TIMEOUT))
    return downloader.TorrentMeta(**value)


async def download_torrent(meta, output_dir, progress_callback=None, cancel_flag=None, media_only=True):
    values = await _call('torrent', {'meta': asdict(meta), 'output_dir': str(output_dir), 'media_only': media_only},
                         output=output_dir, timeout=config.TORRENT_TIMEOUT, progress_callback=progress_callback,
                         cancel_flag=cancel_flag, total_limit=config.TORRENT_MAX_TOTAL_BYTES)
    return [_result(value) for value in values]


async def _worker():
    import resource
    request = json.loads(sys.stdin.readline(2 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (request['file_limit'], request['file_limit']))
    for key, value in request['settings'].items():
        if key in _settings():
            setattr(downloader, key, value)
    downloader._SSRF_ALLOWED_HOSTS = downloader._ssrf_allowed_hosts()
    params = request['params']
    if 'output_dir' in params:
        params['output_dir'] = Path(params['output_dir'])
    protocol = sys.stdout
    # Third-party diagnostics must not corrupt the JSON stream or expose credentials.
    sys.stdout = sys.stderr

    def emit(kind, value):
        protocol.write(json.dumps({'type': kind, 'value': value}, default=str) + '\n')
        protocol.flush()

    async def progress(tracker):
        emit('progress', {key: getattr(tracker, key) for key in ('downloaded', 'total', 'speed', 'eta', 'status')})

    try:
        from landlock import restrict, system_paths
        read_paths = system_paths() + [Path(downloader.yt_dlp.__file__).resolve().parent]
        if downloader.COOKIES_FILE:
            read_paths.append(Path(downloader.COOKIES_FILE))
        if request['operation'] == 'torrent' and not params['meta']['is_magnet']:
            read_paths.append(Path(params['meta']['source']))
        write_paths = [Path.cwd()]
        if params.get('output_dir'):
            write_paths.append(params['output_dir'])
            os.environ['YTDLP_MEDIA_ROOT'] = str(params['output_dir'])
        os.environ['YTDLP_WORKER_SANDBOX'] = '1'
        os.environ['YTDLP_FFMPEG_NETWORK'] = '1' if (not downloader.SSRF_PROTECTION or downloader.TRUST_EXTERNAL_NETWORK_FOR_SSRF) else '0'
        ffmpeg_temp = Path.cwd() / 'ffmpeg'
        ffmpeg_temp.mkdir()
        os.environ['YTDLP_FFMPEG_TMP'] = str(ffmpeg_temp)
        restrict(read_paths, write_paths)
        operation = request['operation']
        if operation == 'supported':
            result = [url for url in params['urls'] if downloader.is_supported_url(url)]
        elif operation == 'thumbnail':
            result = base64.b64encode(await downloader.get_thumbnail(**params)).decode()
        elif operation == 'info':
            result = asdict(await downloader.get_video_info(**params))
        elif operation == 'search':
            result = await downloader.search_videos(**params)
        elif operation == 'video':
            result = asdict(await downloader.download_video(**params, progress_callback=progress))
        elif operation == 'playlist':
            result = [asdict(item) for item in await downloader.download_playlist(**params)]
        elif operation in ('torrent', 'magnet'):
            if downloader.SSRF_PROTECTION and not downloader.TRUST_EXTERNAL_NETWORK_FOR_SSRF:
                raise RuntimeError('Торренты отключены сетевой политикой сервера')
            if operation == 'magnet':
                result = asdict(await downloader.fetch_magnet_metadata(params['magnet'], params['output_dir']))
            else:
                params['meta'] = downloader.TorrentMeta(**params['meta'])
                result = [asdict(item) for item in await downloader.download_torrent(**params, progress_callback=progress)]
        else:
            raise ValueError('Unknown worker operation')
        emit('result', result)
    except Exception as exc:
        emit('error', downloader._YDLBotLogger._redact(exc))


if __name__ == '__main__':
    asyncio.run(_worker())
