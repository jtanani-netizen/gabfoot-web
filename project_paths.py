from __future__ import annotations

from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent
CARDS_DIR = APP_ROOT / "cards"
CACHE_DIR = APP_ROOT / ".cache"
ENV_FILE = APP_ROOT / ".env"
