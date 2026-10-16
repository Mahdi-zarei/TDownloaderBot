# Telegram Bot API server (tdlib/telegram-bot-api), built from source as in
# https://tdlib.github.io/telegram-bot-api/build.html (Alpine, Release).
ARG ALPINE_VERSION=3.24

FROM alpine:${ALPINE_VERSION} AS build

RUN apk add --no-cache alpine-sdk linux-headers git zlib-dev openssl-dev gperf cmake

# A branch, tag or commit sha. The clone layer is cached, so pass a new ref
# (or build with --no-cache) to pick up a newer Bot API release.
ARG TELEGRAM_BOT_API_REF=master
WORKDIR /src
RUN git init -q telegram-bot-api \
 && cd telegram-bot-api \
 && git remote add origin https://github.com/tdlib/telegram-bot-api.git \
 && git fetch -q --depth 1 origin "${TELEGRAM_BOT_API_REF}" \
 && git checkout -q --detach FETCH_HEAD \
 && git submodule update -q --init --recursive --depth 1

# Compiling td takes several GB of RAM per job; lower JOBS on small machines.
ARG JOBS=2
RUN mkdir telegram-bot-api/build \
 && cd telegram-bot-api/build \
 && cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX:PATH=/opt/telegram-bot-api .. \
 && cmake --build . --target install -j "${JOBS}" \
 && strip /opt/telegram-bot-api/bin/telegram-bot-api

FROM alpine:${ALPINE_VERSION}

# uid/gid 101 match the aiogram/telegram-bot-api image, so the two are interchangeable on the same volume.
RUN apk add --no-cache libstdc++ openssl zlib \
 && addgroup -S -g 101 telegram-bot-api \
 && adduser -S -D -H -u 101 -G telegram-bot-api -h /var/lib/telegram-bot-api -s /sbin/nologin telegram-bot-api \
 && mkdir -p /var/lib/telegram-bot-api /tmp/telegram-bot-api \
 && chown telegram-bot-api:telegram-bot-api /var/lib/telegram-bot-api /tmp/telegram-bot-api

COPY --from=build /opt/telegram-bot-api/bin/telegram-bot-api /usr/local/bin/telegram-bot-api
COPY --chmod=755 entrypoint.sh /entrypoint.sh
RUN telegram-bot-api --version

EXPOSE 8081
ENTRYPOINT ["/entrypoint.sh"]
