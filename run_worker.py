from __future__ import annotations

import logging
import signal

from app import app
from imagegen.worker import GenerationWorker

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker = GenerationWorker(
        app,
        app.extensions["channel_registry"],
        app.extensions["image_storage"],
    )

    def stop_worker(_signum, _frame) -> None:
        worker.stop()

    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)
    try:
        worker.run_forever()
    finally:
        app.extensions["imagegen_services"].close()
