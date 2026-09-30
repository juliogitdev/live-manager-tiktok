import base64
import hashlib
import hmac
import json
import time
from collections import deque
from functools import lru_cache
from urllib.parse import urlsplit
from fastapi import Header, HTTPException, Request, Response, WebSocket
from .config import settings

COOKIE_NAME = "live_manager_session"
COOKIE_MAX_AGE = 7 * 24 * 60 * 60


def validate_secrets():
    if settings.render and (not settings.secret_key or not settings.api_key):
        raise RuntimeError("Render requires SECRET_KEY and API_KEY")
    if settings.dashboard_password and not (settings.secret_key or settings.api_key):
        raise RuntimeError("Dashboard authentication requires SECRET_KEY or API_KEY")
    if settings.render or settings.environment == "production":
        if min(len(settings.secret_key), len(settings.api_key)) < 32:
            raise RuntimeError("Production requires SECRET_KEY and API_KEY of at least 32 characters")
        if settings.secret_key == settings.api_key:
            raise RuntimeError("Use independent signing and administrator secrets")
        if not settings.cookie_secure:
            raise RuntimeError("Production requires COOKIE_SECURE=true")
    tokens = game_tokens(settings.game_tokens)
    if any(token in {settings.api_key, settings.secret_key, settings.dashboard_password} for token in tokens):
        raise RuntimeError("GAME_TOKENS must be independent of administrator secrets")


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
    try:
        if len(token) > 2048:
            return False
        body, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(_sign(body.encode("ascii")), sig):
            return False
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
    return hmac.compare_digest(password.encode(), expected.encode())


def set_login_cookie(response: Response) -> None:
    response.set_cookie(
        COOKIE_NAME,
        make_session_token(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )


def clear_login_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def authorized_request(request: Request, x_api_key: str | None = None) -> bool:
    if settings.api_key and x_api_key and hmac.compare_digest(x_api_key.encode(), settings.api_key.encode()):
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
    # Never accept credentials in URLs (including when a valid cookie is present).
    if "key" in ws.query_params:
        return False
    key = ws.headers.get("x-api-key")
    if settings.api_key and key and hmac.compare_digest(key.encode(), settings.api_key.encode()):
        return True
    origin = ws.headers.get("origin")
    allowed = {s.strip().rstrip("/") for s in settings.allowed_origins.split(",") if s.strip()}
    parsed = urlsplit(origin or "")
    same_host = (parsed.netloc.lower() == ws.url.netloc.lower() and
                 parsed.scheme in ({"https"} if ws.url.scheme == "wss" else {"http", "https"}) and
                 not parsed.path and not parsed.query and not parsed.fragment)
    if not same_host and (not origin or origin.rstrip("/") not in allowed):
        return False
    return verify_session_token(ws.cookies.get(COOKIE_NAME))


@lru_cache(maxsize=4)
def game_tokens(raw):
    try:
        entries = json.loads(raw)
        if not isinstance(entries, dict):
            raise ValueError()
        for token, spec in entries.items():
            if len(token) < 32 or not isinstance(spec, dict) or set(spec) != {"consumer_id", "usernames"}:
                raise ValueError()
            if not isinstance(spec["consumer_id"], str) or not 1 <= len(spec["consumer_id"]) <= 128:
                raise ValueError()
            names = spec["usernames"]
            if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n or len(n) > 64 for n in names):
                raise ValueError()
        return entries
    except (ValueError, TypeError):
        raise RuntimeError("Invalid GAME_TOKENS: expected tokens with consumer_id and usernames") from None


def require_bridge(key, username, consumer_id=None):
    if key and settings.api_key and hmac.compare_digest(key.encode(), settings.api_key.encode()):
        return consumer_id or "roblox"
    for token, spec in game_tokens(settings.game_tokens).items():
        if key and hmac.compare_digest(key.encode(), token.encode()):
            names = {n.strip().lstrip("@").casefold() for n in spec["usernames"]}
            if username.strip().lstrip("@").casefold() not in names:
                raise HTTPException(403, "Token does not allow this account")
            if consumer_id is not None and consumer_id != spec["consumer_id"]:
                raise HTTPException(403, "Token is bound to another consumer")
            return spec["consumer_id"]
    raise HTTPException(401 if settings.api_key or game_tokens(settings.game_tokens) else 503, "Invalid bridge credential")


def require_bridge_credential(key):
    if key and settings.api_key and hmac.compare_digest(key.encode(), settings.api_key.encode()):
        return
    if key and any(hmac.compare_digest(key.encode(), token.encode()) for token in game_tokens(settings.game_tokens)):
        return
    raise HTTPException(401 if settings.api_key or game_tokens(settings.game_tokens) else 503, "Invalid bridge credential")


class LoginLimiter:
    def __init__(self):
        self.attempts = {}
        self.global_attempts = deque()

    def check(self, ip):
        now = time.monotonic()
        cutoff = now - settings.login_window_seconds
        while self.global_attempts and self.global_attempts[0] <= cutoff:
            self.global_attempts.popleft()
        self.attempts = {key: q for key, q in self.attempts.items() if q and q[-1] > cutoff}
        attempts = self.attempts.setdefault(ip, deque())
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if len(attempts) >= settings.login_attempts or len(self.global_attempts) >= settings.login_attempts * 10:
            raise HTTPException(429, "Too many login attempts", headers={"Retry-After": str(settings.login_window_seconds)})
        # Reserve synchronously before password verification: concurrent attempts count.
        attempts.append(now)
        self.global_attempts.append(now)


login_limiter = LoginLimiter()
