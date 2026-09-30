import base64
import hashlib
import hmac
import json
import time
from fastapi import Header, HTTPException, Request, Response, WebSocket
from .config import settings

COOKIE_NAME = "live_manager_session"
COOKIE_MAX_AGE = 7 * 24 * 60 * 60


def validate_secrets():
    if settings.render and (not settings.secret_key or not settings.api_key):
        raise RuntimeError("Render requires SECRET_KEY and API_KEY")
    if settings.dashboard_password and not (settings.secret_key or settings.api_key):
        raise RuntimeError("Dashboard authentication requires SECRET_KEY or API_KEY")


def _secret() -> bytes:
    value = settings.secret_key or settings.api_key
    if not value:
        raise RuntimeError("SECRET_KEY or API_KEY is required to sign cookies")
    return value.encode("utf-8")


def _sign(payload: bytes) -> str:
    return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()


def make_session_token() -> str:
    data = {"iat": int(time.time()), "exp": int(time.time()) + COOKIE_MAX_AGE}
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = _sign(body.encode("ascii"))
    return f"{body}.{sig}"


def verify_session_token(token: str | None) -> bool:
    if not (settings.secret_key or settings.api_key) or not token or "." not in token:
        return False
    body, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(body.encode("ascii")), sig):
        return False
    try:
        padded = body + "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        return int(data.get("exp", 0)) >= int(time.time())
    except Exception:
        return False


def dashboard_password() -> str:
    return settings.dashboard_password or settings.api_key


def check_password(password: str) -> bool:
    expected = dashboard_password()
    if not expected:
        return False
    return hmac.compare_digest(password, expected)


def set_login_cookie(response: Response) -> None:
    response.set_cookie(
        COOKIE_NAME,
        make_session_token(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.render,
        samesite="strict",
        path="/",
    )


def clear_login_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def authorized_request(request: Request, x_api_key: str | None = None) -> bool:
    if settings.api_key and x_api_key and hmac.compare_digest(x_api_key, settings.api_key):
        return True
    return verify_session_token(request.cookies.get(COOKIE_NAME))


def require_auth(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    if not authorized_request(request, x_api_key):
        raise HTTPException(status_code=401, detail="Authentication required")


def require_api_key(x_api_key: str | None) -> None:
    if not settings.api_key:
        raise HTTPException(status_code=503, detail="API_KEY is not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")


def websocket_authorized(ws: WebSocket) -> bool:
    key = ws.query_params.get("key")
    if settings.api_key and key and hmac.compare_digest(key, settings.api_key):
        return True
    return verify_session_token(ws.cookies.get(COOKIE_NAME))
