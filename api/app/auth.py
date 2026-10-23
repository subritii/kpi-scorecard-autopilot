"""API key check, applied to every API route (see app/main.py).

The caller (n8n) sends X-API-Key; it is compared with API_KEY from the
environment in constant time, so response timing doesn't reveal how much of a
guess was right. Fails closed: the API refuses to start without a strong key
(check_configured_key() runs at startup), and if that were ever bypassed,
every request is refused instead of the API running open.
"""

import hmac
import os

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

MIN_KEY_LENGTH = 32

# auto_error=False: a missing header gets the same 401 as a wrong one.
_header = APIKeyHeader(name="X-API-Key", auto_error=False)


NOT_CONFIGURED = f"API_KEY is not configured (needs at least {MIN_KEY_LENGTH} characters)"


def check_configured_key() -> None:
    """Raise unless API_KEY is set and strong enough. The message never echoes it."""
    if len(os.environ.get("API_KEY", "")) < MIN_KEY_LENGTH:
        raise RuntimeError(NOT_CONFIGURED)


def require_api_key(supplied: str | None = Security(_header)) -> None:
    expected = os.environ.get("API_KEY", "")
    if len(expected) < MIN_KEY_LENGTH:
        raise HTTPException(status_code=500, detail=NOT_CONFIGURED)
    # Compare bytes: compare_digest needs equal types, and bytes handle any input.
    if supplied is None or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="missing or invalid API key")
