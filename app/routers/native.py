"""The two browser/Apple-facing URLs a native device login needs, outside `/api`.

Both are anonymous and both must beat the SPA catch-all in `app.main` — Apple fetches the
association file with no cookies and will not accept an HTML body, and the callback page is what
the authentication session lands on when no entitled app is installed to intercept it. They are
registered as a router extended into the app BEFORE the catch-all is declared, which is what
decides precedence (`app.router.routes` is matched in order).
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import settings
from app.services.native_auth import aasa_document

router = APIRouter(tags=["native"])

# No inline script and no SPA bundle: the page's only job is to say the words, and the app — when
# installed — intercepts this URL before a browser ever renders it. The marker attribute is the
# contract the precedence test checks; the SPA shell would carry `id="app"` instead.
CALLBACK_PAGE = """<!doctype html>
<html lang="en" data-memosng-native-callback>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>memos-ng</title>
</head>
<body>
<h1>Open the memos-ng app</h1>
<p>Sign-in finished. Open the memos-ng app on this device to continue.</p>
<p>If nothing happens, the app is not installed on this device.</p>
</body>
</html>
"""


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
async def ios_auth_callback() -> HTMLResponse:
    """Where a device login lands when no app intercepts it. Carries no code handling of its own —
    the handoff code is in the URL the app reads, and this page must never help a browser use it.
    """
    return HTMLResponse(CALLBACK_PAGE)
