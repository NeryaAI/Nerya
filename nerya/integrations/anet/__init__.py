"""Optional AgentNetwork P2P integration.

Loaded only when ``integrations.anet.enabled`` is true in the workspace
yaml. The package is split into three small modules so unrelated code
can ``import`` the safe ones (``whitelist``) without dragging the
``anet`` SDK in:

* :mod:`nerya.integrations.anet.whitelist` — pure, no third-party deps.
  Defines which Nerya HTTP paths can ever be exposed to peers, and
  decorates each one with a human-readable description for
  ``/anet/meta``.
* :mod:`nerya.integrations.anet.doctor` — connectivity + config
  self-check. Imports :pypi:`httpx` (already a Nerya core dep), but
  not the ``anet`` package.
* :mod:`nerya.integrations.anet.service` — the register loop. Imports
  :pypi:`anet` lazily inside functions so missing the optional pip
  extra never breaks an import-time codepath.
"""

from __future__ import annotations

__all__ = ["whitelist", "doctor", "service"]
