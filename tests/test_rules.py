from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rules import actions_for, get_profile, validate_rules


def test_dance_rose_and_giant():
    rules = validate_rules(get_profile("dance")["rules"])
    event = {
        "type": "gift",
        "data": {
            "user": {"unique_id": "tester"},
            "gift": {"name": "Rose", "diamond_count": 1},
            "repeat_count": 5,
        },
    }
    actions = actions_for(rules, event)
    kinds = [x["type"] for x in actions]
    assert "animation" in kinds
    assert "giant" in kinds


def test_kite_like_scales_with_count():
    rules = validate_rules(get_profile("kite")["rules"])
    event = {
        "type": "like",
        "data": {"user": {"unique_id": "tester"}, "count": 12},
    }
    actions = actions_for(rules, event)
    heal = next(a for a in actions if a["type"] == "heal")
    assert heal["amount"] == 12
