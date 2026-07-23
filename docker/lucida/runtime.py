from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from bgr.registry import MODEL_SPECS
from fastapi import HTTPException
from serving.app import _load_segmenter, app

MODEL = os.environ.get("LUCIDA_MATTING_MODEL", "lucida").strip() or "lucida"
LOCAL_MODEL = Path("/models/lucida")
_segmenter = None


def _preload_model() -> None:
    global _segmenter
    if MODEL.partition("+")[0] == "lucida" and (LOCAL_MODEL / "config.json").is_file():
        MODEL_SPECS["lucida"]["model_id"] = str(LOCAL_MODEL)
    _segmenter = _load_segmenter(MODEL)


@asynccontextmanager
async def _lifespan(_app):
    _preload_model()
    yield


@app.get("/ready")
def ready() -> dict[str, str]:
    if _segmenter is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    return {
        "status": "ready",
        "model": MODEL,
        "device": str(getattr(_segmenter, "device", "unknown")),
    }


app.router.lifespan_context = _lifespan
