"""Native adapter must preserve the worker's existing cancellation boundary."""
import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.harness.cancellation import CancelToken, CancelledError, raise_if_cancelled
from nerya.tools.native.strategy_runtime import strategy_backtest_handler
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke


def test_native_freeform_forwards_cancel_token(tmp_path, monkeypatch):
    from nerya.skills.builtin.backtest.scripts import freeform_run
    token = CancelToken()
    token.cancel()
    seen = []

    def cancelled(**kwargs):
        seen.append(kwargs.get('cancel_token'))
        raise_if_cancelled(kwargs.get('cancel_token'))
        pytest.fail('native adapter lost cancellation')

    monkeypatch.setattr(freeform_run, 'run_freeform_backtest', cancelled)
    with pytest.raises(CancelledError):
        strategy_backtest_handler(ToolCall(name='strategy_backtest',
            arguments={'strategy_id': 'fixture', 'engine': 'freeform'},
            metadata={'cancel_token': token}), config=Config(paths=WorkspacePaths(tmp_path)))
    assert seen == [token]
