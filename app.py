from __future__ import annotations

import os

from imagegen import create_app

app = create_app()


if __name__ == "__main__":
    from waitress import serve

    serve(
        app,
        host=os.environ.get("IMAGE_WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("IMAGE_WEB_PORT", "7860")),
        threads=8,
        channel_timeout=700,
    )
