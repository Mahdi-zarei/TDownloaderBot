#!/bin/sh
# Registers a free Cloudflare WARP account once (kept in /var/lib/wgcf) and serves it as SOCKS5.
# SOCKS_ADDR=<ip:port> is where the proxy listens; loopback by default because the container runs
# with host networking and an open proxy on a public IP would be abused.
# Optional WG_RESOLVE_STRATEGY=ipv4|ipv6|auto picks the address family for proxied hostnames.
set -eu

STATE_DIR=/var/lib/wgcf
PROXY_CONF=/etc/wireproxy/wireproxy.conf
SOCKS_ADDR=${SOCKS_ADDR:-127.0.0.1:1081}

mkdir -p "$STATE_DIR"
cd "$STATE_DIR"
if [ ! -s wgcf-account.toml ]; then
  echo "registering a new WARP account in $STATE_DIR"
  wgcf register --accept-tos
fi
if [ ! -s wgcf-profile.conf ]; then
  wgcf generate
fi

mkdir -p "$(dirname "$PROXY_CONF")"
{
  echo "WGConfig = $STATE_DIR/wgcf-profile.conf"
  echo
  echo "[Socks5]"
  echo "BindAddress = $SOCKS_ADDR"
  if [ -n "${WG_RESOLVE_STRATEGY:-}" ]; then
    echo
    echo "[Resolve]"
    echo "ResolveStrategy = $WG_RESOLVE_STRATEGY"
  fi
} > "$PROXY_CONF"

echo "wireproxy: WARP -> socks5 on $SOCKS_ADDR"
exec wireproxy -c "$PROXY_CONF"
