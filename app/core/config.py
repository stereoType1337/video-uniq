from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    webhook_host: str
    webhook_port: int = 8080
    required_channel: str = ""

    # Local telegram-bot-api server (lifts the 20 MB download / 50 MB upload limits).
    # Empty url -> cloud Bot API. Files dir: where the server writes files (its own view)
    # and where the same directory is mounted in our containers.
    telegram_api_url: str = ""
    telegram_files_server_dir: str = "/var/lib/telegram-bot-api"
    telegram_files_local_dir: str = "/srv/work/tg"

    database_url: str = "postgresql://uniq:uniq@db:5432/uniq"
    redis_url: str = "redis://redis:6379/0"
    service_secret: str

    max_file_mb: int = 150
    min_duration_s: float = 0.5
    max_duration_s: float = 180.0
    max_active_jobs: int = 2
    files_ttl_hours: int = 24
    work_dir: str = "/srv/work"
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    fpcalc_bin: str = "fpcalc"

    # Quality gate. Units: pHash mean Hamming bits (0..64), Chromaprint bit error rate (0..1).
    # Defaults come from local measurements; calibrate on test accounts (ARCHITECTURE §6).
    min_phash_to_original: float = 6.0
    min_phash_to_copies: float = 5.0
    min_audio_to_original: float = 0.10
    max_render_attempts: int = 4
    fingerprint_history: int = 500

    credit_ttl_days: int = 30
    referral_bonus: int = 30

    # .env is shared with the tg_api container (TELEGRAM_API_ID/HASH are its, not ours)
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def webhook_path(self) -> str:
        return f"/webhook/{self.bot_token}"

    @property
    def webhook_url(self) -> str:
        return f"{self.webhook_host.rstrip('/')}{self.webhook_path}"


settings = Settings()
