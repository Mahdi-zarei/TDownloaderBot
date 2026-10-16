# NovaBot: Telegram media downloader

Send the bot a YouTube, Instagram, SoundCloud or Spotify track link (or anything else yt-dlp supports) and
it sends the file back: video in the quality you pick, or audio with tags and cover. Repeat requests come
from a `file_id` cache instantly.

Two containers, both with `network_mode: host`. They talk over `127.0.0.1` and every listener is bound
to loopback, so nothing is reachable from outside (the bot uses long polling). Keep these host ports free:

| Service | What | Listens on (loopback) |
| --- | --- | --- |
| `bot` | Python 3.13 + aiogram 3, yt-dlp (+ yt-dlp-ejs, Deno), gallery-dl, ffmpeg | `127.0.0.1:9091` metrics |
| `telegram-bot-api` | Telegram's Bot API server in `--local` mode (uploads up to 2000 MB, by local path) | `127.0.0.1:8081` API, `127.0.0.1:8082` stats |

Outgoing traffic goes direct unless you point a source at a proxy you run yourself (see [Egress](#egress)).

```text
.
├── bot/                    # the Python bot
├── Dockerfile              # bot image
├── requirements.txt
├── compose.yaml
├── .env.example            # every setting, with defaults
├── telegram-bot-api/       # Bot API server, built from source
├── scripts/update.sh       # nightly yt-dlp refresh
├── .gitlab-ci.yml          # scheduled pipeline that runs update.sh over SSH
├── secrets/                # cookies (git-ignored, see secrets/README.md)
├── data/  state/  tg-bot-api/   # created on the server, git-ignored
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
- Optional: a SOCKS5 or HTTP proxy you run, for YouTube if the server's own IP gets blocked.

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

mkdir -p data state tg-bot-api secrets
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
      TELEGRAM_LOCAL: "1"                      # --local
      TELEGRAM_HTTP_IP_ADDRESS: "127.0.0.1"    # loopback only; host networking would expose 0.0.0.0
    # No TELEGRAM_STAT: this image can't bind the stats page to loopback, so check the API port instead
    # (any HTTP answer, even a 404, means the server is up).
    healthcheck:
      test: ["CMD-SHELL", "wget -S -O /dev/null http://127.0.0.1:8081/ 2>&1 | grep -q 'HTTP/'"]
```

`TELEGRAM_API_ID` / `TELEGRAM_API_HASH` already come from `.env` through `compose.yaml`.

**Updating the code:** `git pull && docker compose up -d --build bot`.

## Logs

```sh
docker compose logs -f bot
docker compose logs -f telegram-bot-api
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
- [ ] A Spotify track link arrives as audio tagged with Spotify's title, artist, album and cover; the
      caption links the YouTube upload it was matched to.
- [ ] Send an already-delivered link again: it comes back instantly from cache (marked with the lightning icon).
- [ ] `/selftest` (admin): pass/fail per source and egress.
- [ ] `/stats` (admin): users, jobs, bytes, failures; the period buttons switch between today, 7 days,
      30 days and all time.
- [ ] `/users`, then tap a `/u_<id>` link: the user's history, with Ban and Cancel buttons.
- [ ] Start a long download and press **Cancel**: the job stops and its temp files are removed.

## Egress

The bot doesn't run or manage any proxy. Each source goes `direct` (the server's own IP) unless you set
it to the URL of a proxy you run, in `.env`:

```sh
EGRESS_YOUTUBE=socks5h://127.0.0.1:1080              # a SOCKS5 proxy on this host
EGRESS_YOUTUBE=socks5h://user:pass@host:port         # a remote one, with credentials
EGRESS_YOUTUBE=http://host:port                      # an HTTP proxy
```

`EGRESS_INSTAGRAM`, `EGRESS_SOUNDCLOUD` and `EGRESS_GENERIC` work the same way (all `direct` by default).
Spotify tracks are downloaded from YouTube, so they follow `EGRESS_YOUTUBE`. Use `socks5h://` rather than
`socks5://` so DNS is resolved by the proxy too. The bot uses host networking, so `127.0.0.1` is the host.
Anything that isn't `direct` or a URL stops the bot at startup with an error naming the setting.
Proxy credentials are masked as `***` in logs, the job history, metrics, alerts and `/selftest`.

`/selftest` runs every source through `direct` and through each distinct proxy you configured, and shows
the exit IP of each, so you can see whether the proxy actually helps. After changing `.env`:
`docker compose up -d bot`.

Check a proxy by hand from the host:

```sh
curl -s --proxy socks5h://127.0.0.1:1080 https://www.cloudflare.com/cdn-cgi/trace
```

## Spotify

Spotify audio is DRM-protected, so the bot doesn't download from Spotify. It reads the track's title,
artists, album, year, duration and cover from the public track page (no API key), finds the same song on
YouTube Music (official "Topic" album audio when available, otherwise a regular YouTube search), downloads
that audio through YouTube's egress and concurrency limit, and tags it with Spotify's metadata and cover.

- Single tracks only: albums, playlists, artists and podcasts get a one-line reply.
- Audio quality is YouTube's (AAC ~128 kbps), not Spotify's.
- Matches must agree on duration (within a few seconds) and title; live, cover, remix and similar uploads
  are penalized unless the Spotify title says so. The caption links the matched upload so a wrong match
  is easy to spot.
- `/selftest` ends with a `spotify · match` row showing which search found `SELFTEST_SPOTIFY`: "YouTube
  Music" is the good case; "YouTube" means the YouTube Music search came back empty from the server.

## Access

- `ALLOWED_USERS=` empty: anyone who finds the bot can use it (bot usernames show up in Telegram search).
- `ALLOWED_USERS=11111111,22222222`: allowlist. `ADMIN_ID` is always allowed.
- `/ban <id or @username>` and `/unban …` (admin only) take effect immediately, no restart.

After editing `.env`: `docker compose up -d bot`.

## Admin commands

Only `ADMIN_ID` can use these; for anyone else they're ordinary messages. `/admin` lists them in the chat.

| Command | What it does |
| --- | --- |
| `/stats [today\|7d\|30d\|all]` | Active/new users, requests by outcome and success rate, data sent, cache size, per-source numbers, top users, failure reasons, failures by egress. Buttons switch the period. |
| `/users [recent\|jobs\|bytes]` | Everyone who used the bot, 15 per page: delivered and failed requests, data sent, last seen, banned flag. Buttons change the sort and page. |
| `/user <id or @name>` | One user: first/last seen, requests by outcome, data, sources, live jobs, the last 10 requests (no links). Buttons: Ban/Unban, Cancel their jobs, Refresh. Every list shows a tappable `/u_<id>` shortcut to this. |
| `/jobs` | Running and queued jobs with stage and progress; a cancel button per job. |
| `/failures [n]` | The last n failures (default 10, max 30) with source, egress, reason, user and the site's error line, with links and media ids masked. |
| `/link <id>` | The link of one failed request, only when you ask (tap `/link_<id>` in `/failures`, `/user` or an alert). |
| `/uncache <link>` | Forgets every cached file for that link (all qualities), e.g. after a wrong Spotify match, so the next request downloads fresh. |
| `/broadcast` | Reply to any message with `/broadcast` to send all non-banned users a copy (formatting and media kept), or `/broadcast <text>`. Shows the recipient count and waits for a Send button; reports delivered / blocked / failed at the end. |
| `/selftest`, `/ban`, `/unban` | As above. |

Times are UTC. Usage counts come from the job history in `state/bot.db`, so they survive restarts.

**Privacy.** The admin never sees what users download unless something failed and they ask for it:

- A request's link is written to the database only when it fails; successful, cached and cancelled
  requests keep no link (older rows are cleared on startup).
- Admin views list requests without links. A failed one shows `/link_<id>`, and only that reveals its link.
- Alerts and logs mask links, YouTube searches and media ids as `<link>`, `<search>` and `<id>`, and
  Spotify matching logs no titles or artists.
- Still stored, because the features need them: the `file_id` cache keyed by media id (that's how a
  repeat request is answered instantly), and per-user counts by source and size.

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

The port listens on loopback only. A scraper in a container without host networking can't reach it;
set `METRICS_HOST` to an address that scraper can reach (e.g. the Docker bridge IP `172.17.0.1`), never
`0.0.0.0` on a public server.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Bot logs `Unauthorized` at start | Token typo, or the bot wasn't logged out of the cloud API: run the `logOut` curl above. Check `docker compose logs telegram-bot-api` for a wrong `api_id` / `api_hash`. |
| "Sign in to confirm you're not a bot" | Refresh `secrets/youtube-cookies.txt` from the burner account, then point `EGRESS_YOUTUBE` at a proxy with a cleaner IP and compare with `/selftest`. |
| Every download through a proxy fails | Test it from the host with the `curl --proxy` line under [Egress](#egress); check the URL scheme (`socks5h://`, `http://`) and credentials. |
| Uploads fail with "file not found" | `/data` differs between `bot` and `telegram-bot-api` (both must mount `./data:/data`), or the Bot API server (uid 101) can't read the files: `chmod 755 data`. |
| `telegram-bot-api` build dies with `Killed signal terminated program cc1plus` | Out of memory: `--build-arg JOBS=1`, add swap, or use the prebuilt image. |
| A service logs "address already in use" | Something else on the host holds 8081, 8082 or 9091 (`sudo ss -ltnp`); move it, or change `METRICS_PORT`. |
| Bot waits forever at `up` | `telegram-bot-api` is not healthy: `docker compose logs telegram-bot-api`. |

## Data layout and backup

| Path | Content | Back up? |
| --- | --- | --- |
| `.env`, `secrets/` | Token, API keys, cookies | Yes (keep private) |
| `state/bot.db` | SQLite: `file_id` cache, users and bans, job history | Yes |
| `tg-bot-api/` | Bot API server state and files users sent (owned by uid 101) | No, recreated on login |
| `data/` | Per-job temp files, removed after delivery | No |

Online backup of the database (consistent even while the bot runs; lands in `state/bot-backup.db`):

```sh
docker compose exec bot python -c "import sqlite3; sqlite3.connect('/state/bot.db').backup(sqlite3.connect('/state/bot-backup.db'))"
```

The cache holds Telegram `file_id`s, which belong to the bot token: with a new token from @BotFather
the cache no longer works and fills again from scratch; users and bans carry over.