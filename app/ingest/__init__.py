"""The ingest installation (trip pass P2): the guest door (`APP_ROLE=ingest`) and the interface home
pulls from (`APP_ROLE=ingest-home`), served from the same image as home by `app.ingest.asgi:app`.

Nothing in this package imports `app.routers`, `app.auth` or `app.main`: a guest principal never
meets a member route because the member routes are never loaded here (tests/test_ingest_roles.py
proves it in a fresh process).
"""
