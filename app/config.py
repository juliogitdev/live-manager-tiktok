from dataclasses import dataclass
import os


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    api_key: str = os.getenv("API_KEY", "").strip()
    dashboard_password: str = os.getenv("DASHBOARD_PASSWORD", "").strip()
    secret_key: str = os.getenv("SECRET_KEY", "").strip()

    max_sessions: int = int(os.getenv("MAX_SESSIONS", "5"))
    max_events: int = int(os.getenv("MAX_EVENTS", "2500"))
    max_participants: int = int(os.getenv("MAX_PARTICIPANTS", "2000"))

    reconnect_base_seconds: float = float(os.getenv("RECONNECT_BASE_SECONDS", "5"))
    reconnect_max_seconds: float = float(os.getenv("RECONNECT_MAX_SECONDS", "60"))
    offline_retry_seconds: float = float(os.getenv("OFFLINE_RETRY_SECONDS", "30"))

    ignore_broken_payload: bool = _bool("IGNORE_BROKEN_PAYLOAD", True)
    store_raw_diagnostics: bool = _bool("STORE_RAW_DIAGNOSTICS", True)

    diagnostic_historical_grace_seconds: float = float(os.getenv("DIAGNOSTIC_HISTORICAL_GRACE_SECONDS", "15"))
    diagnostic_fresh_max_seconds: float = float(os.getenv("DIAGNOSTIC_FRESH_MAX_SECONDS", "120"))
    diagnostic_startup_quarantine_seconds: float = float(os.getenv("DIAGNOSTIC_STARTUP_QUARANTINE_SECONDS", "20"))
    diagnostic_corroboration_seconds: float = float(os.getenv("DIAGNOSTIC_CORROBORATION_SECONDS", "20"))
    diagnostic_repeat_suppress_seconds: float = float(os.getenv("DIAGNOSTIC_REPEAT_SUPPRESS_SECONDS", "30"))

    auto_connect: str = os.getenv("AUTO_CONNECT_USERS", "").strip()
    euler_api_key: str = os.getenv("EULER_API_KEY", "").strip()

    render: bool = os.getenv("RENDER", "").lower() == "true"
    environment: str = os.getenv("ENVIRONMENT", "development").lower()
    cookie_secure: bool = _bool("COOKIE_SECURE", os.getenv("RENDER", "").lower() == "true" or os.getenv("ENVIRONMENT", "").lower() == "production")
    allowed_origins: str = os.getenv("ALLOWED_ORIGINS", "")
    game_tokens: str = os.getenv("GAME_TOKENS", "{}")
    database_path: str = os.getenv("DATABASE_PATH", "data/live-manager.sqlite3")
    retention_seconds: int = int(os.getenv("RETENTION_SECONDS", "86400"))
    consumer_timeout_seconds: int = int(os.getenv("CONSUMER_TIMEOUT_SECONDS", "300"))
    max_simulations: int = int(os.getenv("MAX_SIMULATIONS", "20"))
    max_consumers: int = int(os.getenv("MAX_CONSUMERS", "100"))
    max_stored_sessions: int = int(os.getenv("MAX_STORED_SESSIONS", "100"))
    offline_max_seconds: float = float(os.getenv("OFFLINE_MAX_SECONDS", "900"))
    ws_queue_size: int = int(os.getenv("WS_QUEUE_SIZE", "128"))
    ws_send_timeout: float = float(os.getenv("WS_SEND_TIMEOUT", "5"))
    login_attempts: int = int(os.getenv("LOGIN_ATTEMPTS", "10"))
    login_window_seconds: int = int(os.getenv("LOGIN_WINDOW_SECONDS", "60"))
    max_json_bytes: int = int(os.getenv("MAX_JSON_BYTES", "262144"))
    app_version: str = "4.2.0"

    def validate(self):
        import math
        for name in ("max_sessions", "max_events", "max_participants", "retention_seconds",
                     "consumer_timeout_seconds", "max_simulations", "max_consumers", "max_stored_sessions", "ws_queue_size",
                     "ws_send_timeout", "login_attempts", "login_window_seconds", "max_json_bytes", "offline_retry_seconds",
                     "offline_max_seconds", "reconnect_base_seconds", "reconnect_max_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise RuntimeError(f"{name} must be positive and finite")
        for name in ("diagnostic_historical_grace_seconds", "diagnostic_fresh_max_seconds",
                     "diagnostic_startup_quarantine_seconds", "diagnostic_corroboration_seconds",
                     "diagnostic_repeat_suppress_seconds"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) < 0:
                raise RuntimeError(f"Invalid {name}")
        if self.offline_max_seconds < self.offline_retry_seconds or self.reconnect_max_seconds < self.reconnect_base_seconds:
            raise RuntimeError("Retry maximum must be >= base")
        if not self.database_path:
            raise RuntimeError("DATABASE_PATH is required")
        if (self.render or self.environment == "production") and self.database_path == ":memory:":
            raise RuntimeError("Production requires a persistent DATABASE_PATH")


settings = Settings()
