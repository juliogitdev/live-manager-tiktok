import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from TikTokLive import TikTokLiveClient
import TikTokLive.events as tt_events
from TikTokLive.client.errors import UserOfflineError

from .config import settings
from .rules import actions_for, get_profile, validate_rules

log = logging.getLogger("live-manager")


def now_ms() -> int:
    return int(time.time() * 1000)


def clean_username(value: str) -> str:
    value = (value or "").strip().lstrip("@")
    if not value or len(value) > 64 or any(ch.isspace() for ch in value):
        raise ValueError("Invalid TikTok username")
    return value


REDACT_PARTS = ("cookie", "token", "signature", "session", "sec_uid", "ms_token")


def safe_value(value: Any, depth: int = 0) -> Any:
    if depth >= 4:
        return str(value)[:300]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return value[:2000]
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, (list, tuple)):
        return [safe_value(v, depth + 1) for v in value[:20]]
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:50]:
            key = str(k)
            if any(part in key.lower() for part in REDACT_PARTS):
                out[key] = "[redacted]"
            else:
                out[key] = safe_value(v, depth + 1)
        return out
    if hasattr(value, "to_dict"):
        try:
            return safe_value(value.to_dict(), depth + 1)
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        try:
            return safe_value(
                {k: v for k, v in vars(value).items() if not k.startswith("_")},
                depth + 1,
            )
        except Exception:
            pass
    return str(value)[:1000]


def event_user(event: Any) -> dict[str, Any] | None:
    user = getattr(event, "user", None)
    if not user:
        return None
    return {
        "unique_id": getattr(user, "unique_id", None),
        "nickname": getattr(user, "nickname", None),
        "user_id": str(getattr(user, "id", "") or "") or None,
    }


def event_message_id(event: Any) -> str | None:
    common = getattr(event, "common", None)
    for obj, names in [
        (common, ("msg_id", "message_id", "id")),
        (event, ("log_id", "message_id", "msg_id")),
    ]:
        if obj:
            for name in names:
                value = getattr(obj, name, None)
                if value not in (None, "", 0):
                    return str(value)
    return None


class Broadcaster:
    def __init__(self):
        self.clients: set[Any] = set()
        self.lock = asyncio.Lock()

    async def add(self, ws: Any):
        async with self.lock:
            self.clients.add(ws)

    async def remove(self, ws: Any):
        async with self.lock:
            self.clients.discard(ws)

    async def send(self, payload: dict[str, Any]):
        async with self.lock:
            clients = list(self.clients)
        dead = []
        for ws in clients:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self.lock:
                for ws in dead:
                    self.clients.discard(ws)


broadcaster = Broadcaster()


SAFETY_EVENTS = {
    "BottomEvent": ("bottom_notice", "high"),
    "PerceptionEvent": ("perception", "high"),
    "PartnershipPunishEvent": ("partnership_punish", "high"),
    "RoomVerifyEvent": ("room_verify", "high"),
    "GiftDynamicRestrictionEvent": ("gift_restriction", "high"),
    "AccessControlEvent": ("access_control", "medium"),
    "GiftPromptEvent": ("gift_prompt", "medium"),
    "NoticeEvent": ("notice", "info"),
    "RoomNotifyEvent": ("room_notify", "info"),
    "SystemEvent": ("system", "info"),
    "InRoomBannerEvent": ("in_room_banner", "info"),
    "ToastEvent": ("toast", "info"),
    "AccessRecallEvent": ("access_recall", "info"),
}


@dataclass
class LiveSession:
    session_id: str
    username: str
    profile_name: str = "raw"

    events: deque = field(default_factory=lambda: deque(maxlen=settings.max_events))
    seq: int = 0
    client: TikTokLiveClient | None = None
    task: asyncio.Task | None = None
    stop_requested: bool = False
    ended_recently: bool = False

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    new_event: asyncio.Event = field(default_factory=asyncio.Event)

    dedup_order: deque = field(default_factory=lambda: deque(maxlen=4000))
    dedup_set: set[str] = field(default_factory=set)

    participants: dict[str, dict[str, Any]] = field(default_factory=dict)

    state: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    rules: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        if self.profile_name not in {"raw", "dance", "kite"}:
            self.profile_name = "raw"
        profile = get_profile(self.profile_name)
        self.rules = validate_rules(profile["rules"])
        self.config = {
            "comment_enabled": True,
            "like_enabled": True,
            "follow_enabled": True,
            "share_enabled": True,
            "gift_enabled": True,
            "safety_auto_pause_high": True,
            "count_simulations_in_stats": False,
        }
        self.stats = {
            "comments": 0,
            "likes": 0,
            "follows": 0,
            "shares": 0,
            "gifts": 0,
            "gift_coins": 0,
            "subscriptions": 0,
            "diagnostics": 0,
            "simulations": 0,
        }
        self.state = {
            "session_id": self.session_id,
            "username": self.username,
            "profile": self.profile_name,
            "connection_state": "idle",
            "connected": False,
            "live": False,
            "live_paused": False,
            "automation_paused": False,
            "room_id": None,
            "viewers": None,
            "last_event_at": None,
            "last_error": None,
            "reconnects": 0,
            "safety_state": "NORMAL",
            "last_safety_event": None,
            "created_at": now_ms(),
            "connected_at": None,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.state,
            "stats": dict(self.stats),
            "config": dict(self.config),
            "rules": self.rules,
            "latest_seq": self.seq,
            "oldest_seq": self.events[0]["seq"] if self.events else self.seq,
            "queued_events": len(self.events),
            "participants": len(self.participants),
        }

    def _seen(self, key: str | None) -> bool:
        if not key:
            return False
        if key in self.dedup_set:
            return True
        if len(self.dedup_order) == self.dedup_order.maxlen:
            old = self.dedup_order.popleft()
            self.dedup_set.discard(old)
        self.dedup_order.append(key)
        self.dedup_set.add(key)
        return False

    def _update_participant(self, event: dict[str, Any], simulated: bool):
        if simulated and not self.config.get("count_simulations_in_stats"):
            return
        data = event.get("data") or {}
        user = data.get("user") or {}
        key = str(user.get("user_id") or user.get("unique_id") or "")
        if not key:
            return
        if key not in self.participants and len(self.participants) >= settings.max_participants:
            return
        p = self.participants.setdefault(key, {
            "unique_id": user.get("unique_id"),
            "nickname": user.get("nickname"),
            "comments": 0, "likes": 0, "follows": 0,
            "shares": 0, "gifts": 0, "coins": 0,
        })
        typ = event["type"]
        if typ == "comment":
            p["comments"] += 1
        elif typ == "like":
            p["likes"] += int(data.get("count") or 1)
        elif typ == "follow":
            p["follows"] += 1
        elif typ == "share":
            p["shares"] += 1
        elif typ == "gift":
            repeat = int(data.get("repeat_count") or 1)
            coins = int((data.get("gift") or {}).get("diamond_count") or 0) * repeat
            p["gifts"] += repeat
            p["coins"] += coins

    def _update_stats(self, event: dict[str, Any], simulated: bool):
        if simulated:
            self.stats["simulations"] += 1
            if not self.config.get("count_simulations_in_stats"):
                return
        data = event.get("data") or {}
        typ = event["type"]
        if typ == "comment":
            self.stats["comments"] += 1
        elif typ == "like":
            self.stats["likes"] += int(data.get("count") or 1)
        elif typ == "follow":
            self.stats["follows"] += 1
        elif typ == "share":
            self.stats["shares"] += 1
        elif typ == "gift":
            repeat = int(data.get("repeat_count") or 1)
            gift = data.get("gift") or {}
            self.stats["gifts"] += repeat
            self.stats["gift_coins"] += int(gift.get("diamond_count") or 0) * repeat
        elif typ == "subscription":
            self.stats["subscriptions"] += 1

    async def push(
        self,
        typ: str,
        data: dict[str, Any] | None = None,
        *,
        source: str = "tiktok",
        safety_severity: str | None = None,
        dedup_key: str | None = None,
    ) -> dict[str, Any] | None:
        if source == "tiktok" and self._seen(dedup_key):
            return None

        simulated = source == "simulation"

        async with self.lock:
            self.seq += 1
            item = {
                "seq": self.seq,
                "id": uuid.uuid4().hex,
                "type": typ,
                "timestamp": now_ms(),
                "session_id": self.session_id,
                "username": self.username,
                "source": source,
                "simulated": simulated,
                "data": data or {},
                "actions": [],
            }

            if safety_severity:
                self.stats["diagnostics"] += 1
                item["safety"] = {"severity": safety_severity}
                if safety_severity in {"medium", "high"}:
                    self.state["safety_state"] = "ATTENTION"
                    self.state["last_safety_event"] = item
                if safety_severity == "high" and self.config.get("safety_auto_pause_high"):
                    self.state["automation_paused"] = True

            gameplay_type = typ in {"comment", "like", "follow", "share", "gift", "subscription"}
            if gameplay_type and not self.state["automation_paused"]:
                item["actions"] = actions_for(self.rules, item)

            self._update_stats(item, simulated)
            self._update_participant(item, simulated)

            self.events.append(item)
            self.state["last_event_at"] = item["timestamp"]
            self.new_event.set()

        await broadcaster.send({"kind": "event", "event": item})
        return item

    def _raw_event(self, event: Any) -> dict[str, Any]:
        if not settings.store_raw_diagnostics:
            return {"event_class": event.__class__.__name__}
        return {
            "event_class": event.__class__.__name__,
            "payload": safe_value(event),
        }

    def _gift_diamonds(self, gift: Any) -> int:
        direct = getattr(gift, "diamond_count", None)
        if direct is not None:
            try:
                return int(direct)
            except Exception:
                pass
        gift_id = getattr(gift, "id", None)
        info = getattr(self.client, "gift_info", None) if self.client else None
        if isinstance(info, dict) and gift_id is not None:
            for key in (gift_id, str(gift_id)):
                row = info.get(key)
                if row:
                    value = getattr(row, "diamond_count", None)
                    if value is None and isinstance(row, dict):
                        value = row.get("diamond_count")
                    try:
                        return int(value or 0)
                    except Exception:
                        return 0
        return 0

    def register_handlers(self):
        c = self.client

        @c.on(tt_events.ConnectEvent)
        async def on_connect(event):
            self.ended_recently = False
            self.state.update({
                "connection_state": "connected",
                "connected": True,
                "live": True,
                "live_paused": False,
                "room_id": str(c.room_id) if c.room_id else None,
                "last_error": None,
                "connected_at": now_ms(),
            })
            await self.push("connect", {"room_id": self.state["room_id"]}, dedup_key=event_message_id(event))

        @c.on(tt_events.DisconnectEvent)
        async def on_disconnect(event):
            self.state["connected"] = False
            if not self.stop_requested:
                self.state["connection_state"] = "reconnecting"
            await self.push("disconnect", dedup_key=event_message_id(event))

        @c.on(tt_events.LiveEndEvent)
        async def on_end(event):
            self.ended_recently = True
            self.state.update({
                "connection_state": "offline",
                "connected": False,
                "live": False,
                "live_paused": False,
            })
            await self.push("live_end", dedup_key=event_message_id(event))

        @c.on(tt_events.LivePauseEvent)
        async def on_pause(event):
            self.state["live_paused"] = True
            await self.push("live_pause", dedup_key=event_message_id(event))

        @c.on(tt_events.LiveUnpauseEvent)
        async def on_unpause(event):
            self.state["live_paused"] = False
            await self.push("live_unpause", dedup_key=event_message_id(event))

        @c.on(tt_events.CommentEvent)
        async def on_comment(event):
            if self.config["comment_enabled"]:
                await self.push(
                    "comment",
                    {"user": event_user(event), "comment": getattr(event, "comment", "")},
                    dedup_key=event_message_id(event),
                )

        @c.on(tt_events.LikeEvent)
        async def on_like(event):
            if self.config["like_enabled"]:
                await self.push(
                    "like",
                    {
                        "user": event_user(event),
                        "count": int(getattr(event, "count", 1) or 1),
                        "total": getattr(event, "total", None),
                    },
                    dedup_key=event_message_id(event),
                )

        @c.on(tt_events.FollowEvent)
        async def on_follow(event):
            if self.config["follow_enabled"]:
                await self.push("follow", {"user": event_user(event)}, dedup_key=event_message_id(event))

        @c.on(tt_events.ShareEvent)
        async def on_share(event):
            if self.config["share_enabled"]:
                await self.push("share", {"user": event_user(event)}, dedup_key=event_message_id(event))

        @c.on(tt_events.GiftEvent)
        async def on_gift(event):
            if not self.config["gift_enabled"]:
                return
            gift = getattr(event, "gift", None)
            if gift is None:
                return
            gift_type = getattr(gift, "type", None)
            if gift_type == 1 and bool(getattr(event, "streaking", False)):
                return
            await self.push(
                "gift",
                {
                    "user": event_user(event),
                    "gift": {
                        "id": str(getattr(gift, "id", "") or "") or None,
                        "name": getattr(gift, "name", None),
                        "type": gift_type,
                        "diamond_count": self._gift_diamonds(gift),
                    },
                    "repeat_count": int(getattr(event, "repeat_count", 1) or 1),
                    "repeat_end": getattr(event, "repeat_end", None),
                },
                dedup_key=event_message_id(event),
            )

        @c.on(tt_events.RoomUserSeqEvent)
        async def on_viewers(event):
            viewers = getattr(event, "total", None)
            if viewers is None:
                viewers = getattr(event, "total_user", None)
            if viewers is not None:
                self.state["viewers"] = int(viewers)
            await broadcaster.send({"kind": "status", "session": self.snapshot()})

        # Optional subscription event in TikTokLive 7.x.
        sub_cls = getattr(tt_events, "SubNotifyEvent", None)
        if sub_cls:
            async def on_sub(event):
                await self.push(
                    "subscription",
                    {"user": event_user(event), "raw": self._raw_event(event)},
                    dedup_key=event_message_id(event),
                )
            c.add_listener(sub_cls, on_sub)

        # Diagnostic/compliance events. Optional imports avoid breaking when the schema changes.
        for class_name, (typ, severity) in SAFETY_EVENTS.items():
            cls = getattr(tt_events, class_name, None)
            if not cls:
                continue

            async def handler(event, _typ=typ, _severity=severity):
                await self.push(
                    _typ,
                    self._raw_event(event),
                    safety_severity=_severity,
                    dedup_key=event_message_id(event),
                )
            c.add_listener(cls, handler)

        unknown_cls = getattr(tt_events, "UnknownEvent", None)
        if unknown_cls:
            async def on_unknown(event):
                await self.push(
                    "unknown",
                    self._raw_event(event),
                    dedup_key=event_message_id(event),
                )
            c.add_listener(unknown_cls, on_unknown)

    async def run(self):
        backoff = settings.reconnect_base_seconds
        while not self.stop_requested:
            try:
                self.state["connection_state"] = "connecting"
                self.state["last_error"] = None

                self.client = TikTokLiveClient(unique_id=f"@{self.username}")
                self.client.ignore_broken_payload = settings.ignore_broken_payload
                self.register_handlers()

                log.info("Connecting @%s", self.username)
                await self.client.connect(
                    fetch_room_info=True,
                    fetch_gift_info=True,
                    fetch_live_check=True,
                )

                if self.stop_requested:
                    break

                self.state["connected"] = False
                self.state["reconnects"] += 1

                if self.ended_recently or not self.state.get("live"):
                    self.state["connection_state"] = "offline"
                    await asyncio.sleep(settings.offline_retry_seconds)
                    backoff = settings.reconnect_base_seconds
                else:
                    self.state["connection_state"] = "reconnecting"
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, settings.reconnect_max_seconds)

            except UserOfflineError:
                self.state.update({
                    "connection_state": "offline",
                    "connected": False,
                    "live": False,
                    "last_error": "User is not currently LIVE",
                })
                await broadcaster.send({"kind": "status", "session": self.snapshot()})
                await asyncio.sleep(settings.offline_retry_seconds)
                backoff = settings.reconnect_base_seconds

            except asyncio.CancelledError:
                break

            except Exception as exc:
                self.state["connected"] = False
                self.state["connection_state"] = "error"
                self.state["last_error"] = f"{type(exc).__name__}: {exc}"
                self.state["reconnects"] += 1
                await self.push("connection_error", {"message": self.state["last_error"]})
                log.exception("Connection failure @%s", self.username)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, settings.reconnect_max_seconds)

            finally:
                if self.client:
                    try:
                        await self.client.disconnect(close_client=True)
                    except Exception:
                        pass

        self.state.update({"connection_state": "stopped", "connected": False})

    async def stop(self):
        self.stop_requested = True
        if self.client and self.client.connected:
            try:
                await self.client.disconnect(close_client=True)
            except Exception:
                pass
        if self.task and not self.task.done():
            self.task.cancel()

    async def wait_for_events(self, after: int, wait_seconds: float) -> list[dict[str, Any]]:
        def collect():
            return [e for e in self.events if e["seq"] > after]

        found = collect()
        if found or wait_seconds <= 0:
            return found

        self.new_event.clear()
        # Avoid race between collect() and clear().
        if self.seq > after:
            return collect()

        try:
            await asyncio.wait_for(self.new_event.wait(), timeout=wait_seconds)
        except asyncio.TimeoutError:
            pass
        return collect()

    def apply_profile(self, profile_name: str):
        profile = get_profile(profile_name)
        self.profile_name = profile_name if profile_name in {"raw", "dance", "kite"} else "raw"
        self.rules = validate_rules(profile["rules"])
        self.state["profile"] = self.profile_name

    def set_rules(self, rules: Any):
        self.rules = validate_rules(rules)

    def leaderboard(self, sort_by: str = "coins", limit: int = 50) -> list[dict[str, Any]]:
        allowed = {"coins", "gifts", "likes", "comments", "shares", "follows"}
        key = sort_by if sort_by in allowed else "coins"
        rows = sorted(self.participants.values(), key=lambda p: p.get(key, 0), reverse=True)
        return rows[:max(1, min(limit, 100))]


class LiveManager:
    def __init__(self):
        self.sessions: dict[str, LiveSession] = {}
        self.lock = asyncio.Lock()

    async def connect(self, username: str, profile: str = "raw") -> tuple[LiveSession, bool]:
        username = clean_username(username)
        async with self.lock:
            for session in self.sessions.values():
                if session.username.casefold() == username.casefold() and not session.stop_requested:
                    if profile and profile != session.profile_name:
                        session.apply_profile(profile)
                    return session, False

            if len(self.sessions) >= settings.max_sessions:
                raise RuntimeError(f"MAX_SESSIONS limit reached ({settings.max_sessions})")

            sid = uuid.uuid4().hex[:16]
            session = LiveSession(sid, username, profile_name=profile or "raw")
            self.sessions[sid] = session
            session.task = asyncio.create_task(session.run())
            return session, True

    def get(self, session_id: str) -> LiveSession:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)
        return session

    async def disconnect(self, session_id: str):
        async with self.lock:
            session = self.sessions.get(session_id)
            if not session:
                return
            await session.stop()
            self.sessions.pop(session_id, None)

    async def stop_all(self):
        async with self.lock:
            rows = list(self.sessions.values())
        await asyncio.gather(*(s.stop() for s in rows), return_exceptions=True)

    async def auto_connect(self):
        raw = settings.auto_connect
        if not raw:
            return
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                username, profile = item.split(":", 1)
            else:
                username, profile = item, "raw"
            try:
                await self.connect(username.strip(), profile.strip())
            except Exception:
                log.exception("AUTO_CONNECT failed for %s", item)


manager = LiveManager()
