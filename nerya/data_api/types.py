"""Typed building blocks for provider-specific read-only data APIs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


class DataApiError(Exception):
    """Structured exception raised by data API providers."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "provider_error",
        detail: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.detail = dict(detail or {})
        self.retryable = retryable


@dataclass(frozen=True)
class DataApiContext:
    """Runtime context passed to read-only data action handlers."""

    config_like: Any | None = None

    @property
    def config_data(self) -> dict[str, Any]:
        cfg = self.config_like
        if cfg is None:
            return {}
        data = getattr(cfg, "data", None)
        if isinstance(data, dict):
            return data
        if isinstance(cfg, dict):
            return cfg
        return {}

    @property
    def paths(self) -> Any | None:
        return getattr(self.config_like, "paths", None)

    @property
    def workspace(self) -> Path | None:
        paths = self.paths
        root = getattr(paths, "root", None)
        if root is None:
            return None
        try:
            return Path(root)
        except Exception:
            return None

    @property
    def vault_passphrase(self) -> str | None:
        val = (os.environ.get("NERYA_VAULT_PASSPHRASE") or "").strip()
        return val or None


DataApiHandler = Callable[[DataApiContext, dict[str, Any]], Any]


@dataclass(frozen=True)
class DataActionSpec:
    """One provider-specific, read-only callable exposed through data_api."""

    provider: str
    action: str
    title: str
    description: str
    input_schema: dict[str, Any]
    handler: DataApiHandler
    tags: tuple[str, ...] = ()
    output_kind: str = "json"
    docs_url: str = ""

    def preview(self) -> dict[str, Any]:
        out = {
            "provider": self.provider,
            "action": self.action,
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "output_kind": self.output_kind,
        }
        if self.docs_url:
            out["docs_url"] = self.docs_url
        return out


class DynamicDataProvider(Protocol):
    """Provider adapter for large catalogs discovered lazily."""

    provider: str

    def list_actions(
        self,
        *,
        query: str = "",
        tags: tuple[str, ...] = (),
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        ...

    def schema(self, action: str) -> dict[str, Any]:
        ...

    def call(
        self,
        action: str,
        args: dict[str, Any],
        *,
        context: DataApiContext,
    ) -> Any:
        ...
