"""Minimal Agent Client Protocol (ACP) adapter for Nerya.

This is a small, dependency-free implementation of the parts of ACP that
actually matter for a trading agent: letting an IDE (Cursor / Claude Code /
VS Code / Zed) see the runtime's current capabilities, pending approvals,
and recent turns, and reply with approve/reject / free-text responses.

It speaks JSON-RPC 2.0 over stdio — the same framing ACP uses. The
surface it exposes is intentionally minimal relative to Hermes-style
editor-native agents (no file diffs, no terminal commands, no streamed
tool activity chunks), but it is dynamic where it counts:

* ``initialize`` returns the *live* capability block — enabled skills,
  approvals availability, recent-turn support, and trigger explain — so
  editors do not have to hardcode what Nerya supports.
* ``agent.capabilities`` reports the same dynamic capability info on demand
  (see :mod:`nerya.core.capabilities`).
* ``agent.skills`` lists currently loaded skills via
  ``InternalClient.skills.list``.
* ``agent.triggers.explain`` returns the route-resolution trace for a
  hypothetical trigger.
* ``agent.pending_approvals`` / ``agent.approve`` / ``agent.reject``
  drive the approval queue.
* ``agent.recent_turns`` and ``agent.submit_message`` cover the minimum
  viable IDE chat flow.

Anything richer — file diffs, terminal streams, multi-tool activity
chunks — is deliberately out of scope for this adapter.
"""

from .server import AcpServer, handle_request

__all__ = ["AcpServer", "handle_request"]
