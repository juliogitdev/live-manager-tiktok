import asyncio
import csv
import io
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import (
    check_password, clear_login_cookie, require_api_key,
    require_auth, set_login_cookie, verify_session_token,
    websocket_authorized,
)
from .config import settings
from .live import broadcaster, manager
from .rules import BUILTIN_PROFILES, validate_rules

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("live-manager")

# Optional Euler Stream signing key for higher community signing limits.
if settings.euler_api_key:
    try:
        from TikTokLive.client.web.web_settings import WebDefaults
        WebDefaults.tiktok_sign_api_key = settings.euler_api_key
    except Exception:
        log.exception("Could not configure EULER_API_KEY")


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=300)


class ConnectRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    profile: str = "raw"


class SimulationRequest(BaseModel):
    type: str
    username: str = "Teste"
    gift_name: str = "Rose"
    gift_coins: int = Field(default=1, ge=0, le=1000000)
    count: int = Field(default=1, ge=1, le=1000000)
    comment: str = "teste"


class ConfigRequest(BaseModel):
    config: dict[str, Any]


class RulesRequest(BaseModel):
    rules: list[dict[str, Any]]


class ProfileRequest(BaseModel):
    profile: str


class BridgeRegisterRequest(BaseModel):
    username: str
    profile: str = "raw"
    consumer_id: str = "roblox"


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(manager.auto_connect())
    yield
    await manager.stop_all()


app = FastAPI(
    title="TikTok → Roblox Live Manager",
    version=settings.app_version,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory="static"), name="static")


def get_session(sid: str):
    try:
        return manager.get(sid)
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")


@app.get("/")
async def dashboard():
    return FileResponse("static/index.html")


@app.get("/health")
async def health():
    # Render health check: app health, not TikTok connection state.
    return {
        "ok": True,
        "version": settings.app_version,
        "sessions": len(manager.sessions),
        "max_sessions": settings.max_sessions,
    }


@app.get("/api/auth/status")
async def auth_status(request: Request):
    return {
        "authenticated": verify_session_token(request.cookies.get("live_manager_session")),
        "password_configured": bool(settings.dashboard_password or settings.api_key),
    }


@app.post("/api/auth/login")
async def login(body: LoginRequest):
    if not check_password(body.password):
        await asyncio.sleep(0.35)
        raise HTTPException(status_code=401, detail="Invalid password")
    response = JSONResponse({"ok": True})
    set_login_cookie(response)
    return response


@app.post("/api/auth/logout")
async def logout():
    response = JSONResponse({"ok": True})
    clear_login_cookie(response)
    return response


@app.get("/api/meta")
async def meta(request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    return {
        "version": settings.app_version,
        "max_sessions": settings.max_sessions,
        "profiles": BUILTIN_PROFILES,
        "render_free_note": "On Render Free, active Roblox polling or dashboard WebSocket traffic keeps the service receiving inbound traffic.",
    }


@app.get("/api/live/sessions")
async def list_sessions(request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    return {"sessions": [s.snapshot() for s in manager.sessions.values()]}


@app.post("/api/live/connect")
async def connect_live(body: ConnectRequest, request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    try:
        session, created = await manager.connect(body.username, body.profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"created": created, **session.snapshot()}


@app.post("/api/live/{sid}/disconnect")
async def disconnect_live(sid: str, request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    get_session(sid)
    await manager.disconnect(sid)
    return {"ok": True}


@app.get("/api/live/{sid}/status")
async def session_status(sid: str, request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    return get_session(sid).snapshot()


@app.get("/api/live/{sid}/events")
async def session_events(
    sid: str,
    request: Request,
    after: int = Query(0, ge=0),
    limit: int = Query(150, ge=1, le=500),
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    s = get_session(sid)
    rows = [e for e in s.events if e["seq"] > after][:limit]
    return {
        "events": rows,
        "latest_seq": s.seq,
        "oldest_seq": s.events[0]["seq"] if s.events else s.seq,
        "has_more": bool(rows and rows[-1]["seq"] < s.seq),
    }


@app.put("/api/live/{sid}/config")
async def update_config(
    sid: str,
    body: ConfigRequest,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    s = get_session(sid)
    allowed = {
        "comment_enabled", "like_enabled", "follow_enabled",
        "share_enabled", "gift_enabled", "safety_auto_pause_high",
        "count_simulations_in_stats",
    }
    for key, value in body.config.items():
        if key in allowed:
            s.config[key] = bool(value)
    return s.config


@app.put("/api/live/{sid}/rules")
async def update_rules(
    sid: str,
    body: RulesRequest,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    s = get_session(sid)
    try:
        s.set_rules(body.rules)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "rules": s.rules}


@app.post("/api/live/{sid}/profile")
async def apply_profile(
    sid: str,
    body: ProfileRequest,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    s = get_session(sid)
    s.apply_profile(body.profile)
    return {"ok": True, "profile": s.profile_name, "rules": s.rules}


@app.post("/api/live/{sid}/safety/reset")
async def reset_safety(sid: str, request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    s = get_session(sid)
    s.state["safety_state"] = "NORMAL"
    s.state["last_safety_event"] = None
    s.state["automation_paused"] = False
    return {"ok": True}


@app.post("/api/live/{sid}/simulate")
async def simulate(
    sid: str,
    body: SimulationRequest,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    s = get_session(sid)
    typ = body.type.strip().lower()
    if typ not in {"gift", "comment", "like", "follow", "share", "subscription"}:
        raise HTTPException(status_code=400, detail="Unsupported simulation type")

    user = {
        "unique_id": body.username,
        "nickname": body.username,
        "user_id": f"simulation:{body.username}",
    }
    if typ == "gift":
        data = {
            "user": user,
            "gift": {
                "id": "simulation",
                "name": body.gift_name,
                "type": 0,
                "diamond_count": body.gift_coins,
            },
            "repeat_count": body.count,
            "repeat_end": 1,
        }
    elif typ == "comment":
        data = {"user": user, "comment": body.comment}
    elif typ == "like":
        data = {"user": user, "count": body.count, "total": None}
    else:
        data = {"user": user}

    event = await s.push(typ, data, source="simulation")
    return event


@app.get("/api/live/{sid}/leaderboard")
async def leaderboard(
    sid: str,
    request: Request,
    sort: str = "coins",
    limit: int = Query(50, ge=1, le=100),
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    s = get_session(sid)
    return {"rows": s.leaderboard(sort, limit)}


@app.get("/api/live/{sid}/export.json")
async def export_json(sid: str, request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    s = get_session(sid)
    return {
        "session": s.snapshot(),
        "events": list(s.events),
        "leaderboard": s.leaderboard("coins", 100),
    }


@app.get("/api/live/{sid}/export.csv")
async def export_csv(sid: str, request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    s = get_session(sid)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["seq", "timestamp", "type", "source", "user", "detail", "actions"])
    for e in s.events:
        data = e.get("data") or {}
        user = (data.get("user") or {}).get("unique_id") or ""
        if e["type"] == "comment":
            detail = data.get("comment") or ""
        elif e["type"] == "gift":
            gift = data.get("gift") or {}
            detail = f'{gift.get("name","")} x{data.get("repeat_count",1)} ({gift.get("diamond_count",0)} coins each)'
        else:
            detail = ""
        writer.writerow([
            e["seq"], e["timestamp"], e["type"], e["source"],
            user, detail, json.dumps(e.get("actions") or [], ensure_ascii=False),
        ])
    data = buf.getvalue().encode("utf-8-sig")
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{s.username}-events.csv"'},
    )


# Roblox-facing API: API key only, stateless and restart-safe.
@app.post("/api/bridge/register")
async def bridge_register(body: BridgeRegisterRequest, x_api_key: str | None = Header(default=None)):
    require_api_key(x_api_key)
    try:
        s, created = await manager.connect(body.username, body.profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "ok": True,
        "created": created,
        "session_id": s.session_id,
        "username": s.username,
        "profile": s.profile_name,
        # Start at current sequence to avoid replaying old gifts after Roblox restarts.
        "cursor": s.seq,
        "state": s.state["connection_state"],
        "automation_paused": s.state["automation_paused"],
    }


@app.get("/api/bridge/{sid}/poll")
async def bridge_poll(
    sid: str,
    after: int = Query(0, ge=0),
    wait: float = Query(8.0, ge=0, le=15),
    limit: int = Query(100, ge=1, le=250),
    x_api_key: str | None = Header(default=None),
):
    require_api_key(x_api_key)
    s = get_session(sid)

    oldest = s.events[0]["seq"] if s.events else s.seq
    if s.events and after < oldest - 1:
        return {
            "ok": True,
            "cursor_expired": True,
            "events": [],
            "cursor": s.seq,
            "latest_seq": s.seq,
            "automation_paused": s.state["automation_paused"],
        }

    rows = await s.wait_for_events(after, wait)
    rows = rows[:limit]

    # Roblox gets gameplay-sized payloads, not raw diagnostic payloads.
    bridge_rows = []
    for e in rows:
        bridge_rows.append({
            "seq": e["seq"],
            "id": e["id"],
            "type": e["type"],
            "timestamp": e["timestamp"],
            "source": e["source"],
            "data": e["data"] if e["type"] in {"comment", "like", "follow", "share", "gift", "subscription"} else {},
            "actions": e.get("actions") or [],
            "safety": e.get("safety"),
        })

    cursor = rows[-1]["seq"] if rows else after
    return {
        "ok": True,
        "cursor_expired": False,
        "events": bridge_rows,
        "cursor": cursor,
        "latest_seq": s.seq,
        "connection_state": s.state["connection_state"],
        "automation_paused": s.state["automation_paused"],
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if not websocket_authorized(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    await broadcaster.add(ws)
    try:
        await ws.send_json({"kind": "hello", "version": settings.app_version})
        while True:
            message = await ws.receive_text()
            if message == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        await broadcaster.remove(ws)
