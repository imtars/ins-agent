"""Local account store and rotating refresh tokens for M10."""

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from packages.persistence.approvals import psycopg_url


ROLES = {"analyst", "reviewer", "admin"}


def password_hash(password: str, salt: bytes) -> bytes:
    if len(password) < 12:
        raise ValueError("password must contain at least 12 characters")
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)


class AuthStore:
    def __init__(self, database_url: str, secret: str):
        if len(secret.encode()) < 32:
            raise ValueError("JWT signing secret must be at least 32 bytes")
        self.database_url = psycopg_url(database_url)
        self.secret = secret

    async def setup(self) -> None:
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS m10_users (
                    id uuid PRIMARY KEY, username text NOT NULL UNIQUE,
                    password_salt bytea NOT NULL, password_digest bytea NOT NULL,
                    role text NOT NULL CHECK (role IN ('analyst', 'reviewer', 'admin')),
                    active boolean NOT NULL DEFAULT true,
                    created_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS m10_refresh_tokens (
                    jti_hash char(64) PRIMARY KEY,
                    user_id uuid NOT NULL REFERENCES m10_users(id),
                    expires_at timestamptz NOT NULL, revoked_at timestamptz,
                    created_at timestamptz NOT NULL DEFAULT now()
                )
            """)

    async def create_user(self, username: str, password: str, role: str) -> None:
        if role not in ROLES or not username or len(username) > 100:
            raise ValueError("invalid account")
        salt = os.urandom(16)
        digest = password_hash(password, salt)
        async with await AsyncConnection.connect(self.database_url) as conn:
            await conn.execute("""
                INSERT INTO m10_users (id, username, password_salt, password_digest, role)
                VALUES (%s, %s, %s, %s, %s)
            """, (str(uuid4()), username, salt, digest, role))

    async def _user(self, username: str | None = None, user_id: str | None = None):
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            return await (await conn.execute("""
                SELECT * FROM m10_users WHERE username = %s OR id = %s
            """, (username, user_id))).fetchone()

    async def login(self, username: str, password: str) -> dict | None:
        row = await self._user(username=username)
        # Equal-cost check for unknown accounts avoids a cheap username oracle.
        salt = row["password_salt"] if row else bytes(16)
        actual = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        if not row or not row["active"] or not hmac.compare_digest(actual, row["password_digest"]):
            return None
        return await self._tokens(row)

    def _encode(self, user_id: str, kind: str, jti: str, expires: datetime) -> str:
        now = datetime.now(timezone.utc)
        return jwt.encode({"sub": user_id, "type": kind, "jti": jti,
                           "iss": "ins-agent", "aud": "ins-agent-api",
                           "iat": now, "nbf": now, "exp": expires}, self.secret,
                          algorithm="HS256")

    async def _tokens(self, row: dict) -> dict:
        user_id = str(row["id"])
        now = datetime.now(timezone.utc)
        refresh_jti = str(uuid4())
        expires = now + timedelta(days=7)
        async with await AsyncConnection.connect(self.database_url) as conn:
            await conn.execute("""
                INSERT INTO m10_refresh_tokens (jti_hash, user_id, expires_at)
                VALUES (%s, %s, %s)
            """, (hashlib.sha256(refresh_jti.encode()).hexdigest(), user_id, expires))
        return {"access_token": self._encode(user_id, "access", str(uuid4()),
                                                now + timedelta(minutes=15)),
                "refresh_token": self._encode(user_id, "refresh", refresh_jti, expires),
                "token_type": "bearer", "role": row["role"], "username": row["username"]}

    def decode(self, token: str, kind: str) -> dict:
        value = jwt.decode(token, self.secret, algorithms=["HS256"],
                           issuer="ins-agent", audience="ins-agent-api",
                           options={"require": ["sub", "type", "jti", "exp", "iat", "nbf"]})
        if value["type"] != kind:
            raise jwt.InvalidTokenError("incorrect token type")
        return value

    async def authenticate(self, token: str) -> dict | None:
        claims = self.decode(token, "access")
        row = await self._user(user_id=claims["sub"])
        if not row or not row["active"]:
            return None
        return {"id": str(row["id"]), "username": row["username"], "role": row["role"]}

    async def refresh(self, token: str) -> dict | None:
        claims = self.decode(token, "refresh")
        key = hashlib.sha256(claims["jti"].encode()).hexdigest()
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            async with conn.transaction():
                old = await (await conn.execute("""
                    UPDATE m10_refresh_tokens SET revoked_at = now()
                    WHERE jti_hash = %s AND user_id = %s AND revoked_at IS NULL
                      AND expires_at > now() RETURNING user_id
                """, (key, claims["sub"]))).fetchone()
        if old is None:
            return None
        user = await self._user(user_id=claims["sub"])
        return await self._tokens(user) if user and user["active"] else None
