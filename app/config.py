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

    auto_connect: str = os.getenv("AUTO_CONNECT_USERS", "").strip()
    euler_api_key: str = os.getenv("EULER_API_KEY", "").strip()

    render: bool = os.getenv("RENDER", "").lower() == "true"
    app_version: str = "4.0.0"


settings = Settings()
