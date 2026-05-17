"""Live natural-language session for Nerya's agent teams.

Drives the **installed** Nerya HTTP service with a multi-turn,
operator-style session — no canned subagent fakes, no fixtures, no
hard-coded mock outputs. Every turn hits a real LLM through the
configured provider.

Designed to be invoked **after** ``pip install -e .`` (or any other
install) so the script talks to whichever ``nerya serve`` instance
the operator has running. By default it starts its own service in a
subprocess against a fresh workspace, with ``NERYA_DEV_MODE=1`` and a
real LLM provider auto-detected from environment variables.

Session shape (single ``session_id``)::

    1. strategy_line    -> draft a paper-only BTC strategy production line
    2. risk_tightening  -> tighten the risk numbers like an operator would
    3. trigger_gate     -> turn it into a trigger + promotion checklist
    4. demo_summary     -> produce a short Chinese stage-ready summary

The driver records every HTTP call, every reply, every team-run
artifact, and prints a final audit report so the operator can see
what the Agent actually said and whether it is sensible.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_HERE = Path(__file__).resolve().parent
_NERYA_ROOT = _HERE.parent
if str(_NERYA_ROOT) not in sys.path:
    sys.path.insert(0, str(_NERYA_ROOT))


# ---------------------------------------------------- live provider mapping

_LIVE_PROVIDER_PREFERENCE: list[tuple[str, str, str]] = [
    ("OPENAI_API_KEY",     "openai",     "gpt-4o-mini"),
    ("ANTHROPIC_API_KEY",  "anthropic",  "claude-haiku-4-5"),
    ("DEEPSEEK_API_KEY",   "deepseek",   "deepseek-chat"),
    ("OPENROUTER_API_KEY", "openrouter", "openrouter/auto"),
    ("XAI_API_KEY",        "xai",        "grok-2-latest"),
    ("MOONSHOT_API_KEY",   "moonshot",   "moonshot-v1-8k"),
    ("MISTRAL_API_KEY",    "mistral",    "mistral-large-latest"),
    ("TOGETHER_API_KEY",   "together",
     "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    ("GROQ_API_KEY",       "groq",       "llama-3.3-70b-versatile"),
    ("CEREBRAS_API_KEY",   "cerebras",   "llama-3.3-70b"),
    ("GEMINI_API_KEY",     "gemini",     "gemini-2.5-flash"),
]


def _detect_live_provider(
    *, override_provider: str | None, override_model: str | None,
) -> dict[str, Any] | None:
    if override_provider:
        provider = override_provider.lower().strip()
        env_name: str | None = None
        for env, prov, _model in _LIVE_PROVIDER_PREFERENCE:
            if prov == provider:
                env_name = env
                break
        if env_name is None:
            env_name = f"{provider.upper()}_API_KEY"
        if not os.environ.get(env_name):
            return None
        model = override_model or next(
            (m for _, p, m in _LIVE_PROVIDER_PREFERENCE if p == provider),
            "",
        )
        return {"provider": provider, "model": model, "env": env_name}

    for env, provider, model in _LIVE_PROVIDER_PREFERENCE:
        if os.environ.get(env):
            return {
                "provider": provider,
                "model": override_model or model,
                "env": env,
            }
    return None


def _adopt_user_config(
    user_cfg_path: Path, workspace: Path,
) -> dict[str, Any] | None:
    """Adopt the operator's ``~/.nerya/nerya.yml`` for the demo workspace.

    Behaviour:

    1. Read the operator's ``nerya.yml`` and copy it into
       ``<workspace>/nerya.yml`` *with* ``runtime.dev_mode: true`` and
       ``runtime.mock_mode: false`` enforced — the demo always wants a
       real LLM and dev recordings.
    2. Mirror sibling state directories that the runtime expects to be
       present (``vault/``, ``providers/``, ``accounts/``, ``state/``,
       ``skills/``, ``subagents/``, ``triggers/``, ``strategies/``,
       ``memory/``, ``messages/``, ``inbox/``, ``outbox/``). Each one is
       *referenced* from the user's home rather than copied so that
       secrets stay in their original location and the demo never
       writes to ``~/.nerya``.

    The demo does *not* mutate any files under the user's home; it only
    populates the dedicated workspace with a derived ``nerya.yml`` and
    a synthetic ``providers.yml`` symlink/copy when present. Returns a
    metadata dict (or ``None`` if the config was unreadable).
    """

    if not user_cfg_path.exists():
        return None
    try:
        raw = user_cfg_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        import yaml  # local import: not all paths use yaml
    except ImportError:
        yaml = None  # type: ignore[assignment]
    parsed: dict[str, Any] = {}
    if yaml is not None:
        try:
            parsed = yaml.safe_load(raw) or {}
        except Exception:
            parsed = {}

    # Force demo-friendly runtime flags. We never *publish* trades from
    # the demo, regardless of what the user's home has set.
    runtime = parsed.setdefault("runtime", {}) if parsed else {}
    runtime["live_trading_enabled"] = False
    runtime["mock_mode"] = False
    runtime["dev_mode"] = True

    workspace.mkdir(parents=True, exist_ok=True)
    cfg_path = workspace / "nerya.yml"
    if yaml is not None and parsed:
        cfg_path.write_text(
            yaml.safe_dump(parsed, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    else:
        # Fall back to copying the raw bytes verbatim; the runtime can
        # still read them, we just lose the dev_mode override.
        cfg_path.write_text(raw, encoding="utf-8")

    # Surface a few links so providers/vault/accounts resolve from the
    # workspace too. Symlink where the OS allows it, fall back to copy
    # so the demo never crashes on Windows perms.
    user_home = user_cfg_path.parent
    for sub in (
        "vault", "providers", "accounts", "skills", "subagents",
        "triggers", "strategies",
    ):
        src = user_home / sub
        dst = workspace / sub
        if not src.exists() or dst.exists():
            continue
        try:
            os.symlink(src, dst, target_is_directory=src.is_dir())
        except (OSError, NotImplementedError):
            try:
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            except OSError:
                pass

    info: dict[str, Any] = {
        "source": str(user_cfg_path),
        "cfg_path": str(cfg_path),
    }
    # Try to surface which provider/model the user configured so the
    # operator can see it in the demo banner.
    llm = (parsed or {}).get("llm") or {}
    default_tier = llm.get("default_tier") or "medium"
    tier = (llm.get("tiers") or {}).get(default_tier) or {}
    if tier:
        info["provider"] = tier.get("provider")
        info["model"] = tier.get("model")
        info["base_url"] = tier.get("base_url")
        info["default_tier"] = default_tier
    return info


def _write_live_nerya_yml(
    workspace: Path, *, provider: str, model: str, env_name: str,
) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    cfg_path = workspace / "nerya.yml"
    body = f"""# Auto-generated by scripts/run_nl_team_demo.py.
# All LLM tiers wired to a real provider via {env_name} so subagents
# make actual API calls. Dev mode is enabled so every HTTP/tool call
# is captured under workspace/dev_recordings/.
runtime:
  live_trading_enabled: false
  mock_mode: false
  dev_mode: true
llm:
  default_tier: medium
  tiers:
    light:
      provider: {provider}
      model: {model}
      provider_key_env: {env_name}
      max_tokens: 2048
      temperature: 0.1
      timeout_s: 60
      allowed_classes:
        - classification
        - structured_extraction
        - content_compression
    medium:
      provider: {provider}
      model: {model}
      provider_key_env: {env_name}
      max_tokens: 8192
      temperature: 0.2
      timeout_s: 180
      allowed_classes:
        - agent_loop
        - subagent_reasoning
        - strategy_review
    high:
      provider: {provider}
      model: {model}
      provider_key_env: {env_name}
      max_tokens: 16384
      temperature: 0.2
      timeout_s: 300
      allowed_classes:
        - proposal_generation
        - complex_reasoning
"""
    cfg_path.write_text(body, encoding="utf-8")
    return cfg_path


# ------------------------------------------------------------ HTTP helpers

def _post(host: str, port: int, path: str, body: dict[str, Any],
          *, timeout: float = 30.0) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = Request(
        f"http://{host}:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8") or "{}"
            return {"status": r.status, "body": json.loads(raw)}
    except HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace") if exc.fp else ""
        return {"status": exc.code,
                "body": {"error": exc.reason, "raw": body_text}}
    except URLError as exc:
        return {"status": 0, "body": {"error": str(exc)}}


def _get(host: str, port: int, path: str,
         *, timeout: float = 30.0) -> dict[str, Any]:
    try:
        with urlopen(f"http://{host}:{port}{path}", timeout=timeout) as r:
            raw = r.read().decode("utf-8") or "{}"
            return {"status": r.status, "body": json.loads(raw)}
    except HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace") if exc.fp else ""
        return {"status": exc.code,
                "body": {"error": exc.reason, "raw": body_text}}
    except URLError as exc:
        return {"status": 0, "body": {"error": str(exc)}}


def _wait_health(host: str, port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = _get(host, port, "/health", timeout=2.0)
        if last["status"] == 200:
            return True
        time.sleep(0.5)
    print(f"!! /health never returned 200 within {timeout}s "
          f"(last={last})", file=sys.stderr)
    return False


# ------------------------------------------------------------ rendering

def _hr(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _trim(text: str, n: int = 800) -> str:
    if len(text) <= n:
        return text
    return text[:n] + "...<trimmed>"


def _show_run_turn(label: str, body: dict[str, Any]) -> dict[str, Any]:
    plan = body.get("plan") or {}
    decision = body.get("decision") or {}
    # The HTTP envelope ships the assistant's reply under
    # ``reply_text`` (kernel-level field). ``decision.text`` is the
    # planner-level draft and may not always equal the rendered
    # reply, so prefer ``reply_text`` and fall back to the decision
    # text only when the kernel did not surface one.
    reply = body.get("reply_text") or ""
    if not reply and isinstance(decision, dict):
        reply = decision.get("text") or ""
    steps = body.get("steps") or []
    team_steps = [s for s in steps if s.get("step_kind") == "team_run"]
    team_run_id = ""
    template = ""
    if team_steps:
        detail = team_steps[0].get("detail") or {}
        team_run_id = detail.get("run_id") or ""
        template = detail.get("template") or ""

    print(f"-- planner    : kind={plan.get('kind')!r} "
          f"tier={plan.get('tier')!r}")
    if template:
        print(f"-- team       : template={template!r} "
              f"run_id={team_run_id!r}")
    if decision:
        intent = decision.get('intent') or decision.get('action') \
            or decision.get('kind')
        print(f"-- decision   : {intent!r}")
    if reply:
        print(f"-- reply (head) : {_trim(reply.splitlines()[0], 240)}")
    return {
        "label": label,
        "team_run_id": team_run_id,
        "template": template,
        "reply_head": reply.splitlines()[0] if reply else "",
        "reply_full": reply,
        "decision": decision,
    }


def _dump_team_run(workspace: Path, run_id: str, host: str, port: int,
                   out_dir: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"run_id": run_id}
    if not run_id:
        return out
    fetched = _post(host, port, "/teams/get", {"run_id": run_id})
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "teams_get.json").write_text(
        json.dumps(fetched, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    body = (fetched.get("body") or {}) if isinstance(
        fetched.get("body"), dict) else {}
    run = body.get("run") or {}
    tasks = body.get("tasks") or []
    completed = [t for t in tasks if t.get("status") == "completed"]
    print(f"-- /teams/get : template={run.get('template_id')!r} "
          f"completed={len(completed)}/{len(tasks)} "
          f"status={run.get('status')!r}")
    teams_dir = workspace / "teams" / run_id
    final_report = teams_dir / "synthesis" / "final_report.md"
    if final_report.exists():
        text = final_report.read_text(encoding="utf-8")
        (out_dir / "final_report.md").write_text(text, encoding="utf-8")
        out["final_report_path"] = str(final_report)
        out["final_report_excerpt"] = "\n".join(text.splitlines()[:24])
    out["status"] = run.get("status")
    out["template"] = run.get("template_id")
    out["completed_tasks"] = len(completed)
    out["total_tasks"] = len(tasks)
    return out


# --------------------------------------------------------- prompt deck

def _trigger(kind: str, text: str, **payload: Any) -> dict[str, Any]:
    return {
        "id": f"demo-{kind.replace('.', '-')}-{uuid.uuid4().hex[:8]}",
        "event_id": f"demo-{kind.replace('.', '-')}-{uuid.uuid4().hex[:8]}",
        "kind": kind,
        "source": "user_command",
        "payload": dict(payload, text=text, user="ricky"),
    }


def _session_prompts(session_id: str) -> list[dict[str, Any]]:
    """Short Chinese operator session, all under one session_id.

    These prompts intentionally sound like a real user rather than a
    benchmark prompt. They avoid claiming live market/on-chain/news data
    is available, so the demo validates the production-line workflow
    instead of depending on optional data skills.
    """

    return [
        {
            "label": "1_strategy_line",
            "trigger": _trigger(
                "user.chat",
                (
                    "帮我做一个 BTC 1 小时趋势突破策略的演示版本。"
                    "先只做 draft/paper，不要实盘。不要只说你要创建，"
                    "也不要调用文件创建工具，直接在聊天里一次性给出："
                    "策略规则、入场/退出、风控限制、触发器思路、纸交易"
                    "验收门槛。没有实时行情就明确写 demo assumptions，"
                    "不要假装已经读取实时数据。"
                ),
                asset="BTC", timeframe="1h", style="trend_breakout",
            ),
        },
        {
            "label": "2_risk_tightening",
            "trigger": _trigger(
                "user.chat",
                (
                    "这个策略风险哪里最大？请像风控负责人一样把参数改紧。"
                    "必须给具体数字：单笔风险 1.5%、最多 2 个并发仓位、"
                    "日亏损 4% 停止、周回撤 12% 暂停、CPI/FOMC/NFP 前后"
                    " 2 小时不交易。顺便说明为什么这样更适合 demo。"
                ),
            ),
        },
        {
            "label": "3_trigger_gate",
            "trigger": _trigger(
                "user.chat",
                (
                    "把上面的策略整理成 Nerya 可以注册的触发器和纸交易"
                    "验收清单。不要只说准备创建，直接输出正文。"
                    "触发器按 1 小时 K 线收盘运行，必须写清楚风控 guard、"
                    "禁止实盘、draft -> paper -> canary -> live 每一步"
                    "需要哪些证据。"
                ),
            ),
        },
        {
            "label": "4_demo_summary",
            "trigger": _trigger(
                "user.chat",
                (
                    "最后帮我生成一段中文演示总结，控制在 90 秒内。"
                    "重点讲清楚：这不是普通交易机器人，而是策略生产线；"
                    "它有风控、日志、验证、审批和后续进化。不要写成营销长文。"
                ),
            ),
        },
    ]


# ------------------------------------------------------------ service mgmt

def _service_cmd_candidates(
    workspace: Path, host: str, port: int,
) -> list[list[str]]:
    """Return service launch commands in safest-first order."""

    module_cmd = [
        sys.executable, "-m", "nerya.cli.app",
        "serve", "--workspace", str(workspace),
        "--host", host, "--port", str(port),
        "--no-dashboard",
    ]
    candidates = [module_cmd]
    nerya_cmd = shutil.which("nerya")
    if nerya_cmd:
        candidates.append([
            nerya_cmd, "serve", "--workspace", str(workspace),
            "--host", host, "--port", str(port), "--no-dashboard",
        ])
    return candidates


def _start_service(workspace: Path, host: str, port: int,
                   *, log_path: Path) -> subprocess.Popen:
    """Start ``nerya serve`` as a subprocess."""

    env = os.environ.copy()
    env["NERYA_DEV_MODE"] = "1"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[demo] service log     : {log_path}")

    # On Windows we MUST start the service in its own process group, so
    # later we can deliver ``CTRL_BREAK_EVENT`` only to that group and
    # not to the parent shell. Without ``CREATE_NEW_PROCESS_GROUP``,
    # ``send_signal(CTRL_BREAK_EVENT)`` propagates up the entire console
    # tree and kills the demo runner with -1073741510 (STATUS_CONTROL_C_EXIT).
    popen_kwargs: dict[str, Any] = {
        "stderr": subprocess.STDOUT,
        "env": env,
        "cwd": str(_NERYA_ROOT),
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        )
    else:
        popen_kwargs["start_new_session"] = True
    last_exc: Exception | None = None
    for idx, cmd in enumerate(_service_cmd_candidates(workspace, host, port)):
        log_mode = "w" if idx == 0 else "a"
        log_fh = log_path.open(log_mode, encoding="utf-8")
        print(f"[demo] starting service: {' '.join(cmd)}")
        try:
            return subprocess.Popen(cmd, stdout=log_fh, **popen_kwargs)
        except OSError as exc:
            last_exc = exc
            log_fh.write(
                f"\n[demo] failed to start {' '.join(cmd)}: {exc}\n",
            )
            log_fh.close()
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("no service command candidates")


def _stop_service(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            if os.name == "nt":
                # The service was started with CREATE_NEW_PROCESS_GROUP,
                # so CTRL_BREAK only hits its group, not ours.
                try:
                    proc.send_signal(  # type: ignore[attr-defined]
                        signal.CTRL_BREAK_EVENT,
                    )
                except (OSError, AttributeError, ValueError):
                    proc.terminate()
            else:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
    except Exception:
        pass


# ------------------------------------------------------------ validation

_REQUIRED_KEYWORDS_BY_LABEL: dict[str, list[list[str]]] = {
    # Each inner list is an OR group. The Chinese terms are primary;
    # English fallbacks keep the check robust if the model mirrors a
    # built-in English tool/schema term.
    "1_strategy_line": [
        ["btc", "bitcoin"],
        ["1 小时", "1小时", "1h", "1 hour"],
        ["趋势", "突破", "breakout", "trend"],
        ["入场", "进场", "entry"],
        ["退出", "止损", "exit", "stop"],
        ["风控", "风险", "risk"],
        ["触发器", "trigger"],
        ["纸交易", "paper"],
        ["demo assumptions", "假设", "没有实时"],
    ],
    "2_risk_tightening": [
        ["1.5%", "单笔"],
        ["2 个", "2个", "并发"],
        ["4%", "日亏损"],
        ["12%", "周回撤"],
        ["cpi", "fomc", "nfp"],
        ["2 小时", "2小时"],
    ],
    "3_trigger_gate": [
        ["触发器", "trigger"],
        ["1 小时", "1小时", "1h"],
        ["guard", "风控", "门禁"],
        ["禁止实盘", "不要实盘", "paper"],
        ["draft", "paper", "canary", "live"],
        ["证据", "验收", "validation"],
    ],
    "4_demo_summary": [
        ["策略生产线", "生产线"],
        ["交易机器人", "机器人"],
        ["风控", "风险"],
        ["日志", "审计"],
        ["验证", "验收"],
        ["审批", "approval"],
        ["进化", "复盘"],
    ],
}

_DEGRADED_REPLY_MARKERS = [
    "hit skill access walls",
    "didn't produce detailed outputs",
    "我来为你创建",
    "我现在为你创建",
    "将为你创建",
    "准备创建",
    "没有实际产出",
    "没能产出",
    "无法访问",
    "skillnotfounderror",
    "approval_pending",
    "max_tokens",
]


def _validate_reply(label: str, reply: str) -> dict[str, Any]:
    findings: dict[str, Any] = {
        "label": label,
        "reply_len": len(reply),
        "non_empty": bool(reply.strip()),
        "checks": [],
        "missing_groups": [],
        "degraded_markers": [],
    }
    if not reply.strip():
        findings["ok"] = False
        return findings

    groups = _REQUIRED_KEYWORDS_BY_LABEL.get(label, [])
    text_lower = reply.lower()
    degraded = [m for m in _DEGRADED_REPLY_MARKERS if m in text_lower]
    findings["degraded_markers"] = degraded
    for group in groups:
        hits = [t for t in group if t.lower() in text_lower]
        findings["checks"].append({"group": group, "hits": hits})
        if not hits:
            findings["missing_groups"].append(group)
    findings["ok"] = (
        findings["non_empty"]
        and not degraded
        and not findings["missing_groups"]
        and len(reply) >= 300
    )
    return findings


# --------------------------------------------------------------- driver

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", type=Path, default=None,
        help="Workspace dir to use; defaults to "
             "Nerya/.nl_e2e_runs/<ts>/",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Bind host for the spawned nerya serve (default 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=18317,
        help="Bind port for the spawned nerya serve (default 18317)",
    )
    parser.add_argument(
        "--keep-running", action="store_true",
        help="Leave the service running after the demo (Ctrl-C to stop).",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Where to dump JSON snapshots / report copies "
             "(default <workspace>/_demo_outputs/)",
    )
    parser.add_argument(
        "--provider", default=None,
        help="Override the auto-detected live provider "
             "(openai|anthropic|deepseek|openrouter|xai|moonshot|...).",
    )
    parser.add_argument(
        "--model", default=None,
        help="Override the live model id for the chosen provider.",
    )
    parser.add_argument(
        "--call-timeout", type=float, default=240.0,
        help="HTTP read timeout for /agent/run_turn calls (default "
             "240s — multi-team turns can be slow).",
    )
    parser.add_argument(
        "--external-service", action="store_true",
        help="Don't start nerya serve; assume one is already listening "
             "on --host:--port (e.g. operator booted it manually).",
    )
    parser.add_argument(
        "--user-config", action="store_true", default=None,
        help="Use the operator's installed ~/.nerya/nerya.yml (and vault, "
             "providers, accounts) as the LLM/runtime config instead of "
             "auto-generating a fresh nerya.yml from $*_API_KEY env vars. "
             "Default: enabled if ~/.nerya/nerya.yml exists.",
    )
    parser.add_argument(
        "--no-user-config", action="store_true",
        help="Force the legacy behaviour: ignore ~/.nerya/nerya.yml and "
             "auto-generate a config from $*_API_KEY env vars.",
    )
    parser.add_argument(
        "--user-config-path",
        type=Path,
        default=Path.home() / ".nerya" / "nerya.yml",
        help="Path to the operator's nerya.yml (default ~/.nerya/nerya.yml).",
    )
    args = parser.parse_args()
    if args.no_user_config:
        args.user_config = False
    elif args.user_config is None:
        args.user_config = args.user_config_path.exists()

    if args.workspace is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.workspace = _NERYA_ROOT / ".nl_e2e_runs" / ts
    args.workspace = args.workspace.resolve()
    args.workspace.mkdir(parents=True, exist_ok=True)

    if args.out is None:
        args.out = args.workspace / "_demo_outputs"
    args.out.mkdir(parents=True, exist_ok=True)

    print(f"[demo] workspace = {args.workspace}")
    print(f"[demo] outputs   = {args.out}")

    # ------------- live LLM wiring (always, no fakes) ------------
    info: dict[str, Any] | None = None
    if args.user_config:
        info = _adopt_user_config(
            args.user_config_path,
            args.workspace,
        )
        if info is None:
            print(
                f"!! --user-config requested but {args.user_config_path} "
                f"could not be loaded; falling back to env-based config",
                file=sys.stderr,
            )
    if info is None:
        info = _detect_live_provider(
            override_provider=args.provider, override_model=args.model)
        if info is None:
            print(
                "!! no LLM API key found in env. Set OPENAI_API_KEY / "
                "ANTHROPIC_API_KEY / DEEPSEEK_API_KEY / OPENROUTER_API_KEY "
                "/ XAI_API_KEY / MOONSHOT_API_KEY / MISTRAL_API_KEY / "
                "TOGETHER_API_KEY / GROQ_API_KEY / CEREBRAS_API_KEY / "
                "GEMINI_API_KEY before running, or run with "
                "--user-config-path pointing at a configured nerya.yml.",
                file=sys.stderr,
            )
            return 2
        cfg_path = _write_live_nerya_yml(
            args.workspace,
            provider=info["provider"],
            model=info["model"],
            env_name=info["env"],
        )
        print(f"[demo] llm wired: provider={info['provider']!r} "
              f"model={info['model']!r} env={info['env']}")
        print(f"[demo] wrote    : {cfg_path}")
    else:
        print(
            f"[demo] llm wired: source={info['source']!r} "
            f"provider={info.get('provider')!r} "
            f"model={info.get('model')!r}"
        )
        print(f"[demo] wrote    : {info['cfg_path']}")

    # ------------- service ------------
    proc = None
    if not args.external_service:
        proc = _start_service(
            args.workspace, args.host, args.port,
            log_path=args.workspace / "logs" / "nerya_serve.log",
        )

    try:
        if not _wait_health(args.host, args.port, timeout=60.0):
            print("!! service did not come up — aborting", file=sys.stderr)
            if proc is not None:
                # Surface the last log lines so the operator can see why.
                log_path = args.workspace / "logs" / "nerya_serve.log"
                if log_path.exists():
                    tail = log_path.read_text(encoding="utf-8")
                    print("---- nerya_serve.log tail ----")
                    for line in tail.splitlines()[-40:]:
                        print(line)
            return 3

        session_id = f"demo-{uuid.uuid4().hex[:12]}"
        print(f"[demo] session_id = {session_id}")

        all_findings: list[dict[str, Any]] = []
        all_team_dumps: list[dict[str, Any]] = []
        all_replies: list[dict[str, Any]] = []
        prompts = _session_prompts(session_id)

        for prompt in prompts:
            label = prompt["label"]
            _hr(f"turn {label}  trigger.kind={prompt['trigger']['kind']}")
            t0 = time.time()
            res = _post(
                args.host, args.port, "/agent/run_turn",
                {"trigger": prompt["trigger"], "session_id": session_id},
                timeout=args.call_timeout,
            )
            elapsed = time.time() - t0
            print(f"-- /agent/run_turn -> {res['status']} "
                  f"({elapsed:.1f}s)")
            if res["status"] != 200:
                print(_trim(json.dumps(res, ensure_ascii=False), 600))
                all_findings.append({
                    "label": label, "ok": False,
                    "http_status": res["status"],
                    "error": (res.get("body") or {}).get("error"),
                })
                continue

            run_dir = args.out / label
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "run_turn.json").write_text(
                json.dumps(res, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            head = _show_run_turn(label, res["body"])
            all_replies.append({
                "label": label,
                "reply": head["reply_full"],
                "decision": head["decision"],
            })

            if head["team_run_id"]:
                dump = _dump_team_run(
                    args.workspace, head["team_run_id"],
                    args.host, args.port, run_dir / "team",
                )
                all_team_dumps.append({"label": label, **dump})

            findings = _validate_reply(label, head["reply_full"])
            findings["http_status"] = res["status"]
            findings["elapsed_s"] = round(elapsed, 1)
            findings["team_run_id"] = head["team_run_id"]
            all_findings.append(findings)

        # ------------- session-level audit ------------
        _hr("session audit")
        for f in all_findings:
            mark = "ok " if f.get("ok") else "FAIL"
            extras = []
            if f.get("missing_groups"):
                extras.append(f"missing={f['missing_groups']!r}")
            if not f.get("non_empty", True):
                extras.append("empty_reply")
            if f.get("http_status") and f["http_status"] != 200:
                extras.append(f"http={f['http_status']}")
            print(f"  [{mark}] {f['label']:<22} "
                  f"reply_len={f.get('reply_len', 0):>5} "
                  f"elapsed={f.get('elapsed_s', '?')}s "
                  f"{' '.join(extras)}")

        (args.out / "session_audit.json").write_text(
            json.dumps({
                "session_id": session_id,
                "provider": info,
                "workspace": str(args.workspace),
                "findings": all_findings,
                "team_dumps": all_team_dumps,
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        (args.out / "session_replies.md").write_text(
            "\n\n".join(
                f"# Turn `{r['label']}`\n\n{r['reply'] or '_(no reply)_'}"
                for r in all_replies
            ),
            encoding="utf-8",
        )

        # Tail of agent journal (real journal entries, not fixtures).
        journal = args.workspace / "journal" / "agent.jsonl"
        if journal.exists():
            text = journal.read_text(encoding="utf-8")
            tail = text.splitlines()[-30:]
            print()
            print("agent journal tail (last 30):")
            for line in tail:
                print(f"   {line[:240]}")
            (args.out / "agent_journal_tail.txt").write_text(
                "\n".join(tail), encoding="utf-8",
            )

        all_ok = all(f.get("ok") for f in all_findings)
        print()
        print(f"[demo] all_ok = {all_ok}")

        if args.keep_running:
            _hr(f"service still running at "
                f"http://{args.host}:{args.port}")
            print("    press Ctrl-C to stop")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\n[demo] shutting down by user request")

        return 0 if all_ok else 1
    finally:
        if proc is not None:
            _stop_service(proc)


if __name__ == "__main__":
    sys.exit(main())
