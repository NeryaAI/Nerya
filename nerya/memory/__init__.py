"""Nerya memory subsystem.

Two layers:

* ``nerya.agent.memory_index.MemoryIndex`` — structured fact log at
  ``<workspace>/memory/index.jsonl``. The source of truth for "what
  does Nerya remember about this operator / strategy".
* ``nerya.memory.memsearch_index`` — optional vector index over the
  markdown notes + the fact log. Off by default, opt-in via
  ``memory.vector_search.enabled``.

Submodules ``nerya.memory.write_rules``, ``nerya.memory.writer``, and
``nerya.memory.activity`` add a write-rule + activity-log layer on top.
They are NOT imported eagerly here because ``nerya.memory.writer``
pulls in ``nerya.agent.memory_index`` which in turn drags in the agent
kernel (and its heavy strategy imports). Callers should import the
submodule they need directly:

.. code-block:: python

    from nerya.memory.writer import MemoryWriter
    from nerya.memory.write_rules import load_write_rules
    from nerya.memory.activity import MemoryActivityLog
"""

# Intentionally empty — keep the package import side-effect-free so that
# ``from nerya.memory import memsearch_index`` does not pull in the
# heavy agent-kernel transitive imports.
