"""The two pages people open from an email: approval and owner reply. Pure HTML.

Every dynamic value goes through esc(). Pages are served with HEADERS: no
caching (they show client data), no Referer (the token is in the URL), no
framing, and a content policy that allows only inline styles and posting
forms back to this server.
"""

import html
from urllib.parse import parse_qs

esc = html.escape

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    ),
}

STYLE = """
body { font-family: system-ui, sans-serif; max-width: 46rem; margin: 2rem auto;
       padding: 0 1rem; line-height: 1.5; color: #1b1b1b; }
pre { white-space: pre-wrap; background: #f6f6f6; padding: 1rem; border-radius: 6px; }
label { display: block; font-weight: 600; margin-top: 1rem; }
textarea { width: 100%; min-height: 5rem; font: inherit; padding: .5rem; }
button { font: inherit; padding: .6rem 1.2rem; margin: 1rem .5rem 0 0; cursor: pointer; }
.error { color: #a40000; font-weight: 600; }
.muted { color: #666; }
"""

REPLY_FIELDS = (
    ("what_changed", "What changed?"),
    ("why", "Why did it change?"),
    ("owner_of_fix", "Who owns the fix?"),
    ("actions", "What's being done?"),
)


def page(title: str, body_html: str) -> str:
    """A complete page. `body_html` must already be escaped."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title><style>{STYLE}</style></head>"
        f"<body>{body_html}</body></html>"
    )


def message(title: str, text: str) -> str:
    return page(title, f"<h1>{esc(title)}</h1><p>{esc(text)}</p>")


def parse_form(body: bytes) -> dict[str, str]:
    """application/x-www-form-urlencoded -> {name: value} (first value of each)."""
    fields = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {k: v[0] for k, v in fields.items()}


def approval(token: str, client_name: str, week: str, packet_html: str) -> str:
    """Shows the packet; nothing is decided until the form is submitted.

    `packet_html` comes from packet.assemble_html(), which escapes every value,
    so it is inserted as-is.
    """
    return page(
        f"Approve the KPI packet: {client_name}",
        f"<h1>KPI packet: {esc(client_name)}</h1>"
        f'<p class="muted">Week of {esc(week)}. Approving lets it be sent to leadership; '
        "rejecting sends it back to be rebuilt. This link works once.</p>"
        f"{packet_html}"
        '<form method="post" action="/approve">'
        f'<input type="hidden" name="token" value="{esc(token)}">'
        '<label for="notes">Notes (optional; say why if you reject)</label>'
        '<textarea id="notes" name="notes"></textarea>'
        '<button type="submit" name="decision" value="approved">Approve</button>'
        '<button type="submit" name="decision" value="rejected">Reject</button>'
        "</form>",
    )


def reply_form(
    token: str,
    kpi_name: str,
    week: str,
    owner_name: str,
    values: dict[str, str] | None = None,
    error: str | None = None,
) -> str:
    """The four questions. `values` refills the form after a validation error."""
    values = values or {}
    fields = "".join(
        f'<label for="{name}">{esc(label)}</label>'
        f'<textarea id="{name}" name="{name}" required>{esc(values.get(name, ""))}</textarea>'
        for name, label in REPLY_FIELDS
    )
    error_html = f'<p class="error">{esc(error)}</p>' if error else ""
    return page(
        f"{kpi_name}: your reply",
        f"<h1>{esc(kpi_name)}</h1>"
        f'<p class="muted">Week of {esc(week)}. Hi {esc(owner_name.split()[0])}: your answers '
        "go into the packet word for word. This link works once.</p>"
        f"{error_html}"
        '<form method="post" action="/reply">'
        f'<input type="hidden" name="token" value="{esc(token)}">'
        f'{fields}<button type="submit">Send reply</button></form>',
    )
