"""Offline tests of reviewed skill assets, never imports a live candidate."""
import sys
from math import nextafter
from pathlib import Path
from types import ModuleType

import pytest
from types import SimpleNamespace

from nerya.strategies.configuration_contract import configuration_issues

pytestmark = pytest.mark.smoke
TEMPLATES = Path(__file__).parents[1] / "nerya/skills/builtin/strategy_author/templates"


@pytest.fixture
def reference(monkeypatch):
    helper = ModuleType("strategy_execution")
    helper_asset = TEMPLATES / "strategy_execution.py.j2"
    exec(compile(helper_asset.read_text(), str(helper_asset), "exec"), helper.__dict__)
    monkeypatch.setitem(sys.modules, "strategy_execution", helper)
    module = ModuleType("main")
    asset = TEMPLATES / "ema_cross.py.j2"
    exec(compile(asset.read_text(), str(asset), "exec"), module.__dict__)
    monkeypatch.setitem(sys.modules, "main", module)
    return module


def _original_ema(values, period):
    # Minimized exact math read from prp_fb320943b7e8; no candidate code is loaded.
    if not values or period <= 0:
        return [None] * len(values)
    k = 2.0 / (period + 1.0)
    out = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def test_exact_candidate_roundoff_is_reproduced_and_reference_fixes_it(reference):
    prices = [3000.0] * 190 + [3500.0]
    fast, slow = (_original_ema(prices, period) for period in (12, 48))
    assert (fast[11], slow[47]) == (3000.0, 3000.0)
    assert (fast[-2], slow[-2]) == (3000.0, 2999.9999999999995)
    assert not (fast[-2] <= slow[-2] and fast[-1] > slow[-1])
    # The old formula already invents a cross on a constant bar.
    assert fast[47] <= slow[47] and fast[48] > slow[48]
    stable_fast, stable_slow = (reference.ema(prices, period) for period in (12, 48))
    assert stable_fast[-2] == stable_slow[-2] == 3000.0
    assert stable_fast[-1] > stable_slow[-1]


@pytest.mark.parametrize("price", [0.1, 1.0, 3000.0, 3500.0, 60000.0])
@pytest.mark.parametrize("period", [1, 2, 7, 12, 26, 48, 200])
def test_constant_ema_stays_exactly_constant(reference, price, period):
    values = reference.ema([price] * 240, period)
    assert values[:period - 1] == [None] * (period - 1)
    assert values[period - 1:] == [price] * (241 - period)


def test_known_values_warmup_and_prefix_causality(reference):
    assert reference.ema([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4]
    assert reference.ema([], 3) == []
    assert reference.ema([1, 2], 3) == [None, None]
    rows = [{"close": value} for value in [100] * 5 + [110, 90, 80]]
    full = reference.ema_cross(rows, fast_period=2, slow_period=5)
    for end in range(1, len(rows) + 1):
        prefix = reference.ema_cross(rows[:end], fast_period=2, slow_period=5)
        assert prefix == {key: values[:end] for key, values in full.items()}
    assert full["buy"][5] is True and full["sell"][6] is True


def test_ema_manifest_requires_slow_period_plus_one_closed_bars():
    manifest = SimpleNamespace(extras={
        "params": {
            "indicator": {"fast_period": 12, "slow_period": 48},
            "lookback": 48,
        }
    })
    issues = configuration_issues(manifest)
    assert any(code == "insufficient_closed_history_lookback" for code, _ in issues)
    assert configuration_issues(SimpleNamespace(extras={
        "params": {
            "indicator": {"fast_period": 12, "slow_period": 48},
            "lookback": 49,
        }
    })) == []


def test_price_simulation_rejects_note_before_replay():
    manifest = SimpleNamespace(extras={'params': {'price_simulation': {
        'dataset_id': 'pool_prices', 'note': 'User-authorized pool-price simulation'}}})
    assert configuration_issues(manifest) == [('invalid_price_simulation',
        'params.price_simulation requires only dataset_id')]
    del manifest.extras['params']['price_simulation']['note']
    assert configuration_issues(manifest) == []


@pytest.mark.parametrize("period", [0, -1, 2.5, True])
def test_invalid_period_is_not_silently_changed(reference, period):
    with pytest.raises(ValueError, match="positive integer"):
        reference.ema([100.0] * 10, period)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_data_is_not_no_signal(reference, value):
    with pytest.raises(ValueError, match="finite"):
        reference.ema([100.0, value], 2)


def test_no_tolerance_creates_cross_from_already_same_side(reference, monkeypatch):
    # A one-ULP previous gap is still on that side, not an equality to forgive.
    for prior, current in ((nextafter(3000.0, float("inf")), 3001.0),
                           (nextafter(3000.0, 0.0), 2999.0), (3000.0, 3000.0)):
        series = iter(([prior, current], [3000.0, 3000.0]))
        monkeypatch.setattr(reference, "ema", lambda values, period: next(series))
        result = reference.ema_cross([{"close": 1}] * 2, fast_period=2, slow_period=5)
        assert result["buy"] == result["sell"] == [False, False]


@pytest.mark.parametrize("name", [
    "test_entry_and_duplicate", "test_exit_on_last_bar",
    "test_no_signal", "test_saved_periods_and_position_rules",
])
def test_packaged_contract_example(reference, name):
    asset = TEMPLATES / "ema_cross_contract.py.j2"
    scope = {}
    exec(compile(asset.read_text(), str(asset), "exec"), scope)
    scope[name]()
