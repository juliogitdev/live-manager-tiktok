import asyncio
import hashlib
import sqlite3
from dataclasses import replace

import httpx
import pytest
from starlette.testclient import TestClient

from app import auth, live, main
from app.config import settings
from app.live import LiveManager
from app.storage import EventStore


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    config = replace(settings, database_path=str(tmp_path / "access.sqlite"),
                     api_key="admin-key", secret_key="independent-signing-key", dashboard_password="panel-password",
                     auto_connect="", render=False, environment="development", cookie_secure=False, game_tokens="{}")
    for module in (auth, live, main):
        monkeypatch.setattr(module, "settings", config)
    monkeypatch.setattr(main, "manager", LiveManager())
    return config


def test_database_credentials_scope_hash_persistence_update_and_revocation(configuration):
    admin = {"X-API-Key": configuration.api_key}
    with TestClient(main.app) as client:
        assert client.get("/api/game-access").status_code == 401
        created = client.post("/api/game-access", headers=admin, json={
            "name": "Race phone", "consumer_id": "race-phone", "usernames": ["@HOST", "host", "second"]})
        assert created.status_code == 201, created.text
        assert created.headers["cache-control"] == "no-store"
        data = created.json()
        token, credential = data["token"], data["credential"]
        cid = credential["id"]
        assert credential["usernames"] == ["host", "second"]
        assert len(token) >= 64 and credential["active"]
        with sqlite3.connect(configuration.database_path) as database:
            raw = database.execute("SELECT token_hash FROM game_credentials WHERE id=?", (cid,)).fetchone()[0]
        assert raw == hashlib.sha256(token.encode()).hexdigest() and token != raw
        listed = client.get("/api/game-access", headers=admin)
        assert token not in listed.text and "token_hash" not in listed.text
        game = {"X-API-Key": token}
        for path, method, body in [("/api/game-access", "get", None), ("/api/game-access", "post", {
                "name": "Forbidden", "consumer_id": "bad", "usernames": ["host"]}),
                (f"/api/game-access/{cid}", "patch", {"usernames": ["other"]}),
                (f"/api/game-access/{cid}", "delete", None)]:
            assert client.request(method, path, headers=game, json=body).status_code == 401
        registered = client.post("/api/bridge/register", headers=game, json={"username": "HOST", "simulation": True})
        assert registered.status_code == 200, registered.text
        assert registered.json()["consumer_id"] == "race-phone"
        sid = registered.json()["session_id"]
        assert client.post("/api/bridge/register", headers=game, json={"username": "other"}).status_code == 403
        assert client.post("/api/bridge/register", headers=game, json={"username": "host", "consumer_id": "imposter"}).status_code == 403
        assert client.post(f"/api/live/{sid}/simulate", headers=game, json={"type": "gift"}).status_code == 401
        changed = client.patch(f"/api/game-access/{cid}", headers=admin, json={"usernames": ["second"]})
        assert changed.status_code == 200 and token not in changed.text
        assert client.get(f"/api/bridge/{sid}/poll?wait=0", headers=game).status_code == 403

    with TestClient(main.app) as client:
        listed = client.get("/api/game-access", headers=admin).json()["credentials"]
        assert listed[0]["id"] == cid and listed[0]["usernames"] == ["second"]
        assert client.post("/api/bridge/register", headers=game, json={"username": "second", "simulation": True}).status_code == 200
        revoked = client.delete(f"/api/game-access/{cid}", headers=admin)
        assert revoked.status_code == 200 and revoked.json()["credential"]["revoked_at"]
        assert not revoked.json()["credential"]["active"] and token not in revoked.text
        assert client.post("/api/bridge/register", headers=game, json={"username": "second", "simulation": True}).status_code == 401
        assert client.patch(f"/api/game-access/{cid}", headers=admin, json={"name": "Resurrect"}).status_code == 409
        assert client.delete(f"/api/game-access/{cid}", headers=admin).status_code == 200


def test_cookie_administrator_and_input_validation(configuration):
    with TestClient(main.app) as client:
        assert client.post("/api/auth/login", json={"password": configuration.dashboard_password}).status_code == 200
        for body in [{"name": "x", "consumer_id": "x", "usernames": ["*"]},
                     {"name": "x", "consumer_id": "x", "usernames": []},
                     {"name": " ", "consumer_id": "x", "usernames": ["host"]}]:
            assert client.post("/api/game-access", json=body).status_code == 422
        created = client.post("/api/game-access", json={"name": "Browser", "consumer_id": "browser", "usernames": ["host"]})
        assert created.status_code == 201
        cid = created.json()["credential"]["id"]
        for body in [{"name": None}, {"consumer_id": None}, {"usernames": None}, {"usernames": ["@"]}, {"token": "secret"}]:
            assert client.patch(f"/api/game-access/{cid}", json=body).status_code == 422
        assert client.delete("/api/game-access/missing").status_code == 404


def test_revocation_during_long_poll_blocks_delivery(configuration):
    async def scenario():
        async with main.lifespan(main.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                admin = {"X-API-Key": configuration.api_key}
                created = (await client.post("/api/game-access", headers=admin, json={
                    "name": "Waiting", "consumer_id": "waiter", "usernames": ["host"]})).json()
                game = {"X-API-Key": created["token"]}
                registered = (await client.post("/api/bridge/register", headers=game, json={"username": "host", "simulation": True})).json()
                sid = registered["session_id"]
                waiting = asyncio.create_task(client.get(f"/api/bridge/{sid}/poll?after={registered['cursor']}&wait=2", headers=game))
                await asyncio.sleep(0.05)
                await client.delete(f"/api/game-access/{created['credential']['id']}", headers=admin)
                await client.post(f"/api/live/{sid}/simulate", headers=admin, json={"type": "gift"})
                response = await waiting
                assert response.status_code == 401, response.text
    asyncio.run(scenario())
