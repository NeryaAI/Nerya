"""Memory isolation tests for the Trading Evidence Vault.

These tests lock in the ACL invariant promised by ``EvidenceStore.search``:

* ``scope="shared"``    - only shared docs.
* ``scope="strategy"``  - only that strategy's docs (caller's ``strategy_id``).
* ``scope="session"``   - only that session's docs (caller's ``session_id``).
* ``scope="any"``       - operator-level view; ``shared`` docs always visible,
  ``strategy``/``session`` docs visible only when the caller passes the
  matching id. Strategy/session evidence **never** leaks across strategies or
  sessions.

Failures here mean private trading research is leaking into operator search.
"""

from __future__ import annotations

import pytest

from nerya.evidence.store import EvidenceStore


pytestmark = pytest.mark.smoke


def _seed_store(tmp_path) -> EvidenceStore:
    store = EvidenceStore(workspace_root=tmp_path)
    # 2 strategy-private docs for two different strategies
    store.ingest(
        source_type="strategy",
        source_id="strategy:alpha:proposal:p_001",
        title="Alpha proposal",
        body="Alpha's private momentum tuning.",
        scope="strategy",
        strategy_id="alpha",
    )
    store.ingest(
        source_type="strategy",
        source_id="strategy:beta:proposal:p_001",
        title="Beta proposal",
        body="Beta's private mean-revert tuning.",
        scope="strategy",
        strategy_id="beta",
    )
    # 1 session-private doc
    store.ingest(
        source_type="memory",
        source_id="session:s1:scratch",
        title="Session scratchpad",
        body="Operator-only thoughts.",
        scope="session",
        session_id="s1",
    )
    # 1 shared doc (everyone can see it)
    store.ingest(
        source_type="research",
        source_id="research:market_note_001",
        title="Shared market note",
        body="Public note about market regime.",
        scope="shared",
    )
    return store


def _titles(rows) -> set[str]:
    return {row["title"] for row in rows}


# ---------------------------------------------------------------------------
# scope="shared"
# ---------------------------------------------------------------------------


def test_shared_scope_only_returns_shared_docs(tmp_path):
    store = _seed_store(tmp_path)
    titles = _titles(store.search(scope="shared"))
    assert titles == {"Shared market note"}


def test_shared_scope_ignores_strategy_id_argument(tmp_path):
    store = _seed_store(tmp_path)
    titles = _titles(store.search(scope="shared", strategy_id="alpha"))
    # passing a strategy id does not pull alpha's strategy doc into shared scope
    assert titles == {"Shared market note"}


# ---------------------------------------------------------------------------
# scope="strategy"
# ---------------------------------------------------------------------------


def test_strategy_scope_requires_matching_id(tmp_path):
    store = _seed_store(tmp_path)
    # without strategy_id we must see nothing under strategy scope
    assert store.search(scope="strategy") == []
    # alpha caller sees alpha doc only
    titles = _titles(store.search(scope="strategy", strategy_id="alpha"))
    assert titles == {"Alpha proposal"}
    # beta caller sees beta doc only
    titles = _titles(store.search(scope="strategy", strategy_id="beta"))
    assert titles == {"Beta proposal"}


def test_strategy_scope_does_not_leak_across_strategies(tmp_path):
    store = _seed_store(tmp_path)
    alpha_rows = store.search(scope="strategy", strategy_id="alpha")
    beta_titles = {row["title"] for row in alpha_rows if row["title"].startswith("Beta")}
    assert beta_titles == set(), "beta evidence leaked into alpha scope=strategy search"


# ---------------------------------------------------------------------------
# scope="session"
# ---------------------------------------------------------------------------


def test_session_scope_requires_matching_id(tmp_path):
    store = _seed_store(tmp_path)
    assert store.search(scope="session") == []
    titles = _titles(store.search(scope="session", session_id="s1"))
    assert titles == {"Session scratchpad"}
    # different session id sees nothing
    assert store.search(scope="session", session_id="s_other") == []


# ---------------------------------------------------------------------------
# scope="any" — operator-level view, ACL filtered
# ---------------------------------------------------------------------------


def test_any_scope_without_ids_returns_only_shared(tmp_path):
    store = _seed_store(tmp_path)
    titles = _titles(store.search(scope="any"))
    assert titles == {"Shared market note"}, (
        "operator scope=any must not leak strategy/session-private docs when "
        "no strategy_id/session_id is provided"
    )


def test_any_scope_with_strategy_id_does_not_leak_other_strategies(tmp_path):
    store = _seed_store(tmp_path)
    alpha_titles = _titles(store.search(scope="any", strategy_id="alpha"))
    assert alpha_titles == {"Alpha proposal", "Shared market note"}, (
        "scope=any with strategy_id=alpha must only return alpha + shared; "
        "beta evidence must not leak"
    )

    # cross-strategy probe: pass beta id, alpha doc must NOT appear
    beta_titles = _titles(store.search(scope="any", strategy_id="beta"))
    assert "Alpha proposal" not in beta_titles
    assert beta_titles == {"Beta proposal", "Shared market note"}


def test_any_scope_with_session_id_does_not_leak_other_sessions(tmp_path):
    store = _seed_store(tmp_path)
    s1_titles = _titles(store.search(scope="any", session_id="s1"))
    assert s1_titles == {"Session scratchpad", "Shared market note"}

    # other session sees only shared
    other_titles = _titles(store.search(scope="any", session_id="s_other"))
    assert other_titles == {"Shared market note"}


def test_any_scope_with_both_ids_returns_full_operator_view(tmp_path):
    store = _seed_store(tmp_path)
    titles = _titles(store.search(scope="any", strategy_id="alpha", session_id="s1"))
    assert titles == {"Alpha proposal", "Session scratchpad", "Shared market note"}, (
        "operator with both strategy and session ids should see all three "
        "scopes that match their identity"
    )


def test_any_scope_unknown_record_scope_is_treated_as_private(tmp_path):
    """A doc written with an unrecognized scope label must not leak."""

    store = EvidenceStore(workspace_root=tmp_path)
    store.ingest(
        source_type="research",
        source_id="research:weird:doc",
        title="Weird scope doc",
        body="should not be visible without explicit scope match",
        scope="custom_unknown",
    )
    titles = _titles(store.search(scope="any"))
    assert titles == set(), (
        "doc with unknown scope label must be hidden from operator scope=any view"
    )


# ---------------------------------------------------------------------------
# Filters compose with ACL (source_type / topic)
# ---------------------------------------------------------------------------


def test_source_type_filter_respects_acl(tmp_path):
    store = _seed_store(tmp_path)
    # research source_type is shared in our seed; operator scope=any should see it
    titles = _titles(store.search(scope="any", source_type="research"))
    assert titles == {"Shared market note"}

    # strategy source_type is private; operator scope=any without id must see nothing
    assert store.search(scope="any", source_type="strategy") == []

    # but with the right strategy_id we see the matching one
    titles = _titles(store.search(scope="any", source_type="strategy", strategy_id="alpha"))
    assert titles == {"Alpha proposal"}
