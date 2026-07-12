"""Nerya's scoped memory subsystem.

``MemoryRuntime`` is the public read/write/lifecycle seam and SQLite is
the canonical store. Markdown, ``memory/index.jsonl``, MemSearch, and
external providers are compatibility or derived recall surfaces; none
is authoritative for scope, retention, supersession, or forgetting.

The package stays side-effect free. Import the specific surface needed:

.. code-block:: python

    from nerya.memory.runtime import MemoryRuntime
    from nerya.memory.writer import MemoryWriter  # compatibility adapter
    from nerya.memory.write_rules import load_write_rules
    from nerya.memory.activity import MemoryActivityLog
"""

# Intentionally empty so importing ``nerya.memory.memsearch_index`` does not
# open SQLite, scan legacy files, or initialise an external provider.
