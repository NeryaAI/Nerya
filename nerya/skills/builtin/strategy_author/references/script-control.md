# Script control and review workflow (installed SDK)

Use the actual installed SDK. There is currently no public `ctx.flow` or executable visual-edge API. Do not invent either. A workflow graph is a view of Python and manifest resources; the script's `if/elif/return` is the control flow.

## Stop before Agent work

In Agent execution mode every branch returns `StrategyAgentTask`:
- `stop(reason=..., path=..., metadata=...)` or the compatible `skip(reason=..., metadata=...)`: end this invocation without dispatching an Agent. stop returns the existing skip status; it does not create another execution engine.
- `error(reason=..., metadata=...)`: an invalid/failed read is not a successful no-signal branch.
- `dispatch(prompt=..., context=..., session_key=..., metadata=...)`: request Agent work only after the gate passes.

Use the constructors, not guessed dictionary statuses. The result states remain dispatch/skip/error; unknown dictionary states now produce an explicit error and never fall back to dispatch. For a script-only package use `ctx.result.skip/hold/ok/error` instead.

Always `return` a stop result through helper callers to the entrypoint. Creating a skip object without returning it does not terminate Python. A stop applies to this tick, not the schedule and not side effects already performed. Evaluate data quality, closed-candle conditions and dedupe before any model, team, order or external message call. Do not put an LLM call inside a condition meant to avoid LLM usage.

A valid skip creates a skipped run record, with a human-readable reason, and no Agent session/model call. Missing/stale data must be identified, not converted into no-cross success. Do not catch an exception and fall through into analysis or trading.

## Choose only the selected Python path

Keep each substantial branch in an actual helper when useful. Call only the chosen helper, and return its task. Do not build every branch eagerly in a dictionary of evaluated calls. Conditional imports are optional; importing a module is not evidence its function ran. Put no I/O at import time.

```python
from nerya.strategies import StrategyAgentTask
from signals import read_closed_signal
from opportunities import opportunity_task
from risks import risk_task


def run(ctx):
    try:
        signal = read_closed_signal(ctx)  # real data + configured parameters
        if not signal["crossed"]:
            return StrategyAgentTask.skip(
                reason="MACD 未交叉，本轮结束",
                metadata={"path": "stop", "bar_ts": signal["bar_ts"]},
            )
        # Persist dedupe using the SDK state/CAS example in workflows.md.
        # Do not mark delivered before there is delivery evidence.
        if signal["direction"] == "up":
            return opportunity_task(ctx, signal)
        if signal["direction"] == "down":
            return risk_task(ctx, signal)
        return StrategyAgentTask.error(reason="无法识别交叉方向")
    except Exception as exc:
        return StrategyAgentTask.error(reason=f"{type(exc).__name__}: {exc}")
```

This is a pattern, not a mandated MACD strategy. Implement the user's actual rule. Resolve the requested market, timeframe, indicator parameters, required history and candle completion from actual source configuration. Test true and false branches using the actual generated module, not a rewritten algorithm.

## Control what enters context

Build a small dictionary containing the chosen branch's actual evidence. Pass it through `dispatch(context=payload)`, not Python repr or a huge prompt. Preserve 0, false, empty lists and provenance. Separate instructions from data.

Use the invocation selectors on dispatch, not a string embedded in the prompt:

```python
return StrategyAgentTask.dispatch(
    prompt="Analyze this signal and explain the uncertainty. Do not trade.",
    path="risk_review",
    context={"signal": actual_signal},
    sources=[],              # no default raw data-source snapshots
    outputs=["risk_summary"], # only this previously published result
    include_trigger=False,   # do not append the trigger payload
    roles=["risk_critic"],   # actual declared role, then coordinator
)
```

Every selector is optional. None/omission inherits manifest defaults; [] means none. `sources` selects complete snapshots for declared data-source IDs, which can themselves contain several markets/timeframes. `outputs` selects exact names already published with ctx.inputs.publish and does not remove the explicitly supplied dispatch.context. Use `sources=[], outputs=[], include_trigger=False` to supply only your shaped context dictionary. For a particular subset of rows/fields, read the source, select actual values in Python, and include those through context rather than selecting its entire snapshot. Missing source/output identifiers are errors, not silently empty input. The configured include_script_outputs:false gate still suppresses both published outputs and explicit context; do not use it while expecting context to survive. Selection is an input contract, not a grant or revocation of tool/file access.

Choose source IDs, dimensions and payload schema in code/config, not from untrusted data. Do not include credentials or private unrelated history. The runtime stores the redacted context and a bounded prompt preview. A preview does not prove the model read the full artifact.

## Parallel Agents and route limits

The default long-running team is configured in agent_execution.team. The new dispatch.roles selector overrides the initial team's membership for this invocation: None inherits; [] skips the initial team and runs the coordinator; one role runs that role then the coordinator; several roles run through the existing parallel team runtime. Names must be declared subagents. Each selected role retains its instructions, tools and reviewed role policy. Neither metadata.roles nor a decorative workflow edge overrides this explicit selector.

Choose only the relevant helper with real if/elif/return, and let that helper construct the dispatch with its own roles/context/path. The path field records the human-readable branch and separates default market/timeframe sessions; explicitly chosen shared-session policies remain shared. Runtime input_selection and selected_roles records show what was actually chosen, not just what the graph allows. With arbitrary computed selectors the static graph cannot know the outcome in advance: say that the script decides at runtime, not that every role ran. The coordinator can still reason, call authorized tools or delegate subsequent work; initial role selection is not a global role-access restriction. Do not reduce it to one response to implement branching. No main-loop strategy-specific branching is needed.

ctx.subagents.run_many remains available for short script-owned tasks but consumes the short script deadline. Long analysis belongs after dispatch. Apply budget/role/Workspace/account restrictions normally.

## Review is a change-producing workflow

Use the existing tuning workflow: selected runtime evidence -> tuner instructions -> concrete proposed changes -> validation plan -> reviewable proposal -> separately approved application -> subsequent observation.

The frozen review snapshot includes `workflow_context.tasks`: strategy-local Agent tasks matched to the current package hash, requested scope and time window. It contains skips, selected paths, final output previews and references to captured inputs. Script tick metrics remain separate; `runs_considered:0` alone does not mean no Agent task ran. Missing or mismatched attribution is excluded with a reason. The current tuning runner uses explicit_payload_only scope with no tools. The frozen package_context.files includes allowed current source files with their content and hash, bounded by the configured source budget; excluded_files identifies gaps. The tuner can edit those supplied files by returning full replacements, but cannot fetch linked runtime artifacts itself. When more evidence is needed, have the main Agent inspect the references or supply it through the supported review-input mechanism; otherwise report the gap, rather than assuming the preview is complete.

Read the current package and evidence. When evidence is insufficient or no change is warranted, return no proposed_changes with a reason, rather than fabricate improvement. Observation-only strategies must not require closed trades as evidence just to permit a review. Do not label a configuration graph as completed execution.

For actual code changes, the tuner must return `proposed_changes` entries with a complete `after_content`; for strategy.yml use complete `config_after` or `yaml_after`. Prose suggestions or diff-only code_patch are advisory and are not an applyable package. Include rationale, a runnable validation plan and rollback context. Respect allowed_targets and forbidden_targets, and retain every unrelated file and field.

The existing evolution runner filters changes, writes before/after evidence and materializes a pending_review proposal. A validation plan is not a test execution, and pending_review is not applied. Inspect optimizer/validation output and proposal materialization; do not say code changed unless after files exist. No automatic approval, live activation, credential mutation or invented backtest results.

## Acceptance evidence

Verify no-signal, upward path, downward path, duplicate candle, open candle, stale/insufficient data and reader failure separately. Spy on real callers: unselected branch function count=0, no-signal model count=0, no-signal order attempts=0. Inspect the actual input context artifact to prove selected data is present and excluded data is absent. Test configured parameter edits and preserve old source IDs. For review verify both no-change hold and an actual materialized candidate, leaving the active package unchanged.

Keep run reason and branch name concise so users understand why a path ran. Python editor colors are syntax highlighting, not proof of runtime success. Keep model fixtures clearly labeled; never present them as real financial analysis.
