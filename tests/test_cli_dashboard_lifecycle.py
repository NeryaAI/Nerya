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
        lambda workspace, profile=None: SimpleNamespace(config=object()),
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


def test_spawn_dashboard_uses_direct_next_cli(monkeypatch):
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
    assert captured["argv"][-1] == "dev"
    assert "npm" not in captured["argv"]
    assert captured["kwargs"]["cwd"].endswith("/dashboard")
