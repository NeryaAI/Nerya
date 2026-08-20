from __future__ import annotations

from nerya.llm.adapters._base import _post_with_retry
from nerya.llm.attempt_budget import AttemptBudget, attempt_budget_scope


class _StatusTransport:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)
        self.calls = 0

    def post_json(self, _url: str, **_kwargs):  # noqa: ANN201
        index = min(self.calls, len(self.statuses) - 1)
        status = self.statuses[index]
        self.calls += 1
        return status, {"status": status}


def test_turn_budget_caps_nested_provider_wire_retries() -> None:
    transport = _StatusTransport([503, 503, 200])
    budget = AttemptBudget(limit=1)

    with attempt_budget_scope(budget):
        status, body, _headers = _post_with_retry(
            transport,
            url="https://provider.test/v1/messages",
            headers={},
            body={"model": "test"},
            timeout=1.0,
            provider_name="test-provider",
            api_key="sk-test",
            max_attempts=5,
            base_delay=0.0,
            deadline=None,
        )

    assert transport.calls == 2
    assert status == 503
    assert body == {"status": 503}
    assert budget.used == 1
    assert budget.remaining == 0
    assert budget.by_reason == {"transport_retry": 1}


def test_tightening_restored_budget_preserves_actual_attempt_history() -> None:
    budget = AttemptBudget(limit=8)
    assert budget.claim("transport_retry")
    assert budget.claim("transient_retry")
    assert budget.claim("context_overflow_recovery")

    budget.constrain(2)

    assert budget.limit == 2
    assert budget.used == 3
    assert budget.remaining == 0
    assert budget.claim("transient_retry") is False
    assert budget.denied == 1
