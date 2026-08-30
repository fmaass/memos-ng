"""The daily digest pipeline (MEMNG-234), ported from the `daily-dami-memos-mail` satellite.

One module per concern, in the order a run walks them:

* `gate` — is today's run due? (pure, no I/O, no clock of its own)
* `select` — which memos, and which are deferred for media that is not ready yet
* `render` — Markdown → sanitised HTML → the satellite's Jinja2 mail body + a plain alternative
* `hosting` — Dropbox upload + shared link for video (and, optionally, full-size photo) media
* `splitting` — greedy split into parts under `max_email_bytes`, with photo shedding
* `mailer` — the MIME message and the SMTP transport behind `to_thread.run_sync`
* `runner` — the run row, the claim CAS, the per-part commit, the sweeps
* `metrics` — the process-local snapshot the app's `/metrics` collector reads

Nothing here imports a router, and `runner` is the only module that writes to the database.
"""
