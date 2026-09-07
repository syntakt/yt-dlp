#!/usr/bin/env python3
"""Read readiness from the running process, not from a fresh import."""

from http.client import HTTPConnection
import json
import os
import time

from runtime import health_path

try:
    state = json.loads(health_path().read_text())
    now = time.time()
    if not 0 <= now - state['heartbeat'] <= 30 or not 0 <= now - state['telegram_ok_at'] <= 120:
        raise SystemExit(1)
    os.kill(state['pid'], 0)
    if state['fileserver']:
        connection = HTTPConnection('127.0.0.1', state['http_port'], timeout=5)
        try:
            connection.request('GET', '/health')
            response = connection.getresponse()
            if response.status != 200 or response.read(16).strip() != b'ok':
                raise SystemExit(1)
        finally:
            connection.close()
except (OSError, ValueError, KeyError):
    raise SystemExit(1)
