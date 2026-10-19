#!/usr/bin/env bash
# NovaBot setup: installs Docker if needed, asks for your bot's settings, writes .env and starts the bot.
# Safe to re-run: current values are offered as defaults, so it also works for changing settings.
#
#   git clone https://github.com/Mahdi-zarei/novabot.git && cd novabot && ./setup.sh
# or, without cloning first (clones into ~/novabot, or $NOVABOT_DIR):
#   curl -fsSL https://raw.githubusercontent.com/Mahdi-zarei/novabot/main/setup.sh | bash
set -euo pipefail

REPO_URL=${NOVABOT_REPO:-https://github.com/Mahdi-zarei/novabot.git}
INSTALL_DIR=${NOVABOT_DIR:-$HOME/novabot}
PREBUILT_BOT_API=aiogram/telegram-bot-api:latest
OVERRIDE_MARK="# Written by setup.sh"
MIN_COMPOSE=2.24.0

if [[ -t 1 ]]; then
  B=$'\e[1m' G=$'\e[32m' Y=$'\e[33m' R=$'\e[31m' N=$'\e[0m'
else
  B='' G='' Y='' R='' N=''
fi
say() { printf '%s\n' "$*"; }
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok() { printf '%s✔ %s%s\n' "$G" "$*" "$N"; }
warn() { printf '%s! %s%s\n' "$Y" "$*" "$N" >&2; }
die() { printf '%sError: %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

# ------------------------------------------------------------------------------ prompts (read fd 3: the terminal)

# ask VAR "Question" [default] [default as shown]
ask() {
  local _reply _shown=${4-${3-}}
  printf '%s%s: ' "$2" "${_shown:+ [$_shown]}" >&2
  read -r -u 3 _reply || die "input closed"
  printf -v "$1" '%s' "${_reply:-${3-}}"
}

# confirm "Question" [y|n default]
confirm() {
  local reply def=${2:-y}
  printf '%s [%s]: ' "$1" "$([[ $def == y ]] && echo Y/n || echo y/N)" >&2
  read -r -u 3 reply || die "input closed"
  [[ ${reply:-$def} == [Yy]* ]]
}

# choose VAR default "option" ...: VAR gets the 1-based number of the picked option
choose() {
  local _var=$1 _def=$2 _i _pick
  shift 2
  for _i in $(seq $#); do say "  $_i) ${!_i}"; done
  while :; do
    ask _pick "Choose" "$_def"
    if [[ $_pick =~ ^[0-9]+$ ]] && ((_pick >= 1 && _pick <= $#)); then
      printf -v "$_var" '%s' "$_pick"
      return
    fi
    warn "enter a number from 1 to $#"
  done
}

mask() {
  local v=$1
  if ((${#v} > 10)); then printf '%s…%s' "${v:0:4}" "${v: -4}"; else printf '%s' "${v:+****}"; fi
}

# ------------------------------------------------------------------------------ system helpers

pkg_install() {
  if command -v apt-get >/dev/null; then
    "${SUDO[@]}" apt-get update -qq && "${SUDO[@]}" apt-get install -y -qq "$@"
  elif command -v dnf >/dev/null; then
    "${SUDO[@]}" dnf install -y -q "$@"
  elif command -v yum >/dev/null; then
    "${SUDO[@]}" yum install -y -q "$@"
  else
    die "install $* and run this again"
  fi
}

need_cmd() {
  local c missing=()
  for c; do command -v "$c" >/dev/null || missing+=("$c"); done
  if ((${#missing[@]})); then
    say "Installing ${missing[*]}"
    pkg_install "${missing[@]}" </dev/null
  fi
}

in_docker_group() { [[ $EUID -eq 0 ]] || id -nG "$(id -un)" | grep -qw docker; }

dc() { "${DOCKER[@]}" compose "$@"; }

# ------------------------------------------------------------------------------ .env helpers

get_env() {
  local v
  v=$(sed -n "s/^$1=//p" .env | tail -n 1)
  v=${v%$'\r'}
  if ((${#v} >= 2)) && [[ $v == \'*\' || $v == \"*\" ]]; then v=${v:1:-1}; fi
  printf '%s' "$v"
}

# Replaces KEY=..., else the first "# KEY=..." example line, else appends; later duplicates are dropped.
set_env() {
  local tmp value=$2 safe='^[A-Za-z0-9_.,:/@+=%-]*$'
  [[ $value =~ $safe ]] || value="'$value'"
  tmp=$(mktemp)
  K=$1 V=$value awk '
    BEGIN { k = ENVIRON["K"] "="; v = ENVIRON["V"] }
    FNR == NR { if (index($0, k) == 1) has = 1; next }
    !done && (index($0, k) == 1 || (!has && index($0, "# " k) == 1)) { print k v; done = 1; next }
    index($0, k) == 1 { next }
    { print }
    END { if (!done) print k v }
  ' .env .env >"$tmp"
  cat "$tmp" >.env
  rm -f "$tmp"
}

# Cloud Bot API call; the token stays off the command line.
tg() {
  curl -sS --max-time 20 -K - <<<"url = \"https://api.telegram.org/bot$token/$1\"" 2>/dev/null || true
}

json_str() { sed -n "s/.*\"$1\":\"\([^\"]*\)\".*/\1/p" <<<"$2" | head -n 1; }

# Exit IP through a proxy URL (empty: direct), from Cloudflare's trace page.
exit_ip() {
  local trace
  trace=$(curl -sS --max-time 15 -K - <<<"${1:+proxy = \"$1\"}
url = \"https://www.cloudflare.com/cdn-cgi/trace\"" 2>/dev/null) || return 1
  sed -n 's/^ip=//p' <<<"$trace"
}

# ------------------------------------------------------------------------------ steps

get_code() {
  local src=${BASH_SOURCE[0]:-}
  if [[ -n $src && -f $(dirname -- "$src")/compose.yaml ]]; then
    cd -- "$(dirname -- "$src")"
    return
  fi
  # Started through curl | bash: fetch the repo and continue with its copy of this script.
  need_cmd git curl
  if [[ -d $INSTALL_DIR/.git ]]; then
    say "Updating $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only
  elif [[ -e $INSTALL_DIR ]]; then
    die "$INSTALL_DIR exists but isn't a NovaBot clone; set NOVABOT_DIR to another directory"
  else
    say "Cloning NovaBot into $INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
  fi
  exec bash "$INSTALL_DIR/setup.sh" </dev/tty
}

setup_docker() {
  step "Docker"
  need_cmd curl
  if ! command -v docker >/dev/null; then
    confirm "Docker isn't installed. Install it now with Docker's official script (get.docker.com)?" ||
      die "install Docker Engine with the Compose plugin, then run this again"
    curl -fsSL https://get.docker.com | "${SUDO[@]}" sh
  fi
  if ! "${SUDO[@]}" docker info >/dev/null 2>&1 && command -v systemctl >/dev/null; then
    "${SUDO[@]}" systemctl enable --now docker >/dev/null 2>&1 || true
  fi
  if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
  elif "${SUDO[@]}" docker info >/dev/null 2>&1; then
    DOCKER=("${SUDO[@]}" docker)
    if ! in_docker_group &&
      confirm "Add $(id -un) to the docker group, so docker works without sudo (from your next login)?"; then
      "${SUDO[@]}" usermod -aG docker "$(id -un)"
    fi
  else
    die "can't reach the Docker daemon; start it (sudo systemctl start docker) and run this again"
  fi

  local version
  version=$(dc version --short 2>/dev/null) || die "the Docker Compose plugin is missing (package docker-compose-plugin)"
  version=${version#v}
  printf '%s\n' "$MIN_COMPOSE" "$version" | sort -V -C ||
    die "Docker Compose $version is too old, $MIN_COMPOSE or newer is needed: update Docker"
  ok "Docker $("${DOCKER[@]}" version --format '{{.Server.Version}}' 2>/dev/null), Compose $version"
}

ask_token() {
  say "Create a bot with @BotFather (https://t.me/BotFather, /newbot) and paste its token."
  local old me
  old=$(get_env BOT_TOKEN)
  while :; do
    ask token "Bot token" "$old" "$(mask "$old")"
    if [[ ! $token =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
      warn "that doesn't look like a bot token (123456789:AAE...)"
      continue
    fi
    # Only a new token is checked: one already moved to the local server must stay off the cloud API.
    [[ $token == "$old" ]] && break
    new_token=1
    me=$(tg getMe)
    if [[ $me == *'"ok":true'* ]]; then
      bot_username=$(json_str username "$me")
      ok "token belongs to @$bot_username"
      break
    elif [[ -n $me ]]; then
      warn "Telegram rejected this token: $(json_str description "$me")"
    else
      warn "couldn't reach api.telegram.org to check the token; keeping it unchecked"
      break
    fi
  done
  set_env BOT_TOKEN "$token"
}

ask_admin() {
  say
  say "Your numeric Telegram user id makes you the bot's admin (error reports, /admin commands)."
  local old admin=''
  old=$(get_env ADMIN_ID)
  if [[ -n $old ]]; then
    ask admin "Admin user id" "$old"
  elif [[ -n $bot_username ]]; then
    ask admin "Admin user id (Enter to detect it)" ""
    if [[ -z $admin ]]; then
      say "Open https://t.me/$bot_username in Telegram, send the bot any message, then press Enter here."
      read -r -u 3 _ || true
      admin=$(grep -o '"from":{"id":[0-9]*' <<<"$(tg getUpdates)" | tail -n 1 | grep -o '[0-9]*$' || true)
      if [[ -z $admin ]]; then
        warn "no message found"
      elif ! confirm "Found user id $admin. Use it?"; then
        admin=''
      fi
    fi
  fi
  until [[ $admin =~ ^[0-9]+$ ]]; do
    [[ -n $admin ]] && warn "a user id is a number"
    ask admin "Admin user id (@userinfobot tells you yours)" ""
  done
  set_env ADMIN_ID "$admin"
}

ask_api_keys() {
  say
  say "The local Bot API server (it lets the bot send files up to 2 GB) needs an api_id and api_hash:"
  say "log in at https://my.telegram.org, open 'API development tools' and create an app (any name)."
  local old_id old_hash api_id='' api_hash=''
  old_id=$(get_env TELEGRAM_API_ID)
  old_hash=$(get_env TELEGRAM_API_HASH)
  until [[ $api_id =~ ^[0-9]+$ ]]; do
    [[ -n $api_id ]] && warn "api_id is a number"
    ask api_id "api_id" "$old_id"
  done
  until [[ $api_hash =~ ^[0-9a-f]{32}$ ]]; do
    [[ -n $api_hash ]] && warn "api_hash is 32 hex characters"
    ask api_hash "api_hash" "$old_hash" "$(mask "$old_hash")"
  done
  set_env TELEGRAM_API_ID "$api_id"
  set_env TELEGRAM_API_HASH "$api_hash"
}

ask_access() {
  say
  say "Who may use the bot? Bots show up in Telegram search, so 'anyone' really means anyone."
  local old allowed
  old=$(get_env ALLOWED_USERS)
  while :; do
    ask allowed "Allowed user ids, comma-separated, or 'anyone' (you're always allowed)" "${old:-anyone}"
    [[ $allowed == anyone ]] && allowed=''
    allowed=${allowed// /}
    [[ $allowed =~ ^([0-9]+(,[0-9]+)*)?$ ]] && break
    warn "use numeric ids separated by commas, e.g. 11111111,22222222"
  done
  set_env ALLOWED_USERS "$allowed"
}

ask_egress() {
  say
  say "YouTube often blocks server IPs (\"Sign in to confirm you're not a bot\")."
  say "How should YouTube (and Spotify) downloads leave this server?"
  local old egress pick def ip
  old=$(get_env EGRESS_YOUTUBE)
  case ${old:-$( ((fresh)) && echo warp || echo direct)} in
    warp) def=1 ;;
    direct) def=2 ;;
    *) def=3 ;;
  esac
  choose pick "$def" \
    "Cloudflare WARP, built in (free; registers an anonymous WARP account under Cloudflare's terms)" \
    "Direct, from this server's own IP" \
    "A proxy you run (SOCKS5 or HTTP URL)"
  case $pick in
    1) egress=warp ;;
    2) egress=direct ;;
    3)
      [[ $old == *://* ]] || old=''
      while :; do
        ask egress "Proxy URL, e.g. socks5h://127.0.0.1:1080 or socks5h://user:pass@host:port" "$old"
        if [[ ! $egress =~ ^(socks5h?|socks4a?|https?):// || $egress == *[[:space:]\'\"]* ]]; then
          warn "use a socks5h://, socks5://, socks4://, http:// or https:// URL"
          continue
        fi
        if ip=$(exit_ip "$egress") && [[ -n $ip ]]; then
          ok "proxy works, exit IP $ip"
          break
        fi
        confirm "That proxy didn't answer from here. Use it anyway?" n && break
      done
      ;;
  esac
  set_env EGRESS_YOUTUBE "$egress"

  # The warp service runs (COMPOSE_PROFILES) while any source uses it.
  local key p old_profiles profiles=() listed=()
  needs_warp=0
  for key in EGRESS_YOUTUBE EGRESS_INSTAGRAM EGRESS_SOUNDCLOUD EGRESS_GENERIC; do
    if [[ $(get_env "$key") == warp ]]; then needs_warp=1; fi
  done
  old_profiles=$(get_env COMPOSE_PROFILES)
  IFS=, read -ra listed <<<"$old_profiles"
  for p in "${listed[@]}"; do
    p=${p// /}
    if [[ -n $p && $p != warp ]]; then profiles+=("$p"); fi
  done
  if ((needs_warp)); then profiles+=(warp); fi
  p=$(IFS=,; echo "${profiles[*]}")
  if [[ $p != "$old_profiles" ]]; then set_env COMPOSE_PROFILES "$p"; fi
}

configure() {
  step "Settings"
  if [[ ! -f .env ]]; then
    cp .env.example .env
    fresh=1
  else
    say "Found .env; press Enter to keep a current value."
  fi
  chmod 600 .env
  ask_token
  ask_admin
  ask_api_keys
  ask_access
  ask_egress
}

choose_bot_api() {
  step "Telegram Bot API server"
  if [[ -f compose.override.yaml ]] && ! grep -qF "$OVERRIDE_MARK" compose.override.yaml; then
    say "compose.override.yaml isn't one setup.sh wrote; leaving it as it is."
    grep -q "image:" compose.override.yaml || from_source=1
  else
    local pick def=1
    if ((!fresh)) && [[ ! -f compose.override.yaml ]]; then def=2; fi
    choose pick "$def" \
      "Prebuilt image $PREBUILT_BOT_API (ready in a minute)" \
      "Build it from source (30+ minutes, about 4 GB of RAM or swap)"
    if [[ $pick == 1 ]]; then
      cat >compose.override.yaml <<EOF
$OVERRIDE_MARK: the prebuilt Bot API server instead of building telegram-bot-api/.
# Delete this file (or re-run setup.sh) to build it from source.
services:
  telegram-bot-api:
    build: !reset null
    image: $PREBUILT_BOT_API
    environment:
      TELEGRAM_LOCAL: "1"
      TELEGRAM_HTTP_IP_ADDRESS: "127.0.0.1"   # loopback only; host networking would expose 0.0.0.0
    # This image can't bind its stats page to loopback, so the healthcheck asks the API port instead
    # (any HTTP answer, even a 404, means the server is up).
    healthcheck:
      test: ["CMD-SHELL", "wget -S -O /dev/null http://127.0.0.1:8081/ 2>&1 | grep -q 'HTTP/'"]
EOF
    else
      rm -f compose.override.yaml
      from_source=1
    fi
  fi

  if ((from_source)); then
    local mem_mb
    mem_mb=$(awk '/^(MemTotal|SwapTotal):/ { kb += $2 } END { print int(kb / 1024) }' /proc/meminfo)
    if ((mem_mb < 7000)); then build_jobs=1; fi
    if ((mem_mb < 3500)); then
      warn "only ${mem_mb} MB of RAM + swap: the compile may run out of memory; add swap (see README.md)"
    fi
  fi
}

prepare_host() {
  mkdir -p data state tg-bot-api secrets warp-data
  chmod 755 data
  chmod 700 secrets

  # A first start fails on a taken port; check while none of our containers holds it.
  [[ -z $(dc ps -q 2>/dev/null) ]] && command -v ss >/dev/null || return 0
  local port ports=(8081 8082) metrics warp_port
  metrics=$(get_env METRICS_PORT)
  warp_port=$(get_env WARP_PORT)
  if [[ ${metrics:-9091} != 0 ]]; then ports+=("${metrics:-9091}"); fi
  if ((needs_warp)); then ports+=("${warp_port:-1081}"); fi
  for port in "${ports[@]}"; do
    if [[ -n $(ss -Hltn "sport = :$port" 2>/dev/null) ]]; then
      warn "port $port is already in use on this host (see Troubleshooting in README.md)"
    fi
  done
}

start() {
  if ((new_token)); then
    step "Moving the bot to the local Bot API server"
    say "Logging the bot out of Telegram's cloud Bot API once, so the local server gets its updates"
    say "(it can't log back in to api.telegram.org for 10 minutes)."
    local out
    out=$(tg logOut)
    if [[ $out == *'"ok":true'* ]]; then
      ok "logged out of the cloud Bot API"
    else
      warn "logOut didn't succeed (${out:-no answer}); if the bot gets no messages, see README.md"
    fi
  fi

  step "Building and starting (the first build takes a few minutes)"
  local build=(bot) logs ip='' warp_port _
  if ((needs_warp)); then build+=(warp); fi
  dc build "${build[@]}"
  # Builds telegram-bot-api separately so JOBS stays the same across runs and the compile stays cached.
  if ((from_source)); then
    dc build --build-arg JOBS="$build_jobs" telegram-bot-api
  else
    dc pull telegram-bot-api
  fi
  dc up -d --remove-orphans
  if ((!needs_warp)); then dc --profile warp rm -sf warp >/dev/null 2>&1 || true; fi

  say "Waiting for the bot to come up…"
  for _ in $(seq 60); do
    logs=$(dc logs --no-color bot 2>&1 || true)
    running=$(grep -o 'running as @[A-Za-z0-9_]*' <<<"$logs" | tail -n 1 | cut -d@ -f2 || true)
    [[ -n $running ]] && break
    sleep 2
  done
  if [[ -n $running ]]; then
    ok "running as @$running"
  else
    warn "the bot didn't report in yet; check: docker compose logs bot telegram-bot-api"
  fi

  if ((needs_warp)); then
    warp_port=$(get_env WARP_PORT)
    for _ in $(seq 15); do
      ip=$(exit_ip "socks5h://127.0.0.1:${warp_port:-1081}" || true)
      [[ -n $ip ]] && break
      sleep 2
    done
    if [[ -n $ip ]]; then ok "WARP is up, exit IP $ip"; else warn "WARP isn't answering yet; check: docker compose logs warp"; fi
  fi
}

schedule_updates() {
  step "Nightly yt-dlp refresh"
  local current line
  line="0 4 * * * /usr/bin/env bash '$PWD/scripts/update.sh' >> \"\$HOME/novabot-update.log\" 2>&1"
  if ! command -v crontab >/dev/null; then
    say "No crontab here; see 'Updating' in README.md to schedule scripts/update.sh."
    return
  fi
  current=$(crontab -l 2>/dev/null || true)
  if [[ $current == *"$PWD/scripts/update.sh"* ]]; then
    ok "already in your crontab"
  elif ! in_docker_group; then
    say "Skipped: cron needs docker access without sudo (the docker group). Re-run setup.sh after joining it."
  elif confirm "Sites change often. Rebuild the bot with the latest yt-dlp every night at 04:00 (cron)?"; then
    printf '%s\n' "$current" "$line" | grep . | crontab -
    ok "scheduled; log in ~/novabot-update.log"
  fi
}

finish() {
  step "Done"
  if [[ -n $running ]]; then
    say "Open ${B}https://t.me/$running${N} and send /start. As the admin, /admin lists your commands."
  fi
  local f missing=''
  for f in youtube-cookies.txt instagram-cookies.txt; do
    [[ -f secrets/$f ]] || missing+=" $f"
  done
  if [[ -n $missing ]]; then
    say "Optional:$missing in secrets/ (burner accounts only, see secrets/README.md)."
    say "Instagram photo posts and carousels need instagram-cookies.txt."
  fi
  say "Run /selftest in the bot to check every source. Logs: docker compose logs -f bot"
  say "Change settings: re-run ./setup.sh, or edit .env and run: docker compose up -d"
}

main() {
  [[ $(uname -s) == Linux ]] || die "NovaBot runs on a Linux server (Docker with host networking)."
  # Prompts read the terminal, so they work under curl | bash too.
  (: </dev/tty) 2>/dev/null || die "setup.sh asks questions; run it from a terminal."
  exec 3</dev/tty

  if [[ $EUID -ne 0 ]] && command -v sudo >/dev/null; then SUDO=(sudo); else SUDO=(); fi
  DOCKER=(docker)
  fresh=0 new_token=0 needs_warp=0 from_source=0 build_jobs=2
  token='' bot_username='' running=''

  get_code
  say "${B}NovaBot setup${N} in $PWD"
  setup_docker
  configure
  choose_bot_api
  prepare_host
  start
  schedule_updates
  finish
}

main "$@"
