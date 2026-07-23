#!/bin/sh
set -eu

if [ -d /models/lucida ] && [ -f /models/lucida/config.json ]; then
  python - <<'PY'
from pathlib import Path
reg = Path("/app/bgr/registry.py")
text = reg.read_text(encoding="utf-8")
old = '"lucida": {"model_id": "egeorcun/lucida", "input_size": 1024},'
new = '"lucida": {"model_id": "/models/lucida", "input_size": 1024},'
if old in text:
    reg.write_text(text.replace(old, new, 1), encoding="utf-8")
    print("using local model at /models/lucida")
else:
    print("registry already customized or marker missing")
PY
fi

exec uvicorn serving.app:app --host 0.0.0.0 --port 8000
