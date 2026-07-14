from __future__ import annotations

import logging

from app import app
from imagegen.worker import GenerationWorker

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    GenerationWorker(
        app,
        app.extensions["channel_registry"],
        app.extensions["image_storage"],
    ).run_forever()
