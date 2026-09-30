from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.diagnostics import classify_diagnostic, payload_has_meaningful_content


def classify(policy, connected, received, event_time, meaningful, corroborated=None):
    return classify_diagnostic(
        policy=policy,
        connected_at_ms=connected,
        received_at_ms=received,
        event_time_ms=event_time,
        meaningful=meaningful,
        historical_grace_ms=15_000,
        fresh_max_age_ms=120_000,
        startup_quarantine_ms=20_000,
        corroborated_by=corroborated,
    )


def test_empty_dynamic_restriction_is_not_meaningful():
    payload = {
        "common": {"method": "WebcastGiftDynamicRestrictionMessage", "createTime": "1000"},
        "dynamicRestriction": {},
    }
    assert payload_has_meaningful_content(payload) is False


def test_old_restriction_becomes_historical():
    connected = 2_000_000
    received = 2_000_100
    event_time = connected - (20 * 60 * 1000)
    result = classify("watch", connected, received, event_time, False)
    assert result["level"] == "HISTORICAL"
    assert result["auto_pause"] is False


def test_current_empty_restriction_is_observation():
    connected = 2_000_000
    received = 2_030_000
    event_time = 2_029_000
    result = classify("watch", connected, received, event_time, False)
    assert result["level"] == "OBSERVATION"
    assert result["auto_pause"] is False


def test_current_meaningful_watch_is_alert_only():
    connected = 2_000_000
    received = 2_030_000
    event_time = 2_029_000
    result = classify("watch", connected, received, event_time, True)
    assert result["level"] == "ALERT"
    assert result["auto_pause"] is False


def test_current_meaningful_strong_signal_is_critical():
    connected = 2_000_000
    received = 2_030_000
    event_time = 2_029_000
    result = classify("strong", connected, received, event_time, True)
    assert result["level"] == "CRITICAL"
    assert result["auto_pause"] is True


def test_two_different_alerts_can_corroborate_to_critical():
    connected = 2_000_000
    received = 2_030_000
    event_time = 2_029_000
    result = classify("watch", connected, received, event_time, True, "access_control")
    assert result["level"] == "CRITICAL"
    assert result["auto_pause"] is True
