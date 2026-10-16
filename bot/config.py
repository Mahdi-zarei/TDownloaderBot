from functools import cached_property
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

MB = 1024 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    bot_api_url: str = "http://telegram-bot-api:8081"
    admin_id: int
    # Comma-separated Telegram user ids; empty means anyone may use the bot.
    allowed_users: str = ""

    data_dir: Path = Path("/data")
    db_path: Path = Path("/state/bot.db")
    youtube_cookies: Path = Path("/secrets/youtube-cookies.txt")
    instagram_cookies: Path = Path("/secrets/instagram-cookies.txt")

    # Named egresses, "name=url,name=url". "direct" is always available.
    egress_proxies: str = "warp=socks5h://warp:1080"
    # Per-source egress: "direct", a name from egress_proxies, or a proxy URL.
    egress_youtube: str = "warp"
    egress_instagram: str = "direct"
    egress_soundcloud: str = "direct"
    egress_generic: str = "direct"

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

    metrics_port: int = 9090
    log_level: str = "INFO"

    selftest_youtube: str = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    selftest_instagram: str = "https://www.instagram.com/p/aye83DjauH/"
    selftest_soundcloud: str = "https://soundcloud.com/forss/flickermood"

    @cached_property
    def allowed_user_ids(self) -> frozenset[int]:
        return frozenset(int(x) for x in self.allowed_users.replace(" ", "").split(",") if x)

    @cached_property
    def proxies(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for pair in self.egress_proxies.split(","):
            name, sep, url = pair.strip().partition("=")
            if sep and name.strip() and url.strip():
                out[name.strip()] = url.strip()
        return out

    @property
    def egress_names(self) -> list[str]:
        return ["direct", *self.proxies]

    def resolve_egress(self, egress: str) -> str | None:
        """Proxy URL for an egress setting; None means direct."""
        egress = egress.strip()
        if not egress or egress == "direct":
            return None
        if egress in self.proxies:
            return self.proxies[egress]
        if "://" in egress:
            return egress
        raise ValueError(f"unknown egress {egress!r}; define it in EGRESS_PROXIES")

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
