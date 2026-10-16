#!/bin/sh
# WG_CONFIG=<path>: use that WireGuard config (Windscribe or any other provider).
# Otherwise: register a Cloudflare WARP account once and keep it in /var/lib/wgcf.
# SOCKS_ADDR=<ip:port> is where the SOCKS5 proxy listens; loopback by default because the
# container runs with host networking and an open proxy on a public IP would be abused.
# Optional WG_RESOLVE_STRATEGY=ipv4|ipv6|auto picks the address family for proxied hostnames.
set -eu

STATE_DIR=/var/lib/wgcf
PROXY_CONF=/etc/wireproxy/wireproxy.conf
SOCKS_ADDR=${SOCKS_ADDR:-127.0.0.1:1080}

if [ -n "${WG_CONFIG:-}" ]; then
  if [ ! -r "$WG_CONFIG" ]; then
    echo "WG_CONFIG=$WG_CONFIG does not exist or is not readable" >&2
    exit 1
  fi
  wg_conf=$WG_CONFIG
else
  mkdir -p "$STATE_DIR"
  cd "$STATE_DIR"
  if [ ! -s wgcf-account.toml ]; then
    echo "registering a new WARP account in $STATE_DIR"
    wgcf register --accept-tos
  fi
  if [ ! -s wgcf-profile.conf ]; then
    wgcf generate
  fi
  wg_conf=$STATE_DIR/wgcf-profile.conf
fi

mkdir -p "$(dirname "$PROXY_CONF")"
{
  echo "WGConfig = $wg_conf"
  echo
  echo "[Socks5]"
  echo "BindAddress = $SOCKS_ADDR"
  if [ -n "${WG_RESOLVE_STRATEGY:-}" ]; then
    echo
    echo "[Resolve]"
    echo "ResolveStrategy = $WG_RESOLVE_STRATEGY"
  fi
} > "$PROXY_CONF"

echo "wireproxy: $wg_conf -> socks5 on $SOCKS_ADDR"
exec wireproxy -c "$PROXY_CONF"
