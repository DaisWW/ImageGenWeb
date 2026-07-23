from __future__ import annotations

import os
import secrets
import threading

from flask import abort, request
from waitress import create_server

from imagegen import create_app

app = create_app()
token = os.environ["E2E_SHUTDOWN_TOKEN"]
server = create_server(
    app,
    host=os.environ.get("IMAGE_WEB_HOST", "127.0.0.1"),
    port=int(os.environ.get("IMAGE_WEB_PORT", "18765")),
    threads=8,
    channel_timeout=700,
)


@app.post("/__e2e_shutdown")
def shutdown():
    if not secrets.compare_digest(request.headers.get("X-E2E-Shutdown-Token", ""), token):
        abort(404)
    threading.Timer(0.1, server.close).start()
    return {"ok": True}


server.run()
