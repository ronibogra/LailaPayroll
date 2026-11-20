import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request

from config import settings

ALGORITHM = "HS256"
COOKIE = "laila_session"
ROLES = ("ADMIN", "HR", "ACCOUNTS", "VIEWER")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310000).hex()
    return f"pbkdf2_sha256$310000${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, expected = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def create_token(username: str, role: str, hours: int | None = None) -> str:
    payload = {"sub": username, "role": role,
               "exp": datetime.now(timezone.utc) + timedelta(hours=hours or settings.session_hours)}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])


class LoginRequired(Exception):
    """Raised for browser pages; main.py turns it into a redirect to /login."""


def _token_from(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(COOKIE)


def current_user(request: Request) -> dict:
    token = _token_from(request)
    if not token:
        raise HTTPException(401, "Login required")
    try:
        data = decode_token(token)
    except Exception:
        raise HTTPException(401, "Session expired - please log in again")
    if data.get("role") not in ROLES:   # e.g. a mobile EMPLOYEE token
        raise HTTPException(401, "Login required")
    return {"username": data["sub"], "role": data["role"]}


def require(*roles: str):
    """Dependency: any logged-in user if no roles given, ADMIN always allowed."""
    def dep(user: dict = Depends(current_user)) -> dict:
        if roles and user["role"] != "ADMIN" and user["role"] not in roles:
            raise HTTPException(403, "You do not have permission for this action")
        return user
    return dep


def page_user(request: Request) -> dict:
    """Logged-in staff user, re-checked against the database so a disabled
    user or a changed role takes effect immediately."""
    from database import SessionLocal
    from models import User
    try:
        user = current_user(request)
    except HTTPException:
        raise LoginRequired()
    db = SessionLocal()
    try:
        row = db.query(User).filter(User.username == user["username"]).first()
    finally:
        db.close()
    if not row or not row.active:
        raise LoginRequired()
    return {"username": row.username, "role": row.role, "company_id": row.company_id}
