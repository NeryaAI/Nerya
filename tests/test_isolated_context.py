from types import SimpleNamespace
import pytest

from nerya.skills.builtin.backtest.scripts.isolated_context import (
    dispatch_context,
    json_value,
)
from nerya.skills.builtin.backtest.scripts.mock_ctx import MockState
from nerya.strategies.context import StrategyPosition


def context():
    return SimpleNamespace(
        state=MockState(),
        config=SimpleNamespace(markets=("VENUE:A/B",), accounts=("paper",)),
        market=SimpleNamespace(candles=lambda *a, **kw: [{"ts": 1, "close": 2}]),
    )


def test_accounting_state_cannot_be_overwritten_by_candidate():
    ctx = context()
    ctx.state.set("position:VENUE:A/B", {"qty": 1})
    for method, args, kwargs in [
        ("state.set", ["position:VENUE:A/B", None], {}),
        ("state.delete", ["__portfolio_nav__"], {}),
        ("state.update", [], {"__portfolio_nav__": {}}),
        (
            "state.compare_and_set",
            ["position:VENUE:A/B"],
            {"expect": {"qty": 1}, "new_value": None},
        ),
    ]:
        with pytest.raises(ValueError, match="accounting state"):
            dispatch_context(ctx, method, args, kwargs, decision=None)
    assert ctx.state.get("position:VENUE:A/B") == {"qty": 1}
    dispatch_context(
        ctx, "state.set", ["signal:current", {"last_ts": 1}], {}, decision=None
    )
    assert dispatch_context(
        ctx, "state.get", ["signal:current"], {}, decision=None
    ) == {"last_ts": 1}


def test_no_arbitrary_attributes_or_foreign_markets():
    ctx = context()
    for method in [
        "__dict__",
        "state.__class__",
        "financial.execute",
        "trading.__getattribute__",
    ]:
        with pytest.raises(ValueError, match="not allowed"):
            dispatch_context(ctx, method, [], {}, decision=None)
    with pytest.raises(ValueError, match="outside the candidate"):
        dispatch_context(ctx, "market.candles", ["OTHER:A/B"], {}, decision=None)
    assert dispatch_context(
        ctx, "market.candles", ["VENUE:A/B"], {}, decision=None
    ) == [{"ts": 1, "close": 2}]


def test_decision_missing_propagates_and_does_not_become_stub():
    class MissingDecision(Exception):
        pass

    def missing(*args):
        raise MissingDecision()

    with pytest.raises(MissingDecision):
        dispatch_context(
            context(), "subagents.run", ["analyst"], {"payload": {}}, decision=missing
        )
    assert json_value(StrategyPosition(market="VENUE:A/B", size=2))["size"] == 2
