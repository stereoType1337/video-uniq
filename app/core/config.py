from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    webhook_host: str
    webhook_port: int = 8080
    required_channel: str = ""
    support_contact: str = ""  # @username or link shown by /support

    # Legal pages (/terms, /privacy). Empty operator_name -> pages show a "draft" banner.
    service_name: str = "Uniq"
    public_url: str = "https://uniq.pulsemarket.eu"
    bot_username: str = ""
    operator_name: str = ""  # e.g. ИП Иванов Иван Иванович / самозанятый ...
    operator_details: str = ""  # e.g. ИНН ..., ОГРНИП ...
    contact_email: str = ""
    legal_effective_date: str = "11.11.2026"
    hosting_provider: str = "WEYRO LTD, Ахен"
    admin_ids: str = ""  # comma-separated Telegram ids with access to /admin

    @property
    def admin_id_set(self) -> frozenset[int]:
        return frozenset(int(x) for x in self.admin_ids.replace(" ", "").split(",") if x)

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
    # Benchmarked on 8 vCPU: 4 FFmpeg x 2 threads beats 1 x 8 by ~10 %; more processes thrash.
    # One worker owns the machine; up to 2 jobs share its 4 render slots (arq default is 10 jobs).
    worker_max_jobs: int = 2
    render_slots: int = 4
    ffmpeg_threads: int = 2
    job_timeout_s: int = 3600  # 10 HD copies of a 3-minute clip must fit
    fingerprint_history: int = 500

    credit_ttl_days: int = 30
    referral_bonus: int = 30
    free_credits: int = 5  # one Base run of 5 copies
    # Jobs left unsettled by a dead worker get refunded after these delays
    stale_finished_minutes: int = 10
    stale_running_minutes: int = 120
    draft_ttl_minutes: int = 60  # an unstarted video card expires after this

    fingerprint_retention_days: int = 365  # privacy policy promises this; purged daily
    backup_dir: str = "/srv/backups"
    backup_keep: int = 14
    backup_to_telegram: bool = True  # the off-server copy goes to ADMIN_IDS as a document

    # .env is shared with the tg_api container (TELEGRAM_API_ID/HASH are its, not ours)
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def webhook_path(self) -> str:
        return f"/webhook/{self.bot_token}"

    @property
    def webhook_url(self) -> str:
        return f"{self.webhook_host.rstrip('/')}{self.webhook_path}"


settings = Settings()
