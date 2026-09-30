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
from pydantic import BaseModel, Field, StrictBool, ConfigDict
from pathlib import Path
from . import models
from .storage import EventStore
from .body_limit import BodyLimit
from .auth import require_bridge, require_bridge_credential, login_limiter
from typing import Literal

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


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(StrictRequest):
    password: str = Field(min_length=1, max_length=300)


class ConnectRequest(StrictRequest):
    username: str = Field(min_length=1, max_length=64)
    profile: Literal["raw", "dance", "kite"] = "raw"


class SessionOptions(ConnectRequest):
    simulation: StrictBool = False
    pinned: StrictBool = True


class SimulationRequest(StrictRequest):
    type: Literal["gift", "like", "comment", "share", "follow", "subscription"]
    username: str = Field(default="Teste", max_length=64)
    gift_name: str = Field(default="Rose", max_length=200)
    gift_coins: int = Field(default=1, strict=True, ge=0, le=1000000)
    count: int = Field(default=1, strict=True, ge=1, le=1000000)
    comment: str = Field(default="teste", max_length=2000)


class ConfigRequest(StrictRequest):
    config: dict[str, StrictBool]


class RulesRequest(StrictRequest):
    rules: list[dict[str, Any]] = Field(max_length=100)


class ProfileRequest(StrictRequest):
    profile: Literal["raw", "dance", "kite"]


class BridgeRegisterRequest(StrictRequest):
    username: str = Field(min_length=1, max_length=64)
    profile: Literal["raw", "dance", "kite"] = "raw"
    consumer_id: str | None = Field(default=None, min_length=1, max_length=128)
    resume: StrictBool = False
    simulation: StrictBool = False


class AckRequest(StrictRequest):
    consumer_id: str | None = Field(default=None, min_length=1, max_length=128)
    cursor: int = Field(ge=0, strict=True)


class PinRequest(StrictRequest):
    pinned: StrictBool


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .auth import validate_secrets
    settings.validate()
    validate_secrets()
    manager.store = EventStore(settings.database_path, settings.retention_seconds)
    maintenance = None
    try:
        manager.restore()
        await manager.auto_connect()
        maintenance = asyncio.create_task(manager.maintenance())
        yield
    finally:
        if maintenance:
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)
        await manager.stop_all()
        await broadcaster.close()
        manager.store.close()
        manager.store = None
        manager.sessions.clear()



app = FastAPI(
    title="TikTok → Roblox Live Manager",
    version=settings.app_version,
    lifespan=lifespan,
)
app.add_middleware(BodyLimit, max_bytes=settings.max_json_bytes)
STATIC = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def get_session(sid: str):
    try:
        return manager.get(sid)
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")


@app.get("/")
async def dashboard():
    return FileResponse(STATIC / "index.html")


@app.get("/health", response_model=models.Health)
async def health():
    # Render health check: app health, not TikTok connection state.
    return {
        "ok": True,
        "version": settings.app_version,
        "sessions": len(manager.sessions),
        "max_sessions": settings.max_sessions,
    }


@app.get("/api/auth/status", response_model=models.AuthStatus)
async def auth_status(request: Request):
    return {
        "authenticated": verify_session_token(request.cookies.get("live_manager_session")),
        "password_configured": bool(settings.dashboard_password or settings.api_key),
    }


@app.post("/api/auth/login", response_model=models.OK)
async def login(body: LoginRequest, request: Request):
    login_limiter.check(request.client.host if request.client else "unknown")
    if not check_password(body.password):
        raise HTTPException(status_code=401, detail="Invalid password")
    response = JSONResponse({"ok": True})
    set_login_cookie(response)
    return response


@app.post("/api/auth/logout", response_model=models.OK)
async def logout():
    response = JSONResponse({"ok": True})
    clear_login_cookie(response)
    return response


@app.get("/api/meta", response_model=models.Meta)
async def meta(request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    return {
        "version": settings.app_version,
        "max_sessions": settings.max_sessions,
        "profiles": BUILTIN_PROFILES,
        "render_free_note": "On Render Free, active Roblox polling or dashboard WebSocket traffic keeps the service receiving inbound traffic.",
    }


@app.get("/api/live/sessions", response_model=models.Sessions)
async def list_sessions(request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    return {"sessions": [s.snapshot() for s in manager.sessions.values()]}


@app.post("/api/live/connect", response_model=models.Connected)
async def connect_live(body: SessionOptions, request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    try:
        session, created = await manager.connect(body.username, body.profile, simulation=body.simulation, pinned=body.pinned)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"created": created, "profile_conflict": body.profile != session.profile_name, **session.snapshot()}


@app.post("/api/live/{sid}/disconnect", response_model=models.OK)
async def disconnect_live(sid: str, request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    get_session(sid)
    await manager.disconnect(sid)
    return {"ok": True}


@app.get("/api/live/{sid}/status", response_model=models.Snapshot)
async def session_status(sid: str, request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    return get_session(sid).snapshot()


@app.get("/api/live/{sid}/events", response_model=models.Events)
async def session_events(
    sid: str,
    request: Request,
    after: int = Query(0, ge=0),
    limit: int = Query(150, ge=1, le=500),
    tail: bool = Query(False),
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    s = get_session(sid)
    rows = list(s.events)[-limit:] if tail else [e for e in s.events if e["seq"] > after][:limit]
    return {
        "events": rows,
        "latest_seq": s.seq,
        "oldest_seq": s.oldest_seq(),
        "has_more": bool(rows and rows[-1]["seq"] < s.seq),
    }


@app.put("/api/live/{sid}/config", response_model=dict[str, bool])
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
        "share_enabled", "gift_enabled", "safety_auto_pause_critical",
        "count_simulations_in_stats",
    }
    # v4.0 alias controls the v4.1 critical policy; it never restores type-only pauses.
    config = dict(body.config)
    if "safety_auto_pause_high" in config:
        old = config.pop("safety_auto_pause_high")
        if "safety_auto_pause_critical" in config and config["safety_auto_pause_critical"] != old:
            raise HTTPException(status_code=400, detail="Conflicting safety settings")
        config["safety_auto_pause_critical"] = old
    if config.keys() - allowed:
        raise HTTPException(status_code=400, detail="Unknown configuration field")
    s.config.update(config)
    s.persist()
    return s.config


@app.put("/api/live/{sid}/rules", response_model=models.Rules)
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


@app.post("/api/live/{sid}/profile", response_model=models.Profile)
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


@app.post("/api/live/{sid}/safety/reset", response_model=models.OK)
async def reset_safety(sid: str, request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    s = get_session(sid)
    s.state["safety_state"] = "NORMAL"
    s.state["last_safety_event"] = None
    s.state["last_monitor_event"] = None
    s.state["automation_paused"] = False
    s.recent_safety_signals.clear()
    await s.push("safety_reset", {"automation_paused": False}, source="control")
    return {"ok": True}


@app.post("/api/live/{sid}/simulate", response_model=models.Event)
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


@app.get("/api/live/{sid}/leaderboard", response_model=models.Leaderboard)
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


@app.get("/api/live/{sid}/export.json", response_model=models.Export)
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


# Bridge API: API key only; in-memory history is lost on restart.
@app.post("/api/bridge/register", response_model=models.Registered)
async def bridge_register(body: BridgeRegisterRequest, x_api_key: str | None = Header(default=None)):
    consumer = require_bridge(x_api_key, body.username, body.consumer_id)
    try:
        s, created = await manager.connect(body.username, body.profile, simulation=body.simulation)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    previous = s.store.consumer(s.session_id, consumer) if s.store else None
    if s.store:
        try:
            s.store.touch(s.session_id, consumer, s.seq, settings.max_consumers)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
    cursor = previous["ack"] if body.resume and previous else s.seq
    return {
        "ok": True,
        "created": created,
        "session_id": s.session_id,
        "username": s.username,
        "profile": s.profile_name,
        "profile_conflict": body.profile != s.profile_name,
        "requested_profile": body.profile,
        "oldest_seq": s.oldest_seq(),
        "latest_seq": s.seq,
        "gap_detected": cursor < s.oldest_seq() - 1,
        "consumer_id": consumer,
        "resumed": bool(body.resume and previous),
        # Start at current sequence to avoid replaying old gifts after Roblox restarts.
        "cursor": cursor,
        "state": s.state["connection_state"],
        "automation_paused": s.state["automation_paused"],
    }


@app.get("/api/bridge/{sid}/poll", response_model=models.Poll)
async def bridge_poll(
    sid: str,
    after: int = Query(0, ge=0),
    wait: float = Query(8.0, ge=0, le=15),
    recover: bool = Query(False),
    consumer_id: str | None = Query(None, min_length=1, max_length=128),
    limit: int = Query(100, ge=1, le=250),
    x_api_key: str | None = Header(default=None),
):
    require_bridge_credential(x_api_key)
    s = get_session(sid)
    consumer = require_bridge(x_api_key, s.username, consumer_id)
    try:
        manager.activate(s)
        if s.store:
            s.store.touch(sid, consumer, min(after, s.seq), settings.max_consumers)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc))

    # Recheck retention after the wait: a burst can evict events before this task resumes.
    rows = await s.wait_for_events(after, wait) if after <= s.seq else []
    oldest = s.oldest_seq()
    gap = after < oldest - 1 or after > s.seq
    if gap and (not recover or after > s.seq):
        if s.store:
            s.store.delivered(sid, consumer, s.seq, True)
        return {
            "ok": True,
            "cursor_expired": True,
            "gap_detected": True,
            "session_id": s.session_id,
            "oldest_seq": oldest,
            "connection_state": s.state["connection_state"],
            "events": [],
            "cursor": s.seq,
            "latest_seq": s.seq,
            "automation_paused": s.state["automation_paused"],
        }

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
            "schema_version": e["schema_version"],
            "automation_paused_at_ingest": e["automation_paused_at_ingest"],
        })

    cursor = rows[-1]["seq"] if rows else after
    if s.store:
        s.store.delivered(sid, consumer, cursor, gap)
    return {
        "ok": True,
        "cursor_expired": False,
        "gap_detected": gap,
        "session_id": s.session_id,
        "oldest_seq": s.oldest_seq(),
        "events": bridge_rows,
        "cursor": cursor,
        "latest_seq": s.seq,
        "connection_state": s.state["connection_state"],
        "automation_paused": s.state["automation_paused"],
    }


@app.post("/api/bridge/{sid}/ack", response_model=models.Ack)
async def bridge_ack(sid: str, body: AckRequest, x_api_key: str | None = Header(default=None)):
    require_bridge_credential(x_api_key)
    s = get_session(sid)
    consumer = require_bridge(x_api_key, s.username, body.consumer_id)
    if not s.store:
        raise HTTPException(503, "Persistent store unavailable")
    try:
        cursor = s.store.ack(sid, consumer, body.cursor)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "cursor": cursor}


@app.put("/api/live/{sid}/pin", response_model=models.OK)
async def pin_session(sid: str, body: PinRequest, request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    s = get_session(sid)
    if body.pinned:
        try:
            manager.activate(s)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
    s.pinned = body.pinned
    s.persist()
    return {"ok": True}


@app.get("/api/live/{sid}/metrics", response_model=models.Metrics)
async def metrics(sid: str, request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    s = get_session(sid)
    return {"consumers": s.store.metrics(sid, s.seq) if s.store else [],
            "ws_dropped_clients": broadcaster.dropped_clients}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if not websocket_authorized(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    sid = ws.query_params.get("session_id")
    if sid and sid not in manager.sessions:
        await ws.close(code=1008)
        return
    await broadcaster.add(ws, {sid} if sid else None)
    writer = broadcaster.clients[ws].task
    try:
        await broadcaster.send_to(ws, {"kind": "hello", "version": settings.app_version})
        while True:
            received = asyncio.create_task(ws.receive_text())
            finished, pending = await asyncio.wait(
                {received, writer}, return_when=asyncio.FIRST_COMPLETED)
            if received not in finished:
                received.cancel()
                await asyncio.gather(received, return_exceptions=True)
                break
            message = received.result()
            if message == "ping":
                await broadcaster.send_to(ws, "pong")
    except (WebSocketDisconnect, KeyError):
        pass
    finally:
        await broadcaster.remove(ws)
