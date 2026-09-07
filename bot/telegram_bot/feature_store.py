"""Owner-scoped preferences, jobs, Telegram reuse and opt-in subscriptions."""

import json
import time

import database as db
from media_options import validate
from safe_urls import canonical_url


def init(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS preferences (user_id INTEGER PRIMARY KEY, options TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs (
            download_id INTEGER PRIMARY KEY REFERENCES download_history(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL, url TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(user_id);
        CREATE TABLE IF NOT EXISTS media_cache (
            user_id INTEGER NOT NULL, cache_key TEXT NOT NULL, file_id TEXT NOT NULL,
            kind TEXT NOT NULL, size INTEGER NOT NULL, created REAL NOT NULL,
            PRIMARY KEY (user_id, cache_key));
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
            url TEXT NOT NULL, title TEXT NOT NULL, seen TEXT NOT NULL, checked REAL NOT NULL,
            UNIQUE (user_id, url));
    ''')


def preferences(user_id, values=None):
    with db.get_connection() as conn:
        if values is not None:
            conn.execute('INSERT OR REPLACE INTO preferences VALUES (?, ?)', (user_id, json.dumps(validate(values))))
        row = conn.execute('SELECT options FROM preferences WHERE user_id=?', (user_id,)).fetchone()
        return validate(json.loads(row[0]) if row else {})


def save_job(download_id, user_id, url, payload):
    url = canonical_url(url)
    if not url:
        return
    with db.get_connection() as conn:
        conn.execute('INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?)',
                     (download_id, user_id, url, json.dumps(payload)))


def job(download_id, user_id):
    with db.get_connection() as conn:
        row = conn.execute('SELECT j.*, h.status, h.title FROM jobs j JOIN download_history h ON h.id=j.download_id '
                           'WHERE j.download_id=? AND j.user_id=?', (download_id, user_id)).fetchone()
        return dict(row) if row else None


def queue(user_id):
    with db.get_connection() as conn:
        return [dict(row) for row in conn.execute('''
            SELECT h.*, CASE WHEN h.status='queued' THEN
                (SELECT count(*) FROM download_history q WHERE q.status='queued' AND q.id<=h.id)
                ELSE NULL END AS position
            FROM download_history h WHERE h.user_id=?
            ORDER BY h.status IN ('pending','queued','downloading','processing') DESC, h.id DESC LIMIT 15
        ''', (user_id,))]


def cached(user_id, key):
    if not key:
        return None
    with db.get_connection() as conn:
        conn.execute('DELETE FROM media_cache WHERE created<?', (time.time() - 7 * 86400,))
        row = conn.execute('SELECT * FROM media_cache WHERE user_id=? AND cache_key=?', (user_id, key)).fetchone()
        return dict(row) if row else None


def remember_file(user_id, key, message):
    if not key or not message:
        return
    for kind in ('video', 'audio', 'document'):
        media = getattr(message, kind, None)
        if media and isinstance(media.file_id, str):
            with db.get_connection() as conn:
                conn.execute('INSERT OR REPLACE INTO media_cache VALUES (?, ?, ?, ?, ?, ?)',
                             (user_id, key, media.file_id, kind, media.file_size or 0, time.time()))
                conn.execute('DELETE FROM media_cache WHERE user_id=? AND cache_key NOT IN '
                             '(SELECT cache_key FROM media_cache WHERE user_id=? ORDER BY created DESC LIMIT 200)',
                             (user_id, user_id))
            break


def forget_file(user_id, key):
    with db.get_connection() as conn:
        conn.execute('DELETE FROM media_cache WHERE user_id=? AND cache_key=?', (user_id, key))


def ready_delivery(user_id, key):
    if not key:
        return None
    with db.get_connection() as conn:
        rows = conn.execute('SELECT d.* FROM deliveries d JOIN download_history h ON h.id=d.download_id '
                            'WHERE h.user_id=? AND d.expires_at>? ORDER BY d.expires_at DESC LIMIT 200',
                            (user_id, time.time()))
        return next((dict(row) for row in rows if json.loads(row['context']).get('cache_key') == key), None)


def subscriptions(user_id=None, *, due=False):
    with db.get_connection() as conn:
        if due:
            rows = conn.execute('SELECT * FROM subscriptions WHERE checked<? ORDER BY checked LIMIT 20',
                                (time.time() - 6 * 3600,))
        else:
            rows = conn.execute('SELECT * FROM subscriptions WHERE user_id=? ORDER BY id', (user_id,))
        return [dict(row) for row in rows]


def subscribe(user_id, chat_id, url, title, seen):
    url = canonical_url(url)
    if not url or chat_id != user_id:
        raise ValueError('Подписки доступны в личном чате для публичных каналов и подкастов.')
    with db.get_connection() as conn:
        # Reserve quota atomically against parallel commands.
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT count(*) FROM subscriptions WHERE user_id=?', (user_id,)).fetchone()[0] >= 3:
            raise ValueError('Лимит: три подписки. Удалите ненужную через /subscriptions.')
        if conn.execute('SELECT count(*) FROM subscriptions').fetchone()[0] >= 500:
            raise ValueError('Достигнут общий лимит подписок сервера.')
        conn.execute('INSERT INTO subscriptions(user_id,chat_id,url,title,seen,checked) VALUES(?,?,?,?,?,?) '
                     'ON CONFLICT(user_id,url) DO UPDATE SET title=excluded.title',
                     (user_id, chat_id, url, title[:200], json.dumps(seen[:200]), time.time()))


def unsubscribe(sub_id, user_id):
    with db.get_connection() as conn:
        conn.execute('DELETE FROM subscriptions WHERE id=? AND user_id=?', (sub_id, user_id))


def checked(sub_id, user_id, seen):
    with db.get_connection() as conn:
        conn.execute('UPDATE subscriptions SET checked=?, seen=? WHERE id=? AND user_id=?',
                     (time.time(), json.dumps(seen[:200]), sub_id, user_id))
