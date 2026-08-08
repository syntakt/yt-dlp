#!/usr/bin/env python3
"""Container healthcheck for the bot and its optional file server."""

from http.client import HTTPConnection

import config
import yt_dlp  # noqa: F401 - verifies that the runtime dependency imports


# Проверяем /health ровно в тех условиях, при которых бот реально поднимает
# файловый сервер (см. bot._post_init) — то есть когда задан хотя бы один
# внешний URL раздачи. Раньше сюда входил и quick-tunnel режим: пока cloudflared
# не отдал URL, PUBLIC_BASE_URL пуст, сервер не стартует, а healthcheck всё
# равно стучался в порт и навсегда оставлял контейнер unhealthy.
if config.PUBLIC_BASE_URL or config.DIRECT_BASE_URL or config.RELAY_BASE_URLS:
    connection = HTTPConnection('127.0.0.1', config.HTTP_PORT, timeout=5)
    try:
        connection.request('GET', '/health')
        response = connection.getresponse()
        if response.status != 200 or response.read(16).strip() != b'ok':
            raise SystemExit(1)
    finally:
        connection.close()
