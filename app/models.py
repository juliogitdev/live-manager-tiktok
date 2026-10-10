from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
import re


class GameAccessFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    consumer_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    usernames: list[str] = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if value is None:
            raise ValueError("Name cannot be null")
        value = value.strip()
        if not value:
            raise ValueError("Name cannot be blank")
        return value

    @field_validator("usernames")
    @classmethod
    def clean_usernames(cls, values):
        if values is None:
            raise ValueError("Usernames cannot be null")
        names = []
        for value in values:
            name = value.strip().removeprefix("@").casefold()
            if not re.fullmatch(r"[a-z0-9_.]{1,64}", name):
                raise ValueError("Invalid TikTok username; wildcards are not allowed")
            if name not in names:
                names.append(name)
        return names

    @field_validator("consumer_id")
    @classmethod
    def clean_consumer(cls, value):
        if value is None:
            raise ValueError("Consumer cannot be null")
        return value


class GameAccessPatch(GameAccessFields):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    consumer_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    usernames: list[str] | None = Field(default=None, min_length=1, max_length=100)


class GameCredential(BaseModel):
    id: str
    name: str
    consumer_id: str
    usernames: list[str]
    created_at: int
    updated_at: int
    revoked_at: int | None
    active: bool


class GameCredentialResult(BaseModel):
    credential: GameCredential


class GameCredentialCreated(GameCredentialResult):
    token: str


class GameCredentialList(BaseModel):
    credentials: list[GameCredential]


class OK(BaseModel):
    ok: bool = True


class Health(OK):
    version: str
    sessions: int
    max_sessions: int


class AuthStatus(BaseModel):
    authenticated: bool
    password_configured: bool


class User(BaseModel):
    unique_id: str | None = None
    nickname: str | None = None
    user_id: str | None = None
    avatar_url: str | None = None


class Safety(BaseModel):
    model_config = ConfigDict(extra="allow")
    level: Literal["INFO", "HISTORICAL", "OBSERVATION", "ALERT", "CRITICAL"]
    severity: Literal["info", "medium", "high"]
    auto_pause: bool


class BridgeEvent(BaseModel):
    seq: int
    id: str
    type: str
    timestamp: int
    source: Literal["tiktok", "simulation", "control"]
    data: dict[str, Any]
    actions: list[dict[str, Any]]
    safety: Safety | None = None
    schema_version: int = 1
    automation_paused_at_ingest: bool


class Event(BridgeEvent):
    session_id: str
    username: str
    simulated: bool


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="allow")
    session_id: str
    username: str
    profile: str
    connection_state: str
    connected: bool
    live: bool
    live_paused: bool
    automation_paused: bool
    room_id: str | None
    viewers: int | None
    last_event_at: int | None
    last_error: str | None
    reconnects: int
    safety_state: str
    last_safety_event: Event | None
    last_monitor_event: Event | None
    created_at: int
    connected_at: int | None
    simulation: bool
    pinned: bool
    last_activity: int
    stats: dict[str, int]
    config: dict[str, bool]
    rules: list[dict[str, Any]]
    latest_seq: int
    oldest_seq: int
    queued_events: int
    participants: int


class Sessions(BaseModel):
    sessions: list[Snapshot]


class Connected(Snapshot):
    created: bool
    profile_conflict: bool


class Events(BaseModel):
    events: list[Event]
    latest_seq: int
    oldest_seq: int
    has_more: bool


class Registered(OK):
    created: bool
    session_id: str
    username: str
    profile: str
    profile_conflict: bool
    requested_profile: str
    oldest_seq: int
    latest_seq: int
    gap_detected: bool
    cursor: int
    state: str
    automation_paused: bool
    consumer_id: str
    resumed: bool


class Poll(OK):
    cursor_expired: bool
    gap_detected: bool
    session_id: str
    oldest_seq: int
    connection_state: str
    events: list[BridgeEvent]
    cursor: int
    latest_seq: int
    automation_paused: bool


class Ack(OK):
    cursor: int


class Rules(OK):
    rules: list[dict[str, Any]]


class Profile(Rules):
    profile: str


class Participant(User):
    comments: int
    likes: int
    follows: int
    shares: int
    gifts: int
    coins: int


class Leaderboard(BaseModel):
    rows: list[Participant]


class Export(BaseModel):
    session: Snapshot
    events: list[Event]
    leaderboard: list[Participant]


class Meta(BaseModel):
    version: str
    max_sessions: int
    profiles: dict[str, dict[str, Any]]
    render_free_note: str


class ConsumerMetric(BaseModel):
    consumer_id: str
    ack: int
    delivered: int
    last_seen: int
    lag_events: int
    gap_count: int


class Metrics(BaseModel):
    consumers: list[ConsumerMetric]
    ws_dropped_clients: int
