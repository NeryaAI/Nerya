"""Messaging pipeline — renders and dispatches outgoing messages.

Channels are configured in `workspace/messages/channels.yml`. Secrets live
in the Vault; the agent never sees tokens directly."""

from .pipeline import MessagePipeline
from .rate_limits import RateLimiter
from .templates import render
from .mirror import GatewayMirror, MirrorEntry, SessionContext

__all__ = [
    "MessagePipeline", "RateLimiter", "render",
    "GatewayMirror", "MirrorEntry", "SessionContext",
]
