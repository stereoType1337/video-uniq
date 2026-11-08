from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    webhook_host: str
    webhook_port: int = 8080
    required_channel: str = ""

    database_url: str = "postgresql://uniq:uniq@db:5432/uniq"
    redis_url: str = "redis://redis:6379/0"
    service_secret: str

    max_file_mb: int = 150
    work_dir: str = "/srv/work"
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    credit_ttl_days: int = 30
    referral_bonus: int = 30

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def webhook_path(self) -> str:
        return f"/webhook/{self.bot_token}"

    @property
    def webhook_url(self) -> str:
        return f"{self.webhook_host.rstrip('/')}{self.webhook_path}"


settings = Settings()
