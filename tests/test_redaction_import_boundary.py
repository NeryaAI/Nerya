from __future__ import annotations

import importlib


def test_redaction_toggle_is_snapshotted_at_import_and_withholds_text(monkeypatch) -> None:
    import nerya.core.redaction as redaction

    monkeypatch.setenv("NERYA_REDACT_ENABLED", "0")
    disabled = importlib.reload(redaction)
    try:
        assert disabled._REDACT_ENABLED == "0"
        assert disabled.redact_text("sk-test-secret-value-1234567890") == (
            "***REDACTION_DISABLED_AT_IMPORT_TEXT_WITHHELD***"
        )

        monkeypatch.setenv("NERYA_REDACT_ENABLED", "1")
        # The module keeps the import-time snapshot until it is reloaded.
        assert disabled.redact_text("sk-test-secret-value-1234567890") == (
            "***REDACTION_DISABLED_AT_IMPORT_TEXT_WITHHELD***"
        )
    finally:
        monkeypatch.setenv("NERYA_REDACT_ENABLED", "1")
        importlib.reload(redaction)

