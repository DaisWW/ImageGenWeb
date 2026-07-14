from __future__ import annotations

import os

__version__ = os.environ.get("APP_VERSION", "0.1.0").strip() or "0.1.0"
