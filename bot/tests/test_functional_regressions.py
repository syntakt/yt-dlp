"""Регрессии на функциональные баги, найденные аудитом.

Каждый тест соответствует конкретной поломке, которая уже случалась:
субтитры не доставлялись, очистка сносила активную загрузку, /status у админа
упирался в лимит Telegram, кнопка «в Telegram» предлагалась для файлов, которые
Telegram не примет, WAV оценивался как MP3.
"""

import os
from pathlib import Path
import sys
import tempfile
import time
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
import downloader  # noqa: E402
import fileserver  # noqa: E402


class _FakeYDL:
    """Подменяет yt_dlp.YoutubeDL: запоминает opts и «скачивает» пустой файл."""

    captured_opts: dict = {}
    target: Path = Path()
    info: dict = {}

    def __init__(self, opts):
        type(self).captured_opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return dict(type(self).info)

    def prepare_filename(self, info):
        type(self).target.parent.mkdir(parents=True, exist_ok=True)
        type(self).target.write_bytes(b'x' * 16)
        return str(type(self).target)


class SubtitleDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """Раньше writesubtitles включался, но .vtt оставался в tmp_dir и удалялся."""

    async def _run(self, subtitle_lang, info_extra):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _FakeYDL.target = out / 'video.mp4'
            _FakeYDL.info = {'title': 'video', **info_extra}
            with mock.patch.object(downloader, 'SafeYoutubeDL', _FakeYDL):
                result = await downloader.download_video(
                    url='https://example.com/v',
                    format_id='best',
                    output_dir=out,
                    subtitle_lang=subtitle_lang,
                )
            return result, _FakeYDL.captured_opts

    async def test_subtitles_are_embedded_into_the_file(self):
        result, opts = await self._run('ru', {'requested_subtitles': {'ru': {}}})
        self.assertTrue(result.success)
        self.assertTrue(result.has_subtitles)
        self.assertTrue(opts['writesubtitles'])
        self.assertEqual(opts['subtitleslangs'], ['ru'])
        keys = [pp['key'] for pp in opts.get('postprocessors', [])]
        self.assertIn('FFmpegEmbedSubtitle', keys)

    async def test_missing_subtitles_are_reported_not_silently_dropped(self):
        result, _opts = await self._run('ru', {})
        self.assertTrue(result.success)
        self.assertFalse(result.has_subtitles)
        self.assertIn('не найдены', bot._subtitle_note('s', result))

    async def test_plain_video_download_has_no_subtitle_postprocessor(self):
        result, opts = await self._run(None, {})
        self.assertTrue(result.success)
        self.assertNotIn('writesubtitles', opts)
        self.assertEqual(bot._subtitle_note('v', result), '')


class FormatSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_uses_real_height_not_numeric_format_id(self):
        """format_id «137» у YouTube означает 1080p, а не height<=137."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _FakeYDL.target = out / 'video.mp4'
            _FakeYDL.info = {'title': 'video'}
            with mock.patch.object(downloader, 'SafeYoutubeDL', _FakeYDL):
                await downloader.download_video(
                    url='https://example.com/v', format_id='137',
                    output_dir=out, max_height=1080,
                )
        self.assertEqual(
            _FakeYDL.captured_opts['format'],
            '137+bestaudio/best[height<=1080]/best',
        )

    def test_height_from_resolution(self):
        self.assertEqual(downloader.height_from_resolution('1080p'), 1080)
        self.assertIsNone(downloader.height_from_resolution('audio only'))
        self.assertIsNone(downloader.height_from_resolution(None))

    def test_dedup_keeps_highest_bitrate_and_ignores_storyboards(self):
        raw = [
            {'format_id': 'sb0', 'ext': 'mhtml', 'height': 720, 'vcodec': 'none',
             'acodec': 'none', 'tbr': 1},
            {'format_id': 'low', 'ext': 'mp4', 'height': 720, 'vcodec': 'avc1',
             'acodec': 'none', 'tbr': 500},
            {'format_id': 'high', 'ext': 'mp4', 'height': 720, 'vcodec': 'avc1',
             'acodec': 'none', 'tbr': 2500},
        ]
        parsed = downloader._parse_formats(raw)
        self.assertEqual([f.format_id for f in parsed], ['high'])


class SizeEstimationTests(unittest.TestCase):
    def test_wav_is_not_estimated_as_mp3(self):
        duration = 600  # 10 минут
        mp3 = bot._estimate_download_size(None, duration, True, 'mp3')
        opus = bot._estimate_download_size(None, duration, True, 'opus')
        wav = bot._estimate_download_size(None, duration, True, 'wav')
        self.assertLess(opus, mp3)
        self.assertGreater(wav, mp3 * 5)
        # 10 минут WAV ≈ 100 МБ — оценка должна быть в этом порядке
        self.assertAlmostEqual(wav / (1024 * 1024), 100, delta=15)


class TelegramUploadLimitTests(unittest.TestCase):
    def test_oversized_file_hides_telegram_button_but_keeps_links(self):
        big = config.TELEGRAM_UPLOAD_LIMIT_BYTES + 1
        with (
            mock.patch.object(config, 'PUBLIC_BASE_URL', 'https://cf.example'),
            mock.patch.object(config, 'DIRECT_BASE_URL', ''),
            mock.patch.object(config, 'RELAY_BASE_URLS', []),
        ):
            small_buttons = bot._build_delivery_buttons(1, 1024)
            big_buttons = bot._build_delivery_buttons(1, big)
        small_actions = [b[0].callback_data for b in small_buttons]
        big_actions = [b[0].callback_data for b in big_buttons]
        self.assertIn('deliver:1:tg', small_actions)
        self.assertNotIn('deliver:1:tg', big_actions)
        self.assertIn('deliver:1:link', big_actions)


class MessageLimitTests(unittest.IsolatedAsyncioTestCase):
    def test_join_within_limit_cuts_on_line_boundary(self):
        lines = [f'line-{i}' for i in range(100)]
        text, dropped = bot._join_within_limit(lines, budget=50)
        self.assertGreater(dropped, 0)
        self.assertLessEqual(len(text), 50)
        self.assertTrue(all(line in lines for line in text.split('\n')))

    async def test_admin_status_stays_within_telegram_limit(self):
        users = [
            {
                'user_id': 1000 + i,
                'username': f'user{i}' * 3,
                'full_name': f'Пользователь {i}',
                'is_approved': 1, 'is_banned': 0, 'is_admin': 0,
                'created_at': '2026-01-01T00:00:00+00:00',
                'approved_at': None, 'approved_by': None,
                'downloads': 5, 'total_bytes': 12345678,
                'last_download_at': '2026-02-02T00:00:00+00:00',
            }
            for i in range(200)
        ]
        stats = {'total_users': 200, 'total_downloads': 10,
                 'total_size_bytes': 999, 'pending_requests': 0}
        with (
            mock.patch.object(bot.db, 'get_global_stats', return_value=stats),
            mock.patch.object(bot.db, 'get_user_stats',
                              return_value={'downloads': 1, 'total_bytes': 2}),
            mock.patch.object(bot.db, 'get_all_users_detailed', return_value=users),
            mock.patch.object(bot.db, 'is_super_admin', return_value=True),
            mock.patch.object(config, 'ADMIN_IDS', [1]),
            mock.patch.object(
                bot.shutil, 'disk_usage',
                return_value=SimpleNamespace(free=10, total=100, used=90),
            ),
        ):
            text = await bot._build_status_text(1, verbose=True)
        self.assertLess(len(text), 4096)
        self.assertIn('/users', text)


class CleanupProtectsActiveDownloadsTests(unittest.IsolatedAsyncioTestCase):
    """_cleanup_job удалял user_<id> целиком: его mtime не меняется при записи
    в user_<id>/pl_7/, поэтому многочасовой плейлист сносился на ходу."""

    async def _run_cleanup(self, root: Path):
        with (
            mock.patch.object(config, 'DOWNLOAD_DIR', root),
            mock.patch.object(bot.db, 'cleanup_old_sessions', return_value=0),
            mock.patch.object(bot.db, 'cleanup_old_history', return_value=0),
        ):
            await bot._cleanup_job()

    async def test_active_download_dir_survives_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / 'user_1' / 'pl_7'
            active.mkdir(parents=True)
            (active / 'video.mp4').write_bytes(b'x')
            stale = root / 'user_2' / 'dl_1'
            stale.mkdir(parents=True)
            old = time.time() - 30 * 3600
            for path in (root / 'user_1', root / 'user_2'):
                os.utime(path, (old, old))

            marked = bot._mark_dir_active(active)
            try:
                await self._run_cleanup(root)
                self.assertTrue(active.exists(), 'активная загрузка была удалена')
                self.assertFalse((root / 'user_2').exists())
            finally:
                bot._unmark_dir_active(marked)

            os.utime(root / 'user_1', (old, old))
            await self._run_cleanup(root)
            self.assertFalse((root / 'user_1').exists())


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        bot._USER_ACTIONS.clear()

    tearDown = setUp

    def test_user_actions_are_throttled_per_minute(self):
        with mock.patch.object(config, 'USER_ACTIONS_PER_MINUTE', 3):
            self.assertTrue(all(bot._allow_user_action(7) for _ in range(3)))
            self.assertFalse(bot._allow_user_action(7))
            # другой пользователь не задет
            self.assertTrue(bot._allow_user_action(8))


class TokenValidationTests(unittest.TestCase):
    def test_non_hex_tokens_are_rejected(self):
        valid = 'a' * 32
        self.assertEqual(fileserver._verify_token(valid), valid)
        for bad in ('0x' + 'a' * 30, '-' + 'a' * 31, 'A' * 32, 'a' * 31, 'a' * 33):
            self.assertIsNone(fileserver._verify_token(bad), bad)


class FileServerCleanupTests(unittest.TestCase):
    def test_missing_file_still_removes_empty_serve_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            serve_dir = root / 'fileserver' / ('b' * 32)
            serve_dir.mkdir(parents=True)
            target = serve_dir / 'video.mp4'
            target.write_bytes(b'x')
            entry = fileserver.FileEntry(
                path=target, filename=target.name, file_size=1, expires_at=0,
            )
            target.unlink()  # файл уже ушёл в Telegram
            with mock.patch.object(config, 'DOWNLOAD_DIR', root):
                fileserver._delete_entry_file(entry)
            self.assertFalse(serve_dir.exists(), 'пустой каталог остался на диске')


if __name__ == '__main__':
    unittest.main()
