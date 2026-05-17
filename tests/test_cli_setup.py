"""Coverage for the unified ``nerya setup`` onboarding command.

The wizard has two front-ends (Rich TUI + dashboard web page) but only
the Python side is in scope for these tests. We check:

* ``setup`` is wired into the top-level parser.
* The mutually-exclusive mode flags work and ``--print-url`` is the
  testable form (it writes to stdout and exits 0 without opening a
  browser).
* ``cmd_init`` prints the post-init hint that points at ``nerya setup``,
  and ``--no-hint`` suppresses it.
* The TUI runs end-to-end in non-interactive (``accept_defaults``)
  mode against a freshly initialised workspace without crashing.
"""

from __future__ import annotations

import io
import sys
from copy import deepcopy

import pytest

from nerya.cli import setup_tui
from nerya.cli.app import build_parser
from nerya.cli.commands import core as core_cmd
from nerya.cli.commands import quickstart as quickstart_cmd
from nerya.cli.commands import setup as setup_cmd
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths


pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# parser wiring
# ---------------------------------------------------------------------------


def test_setup_subcommand_is_registered():
    parser = build_parser()
    args = parser.parse_args(["setup"])
    assert args.func is setup_cmd.cmd_setup


def test_setup_mode_flags_are_mutually_exclusive():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["setup", "--tui", "--web"])
    with pytest.raises(SystemExit):
        parser.parse_args(["setup", "--web", "--yes"])


def test_setup_flags_parse_individually():
    parser = build_parser()
    for flag in ("--tui", "--web", "--print-url", "--yes"):
        args = parser.parse_args(["setup", flag])
        assert args.func is setup_cmd.cmd_setup, flag
    args = parser.parse_args([
        "setup",
        "--web",
        "--dashboard-port",
        "4123",
        "--no-open",
        "--url",
        "http://example.test/setup",
    ])
    assert args.dashboard_port == 4123
    assert args.no_open is True
    assert args.url == "http://example.test/setup"


# ---------------------------------------------------------------------------
# --print-url is the headless / CI form
# ---------------------------------------------------------------------------


def test_print_url_uses_explicit_url(capsys):
    parser = build_parser()
    args = parser.parse_args([
        "setup", "--print-url", "--url", "http://test.local/setup",
    ])
    rc = setup_cmd.cmd_setup(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "http://test.local/setup" in out


def test_print_url_defaults_to_localhost_18380(capsys, monkeypatch):
    # Make sure stray env vars don't leak into the test.
    monkeypatch.delenv("NERYA_SETUP_URL", raising=False)
    monkeypatch.delenv("NERYA_DASHBOARD_URL", raising=False)

    parser = build_parser()
    args = parser.parse_args(["setup", "--print-url"])
    rc = setup_cmd.cmd_setup(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "http://localhost:18380/setup" in out


def test_print_url_respects_env_dashboard_url(capsys, monkeypatch):
    monkeypatch.delenv("NERYA_SETUP_URL", raising=False)
    monkeypatch.setenv("NERYA_DASHBOARD_URL", "http://dash.local:9999")

    parser = build_parser()
    args = parser.parse_args(["setup", "--print-url"])
    rc = setup_cmd.cmd_setup(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "http://dash.local:9999/setup" in out


def test_print_url_respects_env_setup_url(capsys, monkeypatch):
    monkeypatch.setenv("NERYA_SETUP_URL", "http://wizard.example/onboarding")
    monkeypatch.delenv("NERYA_DASHBOARD_URL", raising=False)

    parser = build_parser()
    args = parser.parse_args(["setup", "--print-url"])
    rc = setup_cmd.cmd_setup(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "http://wizard.example/onboarding" in out


# ---------------------------------------------------------------------------
# cmd_init nudges the user toward `nerya setup`
# ---------------------------------------------------------------------------


def test_init_prints_setup_hint(tmp_path, capsys):
    """``nerya init`` should print a discoverable next-step hint."""

    parser = build_parser()
    args = parser.parse_args(["init", "--workspace", str(tmp_path)])
    rc = core_cmd.cmd_init(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert "nerya setup" in out


def test_init_no_hint_silences_the_nudge(tmp_path, capsys):
    parser = build_parser()
    args = parser.parse_args([
        "init", "--workspace", str(tmp_path), "--no-hint",
    ])
    rc = core_cmd.cmd_init(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert "nerya setup" not in out


# ---------------------------------------------------------------------------
# TUI smoke — non-interactive accept-defaults run
# ---------------------------------------------------------------------------


class _StubClient:
    """Minimal stand-in for ``InternalClient`` used by the TUI smoke test.

    The TUI only touches ``client.config`` (for ``Config.get`` and
    ``config.paths.config``), so we mount a real :class:`Config` against
    a tmp workspace and skip the heavier ``InternalClient.boot``.
    """

    def __init__(self, config: Config) -> None:
        self.config = config


def test_setup_tui_runs_in_non_interactive_mode(tmp_path):
    """The TUI must produce a summary table without crashing.

    ``accept_defaults=True`` forces every confirm to "skip", every
    prompt to its default value. No persistence is exercised in this
    path because all step bodies have a "no, skip" branch under
    non-interactive mode.
    """
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    client = _StubClient(cfg)

    # Drive output to an in-memory buffer so pytest doesn't echo
    # 100+ lines of Rich panels into the captured stdout.
    from rich.console import Console
    sink = io.StringIO()
    console = Console(file=sink, force_terminal=False, width=120)

    result = setup_tui.run(client, console=console, accept_defaults=True)  # type: ignore[arg-type]

    rendered = sink.getvalue()

    # Every step name should appear in the summary table.
    expected_step_names = [
        "Password", "LLM model", "Gateway", "Memory",
        "Browser", "Account", "Search",
    ]
    seen = {row.name for row in result.steps}
    assert seen == set(expected_step_names), f"missing steps: {set(expected_step_names) - seen}"

    # Output should contain the banner + summary header.
    assert "Nerya setup" in rendered
    assert "Setup summary" in rendered

    # Non-interactive runs never raise from missing input. The wizard
    # should resolve every step to a non-"error" status — defaults are
    # safe by construction.
    statuses = {row.status for row in result.steps}
    assert "error" not in statuses


# ---------------------------------------------------------------------------
# Interactive end-to-end paths — Telegram + Search engines
# ---------------------------------------------------------------------------


def _drive_interactive_tui(
    monkeypatch,
    cfg: Config,
    *,
    yes_answers: dict[str, bool] | None = None,
    ask_answers: dict[str, str] | None = None,
) -> setup_tui.WizardResult:
    """Run the TUI with `_ask` / `_ask_yes` patched to return scripted
    answers. Match by substring against the prompt text. Unmatched
    prompts return the supplied default.
    """
    yes_answers = yes_answers or {}
    ask_answers = ask_answers or {}

    def fake_yes(ctx, prompt: str, *, default: bool) -> bool:
        for needle, answer in yes_answers.items():
            if needle in prompt:
                return answer
        return default

    def fake_ask(ctx, prompt: str, *, default=None, choices=None, password=False):
        for needle, answer in ask_answers.items():
            if needle in prompt:
                return answer
        return default if default is not None else ""

    # Force the wizard to treat the run as interactive.
    monkeypatch.setattr(setup_tui, "_ask_yes", fake_yes)
    monkeypatch.setattr(setup_tui, "_ask", fake_ask)

    from rich.console import Console
    sink = io.StringIO()
    console = Console(file=sink, force_terminal=False, width=120)

    client = _StubClient(cfg)
    # accept_defaults=False would normally enable interactive mode only
    # via TTY detection. Patching the helpers makes the TTY check moot.
    return setup_tui.run(client, console=console, accept_defaults=False)  # type: ignore[arg-type]


def test_setup_tui_telegram_path_persists_channel(tmp_path, monkeypatch):
    """Selecting Telegram + entering bot_token + chat_id must write to
    the channels doc via the existing gateway upsert helper."""
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    # Pre-create the workspace root so yaml_io can persist.
    tmp_path.mkdir(parents=True, exist_ok=True)

    # Capture whatever the TUI hands to gateway_config_upsert.
    captured: dict[str, object] = {}

    def fake_upsert(client, payload):
        captured["payload"] = payload
        return {"ok": True, "channel": {"name": "telegram"}}

    from nerya.api import routes_gateway as gw_mod
    monkeypatch.setattr(gw_mod, "gateway_config_upsert", fake_upsert)

    result = _drive_interactive_tui(
        monkeypatch, cfg,
        yes_answers={
            "Set an admin password": False,
            "Configure a gateway": True,
            "Configure the memory backend": False,
            "Configure a search engine": False,
            "Enable the headless browser": False,
            "Stay on paper trading": True,
        },
        ask_answers={
            "Platform": "telegram",
            "Telegram bot token": "TEST_BOT_TOKEN_123",
            "Chat ID": "987654321",
            # Skip LLM step so it doesn't try to actually write config.
            "Provider for the": "",
        },
    )

    # The gateway step landed in `ok` and the payload carries the
    # Telegram credentials we typed.
    gateway_rows = [r for r in result.steps if r.name == "Gateway"]
    assert gateway_rows, result.steps
    assert gateway_rows[0].status == "ok", gateway_rows[0]
    payload = captured.get("payload") or {}
    assert isinstance(payload, dict)
    assert payload.get("channel") == "telegram"
    assert payload.get("platform") == "telegram"
    assert payload.get("enabled") is True
    assert payload.get("polling") is True
    assert (payload.get("secrets") or {}).get("bot_token") == "TEST_BOT_TOKEN_123"
    assert payload.get("chat_id") == "987654321"


def test_setup_tui_search_duckduckgo_path(tmp_path, monkeypatch):
    """DuckDuckGo is keyless — selecting it should hit the search config
    handler with `engines=["duckduckgo"]`."""
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    tmp_path.mkdir(parents=True, exist_ok=True)

    captured: dict[str, object] = {}

    from nerya.api import routes_search

    def patched_routes():
        original = routes_search_routes_original()

        def wrapped_config(client, payload):
            captured["payload"] = payload
            # Build a fake "ready" status row so the wizard marks the
            # step `ok` without us having to also stub _build_status_payload.
            return {
                "ok": True,
                "engines": [
                    {"name": "duckduckgo", "ready": True},
                ],
            }

        out = []
        for method, path, _handler in original:
            if method == "POST" and path == "/search/engines/config":
                out.append((method, path, wrapped_config))
            else:
                out.append((method, path, _handler))
        return out

    routes_search_routes_original = routes_search.routes
    monkeypatch.setattr(routes_search, "routes", patched_routes)

    result = _drive_interactive_tui(
        monkeypatch, cfg,
        yes_answers={
            "Set an admin password": False,
            "Configure a gateway": False,
            "Configure the memory backend": False,
            "Configure a search engine": True,
            "Enable the headless browser": False,
            "Stay on paper trading": True,
        },
        ask_answers={
            "Engine": "duckduckgo",
        },
    )

    search_rows = [r for r in result.steps if r.name == "Search"]
    assert search_rows, result.steps
    assert search_rows[0].status == "ok", search_rows[0]
    payload = captured.get("payload") or {}
    assert isinstance(payload, dict)
    assert payload.get("engines") == ["duckduckgo"]
    # No `keys` block for a keyless engine.
    assert "keys" not in payload


def test_setup_tui_search_keyed_engine_path(tmp_path, monkeypatch):
    """Selecting Brave should round-trip the API key through the search
    config endpoint via the wizard's `keys: { brave: "..." }` payload."""
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    tmp_path.mkdir(parents=True, exist_ok=True)

    captured: dict[str, object] = {}

    from nerya.api import routes_search

    def patched_routes():
        original = routes_search_routes_original()

        def wrapped_config(client, payload):
            captured["payload"] = payload
            return {
                "ok": True,
                "engines": [
                    {"name": "brave", "ready": True},
                ],
            }

        out = []
        for method, path, _handler in original:
            if method == "POST" and path == "/search/engines/config":
                out.append((method, path, wrapped_config))
            else:
                out.append((method, path, _handler))
        return out

    routes_search_routes_original = routes_search.routes
    monkeypatch.setattr(routes_search, "routes", patched_routes)

    result = _drive_interactive_tui(
        monkeypatch, cfg,
        yes_answers={
            "Set an admin password": False,
            "Configure a gateway": False,
            "Configure the memory backend": False,
            "Configure a search engine": True,
            "Enable the headless browser": False,
            "Stay on paper trading": True,
        },
        ask_answers={
            "Engine": "brave",
            "API key(s) for brave": "test-key-1,test-key-2",
        },
    )

    search_rows = [r for r in result.steps if r.name == "Search"]
    assert search_rows, result.steps
    assert search_rows[0].status == "ok", search_rows[0]
    payload = captured.get("payload") or {}
    assert isinstance(payload, dict)
    assert payload.get("engines") == ["brave"]
    assert (payload.get("keys") or {}).get("brave") == "test-key-1,test-key-2"


# ---------------------------------------------------------------------------
# Phase 5 — `--quick` mode
# ---------------------------------------------------------------------------


def test_quick_flag_parses():
    parser = build_parser()
    args = parser.parse_args(["setup", "--quick"])
    assert args.quick is True


def test_quick_flag_appends_query_string_to_web_url(capsys, monkeypatch):
    """`nerya setup --web --quick --print-url` should append `mode=quick`
    to the URL so the web wizard renders the single-step view."""
    monkeypatch.delenv("NERYA_SETUP_URL", raising=False)
    monkeypatch.delenv("NERYA_DASHBOARD_URL", raising=False)

    parser = build_parser()
    args = parser.parse_args(["setup", "--print-url", "--quick"])
    rc = setup_cmd.cmd_setup(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "mode=quick" in out


def test_setup_tui_quick_runs_only_the_llm_step(tmp_path):
    """`run(quick=True, accept_defaults=True)` must run the LLM step
    end-to-end (here non-interactively, so it keeps the existing mock
    tier from DEFAULT_CONFIG and records "ok") and mark the other six
    domains as auto-defaults. No prompt should ever raise."""
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    tmp_path.mkdir(parents=True, exist_ok=True)

    client = _StubClient(cfg)
    from rich.console import Console
    sink = io.StringIO()
    console = Console(file=sink, force_terminal=False, width=120)

    result = setup_tui.run(client, console=console, accept_defaults=True, quick=True)  # type: ignore[arg-type]

    # All 7 step rows should be present in the summary.
    names = {row.name for row in result.steps}
    assert names == {
        "LLM model", "Password", "Gateway",
        "Memory", "Browser", "Account", "Search",
    }, names

    # Quick mode never errors.
    statuses = {row.status for row in result.steps}
    assert "error" not in statuses

    # Memory and Account auto-default to "ok"; Search/Gateway/Browser/
    # Password to "skipped" because their defaults mean "do nothing".
    by_name = {row.name: row for row in result.steps}
    assert by_name["Memory"].status == "ok"
    assert by_name["Account"].status == "ok"
    assert by_name["Password"].status == "skipped"
    assert by_name["Gateway"].status == "skipped"
    assert by_name["Browser"].status == "skipped"
    assert by_name["Search"].status == "skipped"

    # Banner reflects quick mode.
    output = sink.getvalue()
    assert "quick setup" in output.lower()


# ---------------------------------------------------------------------------
# Phase 6 — `nerya quickstart`
# ---------------------------------------------------------------------------


def test_quickstart_subcommand_is_registered():
    parser = build_parser()
    args = parser.parse_args(["quickstart"])
    assert args.func is quickstart_cmd.cmd_quickstart


def test_quickstart_flag_defaults():
    parser = build_parser()
    args = parser.parse_args(["quickstart"])
    assert args.mode == "tui"
    assert args.api_port == 18317
    assert args.dashboard_port == 18380
    assert args.no_service is False
    assert args.no_open is False


def test_quickstart_runs_quick_setup_in_tui_mode(tmp_path, monkeypatch, capsys):
    """`nerya quickstart --mode tui --no-open` against a brand-new
    workspace should: init the workspace, skip spawning the service,
    forward to ``cmd_setup`` with ``quick=True`` and ``tui=True``,
    and exit 0."""
    invocations: list[dict] = []

    def fake_cmd_setup(args):
        invocations.append({
            "tui": getattr(args, "tui", False),
            "web": getattr(args, "web", False),
            "quick": getattr(args, "quick", False),
            "workspace": getattr(args, "workspace", None),
        })
        return 0

    # ``cmd_quickstart`` does ``from . import setup as setup_cmd`` and
    # then calls ``setup_cmd.cmd_setup(shim)`` — patching the attribute
    # on the actual module object intercepts that call.
    from nerya.cli.commands import setup as real_setup_mod
    monkeypatch.setattr(real_setup_mod, "cmd_setup", fake_cmd_setup)

    parser = build_parser()
    args = parser.parse_args([
        "quickstart",
        "--mode", "tui",
        "--no-service",
        "--no-open",
        "--workspace", str(tmp_path),
    ])
    rc = quickstart_cmd.cmd_quickstart(args)
    assert rc == 0

    # Setup was called exactly once with the quick + tui flags.
    assert len(invocations) == 1, invocations
    assert invocations[0]["tui"] is True
    assert invocations[0]["web"] is False
    assert invocations[0]["quick"] is True
    assert invocations[0]["workspace"] == str(tmp_path)

    # The workspace yaml should now exist.
    assert (tmp_path / "nerya.yml").exists()

    # Stdout should mention the four-step ceremony.
    out = capsys.readouterr().out
    assert "step 1/4" in out
    assert "step 3/4" in out


# ---------------------------------------------------------------------------
# Phase 7 — smart `--web` auto-spawn
# ---------------------------------------------------------------------------


def test_setup_web_no_auto_serve_flag_parses():
    parser = build_parser()
    args = parser.parse_args(["setup", "--web", "--no-auto-serve"])
    assert args.no_auto_serve is True
    args = parser.parse_args(["setup", "--web"])
    assert args.no_auto_serve is False


def test_setup_web_skips_auto_spawn_when_port_open(monkeypatch, capsys):
    """When the dashboard port is already open, --web must NOT spawn a
    second service — just print the URL and try to open the browser."""
    monkeypatch.delenv("NERYA_SETUP_URL", raising=False)
    monkeypatch.delenv("NERYA_DASHBOARD_URL", raising=False)

    monkeypatch.setattr(setup_cmd, "_port_is_open",
                        lambda host, port, timeout=0.5: True)
    spawn_calls = []
    monkeypatch.setattr(setup_cmd, "_spawn_service_if_needed",
                        lambda api, dash: spawn_calls.append((api, dash)))
    monkeypatch.setattr(setup_cmd.webbrowser, "open", lambda url: True)

    parser = build_parser()
    args = parser.parse_args(["setup", "--web"])
    rc = setup_cmd.cmd_setup(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert spawn_calls == []
    assert "http://localhost:18380/setup" in out


def test_setup_web_auto_spawns_when_port_closed(monkeypatch, capsys):
    """When the dashboard port is closed, --web must spawn the service
    and wait for the port — no --start-server flag required."""
    monkeypatch.delenv("NERYA_SETUP_URL", raising=False)
    monkeypatch.delenv("NERYA_DASHBOARD_URL", raising=False)

    monkeypatch.setattr(setup_cmd, "_port_is_open",
                        lambda host, port, timeout=0.5: False)
    spawn_calls = []
    monkeypatch.setattr(setup_cmd, "_spawn_service_if_needed",
                        lambda api, dash: spawn_calls.append((api, dash)))
    wait_calls = []
    monkeypatch.setattr(
        setup_cmd,
        "_wait_for_port",
        lambda host, port, timeout_s=30.0, label="service":
        (wait_calls.append((host, port)) or True),
    )
    monkeypatch.setattr(setup_cmd.webbrowser, "open", lambda url: True)

    parser = build_parser()
    args = parser.parse_args(["setup", "--web"])
    rc = setup_cmd.cmd_setup(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert spawn_calls == [(18317, 18380)]
    assert wait_calls == [("127.0.0.1", 18380)]
    assert "auto-spawning" in out.lower() or "spawning" in out.lower()


def test_setup_web_no_auto_serve_disables_auto_spawn(monkeypatch, capsys):
    monkeypatch.delenv("NERYA_SETUP_URL", raising=False)
    monkeypatch.delenv("NERYA_DASHBOARD_URL", raising=False)

    monkeypatch.setattr(setup_cmd, "_port_is_open",
                        lambda host, port, timeout=0.5: False)
    spawn_calls = []
    monkeypatch.setattr(setup_cmd, "_spawn_service_if_needed",
                        lambda api, dash: spawn_calls.append((api, dash)))
    monkeypatch.setattr(setup_cmd.webbrowser, "open", lambda url: True)

    parser = build_parser()
    args = parser.parse_args(["setup", "--web", "--no-auto-serve"])
    rc = setup_cmd.cmd_setup(args)
    assert rc == 0
    assert spawn_calls == []  # the flag disabled the auto-spawn
