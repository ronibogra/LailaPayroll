import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException, Request

from config import settings

ALGORITHM = "HS256"
COOKIE = "laila_session"
MOBILE_ROLE = "EMPLOYEE"


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
    if not data.get("role") or data.get("role") == MOBILE_ROLE:   # mobile tokens are not staff logins
        raise HTTPException(401, "Login required")
    return {"username": data["sub"], "role": data["role"]}


def page_user(request: Request) -> dict:
    """Logged-in staff user, re-checked against the database so a disabled user or a changed role or
    permission takes effect immediately. Returns username, role, company_id and the set of permissions."""
    from database import SessionLocal
    from models import Role, User
    from services.permissions import PERMS, parse
    try:
        user = current_user(request)
    except HTTPException:
        raise LoginRequired()
    db = SessionLocal()
    try:
        row = db.query(User).filter(User.username == user["username"]).first()
        if not row or not row.active:
            raise LoginRequired()
        if row.role in ("SUPER_ADMIN", "ADMIN"):
            perms = set(PERMS) - {"data.delete"}          # deleting: only if ticked for this user
        else:
            role = db.query(Role).filter(Role.name == row.role).first()
            perms = parse(role.permissions) if role else set()
        if getattr(row, "super_admin", False):            # user form: "May delete data"
            perms.add("data.delete")
        return {"username": row.username, "role": row.role, "company_id": row.company_id, "perms": perms,
                "full_name": row.full_name or row.username, "employee_id": row.employee_id,
                "must_change": bool(row.must_change),
                "super_admin": row.role == "SUPER_ADMIN", "can_delete": "data.delete" in perms, "is_admin": row.role in ("SUPER_ADMIN", "ADMIN")}
    finally:
        db.close()
