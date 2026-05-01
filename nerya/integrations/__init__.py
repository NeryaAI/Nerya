"""``nerya.integrations`` namespace.

Each submodule under this package is an *optional* third-party
integration. None of them is imported by core Nerya at startup; the
operator opts in via ``integrations.<name>.enabled: true`` in
``nerya.yml``. See :mod:`nerya.integrations.anet` for the canonical
pattern.
"""
