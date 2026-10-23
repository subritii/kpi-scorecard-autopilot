"""Signed, single-use, expiring links for people: the approval page and reply form.

A browser can't send the API key, so these pages authenticate with a token in
the link instead:

    <base64url payload>.<base64url HMAC-SHA256(LINK_SECRET, payload)>

The payload says what the link is for (approve a run's packet version, or
reply about one KPI), the client, when it expires, and a random id (jti).
The signature means nobody can make or alter a link without LINK_SECRET; the
link_tokens row makes each link work once (used_at) and lets the server
refuse it after expiry. Holding a link proves access to the inbox it was sent
to, nothing more (see README: real approver identity needs single sign-on).
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta

import psycopg

MIN_SECRET_LENGTH = 32
PURPOSES = ("approve", "reply")


class LinkConfigError(Exception):
    """LINK_SECRET, PUBLIC_BASE_URL or LINK_TTL_HOURS is missing or invalid."""


class InvalidLink(Exception):
    """Forged, altered, malformed or unknown link (HTTP 403)."""


class LinkGone(Exception):
    """The link was real but can't be used any more (HTTP 410)."""


class ExpiredLink(LinkGone):
    pass


class UsedLink(LinkGone):
    pass


@dataclass(frozen=True)
class Link:
    purpose: str  # "approve" or "reply"
    run_id: int
    client_id: str
    packet_version: int | None  # approve links: the version being approved
    kpi_id: int | None  # reply links: the KPI being explained
    expires: int  # unix time
    jti: str


# --- settings --------------------------------------------------------------------------


def _secret() -> bytes:
    secret = os.environ.get("LINK_SECRET", "")
    if len(secret) < MIN_SECRET_LENGTH:
        raise LinkConfigError(
            f"LINK_SECRET is not configured (needs {MIN_SECRET_LENGTH}+ characters)"
        )
    return secret.encode()


def base_url() -> str:
    """Where people's browsers reach the API, e.g. http://localhost:8000."""
    url = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise LinkConfigError(f"PUBLIC_BASE_URL must start with http:// or https://, got {url!r}")
    return url


def ttl() -> timedelta:
    raw = os.environ.get("LINK_TTL_HOURS", "72")
    if not raw.isdigit() or not 1 <= int(raw) <= 720:
        raise LinkConfigError(f"LINK_TTL_HOURS must be a whole number of hours, 1-720; got {raw!r}")
    return timedelta(hours=int(raw))


def check_settings() -> None:
    """Raise unless the link settings are valid (run at startup)."""
    _secret()
    base_url()
    ttl()


# --- signing ----------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(body: str) -> str:
    return _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())


def sign(link: Link) -> str:
    payload = {
        "p": link.purpose,
        "r": link.run_id,
        "c": link.client_id,
        "v": link.packet_version,
        "k": link.kpi_id,
        "e": link.expires,
        "j": link.jti,
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_signature(body)}"


def verify(token: str) -> Link:
    """The link's contents, if the signature is valid and it hasn't expired."""
    parts = token.split(".")
    if len(parts) != 2:
        raise InvalidLink("malformed link")
    body, sig = parts
    # Constant-time comparison, like the API key.
    if not hmac.compare_digest(_signature(body).encode(), sig.encode()):
        raise InvalidLink("invalid link signature")
    try:
        data = json.loads(_b64d(body))
        link = Link(
            purpose=data["p"],
            run_id=int(data["r"]),
            client_id=str(data["c"]),
            packet_version=data["v"],
            kpi_id=data["k"],
            expires=int(data["e"]),
            jti=str(data["j"]),
        )
    except (ValueError, KeyError, TypeError) as e:
        raise InvalidLink("malformed link") from e
    if link.purpose not in PURPOSES:
        raise InvalidLink("unknown link purpose")
    if link.expires <= time.time():
        raise ExpiredLink("this link has expired")
    return link


# --- issuing and using links -----------------------------------------------------------------


def issue(
    conn: psycopg.Connection,
    run: dict,
    purpose: str,
    kpi_id: int | None = None,
    lifetime: timedelta | None = None,
) -> str:
    """Create a link for a run (and KPI, for replies). Returns the full URL.

    `lifetime` defaults to LINK_TTL_HOURS (tests pass a negative one to get an
    expired link).
    """
    lifetime = lifetime if lifetime is not None else ttl()
    link = Link(
        purpose=purpose,
        run_id=run["id"],
        client_id=run["client_id"],
        packet_version=run["packet_version"] if purpose == "approve" else None,
        kpi_id=kpi_id if purpose == "reply" else None,
        expires=int(time.time() + lifetime.total_seconds()),
        jti=secrets.token_urlsafe(16),
    )
    conn.execute(
        "INSERT INTO link_tokens (jti, run_id, purpose, kpi_id, packet_version, expires_at)"
        " VALUES (%s, %s, %s, %s, %s, to_timestamp(%s))",
        (link.jti, link.run_id, purpose, link.kpi_id, link.packet_version, link.expires),
    )
    return f"{base_url()}/{purpose}?token={sign(link)}"


def check_unused(conn: psycopg.Connection, link: Link, purpose: str) -> None:
    """Raise unless this link is on record for this purpose and still usable."""
    if link.purpose != purpose:
        raise InvalidLink(f"this is not a{'n' if purpose == 'approve' else ''} {purpose} link")
    row = conn.execute(
        "SELECT t.used_at, t.expires_at <= clock_timestamp() AS expired, t.kpi_id,"
        " t.packet_version FROM link_tokens t JOIN weekly_runs r ON r.id = t.run_id"
        " WHERE t.jti = %s AND t.run_id = %s AND t.purpose = %s AND r.client_id = %s",
        (link.jti, link.run_id, purpose, link.client_id),
    ).fetchone()
    if row is None or (row["kpi_id"], row["packet_version"]) != (link.kpi_id, link.packet_version):
        raise InvalidLink("unknown link")
    if row["used_at"] is not None:
        raise UsedLink("this link has already been used")
    if row["expired"]:
        raise ExpiredLink("this link has expired")


def consume(conn: psycopg.Connection, link: Link) -> None:
    """Mark the link used. Atomic, so two clicks at once can't both succeed."""
    used = conn.execute(
        "UPDATE link_tokens SET used_at = clock_timestamp()"
        " WHERE jti = %s AND used_at IS NULL AND expires_at > clock_timestamp() RETURNING jti",
        (link.jti,),
    ).fetchone()
    if used is None:
        raise UsedLink("this link has already been used")
