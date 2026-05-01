"""The ``anet`` skill is gated twice: the skill manifest declares
``requires_integration: anet`` so the registry skips it entirely
when the integration is off, and each script re-checks the config at
runtime so an operator who only flipped the outbound sub-flag still
hits a clean refusal.
"""
