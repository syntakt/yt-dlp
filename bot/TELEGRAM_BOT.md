# YT-DLP Telegram Bot

A fully-featured, Docker-ready Telegram bot that wraps **yt-dlp** to let
authorised users download videos and audio from YouTube and thousands of
other sites — all via a clean inline-button interface.

---

## Features

| Feature | Description |
|---------|-------------|
| 🔐 Access control | Only approved users can use the bot |
| 🎬 Video download | Choose from all available resolutions |
| 🎵 Audio extraction | MP3 download with one tap |
| 📋 Playlists | Download first N items of a playlist |
| 🧲 Torrents | magnet / `.torrent` via aria2c — opt-in, port never exposed |
| 📄 Subtitles | Download video with embedded subtitles |
| 📜 History | Per-user download history |
| 👑 Admin panel | Approve/ban users, view stats |
| 🐳 Docker | Single `cd bot && ./deploy.sh` deploy |
| 💾 SQLite | Zero-config persistent storage |
| 🔔 Admin notifications | Instant approval-request alerts |

---

## Quick Start

### 1. Create a Telegram bot

1. Open [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the **API token**

### 2. Find your Telegram user ID

Send any message to [@userinfobot](https://t.me/userinfobot) — it replies
with your numeric ID (e.g. `123456789`).

### 3. Configure

```bash
cd bot
cp .env.example .env
# Edit .env — at minimum set BOT_TOKEN and ADMIN_IDS
nano .env
```

### 4. Create data directories

```bash
mkdir -p bot/data/downloads bot/data/db
```

### 5. Build & run

```bash
cd bot
./deploy.sh
```

Check logs:
```bash
cd bot
./deploy.sh logs bot
```

---

## Usage

### User flow

1. Send `/start` — if you're not approved yet, the admin gets a notification
2. Once approved, send any video URL
3. The bot shows video info + quality buttons
4. Tap a quality → bot downloads and sends the file

### Commands (all users)

| Command | Description |
|---------|-------------|
| `/start` | Welcome & registration |
| `/help` | Command reference |
| `/history` | Last 10 downloads |
| `/status` | Bot & disk stats |
| `/cancel` | Cancel current operation |

### Commands (admin only)

| Command | Description |
|---------|-------------|
| `/pending` | List pending access requests |
| `/users` | List all approved users |
| `/approve <id>` | Approve a user |
| `/deny <id>` | Remove/deny a user |
| `/ban <id>` | Ban a user |
| `/unban <id>` | Unban a user |
| `/stats` | Global download statistics |

---

## Configuration reference

All settings are in `.env` (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `BOT_TOKEN` | — | **Required.** Telegram bot token |
| `ADMIN_IDS` | — | **Required.** Comma-separated admin user IDs |
| `REGISTRATION_MODE` | `closed` | `closed` = admin approves; `open` = anyone |
| `MAX_FILE_SIZE_MB` | `10240` | Max downloaded file size; file-server links support the full configured limit |
| `DOWNLOAD_TIMEOUT` | `3600` | Per-download timeout (seconds) |
| `INFO_TIMEOUT` | `120` | Metadata extraction timeout (seconds) |
| `MAX_CONCURRENT_DOWNLOADS` | `3` | Parallel download limit |
| `MAX_CONCURRENT_DOWNLOADS_PER_USER` | `1` | Active download limit per user |
| `MAX_PLAYLIST_TOTAL_MB` | `10240` | Aggregate size limit for one playlist |
| `MIN_FREE_DISK_MB` | `1024` | Disk space that downloads must leave free |
| `ALLOW_PLAYLISTS` | `true` | Enable playlist downloads |
| `ALLOW_AUDIO` | `true` | Enable audio-only (MP3) |
| `ALLOW_SUBTITLES` | `true` | Enable subtitle download |
| `PROXY_URL` | — | HTTP/SOCKS5 proxy URL |
| `COOKIES_FILE` | — | Path to Netscape cookies file |
| `ENABLE_CLOUDFLARED` | `false` | Start Cloudflare Tunnel container via `deploy.sh`; keep `false` to skip cloudflared entirely |
| `ENABLE_CLOUDFLARE_QUICK_TUNNEL` | `false` | Allow temporary Cloudflare Quick Tunnel when no tunnel token is set |
| `PUBLIC_BASE_URL` | — | Cloudflare/public file-server URL |
| `DIRECT_BASE_URL` | — | Direct HTTPS URL, usually served by manually configured nginx |
| `RELAY_BASE_URLS` | — | Comma-separated relay HTTPS base URLs |
| `ALLOW_GENERIC_URLS` | `false` | Allow yt-dlp generic extractor for arbitrary HTTP(S) pages |
| `SSRF_PROTECTION` | `true` | Reject non-public DNS results on every yt-dlp connection |
| `TRUST_PROXY_FOR_SSRF` | `false` | Opt in only for a proxy that filters private destinations itself |

---

## Age-restricted / login-required videos

Export your browser cookies with a browser extension such as
[Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
and place the file at `./cookies.txt`, then uncomment the volume line in
`docker-compose.yml` and set `COOKIES_FILE=/cookies.txt` in `.env`.

---

## Updating

```bash
git pull
cd bot && ./deploy.sh
```

---

## File size limits

The configured download limit is **10 GB** by default. Files delivered through
the built-in signed-link file server can use that full limit. Direct Telegram
delivery through the local Bot API is limited by Telegram to **2 GB**.

For files that do not fit the selected delivery channel:

- Choosing a lower quality
- Using audio-only mode
- Configure `PUBLIC_BASE_URL`, `DIRECT_BASE_URL`, or `RELAY_BASE_URLS` for signed links

---

## Security notes

- The `.env` file contains your bot token — **never commit it to git**
  (`.gitignore` already excludes `.env`)
- The bot runs as a non-root user inside Docker
- Only approved users can trigger downloads
- Generic extraction is disabled and DNS answers are checked again on each
  connection to prevent redirects or DNS rebinding into private networks

---

## BitTorrent (magnet + .torrent)

Disabled by default. Enable with `ALLOW_TORRENTS=true`. Downloads run through
`aria2c` (already in the image). A user can send a **magnet link** (preferably
with trackers) or upload a **`.torrent` file**; the bot fetches metadata, shows a
confirmation menu (name, size, media-file count), then downloads and delivers the
**media files** from the swarm (like a playlist).

### Port isolation (the core requirement)

The torrent listening port must never be reachable from the internet. This is
enforced by several layers:

1. **No port publishing / no DNAT.** The `ytdlp-bot` container publishes **no
   ports** and Docker's own iptables is disabled — all ingress is manual nftables
   DNAT on the host. `TORRENT_LISTEN_PORT` (default `51413`) is **never** added to
   `ports:` and **never** gets a DNAT rule, so nothing on the internet can reach it.
2. **No seeding.** `--seed-time=0` — the client stops as soon as the download
   finishes and never acts as a server, so an inbound port is not even needed.
3. **DHT / LPD / PEX disabled** by default — no UDP listeners, no announcing the
   node to the DHT network.

Verify: `docker exec ytdlp-bot ss -ltnp` (port bound only to the container IP);
`nmap -Pn -p 51413 <server_ip>` from outside during an active download → filtered.

### Outbound SSRF hardening (required when enabling torrents)

`aria2c` is a **subprocess** and does **not** inherit the Python SSRF guard used
for yt-dlp. Peer/tracker IPs from a malicious torrent could point at internal
services (e.g. `telegram-bot-api` at `10.10.2.3`, the host gateway). The bot
rejects trackers that resolve to non-public IPs, but it cannot filter peer IPs —
so you **must** block the container's access to private ranges with host nftables
egress rules. Add this in the `forward` chain **before** the generic
`docker_nets accept`:

```nft
define YTDLP_IP  = 10.10.2.2
define TG_API_IP = 10.10.2.3

# Allow only the internal service the bot legitimately needs:
ip saddr $YTDLP_IP ip daddr $TG_API_IP tcp dport 8081 accept
# Block the container from reaching ANY private range (SSRF pivot defense):
ip saddr $YTDLP_IP ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, \
    127.0.0.0/8, 169.254.0.0/16, 100.64.0.0/10 } \
    counter log prefix "[nft] ytdlp->private BLOCKED " drop
# Public internet keeps working through the existing MASQUERADE/accept rules.
# The torrent port is NOT forwarded: no ports:, no DNAT.
```

Verify: `docker exec ytdlp-bot sh -c 'wget -qO- http://10.10.2.3:8081; echo rc=$?'`
should be blocked (nft log line appears), while `wget -qO- https://ifconfig.me`
still works.

### `.torrent` file uploads

The local Bot API server stores uploaded files on its own filesystem and (in
`local_mode`) hands the bot an absolute path to them. To let the bot read that
path, `docker-compose.yml` mounts the API server's data directory read-only into
`ytdlp-bot` (`/var/lib/telegram-bot-api:ro`). If the bot cannot read the file
(e.g. filesystem permissions), it falls back to asking the user for a magnet link
— magnet downloads need no cross-container file access.

### Notes / limits

- With `TORRENT_ENABLE_DHT=false`, a magnet **without** trackers may fail to fetch
  metadata. Prefer magnets with `&tr=` or upload a `.torrent`.
- Only media files (`TORRENT_MEDIA_ONLY=true`) are delivered; each delivered file
  must fit `MAX_FILE_SIZE_MB`, and the whole swarm must fit `TORRENT_MAX_TOTAL_MB`.

---

## Architecture

```
bot/                          — all bot infrastructure (separate from yt-dlp core)
├── telegram_bot/
│   ├── bot.py                — Telegram handlers, inline menus, admin commands
│   ├── downloader.py         — yt-dlp wrapper (async, format parsing, progress)
│   ├── database.py           — SQLite: users, download history, stats
│   ├── fileserver.py         — HTTP file server with token-based access
│   ├── config.py             — Settings from environment variables
│   ├── entrypoint.sh         — Docker entrypoint (gosu privilege drop)
│   └── requirements.txt
├── nginx/
│   ├── nginx.conf.template   — SSL reverse proxy config
│   └── entrypoint.sh         — Certificate management
├── Dockerfile                — Python 3.12 + ffmpeg + yt-dlp from source
├── Dockerfile.cloudflared    — Cloudflare Tunnel container
├── Dockerfile.nginx          — Nginx SSL container
├── docker-compose.yml        — Service orchestration
├── deploy.sh                 — Build & deploy script
├── entrypoint.sh             — Quick Tunnel URL injection
├── cf-entrypoint.sh          — Cloudflared entrypoint
└── .env.example              — Configuration template
```

`cloudflared` is optional and disabled by default. Set
`ENABLE_CLOUDFLARED=true` only when you need Cloudflare Tunnel. Quick Tunnel is
a separate opt-in: set `ENABLE_CLOUDFLARE_QUICK_TUNNEL=true` and leave
`CLOUDFLARE_TUNNEL_TOKEN` empty. If you serve files through manually configured
nginx/direct HTTPS, keep both flags `false` and use `DIRECT_BASE_URL` or
`RELAY_BASE_URLS`.
