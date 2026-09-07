"""Runtime readiness and live Quick Tunnel discovery."""

import asyncio
import json
import os
from pathlib import Path
import re
import time

import config

QUICK_TUNNEL = (config._is_true('ENABLE_CLOUDFLARED')
                and config._is_true('ENABLE_CLOUDFLARE_QUICK_TUNNEL') and not config.PUBLIC_BASE_URL)
QUICK_FILE = Path('/cf-url/ready_url')


def health_path():
    return config.DB_PATH.parent / 'health.json'


def refresh_tunnel():
    if not QUICK_TUNNEL:
        return
    url = ''
    try:
        if 0 <= time.time() - QUICK_FILE.stat().st_mtime <= 20:
            value = QUICK_FILE.read_text().strip()
            if re.fullmatch(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', value):
                url = value
    except OSError:
        pass
    config.PUBLIC_BASE_URL = url


async def monitor(application, fileserver_enabled):
    last_api_ok = time.time()  # PTB initialization has already called getMe.

    async def probe():
        nonlocal last_api_ok
        while True:
            await asyncio.sleep(30)
            try:
                await asyncio.wait_for(application.bot.get_me(), 8)
                last_api_ok = time.time()
            except Exception:
                pass

    probe_task = asyncio.create_task(probe())
    try:
        while True:
            if application.bot_data.get('_stopping'):
                return
            refresh_tunnel()
            state = {'pid': os.getpid(), 'heartbeat': time.time(), 'telegram_ok_at': last_api_ok,
                     'fileserver': fileserver_enabled, 'http_port': config.HTTP_PORT}
            target = health_path()
            tmp = target.with_suffix('.tmp')
            tmp.write_text(json.dumps(state))
            tmp.chmod(0o600)
            tmp.replace(target)
            await asyncio.sleep(2)
    finally:
        probe_task.cancel()
        await asyncio.gather(probe_task, return_exceptions=True)
        health_path().unlink(missing_ok=True)
