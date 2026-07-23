import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


BOT_DIR = Path(__file__).resolve().parents[1]
MODULE_DIR = BOT_DIR / 'telegram_bot'
sys.path.insert(0, str(MODULE_DIR))

os.environ.setdefault('DOWNLOAD_DIR', tempfile.gettempdir())
os.environ.setdefault('DB_PATH', str(Path(tempfile.gettempdir()) / 'yt-dlp-bot-test.db'))

import bot  # noqa: E402
import config  # noqa: E402
import database  # noqa: E402
import downloader  # noqa: E402
import fileserver  # noqa: E402


def _video_info(title: str = 'test') -> downloader.VideoInfo:
    return downloader.VideoInfo(
        url='https://example.com/video',
        title=title,
        uploader='uploader',
        duration=1,
        view_count=0,
        like_count=None,
        thumbnail='',
        description='',
    )


class SessionAndConcurrencyTests(unittest.TestCase):
    def test_persisted_errors_redact_urls_and_bot_token(self):
        with mock.patch.object(config, 'BOT_TOKEN', '123:secret'):
            error = bot._safe_error_text(
                'failed https://example.com/video?token=value via 123:secret'
            )
        self.assertNotIn('token=value', error)
        self.assertNotIn('123:secret', error)
        self.assertIn('<URL>', error)

    def test_fallback_session_is_bound_to_exact_message(self):
        ctx = SimpleNamespace(user_data={})
        info = _video_info()
        bot._remember_bound_session(ctx, 10, 20, info.url, info)

        with mock.patch.object(bot.db, 'get_session', return_value=None):
            restored, url = bot._restore_session(ctx, 10, 20, user_id=1)
            missing = bot._restore_session(ctx, 10, 21, user_id=1)

        self.assertIs(restored, info)
        self.assertEqual(url, info.url)
        self.assertEqual(missing, (None, None))

    def test_per_user_active_limit_is_released(self):
        ctx = SimpleNamespace(bot_data={})
        with mock.patch.object(config, 'MAX_CONCURRENT_DOWNLOADS_PER_USER', 1):
            self.assertIsNone(bot._claim_download(ctx, 1, 10, 20))
            self.assertIsNotNone(bot._claim_download(ctx, 1, 10, 21))
            bot._release_download(ctx, 1, 10, 20)
            self.assertIsNone(bot._claim_download(ctx, 1, 10, 21))


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_fileserver_start_failure_aborts_bot_startup(self):
        application = SimpleNamespace(bot_data={}, bot=mock.AsyncMock())
        with (
            mock.patch.object(config, 'PUBLIC_BASE_URL', 'https://files.example'),
            mock.patch.object(config, 'DIRECT_BASE_URL', ''),
            mock.patch.object(config, 'RELAY_BASE_URLS', []),
            mock.patch.object(
                fileserver,
                'start',
                new=mock.AsyncMock(side_effect=OSError('address in use')),
            ),
        ):
            with self.assertRaises(OSError):
                await bot._post_init(application)


class DownloaderSecurityTests(unittest.TestCase):
    def tearDown(self):
        downloader._NETWORK_GUARD.enabled = False

    def test_guarded_dns_rejects_private_redirect_target(self):
        private_answer = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))
        ]
        downloader._NETWORK_GUARD.enabled = True
        with mock.patch.object(
            downloader, '_ORIGINAL_GETADDRINFO', return_value=private_answer
        ):
            with self.assertRaises(socket.gaierror):
                downloader._guarded_getaddrinfo('redirect.example', 443)

    def test_guarded_dns_allows_public_target(self):
        public_answer = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))
        ]
        downloader._NETWORK_GUARD.enabled = True
        with mock.patch.object(
            downloader, '_ORIGINAL_GETADDRINFO', return_value=public_answer
        ):
            self.assertEqual(
                downloader._guarded_getaddrinfo('public.example', 443),
                public_answer,
            )

    def test_disk_capacity_preserves_reserve_and_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(downloader, 'MIN_FREE_DISK_BYTES', 1_000),
                mock.patch.object(
                    downloader.shutil,
                    'disk_usage',
                    return_value=SimpleNamespace(free=1_499),
                ),
            ):
                self.assertFalse(downloader.disk_has_capacity(Path(tmp), 500))
                self.assertTrue(downloader.disk_has_capacity(Path(tmp), 499))


class FileServerAtomicityTests(unittest.TestCase):
    def setUp(self):
        fileserver._registry.clear()

    def tearDown(self):
        fileserver._registry.clear()

    def test_failed_registration_rolls_file_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'user_1' / 'file.bin'
            source.parent.mkdir()
            source.write_bytes(b'data')
            with (
                mock.patch.object(config, 'DOWNLOAD_DIR', root),
                mock.patch.object(
                    fileserver, '_validate_served_path', side_effect=ValueError('fail')
                ),
            ):
                with self.assertRaises(ValueError):
                    fileserver.move_and_register(source)

            self.assertEqual(source.read_bytes(), b'data')
            self.assertEqual(fileserver._registry, {})

    def test_registered_file_is_isolated_and_deleted_on_unregister(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'user_1' / 'file.bin'
            source.parent.mkdir()
            source.write_bytes(b'data')
            with mock.patch.object(config, 'DOWNLOAD_DIR', root):
                token = fileserver.move_and_register(source)
                entry = fileserver.get_entry(token)
                self.assertIsNotNone(entry)
                entry.path.resolve().relative_to((root / 'fileserver').resolve())
                fileserver.unregister(token, delete_file=True)
                self.assertFalse(entry.path.exists())

    def test_single_file_delivery_updates_history_after_http_result(self):
        entry = fileserver.FileEntry(
            path=Path('/unused'),
            filename='file.bin',
            file_size=4,
            expires_at=0,
            download_id=42,
        )
        with mock.patch.object(database, 'update_download') as update:
            fileserver._update_download_status(entry, 'done')
        update.assert_called_once_with(42, status='done', error=None)


class DatabaseStatusTests(unittest.TestCase):
    def test_terminal_statuses_set_finished_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(database, 'DB_PATH', Path(tmp) / 'bot.db'):
                database.init_db()
                database.upsert_user(1, 'user', 'User')
                for status in ('done', 'error', 'cancelled', 'partial'):
                    download_id = database.add_download(1, 'https://example.com/video')
                    database.update_download(download_id, status=status)
                    row = database.get_user_history(1, limit=1)[0]
                    self.assertEqual(row['status'], status)
                    self.assertIsNotNone(row['finished_at'])

                ready_id = database.add_download(1, 'https://example.com/video')
                database.update_download(ready_id, status='ready')
                ready = database.get_user_history(1, limit=1)[0]
                self.assertIsNone(ready['finished_at'])


class ConfigParsingTests(unittest.TestCase):
    def test_invalid_numeric_environment_uses_default(self):
        env = os.environ.copy()
        env.update({
            'PYTHONPATH': str(MODULE_DIR),
            'MAX_CONCURRENT_DOWNLOADS': 'not-a-number',
            'FILE_TTL_HOURS': 'invalid',
        })
        result = subprocess.run(
            [
                sys.executable,
                '-c',
                'import config; print(config.MAX_CONCURRENT_DOWNLOADS, '
                'config.FILE_TTL_SECONDS)',
            ],
            env=env,
            cwd=MODULE_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), '3 3600')


def _bencode(o):
    if isinstance(o, int):
        return b'i' + str(o).encode() + b'e'
    if isinstance(o, bytes):
        return str(len(o)).encode() + b':' + o
    if isinstance(o, str):
        return _bencode(o.encode())
    if isinstance(o, list):
        return b'l' + b''.join(_bencode(x) for x in o) + b'e'
    if isinstance(o, dict):
        return b'd' + b''.join(_bencode(k) + _bencode(v) for k, v in o.items()) + b'e'
    raise TypeError(type(o))


class TorrentTests(unittest.TestCase):
    def _write_torrent(self, data: dict) -> Path:
        p = Path(tempfile.mktemp(suffix='.torrent'))
        p.write_bytes(_bencode(data))
        self.addCleanup(lambda: p.unlink(missing_ok=True))
        return p

    def test_parse_single_file_torrent(self):
        p = self._write_torrent({
            b'announce': b'udp://tracker.example:1337/announce',
            b'info': {b'length': 123456, b'name': b'movie.mp4',
                      b'piece length': 16384, b'pieces': b''},
        })
        meta = downloader.parse_torrent_file(p)
        self.assertEqual(meta.name, 'movie.mp4')
        self.assertEqual(meta.total_size, 123456)
        self.assertEqual(meta.files, ['movie.mp4'])
        self.assertIn('udp://tracker.example:1337/announce', meta.trackers)

    def test_parse_multi_file_torrent_and_media_filter(self):
        p = self._write_torrent({
            b'info': {b'name': b'dir', b'piece length': 16384, b'pieces': b'',
                      b'files': [
                          {b'length': 100, b'path': [b'sub', b'a.mkv']},
                          {b'length': 200, b'path': [b'readme.txt']},
                      ]},
        })
        meta = downloader.parse_torrent_file(p)
        self.assertEqual(meta.total_size, 300)
        self.assertEqual(meta.files, ['sub/a.mkv', 'readme.txt'])
        # только медиа отбирается
        self.assertEqual(meta.media_files, ['sub/a.mkv'])

    def test_parse_magnet_valid_and_invalid(self):
        meta = downloader.parse_magnet(
            'magnet:?xt=urn:btih:' + 'A' * 40 + '&dn=Cool+Video'
            '&tr=udp://tracker.example:80/announce'
        )
        self.assertTrue(meta.is_magnet)
        self.assertEqual(meta.btih, 'a' * 40)  # нормализуется в lower
        self.assertEqual(meta.name, 'Cool Video')
        self.assertIn('udp://tracker.example:80/announce', meta.trackers)
        self.assertIsNone(meta.total_size)
        with self.assertRaises(ValueError):
            downloader.parse_magnet('magnet:?xt=urn:btih:zzz')
        with self.assertRaises(ValueError):
            downloader.parse_magnet('https://example.com/not-a-magnet')

    def test_bencode_rejects_malformed(self):
        with self.assertRaises(ValueError):
            downloader._bdecode(b'd3:fooi1e')  # незакрытый dict

    def test_tracker_ssrf_check_blocks_private_hosts(self):
        with mock.patch.object(downloader, 'SSRF_PROTECTION', True):
            self.assertFalse(
                downloader._torrent_trackers_are_safe(['http://127.0.0.1:80/announce'])
            )
            self.assertFalse(
                downloader._torrent_trackers_are_safe(['udp://10.10.2.3:1337/announce'])
            )
        # при выключенной защите — не блокируем
        with mock.patch.object(downloader, 'SSRF_PROTECTION', False):
            self.assertTrue(
                downloader._torrent_trackers_are_safe(['http://127.0.0.1:80/announce'])
            )

    def test_aria2c_args_are_hardened(self):
        args = downloader._build_aria2c_args(
            'magnet:?xt=urn:btih:' + 'a' * 40,
            Path('/tmp/torrent-out'),
            listen_port=51413, max_peers=50, enable_dht=False, download_limit=0,
        )
        joined = ' '.join(args)
        # не сидируем, не работаем как сервер
        self.assertIn('--seed-time=0', args)
        self.assertIn('--seed-ratio=0.0', args)
        # никаких UDP-listener'ов / анонсов себя
        self.assertIn('--enable-dht=false', args)
        self.assertIn('--enable-dht6=false', args)
        self.assertIn('--bt-enable-lpd=false', args)
        self.assertIn('--enable-peer-exchange=false', args)
        # фиксированный порт
        self.assertIn('--listen-port=51413', args)
        self.assertIn('--dht-listen-port=51413', args)
        # никакого RPC-сокета
        self.assertNotIn('--enable-rpc', joined)

    def test_aria2c_args_dht_toggle_and_select(self):
        args = downloader._build_aria2c_args(
            'src', Path('/tmp/o'), listen_port=6881, max_peers=10,
            enable_dht=True, download_limit=1000, select_indices=[1, 3],
        )
        self.assertIn('--enable-dht=true', args)
        self.assertIn('--max-overall-download-limit=1000', args)
        self.assertIn('--select-file=1,3', args)

    def test_collect_torrent_results_media_only_and_traversal(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(base, ignore_errors=True))
        (base / 'sub').mkdir()
        (base / 'sub' / 'a.mp4').write_bytes(b'x' * 10)
        (base / 'readme.txt').write_bytes(b'y' * 5)
        (base / 'a.mp4.aria2').write_bytes(b'z')  # служебный файл aria2c
        results = downloader._collect_torrent_results(base, media_only=True)
        names = sorted(r.file_path.name for r in results)
        self.assertEqual(names, ['a.mp4'])

    def test_magnet_regex_detects_link(self):
        text = 'смотри magnet:?xt=urn:btih:' + 'b' * 40 + '&dn=x вот'
        found = bot._MAGNET_RE.findall(text)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith('magnet:?xt=urn:btih:'))


if __name__ == '__main__':
    unittest.main()
