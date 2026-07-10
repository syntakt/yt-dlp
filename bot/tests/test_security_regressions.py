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


if __name__ == '__main__':
    unittest.main()
