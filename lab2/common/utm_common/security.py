"""JWT access tokens and password hashing.

auth-service issues tokens; the gateway verifies them once and forwards the user identity to the
services in X-User-Id / X-User-Role headers. Services are reachable only through the internal
network (compose network / Kubernetes ClusterIP), so they trust these headers.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

import jwt
from fastapi import Depends, Header, HTTPException

from .config import env, env_int

JWT_ALGORITHM = "HS256"


def jwt_secret() -> str:
    return env("JWT_SECRET", required=True)


def create_token(user_id: int, email: str, role: str) -> tuple[str, int]:
    ttl = env_int("JWT_TTL_SECONDS", 3600)
    now = int(time.time())
    payload = {"sub": str(user_id), "email": email, "role": role, "iat": now, "exp": now + ttl}
    return jwt.encode(payload, jwt_secret(), algorithm=JWT_ALGORITHM), ttl


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc


# scrypt from the standard library: memory-hard, no native dependency to install
_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


class Identity:
    def __init__(self, user_id: int, role: str):
        self.user_id, self.role = user_id, role


def current_identity(x_user_id: str | None = Header(default=None), x_user_role: str | None = Header(default=None)) -> Identity:
    """FastAPI dependency for services behind the gateway."""
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    return Identity(int(x_user_id), x_user_role or "customer")


def require_admin(identity: Identity) -> None:
    if identity.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")


def admin_identity(identity: Identity = Depends(current_identity)) -> Identity:
    """FastAPI dependency: the caller must be an admin."""
    require_admin(identity)
    return identity
