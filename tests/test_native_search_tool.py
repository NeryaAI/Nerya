from pathlib import Path

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
