from copy import deepcopy
import math
from typing import Any

ALLOWED_ACTIONS = {
    "heal", "shield", "aura", "animation", "giant", "spawn",
    "sound", "attack", "custom", "score", "speed"
}

BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "raw": {
        "label": "Somente eventos",
        "description": "Não gera ações automáticas para o Roblox.",
        "rules": [],
    },
    "dance": {
        "label": "Dança / Avatar",
        "description": "Comentário cria/identifica avatar; follow dá aura; presente ativa efeitos.",
        "rules": [
            {
                "id": "dance_comment_spawn",
                "enabled": True,
                "when": {"type": "comment"},
                "actions": [{"type": "spawn", "name": "avatar_from_comment"}],
            },
            {
                "id": "dance_follow_aura",
                "enabled": True,
                "when": {"type": "follow"},
                "actions": [{"type": "aura", "name": "follow", "duration": 12}],
            },
            {
                "id": "dance_rose_animation",
                "enabled": True,
                "when": {"type": "gift", "gift_name": "Rose"},
                "actions": [{"type": "animation", "name": "special"}],
            },
            {
                "id": "dance_giant",
                "enabled": True,
                "when": {"type": "gift", "min_coins": 5},
                "actions": [{"type": "giant", "scale": 2.0, "duration": 15}],
            },
        ],
    },
    "kite": {
        "label": "Batalha de Pipas",
        "description": "Comentários, likes, follows/shares e presentes viram ações de gameplay.",
        "rules": [
            {
                "id": "kite_comment_spawn",
                "enabled": True,
                "when": {"type": "comment"},
                "actions": [{"type": "spawn", "name": "kite_from_comment"}],
            },
            {
                "id": "kite_like_heal",
                "enabled": True,
                "when": {"type": "like"},
                "actions": [{"type": "heal", "amount_per_count": 1}],
            },
            {
                "id": "kite_follow_shield",
                "enabled": True,
                "when": {"type": "follow"},
                "actions": [{"type": "shield", "duration": 10}],
            },
            {
                "id": "kite_share_shield",
                "enabled": True,
                "when": {"type": "share"},
                "actions": [{"type": "shield", "duration": 10}],
            },
            {
                "id": "kite_rose",
                "enabled": True,
                "when": {"type": "gift", "gift_name": "Rose"},
                "actions": [
                    {"type": "heal", "amount": 200},
                    {"type": "attack", "name": "special"},
                ],
            },
            {
                "id": "kite_giant",
                "enabled": True,
                "when": {"type": "gift", "min_coins": 5},
                "actions": [{"type": "giant", "scale": 1.8, "duration": 15}],
            },
        ],
    },
}


def get_profile(name: str) -> dict[str, Any]:
    if name not in BUILTIN_PROFILES:
        raise ValueError("Unknown profile")
    key = name
    return deepcopy(BUILTIN_PROFILES[key])


def validate_rules(rules: Any) -> list[dict[str, Any]]:
    if not isinstance(rules, list):
        raise ValueError("rules must be a list")
    if len(rules) > 100:
        raise ValueError("maximum of 100 rules")
    out = []
    ids = set()
    for idx, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"rule {idx} must be an object")
        rid = str(rule.get("id") or f"rule_{idx}")[:80]
        if rid in ids:
            raise ValueError(f"duplicate rule id: {rid}")
        ids.add(rid)
        when = rule.get("when") or {}
        actions = rule.get("actions") or []
        if not isinstance(when, dict) or not isinstance(actions, list):
            raise ValueError(f"invalid rule {rid}")
        if set(when) - {"type", "gift_name", "min_count", "min_coins", "comment_contains"}:
            raise ValueError("unknown condition")
        for key in ("min_count", "min_coins"):
            if key in when and (type(when[key]) is not int or not 0 <= when[key] <= 1000000000):
                raise ValueError(f"{key} must be a nonnegative integer <= 1000000000")
        for key in ("type", "gift_name", "comment_contains"):
            if key in when and (not isinstance(when[key], str) or len(when[key]) > 2000):
                raise ValueError(f"invalid {key}")
        if when.get("type") and when["type"] not in {"gift", "comment", "like", "follow", "share", "subscription"}:
            raise ValueError("unsupported event type")
        if type(rule.get("enabled", True)) is not bool:
            raise ValueError("enabled must be a boolean")
        if len(actions) > 20:
            raise ValueError("maximum of 20 actions per rule")
        checked_actions = []
        for action in actions[:20]:
            if not isinstance(action, dict):
                raise ValueError("action must be an object")
            atype = str(action.get("type") or "").strip()
            if atype not in ALLOWED_ACTIONS:
                raise ValueError(f"unsupported action type: {atype}")
            for key in ("amount_per_count", "amount", "duration", "scale", "speed"):
                if key in action:
                    value = action[key]
                    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1000000000:
                        raise ValueError(f"invalid numeric action field: {key}")
            checked_actions.append(deepcopy(action))
        out.append({
            "id": rid,
            "enabled": bool(rule.get("enabled", True)),
            "when": dict(when),
            "actions": checked_actions,
        })
    return out


def _gift(event: dict[str, Any]) -> dict[str, Any]:
    return (event.get("data") or {}).get("gift") or {}


def _count(event: dict[str, Any]) -> int:
    data = event.get("data") or {}
    if event.get("type") == "gift":
        return int(data.get("repeat_count") or 1)
    if event.get("type") == "like":
        return int(data.get("count") or 1)
    return 1


def _coins(event: dict[str, Any]) -> int:
    if event.get("type") != "gift":
        return 0
    data = event.get("data") or {}
    gift = _gift(event)
    return int(gift.get("diamond_count") or 0) * int(data.get("repeat_count") or 1)


def matches(rule: dict[str, Any], event: dict[str, Any]) -> bool:
    if not rule.get("enabled", True):
        return False
    when = rule.get("when") or {}
    etype = str(when.get("type") or "")
    if etype and etype != event.get("type"):
        return False

    gift_name = when.get("gift_name")
    if gift_name:
        actual = str(_gift(event).get("name") or "")
        if actual.casefold() != str(gift_name).casefold():
            return False

    min_count = when.get("min_count")
    if min_count is not None and _count(event) < int(min_count):
        return False

    min_coins = when.get("min_coins")
    if min_coins is not None and _coins(event) < int(min_coins):
        return False

    contains = when.get("comment_contains")
    if contains:
        text = str((event.get("data") or {}).get("comment") or "")
        if str(contains).casefold() not in text.casefold():
            return False

    return True


def resolve_action(action: dict[str, Any], event: dict[str, Any], rule_id: str) -> dict[str, Any]:
    result = dict(action)
    result["rule_id"] = rule_id
    if "amount_per_count" in result:
        per = float(result.pop("amount_per_count"))
        result["amount"] = per * _count(event)
    result["event_type"] = event.get("type")
    user = (event.get("data") or {}).get("user")
    if user:
        result["user"] = user
    if event.get("type") == "gift":
        result["gift"] = _gift(event)
        result["gift_count"] = _count(event)
        result["gift_coins"] = _coins(event)
    return result


def actions_for(rules: list[dict[str, Any]], event: dict[str, Any]) -> list[dict[str, Any]]:
    actions = []
    for rule in rules:
        if matches(rule, event):
            for action in rule.get("actions") or []:
                actions.append(resolve_action(action, event, str(rule.get("id") or "")))
    return actions
