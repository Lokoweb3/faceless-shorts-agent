"""
Shared hardening for the local aiohttp dashboards (dashboard.py,
caption_dashboard.py) — same model as channel_dashboard.py:

1. A random token is generated at startup and shown only in the terminal.
   The first visit (via the printed URL) exchanges it for an HttpOnly,
   SameSite=Strict cookie; every request must carry token or cookie.
2. The Host header must be a localhost name — a DNS-rebound hostname that
   resolves to 127.0.0.1 fails before auth is even considered.

Without this, any local process, LAN device (if bound wider), or web page
driving the user's browser could hit action endpoints like /api/run.
"""

import hmac
import secrets

from aiohttp import web

COOKIE_NAME = "cc_auth"

LOCKED_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Locked</title></head>
<body style="font-family:system-ui;background:#0a0d12;color:#eef3f9;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0">
<div style="max-width:460px;text-align:center">
<h2>🔒 This dashboard is locked</h2>
<p style="color:#7e8a9a;line-height:1.6">Open the exact URL printed in the
terminal where you started it — it contains a one-time access token.<br><br>
If you restarted it, a new token was generated: check the terminal again.</p>
</div></body></html>"""


def make_token() -> str:
    return secrets.token_urlsafe(24)


def _host_name(raw: str) -> str:
    """Hostname part of a Host header ('[::1]:8787' -> '::1')."""
    raw = (raw or "").strip().lower()
    if raw.startswith("["):
        return raw[1:raw.find("]")] if "]" in raw else raw
    return raw.rsplit(":", 1)[0] if ":" in raw else raw


def security_middleware(token: str):
    """aiohttp middleware enforcing the token + Host checks above."""

    @web.middleware
    async def mw(request, handler):
        if _host_name(request.headers.get("Host", "")) not in (
                "127.0.0.1", "localhost", "::1"):
            return web.Response(status=403,
                                text="Forbidden: unexpected Host header.")
        supplied = (request.query.get("token", "")
                    or request.cookies.get(COOKIE_NAME, ""))
        if not hmac.compare_digest(supplied, token):
            if request.path == "/":
                return web.Response(status=403, text=LOCKED_HTML,
                                    content_type="text/html")
            return web.json_response({"error": "unauthorized"}, status=403)
        if request.query.get("token"):
            # First visit: swap the URL token for an HttpOnly cookie and
            # clean the address bar.
            resp = web.HTTPSeeOther(request.path)
            resp.set_cookie(COOKIE_NAME, token, httponly=True,
                            samesite="Strict", path="/")
            return resp
        return await handler(request)

    return mw
