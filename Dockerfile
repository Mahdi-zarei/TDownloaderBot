FROM denoland/deno:bin-2.9.7 AS deno

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    DENO_NO_UPDATE_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# JavaScript runtime for yt-dlp's YouTube challenge solver (yt-dlp-ejs).
COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# The nightly rebuild passes a new YTDLP_REFRESH value, which invalidates the cache from here on:
# only this layer and the code copy below are rebuilt. yt-dlp[default] pins its matching yt-dlp-ejs.
ARG YTDLP_REFRESH=0
RUN echo "yt-dlp refresh: ${YTDLP_REFRESH}" \
 && pip install -U "yt-dlp[default]" gallery-dl \
 && pip show yt-dlp-ejs >/dev/null \
 && yt-dlp --version && deno --version && gallery-dl --version && ffmpeg -version

COPY bot/ /app/bot/

# Runs as root on purpose: it reads files the Bot API server (uid 101) writes under 0750 directories.
CMD ["python", "-m", "bot"]
