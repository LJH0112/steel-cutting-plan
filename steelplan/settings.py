"""Atomic local settings persistence; no changes to source or dependencies."""
import os
import tempfile
import threading
from pathlib import Path

from .materials import DEFAULT_STOCK_LENGTHS
from .models import StockSettings

SETTINGS_PATH = Path(__file__).resolve().parent.parent / 'data' / 'stock_settings.json'
SETTINGS_LOCK = threading.Lock()


def load_settings():
    with SETTINGS_LOCK:
        if not SETTINGS_PATH.exists():
            return StockSettings(stock_lengths_by_type=DEFAULT_STOCK_LENGTHS)
        return StockSettings.model_validate_json(SETTINGS_PATH.read_text(encoding='utf-8'))


def save_settings(settings):
    with SETTINGS_LOCK:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=SETTINGS_PATH.parent, delete=False) as handle:
                name = handle.name
                handle.write(settings.model_dump_json(indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, SETTINGS_PATH)
        finally:
            if name and Path(name).exists():
                Path(name).unlink()
