import re
from functools import cached_property
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MB = 1024 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    bot_api_url: str = "http://127.0.0.1:8081"
    admin_id: int
    # Comma-separated Telegram user ids; empty means anyone may use the bot.
    allowed_users: str = ""

    data_dir: Path = Path("/data")
    db_path: Path = Path("/state/bot.db")
    youtube_cookies: Path = Path("/secrets/youtube-cookies.txt")
    instagram_cookies: Path = Path("/secrets/instagram-cookies.txt")

    # Per-source egress: "direct", "warp" (the built-in WARP service), or the URL of a proxy you run
    # (socks5h://, socks5://, http://).
    egress_youtube: str = "direct"
    egress_instagram: str = "direct"
    egress_soundcloud: str = "direct"
    egress_generic: str = "direct"
    # Loopback port of the warp service's SOCKS5 proxy; compose reads the same WARP_PORT.
    warp_port: int = 1081

    global_downloads: int = 2
    youtube_concurrency: int = 2
    probe_concurrency: int = 3
    user_running: int = 1
    user_queued: int = 3
    max_links_per_message: int = 5

    max_duration: int = 3 * 3600
    job_timeout: int = 30 * 60
    probe_timeout: int = 120
    max_upload_mb: int = 2000
    offer_limit_mb: int = 1900
    youtube_sleep_requests: float = 1.0

    progress_interval: float = 4.0
    picker_ttl: int = 15 * 60
    tag_editor_ttl: int = 15 * 60
    admin_alert_interval: int = 60

    metrics_host: str = "127.0.0.1"
    metrics_port: int = 9091
    log_level: str = "INFO"

    selftest_youtube: str = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    selftest_instagram: str = "https://www.instagram.com/p/aye83DjauH/"
    selftest_soundcloud: str = "https://soundcloud.com/forss/flickermood"
    selftest_spotify: str = "https://open.spotify.com/track/4PTG3Z6ehGkBFwjybzWkR8"

    @cached_property
    def allowed_user_ids(self) -> frozenset[int]:
        return frozenset(int(x) for x in self.allowed_users.replace(" ", "").split(",") if x)

    @field_validator("allowed_users")
    @classmethod
    def _check_allowed_users(cls, v: str) -> str:
        if not all(re.fullmatch(r"\d*", x.strip()) for x in v.split(",")):
            raise ValueError("must be comma-separated numeric user ids")
        return v

    @field_validator("egress_youtube", "egress_instagram", "egress_soundcloud", "egress_generic")
    @classmethod
    def _check_egress(cls, v: str) -> str:
        v = v.strip()
        if v and v not in ("direct", "warp") and "://" not in v:
            raise ValueError(f"must be 'direct', 'warp' or a proxy URL such as socks5h://127.0.0.1:1080, got {v!r}")
        return v or "direct"

    @property
    def egress_names(self) -> list[str]:
        """What /selftest compares: direct plus every distinct proxy a source is set to."""
        used = (self.egress_youtube, self.egress_instagram, self.egress_soundcloud, self.egress_generic)
        return list(dict.fromkeys(["direct", *(e.strip() for e in used if self.resolve_egress(e))]))

    @property
    def warp_proxy(self) -> str:
        return f"socks5h://127.0.0.1:{self.warp_port}"

    def resolve_egress(self, egress: str) -> str | None:
        """Proxy URL for an egress setting; None means direct."""
        egress = egress.strip()
        if not egress or egress == "direct":
            return None
        if egress == "warp":
            return self.warp_proxy
        if "://" in egress:
            return egress
        raise ValueError(f"egress {egress!r} must be 'direct', 'warp' or a proxy URL such as socks5h://127.0.0.1:1080")

    @staticmethod
    def egress_label(egress: str) -> str:
        """An egress safe to log, store and show: proxy credentials masked."""
        return re.sub(r"(://)[^/@\s]*@", r"\1***@", egress.strip()) or "direct"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * MB

    @property
    def offer_limit_bytes(self) -> int:
        return self.offer_limit_mb * MB

    def is_allowed(self, user_id: int) -> bool:
        return user_id == self.admin_id or not self.allowed_user_ids or user_id in self.allowed_user_ids
