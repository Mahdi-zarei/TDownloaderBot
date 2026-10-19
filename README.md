# NovaBot

A self-hosted Telegram bot that downloads media. Send it a YouTube, Instagram, SoundCloud or Spotify link
(or anything else [yt-dlp](https://github.com/yt-dlp/yt-dlp) supports) and it sends the file back: video
in the quality you pick, or audio with tags and cover art.

- Files up to 2 GB, through a local Telegram Bot API server.
- Quality picker with file sizes; audio as tagged M4A, with an editor for your own tags and cover.
- Instagram reels, photo posts and carousels; Spotify tracks matched to the same song on YouTube Music.
- Spotify albums and playlists: pick the tracks you want, or download them all.
- Repeat requests are answered instantly from a cache.
- Per-source egress: direct, built-in Cloudflare WARP, or your own SOCKS5/HTTP proxy.
- Admin tools (stats, users, bans, broadcast, failure log, `/selftest`) that don't show the admin what
  people download.

## Quick start

You need a Linux server with a public internet connection. From Telegram you need:

- a bot token from [@BotFather](https://t.me/BotFather) (`/newbot`)
- an `api_id` and `api_hash` from <https://my.telegram.org> (API development tools)

Then run on the server:

```bash
curl -fsSL https://raw.githubusercontent.com/Mahdi-zarei/novabot/main/setup.sh | bash
```

or from a clone:

```bash
git clone https://github.com/Mahdi-zarei/novabot.git && cd novabot && ./setup.sh
```

The script installs Docker if it's missing. It then asks for the token, the API keys, your user id (it can
detect it), who may use the bot and how YouTube traffic leaves the server. It writes `.env`, starts
everything and offers a nightly yt-dlp refresh. Run it again at any time to change settings: press Enter to
keep a value.

Then send `/start` to your bot. As the admin, `/admin` lists your commands and `/selftest` checks every source.

## How it runs

Docker Compose runs it. Every service uses host networking and listens on loopback only. The bot uses long
polling, so nothing is reachable from outside.

| Service | What | Listens on |
| --- | --- | --- |
| `bot` | Python 3.13, aiogram 3, yt-dlp (+ Deno), gallery-dl, ffmpeg | `127.0.0.1:9091` metrics |
| `telegram-bot-api` | Telegram's Bot API server in `--local` mode | `127.0.0.1:8081`, `:8082` stats |
| `warp` (optional) | Cloudflare WARP as a SOCKS5 proxy (wgcf + wireproxy) | `127.0.0.1:1081` |

All settings live in `.env`. [`.env.example`](.env.example) lists every setting with its default. After
editing `.env`, run `docker compose up -d`.

## Egress

YouTube often blocks datacenter IPs ("Sign in to confirm you're not a bot"). Each source chooses its own
way out:

```sh
EGRESS_YOUTUBE=direct                          # the server's own IP (default)
EGRESS_YOUTUBE=warp                            # built-in Cloudflare WARP, also set COMPOSE_PROFILES=warp
EGRESS_YOUTUBE=socks5h://127.0.0.1:1080        # a proxy you run: socks5h://, socks5:// or http://
EGRESS_YOUTUBE=socks5h://user:pass@host:port
```

- `EGRESS_INSTAGRAM`, `EGRESS_SOUNDCLOUD` and `EGRESS_GENERIC` work the same way. Spotify follows YouTube.
- The bot uses host networking, so `127.0.0.1` is the host itself. Prefer `socks5h://`: it resolves DNS
  through the proxy too.
- On its first start, WARP registers a free anonymous account and keeps it in `warp-data/`. To get a
  new one, delete that directory. `WARP_PORT` moves its port.
- `/selftest` runs every source through `direct` and through each egress in use, and shows each exit IP.
- Logs, stats and alerts mask proxy credentials.

## Cookies

Put optional cookies from **burner** accounts in `secrets/`, as described in
[secrets/README.md](secrets/README.md). Instagram photo posts and carousels need them. YouTube retries with
them after a bot check.

## Access and privacy

- `ALLOWED_USERS` empty: anyone can use the bot, and bots can be found in Telegram search. Otherwise list
  comma-separated user ids. The admin (`ADMIN_ID`) is always allowed.
- `/ban` and `/unban` take effect immediately.
- The admin doesn't see what people download. A link is stored only when its request fails, and only
  `/link_<id>` shows it. Logs and alerts mask links and media ids. What is kept: the cache, keyed by media
  id, and per-user counts.

## Commands

Users send links (up to 5 per message) and have `/start`, `/help` and `/cancel`. Only `ADMIN_ID` can use
these:

| Command | What it does |
| --- | --- |
| `/stats [today\|7d\|30d\|all]` | Users, requests and success rate, data sent, cache size, failures by reason and egress |
| `/users`, `/user <id or @name>` | Everyone who used the bot; one user's history, with Ban and Cancel buttons |
| `/jobs` | Running and queued downloads, each with a cancel button |
| `/failures [n]`, `/link <id>` | Recent failures; the link of one of them, shown only when you ask |
| `/uncache <link>` | Forgets the cached files of a link, for example after a wrong Spotify match |
| `/broadcast` | Reply to a message (or add text) to send it to every user, after a confirm button |
| `/selftest` | Every source through every egress in use, with tool versions, exit IPs and cookie status |
| `/ban`, `/unban <id or @name>` | Blocks or unblocks a user and cancels their downloads |

**Spotify:** Spotify audio is DRM-protected. The bot reads the track's metadata from the public page, finds the
same song on YouTube Music (matching title and duration) and downloads it through YouTube's egress. It then
tags the file with Spotify's metadata and cover, at YouTube's audio quality (AAC, about 128 kbps). The caption
links the matched upload.

For an album or playlist link the bot lists its tracks (from Spotify's public embed player, which shows at most
the first 100). Tap tracks to pick them, or download all. The tracks then arrive one by one, each as its own
download (cached, counted and limited like any other), with a summary of what could not be matched at the end.
One list per user downloads at a time, and `/cancel` stops it. Private playlists, Liked Songs, artists and
podcasts aren't supported.

## Updating

```bash
git pull && docker compose up -d --build bot
```

If you use WARP, add `warp` to that command. [`scripts/update.sh`](scripts/update.sh) rebuilds the bot
with the latest yt-dlp and gallery-dl, then restarts it. `setup.sh` can schedule it nightly. Base images
refresh only with `docker compose build --pull`.

<details>
<summary><b>Manual setup</b> (without setup.sh)</summary>

1. Install Docker Engine with the Compose plugin, version 2.24 or later.
2. Configure:
   ```bash
   cp .env.example .env && chmod 600 .env   # set BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH, ADMIN_ID
   mkdir -p data state tg-bot-api secrets warp-data
   ```
3. Log the bot out of Telegram's cloud Bot API, once per token. A local server can't be sure to get the
   bot's updates otherwise. Afterwards the bot can't log back in to the cloud for 10 minutes.
   ```bash
   curl "https://api.telegram.org/bot<token>/logOut"
   ```
4. Start: `docker compose up -d --build`.

The first build compiles the Bot API server, which takes 30+ minutes and about 4 GB of RAM per compile job.
On a small server, use `docker compose build --build-arg JOBS=1 telegram-bot-api` or add swap
(`sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`).
To skip the compile, use the prebuilt [aiogram/telegram-bot-api](https://hub.docker.com/r/aiogram/telegram-bot-api)
image with a `compose.override.yaml`; `setup.sh` writes that file when you pick the prebuilt image.

</details>

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Bot logs `Unauthorized` at start | Wrong token, or the bot wasn't logged out of the cloud API (see manual setup, step 3). A wrong `api_id` or `api_hash` shows in `docker compose logs telegram-bot-api`. |
| "Sign in to confirm you're not a bot" | Refresh `secrets/youtube-cookies.txt`, or move `EGRESS_YOUTUBE` to another egress and compare them with `/selftest`. |
| Downloads through a proxy fail | Test the proxy from the host: `curl -x socks5h://127.0.0.1:1080 https://www.cloudflare.com/cdn-cgi/trace`. Check the scheme and the credentials. |
| `/selftest` shows `warp: error` | `.env` needs `COMPOSE_PROFILES=warp`, then run `docker compose up -d`. See `docker compose logs warp`. To re-register: `docker compose stop warp && sudo rm -rf warp-data/* && docker compose up -d`. |
| Bot API build dies with `Killed signal terminated program cc1plus` | Out of memory: use `JOBS=1`, add swap, or switch to the prebuilt image. |
| "address already in use" | Something else holds 8081, 8082, 9091 or 1081. Find it with `sudo ss -ltnp`, then move it, or change `METRICS_PORT` or `WARP_PORT`. |
| Uploads fail with "file not found" | The Bot API server (uid 101) can't read `data/`: `chmod 755 data`. |

## Data and backup

| Path | Content | Back up? |
| --- | --- | --- |
| `.env`, `secrets/` | Token, API keys, cookies | Yes, and keep it private |
| `state/bot.db` | SQLite: cache, users and bans, job history | Yes |
| `warp-data/` | WARP account | Optional: a new one is registered if it's lost |
| `tg-bot-api/`, `data/` | Bot API server state, temporary job files | No |

To make a consistent backup while the bot runs:
`docker compose exec bot python -c "import sqlite3; sqlite3.connect('/state/bot.db').backup(sqlite3.connect('/state/bot-backup.db'))"`.
Cached files belong to the bot token: with a new token, the cache fills up again from scratch.

Prometheus metrics (jobs, failures, bytes sent, cache hits, queue) are at `curl -s 127.0.0.1:9091/metrics`.
That port is loopback only. If your scraper runs elsewhere, set `METRICS_HOST`, but never to `0.0.0.0` on a
public server.
