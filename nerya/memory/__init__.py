"""Nerya's scoped memory subsystem.

``MemoryRuntime`` is the public read/write/lifecycle seam and SQLite is
the canonical store. Owned Markdown and optional external search results
are derived surfaces; neither is authoritative for scope, retention,
supersession, or forgetting. Unmanaged legacy files are not imported.

The package stays side-effect free. Import the specific surface needed:

.. code-block:: python

    from nerya.memory.runtime import MemoryRuntime
    from nerya.memory.write_rules import load_write_rules
    from nerya.memory.activity import MemoryActivityLog
"""

# Intentionally empty so importing ``nerya.memory.memsearch_index`` does not
# open SQLite or initialise an external provider.
