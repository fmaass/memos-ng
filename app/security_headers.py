"""The app-wide response security policy.

Its own module because two callers need it and neither can import the other: the middleware in
`app.main` that stamps it on every response, and the one route that must widen it for its own
inline script (`app.routers.native`, which `app.main` imports). Duplicating the string instead
would let the two copies drift — which is how a directive goes missing on exactly one page.
"""

import base64
import hashlib

CSP = (
    "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)

_SCRIPT_SRC = "script-src 'self'"


def csp_allowing_inline_script(script: str) -> str:
    """`CSP`, widened to permit exactly `script` inline — by its digest, never 'unsafe-inline'.

    A hash allowance is bound to those precise bytes: any other inline script on the page, and any
    edit to this one that does not re-derive the header from it, is refused by the browser rather
    than quietly permitted. Every other directive is carried over untouched.
    """
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    return CSP.replace(_SCRIPT_SRC, f"{_SCRIPT_SRC} 'sha256-{digest}'")
