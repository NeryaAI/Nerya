"""Local HTTP server exposing read-only views and trigger/SDK endpoints.

This is a minimal stdlib implementation — FastAPI is not a hard dependency.
Start with `nerya.api.local_server.serve(config, host, port)`.
"""

from .local_server import build_server, serve

__all__ = ["build_server", "serve"]
