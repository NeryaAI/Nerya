"""Real freeform OS boundary tests; only synthetic, temporary packages."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import sys
import threading

import pytest

from nerya.core.errors import TradingError
from nerya.harness.cancellation import CancelToken, CancelledError
from nerya.skills.builtin.backtest.scripts import freeform_run as runner
from nerya.skills.builtin.backtest.scripts import freeform_sandbox as sandbox

pytestmark = pytest.mark.smoke
macos = pytest.mark.skipif(sys.platform != "darwin", reason="real Seatbelt acceptance")


def package(tmp_path, script, extra=None):
    root = tmp_path / "strategies" / "sandbox_test"
    files = {
        "strategy.yml": "id: sandbox_test\nmode: paper\nentrypoint: custom.py:replay\nschedule: {type: cron, cron: '0 * * * *'}\n",
        "custom.py": "def replay(ctx): return 42\n",
        "backtests/research_backtest.py": script,
        **(extra or {}),
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


HAPPY = "import json\nprint(json.dumps({'equity_curve': [100, 101], 'trades': [], 'limitations': ['synthetic fixture only']}))\n"


def run(tmp_path, **kwargs):
    return runner.run_freeform_backtest(
        strategy_id="sandbox_test", workspace=tmp_path, **kwargs
    )


def test_unsupported_fails_closed(tmp_path, monkeypatch):
    package(tmp_path, HAPPY)
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(TradingError, match="macOS"):
        run(tmp_path)


@macos
def test_real_boundary_and_prepared_input(tmp_path, monkeypatch):
    sentinel = tmp_path / "vault-sentinel"
    sentinel.write_text("private workspace sentinel")
    monkeypatch.setenv("FREEFORM_SECRET_SENTINEL", "never-in-child")
    script = r"""
import importlib
import json
import sys
from pathlib import Path
from os import environ
from nerya.strategies import research
root, out, workspace = research._roots()
assert out.is_relative_to(root)
assert 'FREEFORM_SECRET_SENTINEL' not in environ
assert 'PYTHONPATH' not in environ
assert research.entrypoint()(None) == 42
assert json.loads((root / 'history/input.json').read_text()) == [100, 102]
assert (root / 'backtests/prepared.csv').read_text() == 'ts,rate\n1,3.2\n'
def denied(action):
    try: action()
    except PermissionError: return
    raise AssertionError('OS boundary allowed forbidden operation')
denied(lambda: Path(WORKSPACE_SENTINEL_PATH).read_text())
denied(lambda: (root / 'custom.py').write_text('changed'))
denied(lambda: (root / 'history/input.json').write_text('changed'))
denied(lambda: (root / 'backtests/escape.txt').write_text('changed'))
socket = importlib.import_module('socket')
denied(lambda: socket.socket().connect(('127.0.0.1', 9)))
process = importlib.import_module('subprocess')
denied(lambda: process.run(['/usr/bin/true'], check=True))
denied(lambda: process.run([sys.executable, '-c', 'pass'], check=True))
system = importlib.import_module('os')
denied(lambda: system.fork())
(Path(environ['TMPDIR']) / 'allowed.txt').write_text('scratch')
research.write_json('result.json', {'equity_curve': [100, 102], 'trades': [], 'limitations': ['prepared synthetic input']})
""".replace("WORKSPACE_SENTINEL_PATH", repr(str(sentinel)))
    root = package(
        tmp_path,
        script,
        {
            "history/input.json": "[100, 102]",
            "backtests/prepared.csv": "ts,rate\n1,3.2\n",
        },
    )
    before = (root / "custom.py").read_bytes()
    result = run(tmp_path)
    assert result["equity_points"] == 2
    assert "prepared synthetic input" in result["limitations"]
    assert (root / "custom.py").read_bytes() == before
    assert sentinel.read_text() == "private workspace sentinel"
    assert "never-in-child" not in json.dumps(result)
    safety = result["isolation"]
    assert safety["replay_script"] == "backtests/research_backtest.py"
    assert safety["entrypoint"] == "custom.py:replay"
    assert (
        safety["snapshot_files"]["history/input.json"]
        == hashlib.sha256(b"[100, 102]").hexdigest()
    )
    from nerya.strategies.package import _hash_files

    assert safety["snapshot_sha256"] == _hash_files(
        root, tuple(safety["snapshot_files"])
    )
    assert safety["process_exited"] and safety["process_group_stopped"]


@macos
def test_structure_rejected_before_execution(tmp_path):
    marker = tmp_path / "parent-imported"
    package(
        tmp_path,
        HAPPY,
        {
            "custom.py": f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef replay(: pass\n"
        },
    )
    with pytest.raises(TradingError, match="structur"):
        run(tmp_path)
    assert not marker.exists()


@macos
def test_legacy_beside_script_write_denied(tmp_path):
    root = package(
        tmp_path,
        "from pathlib import Path\n(Path(__file__).parent / 'result.json').write_text('{}')\n"
        + HAPPY,
    )
    with pytest.raises(TradingError, match="PermissionError|Operation not permitted"):
        run(tmp_path)
    assert not (root / "backtests/result.json").exists()


@macos
@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancel_reap_process_and_remove_temporary_tree(
    tmp_path, monkeypatch, cancel
):
    package(
        tmp_path,
        "import time\nimport signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(.05)\n",
    )
    token = CancelToken()
    seen = []
    launch = sandbox.sandbox_exec

    def capture(*args, **kwargs):
        handle = launch(*args, **kwargs)
        seen.append((handle.process, Path(kwargs["root"])))
        return handle

    monkeypatch.setattr(sandbox, "sandbox_exec", capture)
    timer = threading.Timer(0.3, token.cancel) if cancel else None
    if timer:
        timer.start()
    try:
        with pytest.raises(
            CancelledError if cancel else TradingError, match="cancelled|timeout"
        ) as exc:
            run(tmp_path, timeout_seconds=2 if cancel else 0.3, cancel_token=token)
    finally:
        if timer:
            timer.join()
    assert len(seen) == 1
    process, temporary = seen[0]
    assert exc.value.result.process_exited
    assert exc.value.result.process_group_stopped
    assert process.proc.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(process.proc.pid, 0)
    assert not temporary.exists()


@macos
def test_symlink_artifact_not_followed_by_parent(tmp_path):
    sentinel = tmp_path / "private.txt"
    sentinel.write_text("must not be copied")
    script = f"from pathlib import Path\nfrom nerya.strategies import research\nroot, out, workspace = research._roots()\n(out / 'equity.csv').symlink_to({str(sentinel)!r})\n"
    root = package(tmp_path, script)
    with pytest.raises(TradingError, match="failed closed"):
        run(tmp_path)
    runs = list((root / "backtests").glob("freeform_*"))
    assert len(runs) == 1
    assert json.loads((runs[0] / "run.json").read_text())["status"] == "failed"
    assert not (runs[0] / "equity.csv").exists()


@macos
def test_fifo_artifact_rejected_without_hanging(tmp_path):
    package(
        tmp_path,
        "from os import mkfifo\nfrom nerya.strategies import research\nroot, out, workspace = research._roots()\nmkfifo(out / 'equity.csv')\n",
    )
    with pytest.raises(TradingError, match="regular|Operation not permitted"):
        run(tmp_path)


@macos
def test_snapshot_rejects_source_symlink(tmp_path):
    root = package(tmp_path, HAPPY)
    script = root / "backtests/research_backtest.py"
    script.unlink()
    outside = tmp_path / "outside.py"
    outside.write_text(HAPPY)
    script.symlink_to(outside)
    with pytest.raises(TradingError, match="failed closed"):
        run(tmp_path)


@macos
def test_parent_never_imports_candidate_or_runs_candidate_tests(tmp_path):
    root = package(
        tmp_path,
        HAPPY,
        {
            "custom.py": "raise RuntimeError('candidate imported in parent')\ndef replay(ctx): return 42\n",
            "tests/test_recursive.py": "raise RuntimeError('tests recursively executed')\n",
            "sitecustomize.py": "raise RuntimeError('sitecustomize executed')\n",
        },
    )
    from nerya.strategies.package import load_package_from_dir

    before = load_package_from_dir(root).content_hash
    result = run(tmp_path)
    assert result["ok"]
    assert "synthetic fixture only" in result["limitations"]
    assert before == load_package_from_dir(root).content_hash
    assert not list(root.rglob("__pycache__"))


@macos
def test_fetch_candles_explicitly_requires_prepared_input(tmp_path):
    package(
        tmp_path,
        "from nerya.strategies import research\nresearch.fetch_candles('BYBIT:BTCUSDT', interval='1h', start=1, end=2, count=1)\n",
    )
    with pytest.raises(TradingError, match="requires prepared package history"):
        run(tmp_path)


@macos
def test_environment_injection_and_json_credentials_are_not_returned(
    tmp_path, monkeypatch
):
    injection = tmp_path / "injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "raise RuntimeError('host sitecustomize loaded')"
    )
    monkeypatch.setenv("PYTHONPATH", str(injection))
    monkeypatch.setenv("PYTHONTZPATH", str(tmp_path))
    package(
        tmp_path,
        "from os import environ\nfrom nerya.strategies import research\nassert 'PYTHONTZPATH' not in environ\nresearch.write_json('result.json', {'equity_curve': [100, 101], 'trades': [], 'limitations': [{'password': 'synthetic-sensitive-value'}]})\n",
    )
    result = run(tmp_path)
    assert "synthetic-sensitive-value" not in json.dumps(result)
    assert "synthetic-sensitive-value" not in Path(result["result_path"]).read_text()


@macos
def test_legacy_environment_allowance_is_paths_only(tmp_path):
    package(tmp_path, "import os\nprint(os.environ['UNAPPROVED_KEY'])\n" + HAPPY)
    with pytest.raises(TradingError, match="structural validation failed: env_access"):
        run(tmp_path)


@macos
@pytest.mark.parametrize("kind", ["memory", "storage", "file", "cpu"])
def test_real_resource_bounds(tmp_path, monkeypatch, kind):
    scripts = {
        "memory": "import time\nblob = bytearray(64 * 1024 * 1024)\ntime.sleep(5)\n",
        "storage": "import time\nfrom nerya.strategies import research\nresearch.write_text('large.txt', 'x' * 8192)\ntime.sleep(5)\n",
        "file": "from nerya.strategies import research\nresearch.write_text('large.txt', 'x' * 8192)\n",
        "cpu": "while True: pass\n",
    }
    field, value = {
        "memory": ("_MEMORY_BYTES", 48 * 1024 * 1024),
        "storage": ("_SCRATCH_BYTES", 4096),
        "file": ("_FILE_BYTES", 4096),
        "cpu": ("_CPU_SECONDS", 1),
    }[kind]
    monkeypatch.setattr(sandbox.boundary, field, value)
    package(tmp_path, scripts[kind])
    seen = []
    launch = sandbox.sandbox_exec

    def capture(*args, **kwargs):
        handle = launch(*args, **kwargs)
        seen.append(handle.process)
        return handle

    monkeypatch.setattr(sandbox, "sandbox_exec", capture)
    with pytest.raises(TradingError, match="limit|File too large|script failed"):
        run(tmp_path, timeout_seconds=4)
    assert len(seen) == 1
    assert seen[0].wait().process_exited and seen[0].wait().process_group_stopped


@macos
def test_output_capture_is_bounded(tmp_path, monkeypatch):
    package(tmp_path, "print('x' * 300000)\n" + HAPPY)
    result = run(tmp_path)
    assert result["ok"]
    assert result["isolation"]["output_truncated"] is True


@macos
def test_changed_source_during_execution_is_not_accepted(tmp_path, monkeypatch):
    root = package(tmp_path, HAPPY)
    launch = sandbox.sandbox_exec

    def mutate(*args, **kwargs):
        (root / "custom.py").write_text("def replay(ctx): return 43\n")
        return launch(*args, **kwargs)

    monkeypatch.setattr(sandbox, "sandbox_exec", mutate)
    with pytest.raises(TradingError, match="source changed during replay"):
        run(tmp_path)
    runs = list((root / "backtests").glob("freeform_*"))
    assert len(runs) == 1
    assert json.loads((runs[0] / "run.json").read_text())["status"] == "failed"
    assert not (runs[0] / "metrics.json").exists()


@macos
def test_snapshot_size_limit_fails_before_launch(tmp_path, monkeypatch):
    package(tmp_path, HAPPY, {"history/too_big.json": "x" * 8192})
    monkeypatch.setattr(sandbox.boundary, "_FILE_BYTES", 4096)
    monkeypatch.setattr(
        sandbox,
        "sandbox_exec",
        lambda *a, **kw: pytest.fail("oversized input launched"),
    )
    with pytest.raises(TradingError, match="snapshot byte limit"):
        run(tmp_path)


@macos
def test_non_json_stdout_is_not_deserialized(tmp_path):
    package(tmp_path, "print(\"{'equity_curve': [100, 101], 'trades': []}\")\n")
    with pytest.raises(TradingError, match="must produce equity"):
        run(tmp_path)
