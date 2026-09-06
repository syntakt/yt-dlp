"""Security boundaries and failure cases found during the September 2026 audit."""

import asyncio
import ipaddress
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram_bot"))
os.environ.setdefault("DOWNLOAD_DIR", tempfile.gettempdir())
os.environ.setdefault("DB_PATH", str(Path(tempfile.gettempdir()) / "yt-dlp-bot-test.db"))

import bot  # noqa: E402
import config  # noqa: E402
import database  # noqa: E402
import downloader  # noqa: E402
import fileserver  # noqa: E402
from test_functional_regressions import _FakeYDL  # noqa: E402
from test_security_regressions import _bencode, _video_info  # noqa: E402


class DatabaseMigrationTests(unittest.TestCase):
    def tearDown(self):
        database.close_connection()

    def test_old_sessions_schema_migrates_before_index_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.db"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE sessions (chat_id INTEGER, message_id INTEGER, "
                             "url TEXT, video_info_json TEXT, created_at TEXT, "
                             "PRIMARY KEY (chat_id, message_id))")
            with mock.patch.object(database, "DB_PATH", path):
                database.init_db()
                database.save_session(1, 2, "https://example.com", "{}", user_id=3)
                self.assertIsNotNone(database.get_session(1, 2, 3))
                self.assertIsNone(database.get_session(1, 2, 4))

    def test_cached_approval_is_revoked(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(database, "DB_PATH", Path(tmp) / "bot.db"):
                database.init_db()
                database.upsert_user(1, "", "")
                database.approve_user(1, 0)
                self.assertTrue(database.is_authorized(1))
                database.ban_user(1)
                self.assertFalse(database.is_authorized(1))


class NetworkBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_defaults_reject_clip_and_live_before_starting_worker(self):
        with (
            mock.patch.object(downloader, "SSRF_PROTECTION", True),
            mock.patch.object(downloader, "TRUST_EXTERNAL_NETWORK_FOR_SSRF", False),
            mock.patch.object(downloader, "_run_download_worker") as worker,
        ):
            for kwargs in ({"clip_range": (0, 10)}, {"is_live": True}):
                result = await downloader.download_video("https://example.com", "best", Path("unused"), **kwargs)
                self.assertFalse(result.success)
            worker.assert_not_called()

    def test_all_ffmpeg_postprocessors_and_direct_probes_use_guarded_executables(self):
        import yt_dlp.postprocessor.ffmpeg as ff
        from yt_dlp.postprocessor.embedthumbnail import EmbedThumbnailPP

        wrappers = Path(downloader.__file__).parent / "ffmpeg_guard"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Capture arguments AFTER executing the real wrapper, without a
            # dependency on locally installed ffmpeg/ffprobe or media codecs.
            for program in ("ffmpeg", "ffprobe"):
                binary = root / program
                binary.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
                binary.chmod(0o755)
            env = {**os.environ, "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}"}
            source = root / "input.mp4"
            source.write_bytes(b"media")

            def run(cmd, **kwargs):
                self.assertEqual(Path(cmd[0]).parent, wrappers)
                actual = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
                args = json.loads(actual.stdout)
                if Path(cmd[0]).name == "ffprobe":
                    self.assertEqual(args[:2], ["-protocol_whitelist", "file,pipe,crypto,data"])
                else:
                    inputs = [i for i, arg in enumerate(args) if arg == "-i"]
                    self.assertEqual(len(inputs), 2)
                    for i in inputs:
                        self.assertEqual(args[i - 2:i], ["-protocol_whitelist", "file,pipe,crypto,data"])
                return ('{"streams": [], "format": {}}', "", 0)

            with (
                mock.patch.object(downloader, "SSRF_PROTECTION", True),
                mock.patch.object(downloader, "TRUST_EXTERNAL_NETWORK_FOR_SSRF", False),
                mock.patch.object(ff.FFmpegPostProcessor, "_get_ffmpeg_version", return_value=("7.1", {})),
                mock.patch.object(ff.Popen, "run", side_effect=run),
                downloader.SafeYoutubeDL(downloader._base_opts()) as ydl,
            ):
                classes = {c for c in vars(ff).values() if isinstance(c, type)
                           and issubclass(c, ff.FFmpegPostProcessor)} | {EmbedThumbnailPP}
                for cls in classes:
                    with self.subTest(postprocessor=cls.__name__):
                        # Constructors such as ConcatPP need extra config;
                        # executable selection is inherited from this base.
                        pp = cls.__new__(cls)
                        ff.FFmpegPostProcessor.__init__(pp, ydl)
                        pp.real_run_ffmpeg([(str(source), []), (str(source), [])], [])
                        pp.get_metadata_object(str(source))
                        pp.get_audio_codec(str(source))

    async def test_child_thread_request_cannot_reach_private_dns(self):
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        def request(_ydl, _req):
            return socket.getaddrinfo("rebound.example", 80)
        with (
            mock.patch.object(downloader, "SSRF_PROTECTION", True),
            mock.patch.object(downloader, "_ORIGINAL_GETADDRINFO", return_value=private),
            mock.patch.object(downloader.yt_dlp.YoutubeDL, "urlopen", request),
        ):
            with downloader.SafeYoutubeDL({"quiet": True}) as ydl:
                with self.assertRaises(socket.gaierror):
                    await asyncio.to_thread(ydl.urlopen, "https://rebound.example")

    async def test_implicit_curl_handler_is_disabled(self):
        with (
            mock.patch.object(downloader, "SSRF_PROTECTION", True),
            mock.patch.object(downloader, "TRUST_IMPERSONATE_FOR_SSRF", False),
        ):
            with downloader.SafeYoutubeDL(downloader._base_opts()) as ydl:
                self.assertLessEqual(set(ydl._request_director.handlers), {"Urllib", "Requests"})
                self.assertEqual(ydl.proxies, {"all": "__noproxy__"})

    async def test_thumbnail_local_file_is_never_opened(self):
        for url in ("/data/bot.db", "file:///data/bot.db", "ftp://example.com/pic"):
            with self.subTest(url=url), mock.patch.object(downloader, "run_blocking") as run:
                with self.assertRaises(ValueError):
                    await downloader.get_thumbnail(url)
                run.assert_not_called()

    async def test_progress_does_not_enable_unguarded_fragment_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            _FakeYDL.target = Path(tmp) / "test.mp4"
            _FakeYDL.info = {"title": "test"}
            with (
                mock.patch.object(downloader, "SafeYoutubeDL", _FakeYDL),
                mock.patch.object(downloader, "SSRF_PROTECTION", True),
                mock.patch.object(downloader, "USE_ARIA2C", True),
            ):
                await downloader.download_video("https://example.com", "best", Path(tmp),
                                                progress_callback=mock.AsyncMock())
            self.assertEqual(_FakeYDL.captured_opts["concurrent_fragment_downloads"], 1)

    def test_multicast_and_nonpublic_addresses_are_blocked(self):
        for address in ("224.0.0.1", "ff02::1", "127.0.0.1", "100.64.0.1", "::ffff:127.0.0.1"):
            self.assertFalse(downloader._is_public_address(ipaddress.ip_address(address)))

    def test_ffmpeg_fallback_gets_input_protocol_restrictions(self):
        from yt_dlp.downloader.external import FFmpegFD
        with (
            mock.patch.object(downloader, "SSRF_PROTECTION", True),
            mock.patch.object(downloader, "TRUST_EXTERNAL_NETWORK_FOR_SSRF", False),
        ):
            opts = downloader._base_opts()
            with downloader.SafeYoutubeDL(opts) as ydl:
                fd = FFmpegFD(ydl, opts)
                args = fd._configuration_args(("_i1", "_i"))
                self.assertEqual(args, ["-protocol_whitelist", "file,pipe,crypto,data"])


class WorkerLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_submission_failure_releases_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "download"
            out.mkdir()
            with mock.patch.object(downloader._EXECUTOR, "submit", side_effect=RuntimeError("shutdown")):
                with self.assertRaisesRegex(RuntimeError, "shutdown"):
                    await downloader._run_download_worker(lambda: None, out, [False], 1)
            self.assertFalse(downloader.download_work_in_progress(out))
            self.assertFalse(out.exists())

    async def test_cancel_before_executor_slot_is_available_releases_directory(self):
        loop = asyncio.get_running_loop()
        slots = asyncio.Semaphore(0)
        downloader._EXECUTOR_SLOTS[loop] = slots
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "download"
            out.mkdir()
            parent = asyncio.create_task(downloader._run_download_worker(lambda: None, out, [False], 60))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            parent.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await parent
            self.assertTrue(downloader.download_work_in_progress(out))
            tasks = list(downloader._BACKGROUND_WORKERS)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.assertFalse(downloader.download_work_in_progress(out))
            self.assertFalse(out.exists())

    async def test_timeout_keeps_directory_protected_until_worker_exits(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "download"
            out.mkdir()
            def worker():
                started.set()
                release.wait(5)
                (out / "late.mp4").write_bytes(b"late")
                finished.set()
            flag = [False]
            try:
                with self.assertRaises(asyncio.TimeoutError):
                    await downloader._run_download_worker(worker, out, flag, 0.1)
                self.assertTrue(started.is_set())
                self.assertTrue(flag[0])
                self.assertTrue(bot._is_dir_in_use(out))
                self.assertTrue(out.exists())
            finally:
                release.set()
                async with asyncio.timeout(5):
                    while downloader.download_work_in_progress(out):
                        await asyncio.sleep(0.01)
            self.assertTrue(finished.is_set())
            self.assertFalse(out.exists())


class TorrentValidationTests(unittest.TestCase):
    def test_malformed_tracker_and_file_types_are_value_errors(self):
        for extra in ({b"announce": 1}, {b"announce-list": 1},
                      {b"announce-list": [b"http://example.com"]},
                      {b"announce-list": [[1]]}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.parse({b"info": {b"name": b"a.mp4", b"length": 1}, **extra})
        for info in (
            {b"name": b"dir", b"length": 1, b"files": 1},
            {b"name": b"dir", b"files": []},
            {b"name": b"a.mp4", b"name.utf-8": b"b.mp4", b"length": 1},
            {b"name": b"dir", b"files": [{b"length": 1, b"path": [b"a.mp4"], b"path.utf-8": [b"b.mp4"]}]},
        ):
            with self.subTest(info=info), self.assertRaises(ValueError):
                self.parse({b"info": info})

    def parse(self, meta):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "in.torrent"
            path.write_bytes(_bencode(meta))
            return downloader.parse_torrent_file(path)

    def test_unhashable_duplicate_keys_and_trailing_data_are_rejected(self):
        for data in (b"dlei1ee", b"d1:ai1e1:ai2ee", b"i1ejunk", b"i01e"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                downloader._bdecode(data)

    def test_negative_lengths_and_path_traversal_are_rejected(self):
        for info in (
            {b"name": b"a.mp4", b"length": -1},
            {b"name": b"../a.mp4", b"length": 1},
            {b"name": b"dir", b"files": [{b"length": 1, b"path": [b"..", b"a.mp4"]}]},
        ):
            with self.subTest(info=info), self.assertRaises(ValueError):
                self.parse({b"info": info})

    def test_web_seeds_are_checked_with_trackers(self):
        meta = self.parse({b"info": {b"name": b"a.mp4", b"length": 1},
                           b"url-list": b"http://127.0.0.1/secret"})
        with mock.patch.object(downloader, "SSRF_PROTECTION", True):
            self.assertFalse(downloader._torrent_trackers_are_safe(meta.trackers))
            self.assertFalse(downloader._torrent_trackers_are_safe(["file:///secret"]))
            self.assertFalse(downloader._torrent_trackers_are_safe(["http://[invalid"]))

    def test_metadata_size_is_checked_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "in.torrent"
            path.write_bytes(b"x" * 17)
            with mock.patch.object(downloader, "TORRENT_FILE_MAX_BYTES", 16):
                with self.assertRaises(ValueError):
                    downloader.parse_torrent_file(path)


class PlaylistCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_successful_post_hook_files_are_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            complete = root / "1-complete.mp4"
            partial = root / "2-partial.mp4"

            class PlaylistYDL(_FakeYDL):
                def download(self, urls):
                    complete.write_bytes(b"finished")
                    partial.write_bytes(b"unfinished component")
                    for hook in self.captured_opts["post_hooks"]:
                        hook(str(complete))

            with mock.patch.object(downloader, "SafeYoutubeDL", PlaylistYDL):
                results = await downloader.download_playlist("https://example.com", "best", root)
            self.assertEqual([r.file_path for r in results], [complete])


class FilePublicationTests(unittest.TestCase):
    def tearDown(self):
        fileserver._registry.clear()

    def test_old_remote_mtime_does_not_expire_published_link_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "video.mp4"
            source.write_bytes(b"video")
            os.utime(source, (1, 1))
            with mock.patch.object(config, "DOWNLOAD_DIR", root):
                token = fileserver.move_and_register(source, config.FILE_TTL_SECONDS)
                fileserver._registry.clear()
                fileserver._restore_registry()
                entry = fileserver.get_entry(token)
                self.assertIsNotNone(entry)
                self.assertGreater(entry.expires_at, time.time())


class CallbackPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_link_publication_can_be_retried(self):
        entry = SimpleNamespace(expires_at=time.time() + 300)
        pending = {"token": "test", "dl_id": 1, "info": _video_info()}
        ctx = SimpleNamespace(user_data={"_deliveries": {1: pending}}, bot=mock.AsyncMock())
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=1), message=SimpleNamespace(chat_id=1, message_id=2),
            edit_message_text=mock.AsyncMock(side_effect=bot.TelegramError("offline")),
        )
        ctx.bot.send_message.side_effect = bot.TelegramError("offline")
        with (
            mock.patch.object(bot.db, "is_super_admin", return_value=True),
            mock.patch.object(fileserver, "get_entry", return_value=entry),
            mock.patch.object(config, "PUBLIC_BASE_URL", "https://example.com"),
        ):
            with self.assertRaises(bot.TelegramError):
                await bot._handle_deliver_callback(query, ctx, "deliver:1:link")
        self.assertIs(ctx.user_data["_deliveries"][1], pending)

    async def test_cancelled_telegram_delivery_removes_unregistered_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "video.mp4"
            source.write_bytes(b"media")
            with mock.patch.object(config, "DOWNLOAD_DIR", root):
                token = fileserver.move_and_register(source)
                entry = fileserver.get_entry(token)
                ctx = SimpleNamespace(user_data={"_deliveries": {
                    1: {"token": token, "dl_id": 1, "info": _video_info()},
                }}, bot=mock.AsyncMock())
                query = SimpleNamespace(
                    from_user=SimpleNamespace(id=1), message=SimpleNamespace(chat_id=1, message_id=2),
                    edit_message_text=mock.AsyncMock(side_effect=asyncio.CancelledError),
                )
                with (
                    mock.patch.object(bot.db, "is_super_admin", return_value=True),
                    mock.patch.object(bot.db, "update_download"),
                ):
                    with self.assertRaises(asyncio.CancelledError):
                        await bot._handle_deliver_callback(query, ctx, "deliver:1:tg")
                self.assertIsNone(fileserver.get_entry(token))
                self.assertFalse(entry.path.exists())
                self.assertFalse(entry.path.parent.exists())

    async def test_full_metadata_queue_updates_waiting_message_without_spawning_task(self):
        ctx = SimpleNamespace(bot_data={"_pending_tasks": {1: 16}}, application=mock.Mock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
        msg = SimpleNamespace(edit_text=mock.AsyncMock())
        work = mock.AsyncMock()
        coro = work()
        await bot._spawn_update_task(update, ctx, coro, "metadata_1_2", status_message=msg)
        self.assertIsNone(coro.cr_frame)
        work.assert_not_awaited()
        ctx.application.create_task.assert_not_called()
        msg.edit_text.assert_awaited_once()
        self.assertEqual(ctx.bot_data["_pending_tasks"], {1: 16})

    def test_clip_menu_hidden_without_external_network_trust(self):
        info = _video_info()
        info.duration = 120
        with (
            mock.patch.object(config, "ALLOW_CLIPS", True),
            mock.patch.object(config, "SSRF_PROTECTION", True),
            mock.patch.object(config, "TRUST_EXTERNAL_NETWORK_FOR_SSRF", False),
        ):
            _, menu = bot._build_quality_menu(info)
        self.assertNotIn("dl:clip:best", [b.callback_data for row in menu.inline_keyboard for b in row])

    async def test_disabled_features_rejected_before_session_or_download(self):
        query = SimpleNamespace(answer=mock.AsyncMock(), from_user=SimpleNamespace(id=1))
        ctx = SimpleNamespace(user_data={}, bot_data={})
        for kind, setting in (("a", "ALLOW_AUDIO"), ("ao", "ALLOW_OPUS"),
                              ("aw", "ALLOW_WAV"), ("sc", "ALLOW_SPLIT_CHAPTERS"),
                              ("s", "ALLOW_SUBTITLES"), ("clip", "ALLOW_CLIPS")):
            with mock.patch.object(config, setting, False), mock.patch.object(bot, "_restore_session") as restore:
                await bot._handle_download_callback(query, ctx, f"dl:{kind}:best")
                restore.assert_not_called()
        with mock.patch.object(config, "ALLOW_PLAYLISTS", False):
            await bot._handle_playlist_callback(query, ctx, "pl:10:best")
        with mock.patch.object(config, "ALLOW_TORRENTS", False):
            await bot._handle_torrent_callback(query, ctx, "token")

    async def test_upload_stream_does_not_read_entire_file(self):
        telegram = mock.AsyncMock()
        class ReadGuard:
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def read(self, *args):
                raise AssertionError("InputFile eagerly read the file")
        result = downloader.DownloadResult(True, Path("video.mp4"), file_size=100)
        with mock.patch.object(config, "LOCAL_API_SERVER", ""), mock.patch.object(Path, "open", return_value=ReadGuard()):
            await bot._deliver_file(1, result, telegram, keep_file=True)
        telegram.send_document.assert_awaited_once()

    async def test_large_link_batches_are_split_without_losing_entries(self):
        ctx = SimpleNamespace(bot=mock.AsyncMock(), chat_data={})
        lines = [f'{i}. <a href="https://example.com/{i}">file</a>' for i in range(500)]
        with mock.patch.object(bot, "_schedule_link_expiry_cleanup"):
            await bot._send_link_lines(ctx, 1, "Links\n", lines)
        messages = [c.args[1] for c in ctx.bot.send_message.await_args_list]
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(m) <= bot._TG_SAFE_BUDGET for m in messages))
        joined = "\n".join(messages)
        for line in lines:
            self.assertIn(line, joined)

    def test_session_metadata_does_not_store_thumbnail_or_page_credentials(self):
        info = _video_info()
        info.thumbnail = "https://example.com/image?token=private"
        info.webpage_url = "https://user:password@example.com/video"
        with mock.patch.object(database, "save_session") as save:
            bot._save_session_safe(1, 2, info.url, info, user_id=3)
        saved = json.loads(save.call_args.args[3])
        self.assertEqual(saved["thumbnail"], "")
        self.assertEqual(saved["webpage_url"], "")
        self.assertEqual(info.thumbnail, "https://example.com/image?token=private")

    def test_nonfinite_float_config_uses_default(self):
        for raw in ("nan", "inf", "-inf"):
            with mock.patch.dict(os.environ, {"AUDIT_FLOAT": raw}):
                self.assertEqual(config._parse_float("AUDIT_FLOAT", 1.0), 1.0)


class TorrentProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_aria2_process_does_not_publish_partial_media(self):
        proc = SimpleNamespace(returncode=1, wait=mock.AsyncMock(return_value=1),
                               stdout=SimpleNamespace(readline=mock.AsyncMock(return_value=b"")))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "partial.mp4").write_bytes(b"incomplete")
            meta = downloader.TorrentMeta("test", 10, source="test.torrent")
            with mock.patch.object(asyncio, "create_subprocess_exec", return_value=proc):
                with self.assertRaisesRegex(RuntimeError, "exit 1"):
                    await downloader.download_torrent(meta, root)

    async def test_cancelling_magnet_metadata_terminates_process(self):
        started = asyncio.Event()
        async def wait():
            started.set()
            await asyncio.Future()
        proc = SimpleNamespace(returncode=None, wait=wait)
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(asyncio, "create_subprocess_exec", return_value=proc),
                mock.patch.object(downloader, "_terminate_proc", new_callable=mock.AsyncMock) as terminate,
            ):
                task = asyncio.create_task(downloader.fetch_magnet_metadata(
                    "magnet:?xt=urn:btih:" + "a" * 40, Path(tmp)))
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                terminate.assert_awaited_once_with(proc)


class HttpDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_and_head_preserve_token_and_get_consumes_it(self):
        from aiohttp import ClientSession
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "file.bin"
            source.write_bytes(b"payload")
            with mock.patch.object(config, "DOWNLOAD_DIR", root):
                fileserver._rate_counters.clear()
                token = fileserver.move_and_register(source)
                runner = await fileserver.start(host="127.0.0.1", port=0)
                base = f"http://127.0.0.1:{runner.addresses[0][1]}"
                try:
                    async with ClientSession() as client:
                        async with client.get(f"{base}/info/{token}") as response:
                            self.assertEqual(response.status, 200)
                            self.assertIn("no-store", response.headers["Cache-Control"])
                        async with client.head(f"{base}/dl/{token}") as response:
                            self.assertEqual(response.status, 405)
                        self.assertIsNotNone(fileserver.get_entry(token))
                        async with client.get(f"{base}/dl/{token}") as response:
                            self.assertEqual(response.status, 200)
                            self.assertEqual(await response.read(), b"payload")
                        async with client.get(f"{base}/dl/{token}") as response:
                            self.assertEqual(response.status, 410)
                finally:
                    await fileserver.stop()
                    fileserver._registry.clear()


class LogRedactionTests(unittest.TestCase):
    def test_ytdlp_errors_do_not_print_signed_urls(self):
        message = "failed HTTPS://example.com/secret?token=value after redirect"
        self.assertEqual(downloader._YDLBotLogger._redact(message), "failed <URL> after redirect")
        self.assertEqual(bot._safe_error_text(message), "failed <URL> after redirect")
