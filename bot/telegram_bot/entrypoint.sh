#!/bin/sh
set -e

# Устанавливаем umask до сброса привилегий:
# 0022 → файлы 644, директории 755 — мир может читать.
# Это критично: telegram-bot-api контейнер (другой UID) должен
# иметь возможность stat() файлов в /downloads/fileserver/
umask 0022

# ── Права на смонтированных томах (нужны root-права, только здесь) ────────────
# chown только корневые директории (без -R): на большом /downloads -R медленный
mkdir -p /data/cache
chown botuser:botuser /downloads /data 2>/dev/null || true
chown botuser:botuser /data/cache 2>/dev/null || true
chmod 755 /downloads 2>/dev/null || true
chmod 700 /data 2>/dev/null || true

# Quick Tunnel readiness is refreshed by runtime.py without blocking startup.

# ── Снижаем привилегии и запускаем бота ───────────────────────────────────────
# gosu: exec заменяет shell → PID 1 = python → SIGTERM доходит до бота корректно.
# "$@" пробрасывает CMD из Dockerfile ("python" "bot.py").
exec gosu botuser "$@"
