"""Exception hierarchy for the wallet layer."""

from __future__ import annotations


class WalletError(Exception):
    """Base for all wallet-layer failures."""


class WalletDependencyError(WalletError):
    """Raised when a provider's optional dependency is missing.

    Attributes
    ----------
    provider:
        Provider id (``self_custody``, ``okx_os`` ...).
    missing:
        List of dependency identifiers (e.g. ``["pip:goat-sdk", "npm:@goat-sdk/core"]``).
    install_hint:
        Human-readable instructions — a shell command the user should run.
    """

    def __init__(self, provider: str, missing: list[str], install_hint: str):
        self.provider = provider
        self.missing = list(missing)
        self.install_hint = install_hint
        super().__init__(
            f"wallet provider {provider!r} is not ready: missing {missing}. "
            f"install with: {install_hint}"
        )


class WalletPolicyDenied(WalletError):
    """The operation is blocked by signer policy / kill switch."""


class WalletProviderNotFound(WalletError):
    """Operator asked for a wallet provider name that does not exist."""


class WalletTransportError(WalletError):
    """A provider backend transport failure (HTTP 5xx, RPC error, timeout,
    subprocess crash).

    Deliberately distinct from :class:`WalletPolicyDenied`: a transport
    failure is transient/operational and must not be reported to the
    operator as "policy denied" — approval resume and HTTP routes use
    this to classify honest provider outages.
    """


class WalletQuoteError(WalletError):
    """A quote could not be parsed or is degenerate (no positive output).

    Raised instead of silently returning ``expected_out=0`` so the
    approval flow can refuse to freeze a meaningless floor.
    """
