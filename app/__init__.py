"""Root command shim so `python -m app...` resolves modules under backend/app."""

from pathlib import Path

__path__ = [
    str(Path(__file__).resolve().parent.parent / "backend" / "app")
]
