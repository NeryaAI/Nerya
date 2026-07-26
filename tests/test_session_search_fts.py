from __future__ import annotations

import pytest

from nerya.agent.session_search import search
from nerya.agent.session_search_fts import FTSIndex, is_supported
from nerya.core import jsonl
from nerya.core.paths import WorkspacePaths


pytestmark = pytest.mark.smoke


@pytest.mark.skipif(not is_supported(), reason="SQLite FTS5 is unavailable")
def test_incremental_index_retries_incomplete_jsonl_tail(tmp_path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    journal = paths.journal("turn_steps")
    journal.parent.mkdir(parents=True)
    journal.write_text('{"kind":"message","text":"hel', encoding="utf-8")

    with FTSIndex(tmp_path / "session_index.db") as index:
        index.ensure_fresh(paths, journals=("turn_steps",))
        assert index.stats().rows == 0

        with journal.open("a", encoding="utf-8") as fh:
            fh.write('lo"}\n')
        index.ensure_fresh(paths, journals=("turn_steps",))

        rows = index.search("hello", journals=("turn_steps",))
        assert len(rows) == 1
        assert rows[0]["payload"]["text"] == "hello"


def test_streaming_search_reuses_jsonl_reader(tmp_path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    journal = paths.journal("messages")
    jsonl.append(
        journal,
        {"kind": "message", "session_id": "s1", "text": "needle"},
    )
    with journal.open("a", encoding="utf-8") as fh:
        fh.write("not json\n")

    rows = search(
        paths,
        "needle",
        journals=("messages",),
        session_id="s1",
        use_fts=False,
    )

    assert [row["payload"]["text"] for row in rows] == ["needle"]
