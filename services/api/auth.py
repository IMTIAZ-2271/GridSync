"""Password hashing, tokens, and the request-time identity.

Three things live here: how a password becomes a hash, how an account becomes
a bearer token, and how a request becomes an authenticated `Principal` the
route handlers can authorize against.

Roles map onto the four portals, one name each: the `consumer` role opens the
consumer portal. The portal was called "customer" until 2026-08-27, which is
why verification notes older than that say so; the enum never used the word.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError
from fastapi import Depends, HTTPException, Request, status

from .queries import sql

Role = Literal["consumer", "worker", "government", "supplier", "admin"]

# The placeholder db/sql/seed_demo.sql writes into password_hash. It is not a
# valid argon2 encoding, so verify() rejects it outright -- an unclaimed
# account cannot be logged into even if someone guesses its email.
SEED_PASSWORD_PLACEHOLDER = "$argon2id$seed$notarealhash"

TOKEN_TTL = timedelta(hours=24)
ALGORITHM = "HS256"

# argon2id at the library defaults: 64 MiB, 3 passes, 4 lanes.
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    """Check a password against a stored hash.

    Every argon2 failure mode collapses to False on purpose. A malformed hash
    (an unclaimed seed account) and a wrong password are the same answer to the
    caller, so neither the code nor the response can distinguish them.
    """
    try:
        return _hasher.verify(encoded, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _secret() -> str:
    secret = os.environ.get("JWT_SECRET", "").strip()
    if not secret:
        # Deliberately fatal. A default secret baked into source would let
        # anyone who has read the repository mint a valid admin token, which
        # is worse than having no auth at all -- at least no auth is visible.
        raise RuntimeError(
            "JWT_SECRET is not set. Add it to .env (see .env.example)."
        )
    return secret


def issue_token(account_id: UUID, role: str, email: str) -> tuple[str, int]:
    """Return (token, seconds_until_expiry).

    The role is inside the token so route protection needs no database round
    trip. The cost is that a role change or a suspension does not take effect
    until the token expires -- acceptable at a 24h TTL for a demo.

    `jti` is what makes logout real rather than a client-side no-op: it names
    this one token, distinct from any other token the same account has ever
    been issued, so revoking it (see routes_auth.logout) cannot touch a
    session opened from a different device.
    """
    now = datetime.now(timezone.utc)
    expires = now + TOKEN_TTL
    payload = {
        "sub": str(account_id),
        "role": role,
        "email": email,
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM), int(
        TOKEN_TTL.total_seconds()
    )


@dataclass(frozen=True)
class Principal:
    """Who is making this request."""

    account_id: UUID
    role: Role
    email: str
    full_name: str
    jti: UUID | None

    @property
    def sees_every_site(self) -> bool:
        """Government and supplier are fleet-wide readers; the others are not."""
        return self.role in ("government", "supplier", "admin")


_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)

_REVOKED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="token revoked",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_account(request: Request) -> Principal:
    """Resolve the bearer token to a live account.

    The account is re-read on every request rather than trusted from the token
    body. The token proves who authenticated; whether that account still exists
    and is still active is a fact about now, not about when it was issued.

    Revocation is folded into this same round trip: `revoked_token` is left
    joined on the token's own `jti`, so a logged-out token is rejected here
    without a second query. `jti` can be absent -- a token minted before this
    check existed never got one -- and the join simply never matches a NULL,
    which is the correct fallback: a pre-existing token was never enrollable
    in `revoked_token` in the first place, so it behaves exactly as it always
    has until it expires on its own.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _UNAUTHENTICATED

    try:
        payload = jwt.decode(token, _secret(), algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except jwt.InvalidTokenError:
        raise _UNAUTHENTICATED from None

    raw_jti = payload.get("jti")
    jti = UUID(raw_jti) if raw_jti else None

    pool: asyncpg.Pool = request.app.state.pool
    row = await pool.fetchrow(
        """
        SELECT account.account_id,
               account.email::text AS email,
               account.full_name,
               account.role::text AS role,
               account.status::text AS status,
               (revoked_token.jti IS NOT NULL) AS token_revoked
        FROM account
        LEFT JOIN revoked_token ON revoked_token.jti = $2::uuid
        WHERE account.account_id = $1
        """,
        UUID(payload["sub"]),
        jti,
    )
    if row is None:
        raise _UNAUTHENTICATED
    if row["token_revoked"]:
        raise _REVOKED
    if row["status"] != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"account is {row['status']}",
        )

    return Principal(
        account_id=row["account_id"],
        role=row["role"],  # type: ignore[arg-type]
        email=row["email"],
        full_name=row["full_name"],
        jti=jti,
    )


CurrentAccount = Annotated[Principal, Depends(get_current_account)]


def require_role(*roles: Role):
    """Dependency factory restricting a route to the given roles.

    Returns 403, not 404: the caller is authenticated, and pretending the route
    does not exist would only make a legitimate permissions bug harder to
    diagnose. Which *rows* they may see is a separate question, handled by the
    scoped queries rather than here.
    """

    async def _guard(principal: CurrentAccount) -> Principal:
        if principal.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"this endpoint is not available to role '{principal.role}'",
            )
        return principal

    return _guard


def registration_code(role: Literal["government", "supplier"]) -> str:
    """The shared code a government or supplier registrant must present.

    In .env rather than in source: a code committed to the repository is not a
    check, and these two roles read every site in the system. Still a shared
    secret with no rotation and no per-invite tracking, which is the next thing
    to fix if this stops being a demo.
    """
    key = "GOV_REGISTRATION_CODE" if role == "government" else "SUPPLIER_REGISTRATION_CODE"
    code = os.environ.get(key, "").strip()
    if not code:
        raise RuntimeError(f"{key} is not set. Add it to .env (see .env.example).")
    return code


# --------------------------------------------------------------------------
# Row-level authorization
#
# require_role() answers "may this role call this endpoint at all". This
# answers the narrower question: "may this caller see THIS row". Both are
# needed -- a consumer may legitimately call /summary, but only for a site
# they own.
# --------------------------------------------------------------------------

async def visible_site_or_404(
    conn: asyncpg.Connection,
    principal: Principal,
    site_id: UUID,
) -> None:
    """Raise unless this caller may read this site.

    404 rather than 403 for a site that exists but belongs to someone else.
    403 would confirm the site exists, which turns this endpoint into a probe
    for which meters are registered. The caller cannot act on the difference
    either way.
    """
    not_found = HTTPException(status_code=404, detail="site not found")

    if principal.sees_every_site:
        if not await conn.fetchval("SELECT 1 FROM site WHERE site_id = $1", site_id):
            raise not_found
        return

    if principal.role == "consumer":
        if not await conn.fetchval(
            sql("account_owns_site"), site_id, principal.account_id
        ):
            raise not_found
        return

    if principal.role == "worker":
        # A worker sees a site for as long as an assignment ties them to it.
        if not await conn.fetchval(
            sql("worker_covers_site"), site_id, principal.account_id
        ):
            raise not_found
        return

    raise not_found
