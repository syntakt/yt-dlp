"""Behavioral coverage for user options, durable retries and explicit subscriptions."""

import asyncio
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from telegram.error import BadRequest, NetworkError

import bot
import config
import database as db
import downloader
import feature_store as store
import features
import fileserver
import media_options as media
import runtime
from safe_urls import canonical_url
from test_functional_regressions import _FakeYDL


URL = 'https://www.youtube.com/watch?v=abcdefghijk'


def info(**kwargs):
    return downloader.VideoInfo(url=URL, title='Видео', uploader='Автор', duration=100,
                                view_count=0, like_count=None, thumbnail='', description='', **kwargs)


def fmt(fid, height=720, size=1000, lang='', audio=False, acodec='none'):
    return downloader.FormatInfo(fid, 'm4a' if audio else 'mp4', '', 'audio only' if audio else f'{height}p',
                                 None, 'none' if audio else 'avc1', 'mp4a' if audio else acodec,
                                 size, None, language=lang)


def context():
    user = SimpleNamespace(id=1)
    message = mock.Mock(chat_id=1, message_id=10, photo=())
    message.edit_text = mock.AsyncMock(return_value=message)
    message.reply_text = mock.AsyncMock(return_value=message)
    message.delete = mock.AsyncMock()
    query = bot._MessageQuery(message, user)
    ctx = SimpleNamespace(user_data={}, bot_data={'_download_sem': asyncio.Semaphore(1),
                                                '_metadata_sem': asyncio.Semaphore(1)}, bot=mock.AsyncMock())
    update = SimpleNamespace(effective_user=user, effective_chat=SimpleNamespace(id=1),
                             effective_message=message, message=message, callback_query=query)
    db.upsert_user(1, '', '')
    db.approve_user(1, 0)
    return query, ctx, update


@pytest.mark.parametrize('text', ['0', '-1', '2-1', '1:4', '1,201', '1,999999999999', '1;--exec=x', '1,,2', '1-200'])
def test_playlist_selection_rejects_invalid_or_excessive_input(text):
    with pytest.raises(ValueError):
        media.playlist_indices(text, 10)


def test_playlist_selection_expands_deduplicates_and_bounds():
    assert media.playlist_indices(' 3,1,3,7-9 ', 5) == '1,3,7,8,9'
    with pytest.raises(ValueError):
        media.playlist_indices('9', 10, 8)


def test_auto_size_includes_audio_and_mux_reserve():
    video = info(formats=[fmt('1080', 1080, 900), fmt('720', 720, 700), fmt('audio', size=200, audio=True)])
    assert media.auto_format(video, 1000).format_id == '720'
    assert media.auto_format(video, 500) is None
    video.formats[-1].filesize = None
    assert media.auto_format(video, 1000) is None


def test_auto_size_uses_requested_language_and_codec():
    video = info(formats=[fmt('v', size=700), fmt('ru', size=100, audio=True, lang='ru'),
                         fmt('en', size=500, audio=True, lang='en')])
    assert media.auto_format(video, 1000, 'ru', True).format_id == 'v'
    assert media.auto_format(video, 1000, 'en', True) is None
    assert media.auto_format(video, 1000, 'de', True) is None


def test_selector_cannot_inject_expression_or_fall_back_to_wrong_language():
    with pytest.raises(ValueError):
        media.selector(audio_language='ru]/best')
    with pytest.raises(ValueError):
        media.selector('1/best')
    result = media.selector('best', audio_language='ru', compatible=True, max_height=720)
    assert all('[language=ru]' in branch for branch in result.split('/'))
    assert '[vcodec^=avc1]' in result and '[acodec^=mp4a]' in result


def test_preferences_survive_restart_and_are_owner_scoped():
    store.preferences(1, {'quality': '720', 'audio_language': 'ru'})
    db.close_connection()
    db.init_db()
    assert store.preferences(1)['quality'] == '720'
    assert store.preferences(2)['quality'] == 'best'


def test_cache_is_owner_scoped_option_specific_and_disables_authenticated_sources():
    key = media.cache_key(URL, {'audio_language': 'ru'})
    assert key != media.cache_key(URL, {'audio_language': 'en'})
    assert media.cache_key(URL, {}, cookies=True) is None
    assert media.cache_key(URL, {}, live=True) is None
    assert media.cache_key('https://example.com/private-token', {}) is None
    message = SimpleNamespace(video=SimpleNamespace(file_id='synthetic-id', file_size=100), audio=None, document=None)
    store.remember_file(1, key, message)
    assert store.cached(1, key)['file_id'] == 'synthetic-id'
    assert store.cached(2, key) is None
    with mock.patch.object(store.time, 'time', return_value=time.time() + 8 * 86400):
        assert store.cached(1, key) is None


def test_queue_restarts_and_retry_payload_preserves_options():
    db.upsert_user(1, '', '')
    dl_id = db.add_download(1, URL)
    values = {'data': 'dl:v:best', 'options': {'audio_language': 'ru'}, 'clip_range': [10, 20]}
    store.save_job(dl_id, 1, URL, values)
    db.update_download(dl_id, status='queued')
    assert store.queue(1)[0]['position'] == 1
    assert store.job(dl_id, 2) is None
    db.close_connection()
    db.init_db()
    assert db.cleanup_stale_downloads() == 1
    saved = store.job(dl_id, 1)
    assert saved['status'] == 'error'
    assert json.loads(saved['payload']) == values


def test_subtitle_text_strips_cues_tags_and_adjacent_duplicates():
    vtt = 'WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\n<b>Hello &amp; world</b>\n\n2\n00:00:02.000 --> 00:00:03.000\nHello &amp; world\n\nNOTE hidden\nsecret note\n\n3\n00:00:03.000 --> 00:00:04.000\nNext\n'
    assert media.subtitle_text(vtt) == 'Hello & world\nNext\n'


@pytest.mark.parametrize('source', ['manual', 'auto'])
def test_subtitle_download_uses_selected_source_and_does_not_download_video(tmp_path, source):
    class YDL(_FakeYDL):
        def extract_info(self, *args, **kwargs):
            (tmp_path / 'test.ru.vtt').write_text('WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nТекст\n')
            return {}
    async def run():
        with mock.patch.object(downloader, 'SafeYoutubeDL', YDL):
            result = await downloader.download_video(URL, 'best', tmp_path, subtitle_lang='ru',
                                                     subtitle_source=source, subtitle_format='txt')
        assert result.success
        assert result.file_path.read_text() == 'Текст\n'
        opts = YDL.captured_opts
        assert opts['skip_download'] and opts['subtitleslangs'] == ['ru']
        assert opts['writesubtitles'] == (source == 'manual')
        assert opts['writeautomaticsub'] == (source == 'auto')
    asyncio.run(run())


def test_mp4_player_and_document_fallback_keep_file_until_success(tmp_path):
    async def run():
        path = tmp_path / 'video.mp4'
        path.write_bytes(b'test')
        result = downloader.DownloadResult(True, path, file_size=4, streamable=True)
        telegram = mock.AsyncMock()
        telegram.send_video.side_effect = BadRequest('Wrong video format')
        telegram.send_document.side_effect = BadRequest('Temporary upload failure')
        with mock.patch.object(config, 'LOCAL_API_SERVER', ''):
            with pytest.raises(BadRequest):
                await bot._deliver_file(1, result, telegram)
            assert path.exists()
            telegram.send_video.assert_awaited_once()
            assert telegram.send_video.call_args.kwargs['supports_streaming'] is True
            telegram.send_document.side_effect = None
            message = await bot._deliver_file(1, result, telegram)
            assert message is telegram.send_document.return_value
            assert not path.exists()
    asyncio.run(run())


def test_full_download_options_delivery_restart_and_file_id_reuse(tmp_path):
    async def run():
        query, ctx, update = context()
        video = info(formats=[fmt('v'), fmt('a', audio=True, lang='ru')])
        bot._save_session_safe(1, 10, URL, video, 1, ctx=ctx)
        store.preferences(1, {'quality': '720', 'audio_language': 'ru', 'compatible': True})
        async def download(**kwargs):
            path = kwargs['output_dir'] / 'result.mp4'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'test')
            assert kwargs['max_height'] == 720
            assert kwargs['audio_language'] == 'ru'
            return downloader.DownloadResult(True, path, file_size=4, streamable=True)
        ctx.bot.send_video.return_value = SimpleNamespace(video=SimpleNamespace(file_id='cached-video', file_size=4))
        with mock.patch.object(bot, 'disk_has_capacity', return_value=True), \
                mock.patch.object(config, 'DOWNLOAD_DIR', tmp_path), \
                mock.patch.object(config, 'COOKIES_FILE', ''), \
                mock.patch.object(config, 'LOCAL_API_SERVER', ''), \
                mock.patch.object(bot, 'download_video', side_effect=download) as download_mock:
            await bot._handle_download_callback(query, ctx, 'dl:v:best')
            row = store.queue(1)[0]
            assert row['status'] == 'ready'
            assert not ctx.bot_data['_jobs_live']
            # Simulate a restart: memory no longer supplies delivery metadata.
            ctx.user_data.clear()
            fileserver._registry.clear()
            fileserver._restore_registry()
            await bot._handle_deliver_callback(query, ctx, f'deliver:{row["id"]}:tg')
            assert ctx.bot.send_video.call_args.kwargs['supports_streaming'] is True
            assert store.queue(1)[0]['status'] == 'done'
            await bot._handle_download_callback(query, ctx, 'dl:v:best')
            assert download_mock.await_count == 1
            assert ctx.bot.send_video.call_args.kwargs['video'] == 'cached-video'
            assert store.queue(1)[0]['status'] == 'done'
        for task in list(bot._bg_tasks):
            task.cancel()
        await asyncio.gather(*bot._bg_tasks, return_exceptions=True)
    asyncio.run(run())


def test_subscription_quota_owner_isolation_and_explicit_opt_in():
    assert store.subscriptions(due=True) == []
    for n in range(3):
        store.subscribe(1, 1, f'https://www.youtube.com/@channel{n}/videos', str(n), [])
    with pytest.raises(ValueError):
        store.subscribe(1, 1, 'https://www.youtube.com/@extra/videos', 'Extra', [])
    with pytest.raises(ValueError):
        store.subscribe(2, -10, 'https://www.youtube.com/@extra/videos', 'Extra', [])
    row = store.subscriptions(1)[0]
    store.unsubscribe(row['id'], 2)
    assert len(store.subscriptions(1)) == 3
    store.unsubscribe(row['id'], 1)
    assert len(store.subscriptions(1)) == 2


def test_subscription_monitor_bounded_notifications_and_rechecks_unsubscribe():
    async def run():
        _, ctx, _ = context()
        store.subscribe(1, 1, 'https://www.youtube.com/@channel/videos', 'Канал', [URL])
        row = store.subscriptions(1)[0]
        with db.get_connection() as conn:
            conn.execute('UPDATE subscriptions SET checked=0')
        ui = features.Features(bot)
        video = info(is_playlist=True, playlist_entries=[{'index': i, 'title': str(i),
                      'url': f'https://www.youtube.com/watch?v=abcdefghij{i}'} for i in range(8)])
        async def finish(_):
            raise asyncio.CancelledError
        with mock.patch.object(features.worker_rpc, 'get_video_info', return_value=video), \
                mock.patch.object(features.asyncio, 'sleep', side_effect=finish):
            with pytest.raises(asyncio.CancelledError):
                await ui.monitor(ctx)
        assert ctx.bot.send_message.await_count == 1
        assert ctx.bot.send_message.call_args.args[1].count('https://') == 5
        with db.get_connection() as conn:
            conn.execute('UPDATE subscriptions SET checked=0')
        async def concurrent_unsubscribe(_):
            store.unsubscribe(row['id'], 1)
            return video
        ctx.bot.send_message.reset_mock()
        with mock.patch.object(features.worker_rpc, 'get_video_info', side_effect=concurrent_unsubscribe), \
                mock.patch.object(features.asyncio, 'sleep', side_effect=finish):
            with pytest.raises(asyncio.CancelledError):
                await ui.monitor(ctx)
        ctx.bot.send_message.assert_not_awaited()
    asyncio.run(run())


def test_apple_podcast_playlist_uses_public_episode_urls_only():
    url = 'https://podcasts.apple.com/us/podcast/test/id1234'
    payload = {'results': [{'kind': 'podcast', 'collectionName': 'Test'},
                           {'kind': 'podcast-episode', 'trackName': 'Episode', 'trackViewUrl': url + '?i=12345',
                            'episodeUrl': 'https://example.com/private?signature=secret'},
                           {'kind': 'podcast-episode', 'trackViewUrl': url + '?token=secret'}]}
    ydl = mock.Mock()
    ydl.urlopen.return_value = io.BytesIO(json.dumps(payload).encode())
    result = downloader._apple_playlist(ydl, url)
    assert result['entries'] == [{'_type': 'url', 'url': url + '?i=12345', 'title': 'Episode'}]
    assert canonical_url(url + '?i=12345') == url + '?i=12345'
    assert canonical_url(url + '?i=12345&token=secret') is None


def test_quick_tunnel_lease_changes_and_expires(tmp_path):
    path = tmp_path / 'ready_url'
    with mock.patch.object(runtime, 'QUICK_TUNNEL', True), mock.patch.object(runtime, 'QUICK_FILE', path), \
            mock.patch.object(config, 'PUBLIC_BASE_URL', ''):
        for value in ('https://first.trycloudflare.com', 'https://second.trycloudflare.com'):
            path.write_text(value)
            runtime.refresh_tunnel()
            assert config.PUBLIC_BASE_URL == value
        os.utime(path, (time.time() - 60, time.time() - 60))
        runtime.refresh_tunnel()
        assert config.PUBLIC_BASE_URL == ''


def test_healthcheck_requires_recent_runtime_and_api_heartbeat(tmp_path):
    path = tmp_path / 'health.json'
    state = {'pid': os.getpid(), 'heartbeat': time.time(), 'telegram_ok_at': time.time(), 'fileserver': False}
    script = Path(runtime.__file__).with_name('healthcheck.py')
    env = {'PATH': os.environ['PATH'], 'DB_PATH': str(tmp_path / 'bot.db'), 'PYTHONDONTWRITEBYTECODE': '1'}
    for heartbeat, expected in [(time.time(), 0), (time.time() - 180, 1)]:
        state['telegram_ok_at'] = heartbeat
        path.write_text(json.dumps(state))
        result = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, timeout=5)
        assert result.returncode == expected


def test_old_history_and_sessions_remove_unknown_path_credentials():
    db.upsert_user(1, '', '')
    dl_id = db.add_download(1, 'https://example.com/private-path-token')
    db.save_session(1, 1, 'https://example.com/private-path-token', '{}', 1)
    with db.get_connection() as conn:
        conn.execute("DELETE FROM stats WHERE key='public_urls_v2'")
    db.init_db()
    assert db.get_user_history(1)[0]['id'] == dl_id
    assert db.get_user_history(1)[0]['url'] == '<private-source>'
    assert db.get_session(1, 1, 1) is None
    assert bot._redact_url_for_storage('https://example.com/secret') == '<private-source>'


def test_history_pruning_keeps_queued_jobs_and_ready_files(tmp_path):
    db.upsert_user(1, '', '')
    active = db.add_download(1, URL)
    db.update_download(active, status='queued')
    with mock.patch.object(db, 'MAX_HISTORY_PER_USER', 1):
        ready = db.add_download(1, URL)
        db.update_download(ready, status='ready')
        old = db.add_download(1, URL)
        db.update_download(old, status='done')
        db.add_download(1, URL)
    rows = {row['id']: row for row in db.get_user_history(1, 20)}
    assert active in rows and ready in rows and old not in rows


def test_manual_certificate_renewal_dry_run_and_install(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = (root / 'nginx' / 'renew-certificate.sh').read_text()
    script = script.replace('/etc/letsencrypt', str(tmp_path / 'le')).replace('/etc/nginx/ssl', str(tmp_path / 'ssl'))
    executable = tmp_path / 'renew.sh'
    executable.write_text(script)
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    calls = tmp_path / 'calls'
    for name in ('certbot', 'nginx'):
        path = binaries / name
        path.write_text('#!/bin/sh\nprintf "%s %s\\n" "' + name + '" "$*" >> "$TEST_CALLS"\n')
        path.chmod(0o755)
    lineage = tmp_path / 'le' / 'live' / 'example.com'
    lineage.mkdir(parents=True)
    (lineage / 'fullchain.pem').write_text('synthetic-certificate')
    (lineage / 'privkey.pem').write_text('synthetic-key')
    (tmp_path / 'ssl').mkdir()
    env = {'PATH': f'{binaries}:/usr/bin:/bin', 'SSLIP_DOMAIN': 'example.com', 'TEST_CALLS': str(calls)}
    subprocess.run(['sh', str(executable), '--dry-run'], env=env, check=True, capture_output=True)
    assert not (tmp_path / 'ssl' / 'privkey.pem').exists()
    assert '--dry-run' in calls.read_text() and 'nginx' not in calls.read_text()
    subprocess.run(['sh', str(executable)], env=env, check=True, capture_output=True)
    assert (tmp_path / 'ssl' / 'privkey.pem').stat().st_mode & 0o777 == 0o600
    assert 'nginx -t' in calls.read_text() and 'nginx -s reload' in calls.read_text()


@pytest.mark.parametrize('playlist', [False, True])
def test_status_message_failure_does_not_leave_job_running(tmp_path, playlist):
    async def run():
        query, ctx, _ = context()
        bot._save_session_safe(1, 10, URL, info(is_playlist=playlist), 1, ctx=ctx)
        query.message.edit_text.side_effect = NetworkError('offline')
        ctx.bot.send_message.side_effect = NetworkError('offline')
        with mock.patch.object(bot, 'disk_has_capacity', return_value=True), \
                mock.patch.object(config, 'DOWNLOAD_DIR', tmp_path), \
                mock.patch.object(config, 'ALLOW_PLAYLISTS', True):
            handler = bot._handle_playlist_callback if playlist else bot._handle_download_callback
            try:
                await handler(query, ctx, 'pl:5:best' if playlist else 'dl:v:best')
            except NetworkError:
                pass
        assert not ctx.bot_data.get('_jobs_live')
        assert not ctx.user_data.get('_cancel_flags')
        assert store.queue(1)[0]['status'] == 'error'
    asyncio.run(run())


@pytest.mark.parametrize('cancel', [False, True])
def test_batch_files_survive_telegram_outage_or_shutdown(tmp_path, cancel):
    async def run():
        query, ctx, _ = context()
        download_id = db.add_download(1, URL)
        db.update_download(download_id, title='Пачка', status='ready')
        results = []
        for index in range(2):
            path = tmp_path / f'{index}.mp4'
            path.write_bytes(b'example')
            results.append(downloader.DownloadResult(True, path, file_size=7))
        with mock.patch.object(config, 'DOWNLOAD_DIR', tmp_path), \
                mock.patch.object(bot, '_has_fileserver', return_value=False), \
                mock.patch.object(bot, '_deliver_file', side_effect=asyncio.CancelledError if cancel else NetworkError('offline')):
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await bot._deliver_results_batch(1, results, ctx, download_id=download_id)
            else:
                assert await bot._deliver_results_batch(1, results, ctx, download_id=download_id) == (0, 2)
            records = [row for row in db.list_deliveries().values() if row['download_id'] == download_id]
            assert len(records) == 2 and all(Path(row['path']).exists() for row in records)
            # Restored batch delivery advances to the next file before marking the job done.
            ctx.user_data.clear()
            with mock.patch.object(bot, '_deliver_file_with_progress', new=mock.AsyncMock(return_value=None)):
                await bot._handle_deliver_callback(query, ctx, f'deliver:{download_id}:tg')
                assert store.queue(1)[0]['status'] == 'ready'
                await bot._handle_deliver_callback(query, ctx, f'deliver:{download_id}:tg')
                assert store.queue(1)[0]['status'] == 'done'
        for task in list(bot._bg_tasks):
            task.cancel()
        await asyncio.gather(*bot._bg_tasks, return_exceptions=True)
    asyncio.run(run())


def test_deploy_uses_compose_dotenv_interpolation_and_duplicate_keys(tmp_path):
    standalone = os.environ.get('COMPOSE_TEST_BIN')
    docker = shutil.which('docker')
    if not standalone and not docker:
        pytest.skip('Docker Compose is not installed')
    root = Path(__file__).resolve().parents[1]
    for name in ('deploy.sh', 'docker-compose.yml'):
        shutil.copyfile(root / name, tmp_path / name)
    (tmp_path / '.env').write_text('BASE_MODE=true\nENABLE_CLOUDFLARED=false\n'
                                 'ENABLE_CLOUDFLARED="${BASE_MODE}" # last assignment wins\n'
                                 "COMPOSE_PROFILES='ssl,pot'\nENABLE_POT_PROVIDER=${BASE_MODE}\n")
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    # Only the read-only config command reaches the real Compose binary.
    real = [standalone] if standalone else [docker, 'compose']
    wrapper = binaries / 'docker'
    wrapper.write_text(f'#!{sys.executable}\nimport os,sys,subprocess\n'
                       'if sys.argv[1:4] == ["compose", "config", "--environment"]:\n'
                       f'    sys.exit(subprocess.call({real!r} + sys.argv[2:]))\n'
                       'assert sys.argv[1:] == ["compose", "up", "-d"]\n'
                       'print("selected=" + os.environ["COMPOSE_PROFILES"])\n')
    wrapper.chmod(0o755)
    result = subprocess.run(['bash', str(tmp_path / 'deploy.sh'), 'up'], text=True, capture_output=True,
                            env={'PATH': f'{binaries}:/usr/bin:/bin', 'BOT_TOKEN': '123:synthetic', 'ADMIN_IDS': '1'})
    assert result.returncode == 0, result.stderr
    assert 'selected=ssl,pot,cloudflare' in result.stdout


def test_http_resume_completion_survives_restart(tmp_path):
    from aiohttp import ClientSession
    async def run():
        context()
        download_id = db.add_download(1, URL)
        db.update_download(download_id, status='ready')
        path = tmp_path / 'file.bin'
        path.write_bytes(b'payload')
        with mock.patch.object(config, 'DOWNLOAD_DIR', tmp_path):
            token = fileserver.move_and_register(path, download_id=download_id)
            runner = await fileserver.start(host='127.0.0.1', port=0)
            link = f'http://127.0.0.1:{runner.addresses[0][1]}/dl/{token}'
            try:
                async with ClientSession() as client:
                    async with client.get(link, headers={'Range': 'bytes=0-2'}) as response:
                        assert response.status == 206 and await response.read() == b'pay'
                    assert store.queue(1)[0]['status'] == 'ready'
                    fileserver._registry.clear()
                    fileserver._restore_registry()
                    assert fileserver.get_entry(token).ranges == [[0, 3]]
                    async with client.get(link, headers={'Range': 'bytes=3-'}) as response:
                        assert response.status == 206 and await response.read() == b'load'
                    assert store.queue(1)[0]['status'] == 'done'
            finally:
                await fileserver.stop()
                fileserver.unregister(token, delete_file=True)
    asyncio.run(run())
