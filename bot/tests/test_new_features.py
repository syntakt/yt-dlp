"""Тесты возможностей yt-dlp, подключённых к боту (A–F).

Покрывают то, что легко сломать незаметно: состав опций yt-dlp, порядок
постпроцессоров, взаимодействие новых функций с SSRF-guard и разбор
пользовательского ввода для отрывков.
"""

import os
from pathlib import Path
import socket
import sys
import tempfile
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

from test_functional_regressions import _FakeYDL  # noqa: E402


def _pp_keys(opts: dict) -> list[str]:
    return [pp['key'] for pp in opts.get('postprocessors', [])]


async def _run_download(**kwargs) -> dict:
    """Запускает download_video с подменённым YoutubeDL и отдаёт собранные opts."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        _FakeYDL.target = out / 'video.mp4'
        _FakeYDL.info = {'title': 'video'}
        with mock.patch.object(downloader.yt_dlp, 'YoutubeDL', _FakeYDL):
            await downloader.download_video(
                url='https://example.com/v', format_id='best', output_dir=out, **kwargs,
            )
    return _FakeYDL.captured_opts


class JsRuntimeTests(unittest.TestCase):
    """A. Без JS-рантайма YouTube теряет клиент `web` и часть форматов."""

    def test_configured_runtime_reaches_ytdlp_opts(self):
        with mock.patch.object(downloader, 'JS_RUNTIMES', ['deno']):
            self.assertEqual(downloader._base_opts()['js_runtimes'], {'deno': {'path': None}})
        with mock.patch.object(downloader, 'JS_RUNTIMES', ['node', 'quickjs']):
            self.assertEqual(
                downloader._base_opts()['js_runtimes'],
                {'node': {'path': None}, 'quickjs': {'path': None}},
            )

    def test_empty_runtime_list_leaves_ytdlp_default(self):
        with mock.patch.object(downloader, 'JS_RUNTIMES', []):
            self.assertNotIn('js_runtimes', downloader._base_opts())


class ImpersonateTests(unittest.TestCase):
    """C. curl_cffi обходит SSRF-guard, поэтому требует явного согласия."""

    def test_missing_curl_cffi_degrades_instead_of_crashing(self):
        with (
            mock.patch.object(downloader, 'IMPERSONATE', 'chrome'),
            mock.patch('yt_dlp.dependencies.curl_cffi', None),
        ):
            self.assertIsNone(downloader._impersonate_target())
            self.assertNotIn('impersonate', downloader._base_opts())

    def test_target_is_parsed_when_curl_cffi_present(self):
        from yt_dlp.networking.impersonate import ImpersonateTarget
        with (
            mock.patch.object(downloader, 'IMPERSONATE', 'chrome'),
            mock.patch('yt_dlp.dependencies.curl_cffi', object()),
        ):
            self.assertEqual(downloader._impersonate_target(), ImpersonateTarget('chrome'))

    def test_invalid_target_is_ignored(self):
        with (
            mock.patch.object(downloader, 'IMPERSONATE', 'не-браузер:::'),
            mock.patch('yt_dlp.dependencies.curl_cffi', object()),
        ):
            self.assertIsNone(downloader._impersonate_target())


class PoTokenTests(unittest.TestCase):
    """D. Провайдер живёт на приватном IP — guard обязан его пропускать."""

    def tearDown(self):
        downloader._NETWORK_GUARD.enabled = False

    def test_provider_url_becomes_extractor_args(self):
        with (
            mock.patch.object(downloader, 'POT_PROVIDER_URL', 'http://bgutil-pot:4416'),
            mock.patch.object(downloader, 'YOUTUBE_PO_TOKEN', 'web.gvs+AAA,web.player+BBB'),
            mock.patch.object(downloader, 'YOUTUBE_PLAYER_CLIENT', 'default,-web'),
        ):
            args = downloader._base_opts()['extractor_args']['youtube']
        self.assertEqual(args['getpot_bgutil_baseurl'], ['http://bgutil-pot:4416'])
        self.assertEqual(args['po_token'], ['web.gvs+AAA', 'web.player+BBB'])
        self.assertEqual(args['player_client'], ['default', '-web'])

    def test_no_extractor_args_without_config(self):
        with (
            mock.patch.object(downloader, 'POT_PROVIDER_URL', ''),
            mock.patch.object(downloader, 'YOUTUBE_PO_TOKEN', ''),
            mock.patch.object(downloader, 'YOUTUBE_PLAYER_CLIENT', ''),
        ):
            self.assertNotIn('extractor_args', downloader._base_opts())

    def test_provider_host_is_allowed_but_other_private_hosts_are_not(self):
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.10.2.6', 4416))]
        downloader._NETWORK_GUARD.enabled = True
        with (
            mock.patch.object(downloader, '_SSRF_ALLOWED_HOSTS', frozenset({'bgutil-pot'})),
            mock.patch.object(downloader, '_ORIGINAL_GETADDRINFO', return_value=private),
        ):
            # хост провайдера пропускаем
            self.assertEqual(downloader._guarded_getaddrinfo('bgutil-pot', 4416), private)
            # всё остальное по-прежнему блокируется
            with self.assertRaises(socket.gaierror):
                downloader._guarded_getaddrinfo('internal.example', 80)


class PostProcessorTests(unittest.IsolatedAsyncioTestCase):
    """E1. Обложка/теги/главы и режимы SponsorBlock."""

    async def test_embeds_are_applied_in_ytdlp_order(self):
        with (
            mock.patch.object(downloader, 'EMBED_THUMBNAIL', True),
            mock.patch.object(downloader, 'EMBED_METADATA', True),
            mock.patch.object(downloader, 'EMBED_CHAPTERS', True),
            mock.patch.object(downloader, 'SPONSORBLOCK_MODE', 'off'),
        ):
            opts = await _run_download()
        self.assertTrue(opts['writethumbnail'])
        # Metadata обязан идти перед EmbedThumbnail (порядок как в yt_dlp/__init__.py)
        self.assertEqual(_pp_keys(opts), ['FFmpegMetadata', 'EmbedThumbnail'])

    async def test_embeds_can_be_disabled(self):
        with (
            mock.patch.object(downloader, 'EMBED_THUMBNAIL', False),
            mock.patch.object(downloader, 'EMBED_METADATA', False),
            mock.patch.object(downloader, 'EMBED_CHAPTERS', False),
            mock.patch.object(downloader, 'SPONSORBLOCK_MODE', 'off'),
        ):
            opts = await _run_download()
        self.assertEqual(_pp_keys(opts), [])
        self.assertNotIn('writethumbnail', opts)

    async def test_sponsorblock_remove_adds_modify_chapters_before_metadata(self):
        with (
            mock.patch.object(downloader, 'SPONSORBLOCK_MODE', 'remove'),
            mock.patch.object(downloader, 'EMBED_METADATA', True),
            mock.patch.object(downloader, 'EMBED_CHAPTERS', True),
            mock.patch.object(downloader, 'EMBED_THUMBNAIL', False),
        ):
            opts = await _run_download()
        keys = _pp_keys(opts)
        self.assertEqual(keys[0], 'SponsorBlock')
        self.assertLess(keys.index('ModifyChapters'), keys.index('FFmpegMetadata'))
        modify = opts['postprocessors'][keys.index('ModifyChapters')]
        self.assertTrue(modify['remove_sponsor_segments'])

    async def test_sponsorblock_mark_keeps_segments(self):
        with (
            mock.patch.object(downloader, 'SPONSORBLOCK_MODE', 'mark'),
            mock.patch.object(downloader, 'EMBED_METADATA', True),
            mock.patch.object(downloader, 'EMBED_CHAPTERS', True),
            mock.patch.object(downloader, 'EMBED_THUMBNAIL', False),
        ):
            opts = await _run_download()
        keys = _pp_keys(opts)
        modify = opts['postprocessors'][keys.index('ModifyChapters')]
        self.assertEqual(modify['remove_sponsor_segments'], [])


class PlaylistEmbedTests(unittest.IsolatedAsyncioTestCase):
    """Плейлист должен получать обложки/теги так же, как одиночная загрузка."""

    async def test_playlist_gets_same_embed_postprocessors(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _FakeYDL.target = out / '1-track.m4a'
            _FakeYDL.info = {'title': 'track'}
            with (
                mock.patch.object(downloader, 'EMBED_THUMBNAIL', True),
                mock.patch.object(downloader, 'EMBED_METADATA', True),
                mock.patch.object(downloader.yt_dlp, 'YoutubeDL', _FakeYDL),
                mock.patch.object(_FakeYDL, 'download', create=True, return_value=None),
            ):
                await downloader.download_playlist(
                    url='https://example.com/list', format_id='bestaudio',
                    output_dir=out, max_items=1,
                )
        opts = _FakeYDL.captured_opts
        self.assertTrue(opts['writethumbnail'])
        self.assertEqual(_pp_keys(opts), ['FFmpegMetadata', 'EmbedThumbnail'])


class ClipTests(unittest.IsolatedAsyncioTestCase):
    """E2. Разбор интервала и передача его в yt-dlp."""

    def test_parse_time_range(self):
        self.assertEqual(bot._parse_time_range('10:00-12:30'), (600, 750))
        self.assertEqual(bot._parse_time_range('90-150'), (90, 150))
        self.assertEqual(bot._parse_time_range(' 1:02:00 — 1:05:00 '), (3720, 3900))
        # конец не позже начала / мусор / за пределами длительности
        self.assertIsNone(bot._parse_time_range('12:30-10:00'))
        self.assertIsNone(bot._parse_time_range('десять-двадцать'))
        self.assertIsNone(bot._parse_time_range('10:00'))
        self.assertIsNone(bot._parse_time_range('10:00-12:00', duration=300))

    def test_clip_longer_than_limit_is_rejected(self):
        with mock.patch.object(config, 'MAX_CLIP_SECONDS', 60):
            self.assertIsNone(bot._parse_time_range('0-120'))
            self.assertEqual(bot._parse_time_range('0-60'), (0, 60))

    async def test_clip_range_reaches_ytdlp(self):
        opts = await _run_download(clip_range=(600, 750))
        self.assertTrue(opts['force_keyframes_at_cuts'])
        self.assertEqual(opts['download_ranges'].ranges, [(600, 750)])

    def test_clock_formatting(self):
        self.assertEqual(bot._fmt_clock(600), '10:00')
        self.assertEqual(bot._fmt_clock(3723), '1:02:03')


class SplitChaptersTests(unittest.IsolatedAsyncioTestCase):
    """E2. Разбиение по главам и сбор получившихся файлов."""

    async def test_split_chapters_sets_pp_and_chapter_template(self):
        opts = await _run_download(split_chapters=True)
        self.assertIn('FFmpegSplitChapters', _pp_keys(opts))
        self.assertIn('chapter', opts['outtmpl'])

    def test_only_chapter_files_are_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / 'video.mp4').write_bytes(b'x')          # исходный целый файл
            (out / '001-intro.mp4').write_bytes(b'x')
            (out / '002-main.mp4').write_bytes(b'x')
            (out / 'video.jpg').write_bytes(b'x')          # обложка
            names = [p.name for p in downloader._collect_chapter_files(out)]
        self.assertEqual(names, ['001-intro.mp4', '002-main.mp4'])


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_from_start_only_for_live_videos(self):
        with mock.patch.object(downloader, 'LIVE_FROM_START', True):
            live = await _run_download(is_live=True)
            vod = await _run_download(is_live=False)
        self.assertTrue(live['live_from_start'])
        self.assertNotIn('live_from_start', vod)


class MenuTests(unittest.TestCase):
    """Кнопки появляются только когда функция включена и применима."""

    def _menu_callbacks(self, info):
        _caption, keyboard = bot._build_quality_menu(info)
        return [b.callback_data for row in keyboard.inline_keyboard for b in row]

    def test_clip_and_chapter_buttons_visibility(self):
        info = downloader.VideoInfo(
            url='https://example.com/v', title='t', uploader='u', duration=1200,
            view_count=0, like_count=None, thumbnail='', description='',
            chapter_count=5,
        )
        with (
            mock.patch.object(config, 'ALLOW_CLIPS', True),
            mock.patch.object(config, 'ALLOW_SPLIT_CHAPTERS', True),
        ):
            data = self._menu_callbacks(info)
        self.assertIn('dl:clip:best', data)
        self.assertIn('dl:sc:best', data)

        with (
            mock.patch.object(config, 'ALLOW_CLIPS', False),
            mock.patch.object(config, 'ALLOW_SPLIT_CHAPTERS', False),
        ):
            data = self._menu_callbacks(info)
        self.assertNotIn('dl:clip:best', data)
        self.assertNotIn('dl:sc:best', data)

    def test_chapter_button_hidden_without_chapters(self):
        info = downloader.VideoInfo(
            url='https://example.com/v', title='t', uploader='u', duration=1200,
            view_count=0, like_count=None, thumbnail='', description='',
            chapter_count=0,
        )
        with mock.patch.object(config, 'ALLOW_SPLIT_CHAPTERS', True):
            self.assertNotIn('dl:sc:best', self._menu_callbacks(info))


class BuildFreshnessTests(unittest.TestCase):
    """F. Возраст сборки виден администратору."""

    def test_build_age_reports_days(self):
        with mock.patch.dict(os.environ, {'BUILD_DATE': '2020-01-01T00:00:00Z'}):
            age = bot._get_build_age()
        self.assertIn('2020-01-01', age)
        self.assertIn('дн назад', age)

    def test_unknown_build_date_is_handled(self):
        with (
            mock.patch.dict(os.environ, {'BUILD_DATE': ''}),
            mock.patch.object(bot.Path, 'read_text', side_effect=OSError),
        ):
            self.assertEqual(bot._get_build_age(), 'неизвестно')


if __name__ == '__main__':
    unittest.main()
