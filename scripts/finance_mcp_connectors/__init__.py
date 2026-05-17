"""finance_mcp_connectors — operator CLI for ``mcp_servers.yml``.

This package is *not* part of the Nerya runtime — it's an operator
tool that wraps the runtime modules at
:mod:`nerya.mcp.connectors`. Three subcommands:

* ``list``         — show every server in the catalogue and its status.
* ``materialize``  — write the seed stub at the workspace path (idempotent).
* ``doctor``       — for each enabled server: build the adapter, run
                     ``list_tools()`` against the live transport, and
                     report tool-count + reachability + auth status.

Designed to be the operator's first stop after running
``python -m scripts.finance_skills_importer promote --apply`` in
Phase D — the finance skills are imported but won't actually call out
until at least one MCP server is reachable.
"""

from __future__ import annotations

__all__ = []
