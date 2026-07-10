#!/usr/bin/env python3
"""Container healthcheck for the bot and its optional file server."""

from http.client import HTTPConnection
import os

import config
import yt_dlp  # noqa: F401 - verifies that the runtime dependency imports


def _enabled(name: str) -> bool:
    return os.environ.get(name, '').strip().lower() in {'1', 'true', 'yes', 'on'}


quick_tunnel = _enabled('ENABLE_CLOUDFLARED') and _enabled(
    'ENABLE_CLOUDFLARE_QUICK_TUNNEL'
)
if (
    config.PUBLIC_BASE_URL
    or config.DIRECT_BASE_URL
    or config.RELAY_BASE_URLS
    or quick_tunnel
):
    connection = HTTPConnection('127.0.0.1', config.HTTP_PORT, timeout=5)
    try:
        connection.request('GET', '/health')
        response = connection.getresponse()
        if response.status != 200 or response.read(16).strip() != b'ok':
            raise SystemExit(1)
    finally:
        connection.close()
