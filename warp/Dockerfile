# Cloudflare WARP as a SOCKS5 proxy: wgcf (WARP account + WireGuard profile) and wireproxy
# (userspace WireGuard with a SOCKS5 front). No NET_ADMIN, no kernel module.
ARG GO_VERSION=1.27
ARG ALPINE_VERSION=3.24

FROM golang:${GO_VERSION}-alpine AS build
ARG WGCF_VERSION=v2.3.1
ARG WIREPROXY_VERSION=v1.1.3
ENV CGO_ENABLED=0 GOBIN=/out
# wireproxy moved from pufferffish/wireproxy to windtf/wireproxy; its go.mod declares the new path.
RUN go install -trimpath -ldflags="-s -w" "github.com/ViRb3/wgcf/v2@${WGCF_VERSION}" \
 && go install -trimpath -ldflags="-s -w" "github.com/windtf/wireproxy/cmd/wireproxy@${WIREPROXY_VERSION}"

FROM alpine:${ALPINE_VERSION}
RUN apk add --no-cache ca-certificates curl
COPY --from=build /out/wgcf /out/wireproxy /usr/local/bin/
COPY --chmod=755 entrypoint.sh /entrypoint.sh

ENV SOCKS_ADDR=127.0.0.1:1081
HEALTHCHECK --interval=60s --timeout=20s --start-period=60s --retries=3 \
  CMD curl -fsS -o /dev/null --max-time 15 --proxy "socks5h://${SOCKS_ADDR}" https://www.cloudflare.com/cdn-cgi/trace || exit 1
ENTRYPOINT ["/entrypoint.sh"]
