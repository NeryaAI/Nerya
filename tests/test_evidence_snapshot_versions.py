import hashlib

import pytest

from nerya.evidence.store import EvidenceStore
from nerya.evidence.ingest import ingest_research

pytestmark = pytest.mark.smoke


def test_research_snapshots_append_redacted_versions_and_keep_dates(tmp_path):
    store = EvidenceStore(tmp_path)
    first = ingest_research(store, provider="fixture", artifact_id="note", title="Source",
                            body="api_key=secret12345 revenue=100", as_of="2026-01-01",
                            retrieved_at="2026-01-02T09:00:00Z")
    old_bytes = (tmp_path / first.workspace_path).read_bytes()
    second = ingest_research(store, provider="fixture", artifact_id="note", title="Update", body="revenue=110")
    assert first.content_sha256 == hashlib.sha256(first.body.encode()).hexdigest()
    assert "secret12345" not in first.body
    assert second.previous_evidence_id == first.evidence_id
    assert second.content_sha256 != first.content_sha256
    assert (tmp_path / first.workspace_path).read_bytes() == old_bytes
    assert first.provenance.as_of == "2026-01-01"
    assert first.provenance.retrieved_at == "2026-01-02T09:00:00Z"
    assert second.provenance.as_of == second.provenance.retrieved_at == ""


def test_research_lineage_never_crosses_private_scope(tmp_path):
    store = EvidenceStore(tmp_path)
    arguments = dict(source_type="research", source_id="note", title="Private", body="evidence", scope="session")
    first = store.ingest(**arguments, session_id="one")
    other = store.ingest(**arguments, session_id="two")
    next_one = store.ingest(**arguments, session_id="one")
    assert not other.previous_evidence_id
    assert next_one.previous_evidence_id == first.evidence_id
    assert [row["evidence_id"] for row in store.search(scope="session", session_id="two")] == [other.evidence_id]


def test_locators_exclude_userinfo_and_signed_secrets(tmp_path):
    store = EvidenceStore(tmp_path)
    doc = ingest_research(store, provider="fixture", artifact_id="note", title="Source", body="Public",
        source_locator="https://username:private-password@example.com/report?id=42&access_token=abc-secret&signature=signed-secret")
    rendered = store.get(doc.evidence_id)
    assert "private-password" not in str(rendered) and "abc-secret" not in str(rendered) and "signed-secret" not in str(rendered)
    assert "id=42" in doc.provenance.source_locator
    assert doc.security.contains_secret


def test_nonresearch_ingest_does_not_scan_a_growing_index(tmp_path, monkeypatch):
    store = EvidenceStore(tmp_path)
    monkeypatch.setattr(EvidenceStore, "_iter_index", lambda self: (_ for _ in ()).throw(AssertionError("full scan")))
    doc = store.ingest(source_type="trade", source_id="fill", title="Fill", body="filled")
    assert doc.content_sha256 and not doc.previous_evidence_id
