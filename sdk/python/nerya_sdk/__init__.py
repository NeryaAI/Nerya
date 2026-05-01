"""Nerya Python SDK — end-user surface for scripts and strategies.

This is a thin layer on top of `nerya.sdk.InternalClient`. External scripts
use it to emit triggers, submit TradeIntents, and call LLM skills. Secrets
and provider keys are never exposed here."""

from .client import NeryaClient, connect

__all__ = ["NeryaClient", "connect"]
