import asyncio
import logging
import random
from copy import deepcopy
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from TikTokLive import TikTokLiveClient
import TikTokLive.events as tt_events
from TikTokLive.client.errors import UserOfflineError, SignatureRateLimitError
from .broadcast import Broadcaster
from .storage import EventStore

from .config import settings
from .rules import actions_for, get_profile, validate_rules
from .diagnostics import (
    DIAGNOSTIC_EVENTS, LEVEL_RANK, classify_diagnostic, content_fingerprint,
    extract_event_time_ms, payload_has_meaningful_content, summarize_unknown_payload,
)

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
        return None  # Do not stringify unvisited structures containing secrets.
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
    return "<unsupported>"


def avatar_url(user: Any) -> str | None:
    for name in ("avatar_thumb", "avatar_medium", "avatar_large"):
        image = getattr(user, name, None)
        urls = image.get("url_list", []) if isinstance(image, dict) else getattr(image, "url_list", [])
        for url in urls or []:
            if isinstance(url, str) and url.startswith(("https://", "http://")):
                return url
    return None


def event_user(event: Any) -> dict[str, Any] | None:
    user = getattr(event, "user", None)
    if not user:
        return None
    return {
        "unique_id": getattr(user, "unique_id", None),
        "nickname": getattr(user, "nickname", None),
        "user_id": str(getattr(user, "id", "") or "") or None,
        "avatar_url": avatar_url(user),
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


broadcaster = Broadcaster(settings.ws_queue_size, settings.ws_send_timeout)


@dataclass
class LiveSession:
    session_id: str
    username: str
    profile_name: str = "raw"
    store: EventStore | None = None
    simulation: bool = False
    pinned: bool = False
    last_activity: int = field(default_factory=now_ms)
    retry_count: int = 0

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

    recent_safety_signals: deque = field(default_factory=lambda: deque(maxlen=50))
    diagnostic_fingerprints: dict[str, int] = field(default_factory=dict)

    participants: dict[str, dict[str, Any]] = field(default_factory=dict)

    state: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    rules: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        if self.profile_name not in {"raw", "dance", "kite"}:
            raise ValueError("Unknown profile")
        profile = get_profile(self.profile_name)
        self.rules = validate_rules(profile["rules"])
        self.config = {
            "comment_enabled": True,
            "like_enabled": True,
            "follow_enabled": True,
            "share_enabled": True,
            "gift_enabled": True,
            "safety_auto_pause_critical": True,
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
            "diagnostic_info": 0,
            "diagnostic_historical": 0,
            "diagnostic_observation": 0,
            "diagnostic_alert": 0,
            "diagnostic_critical": 0,
            "diagnostic_suppressed": 0,
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
            "last_monitor_event": None,
            "created_at": now_ms(),
            "connected_at": None,
        }

    def oldest_seq(self):
        if self.store:
            return self.store.oldest(self.session_id, self.seq)
        return self.events[0]["seq"] if self.events else self.seq + 1

    def read_events(self, after, limit=500):
        if self.store:
            return self.store.read(self.session_id, after, limit)
        return [e for e in self.events if e["seq"] > after][:limit]

    def persist(self):
        if self.store:
            self.store.save(self)

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.state,
            "simulation": self.simulation,
            "pinned": self.pinned,
            "last_activity": self.last_activity,
            "stats": dict(self.stats),
            "config": dict(self.config),
            "rules": self.rules,
            "latest_seq": self.seq,
            "oldest_seq": self.oldest_seq(),
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
        p.update({k: user.get(k) for k in ("unique_id", "nickname", "user_id", "avatar_url")})
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
        safety_meta: dict[str, Any] | None = None,
        dedup_key: str | None = None,
    ) -> dict[str, Any] | None:
        if source == "tiktok" and dedup_key and (
            dedup_key in self.dedup_set or (self.store and self.store.seen(self.session_id, dedup_key))
        ):
            return None

        simulated = source == "simulation"

        async with self.lock:
            before = (self.seq, deepcopy(self.state), dict(self.stats), deepcopy(self.participants))
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

            if safety_meta:
                item["safety"] = dict(safety_meta)
                level = str(safety_meta.get("level") or "INFO").upper()
                # Additive compatibility for consumers of the v4.0 bridge.
                item["safety"]["severity"] = (
                    "high" if level == "CRITICAL" else
                    "medium" if level in {"OBSERVATION", "ALERT"} else "info"
                )
                stat_key = {
                    "INFO": "diagnostic_info",
                    "HISTORICAL": "diagnostic_historical",
                    "OBSERVATION": "diagnostic_observation",
                    "ALERT": "diagnostic_alert",
                    "CRITICAL": "diagnostic_critical",
                }.get(level)
                if stat_key:
                    self.stats[stat_key] += 1
                if level in {"OBSERVATION", "ALERT", "CRITICAL"}:
                    self.stats["diagnostics"] += 1
                    self.state["last_monitor_event"] = item
                    current_rank = LEVEL_RANK.get(self.state.get("safety_state", "NORMAL"), 0)
                    new_rank = LEVEL_RANK.get(level, 0)
                    if new_rank >= current_rank:
                        self.state["safety_state"] = level
                    if level in {"ALERT", "CRITICAL"}:
                        self.state["last_safety_event"] = item
                    if (
                        level == "CRITICAL"
                        and safety_meta.get("auto_pause")
                        and self.config.get("safety_auto_pause_critical")
                    ):
                        self.state["automation_paused"] = True

            item["schema_version"] = 1
            item["automation_paused_at_ingest"] = self.state["automation_paused"]
            gameplay_type = typ in {"comment", "like", "follow", "share", "gift", "subscription"}
            if gameplay_type and not self.state["automation_paused"]:
                try:
                    item["actions"] = actions_for(self.rules, item)
                except (ValueError, TypeError, OverflowError):
                    log.exception("Invalid rule execution in session %s", self.session_id)

            self._update_stats(item, simulated)
            self._update_participant(item, simulated)

            self.state["last_event_at"] = item["timestamp"]
            try:
                if self.store:
                    self.store.save(self, item, dedup_key if source == "tiktok" else None)
            except Exception:
                self.seq, self.state, self.stats, self.participants = before
                raise
            if source == "tiktok":
                self._seen(dedup_key)
            self.events.append(item)
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

    def _find_corroborating_signal(self, typ: str, received_at: int) -> str | None:
        window_ms = int(settings.diagnostic_corroboration_seconds * 1000)
        while self.recent_safety_signals and received_at - self.recent_safety_signals[0][0] > window_ms:
            self.recent_safety_signals.popleft()
        for ts, other_type, level in reversed(self.recent_safety_signals):
            if other_type != typ and level in {"ALERT", "CRITICAL"}:
                return other_type
        return None

    def _repeat_suppressed(self, typ: str, level: str, payload: Any, received_at: int) -> bool:
        ttl_ms = int(settings.diagnostic_repeat_suppress_seconds * 1000)
        fingerprint = content_fingerprint(typ, level, payload)
        previous = self.diagnostic_fingerprints.get(fingerprint)
        self.diagnostic_fingerprints[fingerprint] = received_at
        # Opportunistic cleanup so this dict stays bounded.
        if len(self.diagnostic_fingerprints) > 500:
            cutoff = received_at - max(ttl_ms * 3, 60_000)
            self.diagnostic_fingerprints = {
                k: v for k, v in self.diagnostic_fingerprints.items() if v >= cutoff
            }
        return previous is not None and received_at - previous <= ttl_ms

    async def process_diagnostic(self, event: Any, typ: str, policy: str):
        received_at = now_ms()
        payload = safe_value(event)
        raw = {"event_class": event.__class__.__name__}
        if settings.store_raw_diagnostics:
            raw["payload"] = payload
        meaningful = payload_has_meaningful_content(payload)
        event_time = extract_event_time_ms(event)

        preliminary = classify_diagnostic(
            policy=policy,
            connected_at_ms=self.state.get("connected_at"),
            received_at_ms=received_at,
            event_time_ms=event_time,
            meaningful=meaningful,
            historical_grace_ms=int(settings.diagnostic_historical_grace_seconds * 1000),
            fresh_max_age_ms=int(settings.diagnostic_fresh_max_seconds * 1000),
            startup_quarantine_ms=int(settings.diagnostic_startup_quarantine_seconds * 1000),
        )

        # Info messages carrying only common transport fields are pure noise (e.g. banner storms).
        if preliminary["level"] == "INFO" and not meaningful:
            self.stats["diagnostic_suppressed"] += 1
            return None

        corroborated_by = None
        if preliminary["level"] == "ALERT":
            corroborated_by = self._find_corroborating_signal(typ, received_at)

        safety = classify_diagnostic(
            policy=policy,
            connected_at_ms=self.state.get("connected_at"),
            received_at_ms=received_at,
            event_time_ms=event_time,
            meaningful=meaningful,
            historical_grace_ms=int(settings.diagnostic_historical_grace_seconds * 1000),
            fresh_max_age_ms=int(settings.diagnostic_fresh_max_seconds * 1000),
            startup_quarantine_ms=int(settings.diagnostic_startup_quarantine_seconds * 1000),
            corroborated_by=corroborated_by,
        )

        if self._repeat_suppressed(typ, safety["level"], payload, received_at):
            self.stats["diagnostic_suppressed"] += 1
            return None

        if safety["level"] in {"ALERT", "CRITICAL"}:
            self.recent_safety_signals.append((received_at, typ, safety["level"]))

        raw["diagnostic_summary"] = {
            "class": event.__class__.__name__,
            "policy": policy,
            "message_id": event_message_id(event),
        }
        return await self.push(
            typ,
            raw,
            safety_meta=safety,
            dedup_key=event_message_id(event),
        )

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
            self.retry_count = 0
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
            await broadcaster.send({"kind": "status", "session": {"session_id": self.session_id, "viewers": self.state["viewers"]}})

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

        # Diagnostic/compliance events are classified by freshness + payload content.
        for class_name, spec in DIAGNOSTIC_EVENTS.items():
            cls = getattr(tt_events, class_name, None)
            if not cls:
                continue

            async def handler(event, _typ=spec["type"], _policy=spec["policy"]):
                await self.process_diagnostic(event, _typ, _policy)
            c.add_listener(cls, handler)

        # Unknown events are summarized, not stored raw. This avoids leaking route/signing data
        # and suppresses wrapper duplicates of messages already parsed by a typed listener.
        unknown_cls = getattr(tt_events, "UnknownEvent", None)
        if unknown_cls:
            async def on_unknown(event):
                summary = summarize_unknown_payload(safe_value(event))
                ids = summary.get("message_ids") or []
                if ids and any(mid in self.dedup_set for mid in ids):
                    self.stats["diagnostic_suppressed"] += 1
                    return
                if not summary.get("methods") and not ids:
                    return
                await self.push("unknown", {"event_class": "UnknownEvent", **summary})
            c.add_listener(unknown_cls, on_unknown)

    def retry_delay(self, offline=False, rate_limit=None):
        base = settings.offline_retry_seconds if offline else settings.reconnect_base_seconds
        ceiling = settings.offline_max_seconds if offline else settings.reconnect_max_seconds
        delay = min(ceiling, base * 2 ** min(self.retry_count, 16))
        self.retry_count += 1
        delay = random.uniform(delay * .8, delay)
        if rate_limit is not None:
            # Never retry before the provider's Retry-After/reset deadline.
            delay = max(delay, rate_limit) + random.uniform(0, base)
        return delay

    async def run(self):
        if self.simulation:
            return
        try:
            while not self.stop_requested:
                delay = settings.reconnect_base_seconds
                try:
                    self.state.update(connection_state="connecting", last_error=None)
                    self.client = TikTokLiveClient(unique_id=f"@{self.username}")
                    self.client.ignore_broken_payload = settings.ignore_broken_payload
                    self.register_handlers()
                    await self.client.connect(fetch_room_info=True, fetch_gift_info=True, fetch_live_check=True)
                    if self.stop_requested:
                        break
                    offline = self.ended_recently or not self.state.get("live")
                    self.state["connection_state"] = "offline" if offline else "reconnecting"
                    delay = self.retry_delay(offline=offline)
                except UserOfflineError:
                    self.state.update(connection_state="offline", live=False, last_error="User is not currently LIVE")
                    delay = self.retry_delay(offline=True)
                except SignatureRateLimitError as exc:
                    response = getattr(exc, "response", None)
                    headers = response.headers if response is not None else {}
                    minimum = settings.offline_max_seconds
                    from email.utils import parsedate_to_datetime
                    try:
                        raw = headers.get("Retry-After")
                        if raw is not None:
                            try:
                                minimum = max(0, float(raw))
                            except ValueError:
                                minimum = max(0, parsedate_to_datetime(raw).timestamp() - time.time())
                        elif headers.get("RateLimit-Reset"):
                            minimum = max(0, float(headers["RateLimit-Reset"]) - time.time())
                        else:
                            minimum = max(0, float(exc.retry_after))
                    except (ValueError, TypeError, AttributeError):
                        pass
                    self.state.update(connection_state="rate_limited", last_error="Signing service rate limit")
                    delay = self.retry_delay(rate_limit=minimum)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Error text can contain signing URLs/credentials; expose the class only.
                    self.state.update(connection_state="error", last_error=type(exc).__name__)
                    await self.push("connection_error", {"message": type(exc).__name__})
                    log.warning("Connection failure @%s (%s)", self.username, type(exc).__name__)
                    delay = self.retry_delay()
                finally:
                    self.state["connected"] = False
                    if self.client:
                        try:
                            await asyncio.wait_for(self.client.disconnect(close_client=True), 5)
                        except (Exception, asyncio.CancelledError):
                            pass
                        self.client = None
                self.state["reconnects"] += 1
                self.state["next_retry_at"] = now_ms() + int(delay * 1000)
                await broadcaster.send({"kind": "status", "session": {
                    k: self.state[k] for k in ("session_id", "connection_state", "connected", "last_error")}})
                await asyncio.sleep(delay)
        finally:
            self.state.update(connection_state="stopped", connected=False)

    async def stop(self):
        self.stop_requested = True
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        self.state.update(connection_state="stopped", connected=False)

    async def wait_for_events(self, after: int, wait_seconds: float) -> list[dict[str, Any]]:
        def collect():
            return self.read_events(after)

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
        self.persist()

    def set_rules(self, rules: Any):
        self.rules = validate_rules(rules)
        self.persist()

    def leaderboard(self, sort_by: str = "coins", limit: int = 50) -> list[dict[str, Any]]:
        allowed = {"coins", "gifts", "likes", "comments", "shares", "follows"}
        key = sort_by if sort_by in allowed else "coins"
        rows = sorted(self.participants.values(), key=lambda p: p.get(key, 0), reverse=True)
        return rows[:max(1, min(limit, 100))]


class LiveManager:
    def __init__(self, store=None):
        self.sessions: dict[str, LiveSession] = {}
        self.lock = asyncio.Lock()
        self.store = store

    def restore(self):
        if not self.store:
            return
        self.store.prune()
        for snap in self.store.snapshots():
            s = LiveSession(snap["session_id"], snap["username"], snap["profile"], store=self.store,
                            simulation=snap.get("simulation", False), pinned=snap.get("pinned", False))
            s.seq = snap["latest_seq"]
            s.state.update({key: snap[key] for key in s.state if key in snap})
            s.state.update(connected=False, live=False, connection_state="dormant")
            s.stats.update(snap["stats"])
            s.config.update(snap["config"])
            s.rules = validate_rules(snap["rules"])
            s.participants = self.store.participants(s.session_id)
            s.events.extend(self.store.read(s.session_id, max(0, s.seq - settings.max_events), settings.max_events))
            s.last_activity = snap.get("last_activity", now_ms())
            self.sessions[s.session_id] = s

    def active_count(self):
        return sum(bool(s.task and not s.task.done()) for s in self.sessions.values() if not s.simulation)

    def activate(self, session):
        session.last_activity = now_ms()
        if session.simulation:
            session.state["connection_state"] = "simulation"
            return
        if session.task and not session.task.done():
            return
        if self.active_count() >= settings.max_sessions:
            raise RuntimeError(f"MAX_SESSIONS limit reached ({settings.max_sessions})")
        session.stop_requested = False
        session.task = asyncio.create_task(session.run())

    async def connect(self, username, profile="raw", *, simulation=False, pinned=False):
        username = clean_username(username)
        get_profile(profile)
        async with self.lock:
            for s in self.sessions.values():
                if s.username.casefold() == username.casefold() and s.simulation == simulation:
                    self.activate(s)
                    if pinned:
                        s.pinned = True
                    s.persist()
                    return s, False
            if len(self.sessions) >= settings.max_stored_sessions:
                raise RuntimeError("Stored session limit reached")
            if simulation and sum(s.simulation for s in self.sessions.values()) >= settings.max_simulations:
                raise RuntimeError("Simulation session limit reached")
            if not simulation and self.active_count() >= settings.max_sessions:
                raise RuntimeError("MAX_SESSIONS limit reached")
            sid = uuid.uuid4().hex[:16]
            s = LiveSession(sid, username, profile, store=self.store, simulation=simulation, pinned=pinned)
            s.persist()
            self.sessions[sid] = s
            self.activate(s)
            return s, True

    def get(self, session_id):
        if session_id not in self.sessions:
            raise KeyError(session_id)
        return self.sessions[session_id]

    async def disconnect(self, session_id):
        async with self.lock:
            s = self.sessions.get(session_id)
            if s:
                await s.stop()
                if self.store:
                    self.store.delete(session_id)
                self.sessions.pop(session_id, None)

    async def reap(self):
        async with self.lock:
            for s in list(self.sessions.values()):
                idle = (now_ms() - s.last_activity) / 1000
                if not s.pinned and idle > settings.consumer_timeout_seconds:
                    await s.stop()
                    s.state["connection_state"] = "dormant"
                    s.persist()
                    if idle > settings.retention_seconds:
                        if self.store:
                            self.store.delete(s.session_id)
                        self.sessions.pop(s.session_id, None)
            if self.store:
                self.store.prune()

    async def maintenance(self):
        while True:
            await asyncio.sleep(min(30, settings.consumer_timeout_seconds))
            await self.reap()

    async def stop_all(self):
        await asyncio.gather(*(s.stop() for s in self.sessions.values()))
        for s in self.sessions.values():
            s.persist()

    async def auto_connect(self):
        for s in list(self.sessions.values()):
            if s.pinned:
                self.activate(s)
        for item in settings.auto_connect.split(","):
            if item.strip():
                username, _, profile = item.strip().partition(":")
                await self.connect(username, profile or "raw", pinned=True)


manager = LiveManager()
