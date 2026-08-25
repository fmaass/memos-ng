"""The two browser/Apple-facing URLs a native device login needs, outside `/api`.

Both are anonymous and both must beat the SPA catch-all in `app.main` — Apple fetches the
association file with no cookies and will not accept an HTML body, and the callback page is what
the browser running the login lands on when no entitled app intercepts it. They are registered as
a router extended into the app BEFORE the catch-all is declared, which is what decides precedence
(`app.router.routes` is matched in order).
"""

from html import escape
from string import Template
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import settings
from app.security_headers import csp_allowing_inline_script
from app.services.native_auth import aasa_document

router = APIRouter(tags=["native"])

# The app's custom scheme — the second lane home. A universal link only intercepts the HTTPS
# callback on a device that has already been handed this domain's association; the scheme needs no
# association at all, so it is what still works on a fresh install or while Apple's CDN catches up.
RETURN_SCHEME_URL = "memosng://auth-callback"

# Deliberately constant: the URL is read back out of the rendered link instead of being written
# into the script, so the automatic attempt and the visible button are the same URL by
# construction — and the digest this script is allowed to run under stays a compile-time constant.
RETURN_SCRIPT = """
var link = document.querySelector("a[href]");
if (link) { location.href = link.getAttribute("href"); }
"""

# `script-src 'self'` (app-wide) would refuse the inline attempt and leave the button as the only
# lane that ever fires, so this response carries the app-wide policy widened by that one digest.
CALLBACK_CSP = csp_allowing_inline_script(RETURN_SCRIPT)

# No bundle and no third-party asset: this page renders in a browser that is on its way out, and
# every byte of it has to arrive in one response. The marker attribute on <html> is the contract
# the precedence test checks — the SPA shell would carry `id="app"` instead.
CALLBACK_PAGE = Template("""<!doctype html>
<html lang="en" data-memosng-native-callback>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>memosng</title>
<style>
  :root { color-scheme: light dark; }
  body {
    margin: 0 auto; padding: 4rem 1.25rem; max-width: 26rem; text-align: center;
    font: 1rem/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    background: #fff; color: #1c1c1e;
  }
  @media (prefers-color-scheme: dark) { body { background: #111214; color: #e6e6e8; } }
  h1 { font-size: 1.35rem; margin: 0 0 .5rem; }
  p { margin: 0 0 2rem; opacity: .7; font-size: .95rem; }
  a.return {
    display: inline-block; padding: .8rem 1.6rem; border-radius: .7rem;
    background: #0a84ff; color: #fff; font-weight: 600; text-decoration: none;
  }
</style>
</head>
<body>
<h1>$heading</h1>
<p>If nothing happens, tap the button.</p>
<a class="return" href="$url">Return to memosng</a>
<script>$script</script>
</body>
</html>
""")


@router.get("/.well-known/apple-app-site-association", include_in_schema=False)
async def apple_app_site_association() -> JSONResponse:
    """Claim this domain for the app, or 404 while there is no app to claim it for.

    404 rather than an empty document: Apple's CDN caches what it fetches, and a served-but-empty
    association is a claim of "no capabilities" that outlives the config change enabling them.
    """
    if not settings.APPLE_TEAM_ID or not settings.IOS_BUNDLE_ID:
        raise HTTPException(status_code=404, detail="Not found")
    return JSONResponse(
        aasa_document(settings.APPLE_TEAM_ID, settings.IOS_BUNDLE_ID, settings.AASA_KEYS)
    )


@router.get("/ios/auth/callback", include_in_schema=False)
async def ios_auth_callback(request: Request) -> HTMLResponse:
    """Where a device login lands, and how the browser gets back into the app from there.

    The universal link is the fast path, but it only intercepts this URL on a device that already
    holds this domain's association; where it does not — a fresh install, or Apple's CDN not having
    handed the association out yet — the browser simply stops on this response. So the page carries
    the SAME parameters on over the custom scheme: automatically where a browser allows a
    script-initiated scheme navigation, and behind a visible button where it does not.

    It forwards; it does not use. This route runs no exchange of its own (that is
    `/api/auth/callback` and `/api/auth/native/exchange`), and the handoff code it passes along is
    inert without the PKCE verifier that never left the device — so the scheme URL hands a browser
    nothing it was not already holding in its address bar.
    """
    # Re-encoded from the parsed pairs rather than pasted through: the app reads these as
    # parameters, and normalising the encoding here is what keeps a value carrying `&` or `=` one
    # value instead of two. Order and blanks are preserved, and nothing is added or dropped.
    params = parse_qsl(request.url.query, keep_blank_values=True)
    query = urlencode(params)
    url = f"{RETURN_SCHEME_URL}?{query}" if query else RETURN_SCHEME_URL
    refused = any(key == "error" for key, _ in params)
    return HTMLResponse(
        CALLBACK_PAGE.substitute(
            heading="Returning to memosng…" if refused else "Signed in — returning to memosng…",
            url=escape(url, quote=True),
            script=RETURN_SCRIPT,
        ),
        headers={
            "Content-Security-Policy": CALLBACK_CSP,
            # The body now carries a live one-shot handoff code; no cache may keep it.
            "Cache-Control": "no-store",
        },
    )
