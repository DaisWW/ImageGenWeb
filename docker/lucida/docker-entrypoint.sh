#!/bin/sh
set -eu

if [ -d /models/lucida ] && [ -f /models/lucida/config.json ]; then
  python - <<'PY'
from pathlib import Path
import re

reg = Path('/app/bgr/registry.py')
text = reg.read_text(encoding='utf-8')
pattern = re.compile(
    r'("lucida"\s*:\s*\{\s*"model_id"\s*:\s*)(?:r)?(["\'])(.*?)(\2)(\s*,\s*"input_size"\s*:\s*1024\s*\})'
)
new_text, count = pattern.subn(r'\1"/models/lucida"\5', text, count=1)
if count:
    reg.write_text(new_text, encoding='utf-8')
    print('using local model at /models/lucida')
else:
    print('registry rewrite failed')
    print([line for line in text.splitlines() if 'lucida' in line and 'model_id' in line][:5])
PY
fi

if [ "${LUCIDA_PRELOAD_MODEL:-1}" = "1" ]; then
  python - <<'PY' || true
import os
model = os.environ.get('LUCIDA_MATTING_MODEL', 'lucida') or 'lucida'
try:
    from bgr.registry import get_segmenter
    get_segmenter(model)
    print(f'preloaded model: {model}')
except Exception as exc:
    print(f'preload skipped: {exc}')
PY
fi

exec uvicorn serving.app:app --host 0.0.0.0 --port 8000
