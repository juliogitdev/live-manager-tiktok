import asyncio
from collections import deque
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from app import auth, live, main
from app.config import settings
from app.live import LiveManager, LiveSession, event_user, safe_value


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(auth, "settings", replace(settings, api_key="test-key", secret_key="test-secret"))
    monkeypatch.setattr(main, "manager", LiveManager())

    async def no_network(self):
        pass

    monkeypatch.setattr(LiveSession, "run", no_network)

    async def scenario(callback):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test",
                                     headers={"X-API-Key": "test-key"}) as client:
            await callback(client)
        await main.manager.stop_all()

    return lambda callback: asyncio.run(scenario(callback))


def test_deployed_version_and_assets(api):
    async def check(c):
        assert (await c.get("/health")).json()["version"] == "4.2.0"
        assert (await c.get("/api/meta")).json()["version"] == "4.2.0"
        assert "monitorFilter" in (await c.get("/")).text
        assert "safety_auto_pause_critical" in (await c.get("/static/app.js")).text
    api(check)


def test_shared_profile_preserved_and_explicit_admin_change(api):
    async def check(c):
        first = (await c.post("/api/bridge/register", json={"username": "Host", "profile": "kite", "consumer_id": "a"})).json()
        s = main.manager.get(first["session_id"])
        original_rules = s.rules
        task = s.task
        second = (await c.post("/api/bridge/register", json={"username": "@host", "profile": "raw", "consumer_id": "b"})).json()
        assert second["profile_conflict"] and second["profile"] == "kite"
        assert second["session_id"] == first["session_id"] and not second["created"]
        assert s.rules is original_rules and s.task is task and len(main.manager.sessions) == 1
        assert (await c.post("/api/bridge/register", json={"username": "host", "profile": "typo"})).status_code == 422
        assert (await c.post(f"/api/live/{s.session_id}/profile", json={"profile": "raw"})).status_code == 200
        assert s.rules == []
    api(check)


def test_bridge_pagination_retry_gap_and_restart(api):
    async def check(c):
        row = (await c.post("/api/bridge/register", json={"username": "host"})).json()
        sid = row["session_id"]
        s = main.manager.get(sid)
        s.events = deque(maxlen=3)
        for i in range(3):
            await s.push("gift", {"repeat_count": 1, "gift": {"diamond_count": 1}}, dedup_key=str(i))
        url = f"/api/bridge/{sid}/poll?after=0&wait=0&limit=1"
        batch = (await c.get(url)).json()
        assert batch["cursor"] == 1 and batch["latest_seq"] == 3
        assert (await c.get(url)).json()["events"][0]["id"] == batch["events"][0]["id"]
        assert await s.push("gift", {}, dedup_key="2") is None
        await s.push("notice", {"payload": {"private": "data"}})
        expired = (await c.get(url)).json()
        assert expired["cursor_expired"] and expired["gap_detected"]
        assert expired["events"] == [] and expired["cursor"] == 4 and expired["oldest_seq"] == 2
        recovered = (await c.get(url + "&recover=true")).json()
        assert recovered["gap_detected"] and recovered["cursor"] == 2
        assert recovered["events"][0]["seq"] == 2
        tail = (await c.get(f"/api/bridge/{sid}/poll?after=3&wait=0")).json()
        assert tail["events"][0]["data"] == {}
        empty = (await c.get(f"/api/bridge/{sid}/poll?after=4&wait=0.001")).json()
        assert empty["events"] == [] and empty["cursor"] == 4 and empty["session_id"] == sid
        await main.manager.disconnect(sid)
        assert (await c.get(url)).status_code == 404
        new = (await c.post("/api/bridge/register", json={"username": "host"})).json()
        assert new["session_id"] != sid and new["cursor"] == 0
    api(check)


def test_atomic_validation_and_pause_context(api):
    async def check(c):
        row = (await c.post("/api/bridge/register", json={"username": "host", "profile": "kite"})).json()
        sid = row["session_id"]
        s = main.manager.get(sid)
        rules = s.rules
        invalid = [{"when": {"min_coins": "invalid"}, "actions": []}]
        assert (await c.put(f"/api/live/{sid}/rules", json={"rules": invalid})).status_code == 400
        assert s.rules is rules
        assert (await c.put(f"/api/live/{sid}/config", json={"config": {"gift_enabled": "false"}})).status_code == 422
        assert (await c.put(f"/api/live/{sid}/config", json={"config": {"gift_enabled": False, "typo": True}})).status_code == 400
        assert s.config["gift_enabled"] is True
        assert (await c.put(f"/api/live/{sid}/config", json={"config": {"safety_auto_pause_high": False}})).status_code == 200
        assert s.config["safety_auto_pause_critical"] is False
        s.state["automation_paused"] = True
        paused = await s.push("like", {"count": 2})
        assert paused["automation_paused_at_ingest"] and paused["actions"] == []
        await c.post(f"/api/live/{sid}/safety/reset")
        allowed = await s.push("like", {"count": 2})
        assert not allowed["automation_paused_at_ingest"] and allowed["actions"]
        batch = (await c.get(f"/api/bridge/{sid}/poll?after=0&wait=0")).json()
        assert not batch["automation_paused"]
        assert batch["events"][0]["automation_paused_at_ingest"]
        assert batch["events"][1]["type"] == "safety_reset"
    api(check)


@pytest.mark.parametrize("store", [True, False])
@pytest.mark.parametrize("age,content,expected", [(0, "restriction", "CRITICAL"), (600000, "restriction", "HISTORICAL"), (0, "", "OBSERVATION")])
def test_diagnostic_ingestion_independent_of_raw_storage(monkeypatch, store, age, content, expected):
    monkeypatch.setattr(live, "settings", replace(settings, store_raw_diagnostics=store))
    timestamp = live.now_ms()
    event = SimpleNamespace(common=SimpleNamespace(create_time=timestamp-age), message=content)

    async def check():
        s = LiveSession("s", "host")
        s.state["connected_at"] = timestamp - 30000
        result = await s.process_diagnostic(event, "perception", "strong")
        assert result["safety"]["level"] == expected
        assert result["safety"]["severity"] == {"CRITICAL": "high", "HISTORICAL": "info", "OBSERVATION": "medium"}[expected]
        assert s.state["automation_paused"] == (expected == "CRITICAL")
        assert ("payload" in result["data"]) == store
    asyncio.run(check())


def test_redaction_and_avatar():
    payload = {"nested": {"a": {"b": {"token": "NEVER_EXPORT"}}}}
    assert "NEVER_EXPORT" not in str(safe_value(payload))
    user = SimpleNamespace(id=42, avatar_thumb=SimpleNamespace(url_list=[]),
                           avatar_medium=SimpleNamespace(url_list=["https://example.test/avatar"]))
    assert event_user(SimpleNamespace(user=user))["avatar_url"] == "https://example.test/avatar"
    assert event_user(SimpleNamespace(user=SimpleNamespace(id=1)))["avatar_url"] is None


def test_missing_signing_secret_fails(monkeypatch):
    monkeypatch.setattr(auth, "settings", replace(settings, api_key="", secret_key="", dashboard_password="password"))
    with pytest.raises(RuntimeError, match="requires"):
        auth.validate_secrets()
    with pytest.raises(RuntimeError, match="required"):
        auth.make_session_token()


def test_production_startup_rejects_missing_secrets(monkeypatch):
    monkeypatch.setattr(auth, "settings", replace(settings, render=True, api_key="", secret_key=""))

    async def check():
        with pytest.raises(RuntimeError, match="Render requires"):
            async with main.lifespan(main.app):
                pass
    asyncio.run(check())


def test_gap_created_while_poll_waits(api):
    async def check(c):
        row = (await c.post("/api/bridge/register", json={"username": "host"})).json()
        sid = row["session_id"]
        s = main.manager.get(sid)
        s.events = deque(maxlen=1)
        pending = asyncio.create_task(c.get(f"/api/bridge/{sid}/poll?after=0&wait=1"))
        await asyncio.sleep(0)
        await s.push("like", {"count": 1})
        await s.push("like", {"count": 1})
        result = (await pending).json()
        assert result["gap_detected"] and result["cursor_expired"] and result["oldest_seq"] == 2
    api(check)


def test_gift_combo_and_avatar_normalization():
    class FakeClient:
        def __init__(self):
            self.handlers = {}

        def on(self, event_type):
            def register(callback):
                self.handlers[event_type] = callback
                return callback
            return register

        def add_listener(self, event_type, callback):
            self.handlers[event_type] = callback

    async def check():
        s = LiveSession("s", "host")
        s.client = FakeClient()
        s.register_handlers()
        event = SimpleNamespace(gift=SimpleNamespace(id=1, type=1, name="Rose", diamond_count=1),
                                streaking=True, repeat_count=5, repeat_end=0,
                                common=SimpleNamespace(msg_id=123),
                                user=SimpleNamespace(id=42, avatar_thumb=SimpleNamespace(url_list=["https://example.test/a"])))
        handler = s.client.handlers[live.tt_events.GiftEvent]
        await handler(event)
        assert s.seq == 0
        event.streaking = False
        event.repeat_end = 1
        await handler(event)
        await handler(event)
        assert s.seq == 1 and s.stats["gift_coins"] == 5
        assert s.events[0]["data"]["user"]["avatar_url"] == "https://example.test/a"
        assert s.leaderboard()[0]["user_id"] == "42"
    asyncio.run(check())


@pytest.mark.parametrize("value", ["invalid", True, float("inf"), float("nan")])
def test_invalid_action_number_preserves_rules(value):
    s = LiveSession("s", "host", "kite")
    rules = s.rules
    with pytest.raises(ValueError):
        s.set_rules([{"when": {}, "actions": [{"type": "heal", "amount_per_count": value}]}])
    assert s.rules is rules
