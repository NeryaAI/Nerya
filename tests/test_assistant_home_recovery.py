"""Assistant home recovery using only temporary workspace state."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from nerya.agent.assistants import AssistantError, AssistantStore
from nerya.agent.history_mutations import delete_session, is_session_deleted
from nerya.agent.session import SessionStore
from nerya.core.paths import WorkspacePaths
from nerya.db.repositories import AgentSessionRepository
from nerya.db.sqlite import connect

pytestmark = pytest.mark.smoke


@pytest.mark.parametrize("reset_binding", [False, True])
def test_deleted_home_recovers_after_history_reset(tmp_path, reset_binding):
    paths = WorkspacePaths(tmp_path)
    store = AssistantStore(paths)
    profile = store.save({"name": "Scout", "model_provider": "fixture", "model_id": "specialist"})
    old_sid = store.home(profile["id"])["session_id"]
    with closing(connect(paths.db)) as con:
        AgentSessionRepository(con).record_message(
            message_id="old-message", session_id=old_sid, role="user", content="Old history",
        )
    delete_session(paths, {"session_id": old_sid})
    if reset_binding:
        with closing(connect(paths.db)) as con:
            con.execute("DELETE FROM assistant_sessions WHERE session_id=?", (old_sid,))

    recovered = AssistantStore(paths).home(profile["id"])

    assert recovered["session_id"] != old_sid
    assert recovered["assistant_id"] == profile["id"]
    assert recovered["assistant_name"] == profile["name"]
    assert AssistantStore(paths).home(profile["id"]) == recovered
    assert store.get(profile["id"]) == profile
    assert [row["session_id"] for row in store.conversations(profile["id"])] == [recovered["session_id"]]
    assert SessionStore(tmp_path).load(old_sid) is None
    with closing(connect(paths.db)) as con:
        repo = AgentSessionRepository(con)
        assert is_session_deleted(con, old_sid)
        assert repo.get_session(old_sid) is None
        assert repo.transcript(old_sid) == []
        assert repo.transcript(recovered["session_id"]) == []
    with pytest.raises(AssistantError, match="^session_deleted$"):
        store.bind(old_sid, profile["id"])


def test_concurrent_recovery_reuses_one_home_per_assistant(tmp_path):
    paths = WorkspacePaths(tmp_path)
    store = AssistantStore(paths)
    aids = ["main", store.save({"name": "Scout"})["id"]]
    old_sids = {aid: store.home(aid)["session_id"] for aid in aids}
    for sid in old_sids.values():
        delete_session(paths, {"session_id": sid})
    barrier = Barrier(8)

    def recover(aid):
        barrier.wait(timeout=10)
        return AssistantStore(paths).home(aid)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(recover, aids * 4))

    assert len({result["session_id"] for result in results}) == 2
    for aid in aids:
        homes = {result["session_id"] for result in results if result["assistant_id"] == aid}
        assert len(homes) == 1
        sid = homes.pop()
        assert sid != old_sids[aid]
        assert AssistantStore(paths).home(aid)["session_id"] == sid
        assert store.for_session(sid)["id"] == aid
        assert {row["session_id"] for row in store.conversations(aid)} == {sid}
        assert SessionStore(tmp_path).load(sid).meta["conversation_kind"] == "assistant"


def test_redeleting_home_advances_durably_without_resurrecting_ancestors(tmp_path):
    paths = WorkspacePaths(tmp_path)
    deleted = set()
    for _ in range(3):
        store = AssistantStore(paths)
        sid = store.home("main")["session_id"]
        assert sid not in deleted
        delete_session(paths, {"session_id": sid})
        deleted.add(sid)
        recovered = AssistantStore(paths).home("main")
        assert recovered["session_id"] not in deleted
        assert AssistantStore(paths).home("main") == recovered
        with closing(connect(paths.db)) as con:
            assert {row[0] for row in con.execute("SELECT session_id FROM agent_deleted_sessions")} == deleted
            for old_sid in deleted:
                assert AgentSessionRepository(con).get_session(old_sid) is None
                assert SessionStore(tmp_path).load(old_sid) is None


def test_recovery_leaves_retained_tombstone_and_old_history_untouched(tmp_path):
    paths = WorkspacePaths(tmp_path)
    store = AssistantStore(paths)
    old_sid = store.home("main")["session_id"]
    session_path = tmp_path / "sessions" / f"{old_sid}.json"
    old_file = session_path.read_bytes()
    with closing(connect(paths.db)) as con:
        repo = AgentSessionRepository(con)
        repo.record_message(message_id="retained", session_id=old_sid, role="user", content="Retained history")
        con.execute("INSERT INTO agent_deleted_sessions(session_id, deleted_at) VALUES (?, 123)", (old_sid,))
        old_session = repo.get_session(old_sid)
        old_messages = [tuple(row) for row in con.execute("SELECT * FROM agent_messages WHERE session_id=?", (old_sid,))]

    recovered = store.home("main")

    assert recovered["session_id"] != old_sid
    assert session_path.read_bytes() == old_file
    assert {row["session_id"] for row in store.conversations("main")} == {recovered["session_id"]}
    with closing(connect(paths.db)) as con:
        assert con.execute("SELECT deleted_at FROM agent_deleted_sessions WHERE session_id=?", (old_sid,)).fetchone()[0] == 123
        assert AgentSessionRepository(con).get_session(old_sid) == old_session
        assert [tuple(row) for row in con.execute("SELECT * FROM agent_messages WHERE session_id=?", (old_sid,))] == old_messages
        assert AgentSessionRepository(con).transcript(recovered["session_id"]) == []


def test_normal_home_preserves_legacy_id_history_and_kind(tmp_path):
    paths = WorkspacePaths(tmp_path)
    store = AssistantStore(paths)
    home = store.home("main")
    sid = home["session_id"]
    assert sid == "assistant_0d6e4079e36703ebd37c0072"
    with closing(connect(paths.db)) as con:
        repo = AgentSessionRepository(con)
        repo.record_message(message_id="keep", session_id=sid, role="user", content="Keep history")
        before = repo.get_session(sid)
        assert before["source"] == "assistant"
        assert before["title"] == "Nerya"
    for _ in range(3):
        assert AssistantStore(paths).home("main") == home
    assert SessionStore(tmp_path).load(sid).meta["conversation_kind"] == "assistant"
    with closing(connect(paths.db)) as con:
        assert AgentSessionRepository(con).get_session(sid) == before
        assert AgentSessionRepository(con).transcript(sid)[0]["content"] == "Keep history"


def test_home_keeps_existing_bind_kind(tmp_path):
    store = AssistantStore(WorkspacePaths(tmp_path))
    sid = "assistant_0d6e4079e36703ebd37c0072"
    store.bind(sid, "main", source="dashboard", title="Existing task", kind="task")
    assert store.home("main")["session_id"] == sid
    row = store.conversations("main")[0]
    assert (row["source"], row["title"], row["meta"]["conversation_kind"]) == ("dashboard", "Existing task", "task")
    assert SessionStore(tmp_path).load(sid).meta["conversation_kind"] == "task"


def test_home_does_not_skip_cross_assistant_binding_errors(tmp_path):
    store = AssistantStore(WorkspacePaths(tmp_path))
    other = store.save({"name": "Other"})
    sid = "assistant_0d6e4079e36703ebd37c0072"
    store.bind(sid, other["id"])
    with pytest.raises(AssistantError, match="^assistant_session_mismatch$"):
        store.home("main")
    assert store.for_session(sid)["id"] == other["id"]
    assert store.conversations("main") == []
