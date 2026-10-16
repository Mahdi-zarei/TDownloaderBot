# NovaBot: Telegram media downloader

Send the bot a YouTube, Instagram or SoundCloud link (or anything else yt-dlp supports) and it sends the
file back: video in the quality you pick, or audio with tags and cover. Repeat requests come from a
`file_id` cache instantly.

Three containers on one private Docker network, no public ports (the bot uses long polling):

| Service | What | Reachable as |
| --- | --- | --- |
| `bot` | Python 3.13 + aiogram 3, yt-dlp (+ yt-dlp-ejs, Deno), gallery-dl, ffmpeg | metrics on `127.0.0.1:9091` |
| `telegram-bot-api` | Telegram's Bot API server in `--local` mode (uploads up to 2000 MB, by local path) | `http://telegram-bot-api:8081` |
| `warp` | Cloudflare WARP via wgcf + wireproxy, as SOCKS5 (YouTube egress) | `socks5h://warp:1080` |
| `windscribe` (optional profile) | Same image with a Windscribe WireGuard config | `socks5h://windscribe:1080` |

```text
.
├── bot/                    # the Python bot
├── Dockerfile              # bot image
├── requirements.txt
├── compose.yaml
├── .env.example            # every setting, with defaults
├── telegram-bot-api/       # Bot API server, built from source
├── warp/                   # WireGuard -> SOCKS5 egress image
├── scripts/update.sh       # nightly yt-dlp refresh
├── .gitlab-ci.yml          # scheduled pipeline that runs update.sh over SSH
├── secrets/                # cookies, windscribe.conf (git-ignored, see secrets/README.md)
├── data/  state/  tg-bot-api/  warp-data/   # created on the server, git-ignored
```

## Prerequisites

**Docker Engine + Compose plugin** (Ubuntu 22.04 / 24.04 / 26.04, official apt repo):

```sh
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"   # log out and back in afterwards
docker compose version
```

**From Telegram and elsewhere:**

- Bot token from [@BotFather](https://t.me/BotFather).
- `api_id` and `api_hash` from <https://my.telegram.org> (API development tools).
- Your numeric Telegram user id, e.g. from [@userinfobot](https://t.me/userinfobot).
- Cookies of a burner Google account and a burner Instagram account, Netscape format
  (see [secrets/README.md](secrets/README.md)). Never your main accounts.
- Optional: a Windscribe WireGuard config, for a second egress to compare against WARP.

## Setup

```sh
# On the server, e.g. in /opt/novabot (git clone, or rsync from your machine)
sudo mkdir -p /opt/novabot && sudo chown "$USER" /opt/novabot
git clone <your-repo-url> /opt/novabot
#   or from the dev box: rsync -av --exclude .git --exclude .idea ./ user@server:/opt/novabot/
cd /opt/novabot

cp .env.example .env
nano .env                    # BOT_TOKEN, ADMIN_ID, TELEGRAM_API_ID, TELEGRAM_API_HASH
chmod 600 .env

mkdir -p data state tg-bot-api warp-data secrets
# copy youtube-cookies.txt and instagram-cookies.txt into secrets/
chmod 700 secrets && chmod 600 secrets/*
```

### One-time: log the bot out of the cloud Bot API

A bot logged in on `api.telegram.org` is not guaranteed to get its updates on a local server, so log it
out there once before the first start. Afterwards it works on the local server immediately, but cannot
log back in to `api.telegram.org` for 10 minutes.

```sh
curl "https://api.telegram.org/bot$(grep '^BOT_TOKEN=' .env | cut -d= -f2-)/logOut"
# expected: {"ok":true,"result":true}
```

## Start

```sh
docker compose up -d --build
docker compose ps            # telegram-bot-api should turn "healthy", then bot starts
```

The first build compiles the Bot API server (TDLib) from source: 30+ minutes on a small VPS and roughly
3-4 GB of RAM per compile job; later builds reuse the cached layer. The default is `JOBS=2`. On a
small box build with one job, or add swap:

```sh
docker compose build --build-arg JOBS=1 telegram-bot-api
# swap, if needed:
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
```

**Prebuilt alternative.** Skip the compile with the [aiogram/telegram-bot-api](https://hub.docker.com/r/aiogram/telegram-bot-api)
image (same uid 101 and paths). Create `compose.override.yaml` next to `compose.yaml` (needs Compose 2.24+):

```yaml
services:
  telegram-bot-api:
    build: !reset null
    image: aiogram/telegram-bot-api:10.3
    environment:
      TELEGRAM_LOCAL: "1"   # --local
      TELEGRAM_STAT: "1"    # stats page on :8082, used by the healthcheck
```

`TELEGRAM_API_ID` / `TELEGRAM_API_HASH` already come from `.env` through `compose.yaml`.

**Updating the code:** `git pull && docker compose up -d --build bot`.

## Logs

```sh
docker compose logs -f bot
docker compose logs -f telegram-bot-api warp
```

Logs are rotated by Docker (json-file, 10 MB x 3 per container).

## Test checklist

Talk to the bot in a private chat:

- [ ] `/start` answers.
- [ ] A YouTube link: the quality picker shows sizes; pick one, the status message moves through
      queued / downloading % / uploading, and the video arrives with a thumbnail and plays inline.
- [ ] Same link, **Audio**: an M4A arrives in the music player with title, artist and square cover.
- [ ] Same link, **Audio · custom tags**: edit a field (send text), send a photo as cover, press
      **Done**: the file carries your tags and cover.
- [ ] Instagram reel, single-photo post, and a carousel (arrives as an album).
- [ ] A SoundCloud track arrives as audio directly.
- [ ] Send an already-delivered link again: it comes back instantly from cache (marked with the lightning icon).
- [ ] `/selftest` (admin): pass/fail per source and egress.
- [ ] `/stats` (admin): users, jobs, bytes, failures.
- [ ] Start a long download and press **Cancel**: the job stops and its temp files are removed.

## Egress

Each source picks its own egress in `.env`: `direct`, a name from `EGRESS_PROXIES`, or a proxy URL.
The same goes for `EGRESS_INSTAGRAM`, `EGRESS_SOUNDCLOUD` and `EGRESS_GENERIC` (all `direct` by default).

```sh
# pick one
EGRESS_YOUTUBE=warp                                  # default
EGRESS_YOUTUBE=direct                                # the server's own IP
EGRESS_YOUTUBE=windscribe                            # after enabling the profile below
EGRESS_YOUTUBE=socks5h://user:pass@host:port         # any proxy, e.g. residential
```

**Windscribe as a second egress:**

1. Generate a WireGuard config in your Windscribe account (Config Generators, WireGuard) and save it
   as `secrets/windscribe.conf`.
2. In `.env`:
   ```sh
   EGRESS_PROXIES=warp=socks5h://warp:1080,windscribe=socks5h://windscribe:1080
   COMPOSE_PROFILES=windscribe     # so every docker compose command includes it
   ```
3. `docker compose --profile windscribe up -d`

Run `/selftest` to compare `direct`, `warp` and `windscribe` on the same URLs, then set `EGRESS_YOUTUBE`
to the winner and `docker compose up -d bot`.

When VPN IPs get flagged ("Sign in to confirm you're not a bot" on every egress), point
`EGRESS_YOUTUBE` at a residential proxy (`socks5h://user:pass@host:port` or `http://...`), or at a
WireGuard peer you control: put its config in `secrets/` and add another service like `windscribe`
with its own `WG_CONFIG`. Only the setting changes.

Optional for the egress containers: `WG_RESOLVE_STRATEGY=ipv4|ipv6|auto` (environment) picks the address
family for proxied hostnames. WARP defaults to IPv6 because its profile has an IPv6 address.

Check which IP an egress exits from:

```sh
docker compose exec bot curl -s --proxy socks5h://warp:1080 https://www.cloudflare.com/cdn-cgi/trace
```

## Access

- `ALLOWED_USERS=` empty: anyone who finds the bot can use it (bot usernames show up in Telegram search).
- `ALLOWED_USERS=11111111,22222222`: allowlist. `ADMIN_ID` is always allowed.
- `/ban <user_id>` and `/unban <user_id>` (admin only) take effect immediately, no restart.

After editing `.env`: `docker compose up -d bot`.

## Nightly yt-dlp refresh

Sites change and yt-dlp follows within days, so the bot image is rebuilt nightly with the latest yt-dlp,
its matching yt-dlp-ejs, and gallery-dl. Only that layer is rebuilt; the bot restarts (jobs running at
that moment are lost).

**Cron** (as a user in the `docker` group):

```sh
crontab -e
0 4 * * * /usr/bin/bash /opt/novabot/scripts/update.sh >> $HOME/novabot-update.log 2>&1
```

**Or a GitLab scheduled pipeline** (`.gitlab-ci.yml` runs only on schedules):

1. Create a deploy key: `ssh-keygen -t ed25519 -N '' -f novabot-deploy`, append `novabot-deploy.pub` to the
   server user's `~/.ssh/authorized_keys`.
2. CI/CD variables: `SSH_PRIVATE_KEY` (private key text, or a File variable), `DEPLOY_HOST`, `DEPLOY_USER`,
   `DEPLOY_PATH` (`/opt/novabot`), and `SSH_KNOWN_HOSTS` (output of `ssh-keyscan -H <host>`; without it
   the job trusts whatever key it sees first).
3. Build > Pipeline schedules: `0 4 * * *`.

Optional hardening: prefix the key in `authorized_keys` with
`command="cd /opt/novabot && bash scripts/update.sh",no-port-forwarding,no-pty`.

Base images (Python, Debian packages) only refresh with `docker compose build --pull`; run it now and then.

## Metrics

```sh
curl -s 127.0.0.1:9091/metrics
```

Counters: `tgdl_jobs_total{source,status}`, `tgdl_failures_total{source,egress,reason}`,
`tgdl_bytes_sent_total{source}`, `tgdl_cache_hits_total{source}`; gauges `tgdl_jobs_running`,
`tgdl_jobs_queued`. Prometheus or VictoriaMetrics on the same host:

```yaml
scrape_configs:
  - job_name: novabot
    static_configs:
      - targets: ["127.0.0.1:9091"]
```

The port listens on loopback only. A scraper in another container needs host networking, or change the
mapping in `compose.yaml` (for example to the Docker bridge IP `172.17.0.1:9091:9090`).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Bot logs `Unauthorized` at start | Token typo, or the bot wasn't logged out of the cloud API: run the `logOut` curl above. Check `docker compose logs telegram-bot-api` for a wrong `api_id` / `api_hash`. |
| "Sign in to confirm you're not a bot" | Refresh `secrets/youtube-cookies.txt` from the burner account, then try another egress (`/selftest`, `EGRESS_YOUTUBE`). |
| Uploads fail with "file not found" | `/data` differs between `bot` and `telegram-bot-api` (both must mount `./data:/data`), or the Bot API server (uid 101) can't read the files: `chmod 755 data`. |
| `telegram-bot-api` build dies with `Killed signal terminated program cc1plus` | Out of memory: `--build-arg JOBS=1`, add swap, or use the prebuilt image. |
| `warp` stays unhealthy | `docker compose logs warp`. Re-register: `docker compose stop warp && sudo rm -rf warp-data/* && docker compose up -d warp`. |
| `windscribe` restarts in a loop | `secrets/windscribe.conf` missing or not a WireGuard config. |
| Bot waits forever at `up` | `telegram-bot-api` is not healthy: `docker compose logs telegram-bot-api`. |

## Data layout and backup

| Path | Content | Back up? |
| --- | --- | --- |
| `.env`, `secrets/` | Token, API keys, cookies | Yes (keep private) |
| `state/bot.db` | SQLite: `file_id` cache, users and bans, job history | Yes |
| `tg-bot-api/` | Bot API server state and files users sent (owned by uid 101) | No, recreated on login |
| `warp-data/` | WARP account and WireGuard profile | Optional, re-registers if lost |
| `data/` | Per-job temp files, removed after delivery | No |

Online backup of the database (consistent even while the bot runs; lands in `state/bot-backup.db`):

```sh
docker compose exec bot python -c "import sqlite3; sqlite3.connect('/state/bot.db').backup(sqlite3.connect('/state/bot-backup.db'))"
```

The cache holds Telegram `file_id`s, which belong to the bot token: with a new token from @BotFather
the cache no longer works and fills again from scratch; users and bans carry over.
#   n o v a b o t  
 