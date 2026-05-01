"""Prompt firewall — re-export of security.prompt_injection for the agent layer."""

from ..security.prompt_injection import (  # noqa: F401
    wrap_untrusted, flag_suspicious,
)
