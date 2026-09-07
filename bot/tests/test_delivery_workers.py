"""Regression tests for process isolation, budgets, restart and delivery retries."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

import pytest

import bot
import config
import database as db
import downloader
import fileserver
import landlock
import worker_rpc


def test_cookie_source_is_read_only_and_unchanged(tmp_path):
    source = tmp_path / 'cookies.txt'
    original = '# Netscape HTTP Cookie File\n'
    source.write_text(original)
    source.chmod(0o400)
    with downloader.SafeYoutubeDL({'cookiefile': str(source), 'quiet': True}) as ydl:
        copy = Path(ydl.params['cookiefile'])
        assert copy != source
        assert copy.stat().st_mode & 0o777 == 0o600
        _ = ydl.cookiejar
    assert not copy.exists()
    assert source.read_text() == original


def test_real_pot_provider_accepts_configured_url():
    with mock.patch.object(downloader, 'POT_PROVIDER_URL', 'http://bgutil-pot:4416'):
        with downloader.SafeYoutubeDL(downloader._base_opts()) as ydl:
            from yt_dlp.extractor.youtube import YoutubeIE
            from yt_dlp_plugins.extractor.getpot_bgutil_http import BgUtilHTTPPTP
            provider = BgUtilHTTPPTP(YoutubeIE(ydl), mock.Mock(),
                                    ydl.params['extractor_args']['youtubepot-bgutilhttp'])
            assert provider._base_url == 'http://bgutil-pot:4416'
            assert 'getpot_bgutil_baseurl' not in ydl.params['extractor_args'].get('youtube', {})


def test_unknown_signed_urls_are_not_persisted():
    for url in ('https://example.com/path?sig=synthetic', 'https://example.com/private-token',
                'https://www.youtube.com/watch?v=abcdefghijk&auth=synthetic'):
        assert bot._session_url_for_storage(url) is None
    assert bot._session_url_for_storage('https://youtu.be/abcdefghijk?si=share') == 'https://www.youtube.com/watch?v=abcdefghijk'


def test_filesystem_policy_blocks_other_jobs(tmp_path):
    if landlock.abi_version() < 3:
        pytest.skip('Host kernel does not provide Landlock ABI 3')
    allowed = tmp_path / 'own'
    allowed.mkdir()
    other = tmp_path / 'other'
    other.write_text('synthetic-private-file')
    script = '''import sys
from pathlib import Path
from landlock import restrict, system_paths
own, other = map(Path, sys.argv[1:])
restrict(system_paths(), [own])
(own / 'output').write_text('ok')
try:
    other.read_text()
except PermissionError:
    print('blocked')
else:
    raise SystemExit('filesystem isolation failed')
'''
    env = {'PATH': os.environ['PATH'], 'PYTHONDONTWRITEBYTECODE': '1',
           'PYTHONPATH': str(Path(landlock.__file__).parent)}
    result = subprocess.run([sys.executable, '-c', script, str(allowed), str(other)],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'blocked'


def test_process_watchdog_kills_child_group(tmp_path):
    async def run():
        marker = tmp_path / 'pid'
        script = tmp_path / 'blocked.py'
        script.write_text('''import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(60)
''')
        original = asyncio.create_subprocess_exec
        processes = []

        async def spawn(*args, **kwargs):
            proc = await original(sys.executable, str(script), str(marker), **kwargs)
            processes.append(proc)
            return proc

        with mock.patch.object(asyncio, 'create_subprocess_exec', side_effect=spawn):
            with pytest.raises(TimeoutError):
                await worker_rpc._call('supported', {'urls': []}, timeout=1)
        assert processes[0].returncode is not None
        pid = int(marker.read_text())
        status = Path(f'/proc/{pid}/stat')
        # SIGKILL delivery is asynchronous; allow the kernel to schedule the child.
        async with asyncio.timeout(2):
            while status.exists():
                try:
                    if status.read_text().split()[2] == 'Z':
                        break
                except FileNotFoundError:
                    break
                await asyncio.sleep(.01)
        assert not worker_rpc._TASKS
    asyncio.run(run())


def test_cancel_removes_queued_download_without_a_slot():
    async def run():
        flag = [False]
        sem = asyncio.Semaphore(0)
        async def queued():
            async with bot._download_slot(sem, flag):
                assert flag[0]
        task = asyncio.create_task(queued())
        await asyncio.sleep(.01)
        flag[0] = True
        await asyncio.wait_for(task, .5)
        assert sem.locked()
    asyncio.run(run())


def test_restore_delivery_keeps_owner_and_attempt_budget(tmp_path):
    db.upsert_user(10, '', 'test')
    download_id = db.add_download(10, 'https://www.youtube.com/watch')
    source = tmp_path / 'video.mp4'
    source.write_bytes(b'synthetic')
    with mock.patch.object(config, 'DOWNLOAD_DIR', tmp_path):
        token = fileserver.move_and_register(source, download_id=download_id)
        entry = fileserver.get_entry(token)
        entry.attempts, entry.bytes_sent = 3, 9
        fileserver._persist(token[:32], entry)
        fileserver._registry.clear()
        fileserver._restore_registry()
        restored = fileserver.get_entry(token)
        assert (restored.download_id, restored.attempts, restored.bytes_sent) == (download_id, 3, 9)
        assert db.get_delivery(download_id, 10)
        assert db.get_delivery(download_id, 11) is None
        fileserver.unregister(token, delete_file=True)


def test_unknown_length_download_cannot_exceed_file_budget(tmp_path):
    if landlock.abi_version() < 3:
        pytest.skip('Host kernel does not provide Landlock ABI 3')
    requested = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_HEAD(self):
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.end_headers()
        def do_GET(self):
            requested.set()
            self.do_HEAD()
            try:
                self.wfile.write(b'x' * 8192)
            except ConnectionError:
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with (mock.patch.object(downloader, 'SSRF_PROTECTION', False),
              mock.patch.object(downloader, 'ALLOW_GENERIC_URLS', True),
              mock.patch.object(downloader, 'EMBED_METADATA', False),
              mock.patch.object(downloader, 'EMBED_THUMBNAIL', False),
              mock.patch.object(downloader, 'MIN_FREE_DISK_BYTES', 0),
              mock.patch.object(downloader, 'MAX_FILE_SIZE_BYTES', 1024),
              mock.patch.object(config, 'MAX_FILE_SIZE_BYTES', 1024)):
            result = asyncio.run(worker_rpc.download_video(
                f'http://127.0.0.1:{server.server_port}/file.mp4', 'best', tmp_path / 'output'))
        assert not result.success
        assert requested.is_set(), result.error
        assert 'limit' in result.error.lower() or 'too large' in result.error.lower(), result.error
        assert all(path.stat().st_size <= 1024 for path in (tmp_path / 'output').rglob('*') if path.is_file())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_non_music_formats_use_document(tmp_path):
    async def run():
        for extension in ('opus', 'wav', 'flac'):
            source = tmp_path / ('audio.' + extension)
            source.write_bytes(b'media')
            client = SimpleNamespace(send_audio=mock.AsyncMock(), send_document=mock.AsyncMock())
            with mock.patch.object(config, 'LOCAL_API_SERVER', ''):
                await bot._deliver_file(1, downloader.DownloadResult(True, source, file_size=5), client)
            client.send_audio.assert_not_awaited()
            client.send_document.assert_awaited_once()
    asyncio.run(run())
