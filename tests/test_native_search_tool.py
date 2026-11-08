from pathlib import Path
import pytest
from nerya.tools.native import search

from nerya.tools.native.search import glob_handler, grep_handler
from nerya.tools.types import ToolCall, ToolErrorKind


def _glob(pattern: str, *, root: Path, path: str | None = None):
    args = {"pattern": pattern}
    if path is not None:
        args["path"] = path
    return glob_handler(ToolCall(name="glob", arguments=args), root=root)


def test_glob_accepts_absolute_pattern_inside_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    src = root / "strategies" / "demo"
    src.mkdir(parents=True)
    target = src / "main.py"
    target.write_text("print('ok')\n", encoding="utf-8")

    result = _glob(str(root / "strategies" / "**" / "*.py"), root=root)

    assert not result.is_error
    assert result.metadata["count"] == 1
    assert "strategies/demo/main.py" in result.text()


def test_glob_rejects_absolute_pattern_outside_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "main.py").write_text("print('nope')\n", encoding="utf-8")

    result = _glob(str(outside / "*.py"), root=root)

    assert result.is_error
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION_DENIED


def test_grep_honors_max_results(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "matches.txt").write_text("hit one\nhit two\nhit three\n", encoding="utf-8")

    result = grep_handler(
        ToolCall(
            name="grep",
            arguments={"pattern": "hit", "max_results": 2},
        ),
        root=root,
    )

    assert not result.is_error
    assert result.metadata["count"] == 2
    assert result.content[1].data["truncated"] is True

def test_python_fallback_skips_binary_database_without_whole_file_read(tmp_path, monkeypatch):
    (tmp_path / 'nerya.db').write_bytes(b'SQLite format 3\0' + b'private needle' * 100)
    (tmp_path / 'notes.md').write_text('public needle\n')
    monkeypatch.setattr(search, '_have_rg', lambda: False)
    monkeypatch.setattr(Path, 'read_text', lambda *a, **k: pytest.fail('whole-file read'))
    result = grep_handler(ToolCall(name='grep', arguments={'pattern':'needle'}), root=tmp_path)
    assert not result.is_error
    assert result.content[1].data['matches'] == [{'path':'notes.md','line':1,'match':'public needle'}]

def test_python_fallback_enforces_search_deadline(tmp_path, monkeypatch):
    (tmp_path / 'notes.md').write_text('needle\n')
    monkeypatch.setattr(search, '_have_rg', lambda: False)
    clock = iter([0, 0, 16, 16, 16])
    monkeypatch.setattr(search.time, 'monotonic', lambda: next(clock, 16))
    result = grep_handler(ToolCall(name='grep', arguments={'pattern':'needle'}), root=tmp_path)
    assert result.is_error and result.error.kind == ToolErrorKind.TIMEOUT

def test_python_fallback_deadline_inside_file_is_not_swallowed(tmp_path, monkeypatch):
    target = tmp_path / 'notes.md'
    target.write_text('needle\n')
    monkeypatch.setattr(search, '_have_rg', lambda: False)
    clock = iter([0, 0, 0, 16])
    monkeypatch.setattr(search.time, 'monotonic', lambda: next(clock, 16))
    result = grep_handler(ToolCall(name='grep', arguments={'pattern':'needle','path':'notes.md'}), root=tmp_path)
    assert result.is_error and result.error.kind == ToolErrorKind.TIMEOUT

def test_python_fallback_preserves_lines_unicode_and_case(tmp_path, monkeypatch):
    (tmp_path / 'notes.md').write_text('first\n行情 NEEDLE\nthird\n', encoding='utf-8')
    monkeypatch.setattr(search, '_have_rg', lambda: False)
    result = grep_handler(ToolCall(name='grep', arguments={'pattern':'needle','case_insensitive':True}), root=tmp_path)
    assert result.content[1].data['matches'] == [{'path':'notes.md','line':2,'match':'行情 NEEDLE'}]

def test_python_fallback_bounds_long_lines_and_reports_truncation(tmp_path, monkeypatch):
    (tmp_path / 'generated.txt').write_text('x' * 100000 + 'needle')
    monkeypatch.setattr(search, '_have_rg', lambda: False)
    result = grep_handler(ToolCall(name='grep', arguments={'pattern':'needle'}), root=tmp_path)
    assert not result.is_error
    assert result.content[1].data['truncated'] is True
    assert result.content[1].data['matches'] == []
