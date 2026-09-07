"""User menus for media options, search, durable jobs and opt-in notifications."""

import asyncio
import json
import time

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler

import config
import database as db
import feature_store as store
import media_options as media
import worker_rpc
from safe_urls import canonical_url


def options(ctx, message, user_id):
    return media.validate(ctx.user_data.get('_media_options', {}).get(
        (message.chat_id, message.message_id), store.preferences(user_id)))


def set_options(ctx, message, values):
    cache = ctx.user_data.setdefault('_media_options', {})
    cache[(message.chat_id, message.message_id)] = media.validate(values)
    while len(cache) > 30:
        cache.pop(next(iter(cache)))


async def edit(query, text, keyboard):
    if query.message.photo:
        await query.edit_message_caption(caption=text[:1000], reply_markup=Markup(keyboard))
    else:
        await query.edit_message_text(text, reply_markup=Markup(keyboard))


class Features:
    def __init__(self, core):
        self.core = core

    def install(self, app):
        app.bot_data['_features'] = self
        for name in ('settings', 'search', 'queue', 'retry', 'subscribe', 'subscriptions'):
            app.add_handler(CommandHandler(name, self.core.require_auth(getattr(self, name))))
        app.add_handler(CallbackQueryHandler(self.core.require_auth(self.callback), pattern=r'^fx:'))

    async def settings(self, update, ctx):
        user_id = update.effective_user.id
        values = store.preferences(user_id)
        args = ctx.args or []
        if args:
            if args == ['reset']:
                values = dict(media.DEFAULTS)
            elif len(args) == 2 and args[0] in media.DEFAULTS:
                key, value = args
                if key in ('audio_language', 'subtitle_language') and value == 'off':
                    value = ''
                if key in ('compatible', 'skip_duplicates'):
                    value = {'on': True, 'off': False}.get(value, value)
                checked = media.validate({key: value})
                if checked[key] != value:
                    await update.message.reply_text('Недопустимое значение настройки.')
                    return
                values[key] = value
            else:
                await update.message.reply_text('Пример: /settings quality auto; /settings audio_language ru; '
                                                '/settings compatible on; /settings reset')
                return
            values = store.preferences(user_id, values)
        labels = {'mode': 'Режим', 'quality': 'Качество', 'audio_language': 'Язык аудио',
                  'subtitle_language': 'Язык субтитров', 'subtitle_source': 'Источник субтитров',
                  'subtitle_format': 'Формат субтитров', 'compatible': 'Плеер MP4',
                  'sponsorblock': 'SponsorBlock', 'skip_duplicates': 'Повтор из кэша'}
        await update.message.reply_text('Ваши настройки:\n' + '\n'.join(
            f'{labels[key]} ({key}): {value if value != "" else "off"}' for key, value in values.items())
            + '\n\nИзменить: /settings имя значение\n'
              'quality: best, auto, 1080, 720, 480, 360\n'
              'mode: video, mp3, opus, wav\n'
              'subtitle_source: manual, auto; subtitle_format: embed, srt, vtt, txt\n'
              'sponsorblock: default, off, mark, remove\n'
              'compatible / skip_duplicates: on, off\n'
              'Язык: код (ru, en, …) или off. /settings reset — сброс.\n'
              'Для одного видео: кнопка «Параметры» под ссылкой.')

    async def menu(self, query, ctx, info, values):
        rows = [
            [Button(f'Режим: {values["mode"]}', callback_data='fx:cycle:mode'),
             Button(f'Качество: {values["quality"]}', callback_data='fx:cycle:quality')],
            [Button(f'Аудио: {values["audio_language"] or "исходное"}', callback_data='fx:langs:audio:0')],
        ]
        if config.ALLOW_SUBTITLES and not info.is_playlist:
            rows += [[Button(f'Субтитры: {values["subtitle_language"] or "выкл."}', callback_data='fx:langs:sub:0')],
                     [Button(f'Источник: {values["subtitle_source"]}', callback_data='fx:cycle:subtitle_source'),
                      Button(f'Формат: {values["subtitle_format"]}', callback_data='fx:cycle:subtitle_format')]]
        rows += [[Button(f'Плеер MP4: {"да" if values["compatible"] else "нет"}', callback_data='fx:cycle:compatible'),
                  Button(f'SponsorBlock: {values["sponsorblock"]}', callback_data='fx:cycle:sponsorblock')]]
        if info.is_playlist:
            rows += [[Button('Выбрать номера из списка', callback_data='fx:items:0')]]
        rows += [[Button('💾 Сохранить как мои настройки', callback_data='fx:save')],
                 [Button('⬇️ Скачать с этими параметрами', callback_data='fx:download')],
                 [Button('Назад', callback_data='back_to_quality')]]
        selection = ctx.user_data.get('_playlist_items', {}).get((query.message.chat_id, query.message.message_id))
        text = f'Параметры: {info.title[:120]}\n'
        text += 'Авто — оценка размера вместе с аудио и запасом под лимит Telegram.\n'
        if info.is_playlist:
            text += f'Выбрано: {selection or "первые " + str(config.MAX_PLAYLIST_ITEMS)}. '
            text += 'Для плейлиста доступны качество, язык и совместимый MP4; аудио сохраняется в исходном формате. '
            text += 'Авто использует профиль до 480p, точный размер проверяется после загрузки.'
        await edit(query, text, rows)

    async def languages(self, query, ctx, info, values, kind, page):
        if kind == 'audio':
            codes = sorted({f.language for f in info.formats if media.language(f.language)})
            if info.is_playlist:
                codes = ['en', 'ru']
        else:
            codes = sorted(info.subtitles if values['subtitle_source'] == 'manual' else info.automatic_subtitles)
        rows = [[Button('Исходное / выключить', callback_data=f'fx:lang:{kind}:-1')]]
        for index in range(page * 12, min(len(codes), (page + 1) * 12)):
            rows.append([Button(codes[index], callback_data=f'fx:lang:{kind}:{index}')])
        nav = []
        if page:
            nav.append(Button('←', callback_data=f'fx:langs:{kind}:{page-1}'))
        if len(codes) > (page + 1) * 12:
            nav.append(Button('→', callback_data=f'fx:langs:{kind}:{page+1}'))
        if nav:
            rows.append(nav)
        rows.append([Button('Параметры', callback_data='fx:options')])
        await edit(query, 'Выберите язык. Для субтитров сначала выберите manual (авторские) или auto (автоматические).', rows)
        return codes

    async def start(self, update, ctx, query, data):
        error = self.core._claim_download(ctx, query.from_user.id, query.message.chat_id, query.message.message_id)
        if error:
            await query.answer(error, show_alert=True)
            return
        async def run():
            try:
                if data.startswith('pl:'):
                    await self.core._handle_playlist_callback(query, ctx, data)
                else:
                    await self.core._handle_download_callback(query, ctx, data)
            finally:
                self.core._release_download(ctx, query.from_user.id, query.message.chat_id, query.message.message_id)
        await self.core._spawn_update_task(update, ctx, run(), 'media_options', query.message)

    async def callback(self, update, ctx):
        query = update.callback_query
        parts = query.data.split(':')
        await query.answer()
        if not self.core._allow_user_action(query.from_user.id):
            return
        action = parts[1]
        try:
            if action == 'unsubscribe':
                store.unsubscribe(int(parts[2]), query.from_user.id)
                await query.edit_message_text('Подписка удалена.')
                return
            if action == 'job':
                await self.retry_id(update, ctx, int(parts[2]))
                return
            if action == 'stop':
                job = ctx.bot_data.get('_jobs_live', {}).get(int(parts[2]))
                if job and job[0] == query.from_user.id:
                    job[1][0] = True
                    await query.edit_message_text('Запрошена отмена. /queue — обновить состояние.')
                return
            if action == 'result':
                key = (query.message.chat_id, query.message.message_id)
                saved = ctx.user_data.get('_search_results', {}).get(key)
                if not saved or time.monotonic() - saved[0] > 1800:
                    raise ValueError('Результаты устарели. Повторите /search.')
                index = int(parts[2])
                if not 0 <= index < len(saved[1]):
                    raise ValueError('Результат недоступен.')
                msg = await query.message.reply_text('⏳ Получаю информацию…')
                await self.core._fetch_and_show_menu(saved[1][index]['url'], msg, ctx, query.from_user.id)
                return
            info, url = self.core._restore_session(ctx, query.message.chat_id, query.message.message_id,
                                                   user_id=query.from_user.id)
            if not info or not url:
                raise ValueError('Сессия истекла. Отправьте ссылку заново.')
            values = options(ctx, query.message, query.from_user.id)
            if action == 'cycle':
                key = parts[2]
                if key in media.CHOICES:
                    choices = media.CHOICES[key]
                    values[key] = choices[(choices.index(values[key]) + 1) % len(choices)]
                elif key == 'compatible':
                    values[key] = not values[key]
                else:
                    raise ValueError('Неизвестная настройка.')
                set_options(ctx, query.message, values)
            elif action in ('langs', 'lang'):
                kind = parts[2]
                if kind not in ('audio', 'sub'):
                    raise ValueError('Неизвестный список языков.')
                index = int(parts[3])
                if action == 'langs':
                    if not 0 <= index < 100:
                        raise ValueError('Страница недоступна.')
                    await self.languages(query, ctx, info, values, kind, index)
                    return
                if kind == 'audio':
                    codes = ['en', 'ru'] if info.is_playlist else sorted({f.language for f in info.formats if media.language(f.language)})
                else:
                    codes = sorted(info.subtitles if values['subtitle_source'] == 'manual' else info.automatic_subtitles)
                if not -1 <= index < len(codes):
                    raise ValueError('Язык недоступен.')
                values['audio_language' if kind == 'audio' else 'subtitle_language'] = codes[index] if index >= 0 else ''
                set_options(ctx, query.message, values)
            elif action == 'save':
                store.preferences(query.from_user.id, values)
            elif action == 'items':
                if not info.is_playlist or not config.ALLOW_PLAYLISTS:
                    raise ValueError('Это не плейлист.')
                page = int(parts[2])
                entries = info.playlist_entries[page * 15:(page + 1) * 15] if 0 <= page < 14 else []
                if not entries:
                    raise ValueError('Список элементов недоступен.')
                ctx.user_data['_playlist_input'] = (query, time.monotonic())
                rows = []
                if page:
                    rows.append([Button('←', callback_data=f'fx:items:{page-1}')])
                if len(info.playlist_entries) > (page + 1) * 15:
                    rows.append([Button('→', callback_data=f'fx:items:{page+1}')])
                rows.append([Button('Параметры', callback_data='fx:options')])
                text = '\n'.join(f'{entry["index"]}. {entry["title"][:40]}' for entry in entries)
                await edit(query, text + f'\n\nОтветьте номерами: 1,3,7-10. До {config.MAX_PLAYLIST_ITEMS} элементов.', rows)
                return
            elif action == 'download':
                if info.is_playlist:
                    selected = ctx.user_data.get('_playlist_items', {}).get((query.message.chat_id, query.message.message_id))
                    count = len(selected.split(',')) if selected else config.MAX_PLAYLIST_ITEMS
                    data = f'pl:{count}:{"best" if values["mode"] == "video" else "audio"}'
                else:
                    data = 'dl:' + {'video': 'v', 'mp3': 'a', 'opus': 'ao', 'wav': 'aw'}[values['mode']] + ':best'
                await self.start(update, ctx, query, data)
                return
            elif action != 'options':
                raise ValueError('Неизвестное действие.')
            await self.menu(query, ctx, info, values)
        except (ValueError, IndexError) as exc:
            await query.message.reply_text(str(exc)[:300])

    async def handle_input(self, update, ctx):
        pending = ctx.user_data.get('_playlist_input')
        if not pending or pending[0].message.chat_id != update.effective_chat.id:
            return False
        query, timestamp = pending
        text = update.message.text.strip()
        if time.monotonic() - timestamp > 600 or 'http' in text:
            ctx.user_data.pop('_playlist_input', None)
            return False
        info, _ = self.core._restore_session(ctx, query.message.chat_id, query.message.message_id, update.effective_user.id)
        try:
            if not info:
                raise ValueError('Сессия истекла. Отправьте ссылку заново.')
            indices = media.playlist_indices(text, config.MAX_PLAYLIST_ITEMS,
                                            max((e['index'] for e in info.playlist_entries), default=0))
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return True
        selections = ctx.user_data.setdefault('_playlist_items', {})
        selections[(query.message.chat_id, query.message.message_id)] = indices
        while len(selections) > 30:
            selections.pop(next(iter(selections)))
        ctx.user_data.pop('_playlist_input', None)
        await self.menu(query, ctx, info, options(ctx, query.message, update.effective_user.id))
        return True

    async def search(self, update, ctx):
        if not self.core._allow_user_action(update.effective_user.id):
            return
        text = ' '.join(ctx.args or [])
        if not text or len(text) > 200:
            await update.message.reply_text('Использование: /search название видео (до 200 символов).')
            return
        msg = await update.message.reply_text('🔎 Ищу на YouTube…')
        try:
            async with ctx.bot_data['_metadata_sem']:
                results = await worker_rpc.search_videos(text)
            rows = [[Button(item['title'][:70], callback_data=f'fx:result:{i}')] for i, item in enumerate(results)]
            await msg.edit_text('Выберите видео:' if rows else 'Ничего не найдено.', reply_markup=Markup(rows) if rows else None)
            cache = ctx.user_data.setdefault('_search_results', {})
            cache[(msg.chat_id, msg.message_id)] = (time.monotonic(), results)
            while len(cache) > 5:
                cache.pop(next(iter(cache)))
        except (RuntimeError, TimeoutError, ValueError):
            await msg.edit_text('Поиск временно недоступен. Попробуйте позднее.')

    async def queue(self, update, ctx):
        rows = store.queue(update.effective_user.id)
        labels = {'pending': 'ожидает', 'queued': 'в очереди', 'downloading': 'скачивается',
                  'processing': 'обрабатывается', 'ready': 'готов к выдаче', 'done': 'доставлен',
                  'error': 'ошибка', 'cancelled': 'отменён', 'partial': 'частично'}
        text, buttons = [], []
        for row in rows:
            position = f' (позиция ≈{row["position"]})' if row['position'] else ''
            text.append(f'#{row["id"]} {labels.get(row["status"], row["status"])}{position} — {(row["title"] or "Загрузка")[:65]}')
            job = ctx.bot_data.get('_jobs_live', {}).get(row['id'])
            if job and job[0] == update.effective_user.id:
                buttons.append([Button(f'Отменить #{row["id"]}', callback_data=f'fx:stop:{row["id"]}')])
            elif db.get_delivery(row['id'], update.effective_user.id):
                delivery = db.get_delivery(row['id'], update.effective_user.id)
                if delivery:
                    entry = self.core.fileserver.get_entry(self.core.fileserver.token_for_key(delivery['token_key']))
                    if entry:
                        buttons.extend(self.core._build_delivery_buttons(row['id'], entry.file_size))
            elif store.job(row['id'], update.effective_user.id):
                buttons.append([Button(f'Повторить #{row["id"]}', callback_data=f'fx:job:{row["id"]}')])
        await update.effective_message.reply_text('\n'.join(text) or 'Очередь и архив пока пусты.',
                                                  reply_markup=Markup(buttons) if buttons else None)

    async def retry(self, update, ctx):
        try:
            await self.retry_id(update, ctx, int((ctx.args or [''])[0]))
        except ValueError:
            await update.message.reply_text('Использование: /retry номер из /queue.')

    async def retry_id(self, update, ctx, download_id):
        user_id = update.effective_user.id
        saved = store.job(download_id, user_id)
        if not saved or saved['status'] in ('pending', 'queued', 'downloading', 'processing'):
            await update.effective_message.reply_text('Загрузка недоступна для повтора.')
            return
        if not self.core._allow_user_action(user_id):
            return
        payload = json.loads(saved['payload'])
        msg = await update.effective_message.reply_text('⏳ Восстанавливаю параметры загрузки…')
        try:
            async with ctx.bot_data['_metadata_sem']:
                info = await worker_rpc.get_video_info(saved['url'])
            self.core._save_session_safe(msg.chat_id, msg.message_id, saved['url'], info, user_id, ctx=ctx)
            values = payload['options']
            set_options(ctx, msg, values)
            if payload.get('playlist_items'):
                ctx.user_data.setdefault('_playlist_items', {})[(msg.chat_id, msg.message_id)] = payload['playlist_items']
            if payload.get('clip_range'):
                ctx.user_data.setdefault(self.core.KEY_CLIP_RANGES, {})[(msg.chat_id, msg.message_id)] = payload['clip_range']
            query = self.core._MessageQuery(msg, update.effective_user)
            await self.start(update, ctx, query, payload['data'])
        except (RuntimeError, TimeoutError, ValueError):
            await msg.edit_text('Источник временно недоступен. Повторите позднее.')

    async def subscribe(self, update, ctx):
        if update.effective_chat.id != update.effective_user.id:
            await update.message.reply_text('Подписки доступны только в личном чате.')
            return
        if not self.core._allow_user_action(update.effective_user.id):
            return
        url = canonical_url(' '.join(ctx.args or []))
        if not url or not any(host in url for host in ('www.youtube.com/', 'podcasts.apple.com/')):
            await update.message.reply_text('Использование: /subscribe публичный URL канала/плейлиста YouTube или подкаста Apple Podcasts.')
            return
        msg = await update.message.reply_text('⏳ Проверяю источник…')
        try:
            async with ctx.bot_data['_metadata_sem']:
                info = await worker_rpc.get_video_info(url)
            if not info.is_playlist or not info.playlist_entries:
                raise ValueError('Нужен канал, плейлист или подкаст с выпусками.')
            store.subscribe(update.effective_user.id, update.effective_chat.id, url, info.title,
                            [entry['url'] for entry in info.playlist_entries if entry['url']])
            await msg.edit_text('Подписка включена. Проверка каждые 6 часов; до 5 новых ссылок за проверку. '
                                'Старые выпуски пропущены. Загрузка начинается только по вашему выбору. '
                                '/subscriptions — управление.')
        except ValueError as exc:
            await msg.edit_text(str(exc))
        except (RuntimeError, TimeoutError):
            await msg.edit_text('Не удалось проверить источник. Подписка не добавлена.')

    async def subscriptions(self, update, ctx):
        rows = store.subscriptions(update.effective_user.id)
        await update.effective_message.reply_text('Ваши подписки (проверка раз в 6 часов):' if rows else 'Подписок нет. Добавить: /subscribe URL',
            reply_markup=Markup([[Button('Удалить: ' + row['title'][:50], callback_data=f'fx:unsubscribe:{row["id"]}')] for row in rows]) if rows else None)

    async def monitor(self, app):
        while True:
            for row in store.subscriptions(due=True):
                owner = row['user_id']
                if not (db.is_super_admin(owner) or db.is_authorized(owner)):
                    store.unsubscribe(row['id'], owner)
                    continue
                seen = json.loads(row['seen'])
                try:
                    async with app.bot_data['_metadata_sem']:
                        info = await worker_rpc.get_video_info(row['url'])
                    entries = [entry for entry in info.playlist_entries if entry['url']]
                    fresh = [entry for entry in entries if entry['url'] not in seen][:5]
                    if fresh and (db.is_super_admin(owner) or db.is_authorized(owner)):
                        # Recheck explicit consent after the network request; unsubscribe may have raced.
                        if not any(item['id'] == row['id'] for item in store.subscriptions(owner)):
                            continue
                        await app.bot.send_message(row['chat_id'], 'Новые выпуски: ' + row['title'][:100] + '\n\n'
                            + '\n\n'.join(entry['title'][:150] + '\n' + entry['url'] for entry in fresh)
                            + '\n\nОтправьте нужную ссылку боту для выбора загрузки. /subscriptions — отключить.',
                            disable_web_page_preview=True)
                    seen = list(dict.fromkeys([entry['url'] for entry in entries] + seen))[:200]
                except (RuntimeError, TimeoutError, TelegramError, ValueError):
                    # Keep the previous baseline on failure, with a bounded retry interval.
                    pass
                store.checked(row['id'], owner, seen)
            await asyncio.sleep(60)
