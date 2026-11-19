"""Vercel entrypoint. Vercel resolves `module:app` from the project root, and the package lives
in src/, so put src/ on the import path and expose the FastAPI app."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from sprinto.web import app  # noqa: E402

__all__ = ["app"]
