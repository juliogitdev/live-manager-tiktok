import hashlib
import json
import re
from typing import Any


DIAGNOSTIC_EVENTS: dict[str, dict[str, str]] = {
    # Strong signals: can become CRITICAL only when fresh/current and meaningful.
    "BottomEvent": {"type": "bottom_notice", "policy": "strong"},
    "PerceptionEvent": {"type": "perception", "policy": "strong"},
    "PartnershipPunishEvent": {"type": "partnership_punish", "policy": "strong"},

    # Watch signals: useful context, but never critical by class name alone.
    "GiftDynamicRestrictionEvent": {"type": "gift_restriction", "policy": "watch"},
    "RoomVerifyEvent": {"type": "room_verify", "policy": "watch"},
    "AccessControlEvent": {"type": "access_control", "policy": "watch"},
    "GiftPromptEvent": {"type": "gift_prompt", "policy": "watch"},

    # Informational signals. Empty/common-only messages should normally be dropped.
    "NoticeEvent": {"type": "notice", "policy": "info"},
    "RoomNotifyEvent": {"type": "room_notify", "policy": "info"},
    "SystemEvent": {"type": "system", "policy": "info"},
    "InRoomBannerEvent": {"type": "in_room_banner", "policy": "info"},
    "ToastEvent": {"type": "toast", "policy": "info"},
    "AccessRecallEvent": {"type": "access_recall", "policy": "info"},
}

LEVEL_RANK = {
    "INFO": 0,
    "HISTORICAL": 0,
    "OBSERVATION": 1,
    "ALERT": 2,
    "CRITICAL": 3,
}

META_KEYS = {
    "common", "event_class", "eventClass", "method", "msgId", "msg_id",
    "roomId", "room_id", "createTime", "create_time", "isShowMsg", "is_show_msg",
}


def normalize_timestamp_ms(value: Any) -> int | None:
    try:
        if value in (None, "", 0, "0"):
            return None
        n = int(value)
    except (TypeError, ValueError):
        return None

    # seconds -> ms
    if 1_000_000_000 <= n < 10_000_000_000:
        return n * 1000
    # microseconds -> ms
    if n >= 10_000_000_000_000:
        return n // 1000
    # expected millisecond range
    if n >= 1_000_000_000_000:
        return n
    return None


def extract_event_time_ms(event: Any) -> int | None:
    common = getattr(event, "common", None)
    if common is not None:
        for name in ("create_time", "createTime", "timestamp", "time"):
            ts = normalize_timestamp_ms(getattr(common, name, None))
            if ts:
                return ts

    for name in ("create_time", "createTime", "timestamp"):
        ts = normalize_timestamp_ms(getattr(event, name, None))
        if ts:
            return ts
    return None


def _meaningful(value: Any, key: str | None = None, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if key and key in META_KEYS:
        return False
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip()
        return text not in {"", "0", "false", "False", "None", "null", "{}", "[]"}
    if isinstance(value, dict):
        return any(_meaningful(v, str(k), depth + 1) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_meaningful(v, None, depth + 1) for v in value)
    return True


def payload_has_meaningful_content(payload: Any) -> bool:
    """Return true only when payload contains non-common/non-default information."""
    return _meaningful(payload)


def classify_diagnostic(
    *,
    policy: str,
    connected_at_ms: int | None,
    received_at_ms: int,
    event_time_ms: int | None,
    meaningful: bool,
    historical_grace_ms: int,
    fresh_max_age_ms: int,
    startup_quarantine_ms: int,
    corroborated_by: str | None = None,
) -> dict[str, Any]:
    age_ms = None if event_time_ms is None else received_at_ms - event_time_ms
    since_connect_ms = None if connected_at_ms is None else received_at_ms - connected_at_ms

    if event_time_ms is not None and received_at_ms + 120_000 < event_time_ms:
        freshness = "unknown"
    elif connected_at_ms is not None and event_time_ms is not None and event_time_ms < connected_at_ms - historical_grace_ms:
        freshness = "historical"
    elif age_ms is not None and age_ms > fresh_max_age_ms:
        freshness = "historical"
    elif event_time_ms is not None:
        freshness = "current"
    else:
        freshness = "unknown"

    in_startup_quarantine = (
        since_connect_ms is not None and since_connect_ms <= startup_quarantine_ms
    )

    if policy == "info":
        return {
            "level": "INFO",
            "confidence": "low",
            "freshness": freshness,
            "meaningful": meaningful,
            "event_time": event_time_ms,
            "received_at": received_at_ms,
            "age_ms": age_ms,
            "reason": "Evento informativo; não altera o estado de segurança.",
            "auto_pause": False,
        }

    if freshness == "historical":
        return {
            "level": "HISTORICAL",
            "confidence": "high",
            "freshness": freshness,
            "meaningful": meaningful,
            "event_time": event_time_ms,
            "received_at": received_at_ms,
            "age_ms": age_ms,
            "reason": "Mensagem anterior à conexão/fora da janela de frescor; apenas registrada.",
            "auto_pause": False,
        }

    # Unknown-time messages that arrive immediately after connect are often room history/bootstrap.
    if event_time_ms is None and in_startup_quarantine:
        return {
            "level": "OBSERVATION",
            "confidence": "low",
            "freshness": "unknown",
            "meaningful": meaningful,
            "event_time": None,
            "received_at": received_at_ms,
            "age_ms": None,
            "reason": "Timestamp desconhecido durante a carga inicial da sala; não pausa automaticamente.",
            "auto_pause": False,
        }

    if policy == "strong":
        if meaningful and freshness == "current":
            return {
                "level": "CRITICAL",
                "confidence": "high",
                "freshness": freshness,
                "meaningful": True,
                "event_time": event_time_ms,
                "received_at": received_at_ms,
                "age_ms": age_ms,
                "reason": "Sinal forte, atual e com conteúdo; pausa conservadora recomendada.",
                "auto_pause": True,
            }
        if meaningful:
            level = "ALERT"
            reason = "Sinal forte com conteúdo, mas sem confirmação temporal suficiente."
        else:
            level = "OBSERVATION"
            reason = "Sinal forte sem conteúdo útil no payload; observar sem pausar."
    else:  # watch
        if meaningful:
            level = "ALERT"
            reason = "Sinal atual com conteúdo relevante; requer observação/revisão."
        else:
            level = "OBSERVATION"
            reason = "Evento de monitoramento sem conteúdo útil; não indica restrição por si só."

    if corroborated_by and level == "ALERT":
        return {
            "level": "CRITICAL",
            "confidence": "high",
            "freshness": freshness,
            "meaningful": meaningful,
            "event_time": event_time_ms,
            "received_at": received_at_ms,
            "age_ms": age_ms,
            "reason": f"Sinal corroborado por outro tipo recente ({corroborated_by}).",
            "corroborated_by": corroborated_by,
            "auto_pause": True,
        }

    return {
        "level": level,
        "confidence": "medium" if meaningful else "low",
        "freshness": freshness,
        "meaningful": meaningful,
        "event_time": event_time_ms,
        "received_at": received_at_ms,
        "age_ms": age_ms,
        "reason": reason,
        "auto_pause": False,
    }


def content_fingerprint(event_type: str, level: str, payload: Any) -> str:
    # Ignore common transport metadata so repeated banners with different msgId collapse.
    def strip(value: Any, depth: int = 0):
        if depth > 6:
            return None
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if str(k) in META_KEYS:
                    continue
                cleaned = strip(v, depth + 1)
                if cleaned not in (None, "", {}, [], False, 0):
                    out[str(k)] = cleaned
            return out
        if isinstance(value, list):
            return [strip(v, depth + 1) for v in value[:20]]
        return value

    normalized = strip(payload)
    raw = json.dumps([event_type, level, normalized], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


_MSG_ID_RE = re.compile(r"(?:msgId|msg_id)[\"'\s:=]+[\"']?(\d{8,})")
_METHOD_RE = re.compile(r"(?:method)[\"'\s:=]+[\"']?([A-Za-z0-9_]+)")


def summarize_unknown_payload(value: Any) -> dict[str, Any]:
    """Store a compact unknown-event summary instead of raw signing/route data."""
    text = repr(value)
    ids = list(dict.fromkeys(_MSG_ID_RE.findall(text)))[:30]
    methods = list(dict.fromkeys(_METHOD_RE.findall(text)))[:30]
    return {"methods": methods, "message_ids": ids}
