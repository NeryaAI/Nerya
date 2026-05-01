"""Tiny YAML flow DSL for built-in skills.

A ``flow:`` step list under an action spec looks like:

.. code-block:: yaml

    flow:
      - call: market_data.get_mark_price
        with: { market: "{{ market }}" }
        as: mid
      - call: trading.submit_trade_intent
        with:
          strategy_id: "{{ strategy_id }}"
          account_id: "{{ account_id }}"
          market: "{{ market }}"
          side: "{{ side }}"
          order_type: "market"
          size: "{{ size }}"
          size_unit: "{{ size_unit }}"
        as: ack
      - return:
          price: "{{ mid.price }}"
          ack: "{{ ack }}"

Supported keys per step:

* ``call: <skill_id>.<action>`` — invoke another skill action
* ``with: <dict>`` — arguments (template-expanded)
* ``as: <name>`` — bind result into the flow scope
* ``if: <expr>`` — optional guard (boolean-evaluable template)
* ``assert: <expr>`` — raise SkillActionError if falsy
* ``return: <value>`` — early-return a dict (or plain value)

Templates use ``{{ name }}`` / ``{{ name.path.0.x }}`` without Jinja to
keep the runtime dependency-free. The scope starts with all payload
arguments and receives every ``as:`` binding.
"""

from __future__ import annotations

import re
from typing import Any

from ..core.errors import SkillActionError


_TOKEN = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def _lookup(scope: dict[str, Any], path: str) -> Any:
    """Resolve a dotted path in ``scope``; supports ``foo.bar.0``."""
    parts = [p for p in re.split(r"\.|\[|\]", path) if p]
    cur: Any = scope
    for p in parts:
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(p)]
            except (ValueError, IndexError):
                return None
        else:
            cur = getattr(cur, p, None)
        if cur is None:
            return None
    return cur


def _expand(value: Any, scope: dict[str, Any]) -> Any:
    """Recursively expand ``{{ ... }}`` templates inside ``value``."""
    if isinstance(value, str):
        # whole-string single-expression: return typed
        m = _TOKEN.fullmatch(value.strip())
        if m:
            return _lookup(scope, m.group(1).strip())

        def _sub(match: re.Match[str]) -> str:
            v = _lookup(scope, match.group(1).strip())
            return "" if v is None else str(v)
        return _TOKEN.sub(_sub, value)
    if isinstance(value, list):
        return [_expand(v, scope) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, scope) for k, v in value.items()}
    return value


def _truthy(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("", "false", "0", "null", "none"):
            return False
        return True
    if isinstance(val, (list, tuple, dict)):
        return len(val) > 0
    return bool(val)


def run_flow(steps: list[dict[str, Any]], *, payload: dict[str, Any], runtime,
              caller: str, strategy_id: str | None = None,
              session_id: str | None = None) -> Any:
    """Execute a declarative flow. Returns the last explicit ``return:``
    value, or the full scope if none was provided.
    """
    scope: dict[str, Any] = dict(payload)
    for idx, raw_step in enumerate(steps):
        if not isinstance(raw_step, dict):
            raise SkillActionError(f"flow step {idx} is not a mapping: {raw_step!r}")
        step = _expand(raw_step, scope)

        if "if" in step and not _truthy(step["if"]):
            continue

        if "assert" in step and not _truthy(step["assert"]):
            msg = step.get("message") or f"flow assertion failed at step {idx}"
            raise SkillActionError(str(msg))

        if "return" in step:
            return step["return"]

        if "call" in step:
            target = str(step["call"])
            if "." not in target:
                raise SkillActionError(
                    f"flow step {idx}: call must be '<skill>.<action>', got {target!r}"
                )
            skill_id, action = target.split(".", 1)
            args = step.get("with") or {}
            if not isinstance(args, dict):
                raise SkillActionError(
                    f"flow step {idx}: with: must be a mapping, got {type(args).__name__}"
                )
            result = runtime.call(
                skill_id, action,
                payload=args,
                caller=caller or "flow",
                strategy_id=strategy_id,
                session_id=session_id,
            )
            bind = step.get("as")
            if bind:
                scope[str(bind)] = result
            continue

        if "set" in step:
            set_map = step["set"]
            if not isinstance(set_map, dict):
                raise SkillActionError(
                    f"flow step {idx}: set: must be a mapping"
                )
            scope.update(set_map)
            continue

        raise SkillActionError(
            f"flow step {idx}: unknown step keys {list(step.keys())}"
        )
    return scope


__all__ = ["run_flow"]
