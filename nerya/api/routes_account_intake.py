"""Account-intake HTTP endpoints (Plan 2026-04-29 §11 P10).

Backs the sandboxed Agent-driven account creation flow. The agent /
operator opens an intake describing what credentials a venue needs;
the dashboard renders dedicated input fields; the user submits
plaintext into ``/accounts/intake/submit`` which encrypts everything
into the vault and writes the account row through the standard
``upsert_account`` path. The agent never sees plaintext.

Routes
======

``POST /accounts/intake/start``
    Open an intake. Required fields:
    ``account_kind`` (``cex|dex|chain|perp|futures``), ``venue``,
    ``account_id``. Optional ``profile_defaults`` (mode, base
    currency, limits…), ``notes``, ``ttl_seconds``,
    ``requested_by``. Returns the public view of the intake including
    the credential schema the user must fill in.

``POST /accounts/intake/list``
    List intakes, optionally filtered by ``state``.

``POST /accounts/intake/get``
    Fetch a single intake by id (no plaintext, ever).

``POST /accounts/intake/submit``
    Apply the intake. Required: ``intake_id``, ``values`` (plaintext
    map, only used here). Optional: ``profile_overrides``, ``operator``.
    On success returns the new :class:`AccountProfile` plus the
    refreshed (post-apply) intake record.

``POST /accounts/intake/cancel``
    Mark the intake cancelled. Optional ``reason``.

Credential schema lookup
========================

``POST /accounts/intake/schema``
    Lightweight lookup for the dashboard / agent so they can preview
    the credential fields a venue needs without opening an intake.
    Required: ``venue`` plus optional ``account_kind`` (defaults to
    ``cex``). Wallet providers can pass ``account_kind: "chain"`` and
    the wallet provider id as ``venue``.
"""

from __future__ import annotations

from typing import Any

from ..security.secret_buffer import get_default_buffer
from ..trading import account_intake as intake_mod


def routes():
    def start(client, payload):
        body = payload or {}
        try:
            intake = intake_mod.open_intake(
                client.config.paths,
                account_kind=str(body.get("account_kind") or "cex"),
                venue=str(body.get("venue") or ""),
                account_id=str(body.get("account_id") or ""),
                requested_by=str(body.get("requested_by") or "operator"),
                profile_defaults=(
                    body.get("profile_defaults")
                    if isinstance(body.get("profile_defaults"), dict)
                    else None
                ),
                notes=str(body.get("notes") or ""),
                ttl_seconds=int(body.get("ttl_seconds")
                                or intake_mod.DEFAULT_TTL_SECONDS),
            )
        except intake_mod.AccountIntakeError as exc:
            return {"ok": False, "error": "intake_refused", "detail": str(exc)}
        return {"ok": True, "intake": intake.public_view()}

    def list_intakes(client, payload):
        body = payload or {}
        state = (str(body.get("state") or "").strip().lower() or None)
        intakes = intake_mod.list_intakes(client.config.paths, state=state)
        return {
            "ok": True,
            "intakes": [i.public_view() for i in intakes],
            "count": len(intakes),
        }

    def get_intake(client, payload):
        intake_id = str((payload or {}).get("intake_id") or "").strip()
        if not intake_id:
            return {"ok": False, "error": "intake_id_required"}
        try:
            intake = intake_mod.get_intake(client.config.paths, intake_id)
        except intake_mod.AccountIntakeError as exc:
            return {"ok": False, "error": "unknown_intake", "detail": str(exc)}
        return {"ok": True, "intake": intake.public_view()}

    def submit(client, payload):
        body = payload or {}
        intake_id = str(body.get("intake_id") or "").strip()
        values = body.get("values")
        if not intake_id:
            return {"ok": False, "error": "intake_id_required"}
        if not isinstance(values, dict):
            return {"ok": False, "error": "values_required"}
        operator = str(body.get("operator") or "dashboard")
        profile_overrides = body.get("profile_overrides")
        if not isinstance(profile_overrides, dict):
            profile_overrides = None
        try:
            intake, profile = intake_mod.submit_intake(
                client.config.paths,
                intake_id,
                plaintext_values={
                    str(k): str(v) for k, v in values.items()
                    if isinstance(v, (str, int, float))
                },
                profile_overrides=profile_overrides,
                operator=operator,
                secret_buffer=get_default_buffer(),
            )
        except intake_mod.AccountIntakeError as exc:
            return {"ok": False, "error": "submit_refused", "detail": str(exc)}
        return {
            "ok": True,
            "intake": intake.public_view(),
            "account": {
                "profile": profile.asdict(),
            },
        }

    def cancel(client, payload):
        body = payload or {}
        intake_id = str(body.get("intake_id") or "").strip()
        if not intake_id:
            return {"ok": False, "error": "intake_id_required"}
        try:
            intake = intake_mod.cancel_intake(
                client.config.paths,
                intake_id,
                reason=str(body.get("reason") or "operator_cancelled"),
            )
        except intake_mod.AccountIntakeError as exc:
            return {"ok": False, "error": "cancel_refused", "detail": str(exc)}
        return {"ok": True, "intake": intake.public_view()}

    def schema(client, payload):
        body = payload or {}
        venue = str(body.get("venue") or "").strip().lower()
        account_kind = str(body.get("account_kind") or "cex").strip().lower()
        if not venue:
            return {"ok": False, "error": "venue_required"}
        try:
            fields, label = intake_mod._resolve_schema(account_kind, venue)
        except intake_mod.AccountIntakeError as exc:
            return {"ok": False, "error": "unknown_venue", "detail": str(exc)}
        return {
            "ok": True,
            "venue": venue,
            "account_kind": account_kind,
            "provider_label": label,
            "credential_fields": [f.to_dict() for f in fields],
        }

    return [
        ("POST", "/accounts/intake/start", start),
        ("POST", "/accounts/intake/list", list_intakes),
        ("GET", "/accounts/intake/list", list_intakes),
        ("POST", "/accounts/intake/get", get_intake),
        ("POST", "/accounts/intake/submit", submit),
        ("POST", "/accounts/intake/cancel", cancel),
        ("POST", "/accounts/intake/schema", schema),
    ]


__all__ = ["routes"]
