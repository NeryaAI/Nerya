"""Bounded durable streaming projection for a single conversation command.

The live bus still delivers each delta. Only the SQLite replay log combines
adjacent append deltas from the same stream, preserving their concatenated text
and event order. Non-delta events (especially tool and approval receipts) are
always stored immediately. This avoids a SQLite transaction per model token.
"""
from __future__ import annotations

import copy
import threading
import time
from typing import Any


class CommandEventBuffer:
    FLUSH_SECONDS = 0.25
    MAX_DELTA_CHARS = 8192
    MAX_EVENTS = 64

    def __init__(self, store: Any, command_id: str):
        self.store, self.command_id = store, command_id
        self._lock = threading.Lock()
        self._pending: list[dict] = []
        self._last_flush = time.monotonic()
        self._closed = False

    @staticmethod
    def _append_info(event: dict) -> tuple | None:
        stream_id = event.get("stream_id")
        if not stream_id or event.get("mode") != "append":
            return None
        if event.get("kind") == "message.delta" and not event.get("completed"):
            if not isinstance(event.get("text"), str):
                return None
            return ("text", stream_id, event.get("session_id"), event.get("turn_id"),
                    event.get("stream_mode"))
        if event.get("kind") == "turn.step":
            step = event.get("step")
            if not isinstance(step, dict) or step.get("kind") != "thinking":
                return None
            detail = step.get("detail")
            if not isinstance(detail, dict) or set(detail) - {"text", "summary"}:
                return None
            if not isinstance(detail.get("text"), str) or detail.get("summary"):
                return None
            return ("thinking", stream_id, event.get("session_id"), event.get("turn_id"),
                    event.get("stream_mode"))
        return None

    @staticmethod
    def _text(event: dict, info: tuple) -> str:
        return (event["text"] if info[0] == "text"
                else event["step"]["detail"]["text"])

    def _flush_locked(self) -> None:
        if not self._pending:
            return
        # Retain pending data if persistence fails; the runtime fails closed.
        self.store.record_events(self.command_id, self._pending)
        self._pending.clear()
        self._last_flush = time.monotonic()

    def append(self, event: dict) -> None:
        with self._lock:
            if self._closed:
                return
            info = self._append_info(event)
            now = time.monotonic()
            merged = False
            if info and self._pending:
                prior = self._pending[-1]
                if self._append_info(prior) == info:
                    addition = self._text(event, info)
                    if len(self._text(prior, info)) + len(addition) <= self.MAX_DELTA_CHARS:
                        if info[0] == "text":
                            prior["text"] += addition
                        else:
                            prior["step"]["detail"]["text"] += addition
                        merged = True
            if not merged:
                # A copied record is required: bus subscribers share event dicts.
                self._pending.append(copy.deepcopy(event) if info else event)
            if (not info or len(self._pending) >= self.MAX_EVENTS
                    or now - self._last_flush >= self.FLUSH_SECONDS
                    or (info and len(self._text(self._pending[-1], info)) >= self.MAX_DELTA_CHARS)):
                self._flush_locked()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._flush_locked()
            self._closed = True
