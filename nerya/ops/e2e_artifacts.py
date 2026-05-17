"""End-to-end verification artifacts.

Saves request/response/log bundles for HTTP smoke runs, dashboard checks,
and agent task replays so a reviewer can validate work without trusting
terminal scrollback, using Nerya's workspace layout.

Layout::

    workspace/artifacts/e2e/<run_id>/
        meta.json              # run metadata
        00_request_<n>.json
        00_response_<n>.json
        00_screenshot_<n>.png  # optional
        00_dom_<n>.html        # optional
        log.txt
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


_SECRET_KEYS: tuple[str, ...] = (
    "password", "token", "api_key", "secret", "vault", "authorization",
)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            lk = str(k).lower()
            if any(s in lk for s in _SECRET_KEYS):
                out[k] = "[redacted]"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"sk-[A-Za-z0-9]{20,}", "[redacted]", value)
    return value


@dataclass
class RunMeta:
    run_id: str
    started_at: str
    label: str = ""
    base_url: str = ""
    env: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    ended_at: str = ""
    status: str = "running"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArtifactRun:
    workspace_root: Path
    run_id: str
    started_at: str
    base_url: str = ""
    label: str = ""
    env: dict[str, Any] = field(default_factory=dict)
    _step: int = 0

    @property
    def root(self) -> Path:
        return self.workspace_root / "artifacts" / "e2e" / self.run_id

    @property
    def meta_path(self) -> Path:
        return self.root / "meta.json"

    @property
    def log_path(self) -> Path:
        return self.root / "log.txt"

    def _ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _next(self) -> int:
        self._step += 1
        return self._step

    def log(self, line: str) -> None:
        self._ensure()
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{_now_iso()} {line.rstrip()}\n")

    def write_http(
        self,
        *,
        method: str,
        url: str,
        request_body: Optional[Any] = None,
        response_body: Optional[Any] = None,
        status_code: int = 0,
        elapsed_ms: int = 0,
        request_headers: Optional[dict[str, Any]] = None,
        response_headers: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        self._ensure()
        step = self._next()
        req_path = self.root / f"{step:02d}_request.json"
        res_path = self.root / f"{step:02d}_response.json"
        req_blob = _redact({
            "ts": _now_iso(),
            "method": method,
            "url": url,
            "headers": request_headers or {},
            "body": request_body,
        })
        res_blob = _redact({
            "ts": _now_iso(),
            "status_code": status_code,
            "elapsed_ms": elapsed_ms,
            "headers": response_headers or {},
            "body": response_body,
        })
        req_path.write_text(json.dumps(req_blob, ensure_ascii=False, indent=2), encoding="utf-8")
        res_path.write_text(json.dumps(res_blob, ensure_ascii=False, indent=2), encoding="utf-8")
        artifact = {
            "step": step,
            "kind": "http",
            "method": method,
            "url": url,
            "status_code": status_code,
            "request": str(req_path.relative_to(self.workspace_root)),
            "response": str(res_path.relative_to(self.workspace_root)),
        }
        self.log(f"HTTP {method} {url} -> {status_code} ({elapsed_ms}ms)")
        return artifact

    def write_screenshot(self, *, name: str, data: bytes) -> dict[str, Any]:
        self._ensure()
        step = self._next()
        path = self.root / f"{step:02d}_{name}.png"
        path.write_bytes(data)
        self.log(f"SCREENSHOT {name} ({len(data)} bytes)")
        return {
            "step": step,
            "kind": "screenshot",
            "path": str(path.relative_to(self.workspace_root)),
        }

    def write_dom(self, *, name: str, html: str) -> dict[str, Any]:
        self._ensure()
        step = self._next()
        path = self.root / f"{step:02d}_{name}.html"
        path.write_text(html, encoding="utf-8")
        self.log(f"DOM {name} ({len(html)} chars)")
        return {
            "step": step,
            "kind": "dom",
            "path": str(path.relative_to(self.workspace_root)),
        }

    def finalize(self, *, status: str = "ok") -> dict[str, Any]:
        meta = RunMeta(
            run_id=self.run_id,
            started_at=self.started_at,
            label=self.label,
            base_url=self.base_url,
            env=_redact(self.env),
            ended_at=_now_iso(),
            status=status,
        )
        artifacts = self._collect_artifacts()
        meta.artifacts = artifacts
        self.meta_path.write_text(
            json.dumps(meta.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return meta.as_dict()

    def _collect_artifacts(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not self.root.exists():
            return out
        for path in sorted(self.root.iterdir()):
            if path.name in ("meta.json", "log.txt"):
                continue
            out.append({
                "name": path.name,
                "path": str(path.relative_to(self.workspace_root)),
                "size": path.stat().st_size,
            })
        return out


def open_run(
    client,
    *,
    label: str = "",
    base_url: str = "",
    env: Optional[dict[str, Any]] = None,
) -> ArtifactRun:
    return ArtifactRun(
        workspace_root=client.config.paths.root,
        run_id=_run_id(),
        started_at=_now_iso(),
        base_url=base_url,
        label=label,
        env=dict(env or {}),
    )


def list_runs(client) -> list[dict[str, Any]]:
    root = client.config.paths.root / "artifacts" / "e2e"
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        meta_path = child / "meta.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        meta["run_id"] = meta.get("run_id") or child.name
        meta["path"] = str(child.relative_to(client.config.paths.root))
        out.append(meta)
    return out


def get_run(client, run_id: str) -> Optional[dict[str, Any]]:
    root = client.config.paths.root / "artifacts" / "e2e" / run_id
    meta_path = root / "meta.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None
