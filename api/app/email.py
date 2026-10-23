"""Outgoing emails, composed by the API; n8n only sends what it is given.

compose() is the single place an email is built, so TEST_EMAIL_OVERRIDE can't
be bypassed. When it is set (e.g. me@gmail.com), every recipient becomes a
plus-alias of that inbox, and the body starts with a banner naming the real
recipients:

    ceo@brightline.example  ->  me+ceo.brightline@gmail.com
    Test mode: originally addressed to ceo@brightline.example

The alias keeps the domain's first label so ceo@ at two clients stay distinct.

Fail safe: the API only sends to real addresses when EMAIL_MODE=live is set
explicitly. With neither TEST_EMAIL_OVERRIDE nor EMAIL_MODE=live (or with
both), it refuses to start, and compose()/recipient() refuse as a backstop.
"""

import html as html_lib
import os
import re

BANNER = "Test mode: originally addressed to "
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class EmailConfigError(Exception):
    """The email settings are missing, invalid or contradictory."""


def _override() -> str | None:
    """The test inbox, or None in live mode. Raises unless exactly one mode is chosen."""
    value = os.environ.get("TEST_EMAIL_OVERRIDE", "").strip()
    mode = os.environ.get("EMAIL_MODE", "").strip()
    if mode and mode != "live":
        raise EmailConfigError(f"EMAIL_MODE must be 'live' or unset, got {mode!r}")
    if value and mode == "live":
        raise EmailConfigError("TEST_EMAIL_OVERRIDE and EMAIL_MODE=live are both set; choose one")
    if not value and mode != "live":
        raise EmailConfigError(
            "no email mode chosen: set TEST_EMAIL_OVERRIDE to a test inbox, or "
            "EMAIL_MODE=live to send to real addresses"
        )
    if not value:
        return None  # live: real addresses, chosen explicitly
    if not _EMAIL.fullmatch(value):
        raise EmailConfigError(f"TEST_EMAIL_OVERRIDE must be an email address, got {value!r}")
    return value


def test_mode() -> bool:
    """True when emails go to the test inbox instead of the real recipients."""
    return _override() is not None


def check_override() -> None:
    """Raise unless the email settings are valid (run at startup)."""
    _override()


def _tag(text: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-.")


def recipient(address: str) -> str:
    """The address to actually send to: unchanged, or a plus-alias in test mode."""
    override = _override()
    if override is None:
        return address
    base_local, domain = override.split("@")
    base_local = base_local.split("+")[0]  # me+kpi@ -> me@ (Gmail uses only one tag)
    local, _, original_domain = address.partition("@")
    tag = f"{_tag(local)}.{_tag(original_domain.split('.')[0])}"
    return f"{base_local}+{tag}@{domain}"


def recipients(addresses: list[str]) -> list[str]:
    return [recipient(a) for a in addresses]


def compose(to: list[str], subject: str, body: str, html: str | None = None) -> dict:
    """An email for n8n to send as-is: {to, subject, body} plus an `html` part if given.

    `body` is the plain-text version. `html` must already be safe (built with
    escaped values, e.g. packet.assemble_html).
    """
    if _override() is not None:
        banner = f"{BANNER}{', '.join(to)}"
        body = f"{banner}\n\n{body}"
        if html is not None:
            html = (
                '<p style="background:#fff3bf;padding:8px;font-family:system-ui,sans-serif">'
                f"{html_lib.escape(banner)}</p>{html}"
            )
    mail = {"to": recipients(to), "subject": subject, "body": body}
    if html is not None:
        mail["html"] = html
    return mail
