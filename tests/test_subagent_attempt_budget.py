"""Native children share the root loop's provider attempt accounting."""
from dataclasses import replace

import pytest

from nerya.core.errors import LLMError
from test_subagent_native_runtime import Gateway, final, runtime, spec, run

pytestmark = pytest.mark.smoke


@pytest.mark.parametrize("extra_attempts", [0, 1])
def test_transient_retry_obeys_shared_child_budget(tmp_path, extra_attempts):
    gateway = Gateway(LLMError("provider temporarily unavailable (503)"), final("recovered"))
    rt = runtime(tmp_path, gateway)
    rt.config.data["agent"]["subagents"] = {"max_extra_llm_attempts_per_run": extra_attempts}
    child = spec(tmp_path, max_skill_calls=0)
    child.execution_policy.llm_max_attempts = 3
    if not extra_attempts:
        with pytest.raises(LLMError, match="temporarily unavailable"):
            run(rt, child)
        assert len(gateway.calls) == 1
    else:
        result = run(rt, child)
        assert len(gateway.calls) == 2
        assert result["metrics"]["attempt_budget"]["used"] == 1
        assert result["output"]["summary"] == "recovered"
        assert result["completion_status"] == "complete"


def test_child_usage_records_all_native_calls_and_no_hidden_synthesis(tmp_path):
    from test_subagent_native_runtime import call, descriptor
    gateway = Gateway(call(), replace(final(), usd_cost=0.25))
    result = run(runtime(tmp_path, gateway, [descriptor()]), spec(tmp_path))
    assert len(gateway.calls) == len(result["model_calls"]) == 2
    assert result["tokens"] == sum(row["tokens"] for row in result["model_calls"])
    assert result["usd"] == pytest.approx(0.25)
    assert all(row["context_scope"] == "agent_loop" for row in result["model_calls"])
