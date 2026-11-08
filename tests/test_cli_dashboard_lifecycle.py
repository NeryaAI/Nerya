from types import SimpleNamespace
import shutil
import subprocess

import nerya.api.local_server as local_server
import nerya.cli.commands.core as core


def test_cmd_run_terminates_dashboard_process_group(monkeypatch):
    events = []

    class FakeProcess:
        pid = 123

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout):
            events.append(("wait", timeout))

        def kill(self):
            events.append("kill")

    process = FakeProcess()
    monkeypatch.setattr(core.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        core.os,
        "killpg",
        lambda pgid, signum: events.append(("killpg", pgid)),
    )
    monkeypatch.setattr(
        core,
        "_client",
        lambda workspace, profile=None: SimpleNamespace(config={}),
    )
    monkeypatch.setattr(
        core,
        "_spawn_dashboard",
        lambda port, *, api_host, api_port: process,
    )
    monkeypatch.setattr(local_server, "serve", lambda config, host, port: None)

    args = SimpleNamespace(
        workspace=None,
        profile=None,
        no_dashboard=False,
        dashboard_port=18380,
        host="127.0.0.1",
        port=18317,
    )

    core.cmd_run(args)

    assert ("killpg", 123) in events


def test_spawn_dashboard_uses_direct_local_server(monkeypatch, tmp_path):
    monkeypatch.delenv("NERYA_UI_DIST_DIR", raising=False)
    monkeypatch.delenv("NERYA_UI_TSCONFIG", raising=False)
    monkeypatch.delenv("NODE_ENV", raising=False)
    fake_repo = tmp_path / "repo"
    dashboard = fake_repo / "dashboard"
    next_cli = fake_repo / "node_modules" / "next" / "dist" / "bin" / "next"
    dashboard.mkdir(parents=True)
    next_cli.parent.mkdir(parents=True)
    (dashboard / "package.json").write_text("{}", encoding="utf-8")
    next_cli.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        core,
        "__file__",
        str(fake_repo / "nerya" / "cli" / "commands" / "core.py"),
    )
    captured = {}
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/opt/homebrew/bin/node" if name == "node" else None,
    )

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    process = core._spawn_dashboard(18380, api_host="127.0.0.1", api_port=18317)

    assert process is not None
    assert captured["argv"] == ["/opt/homebrew/bin/node", str(dashboard / "scripts/local-server.cjs")]
    assert captured["kwargs"]["env"]["PORT"] == "18380"
    assert captured["kwargs"]["env"]["NERYA_API"] == "http://127.0.0.1:18317"
    assert captured["kwargs"]["env"]["NERYA_UI_DIST_DIR"].startswith(".next-live-18380-")
    assert "npm" not in captured["argv"]
    assert captured["kwargs"]["cwd"].endswith("/dashboard")


def test_each_dashboard_launch_owns_its_build_and_preserves_tracked_tsconfig(tmp_path):
    original = b'{\n // JSONC and relative aliases must survive\n "extends":"./base.json", "include":["app/**/*.ts"]\n}\n'
    source = tmp_path / "tsconfig.json"
    source.write_bytes(original)
    parent = {"PATH": "operator-path", "UNRELATED": "preserved"}
    first = core._dashboard_build_env(tmp_path, 18380, parent)
    second = core._dashboard_build_env(tmp_path, 18380, parent)
    assert first["NERYA_UI_DIST_DIR"] != second["NERYA_UI_DIST_DIR"]
    assert first["NERYA_UI_TSCONFIG"] != second["NERYA_UI_TSCONFIG"]
    assert first["PATH"] == second["PATH"] == "operator-path"
    assert parent == {"PATH": "operator-path", "UNRELATED": "preserved"}
    for child in (first, second):
        assert (tmp_path / child["NERYA_UI_TSCONFIG"]).read_bytes() == original
        assert child["NERYA_UI_DIST_DIR"].startswith(".next-live-18380-")
    assert source.read_bytes() == original


def test_dashboard_honors_explicit_build_and_tsconfig_ownership(tmp_path):
    (tmp_path / "tsconfig.json").write_text("{}")
    explicit = {"NERYA_UI_DIST_DIR": ".next-owned", "NERYA_UI_TSCONFIG": "tsconfig.custom.json"}
    assert core._dashboard_build_env(tmp_path, 18380, explicit) == explicit
    assert list(tmp_path.glob(".next-config-*.json")) == []
    custom_config_only = core._dashboard_build_env(tmp_path, 18380, {"NERYA_UI_TSCONFIG": "tsconfig.custom.json"})
    assert custom_config_only["NERYA_UI_TSCONFIG"] == "tsconfig.custom.json"
    assert custom_config_only["NERYA_UI_DIST_DIR"].startswith(".next-live-18380-")
    assert list(tmp_path.glob(".next-config-*.json")) == []


def test_production_dashboard_does_not_change_prebuilt_paths(tmp_path):
    env = {"NODE_ENV": "production", "PORT": "18380"}
    assert core._dashboard_build_env(tmp_path, 18380, env) == env
    assert list(tmp_path.iterdir()) == []


def test_javascript_dashboard_does_not_create_a_spurious_tsconfig(tmp_path):
    env = core._dashboard_build_env(tmp_path, 18380, {})
    assert env["NERYA_UI_DIST_DIR"].startswith(".next-live-18380-")
    assert "NERYA_UI_TSCONFIG" not in env
    assert list(tmp_path.iterdir()) == []
