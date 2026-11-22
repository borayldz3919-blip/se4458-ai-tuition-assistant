"""JWT (HS256) authentication and role-based authorization."""
import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
from .errors import AppError, forbidden

_PBKDF2_ROUNDS = int(os.getenv("PASSWORD_HASH_ROUNDS", "120000"))

bearer = HTTPBearer(auto_error=False, description="JWT from POST /api/v1/auth/login")


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, dk_b64 = stored.split("$")
    except ValueError:
        return False
    expected = hash_password(password, base64.b64decode(salt_b64))
    return hmac.compare_digest(expected, stored)


@dataclass
class CurrentUser:
    username: str
    role: str  # student | bank | admin
    student_no: str | None


def create_token(username: str, role: str, student_no: str | None) -> tuple[str, int]:
    now = datetime.now(timezone.utc)
    ttl = settings.jwt_ttl_minutes * 60
    claims = {
        "iss": settings.jwt_issuer,
        "sub": username,
        "role": role,
        "student_no": student_no,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256"), ttl


def _unauthorized(msg: str) -> AppError:
    return AppError(401, "UNAUTHORIZED", msg, headers={"WWW-Authenticate": "Bearer"})


def get_current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> CurrentUser:
    if creds is None:
        raise _unauthorized("Missing bearer token")
    try:
        claims = jwt.decode(
            creds.credentials,
            settings.jwt_secret,
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "sub", "iss"]},
        )
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Token expired")
    except jwt.PyJWTError:
        raise _unauthorized("Invalid token")
    return CurrentUser(claims["sub"], claims.get("role", ""), claims.get("student_no"))


def require_roles(*roles: str):
    def dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in roles:
            raise forbidden(f"Role '{user.role}' cannot perform this operation")
        return user

    return dep


def ensure_student_access(user: CurrentUser, student_no: str) -> None:
    """Students may only touch their own records. Bank/admin systems may access any student.

    This is what stops the AI agent from leaking another student's data: the agent always calls the
    API with the logged-in student's token, so a student number typed into the prompt is rejected here.
    """
    if user.role == "student" and user.student_no != student_no:
        raise forbidden("Students can only access their own tuition records")
