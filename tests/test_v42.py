import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from starlette.websockets import WebSocket
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import auth, live, main
from app.broadcast import Broadcaster
from app.config import settings
from app.live import LiveSession, LiveManager
from app.storage import EventStore

TOKEN = "game-token-with-at-least-32-characters"


def test_free_render_bridge_works_and_restarts_with_empty_storage(tmp_path, monkeypatch):
    cfg = replace(settings, database_path=str(tmp_path / "free-instance.sqlite"),
                  api_key="a" * 40, secret_key="b" * 40, dashboard_password="dashboard-password",
                  auto_connect="", game_tokens="{}", render=True, environment="production",
                  cookie_secure=True)
    for module in (auth, live, main):
        monkeypatch.setattr(module, "settings", cfg)
    monkeypatch.setattr(main, "manager", LiveManager())
    admin = {"X-API-Key": cfg.api_key}

    with TestClient(main.app) as client:
        assert client.get("/health").json()["ok"] is True
        login = client.post("/api/auth/login", json={"password": cfg.dashboard_password})
        assert login.status_code == 200
        assert "secure" in login.headers["set-cookie"].lower()
        registered = client.post("/api/bridge/register", headers=admin,
                                 json={"username": "host", "simulation": True})
        assert registered.status_code == 200, registered.text
        sid = registered.json()["session_id"]
        simulated = client.post(f"/api/live/{sid}/simulate", headers=admin,
                                json={"type": "gift", "gift_name": "Rose"})
        assert simulated.status_code == 200, simulated.text
        batch = client.get(f"/api/bridge/{sid}/poll?wait=0", headers=admin)
        assert batch.status_code == 200 and batch.json()["events"]
        cursor = batch.json()["cursor"]
        ack = client.post(f"/api/bridge/{sid}/ack", headers=admin, json={"cursor": cursor})
        assert ack.status_code == 200 and ack.json()["cursor"] == cursor

    # A new ephemeral instance has a different filesystem, as on Render Free.
    empty_cfg = replace(cfg, database_path=str(tmp_path / "next-instance.sqlite"))
    for module in (auth, live, main):
        monkeypatch.setattr(module, "settings", empty_cfg)
    monkeypatch.setattr(main, "manager", LiveManager())
    with TestClient(main.app) as client:
        assert client.get(f"/api/bridge/{sid}/poll?wait=0", headers=admin).status_code == 404
        fresh = client.post("/api/bridge/register", headers=admin,
                            json={"username": "host", "simulation": True, "resume": True})
        assert fresh.status_code == 200
        assert fresh.json()["session_id"] != sid


@pytest.fixture
def environment(tmp_path, monkeypatch):
    cfg = replace(settings, database_path=str(tmp_path / "events.sqlite"), api_key="admin",
                  secret_key="secret", auto_connect="", render=False, environment="development",
                  game_tokens=json.dumps({TOKEN: {"usernames": ["host"], "consumer_id": "game"}}))
    for module in (auth, live, main):
        monkeypatch.setattr(module, "settings", cfg)
    monkeypatch.setattr(main, "manager", LiveManager())

    async def fake_run(self):
        await asyncio.Event().wait()
    monkeypatch.setattr(LiveSession, "run", fake_run)
    return cfg


def test_durable_bridge_ack_restart_scoping_and_simulation(environment):
    async def check():
        async with main.lifespan(main.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as c:
                game = {"X-API-Key": TOKEN}
                admin = {"X-API-Key": "admin"}
                registered = await c.post("/api/bridge/register", headers=game,
                                          json={"username": "host", "simulation": True, "resume": True})
                assert registered.status_code == 200, registered.text
                sid = registered.json()["session_id"]
                s = main.manager.get(sid)
                assert s.client is None and s.task is None
                assert registered.json()["consumer_id"] == "game"
                forbidden = await c.post("/api/bridge/register", headers=game, json={"username": "other"})
                assert forbidden.status_code == 403
                assert (await c.post("/api/bridge/register", headers=game,
                                    json={"username": "host", "consumer_id": "imposter"})).status_code == 403
                for route, method, payload in [("simulate", "post", {"type": "gift"}), ("rules", "put", {"rules": []}),
                                                ("disconnect", "post", None), ("config", "put", {"config": {}})]:
                    assert (await c.request(method, f"/api/live/{sid}/{route}", headers=game, json=payload)).status_code == 401
                other = (await c.post("/api/live/connect", headers=admin, json={"username": "other", "simulation": True})).json()
                assert (await c.get(f'/api/bridge/{other["session_id"]}/poll?wait=0', headers=game)).status_code == 403
                assert (await c.get(f'/api/bridge/{sid}/poll?wait=0')).status_code == 401
                assert (await c.get('/api/bridge/nonexistent/poll?wait=0')).status_code == 401
                event1 = await s.push("gift", {"repeat_count": 2, "gift": {"diamond_count": 1}}, dedup_key="gift-1")
                event2 = await s.push("like", {"count": 3})
                batch = (await c.get(f"/api/bridge/{sid}/poll?wait=0&limit=1", headers=game)).json()
                assert batch["events"][0]["id"] == event1["id"] and batch["cursor"] == 1
                assert (await c.post(f"/api/bridge/{sid}/ack", headers=game, json={"cursor": 2})).status_code == 400
                assert (await c.post(f"/api/bridge/{sid}/ack", headers=game, json={"cursor": 1})).json()["cursor"] == 1
                assert (await c.post(f"/api/bridge/{sid}/ack", headers=game, json={"cursor": 0})).json()["cursor"] == 1
                metrics = (await c.get(f"/api/live/{sid}/metrics", headers=admin)).json()
                assert metrics["consumers"][0]["lag_events"] == 1
        # Reopen SQLite and rebuild the application state as a fresh process would.
        async with main.lifespan(main.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as c:
                result = (await c.post("/api/bridge/register", headers=game,
                                      json={"username": "host", "simulation": True, "resume": True})).json()
                assert result["session_id"] == sid and result["cursor"] == 1 and result["resumed"]
                s = main.manager.get(sid)
                assert await s.push("gift", {}, dedup_key="gift-1") is None
                result = (await c.get(f"/api/bridge/{sid}/poll?after=1&wait=0", headers=game)).json()
                assert result["events"][0]["id"] == event2["id"]
                # Legacy registration still starts at the end.
                legacy = (await c.post("/api/bridge/register", headers=admin,
                                      json={"username": "host", "simulation": True})).json()
                assert legacy["cursor"] == 2
    asyncio.run(check())


def test_state_rules_ranking_and_pause_survive_restart(environment):
    async def check():
        async with main.lifespan(main.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test",
                                         headers={"X-API-Key": "admin"}) as c:
                s = (await c.post('/api/live/connect', json={"username": "host", "simulation": True,
                                                               "profile": "kite"})).json()
                sid = s['session_id']
                assert (await c.put(f'/api/live/{sid}/config', json={"config": {"like_enabled": False}})).status_code == 200
                custom = [{"id": "rose", "when": {"type": "gift", "gift_name": "Rose"},
                           "actions": [{"type": "heal", "amount": 5}]}]
                assert (await c.put(f'/api/live/{sid}/rules', json={"rules": custom})).status_code == 200
                session = main.manager.get(sid)
                await session.push("gift", {"user": {"user_id": "42", "unique_id": "viewer",
                                                          "avatar_url": "https://example.test/a"},
                                            "gift": {"name": "Rose", "diamond_count": 1}, "repeat_count": 2})
                await session.push("perception", {"payload": {"message": "alert"}},
                                   safety_meta={"level": "CRITICAL", "auto_pause": True})
        async with main.lifespan(main.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test",
                                         headers={"X-API-Key": "admin"}) as c:
                status = (await c.get(f'/api/live/{sid}/status')).json()
                assert status['config']['like_enabled'] is False
                assert status['rules'][0]['id'] == custom[0]['id'] and status['rules'][0]['enabled'] is True
                assert status['automation_paused'] is True
                assert status['stats']['gifts'] == 2 and status['latest_seq'] == 2
                rank = (await c.get(f'/api/live/{sid}/leaderboard')).json()['rows'][0]
                assert rank['user_id'] == '42' and rank['coins'] == 2
                events = (await c.get(f'/api/live/{sid}/events?tail=true')).json()['events']
                assert events[0]['type'] == 'gift' and events[1]['data'] == {}
                assert (await c.post(f'/api/live/{sid}/safety/reset')).status_code == 200
                assert (await c.get(f'/api/live/{sid}/status')).json()['automation_paused'] is False
    asyncio.run(check())


def test_retention_and_noise_do_not_evict_gifts(environment):
    async def check():
        async with main.lifespan(main.app):
            s, _ = await main.manager.connect("host", simulation=True)
            first = await s.push("gift", {"repeat_count": 1, "gift": {"diamond_count": 1}})
            from collections import deque
            s.events = deque(maxlen=2)
            for i in range(10):
                await s.push("notice", {"payload": {"detail": str(i)}})
            assert s.read_events(0)[0]["id"] == first["id"]
            assert s.read_events(1)[0]["data"] == {}
            with s.store.db:
                s.store.db.execute("UPDATE events SET timestamp=0 WHERE sid=? AND seq=1", (s.session_id,))
            s.store.prune()
            assert s.oldest_seq() == 2
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test",
                                         headers={"X-API-Key": TOKEN}) as c:
                result = (await c.get(f"/api/bridge/{s.session_id}/poll?wait=0&recover=true&limit=2")).json()
                assert result["gap_detected"] and result["cursor"] == 3
    asyncio.run(check())


def test_failed_commit_does_not_publish_or_deduplicate(tmp_path, monkeypatch):
    async def check():
        store = EventStore(str(tmp_path / "events.sqlite"))
        s = LiveSession("s", "host", store=store)
        s.persist()
        save = store.save
        def fail(*args, **kwargs):
            raise OSError("disk full")
        monkeypatch.setattr(store, "save", fail)
        with pytest.raises(OSError):
            await s.push("like", {"count": 2}, dedup_key="id")
        assert s.seq == 0 and not s.events and s.stats["likes"] == 0 and not s.dedup_set
        monkeypatch.setattr(store, "save", save)
        assert (await s.push("like", {"count": 2}, dedup_key="id"))["seq"] == 1
        store.close()
    asyncio.run(check())


def test_leases_pin_reuse_and_capacity(environment, monkeypatch):
    monkeypatch.setattr(live, "settings", replace(environment, max_sessions=1))
    async def check():
        m = LiveManager()
        s, _ = await m.connect("host")
        task = s.task
        assert (await m.connect("host"))[0].task is task
        with pytest.raises(RuntimeError):
            await m.connect("other")
        # Simulation doesn't need a TikTok slot.
        sim, _ = await m.connect("sim", simulation=True)
        assert sim.task is None and sim.client is None
        s.last_activity = 0
        s.pinned = True
        await m.reap()
        assert s.task is task
        s.pinned = False
        s.last_activity = live.now_ms() - (environment.consumer_timeout_seconds+1)*1000
        await m.reap()
        assert s.task is None and s.state["connection_state"] == "dormant"
        resumed, created = await m.connect("host")
        assert not created and resumed.session_id == s.session_id and resumed.task is not None
        await m.stop_all()
    asyncio.run(check())


def test_dormant_session_count_is_bounded(environment, monkeypatch):
    monkeypatch.setattr(live, "settings", replace(environment, max_stored_sessions=1))
    async def check():
        m = LiveManager()
        s, _ = await m.connect('host', simulation=True)
        with pytest.raises(RuntimeError, match='Stored session limit'):
            await m.connect('other', simulation=True)
        assert (await m.connect('host', simulation=True))[0] is s
    asyncio.run(check())


class Socket:
    def __init__(self, slow=False):
        self.sent = []
        self.closed = False
        self.slow = slow

    async def send_json(self, data):
        if self.slow:
            await asyncio.Event().wait()
        self.sent.append(data)

    async def send_text(self, data):
        await self.send_json(data)

    async def close(self, code=1000):
        self.closed = True


def test_slow_websocket_timeout_filter_coalescing_and_cleanup():
    async def check():
        b = Broadcaster(queue_size=2, timeout=.03)
        slow, fast = Socket(True), Socket()
        await b.add(slow)
        slow_task = b.clients[slow].task
        await b.add(fast, {"s"})
        await b.send({"kind": "event", "event": {"session_id": "s", "seq": 1}})
        await asyncio.sleep(.01)
        assert fast.sent and not slow.sent
        await b.send({"kind": "event", "event": {"session_id": "other", "seq": 2}})
        for i in range(100):
            await b.send({"kind": "status", "session": {"session_id": "s", "viewers": i}})
        assert b.clients[fast].queue.qsize() <= 2
        await asyncio.wait_for(slow_task, 1)
        assert slow.closed and slow not in b.clients
        assert fast.sent[-1]["session"]["viewers"] == 99
        assert len(fast.sent) == 2
        await b.close()
        assert not b.clients and fast.closed
    asyncio.run(check())


def test_websocket_overflow_is_bounded():
    async def check():
        b = Broadcaster(queue_size=2, timeout=.01)
        ws = Socket(True)
        await b.add(ws)
        task = b.clients[ws].task
        for i in range(1000):
            await b.send({"kind": "event", "event": {"seq": i}})
        assert b.dropped_clients == 1
        await task
        assert ws.closed and not b.clients
    asyncio.run(check())


def test_origin_cookie_and_query_key(environment):
    token = auth.make_session_token()
    def socket(origin=None, key=None):
        headers = [(b'host', b'test'), (b'cookie', f'{auth.COOKIE_NAME}={token}'.encode())]
        if origin:
            headers.append((b'origin', origin.encode()))
        return WebSocket({"type": "websocket", "scheme": "ws", "path": "/ws", "server": ("test", 80),
                          "query_string": b'key=admin' if key else b'', "headers": headers}, None, None)
    assert auth.websocket_authorized(socket('http://test'))
    assert not auth.websocket_authorized(socket('https://evil.example'))
    assert not auth.websocket_authorized(socket())
    assert not auth.websocket_authorized(socket('http://test', True))
    assert not auth.verify_session_token('invalid.💥')


def test_login_limit_and_secure_cookie(environment, monkeypatch):
    monkeypatch.setattr(auth, "settings", replace(environment, cookie_secure=True, login_attempts=2))
    monkeypatch.setattr(main, "login_limiter", auth.LoginLimiter())
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="https://test") as c:
            good = await c.post('/api/auth/login', json={"password": "admin"})
            assert good.status_code == 200 and "Secure" in good.headers['set-cookie']
            assert (await c.post('/api/auth/login', json={"password": "bad"})).status_code == 401
            limited = await c.post('/api/auth/login', json={"password": "bad"})
            assert limited.status_code == 429 and limited.headers['retry-after']
    asyncio.run(check())


def test_retry_backoff_and_provider_deadline(monkeypatch):
    monkeypatch.setattr(live.random, "uniform", lambda low, high: low)
    s = LiveSession("s", "host")
    first = s.retry_delay()
    assert s.retry_delay() > first
    s.retry_count = 0
    assert s.retry_delay() == first
    assert s.retry_delay(rate_limit=3600) >= 3600
    assert s.retry_delay(offline=True) <= settings.offline_max_seconds


def test_real_protobuf_avatar_shape():
    from TikTokLive.events import CommentEvent
    from TikTokLive.proto.custom_proto import ExtendedUser
    user = ExtendedUser.from_dict({"id": "42", "displayId": "viewer", "avatarThumb": {"urlList": ["https://example.test/photo"]}})
    event = CommentEvent(user=user, content="hello")
    assert live.event_user(event)["avatar_url"] == "https://example.test/photo"


def test_json_response_schemas_are_declared():
    for route in main.app.routes:
        if getattr(route, "path", "").startswith(("/api/", "/health")) and not route.path.endswith(".csv"):
            assert route.response_model is not None, route.path


def test_large_json_is_rejected_before_parsing(environment):
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test",
                                     headers={"X-API-Key": "admin"}) as c:
            response = await c.post('/api/live/connect', content=b'{' + b' ' * (environment.max_json_bytes + 1),
                                    headers={"Content-Type": "application/json"})
            assert response.status_code == 413
            async def chunks():
                yield b'{'
                yield b' ' * (environment.max_json_bytes + 1)
            streamed = await c.post('/api/live/connect', content=chunks(),
                                    headers={"Content-Type": "application/json"})
            assert streamed.status_code == 413
    asyncio.run(check())


def test_websocket_route_auth_filter_and_ping(environment):
    with TestClient(main.app) as c:
        spec = c.get('/openapi.json')
        assert spec.status_code == 200 and 'Poll' in spec.json()['components']['schemas']
        headers = {"X-API-Key": "admin"}
        first = c.post('/api/live/connect', headers=headers,
                       json={"username": "host", "simulation": True}).json()['session_id']
        other = c.post('/api/live/connect', headers=headers,
                       json={"username": "other", "simulation": True}).json()['session_id']
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect('/ws?key=admin', headers=headers):
                pass
        with c.websocket_connect(f'/ws?session_id={first}', headers=headers) as ws:
            assert ws.receive_json()['kind'] == 'hello'
            c.post(f'/api/live/{other}/simulate', headers=headers, json={"type": "like"})
            c.post(f'/api/live/{first}/simulate', headers=headers, json={"type": "gift"})
            event = ws.receive_json()
            assert event['kind'] == 'event' and event['event']['session_id'] == first
            ws.send_text('ping')
            assert ws.receive_text() == 'pong'
